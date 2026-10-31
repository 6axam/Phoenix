"""Small, deterministic helpers for frame-clock animations."""
from __future__ import annotations


def smoothstep(progress: float) -> float:
    """Ease in and out with zero velocity at both ends."""
    t = min(1.0, max(0.0, progress))
    return t * t * (3.0 - 2.0 * t)
