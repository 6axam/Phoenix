from __future__ import annotations
import argparse
import json
import logging
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from .adapters import builtin_adapters
from .capture import capture_previews
from .core import Collector, DesktopRegistry, HyprlandBackend, SnapshotManager, config, is_locked, lock, trust_desktop, xdg, xdg_path
from .recovery import RecoveryExecutor
from .session import decided, decide
from .watch import watch_workspace_events


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog='phoenix', description='Безопасные снимки сессии Hyprland')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('status', help='Состояние базы и фонового процесса')
    snap = sub.add_parser('snapshot', help='Создать ручной снимок'); snap.add_argument('--name')
    sub.add_parser('history', help='История снимков')
    inspect = sub.add_parser('inspect', help='Проверить и показать снимок'); inspect.add_argument('snapshot', nargs='?', default='latest')
    restore = sub.add_parser('restore', help='Восстановить или показать план'); restore.add_argument('snapshot', nargs='?', default='latest'); restore.add_argument('--safe', action='store_true', help='Только безопасные действия (режим по умолчанию)'); restore.add_argument('--dry-run', action='store_true')
    sub.add_parser('doctor', help='Диагностика окружения')
    gui = sub.add_parser('gui', help='Открыть интерфейс'); gui.add_argument('--prompt', action='store_true', help=argparse.SUPPRESS)
    sub.add_parser('daemon', help='Запустить периодические снимки и запрос при входе')
    sub.add_parser('config', help='Показать настройки и путь')
    sub.add_parser('backup', help='Создать проверенную резервную копию')
    recovery_log = sub.add_parser('recovery-log', help='Журнал шагов восстановления'); recovery_log.add_argument('snapshot', nargs='?', default='latest')
    archive = sub.add_parser('archive', help='Просмотр и удаление архивов терминала')
    archive_sub = archive.add_subparsers(dest='archive_command', required=True)
    archive_list = archive_sub.add_parser('list'); archive_list.add_argument('snapshot', nargs='?', default='latest')
    archive_show = archive_sub.add_parser('show'); archive_show.add_argument('snapshot'); archive_show.add_argument('window', type=int); archive_show.add_argument('tab', type=int); archive_show.add_argument('pane', type=int)
    archive_purge = archive_sub.add_parser('purge'); archive_purge.add_argument('--confirm', action='store_true', required=True, help='Удалить все архивы из текущей базы')
    trust = sub.add_parser('trust-desktop', help='Доверять конкретному desktop-файлу до его изменения'); trust.add_argument('path', help='Абсолютный путь к desktop-файлу')
    return p


