"""Utilities for inspecting stitch command data."""

from collections.abc import Iterable


def stitch_bounds(stitches: Iterable[tuple[int, int, bool]]) -> tuple[int, int, int, int] | None:
    """Return the min/max coordinate bounds for stitch commands."""

    iterator = iter(stitches)
    try:
        first_x, first_y, _ = next(iterator)
    except StopIteration:
        return None

    min_x = max_x = first_x
    min_y = max_y = first_y
    for x, y, _ in iterator:
        min_x = min(min_x, x)
        max_x = max(max_x, x)
        min_y = min(min_y, y)
        max_y = max(max_y, y)
    return min_x, min_y, max_x, max_y
