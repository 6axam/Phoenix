"""Bounded, verified archive previews and literal searches for the GTK viewer."""
from __future__ import annotations
import codecs
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

_ASCII_CONTROLS = str.maketrans('', '', ''.join(chr(i) for i in range(32) if i not in (9, 10)) + '\x7f')


class TextFilter:
    """Discard terminal controls, including sequences spanning chunk boundaries."""
    def __init__(self) -> None:
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.state = 'text'

    def feed(self, data: bytes, *, final: bool = False) -> str:
        output = []
        decoded = self.decoder.decode(data, final=final)
        if self.state == 'text' and decoded.isascii() and '\x1b' not in decoded:
            return decoded.translate(_ASCII_CONTROLS)
        for char in decoded:
            if self.state == 'osc':
                if char == '\x07': self.state = 'text'
                elif char == '\x1b': self.state = 'osc_escape'
                continue
            if self.state == 'osc_escape':
                self.state = 'text' if char == '\\' else ('osc_escape' if char == '\x1b' else 'osc')
                continue
            if self.state == 'csi':
                if '@' <= char <= '~': self.state = 'text'
                elif char == '\x1b': self.state = 'escape'
                continue
            if self.state == 'escape':
                self.state = 'osc' if char == ']' else 'csi' if char == '[' else 'escape' if char == '\x1b' else 'text'
                continue
            if char == '\x1b': self.state = 'escape'; continue
            if char in '\n\t' or unicodedata.category(char) not in ('Cc', 'Cf'):
                output.append(char)
        return ''.join(output)


def filtered_chunks(chunks: Iterable[bytes]) -> Iterator[str]:
    sanitizer = TextFilter()
    for chunk in chunks:
        yield sanitizer.feed(chunk)
    yield sanitizer.feed(b'', final=True)


@dataclass(frozen=True)
class ArchiveResult:
    text: str
    total: int
    match: int | None = None
    selection: int | None = None


def preview(chunks: Iterable[bytes], limit: int = 256 * 1024) -> ArchiveResult:
    pieces: list[str] = []; total = 0; kept = 0
    for text in filtered_chunks(chunks):
        part = text[:max(0, limit - kept)]
        if part: pieces.append(part); kept += len(part)
        total += len(text)
    return ArchiveResult(''.join(pieces), total)


def search_archive(chunks: Iterable[bytes], needle: str, start: int = 0, context: int = 2048) -> ArchiveResult:
    if not needle or len(needle) > 1024 or start < 0:
        raise ValueError('Search text must contain 1–1024 characters')
    before = ''; total = 0; found = None; selection = None; pieces: list[str] = []; remaining = 0
    for text in filtered_chunks(chunks):
        if found is None:
            combined = before + text
            base = total - len(before)
            index = combined.find(needle, max(0, start - base))
            if index >= 0:
                found = base + index
                left = max(0, index - context)
                selection = index - left
                end = min(len(combined), index + len(needle) + context)
                pieces.append(combined[left:end])
                remaining = index + len(needle) + context - end
            else:
                before = combined[-(context + len(needle) - 1):]
        elif remaining:
            pieces.append(text[:remaining]); remaining -= min(remaining, len(text))
        total += len(text)
    return ArchiveResult(''.join(pieces), total, found, selection)
