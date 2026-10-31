from __future__ import annotations

import hashlib
import os
import re
import time
import uuid
from typing import Any
from .core import DesktopRegistry, HyprlandBackend, SnapshotManager, canonical, lock


def same_class(window: dict[str, Any], client: dict[str, Any]) -> bool:
    wanted = {str(window.get(k, '')).casefold() for k in ('class', 'initial_class')} - {''}
    actual = {str(client.get(k, '')).casefold() for k in ('class', 'initialClass')} - {''}
    return bool(wanted & actual)


def geometry(window: dict[str, Any], old_monitors: list[dict[str, Any]], new_monitors: list[dict[str, Any]]) -> tuple[int, int, int, int] | None:
    try:
        old = next(m for m in old_monitors if m['name'] == window['monitor'])
        new = next((m for m in new_monitors if m['name'] == window['monitor']), new_monitors[0])
        ox, oy, ow, oh = float(old['x']), float(old['y']), float(old['width']) / float(old.get('scale') or 1), float(old['height']) / float(old.get('scale') or 1)
        nx, ny, nw, nh = float(new['x']), float(new['y']), float(new['width']) / float(new.get('scale') or 1), float(new['height']) / float(new.get('scale') or 1)
        x, y = window['at']; w, h = window['size']
        w = max(1, min(int(w * nw / ow), int(nw))); h = max(1, min(int(h * nh / oh), int(nh)))
        x = max(int(nx), min(int(nx + (x - ox) * nw / ow), int(nx + nw - w)))
        y = max(int(ny), min(int(ny + (y - oy) * nh / oh), int(ny + nh - h)))
        return x, y, w, h
    except (KeyError, TypeError, ValueError, IndexError, StopIteration, ZeroDivisionError):
        return None


class RecoveryPlanner:
    def __init__(self, registry: DesktopRegistry):
        self.registry = registry

    def plan(self, state: dict[str, Any], clients: list[dict[str, Any]]) -> list[dict[str, Any]]:
        available = list(clients)
        plan = []
        for index, window in enumerate(state['windows']):
            match = next((c for c in available if same_class(window, c)), None)
            if match:
                available.remove(match)
                plan.append({'index': index, 'class': window['class'], 'action': 'existing'})
            else:
                entry = self.registry.find(str(window.get('initial_class') or window['class'])) or self.registry.find(str(window['class']))
                plan.append({'index': index, 'class': window['class'], 'action': 'launch' if entry else 'unsupported', 'entry': str(entry) if entry else None})
        return plan


