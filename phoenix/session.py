from __future__ import annotations
import hashlib
import os
from pathlib import Path
from .core import xdg, xdg_path


def marker(*, create: bool = True) -> Path:
    signature = os.environ.get('HYPRLAND_INSTANCE_SIGNATURE')
    if not signature:
        raise RuntimeError('Not in a Hyprland session')
    return (xdg('STATE') if create else xdg_path('STATE')) / ('decision-' + hashlib.sha256(signature.encode()).hexdigest()[:24])


def decided() -> bool:
    return marker(create=False).exists()


def decide(value: str) -> None:
    if value not in ('restore', 'skip'):
        raise ValueError('Invalid decision')
    path = marker()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as f:
        f.write(value + '\n')
