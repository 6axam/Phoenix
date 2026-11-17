"""Verify an explicitly identified Codex conversation without opening it.

This is not a panel-to-thread detector. The caller must supply the ID; cwd or
recency are never used to infer which conversation belongs to a terminal.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import time
from typing import Any
from uuid import UUID


class CodexCheckError(RuntimeError):
    pass


def normalize_id(value: str) -> str:
    try:
        parsed = UUID(value)
    except (TypeError, ValueError, AttributeError):
        raise CodexCheckError('Укажите полный UUID разговора Codex') from None
    if str(parsed) != value.lower():
        raise CodexCheckError('Укажите полный UUID разговора Codex')
    return str(parsed)


def _read_response(process: subprocess.Popen[bytes], wanted: int, timeout: float) -> dict[str, Any]:
    assert process.stdout is not None
    deadline = time.monotonic() + timeout
    buffer = bytearray(); total = 0
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise CodexCheckError('Codex не ответил вовремя')
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk: raise CodexCheckError('Codex завершился до ответа')
            total += len(chunk)
            if total > 2 * 1024 * 1024: raise CodexCheckError('Ответ Codex слишком большой')
            buffer.extend(chunk)
            while b'\n' in buffer:
                line, _, rest = buffer.partition(b'\n'); buffer = bytearray(rest)
                try: response = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError): raise CodexCheckError('Неизвестный ответ Codex') from None
                if not isinstance(response, dict): raise CodexCheckError('Неизвестный ответ Codex')
                if response.get('id') == wanted:
                    if 'error' in response: raise CodexCheckError('Codex не предоставил данные этого разговора')
                    result = response.get('result')
                    if not isinstance(result, dict): raise CodexCheckError('Неизвестный ответ Codex')
                    return result


def check_session(session_id: str, directory: str, *, timeout: float = 8.0) -> dict[str, Any]:
    ident = normalize_id(session_id)
    try:
        path = Path(directory)
        if not path.is_absolute() or not path.is_dir(): raise ValueError()
        cwd = path.resolve(strict=True)
    except (OSError, ValueError, RuntimeError):
        raise CodexCheckError('Каталог проекта недоступен или указан не полностью') from None
    executable = shutil.which('codex')
    if not executable: raise CodexCheckError('Codex CLI не найден')
    try:
        process = subprocess.Popen([executable, 'app-server', '-c', 'analytics.enabled=false', '--listen', 'stdio://'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        raise CodexCheckError('Не удалось запустить локальный интерфейс Codex') from None
    try:
        assert process.stdin is not None
        def send(message: dict[str, Any]) -> None:
            process.stdin.write((json.dumps(message) + '\n').encode()); process.stdin.flush()
        send({'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'phoenix', 'version': '1.2'}}})
        _read_response(process, 1, timeout)
        send({'method': 'initialized', 'params': {}})
        send({'id': 2, 'method': 'thread/read', 'params': {'threadId': ident, 'includeTurns': False}})
        response = _read_response(process, 2, timeout)
        thread = response.get('thread')
        if not isinstance(thread, dict) or thread.get('id') != ident:
            raise CodexCheckError('Codex не подтвердил этот ID разговора')
        if thread.get('ephemeral') is not False:
            raise CodexCheckError('Codex не подтвердил сохранённую сессию')
        recorded = thread.get('cwd')
        try:
            recorded_cwd = Path(recorded).resolve(strict=True) if isinstance(recorded, str) and Path(recorded).is_absolute() else None
        except (OSError, ValueError, RuntimeError):
            raise CodexCheckError('Каталог разговора недоступен') from None
        if recorded_cwd != cwd:
            raise CodexCheckError('Каталог разговора не совпадает с выбранным проектом')
        return {'session_id': ident, 'cwd': str(cwd), 'status': 'metadata_verified', 'resume_argv': ['codex', 'resume', ident], 'note': 'Проверены только метаданные ID и каталога; запуск, связь с панелью и возможность возобновления не проверены'}
    except (BrokenPipeError, OSError, subprocess.SubprocessError):
        raise CodexCheckError('Связь с локальным интерфейсом Codex прервалась') from None
    finally:
        if process.stdin:
            try: process.stdin.close()
            except OSError: pass
        if process.poll() is None:
            process.terminate()
            try: process.wait(timeout=2)
            except subprocess.TimeoutExpired: process.kill(); process.wait()
        if process.stdout: process.stdout.close()
