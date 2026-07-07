"""Utilities for inspecting stitch command data."""

from collections.abc import Iterable


def stitch_bounds(stitches: Iterable[tuple[int, int, bool]]) -> tuple[int, int, int, int] | None:
    """Return the min/max coordinate bounds for stitch commands."""

    coordinates = [(x, y) for x, y, _ in stitches]
    if not coordinates:
        return None
    xs = [x for x, _ in coordinates]
    ys = [y for _, y in coordinates]
    return min(xs), min(ys), max(xs), max(ys)
