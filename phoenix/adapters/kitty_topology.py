"""Reconstruct small split layouts from observable directional neighbours.

Only two- and three-pane slicing layouts are accepted.  The output contains
indices and fixed enum values, never Kitty IDs, commands or raw session files.
"""
from __future__ import annotations
from fractions import Fraction
from itertools import permutations, product
from typing import Any

DIRECTIONS = ('left', 'right', 'top', 'bottom')


def valid_plan(plan: Any, count: int) -> bool:
    if not isinstance(plan, list) or count not in (2, 3) or len(plan) != count:
        return False
    seen: set[int] = set()
    for i, step in enumerate(plan):
        if not isinstance(step, dict) or set(step) != {'pane', 'parent', 'axis'}:
            return False
        pane, parent, axis = step['pane'], step['parent'], step['axis']
        if type(pane) is not int or not 0 <= pane < count or pane in seen:
            return False
        if i == 0:
            if parent is not None or axis is not None: return False
        elif type(parent) is not int or parent not in seen or axis not in ('vsplit', 'hsplit'):
            return False
        seen.add(pane)
    return True


def _graph(rects: dict[int, tuple[Fraction, Fraction, Fraction, Fraction]]) -> tuple:
    result = []
    for a in sorted(rects):
        x1, y1, x2, y2 = rects[a]
        sides: dict[str, list[int]] = {d: [] for d in DIRECTIONS}
        for b, (u1, v1, u2, v2) in rects.items():
            if a == b: continue
            if max(y1, v1) < min(y2, v2):
                if u2 == x1: sides['left'].append(b)
                if u1 == x2: sides['right'].append(b)
            if max(x1, u1) < min(x2, u2):
                if v2 == y1: sides['top'].append(b)
                if v1 == y2: sides['bottom'].append(b)
        result.append(tuple(tuple(sorted(sides[d])) for d in DIRECTIONS))
    return tuple(result)


def derive_plan(windows: Any) -> list[dict[str, Any]] | None:
    if not isinstance(windows, list) or len(windows) not in (2, 3): return None
    ids = [w.get('id') if isinstance(w, dict) else None for w in windows]
    if any(type(i) is not int or i <= 0 for i in ids) or len(set(ids)) != len(ids): return None
    indices = {identity: index for index, identity in enumerate(ids)}
    actual = []
    for w in windows:
        neighbors = w.get('neighbors')
        if not isinstance(neighbors, dict) or set(neighbors) - set(DIRECTIONS): return None
        edges = []
        for direction in DIRECTIONS:
            values = neighbors.get(direction, [])
            if not isinstance(values, list) or any(type(v) is not int or v not in indices for v in values) or len(set(values)) != len(values): return None
            edges.append(tuple(sorted(indices[v] for v in values)))
        actual.append(tuple(edges))
    actual_graph = tuple(actual)
    n = len(windows)
    for order in permutations(range(n)):
        for axes in product(('vsplit', 'hsplit'), repeat=n - 1):
            for parents in product(*(range(i) for i in range(1, n))):
                rects = {order[0]: (Fraction(0), Fraction(0), Fraction(1), Fraction(1))}
                plan = [{'pane': order[0], 'parent': None, 'axis': None}]
                for i in range(1, n):
                    pane, parent, axis = order[i], order[parents[i - 1]], axes[i - 1]
                    x1, y1, x2, y2 = rects[parent]
                    if axis == 'vsplit':
                        mid = (x1 + x2) / 2
                        rects[parent], rects[pane] = (x1, y1, mid, y2), (mid, y1, x2, y2)
                    else:
                        mid = (y1 + y2) / 2
                        rects[parent], rects[pane] = (x1, y1, x2, mid), (x1, mid, x2, y2)
                    plan.append({'pane': pane, 'parent': parent, 'axis': axis})
                if _graph(rects) == actual_graph:
                    return plan
    return None
