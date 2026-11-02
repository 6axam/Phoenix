"""Privacy-conscious, compact captures of currently visible workspaces."""
from __future__ import annotations
import logging
import os
import subprocess
import time
from typing import Any
from .core import HyprlandBackend

LOG = logging.getLogger('phoenix.capture')


def session_unlocked() -> bool:
    session = os.environ.get('XDG_SESSION_ID')
    if not session:
        return False
    try:
        result = subprocess.run(['loginctl', 'show-session', session, '-p', 'LockedHint', '--value'], capture_output=True, text=True, timeout=3, check=True)
        return result.stdout.strip() == 'no'
    except (OSError, subprocess.SubprocessError):
        return False


def capture_previews(backend: HyprlandBackend, settings: dict[str, Any]) -> list[dict[str, Any]]:
    if not settings.get('capture_previews', True) or not session_unlocked():
        return []
    try:
        monitors = backend.query('monitors')
        clients = backend.query('clients')
    except RuntimeError as exc:
        LOG.warning('Screenshot unavailable: %s', exc)
        return []
    excluded = {str(c).casefold() for c in settings.get('exclude_classes', [])} | {'org.phoenix.session'}
    results: list[dict[str, Any]] = []
    for monitor in monitors:
        name, monitor_id = monitor.get('name'), monitor.get('id')
        workspace = monitor.get('activeWorkspace', {}).get('id')
        special = monitor.get('specialWorkspace', {}).get('id')
        if not isinstance(name, str) or not isinstance(workspace, int) or workspace <= 0:
            continue
        # Skip the entire monitor if an excluded window or Phoenix itself is visible.
        if any(c.get('monitor') == monitor_id and c.get('visible', False) and (c.get('workspace', {}).get('id') in (workspace, special) or c.get('pinned', False)) and str(c.get('class', '')).casefold() in excluded for c in clients):
            continue
        try:
            raw = subprocess.run(['grim', '-o', name, '-'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8, check=True).stdout
            image = subprocess.run(['magick', 'png:-', '-resize', '1920x1920>', '-strip', '-sampling-factor', '4:4:4', '-quality', '88', 'jpeg:-'], input=raw, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10, check=True).stdout
            if not image.startswith(b'\xff\xd8') or not session_unlocked():
                continue
            current = next((m for m in backend.query('monitors') if m.get('name') == name), {})
            if current.get('activeWorkspace', {}).get('id') != workspace:
                continue
            results.append({'workspace': workspace, 'monitor': name, 'captured': time.time(), 'data': image})
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            LOG.warning('Screenshot failed for %s: %s', name, exc)
    return results
