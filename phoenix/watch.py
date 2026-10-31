"""Debounced Hyprland socket2 watcher; capture only after a workspace switch."""
from __future__ import annotations
import logging
import os
from pathlib import Path
import socket
import threading
import time
from typing import Callable

LOG = logging.getLogger('phoenix.watch')
EVENTS = {b'workspacev2', b'focusedmonv2'}


def socket_path() -> Path | None:
    runtime = os.environ.get('XDG_RUNTIME_DIR')
    signature = os.environ.get('HYPRLAND_INSTANCE_SIGNATURE')
    if not runtime or not signature or '/' in signature or signature in ('.', '..'):
        return None
    return Path(runtime) / 'hypr' / signature / '.socket2.sock'


def watch_workspace_events(stop: threading.Event, capture: Callable[[], None], *, path: Path | None = None, debounce: float = .8, min_interval: float = 3.0, reconnect: float = 5.0) -> None:
    path = path or socket_path()
    if path is None:
        LOG.warning('Hyprland socket2 unavailable; periodic screenshots remain active')
        return
    while not stop.is_set():
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
                stream.settimeout(.25)
                stream.connect(str(path))
                LOG.info('Watching Hyprland workspace changes')
                buffer = b''; deadline: float | None = None; last_capture = 0.0
                while not stop.is_set():
                    try:
                        data = stream.recv(8192)
                        if not data: raise ConnectionError('socket2 disconnected')
                        buffer += data
                        if len(buffer) > 131072:
                            buffer = b''
                        lines = buffer.split(b'\n'); buffer = lines.pop()
                        if any(line.partition(b'>>')[0] in EVENTS for line in lines):
                            deadline = max(time.monotonic() + debounce, last_capture + min_interval)
                    except socket.timeout:
                        pass
                    if deadline is not None and time.monotonic() >= deadline:
                        deadline = None
                        try:
                            capture()
                        except Exception:
                            LOG.exception('Workspace capture failed')
                        last_capture = time.monotonic()
        except (OSError, ConnectionError) as exc:
            LOG.warning('Hyprland event stream unavailable: %s', exc)
            stop.wait(reconnect)
