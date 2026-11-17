"""Read a tmux server's structural metadata, without executing pane commands."""
from __future__ import annotations
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time
import unicodedata
from typing import Any
from .kitty import bounded_command, KittyError

SEP = '\x1f'
SESSION_FORMAT = SEP.join(('#{session_id}', '#{session_name}', '#{session_windows}'))
WINDOW_FORMAT = SEP.join(('#{session_id}', '#{window_id}', '#{window_index}', '#{window_name}', '#{window_active}', '#{window_width}', '#{window_height}'))
PANE_FORMAT = SEP.join(('#{session_id}', '#{window_id}', '#{pane_id}', '#{pane_index}', '#{pane_active}', '#{pane_current_path}', '#{pane_left}', '#{pane_top}', '#{pane_width}', '#{pane_height}'))


class TmuxError(RuntimeError):
    pass


def secure_tmux_socket(path: str) -> str:
    p = Path(path)
    if not p.is_absolute() or p.is_symlink() or p.parent.is_symlink(): raise TmuxError('Укажите абсолютный путь к приватному сокету tmux')
    try:
        sock, parent = p.stat(), p.parent.stat()
        if not stat.S_ISSOCK(sock.st_mode) or sock.st_uid != os.getuid() or sock.st_mode & 0o077 or parent.st_uid != os.getuid() or parent.st_mode & 0o077:
            raise TmuxError('Сокет tmux и его каталог должны быть приватными и принадлежать пользователю')
    except OSError:
        raise TmuxError('Сокет tmux недоступен') from None
    return str(p)


def _rows(raw: bytes, columns: int, limit: int) -> list[list[str]]:
    try: lines = raw.decode('utf-8').splitlines()
    except UnicodeError: raise TmuxError('tmux вернул недопустимый текст') from None
    if len(lines) > limit: raise TmuxError('Слишком много объектов tmux')
    result = []
    for line in lines:
        parts = line.split(SEP)
        if len(parts) != columns or any(any(unicodedata.category(c) in ('Cc', 'Cf') for c in part) for part in parts):
            raise TmuxError('Метаданные tmux содержат неподдерживаемые символы')
        result.append(parts)
    return result


def _number(value: str, low: int, high: int) -> int:
    if not re.fullmatch(r'[0-9]{1,8}', value): raise TmuxError('Некорректные числовые метаданные tmux')
    number = int(value)
    if not low <= number <= high: raise TmuxError('Числовые метаданные tmux вне допустимого диапазона')
    return number


def collect_tmux(socket_path: str, *, runner=bounded_command, timeout: float = 10) -> dict[str, Any]:
    socket_path = secure_tmux_socket(socket_path)
    binary = shutil.which('tmux')
    if not binary: raise TmuxError('tmux не найден')
    deadline = time.monotonic() + timeout
    formats = (('list-sessions', [], SESSION_FORMAT, 3, 32), ('list-windows', ['-a'], WINDOW_FORMAT, 7, 128), ('list-panes', ['-a'], PANE_FORMAT, 10, 512))
    def read() -> list[list[list[str]]]:
        result = []
        for command, options, fmt, columns, limit in formats:
            remaining = deadline - time.monotonic()
            if remaining <= 0: raise TmuxError('Сбор tmux превысил лимит времени')
            try: raw = runner([binary, '-S', socket_path, '-f', '/dev/null', command, *options, '-F', fmt], 1024 * 1024, min(4, remaining))
            except (KittyError, OSError, subprocess.SubprocessError): raise TmuxError('Не удалось безопасно прочитать состояние tmux') from None
            result.append(_rows(raw, columns, limit))
        return result
    first = read(); second = read()
    if first != second: raise TmuxError('Состояние tmux изменилось во время сбора; повторите попытку')
    sessions: dict[str, dict[str, Any]] = {}; windows: dict[tuple[str, str], dict[str, Any]] = {}
    for sid, name, count in first[0]:
        if not re.fullmatch(r'\$[0-9]+', sid) or not name or len(name) > 256 or sid in sessions: raise TmuxError('Некорректная сессия tmux')
        sessions[sid] = {'name': name, 'expected_windows': _number(count, 1, 128), 'windows': []}
    for sid, wid, idx, name, active, width, height in first[1]:
        if sid not in sessions or not re.fullmatch(r'@[0-9]+', wid) or (sid, wid) in windows or not name or len(name) > 256: raise TmuxError('Некорректное окно tmux')
        item = {'index': _number(idx, 0, 999999), 'name': name, 'active': bool(_number(active, 0, 1)), 'width': _number(width, 1, 65535), 'height': _number(height, 1, 65535), 'panes': []}
        windows[sid, wid] = item; sessions[sid]['windows'].append(item)
    seen_panes: set[tuple[str, str, str]] = set()
    for sid, wid, pid, idx, active, cwd, x, y, width, height in first[2]:
        if (sid, wid) not in windows or not re.fullmatch(r'%[0-9]+', pid) or (sid, wid, pid) in seen_panes or not cwd.startswith('/') or len(cwd) > 4096: raise TmuxError('Некорректная панель tmux')
        seen_panes.add((sid, wid, pid)); item = windows[sid, wid]
        pane = {'index': _number(idx, 0, 999999), 'active': bool(_number(active, 0, 1)), 'cwd': cwd, 'x': _number(x, 0, 65535), 'y': _number(y, 0, 65535), 'width': _number(width, 1, 65535), 'height': _number(height, 1, 65535)}
        if pane['x'] + pane['width'] > item['width'] or pane['y'] + pane['height'] > item['height']: raise TmuxError('Геометрия tmux выходит за границы окна')
        item['panes'].append(pane)
    output = []
    for session in sessions.values():
        entries = session['windows']; expected = session.pop('expected_windows')
        if len(entries) != expected or len({w['index'] for w in entries}) != len(entries) or sum(w['active'] for w in entries) != 1: raise TmuxError('Состояние окон tmux неполное')
        for item in entries:
            panes = item['panes']
            if not panes or len({p['index'] for p in panes}) != len(panes) or sum(p['active'] for p in panes) != 1: raise TmuxError('Состояние панелей tmux неполное')
            for i, a in enumerate(panes):
                for b in panes[i + 1:]:
                    if max(a['x'], b['x']) < min(a['x'] + a['width'], b['x'] + b['width']) and max(a['y'], b['y']) < min(a['y'] + a['height'], b['y'] + b['height']):
                        raise TmuxError('Панели tmux перекрываются; восстановление не поддерживается')
            panes.sort(key=lambda pane: pane['index'])
        entries.sort(key=lambda item: item['index']); output.append(session)
    return {'version': 1, 'sessions': sorted(output, key=lambda session: session['name']), 'note': 'Только структурные метаданные; процессы и команды не сохранены'}
