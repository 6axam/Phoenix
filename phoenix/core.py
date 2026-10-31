from __future__ import annotations

import configparser
import contextlib
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import stat
import time
from typing import Any, Iterator

LOG = logging.getLogger("phoenix")
SCHEMA = 1
DB_SCHEMA = 4


def xdg(kind: str) -> Path:
    defaults = {"DATA": ".local/share", "CONFIG": ".config", "STATE": ".local/state"}
    base = Path(os.environ.get("XDG_" + kind + "_HOME", Path.home() / defaults[kind]))
    path = base / "phoenix"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@contextlib.contextmanager
def lock(path: Path, blocking: bool = True) -> Iterator[None]:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        yield
    finally:
        os.close(fd)


class SnapshotManager:
    def __init__(self, path: Path | None = None):
        self.path = path or xdg("DATA") / "snapshots.sqlite3"
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        os.chmod(self.path, 0o600)
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > DB_SCHEMA:
                raise ValueError(f"Unsupported database schema {version}")
            if version == 0:
                db.executescript('''CREATE TABLE IF NOT EXISTS snapshots (id INTEGER PRIMARY KEY, created REAL NOT NULL, checked REAL NOT NULL, name TEXT, automatic INTEGER NOT NULL, schema_version INTEGER NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (snapshot_id INTEGER PRIMARY KEY, transaction_id TEXT NOT NULL, state TEXT NOT NULL, updated REAL NOT NULL, result TEXT NOT NULL);
                PRAGMA user_version=1;''')
                version = 1
            if version == 1:
                db.executescript('''BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS preview_images (digest TEXT PRIMARY KEY, data BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS snapshot_previews (snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE, workspace INTEGER NOT NULL, monitor TEXT NOT NULL, captured REAL NOT NULL, digest TEXT NOT NULL REFERENCES preview_images(digest), PRIMARY KEY(snapshot_id, workspace, monitor));
                PRAGMA user_version=2;
                COMMIT;''')
                version = 2
            if version == 2:
                db.executescript('''BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS workspace_captures (id INTEGER PRIMARY KEY, workspace INTEGER NOT NULL, monitor TEXT NOT NULL, captured REAL NOT NULL, digest TEXT NOT NULL REFERENCES preview_images(digest));
                CREATE INDEX IF NOT EXISTS workspace_captures_time ON workspace_captures(workspace, captured DESC);
                PRAGMA user_version=3;
                COMMIT;''')
                version = 3
            if version == 3:
                db.executescript('''BEGIN IMMEDIATE;
                ALTER TABLE snapshots ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;
                PRAGMA user_version=4;
                COMMIT;''')

    def connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=15000")
        return db

    def save(self, state: dict[str, Any], name: str | None = None, automatic: bool = False, previews: list[dict[str, Any]] | None = None) -> int:
        payload = canonical(state)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        now = time.time()
        previews = previews or []
        images = [(p, hashlib.sha256(p['data']).hexdigest()) for p in previews]
        with lock(self.path.with_suffix('.lock')), self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            last = db.execute("SELECT id,digest FROM snapshots WHERE automatic=1 ORDER BY id DESC LIMIT 1").fetchone() if automatic else None
            old_images = [(r['workspace'], r['monitor'], r['digest']) for r in db.execute('SELECT workspace, monitor, digest FROM snapshot_previews WHERE snapshot_id=? ORDER BY workspace,monitor', (last['id'],))] if last else []
            new_images = sorted((p['workspace'], p['monitor'], image_digest) for p, image_digest in images)
            if last and last['digest'] == digest and old_images == new_images:
                db.execute("UPDATE snapshots SET checked=? WHERE id=?", (now, last['id']))
                return int(last['id'])
            cursor = db.execute("INSERT INTO snapshots(created,checked,name,automatic,schema_version,payload,digest) VALUES(?,?,?,?,?,?,?)", (now, now, name, int(automatic), SCHEMA, payload, digest))
            snapshot_id = int(cursor.lastrowid or 0)
            for preview, image_digest in images:
                db.execute('INSERT OR IGNORE INTO preview_images VALUES(?,?)', (image_digest, preview['data']))
                db.execute('INSERT INTO snapshot_previews VALUES(?,?,?,?,?)', (snapshot_id, preview['workspace'], preview['monitor'], preview['captured'], image_digest))
            limit = max(1, min(500, int(config().get('snapshot_retention', 60))))
            db.execute("DELETE FROM snapshots WHERE automatic=1 AND pinned=0 AND id NOT IN (SELECT id FROM snapshots WHERE automatic=1 AND pinned=0 ORDER BY id DESC LIMIT ?)", (limit,))
            db.execute('DELETE FROM preview_images WHERE digest NOT IN (SELECT digest FROM snapshot_previews UNION SELECT digest FROM workspace_captures)')
            return snapshot_id

    def history(self) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT id,created,checked,name,automatic,schema_version,pinned FROM snapshots ORDER BY id DESC")]

    def get(self, identifier: str | int = 'latest') -> tuple[dict[str, Any], dict[str, Any]]:
        with self.connect() as db:
            if identifier == 'latest':
                rows = db.execute("SELECT * FROM snapshots ORDER BY id DESC").fetchall()
            else:
                rows = db.execute("SELECT * FROM snapshots WHERE id=?", (int(identifier),)).fetchall()
            for row in rows:
                try:
                    if row['schema_version'] != SCHEMA or hashlib.sha256(row['payload'].encode()).hexdigest() != row['digest']:
                        continue
                    state = json.loads(row['payload'])
                    if not isinstance(state, dict) or not isinstance(state.get('windows'), list):
                        continue
                    return dict(row), state
                except (ValueError, TypeError):
                    continue
        raise LookupError("No valid snapshot found")

    def previews(self, snapshot_id: int, include_previous: bool = False) -> list[dict[str, Any]]:
        with self.connect() as db:
            operator = '<=' if include_previous else '='
            rows = db.execute(f'SELECT p.snapshot_id,p.workspace,p.monitor,p.captured,i.data FROM snapshot_previews p JOIN preview_images i ON i.digest=p.digest WHERE p.snapshot_id {operator} ? ORDER BY p.snapshot_id DESC', (snapshot_id,))
            result: dict[tuple[int, str], dict[str, Any]] = {}
            for row in rows:
                key = (row['workspace'], row['monitor'])
                if key not in result:
                    result[key] = dict(row)
            return list(result.values())

    def record_workspace_captures(self, previews: list[dict[str, Any]]) -> None:
        if not previews:
            return
        with lock(self.path.with_suffix('.lock')), self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for preview in previews:
                digest = hashlib.sha256(preview['data']).hexdigest()
                db.execute('INSERT OR IGNORE INTO preview_images VALUES(?,?)', (digest, preview['data']))
                db.execute('INSERT INTO workspace_captures(workspace, monitor, captured, digest) VALUES(?,?,?,?)', (preview['workspace'], preview['monitor'], preview['captured'], digest))
            db.execute('DELETE FROM workspace_captures WHERE id NOT IN (SELECT id FROM workspace_captures ORDER BY id DESC LIMIT 240)')
            db.execute('DELETE FROM preview_images WHERE digest NOT IN (SELECT digest FROM snapshot_previews UNION SELECT digest FROM workspace_captures)')

    def earlier_workspace_captures(self, before: float) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT id,workspace,monitor,captured,digest,'event' AS source FROM workspace_captures WHERE captured <= ? UNION ALL SELECT snapshot_id AS id,workspace,monitor,captured,digest,'snapshot' AS source FROM snapshot_previews WHERE captured <= ? ORDER BY captured DESC,id DESC", (before, before))
            latest: dict[tuple[int, str], dict[str, Any]] = {}
            for row in rows:
                key = (row['workspace'], row['monitor'])
                if key not in latest:
                    latest[key] = dict(row)
            return list(latest.values())

    def capture_image(self, capture_id: int, source: str = 'event', workspace: int | None = None, monitor: str | None = None) -> bytes | None:
        with self.connect() as db:
            if source == 'snapshot':
                row = db.execute('SELECT i.data FROM snapshot_previews p JOIN preview_images i ON i.digest=p.digest WHERE p.snapshot_id=? AND p.workspace=? AND p.monitor=?', (capture_id, workspace, monitor)).fetchone()
            elif source == 'event':
                row = db.execute('SELECT i.data FROM workspace_captures c JOIN preview_images i ON i.digest=c.digest WHERE c.id=?', (capture_id,)).fetchone()
            else:
                raise ValueError('Invalid image source')
            return bytes(row['data']) if row else None

    def run_state(self, snapshot_id: int, transaction: str, state: str, result: list[dict[str, Any]]) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO runs VALUES(?,?,?,?,?) ON CONFLICT(snapshot_id) DO UPDATE SET transaction_id=excluded.transaction_id,state=excluded.state,updated=excluded.updated,result=excluded.result", (snapshot_id, transaction, state, time.time(), canonical(result)))

    def set_pinned(self, snapshot_id: int, pinned: bool) -> None:
        with lock(self.path.with_suffix('.lock')), self.connect() as db:
            if not db.execute('UPDATE snapshots SET pinned=? WHERE id=?', (int(pinned), snapshot_id)).rowcount:
                raise LookupError('Snapshot not found')

    def delete(self, snapshot_id: int) -> None:
        with lock(self.path.with_suffix('.lock')), self.connect() as db:
            if not db.execute('DELETE FROM snapshots WHERE id=?', (snapshot_id,)).rowcount:
                raise LookupError('Snapshot not found')
            db.execute('DELETE FROM runs WHERE snapshot_id=?', (snapshot_id,))
            db.execute('DELETE FROM preview_images WHERE digest NOT IN (SELECT digest FROM snapshot_previews UNION SELECT digest FROM workspace_captures)')

    def storage_bytes(self) -> int:
        return sum(p.stat().st_size for p in (self.path, Path(str(self.path)+'-wal'), Path(str(self.path)+'-shm')) if p.exists())

    def backup(self) -> Path:
        directory = self.path.parent / 'backups'
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        with lock(self.path.with_suffix('.backup.lock')):
            fd, temporary = tempfile.mkstemp(prefix='.backup-', suffix='.sqlite3', dir=directory)
            os.close(fd)
            try:
                with contextlib.closing(self.connect()) as source, contextlib.closing(sqlite3.connect(temporary)) as target:
                    source.backup(target)
                    if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise RuntimeError('Backup integrity check failed')
                with open(temporary, 'rb') as f: os.fsync(f.fileno())
                for i in (1, 0):
                    old = directory / f'snapshots-{i}.sqlite3'
                    if old.exists(): os.replace(old, directory / f'snapshots-{i+1}.sqlite3')
                final = directory / 'snapshots-0.sqlite3'
                os.replace(temporary, final)
                directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try: os.fsync(directory_fd)
                finally: os.close(directory_fd)
                return final
            finally:
                if os.path.exists(temporary): os.unlink(temporary)

    def backup_if_due(self, interval: int = 86400) -> bool:
        path = self.path.parent / 'backups' / 'snapshots-0.sqlite3'
        if path.exists() and time.time() - path.stat().st_mtime < interval:
            return False
        self.backup()
        return True

    def integrity(self) -> str:
        with self.connect() as db:
            return str(db.execute("PRAGMA integrity_check").fetchone()[0])