class RecoveryExecutor:
    def __init__(self, backend: HyprlandBackend, registry: DesktopRegistry, manager: SnapshotManager, timeout: float = 8):
        self.backend, self.registry, self.manager, self.timeout = backend, registry, manager, timeout

    def restore(self, snapshot_id: int, state: dict[str, Any], dry_run: bool = False, selected: set[int] | None = None) -> list[dict[str, Any]]:
        if dry_run:
            return RecoveryPlanner(self.registry).plan(state, self.backend.query('clients'))
        # A single restore process can mutate windows at a time; process death releases the lock.
        with lock(self.manager.path.with_suffix('.restore.lock'), blocking=False):
            return self._restore(snapshot_id, state, selected)

    def _restore(self, snapshot_id: int, state: dict[str, Any], selected: set[int] | None) -> list[dict[str, Any]]:
        transaction = str(uuid.uuid4())
        results: list[dict[str, Any]] = []
        # Any previous ambiguous launch must not be repeated automatically.
        with self.manager.connect() as db:
            row = db.execute('SELECT result FROM runs WHERE snapshot_id=?', (snapshot_id,)).fetchone()
        import json
        previous = json.loads(row['result']) if row else []
        uncertain = {r['index'] for r in previous if r.get('status') in ('launching', 'timeout', 'uncertain')}
        self.manager.run_state(snapshot_id, transaction, 'running', previous)
        try:
            clients = self.backend.query('clients')
            monitors = self.backend.query('monitors')
            claimed: set[str] = set()
            for index, window in enumerate(state['windows']):
                if selected is not None and index not in selected:
                    continue
                result: dict[str, Any] = {'index': index, 'class': window.get('class', ''), 'status': 'unsupported'}
                results.append(result)
                try:
                    match = next((c for c in clients if c.get('address') not in claimed and same_class(window, c)), None)
                    if not match:
                        if index in uncertain:
                            result.update(status='uncertain', detail='A previous launch may still open; automatic retry suppressed')
                            continue
                        entry = self.registry.find(str(window.get('initial_class') or window.get('class', ''))) or self.registry.find(str(window.get('class', '')))
                        if not entry:
                            result['detail'] = 'No trusted system desktop entry'
                            continue
                        before = {c.get('address') for c in clients}
                        result['status'] = 'launching'
                        self.manager.run_state(snapshot_id, transaction, 'running', results + [r for r in previous if r.get('index') in uncertain and r.get('index') not in {x['index'] for x in results}])
                        kitty_cwd = window.get('cwd') if str(window.get('class', '')).casefold() == 'kitty' and window.get('cwd_source') == 'single_local_shell' else None
                        used_cwd = bool(isinstance(kitty_cwd, str) and hasattr(self.registry, 'launch_kitty') and self.registry.launch_kitty(entry, kitty_cwd))
                        if not used_cwd:
                            self.registry.launch(entry)
                        elif used_cwd:
                            result['terminal_cwd'] = kitty_cwd
                        deadline = time.monotonic() + self.timeout
                        while time.monotonic() < deadline:
                            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
                            clients = self.backend.query('clients')
                            match = next((c for c in clients if c.get('address') not in before and c.get('address') not in claimed and same_class(window, c)), None)
                            if match:
                                break
                        if not match:
                            result.update(status='timeout', detail='Window did not appear; automatic retry suppressed')
                            continue
                    address = str(match.get('address', ''))
                    if not re.fullmatch(r'0x[0-9a-fA-F]+', address):
                        result.update(status='error', detail='Invalid live window address')
                        continue
                    claimed.add(address)
                    selector = 'address:' + address
                    workspace = window.get('workspace')
                    if isinstance(workspace, int) and workspace > 0:
                        self.backend.dispatch('movetoworkspacesilent', f'{workspace},{selector}')
                    floating = bool(window.get('floating'))
                    if floating != bool(match.get('floating')):
                        self.backend.dispatch('setfloating' if floating else 'settiled', selector)
                    rect = geometry(window, state.get('monitors', []), monitors) if floating else None
                    if rect:
                        x, y, w, h = rect
                        self.backend.dispatch('resizewindowpixel', f'exact {w} {h},{selector}')
                        self.backend.dispatch('movewindowpixel', f'exact {x} {y},{selector}')
                    # Fullscreenstate sets explicit internal/client states rather than toggling.
                    fullscreen = window.get('fullscreen', 0)
                    if isinstance(fullscreen, int) and fullscreen in (0, 1, 2) and fullscreen != match.get('fullscreen', 0):
                        self.backend.dispatch('fullscreenstate', f'{fullscreen} -1,{selector}')
                    verified = next((c for c in self.backend.query('clients') if c.get('address') == address), None)
                    warnings = []
                    if not verified:
                        warnings.append('window disappeared before verification')
                    else:
                        if isinstance(workspace, int) and workspace > 0 and verified.get('workspace', {}).get('id') != workspace:
                            warnings.append('workspace differs')
                        if bool(verified.get('floating')) != floating:
                            warnings.append('floating state differs')
                        if rect and (verified.get('at') != list(rect[:2]) or verified.get('size') != list(rect[2:])):
                            warnings.append('geometry differs')
                        if fullscreen in (0, 1, 2) and verified.get('fullscreen', 0) != fullscreen:
                            warnings.append('fullscreen differs')
                    detail = 'Application internals and tiled geometry are not restored'
                    if warnings:
                        detail += '; verification: ' + ', '.join(warnings)
                    result.update(status='partial', detail=detail)
                except Exception as exc:
                    result.update(status='error', detail=str(exc))
                finally:
                    self.manager.run_state(snapshot_id, transaction, 'running', results + [r for r in previous if r.get('index') in uncertain and r.get('index') not in {x['index'] for x in results}])
            self.manager.run_state(snapshot_id, transaction, 'complete', results)
            return results
        except BaseException:
            self.manager.run_state(snapshot_id, transaction, 'interrupted', results)
            raise
