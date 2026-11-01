"""Display untrusted archive text without terminal control sequences."""
from __future__ import annotations
import re
import unicodedata


def safe_text(data: bytes) -> str:
    value = data.decode('utf-8', errors='replace')
    # Remove ANSI/OSC control sequences before they can affect the viewer's terminal.
    value = re.sub(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?', '', value)
    value = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', value)
    return ''.join(c for c in value if c in '\n\t' or unicodedata.category(c) not in ('Cc', 'Cf'))