def config() -> dict[str, Any]:
    path = xdg('CONFIG') / 'config.json'
    if not path.exists():
        return {'exclude_classes': [], 'interval_seconds': 60}
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or not isinstance(data.get('exclude_classes', []), list):
        raise ValueError('Invalid configuration')
    return data


def update_config(changes: dict[str, Any]) -> None:
    allowed = {'capture_previews', 'snapshot_retention', 'interval_seconds'}
    if set(changes) - allowed:
        raise ValueError('Unknown setting')
    if 'capture_previews' in changes and not isinstance(changes['capture_previews'], bool):
        raise ValueError('Invalid capture setting')
    for key, low, high in [('snapshot_retention', 1, 500), ('interval_seconds', 10, 3600)]:
        if key in changes and (type(changes[key]) is not int or not low <= changes[key] <= high):
            raise ValueError('Invalid ' + key)
    directory = xdg('CONFIG')
    with lock(directory / 'config.lock'):
        settings = config(); settings.update(changes)
        fd, temporary = tempfile.mkstemp(prefix='.config-', dir=directory)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as file:
                json.dump(settings, file, ensure_ascii=False, indent=2); file.write('\n')
                file.flush(); os.fsync(file.fileno())
            os.replace(temporary, directory / 'config.json')
        finally:
            if os.path.exists(temporary): os.unlink(temporary)


