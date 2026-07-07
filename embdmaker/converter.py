"""Utilities for converting simple point paths into stitch commands."""

from collections.abc import Iterable


def convert_points_to_stitches(points: Iterable[tuple[int, int]]) -> list[tuple[int, int, bool]]:
    """Convert 2D points to simple stitch commands.

    The first command is always a travel move (`pen_down=False`) and each
    following command is a stitch (`pen_down=True`).
    """

    commands: list[tuple[int, int, bool]] = []
    for index, point in enumerate(points):
        if len(point) != 2:
            raise ValueError(
                f"Point at index {index} must contain exactly two coordinates: {point!r}"
            )
        x, y = point
        if not isinstance(x, int) or not isinstance(y, int):
            raise TypeError("Point coordinates must be integers")
        commands.append((x, y, index != 0))
    return commands
