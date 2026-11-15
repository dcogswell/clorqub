"""Small shared display helpers (presentation only, no scoring)."""

import math


def rel_percent(value):
    """Clore-reported reliability fraction 0..1 -> '94.0%'; else '-'."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "-"
    if not math.isfinite(value) or not 0 <= value <= 1:
        return "-"
    return f"{value * 100:.1f}%"