class HyprlandBackend:
    def query(self, name: str) -> Any:
        if name not in ('clients', 'workspaces', 'monitors'):
            raise ValueError('Invalid query')
        try:
            result = subprocess.run(['hyprctl', '-j', name], capture_output=True, text=True, timeout=5, check=True)
            value = json.loads(result.stdout)
            if not isinstance(value, list):
                raise ValueError('Unexpected Hyprland response')
            return value
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise RuntimeError(f'Hyprland {name}: {exc}') from exc

    def dispatch(self, action: str, argument: str) -> None:
        # Construct Lua exclusively from validated numeric fields and a live address.
        simple = re.fullmatch(r"address:(0x[0-9a-fA-F]+)", argument)
        pair = re.fullmatch(r"(-?\d+),address:(0x[0-9a-fA-F]+)", argument)
        xy = re.fullmatch(r"exact (-?\d+) (-?\d+),address:(0x[0-9a-fA-F]+)", argument)
        full = re.fullmatch(r"([0-2]) -1,address:(0x[0-9a-fA-F]+)", argument)
        if action == 'movetoworkspacesilent' and pair and int(pair[1]) > 0:
            expression = f'hl.dsp.window.move({{window="address:{pair[2]}", workspace="{pair[1]}", follow=false}})'
        elif action in ('setfloating', 'settiled') and simple:
            flag = 'set' if action == 'setfloating' else 'unset'
            expression = f'hl.dsp.window.float({{window="address:{simple[1]}", action="{flag}"}})'
        elif action in ('movewindowpixel', 'resizewindowpixel') and xy:
            operation = 'move' if action == 'movewindowpixel' else 'resize'
            expression = f'hl.dsp.window.{operation}({{window="address:{xy[3]}", x={int(xy[1])}, y={int(xy[2])}, relative=false}})'
        elif action == 'fullscreenstate' and full:
            expression = f'hl.dsp.window.fullscreen_state({{window="address:{full[2]}", internal={full[1]}, client=-1, action="set"}})'
        else:
            raise ValueError('Unsupported or invalid dispatcher argument')
        result = subprocess.run(['hyprctl', 'eval', 'hl.dispatch(' + expression + ')'], capture_output=True, text=True, timeout=5, check=True)
        if result.stdout.strip() != 'ok':
            raise RuntimeError(result.stdout.strip() or result.stderr.strip())