def dump(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def daemon(manager: SnapshotManager, backend: HyprlandBackend) -> int:
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    with lock(xdg('STATE') / 'daemon.lock', blocking=False):
        has_snapshot = bool(manager.history())
        if has_snapshot and not decided():
            try:
                # GUI owns the decision; closing or crashing leaves the collector paused.
                proc = subprocess.Popen([sys.executable, '-m', 'phoenix', 'gui', '--prompt'])
                while proc.poll() is None and not stop.wait(0.5):
                    pass
                if stop.is_set() and proc.poll() is None:
                    proc.terminate()
                    try: proc.wait(timeout=3)
                    except subprocess.TimeoutExpired: proc.kill(); proc.wait()
            except OSError as exc:
                logging.error('GUI unavailable: %s', exc)
        elif not has_snapshot and not decided():
            decide('skip')
        def capture_workspace() -> None:
            if decided():
                manager.record_workspace_captures(capture_previews(backend, config()))
        watcher = threading.Thread(target=watch_workspace_events, args=(stop, capture_workspace), name='phoenix-workspaces', daemon=True)
        watcher.start()
        while not stop.is_set():
            try:
                if decided():
                    collector = Collector(backend)
                    state = collector.collect()
                    if state['windows']:
                        manager.save(state, automatic=True, previews=capture_previews(backend, config()), archives=collector.archives)
                        for warning in manager.last_archive_warnings: logging.warning('%s', warning)
                    manager.backup_if_due()
                else:
                    logging.warning('Snapshots paused until a session decision is made')
            except Exception:
                logging.exception('Snapshot failed')
            interval = max(10, int(config().get('interval_seconds', 60)))
            stop.wait(interval)
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    args = parser().parse_args(argv)
    try:
        if args.command == 'config':
            dump({'path': str(xdg_path('CONFIG') / 'config.json'), 'settings': config()}); return 0
        readonly = args.command in ('status', 'history', 'inspect', 'doctor', 'recovery-log') or (args.command == 'restore' and args.dry_run) or (args.command == 'archive' and args.archive_command in ('list', 'show'))
        database = xdg_path('DATA') / 'snapshots.sqlite3'
        missing_readonly = readonly and not database.exists()
        if missing_readonly and args.command == 'history':
            dump([]); return 0
        if missing_readonly and args.command == 'status':
            dump({'daemon_running': is_locked(xdg_path('STATE') / 'daemon.lock'), 'database': str(database), 'integrity': 'not_created', 'snapshots': 0, 'session_decided': decided() if os.environ.get('HYPRLAND_INSTANCE_SIGNATURE') else None}); return 0
        manager = None if missing_readonly and args.command == 'doctor' else SnapshotManager(readonly=readonly)
        backend = HyprlandBackend()
        if args.command == 'snapshot':
            collector = Collector(backend); state = collector.collect()
            if not state['windows']: raise RuntimeError('Refusing to save an empty snapshot')
            print(manager.save(state, name=args.name, previews=capture_previews(backend, config()), archives=collector.archives))
            for warning in collector.adapter_warnings + manager.last_archive_warnings: print('phoenix:', warning, file=sys.stderr)
        elif args.command == 'history': dump(manager.history())
        elif args.command == 'inspect':
            row, state = manager.get(args.snapshot); dump({'id': row['id'], 'state': state})
        elif args.command == 'restore':
            row, state = manager.get(args.snapshot)
            result = RecoveryExecutor(backend, DesktopRegistry(), manager).restore(row['id'], state, args.dry_run)
            dump(result)
            if not args.dry_run:
                decide('restore')
                if any(x['status'] in ('error', 'timeout', 'unsupported', 'uncertain') for x in result): return 2
        elif args.command == 'status':
            running = is_locked(xdg_path('STATE') / 'daemon.lock')
            dump({'daemon_running': running, 'database': str(manager.path), 'integrity': manager.integrity(), 'snapshots': len(manager.history()), 'session_decided': decided() if os.environ.get('HYPRLAND_INSTANCE_SIGNATURE') else None})
        elif args.command == 'doctor':
            checks = {x: shutil.which(x) for x in ('hyprctl', 'kitty', 'tmux', 'gio', 'nautilus')}
            try: checks['hyprland_clients'] = len(backend.query('clients'))
            except Exception as e: checks['hyprland_error'] = str(e)
            try:
                import gi
                gi.require_version('Gtk', '4.0')
                from gi.repository import Gtk  # noqa: F401
                checks['gtk4'] = True
            except (ImportError, ValueError): checks['gtk4'] = False
            checks['integrity'] = manager.integrity() if manager else 'not_created'; checks['adapters'] = builtin_adapters(config()).diagnostics(); dump(checks)
        elif args.command == 'trust-desktop':
            from pathlib import Path
            trust_desktop(Path(args.path))
            print('Desktop entry trusted:', args.path)
        elif args.command == 'backup': print(manager.backup())
        elif args.command == 'recovery-log':
            row, _ = manager.get(args.snapshot); dump(manager.recovery_log(row['id']))
        elif args.command == 'archive':
            if args.archive_command == 'purge':
                manager.purge_archives(); print('Архивы удалены из текущей базы; резервные копии не изменены.')
            else:
                row, _ = manager.get(args.snapshot)
                if args.archive_command == 'list': dump(manager.archives(row['id']))
                else:
                    from .text import safe_text
                    print(safe_text(manager.archive_text(row['id'], args.window, args.tab, args.pane)), end='')
        elif args.command == 'daemon': return daemon(manager, backend)
        elif args.command == 'gui':
            from .gui import run
            return run(manager, backend, args.prompt)
        return 0
    except (OSError, RuntimeError, LookupError, ValueError, ImportError, BlockingIOError, sqlite3.Error) as exc:
        print(f'phoenix: {exc}', file=sys.stderr); return 1

if __name__ == '__main__':
    raise SystemExit(main())
