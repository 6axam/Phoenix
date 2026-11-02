"""Kitty remote control: only local user-approved sockets and safe metadata."""
from __future__ import annotations
import json
import os
from pathlib import Path
import selectors
import shlex
import shutil
import socket
import stat
import struct
import subprocess
import time
from typing import Any
from .base import AdapterResult, PlanStep, Status

MAX_JSON = 4 * 1024 * 1024
MAX_PANELS = 64
MAX_TABS = 32
MAX_PANE = 128 * 1024 * 1024
MAX_CAPTURE_BATCH = 64 * 1024 * 1024
LAYOUTS = {'tall', 'fat', 'grid', 'horizontal', 'vertical', 'splits', 'stack'}
PROCESS_KINDS = {'shell', 'codex', 'tmux', 'ssh', 'monitor', 'editor', 'other', 'unknown'}
PROCESS_NAMES = {
    'bash': 'shell', 'zsh': 'shell', 'fish': 'shell', 'sh': 'shell', 'dash': 'shell',
    'codex': 'codex', 'tmux': 'tmux', 'ssh': 'ssh',
    'btop': 'monitor', 'htop': 'monitor', 'top': 'monitor',
    'vim': 'editor', 'nvim': 'editor', 'vi': 'editor', 'nano': 'editor',
}


def foreground_kind(processes: Any) -> str:
    """Only persist a coarse hint; it is never proof of identity or permission to run."""
    if not isinstance(processes, list) or not processes:
        return 'unknown'
    if len(processes) != 1 or not isinstance(processes[0], dict):
        return 'other'
    cmdline = processes[0].get('cmdline')
    if not isinstance(cmdline, list) or not cmdline or not isinstance(cmdline[0], str):
        return 'other'
    name = os.path.basename(cmdline[0]).lstrip('-')
    return PROCESS_NAMES.get(name, 'other')


class KittyError(RuntimeError):
    pass


class KittyTruncated(KittyError):
    def __init__(self, data: bytes):
        super().__init__('Terminal history exceeded the limit; saved the most recent available text')
        self.data = data


def secure_socket(path: str) -> str:
    p = Path(path)
    if not p.is_absolute() or p.is_symlink():
        raise KittyError('Kitty socket must be an absolute non-symlink path')
    try:
        info, parent = p.stat(), p.parent.stat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise KittyError('Kitty socket must belong to the current user')
        if parent.st_uid != os.getuid() or parent.st_mode & 0o077 or p.parent.is_symlink():
            raise KittyError('Kitty socket directory must be owned by you and private (0700)')
    except OSError as exc:
        raise KittyError('Kitty socket is unavailable') from exc
    return str(p)


def bounded_command(argv: list[str], limit: int, timeout: float) -> bytes:
    """Bound time and stdout while discarding stderr, which may contain private data."""
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    assert process.stdout is not None
    output = bytearray(); deadline = time.monotonic() + timeout
    tail = 'get-text' in argv
    total = 0; truncated = False
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0: raise KittyError('Kitty request timed out')
                if not selector.select(remaining): raise KittyError('Kitty request timed out')
                chunk = os.read(process.stdout.fileno(), 65536 if tail else min(65536, limit + 1 - len(output)))
                if not chunk: break
                total += len(chunk)
                if tail and total > min(128 * 1024**2, max(limit * 4, limit + 4 * 1024**2)):
                    raise KittyError('Kitty response exceeded the bounded input budget')
                output.extend(chunk)
                if len(output) > limit:
                    if not tail: raise KittyError('Kitty response exceeded the configured limit')
                    del output[:len(output) - limit]
                    truncated = True
        if process.wait(timeout=max(.1, deadline - time.monotonic())) != 0:
            raise KittyError('Kitty remote control denied or unavailable')
        if truncated: raise KittyTruncated(bytes(output))
        return bytes(output)
    except (OSError, subprocess.SubprocessError) as exc:
        raise KittyError('Kitty request failed') from exc
    finally:
        if process.poll() is None:
            process.kill(); process.wait()
        process.stdout.close()