def read_cwd(pid: Any) -> str | None:
    try:
        n = int(pid)
        if n <= 0 or Path(f'/proc/{n}').stat().st_uid != os.getuid():
            return None
        return os.readlink(f'/proc/{n}/cwd')
    except (OSError, ValueError, TypeError):
        return None


def shell_cwd(kitty_pid: Any) -> str | None:
    """Return cwd only when exactly one local shell is identifiable below Kitty."""
    try:
        root = int(kitty_pid)
        if root <= 0 or Path(f'/proc/{root}').stat().st_uid != os.getuid():
            return None
        todo, seen, shells = [root], set(), []
        while todo and len(seen) < 256:
            pid = todo.pop()
            if pid in seen: continue
            seen.add(pid)
            proc = Path(f'/proc/{pid}')
            try:
                if proc.stat().st_uid != os.getuid(): continue
                if pid != root and (proc / 'comm').read_text().strip() in {'bash', 'zsh', 'fish', 'sh'}:
                    shells.append(pid)
                children = (proc / 'task' / str(pid) / 'children').read_text().split()
                todo.extend(int(child) for child in children)
            except (OSError, ValueError):
                continue
        return read_cwd(shells[0]) if len(shells) == 1 else None
    except (OSError, ValueError, TypeError):
        return None


class Collector:
    def __init__(self, backend: HyprlandBackend, settings: dict[str, Any] | None = None):
        self.backend, self.settings = backend, settings if settings is not None else config()

    def collect(self) -> dict[str, Any]:
        clients = self.backend.query('clients')
        monitors = self.backend.query('monitors')
        workspaces = self.backend.query('workspaces')
        monitor_names = {m.get('id'): m.get('name') for m in monitors}
        excluded = {str(x).casefold() for x in self.settings.get('exclude_classes', [])}
        windows = []
        kitty_pids = [c.get('pid') for c in clients if str(c.get('class', '')).casefold() == 'kitty']
        for c in clients:
            cls = str(c.get('class') or '')
            if not cls or cls.casefold() == 'org.phoenix.session' or cls.casefold() in excluded or not c.get('mapped', True):
                continue
            w: dict[str, Any] = {'class': cls, 'initial_class': str(c.get('initialClass') or cls), 'workspace': c.get('workspace', {}).get('id'), 'monitor': monitor_names.get(c.get('monitor')), 'floating': bool(c.get('floating')), 'fullscreen': int(c.get('fullscreen', 0)), 'at': c.get('at'), 'size': c.get('size')}
            # The process cwd is useful for Kitty but is not necessarily its active pane.
            if cls.casefold() == 'kitty':
                cwd = shell_cwd(c.get('pid')) if kitty_pids.count(c.get('pid')) == 1 else None
                if cwd: w.update(cwd=cwd, cwd_source='single_local_shell')
            windows.append(w)
        windows.sort(key=canonical)
        return {'schema': SCHEMA, 'windows': windows, 'monitors': [{k: m.get(k) for k in ('name', 'x', 'y', 'width', 'height', 'scale', 'transform')} for m in sorted(monitors, key=lambda item: str(item.get('name', '')))], 'workspaces': [{'id': w.get('id'), 'name': w.get('name'), 'monitor': w.get('monitor')} for w in sorted(workspaces, key=lambda item: str(item.get('id', '')))]}


