"""Navigation and workspace selection within one saved snapshot."""
from __future__ import annotations
from typing import Any


class Timeline:
    def __init__(self, items: list[dict[str, Any]]):
        self.items = items
        self.index: int | None = 0 if items else None

    def step(self, amount: int) -> bool:
        if not self.items:
            return False
        new = len(self.items) - 1 if self.index is None else min(max(self.index + amount, 0), len(self.items) - 1)
        changed = new != self.index
        self.index = new
        return changed

    @property
    def current(self) -> dict[str, Any] | None:
        return self.items[self.index] if self.index is not None else None


def workspace_items(snapshot_id: int, state: dict[str, Any], previews: list[dict[str, Any]], earlier: list[dict[str, Any]] | None = None, live: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Describe the workspaces belonging to one snapshot, including those without an image."""
    windows = state.get('windows', [])
    ids = {w.get('workspace') for w in windows if isinstance(w.get('workspace'), int) and w.get('workspace') > 0}
    ids.update(w.get('id') for w in state.get('workspaces', []) if isinstance(w.get('id'), int) and w.get('id') > 0)
    items = []
    for workspace in sorted(ids):
        workspace_windows = [w for w in windows if w.get('workspace') == workspace]
        preferred_monitor = next((w.get('monitor') for w in workspace_windows if w.get('monitor')), None)
        candidates = [p for p in previews if p['workspace'] == workspace]
        image = next((p for p in candidates if p['monitor'] == preferred_monitor), candidates[0] if candidates else None)
        is_earlier = image is None
        if image is None:
            candidates = [p for p in (earlier or []) if p['workspace'] == workspace]
            image = next((p for p in candidates if p['monitor'] == preferred_monitor), candidates[0] if candidates else None)
        recent = [p for p in (live or []) if p['workspace'] == workspace and (image is None or p['captured'] > image['captured'])]
        live_image = next((p for p in recent if p['monitor'] == preferred_monitor), recent[0] if recent else None)
        is_live = live_image is not None
        if is_live:
            image = live_image
        monitor_name = preferred_monitor or (image.get('monitor') if image else None)
        monitor = next((m for m in state.get('monitors', []) if m.get('name') == monitor_name), None)
        items.append({'snapshot_id': snapshot_id, 'workspace': workspace, 'windows': workspace_windows,
                      'monitor_geometry': monitor, 'monitor': image['monitor'] if image else None,
                      'captured': image['captured'] if image else None, 'has_image': image is not None,
                      'capture_id': image['id'] if image and (is_earlier or is_live) else None, 'capture_source': image.get('source', 'event') if image and (is_earlier or is_live) else None, 'is_earlier': bool(image and is_earlier and not is_live), 'is_live': is_live})
    return items