class KittyAdapter:
    name = 'kitty'
    def __init__(self, settings: dict[str, Any], runner: Any = bounded_command):
        self.settings, self.runner = settings, runner

    def matches(self, window: dict[str, Any]) -> bool:
        return str(window.get('class', '')).casefold() == 'kitty'

    def socket(self) -> str:
        paths = self.settings.get('kitty_sockets', [])
        first = next((item for item in paths if isinstance(item, str) and item), '') if isinstance(paths, list) else ''
        path = self.settings.get('kitty_socket') or os.environ.get('PHOENIX_KITTY_SOCKET', '') or first
        if not isinstance(path, str) or not path:
            raise KittyError('Set a private Kitty socket in Phoenix settings')
        return secure_socket(path)

    def peer_pid(self) -> int:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(2)
                connection.connect(self.socket())
                pid, uid, _ = struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.getuid() or pid <= 0:
                raise KittyError('Kitty socket peer does not belong to current user')
            return pid
        except OSError as exc:
            raise KittyError('Kitty socket peer is unavailable') from exc

    def availability(self) -> AdapterResult:
        if not self.settings.get('kitty_enabled', False):
            return AdapterResult(Status.SKIPPED, warnings=['Detailed Kitty collection is disabled'])
        if not shutil.which('kitten') or not shutil.which('kitty'):
            return AdapterResult(Status.UNSUPPORTED, warnings=['kitty or kitten is missing'])
        primary = self.settings.get('kitty_socket') or os.environ.get('PHOENIX_KITTY_SOCKET', '')
        extras = self.settings.get('kitty_sockets', [])
        paths = list(dict.fromkeys(p for p in [primary, *(extras if isinstance(extras, list) else [])] if isinstance(p, str) and p))[:16]
        if not paths:
            return AdapterResult(Status.UNSUPPORTED, warnings=['Set a private Kitty socket in Phoenix settings'])
        available = 0
        for path in paths:
            try:
                secure_socket(path); available += 1
            except KittyError:
                continue
        if not available:
            return AdapterResult(Status.UNSUPPORTED, warnings=['No configured Kitty socket is available or private'])
        return AdapterResult(Status.PARTIAL, warnings=[f'{available}/{len(paths)} Kitty sockets available; authorization is checked on collection'])

    def request(self, command: list[str], limit: int, timeout: float = 5) -> bytes:
        try:
            return self.runner(['kitten', '@', '--to', 'unix:' + self.socket(), *command], limit, timeout)
        except (OSError, ValueError) as exc:
            raise KittyError('Kitty remote control is unavailable') from exc

    def collect(self, collect_text: bool = False, expected_pid: int | None = None, deadline: float | None = None, memory_budget: int = MAX_CAPTURE_BATCH) -> tuple[AdapterResult, list[dict[str, Any]]]:
        if not self.settings.get('kitty_enabled', False):
            return AdapterResult(Status.SKIPPED), []
        try:
            if expected_pid is not None and self.peer_pid() != expected_pid:
                raise KittyError('Kitty socket does not belong to the matching window process')
            deadline = min(deadline or time.monotonic() + 15, time.monotonic() + 15)
            remaining = deadline - time.monotonic()
            if remaining <= 0: raise KittyError('Kitty collection time limit reached')
            os_windows = json.loads(self.request(['ls'], MAX_JSON, timeout=min(5, remaining)))
            if not isinstance(os_windows, list) or len(os_windows) != 1:
                return AdapterResult(Status.PARTIAL, warnings=['Kitty OS windows cannot be matched unambiguously']), []
            tabs = os_windows[0].get('tabs', [])
            if not isinstance(tabs, list) or not 1 <= len(tabs) <= MAX_TABS:
                raise KittyError('Invalid or oversized Kitty tab list')
            out: dict[str, Any] = {'version': 1, 'tabs': [], 'active_tab': 0}
            archives: list[dict[str, Any]] = []
            excluded = {int(i) for i in self.settings.get('kitty_exclude_panels', []) if str(i).isdigit()}
            blocked = [str(s) for s in self.settings.get('kitty_exclude_text', []) if isinstance(s, str) and s]
            limit = max(1024, min(MAX_PANE, int(self.settings.get('kitty_scrollback_limit', 32 * 1024 * 1024))))
            total = 0; archive_bytes = 0; warnings: list[str] = []
            for ti, tab in enumerate(tabs):
                windows = tab.get('windows', [])
                if not isinstance(windows, list) or not windows: raise KittyError('Invalid Kitty panel list')
                total += len(windows)
                if total > MAX_PANELS: raise KittyError('Too many Kitty panels')
                layout = str(tab.get('layout', 'tall')).split(':', 1)[0]
                entry: dict[str, Any] = {'layout': layout if layout in LAYOUTS else 'tall', 'active_pane': 0, 'panes': []}
                if tab.get('is_active'): out['active_tab'] = ti
                for pi, pane in enumerate(windows):
                    if pane.get('is_active'): entry['active_pane'] = pi
                    cwd = pane.get('cwd')
                    # Never persist command lines, environment, titles, or remote host data.
                    foreground = pane.get('foreground_processes') or []
                    data = {'cwd': cwd if isinstance(cwd, str) and cwd.startswith('/') and len(cwd) <= 4096 else None, 'process': foreground_kind(foreground), 'scrollback_capture': 'disabled' if not collect_text else 'unavailable'}
                    entry['panes'].append(data)
                    pid = pane.get('id')
                    if collect_text and isinstance(pid, int) and pid in excluded:
                        data['scrollback_capture'] = 'excluded'
                    if collect_text and isinstance(pid, int) and pid > 0 and pid not in excluded:
                        try:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0: raise KittyError('Kitty collection time limit reached')
                            batch_remaining = min(MAX_CAPTURE_BATCH, max(0, memory_budget)) - archive_bytes
                            if batch_remaining < 1024: raise KittyError('Kitty collection memory budget reached')
                            truncated = False
                            try:
                                text = self.request(['get-text', '--match', f'id:{pid}', '--extent', 'all'], min(limit, batch_remaining), timeout=min(4, remaining))
                            except KittyTruncated as exc:
                                text = exc.data
                                truncated = True
                                warnings.append(f'Tab {ti+1}, pane {pi+1}: {exc}')
                            if any(s.encode('utf-8') in text for s in blocked):
                                data['scrollback_capture'] = 'excluded'
                                warnings.append(f'Tab {ti+1}, pane {pi+1}: excluded by content rule')
                            elif text:
                                data['scrollback_capture'] = 'truncated' if truncated else 'captured'
                                archives.append({'tab': ti, 'pane': pi, 'data': text})
                                archive_bytes += len(text)
                        except KittyError as exc:
                            data['scrollback_capture'] = 'failed'
                            warnings.append(f'Tab {ti+1}, pane {pi+1}: {exc}')
                out['tabs'].append(entry)
            if not self.validate(out): raise KittyError('Invalid Kitty state')
            warnings.append('Process memory, exact split sizes and commands are not restored')
            return AdapterResult(Status.PARTIAL, out, warnings), archives
        except (KittyError, OSError, json.JSONDecodeError, UnicodeError, TypeError, ValueError, AttributeError) as exc:
            return AdapterResult(Status.UNSUPPORTED, warnings=[str(exc)]), []

    def validate(self, data: Any) -> bool:
        if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('tabs'), list) or not 1 <= len(data['tabs']) <= MAX_TABS:
            return False
        active = data.get('active_tab')
        if type(active) is not int or not 0 <= active < len(data['tabs']): return False
        count = 0
        for tab in data['tabs']:
            if not isinstance(tab, dict) or tab.get('layout') not in LAYOUTS or not isinstance(tab.get('panes'), list) or not tab['panes']: return False
            count += len(tab['panes'])
            if type(tab.get('active_pane')) is not int or not 0 <= tab['active_pane'] < len(tab['panes']): return False
            for pane in tab['panes']:
                if not isinstance(pane, dict) or not isinstance(pane.get('process'), str) or pane['process'] not in PROCESS_KINDS: return False
                cwd = pane.get('cwd')
                if cwd is not None and (not isinstance(cwd, str) or len(cwd) > 4096 or not cwd.startswith('/')): return False
        return count <= MAX_PANELS

    def plan(self, data: dict[str, Any]) -> list[PlanStep]:
        if not self.validate(data): return [PlanStep('kitty', 'kitty', status='unavailable', detail='Invalid Kitty state')]
        steps = [PlanStep('workspace', 'workspace'), PlanStep('kitty', 'kitty OS window', ['workspace'])]
        for ti, tab in enumerate(data['tabs']):
            steps.append(PlanStep(f'tab:{ti}', 'tab', ['kitty'], status='partial' if len(tab['panes']) > 1 else 'available', detail='Panel split tree and exact sizes are not documented in the session interface' if len(tab['panes']) > 1 else ''))
            for pi, _ in enumerate(tab['panes']):
                ident = f'pane:{ti}:{pi}'
                cwd = tab['panes'][pi].get('cwd')
                usable = isinstance(cwd, str) and '$' not in cwd and all(ord(c) >= 32 and ord(c) != 127 for c in cwd) and Path(cwd).is_dir()
                steps.append(PlanStep(ident, 'shell pane and directory', [f'tab:{ti}'], status='available' if usable else 'partial', detail='' if usable else 'Working directory unavailable or unsafe; default directory will be used'))
                steps.append(PlanStep(f'archive:{ti}:{pi}', 'view archived scrollback', status='unknown', detail='Stored separately; availability depends on the archive'))
                kind = tab['panes'][pi].get('process', 'unknown')
                steps.append(PlanStep(f'process:{ti}:{pi}', 'previous process', [ident], status='unavailable', requires_confirmation=True, detail=f'Observed process category: {kind}; identity unverified and automatic execution unsupported'))
        return steps

    def serialize(self, data: dict[str, Any]) -> dict[str, Any]:
        if not self.validate(data): raise KittyError('Invalid Kitty state')
        return json.loads(json.dumps(data, ensure_ascii=False))

    def execute(self, data: dict[str, Any], launch: Any) -> AdapterResult:
        try:
            session = self.session(data)
            if not launch(session):
                return AdapterResult(Status.FAILED, warnings=['Kitty session could not be launched'])
            warnings = ['Shells launched; previous programs and exact split geometry are not restored']
            if any(pane.get('cwd') and ('$' in pane['cwd'] or not Path(pane['cwd']).is_dir() or any(ord(c) < 32 or ord(c) == 127 for c in pane['cwd'])) for tab in data['tabs'] for pane in tab['panes']):
                warnings.append('Some working directories were unavailable or unsafe and were not used')
            return AdapterResult(Status.PARTIAL, warnings=warnings)
        except (KittyError, OSError, ValueError):
            return AdapterResult(Status.FAILED, warnings=['Kitty session could not be launched'])

    def select(self, data: dict[str, Any], selected: set[tuple[int, int]]) -> dict[str, Any] | None:
        if not self.validate(data):
            raise KittyError('Invalid Kitty state')
        tabs = []
        active_tab = 0
        for ti, tab in enumerate(data['tabs']):
            included = [(pi, pane) for pi, pane in enumerate(tab['panes']) if (ti, pi) in selected]
            if not included:
                continue
            if ti == data['active_tab']: active_tab = len(tabs)
            active_pane = next((i for i, (pi, _) in enumerate(included) if pi == tab['active_pane']), 0)
            tabs.append({'layout': tab['layout'], 'active_pane': active_pane, 'panes': [dict(pane) for _, pane in included]})
        return {'version': 1, 'tabs': tabs, 'active_tab': active_tab} if tabs else None

    def session(self, data: dict[str, Any]) -> str:
        if not self.validate(data): raise KittyError('Invalid Kitty state')
        lines = []
        for ti, tab in enumerate(data['tabs']):
            lines += [f'new_tab Terminal {ti + 1}', 'layout ' + tab['layout']]
            for pi, pane in enumerate(tab['panes']):
                cwd = pane.get('cwd')
                # Session files expand $ variables; skip paths with special expansion or controls.
                if isinstance(cwd, str) and '$' not in cwd and all(ord(c) >= 32 and ord(c) != 127 for c in cwd) and Path(cwd).is_dir():
                    lines.append('launch --cwd=' + shlex.quote(cwd))
                else:
                    lines.append('launch')
                if pi == tab['active_pane']: lines.append('focus')
        lines.append('focus_tab ' + str(data['active_tab']))
        return '\n'.join(lines) + '\n'