def desktop_fingerprint(path: Path) -> str:
    """Hash a regular desktop file owned by this user or root, without following symlinks."""
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid not in (0, os.getuid()) or info.st_mode & 0o022:
            raise ValueError('Desktop file must be owned by you or root and not writable by others')
        with os.fdopen(fd, 'rb', closefd=False) as file:
            return hashlib.file_digest(file, 'sha256').hexdigest()
    finally:
        os.close(fd)


def trust_desktop(path: Path) -> None:
    """Pin an explicitly selected desktop entry in Phoenix configuration."""
    if not path.is_absolute() or path.suffix != '.desktop':
        raise ValueError('An absolute .desktop path is required')
    fingerprint = desktop_fingerprint(path)
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read(path, encoding='utf-8')
    if parser.get('Desktop Entry', 'Type', fallback='') != 'Application':
        raise ValueError('Not an application desktop entry')
    directory = xdg('CONFIG')
    with lock(directory / 'config.lock'):
        settings = config()
        trusted = settings.setdefault('trusted_desktop_entries', {})
        if not isinstance(trusted, dict):
            raise ValueError('Invalid trusted_desktop_entries')
        trusted[str(path)] = fingerprint
        fd, temporary = tempfile.mkstemp(prefix='.config-', dir=directory)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as file:
                json.dump(settings, file, indent=2, ensure_ascii=False)
                file.write('\n'); file.flush(); os.fsync(file.fileno())
            os.replace(temporary, directory / 'config.json')
        finally:
            if os.path.exists(temporary): os.unlink(temporary)


class DesktopRegistry:
    """Resolve system entries or explicitly pinned entries, never snapshot commands."""
    def __init__(self, directories: list[Path] | None = None, trusted: dict[str, str] | None = None):
        self.directories = directories if directories is not None else [Path('/usr/share/applications'), Path('/usr/local/share/applications')]
        self.trusted = trusted if trusted is not None else config().get('trusted_desktop_entries', {})
        if not isinstance(self.trusted, dict):
            raise ValueError('Invalid trusted_desktop_entries')

    def allowed(self, path: Path) -> bool:
        try:
            digest = desktop_fingerprint(path)
            owner = path.lstat().st_uid
            if str(path) in self.trusted:
                return self.trusted[str(path)] == digest
            return owner == 0 and path.parent in self.directories
        except (OSError, ValueError):
            return False

    def find(self, cls: str) -> Path | None:
        target = cls.casefold()
        paths = [path for directory in self.directories for path in sorted(directory.glob('*.desktop'))]
        paths.extend(Path(path) for path in self.trusted if Path(path) not in paths)
        for path in paths:
            try:
                if not self.allowed(path):
                    continue
                parser = configparser.ConfigParser(interpolation=None, strict=False)
                parser.read(path, encoding='utf-8')
                entry = parser['Desktop Entry']
                if entry.get('Type') != 'Application' or entry.getboolean('Hidden', fallback=False):
                    continue
                if target in {path.stem.casefold(), entry.get('StartupWMClass', '').casefold()}:
                    return path
            except (OSError, configparser.Error, KeyError, ValueError):
                continue
        return None

    def launch_kitty(self, entry: Path, cwd: str) -> bool:
        binary = Path('/usr/bin/kitty')
        try:
            st = binary.stat()
            directory = Path(cwd)
            if not self.allowed(entry) or st.st_uid != 0 or st.st_mode & 0o022 or not directory.is_absolute() or not directory.is_dir():
                return False
            subprocess.Popen([str(binary), '--directory', str(directory)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return True
        except OSError:
            return False

    def launch(self, entry: Path) -> None:
        if not self.allowed(entry):
            raise ValueError('Desktop entry changed or is no longer trusted')
        subprocess.Popen(['gio', 'launch', str(entry)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
