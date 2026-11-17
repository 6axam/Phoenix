"""Explicit, opt-in Codex-to-Kitty panel markers; never infer a conversation."""
from __future__ import annotations
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any
from .codex_session import CodexCheckError, check_session, normalize_id

VAR = 'phoenix_codex_binding'


def _identity(pid: int) -> tuple[int, int]:
    """Return Linux process parent and start ticks, without reading its environment."""
    if type(pid) is not int or pid < 2 or Path(f'/proc/{pid}').stat().st_uid != os.getuid():
        raise ValueError('Invalid process')
    data = Path(f'/proc/{pid}/stat').read_text()
    parts = data[data.rfind(')') + 2:].split()
    return int(parts[1]), int(parts[19])


def _user_var(value: str | None) -> bytes:
    if value is None: return f'\x1b]1337;SetUserVar={VAR}\x07'.encode()
    encoded = base64.b64encode(value.encode()).decode('ascii')
    return f'\x1b]1337;SetUserVar={VAR}={encoded}\x07'.encode()


def _is_phoenix_wrapper(pid: int) -> bool:
    with Path(f'/proc/{pid}/cmdline').open('rb') as stream:
        data = stream.read(65537)
    if len(data) > 65536: return False
    args = data.split(b'\0')
    module = any(args[i:i + 2] == [b'-m', b'phoenix'] for i in range(len(args) - 1))
    script = len(args) > 1 and os.path.basename(args[1]) == b'phoenix'
    return (module or script) and (b'codex-resume' in args or b'codex-restore' in args)


def collect_binding(pane: dict[str, Any]) -> dict[str, str] | None:
    """Accept only a live, explicit wrapper and its matching foreground child."""
    try:
        variables = pane.get('user_vars')
        if not isinstance(variables, dict): return None
        raw = variables.get(VAR)
        if not isinstance(raw, str) or len(raw) > 512: return None
        marker = json.loads(raw)
        if not isinstance(marker, dict) or set(marker) != {'version', 'session_id', 'wrapper', 'wrapper_start', 'child', 'child_start'} or type(marker['version']) is not int or marker['version'] != 1:
            return None
        ident = normalize_id(marker['session_id'])
        wrapper, child = marker['wrapper'], marker['child']
        if any(type(marker[k]) is not int or marker[k] < 1 for k in ('wrapper', 'wrapper_start', 'child', 'child_start')): return None
        _, wrapper_start = _identity(wrapper)
        parent, child_start = _identity(child)
        if parent != wrapper or wrapper_start != marker['wrapper_start'] or child_start != marker['child_start'] or not _is_phoenix_wrapper(wrapper): return None
        processes = pane.get('foreground_processes')
        if not isinstance(processes, list): return None
        matches = [p for p in processes if isinstance(p, dict) and p.get('pid') == child]
        if len(matches) != 1: return None
        args = matches[0].get('cmdline')
        if not isinstance(args, list) or len(args) != 3 or not isinstance(args[0], str) or os.path.basename(args[0]) != 'codex' or args[1:] != ['resume', ident]: return None
        cwd = pane.get('cwd'); process_cwd = matches[0].get('cwd')
        if not isinstance(cwd, str) or not cwd.startswith('/') or not isinstance(process_cwd, str) or os.path.realpath(cwd) != os.path.realpath(process_cwd): return None
        return {'session_id': ident, 'source': 'explicit_resume'}
    except (OSError, TypeError, ValueError, IndexError, KeyError, json.JSONDecodeError, CodexCheckError):
        return None


def resume_in_current_pane(session_id: str, cwd: str) -> int:
    """Verify an explicit ID and run Codex interactively in the current Kitty pane."""
    if not os.isatty(0) or not os.isatty(1) or os.ttyname(0) != os.ttyname(1) or not os.environ.get('KITTY_WINDOW_ID', '').isdigit():
        raise CodexCheckError('Запустите эту команду непосредственно внутри панели Kitty')
    verified = check_session(session_id, cwd)
    executable = shutil.which('codex')
    if not executable: raise CodexCheckError('Codex CLI не найден')
    child = subprocess.Popen([executable, 'resume', verified['session_id']], cwd=verified['cwd'])
    marked = False
    try:
        try:
            _, wrapper_start = _identity(os.getpid())
            _, child_start = _identity(child.pid)
            marker = json.dumps({'version': 1, 'session_id': verified['session_id'], 'wrapper': os.getpid(), 'wrapper_start': wrapper_start, 'child': child.pid, 'child_start': child_start}, separators=(',', ':'))
            os.write(1, _user_var(marker)); marked = True
        except (OSError, ValueError, IndexError):
            pass  # Continue Codex, but do not claim the panel was bound.
        try: return child.wait()
        except KeyboardInterrupt:
            try: return child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.terminate(); return child.wait()
    finally:
        if marked:
            try: os.write(1, _user_var(None))
            except OSError: pass


def saved_binding(state: dict[str, Any], window: int, tab: int, pane: int) -> tuple[str, str]:
    """Resolve an explicit saved binding; indexes must never address another pane."""
    if any(type(index) is not int or index < 0 for index in (window, tab, pane)):
        raise CodexCheckError('Неверный индекс окна, вкладки или панели')
    try:
        terminal = state['windows'][window]
        if str(terminal.get('class', '')).casefold() != 'kitty': raise ValueError()
        saved = terminal['adapters']['kitty']['tabs'][tab]['panes'][pane]
        binding, cwd = saved['codex_session'], saved['cwd']
        if not isinstance(binding, dict) or set(binding) != {'session_id', 'source'} or binding['source'] != 'explicit_resume': raise ValueError()
        ident = normalize_id(binding['session_id'])
        if not isinstance(cwd, str) or not cwd.startswith('/'): raise ValueError()
        return ident, cwd
    except (KeyError, IndexError, TypeError, ValueError):
        raise CodexCheckError('В выбранной панели нет проверяемой привязки Codex') from None
