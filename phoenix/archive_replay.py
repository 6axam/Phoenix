"""Show a bounded, verified historical archive before Kitty starts a new shell."""
from __future__ import annotations
from collections import deque
import sys
from typing import TextIO
from .archive_view import filtered_chunks
from .core import SnapshotManager

MAX_DISPLAY_CHARS = 2 * 1024 * 1024


def replay(manager: SnapshotManager, snapshot: int, window: int, tab: int, pane: int, output: TextIO = sys.stdout) -> None:
    if any(type(x) is not int or x < 0 for x in (snapshot, window, tab, pane)) or snapshot == 0:
        raise ValueError('Invalid archive coordinates')
    _, state = manager.get(snapshot)
    try:
        win = state['windows'][window]
        if str(win.get('class', '')).casefold() != 'kitty': raise ValueError()
        saved = win['adapters']['kitty']['tabs'][tab]['panes'][pane]
        truncated = saved.get('scrollback_capture') == 'truncated'
    except (KeyError, IndexError, TypeError, ValueError):
        raise LookupError('Archive does not correspond to a Kitty pane') from None
    chunks: deque[str] = deque(); kept = 0; total = 0
    for text in filtered_chunks(manager.iter_archive_chunks(snapshot, window, tab, pane)):
        if text:
            chunks.append(text); kept += len(text); total += len(text)
            while chunks and kept - len(chunks[0]) >= MAX_DISPLAY_CHARS:
                kept -= len(chunks.popleft())
    display = ''.join(chunks)[-MAX_DISPLAY_CHARS:]
    output.write('\n=== PHOENIX: СОХРАНЁННАЯ ИСТОРИЯ ТЕРМИНАЛА ===\n')
    if truncated: output.write('[История была усечена при сохранении.]\n')
    if total > len(display): output.write('[Показан только конец архива; полный текст доступен в Phoenix.]\n')
    output.write(display)
    output.write('\n=== КОНЕЦ ИСТОРИИ; ДАЛЕЕ НОВАЯ ОБОЛОЧКА ===\n')
    output.flush()
