from __future__ import annotations

import argparse
import math
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pyembroidery as embroidery
from pyembroidery import EmbPattern, EmbThread
from svgelements import Color, Move, Path as SvgPath, Shape, SVG


SVG_PX_PER_MM = 96.0 / 25.4
EMB_UNITS_PER_MM = 10.0


@dataclass
class StitchRun:
    color: str
    points_mm: list[tuple[float, float]]
    component_id: int = 0
    # Perimeters must be sewn before their companion fill, even when a
    # component is entered from the opposite side.
    phase: int = 1


def color_to_hex(value) -> str:
    if value is None:
        return "#000000"
    text = str(value).strip()
    if text.lower() in {"", "none", "transparent"}:
        return "none"
    try:
        color = Color(text)
        if color.opacity == 0:
            return "none"
        return f"#{int(color.red):02x}{int(color.green):02x}{int(color.blue):02x}"
    except Exception:
        return "#000000"


def path_color(element) -> tuple[str, bool]:
    stroke = color_to_hex(getattr(element, "stroke", None))
    fill = color_to_hex(getattr(element, "fill", None))
    if stroke != "none":
        return stroke, False
    if fill != "none":
        return fill, True
    return "none", False


def flatten_path(path: SvgPath, sample_step_px: float) -> list[list[tuple[float, float]]]:
    subpaths: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []

    for segment in path:
        if isinstance(segment, Move):
            if current:
                subpaths.append(current)
            point = segment.end
            current = [(float(point.x), float(point.y))]
            continue

        if not current:
            start = segment.start
            current.append((float(start.x), float(start.y)))

        try:
            length = max(float(segment.length(error=1e-3)), 0.0)
        except Exception:
            length = sample_step_px
        steps = max(1, int(math.ceil(length / max(sample_step_px, 0.001))))

        for i in range(1, steps + 1):
            point = segment.point(i / steps)
            current.append((float(point.x), float(point.y)))

    if current:
        subpaths.append(current)
    return [dedupe_points(points) for points in subpaths if len(points) > 1]


def dedupe_points(points: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    result: list[tuple[float, float]] = []
    for point in points:
        if not result or distance(result[-1], point) > 1e-6:
            result.append(point)
    return result


def distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def route_point_runs(
    runs: Iterable[list[tuple[float, float]]],
    *,
    start: tuple[float, float] | None = None,
    mode: str = "min_cuts",
) -> list[list[tuple[float, float]]]:
    ordered = [list(run) for run in runs if len(run) >= 2]
    if not ordered:
        return []
    if mode == "clean_top":
        if start is not None and len(ordered) > 1:
            first = ordered[0]
            last = ordered[-1]
            start_distance = min(distance(start, first[0]), distance(start, first[-1]))
            end_distance = min(distance(start, last[0]), distance(start, last[-1]))
            if end_distance < start_distance:
                ordered.reverse()
        routed: list[list[tuple[float, float]]] = []
        current = start
        for run in ordered:
            if current is not None and distance(current, run[-1]) < distance(current, run[0]):
                run.reverse()
            routed.append(run)
            current = run[-1]
        return routed

    routed: list[list[tuple[float, float]]] = []
    current = start
    if current is not None and len(ordered) > 1:
        first = ordered[0]
        last = ordered[-1]
        start_distance = min(distance(current, first[0]), distance(current, first[-1]))
        end_distance = min(distance(current, last[0]), distance(current, last[-1]))
        if end_distance < start_distance:
            ordered.reverse()

    for run in ordered:
        if current is not None and distance(current, run[-1]) < distance(current, run[0]):
            run.reverse()
        routed.append(run)
        current = run[-1]
    return routed


def route_stitch_runs(
    runs: Iterable[StitchRun],
    mode: str = "min_cuts",
    max_stitch_mm: float = 7.0,
) -> list[StitchRun]:
    if mode == "fast":
        return [run for run in runs if len(run.points_mm) >= 2]
    grouped: dict[str, dict[int, list[StitchRun]]] = {}
    color_order: list[str] = []
    for run in runs:
        if len(run.points_mm) < 2:
            continue
        if run.color not in grouped:
            grouped[run.color] = {}
            color_order.append(run.color)
        grouped[run.color].setdefault(run.component_id, []).append(run)

    routed: list[StitchRun] = []
    current: tuple[float, float] | None = None
    for color in color_order:
        remaining = list(grouped[color].items())
        while remaining:
            if current is None:
                component_id, component_runs = remaining.pop(0)
            else:
                component_index = min(
                    range(len(remaining)),
                    key=lambda index: min(
                        distance(current, point)
                        for run in remaining[index][1]
                        for point in (run.points_mm[0], run.points_mm[-1])
                    ),
                )
                component_id, component_runs = remaining.pop(component_index)
            for phase in sorted({run.phase for run in component_runs}):
                phase_runs = [run.points_mm for run in component_runs if run.phase == phase]
                for points in route_point_runs(phase_runs, start=current, mode=mode):
                    routed.append(
                        StitchRun(
                            color=color,
                            points_mm=points,
                            component_id=component_id,
                            phase=phase,
                        )
                    )
                    current = points[-1]
    return routed


def to_mm(points_px: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    return [(x / SVG_PX_PER_MM, y / SVG_PX_PER_MM) for x, y in points_px]


MIN_FILL_STITCH_MM = 0.3
MIN_DETAIL_RUN_MM = 0.15
_FILL_ROUTE_GRID_CACHE: dict[tuple, tuple[float, float, float, frozenset[tuple[int, int]]]] = {}


def split_long_stitches(
    points: Iterable[tuple[float, float]],
    max_stitch_mm: float,
    min_stitch_mm: float = 0.0,
) -> list[tuple[float, float]]:
    source = list(points)
    if len(source) < 2:
        return source
    result = [source[0]]
    for start, end in zip(source, source[1:]):
        span = distance(start, end)
        if 0 < span < min_stitch_mm:
            continue
        steps = max(1, int(math.ceil(span / max(max_stitch_mm, 0.1))))
        for i in range(1, steps + 1):
            t = i / steps
            result.append(
                (start[0] + (end[0] - start[0]) * t, start[1] + (end[1] - start[1]) * t)
            )
    return result


def rotate_point(
    point: tuple[float, float],
    angle_rad: float,
    origin: tuple[float, float],
) -> tuple[float, float]:
    x, y = point
    ox, oy = origin
    dx = x - ox
    dy = y - oy
    cos_a = math.cos(angle_rad)
    sin_a = math.sin(angle_rad)
    return (ox + (dx * cos_a) - (dy * sin_a), oy + (dx * sin_a) + (dy * cos_a))


def point_in_polygon(point: tuple[float, float], polygon: list[tuple[float, float]]) -> bool:
    x, y = point
    inside = False
    for (x1, y1), (x2, y2) in zip(polygon, polygon[1:]):
        if (y1 > y) == (y2 > y):
            continue
        cross_x = x1 + ((y - y1) * (x2 - x1) / (y2 - y1))
        if cross_x > x:
            inside = not inside
    return inside


def point_in_compound_fill(point: tuple[float, float], polygons: list[list[tuple[float, float]]]) -> bool:
    inside_count = sum(1 for polygon in polygons if point_in_polygon(point, polygon))
    return inside_count % 2 == 1


def polygon_abs_area(polygon: list[tuple[float, float]]) -> float:
    area = 0.0
    for (x1, y1), (x2, y2) in zip(polygon, polygon[1:]):
        area += (x1 * y2) - (x2 * y1)
    return abs(area) / 2.0


def close_polygon(polygon: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if len(polygon) >= 3 and distance(polygon[0], polygon[-1]) > 0.01:
        return [*polygon, polygon[0]]
    return polygon


def compound_fill_components(polygons: Iterable[list[tuple[float, float]]]) -> list[list[list[tuple[float, float]]]]:
    closed = [close_polygon(polygon) for polygon in polygons if len(polygon) >= 3]
    if not closed:
        return []
    areas = [polygon_abs_area(polygon) for polygon in closed]
    parents: list[int | None] = []
    for index, polygon in enumerate(closed):
        point = polygon[0]
        parent: int | None = None
        parent_area = float("inf")
        for candidate_index, candidate in enumerate(closed):
            if candidate_index == index or areas[candidate_index] <= areas[index]:
                continue
            if areas[candidate_index] < parent_area and point_in_polygon(point, candidate):
                parent = candidate_index
                parent_area = areas[candidate_index]
        parents.append(parent)

    roots: list[int] = []
    for index, parent in enumerate(parents):
        if parent is None:
            roots.append(index)

    components: list[list[list[tuple[float, float]]]] = []
    for root in roots:
        component: list[list[tuple[float, float]]] = []
        for index, polygon in enumerate(closed):
            ancestor = index
            while parents[ancestor] is not None:
                ancestor = parents[ancestor]  # type: ignore[assignment]
            if ancestor == root:
                component.append(polygon)
        components.append(component)
    return components


def segment_inside_compound_fill(
    start: tuple[float, float],
    end: tuple[float, float],
    polygons: list[list[tuple[float, float]]],
    samples: int = 5,
) -> bool:
    for index in range(1, samples + 1):
        t = index / (samples + 1)
        point = (start[0] + (end[0] - start[0]) * t, start[1] + (end[1] - start[1]) * t)
        if not point_in_compound_fill(point, polygons):
            return False
    return True


def route_within_compound_fill(
    start: tuple[float, float],
    end: tuple[float, float],
    polygons: list[list[tuple[float, float]]],
    grid_step_mm: float,
) -> list[tuple[float, float]] | None:
    """Find a short stitchable detour around a counter within one filled shape."""
    if segment_inside_compound_fill(start, end, polygons):
        return [start, end]

    step = max(grid_step_mm, 0.25)
    key = (
        round(step, 3),
        tuple(tuple((round(x, 3), round(y, 3)) for x, y in polygon) for polygon in polygons),
    )
    grid = _FILL_ROUTE_GRID_CACHE.get(key)
    if grid is None:
        min_x = min(x for polygon in polygons for x, _ in polygon)
        max_x = max(x for polygon in polygons for x, _ in polygon)
        min_y = min(y for polygon in polygons for _, y in polygon)
        max_y = max(y for polygon in polygons for _, y in polygon)
        columns = max(1, int(math.ceil((max_x - min_x) / step)))
        rows = max(1, int(math.ceil((max_y - min_y) / step)))
        if columns * rows > 4000:
            return None
        valid = frozenset(
            (column, row)
            for row in range(rows + 1)
            for column in range(columns + 1)
            if point_in_compound_fill((min_x + column * step, min_y + row * step), polygons)
        )
        grid = (min_x, min_y, step, valid)
        if len(_FILL_ROUTE_GRID_CACHE) >= 128:
            _FILL_ROUTE_GRID_CACHE.clear()
        _FILL_ROUTE_GRID_CACHE[key] = grid
    min_x, min_y, step, valid = grid
    if not valid:
        return None

    def point_at(node: tuple[int, int]) -> tuple[float, float]:
        return min_x + node[0] * step, min_y + node[1] * step

    def nearest_visible_node(point: tuple[float, float]) -> tuple[int, int] | None:
        for node in sorted(valid, key=lambda candidate: distance(point, point_at(candidate)))[:32]:
            if segment_inside_compound_fill(point, point_at(node), polygons):
                return node
        return None

    start_node = nearest_visible_node(start)
    end_node = nearest_visible_node(end)
    if start_node is None or end_node is None:
        return None
    pending: deque[tuple[int, int]] = deque([start_node])
    previous: dict[tuple[int, int], tuple[int, int] | None] = {start_node: None}
    while pending:
        node = pending.popleft()
        if node == end_node:
            break
        for neighbor in ((node[0] - 1, node[1]), (node[0] + 1, node[1]), (node[0], node[1] - 1), (node[0], node[1] + 1)):
            if neighbor not in valid or neighbor in previous:
                continue
            if not segment_inside_compound_fill(point_at(node), point_at(neighbor), polygons):
                continue
            previous[neighbor] = node
            pending.append(neighbor)
    if end_node not in previous:
        return None
    nodes: list[tuple[int, int]] = []
    node: tuple[int, int] | None = end_node
    while node is not None:
        nodes.append(node)
        node = previous[node]
    nodes.reverse()
    route = [start, *(point_at(node) for node in nodes), end]
    if all(distance(a, b) <= 1e-6 or segment_inside_compound_fill(a, b, polygons) for a, b in zip(route, route[1:])):
        return route
    return None


def nearest_point_on_segment(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> tuple[tuple[float, float], float]:
    """Return the closest point on a line segment and its distance."""
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length_squared = dx * dx + dy * dy
    if length_squared <= 1e-12:
        return start, distance(point, start)
    fraction = ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / length_squared
    fraction = max(0.0, min(1.0, fraction))
    closest = (start[0] + fraction * dx, start[1] + fraction * dy)
    return closest, distance(point, closest)


def route_along_compound_boundary(
    start: tuple[float, float],
    end: tuple[float, float],
    polygons: list[list[tuple[float, float]]],
    max_attachment_mm: float,
) -> list[tuple[float, float]] | None:
    """Route between hatch ends along one outline instead of across the fill.

    Hatch row ends normally land directly on an SVG boundary.  When a counter
    blocks the straight connector, using that boundary preserves a continuous
    thread while keeping the connector on an edge that the final hatch covers.
    """
    best: list[tuple[float, float]] | None = None
    best_length: float | None = None
    for polygon in polygons:
        nodes = list(polygon)
        if len(nodes) >= 2 and distance(nodes[0], nodes[-1]) <= 0.01:
            nodes.pop()
        if len(nodes) < 3:
            continue

        start_match: tuple[tuple[float, float], float, int] | None = None
        end_match: tuple[tuple[float, float], float, int] | None = None
        for index, vertex in enumerate(nodes):
            next_vertex = nodes[(index + 1) % len(nodes)]
            start_point, start_distance = nearest_point_on_segment(start, vertex, next_vertex)
            end_point, end_distance = nearest_point_on_segment(end, vertex, next_vertex)
            if start_match is None or start_distance < start_match[1]:
                start_match = (start_point, start_distance, index)
            if end_match is None or end_distance < end_match[1]:
                end_match = (end_point, end_distance, index)
        if start_match is None or end_match is None:
            continue
        start_point, start_distance, start_index = start_match
        end_point, end_distance, end_index = end_match
        if max(start_distance, end_distance) > max_attachment_mm:
            continue

        # Walk the closed outline in both directions and retain the shorter arc.
        forward = [start, start_point]
        index = (start_index + 1) % len(nodes)
        while index != (end_index + 1) % len(nodes):
            forward.append(nodes[index])
            index = (index + 1) % len(nodes)
        forward.extend([end_point, end])

        reverse = [start, start_point]
        index = start_index
        target = (end_index + 1) % len(nodes)
        while True:
            reverse.append(nodes[index])
            index = (index - 1) % len(nodes)
            if index == target:
                reverse.append(nodes[index])
                break
        reverse.extend([end_point, end])

        for candidate in (forward, reverse):
            candidate = dedupe_points(candidate)
            candidate_length = sum(distance(a, b) for a, b in zip(candidate, candidate[1:]))
            if best_length is None or candidate_length < best_length:
                best = candidate
                best_length = candidate_length
    return best


def connect_adjacent_fill_rows(
    rows: list[list[tuple[float, float]]],
    polygons: list[list[tuple[float, float]]],
    max_stitch_mm: float,
    spacing_mm: float,
    connector_limit_mm: float | None = None,
) -> list[list[tuple[float, float]]]:
    connected: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] | None = None
    connector_limit = connector_limit_mm if connector_limit_mm is not None else max(spacing_mm * 3.0, 0.8)
    for row in rows:
        if current is None:
            current = list(row)
            continue
        connector_length = distance(current[-1], row[0])
        safe_connector = (
            connector_length <= connector_limit
            and segment_inside_compound_fill(current[-1], row[0], polygons)
        )
        if safe_connector:
            connector = split_long_stitches([current[-1], row[0]], max_stitch_mm)
            current.extend(connector[1:])
            current.extend(row[1:])
        else:
            # A hole or a separate island sits between these rows. Keep it as a
            # separate run so the writer emits a jump/trim, never a fake stitch.
            connected.append(current)
            current = list(row)
    if current:
        connected.append(current)
    return connected


def connect_fill_rows_as_island(
    rows: list[list[tuple[float, float]]],
    polygons: list[list[tuple[float, float]]],
    max_stitch_mm: float,
    min_stitch_mm: float,
    prefer_boundary_routes: bool = False,
    allow_boundary_routes: bool = True,
    allow_interior_routes: bool = True,
) -> list[list[tuple[float, float]]]:
    remaining = [list(row) for row in rows if len(row) >= 2]
    if not remaining:
        return []
    connected: list[list[tuple[float, float]]] = []
    current = remaining.pop(0)
    direct_connector_limit = max(min_stitch_mm * 3.0, 0.75)
    while remaining:
        end = current[-1]
        best: tuple[float, int, bool] | None = None
        for index, row in enumerate(remaining):
            start_distance = distance(end, row[0])
            end_distance = distance(end, row[-1])
            if (
                (not prefer_boundary_routes or start_distance <= direct_connector_limit)
                and segment_inside_compound_fill(end, row[0], polygons)
            ):
                candidate = (start_distance, index, False)
                if best is None or candidate < best:
                    best = candidate
            if (
                (not prefer_boundary_routes or end_distance <= direct_connector_limit)
                and segment_inside_compound_fill(end, row[-1], polygons)
            ):
                candidate = (end_distance, index, True)
                if best is None or candidate < best:
                    best = candidate
        if best is None:
            # A counter split the hatch rows. Consider every remaining row,
            # not merely the next scanline: a sewing machine can approach the
            # next stitch from any direction, so the shortest contained route
            # is usually around the nearby side of the counter.
            attachment_limit = max(min_stitch_mm * 4.0, 1.25)
            detour_options: list[tuple[float, int, bool, list[tuple[float, float]]]] = []
            for index, candidate_row in enumerate(remaining):
                for reverse, target in ((False, candidate_row[0]), (True, candidate_row[-1])):
                    route = (
                        route_along_compound_boundary(end, target, polygons, attachment_limit)
                        if allow_boundary_routes
                        else None
                    )
                    if route is None and allow_interior_routes:
                        route = route_within_compound_fill(end, target, polygons, min_stitch_mm)
                    if route is None:
                        continue
                    route_length = sum(distance(a, b) for a, b in zip(route, route[1:]))
                    detour_options.append((route_length, index, reverse, route))

            if not detour_options:
                # No safe thread path exists inside this component. Keep it as
                # a separate run so the writer makes one deliberate trim.
                connected.append(current)
                nearest_index, nearest_reverse = min(
                    (
                        (index, distance(end, row[-1]) < distance(end, row[0]))
                        for index, row in enumerate(remaining)
                    ),
                    key=lambda candidate: min(
                        distance(end, remaining[candidate[0]][0]),
                        distance(end, remaining[candidate[0]][-1]),
                    ),
                )
                current = remaining.pop(nearest_index)
                if nearest_reverse:
                    current.reverse()
                continue
            _, best_index, best_reverse, connector_points = min(detour_options, key=lambda candidate: candidate[0])
            row = remaining.pop(best_index)
            if best_reverse:
                row.reverse()
        else:
            _, best_index, best_reverse = best
            row = remaining.pop(best_index)
            if best_reverse:
                row.reverse()
            connector_points = [end, row[0]]
        connector = split_long_stitches(connector_points, max_stitch_mm, min_stitch_mm=min_stitch_mm)
        current.extend(connector[1:])
        current.extend(row[1:])
    connected.append(current)
    return connected


def add_edge_return_rows(
    rows: list[list[tuple[float, float]]],
    max_stitch_mm: float,
    min_stitch_mm: float,
) -> list[list[tuple[float, float]]]:
    returned: list[list[tuple[float, float]]] = []
    for row in rows:
        if len(row) < 2:
            continue
        forward = list(row)
        reverse = list(reversed(row))
        returned.append(forward)
        returned.append(split_long_stitches(reverse, max_stitch_mm, min_stitch_mm=min_stitch_mm))
    return returned


def row_bounds(row: list[tuple[float, float]]) -> tuple[float, float, float, float]:
    xs = [x for x, _ in row]
    ys = [y for _, y in row]
    return min(xs), min(ys), max(xs), max(ys)


def row_bounds_gap(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    dx = max(first[0] - second[2], second[0] - first[2], 0.0)
    dy = max(first[1] - second[3], second[1] - first[3], 0.0)
    return math.hypot(dx, dy)


def cluster_hatch_rows(
    rows: list[list[tuple[float, float]]],
    spacing_mm: float,
) -> list[list[list[tuple[float, float]]]]:
    if len(rows) <= 1:
        return [rows] if rows else []
    gap_limit = max(spacing_mm * 2.25, 0.75)
    parents = list(range(len(rows)))
    bounds_list = [row_bounds(row) for row in rows]
    order = sorted(range(len(rows)), key=lambda index: bounds_list[index][0])

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[right_root] = left_root

    for pos, left_index in enumerate(order):
        left_bounds = bounds_list[left_index]
        for right_index in order[pos + 1 :]:
            right_bounds = bounds_list[right_index]
            if right_bounds[0] > left_bounds[2] + gap_limit:
                break
            if row_bounds_gap(left_bounds, right_bounds) <= gap_limit:
                union(left_index, right_index)

    grouped: dict[int, list[list[tuple[float, float]]]] = {}
    first_seen: dict[int, int] = {}
    for index, row in enumerate(rows):
        root = find(index)
        grouped.setdefault(root, []).append(row)
        first_seen.setdefault(root, index)
    return [grouped[root] for root in sorted(grouped, key=lambda root: first_seen[root])]


def hatch_fill(
    polygon: list[tuple[float, float]],
    spacing_mm: float,
    max_stitch_mm: float,
    fill_angle_deg: float = 0.0,
) -> list[list[tuple[float, float]]]:
    return hatch_compound_fill([polygon], spacing_mm, max_stitch_mm, fill_angle_deg)


def hatch_compound_fill(
    polygons: Iterable[list[tuple[float, float]]],
    spacing_mm: float,
    max_stitch_mm: float,
    fill_angle_deg: float = 0.0,
    min_stitch_mm: float = MIN_FILL_STITCH_MM,
    side_lanes: bool = False,
) -> list[list[tuple[float, float]]]:
    closed_polygons: list[list[tuple[float, float]]] = []
    for polygon in polygons:
        if len(polygon) < 3:
            continue
        if distance(polygon[0], polygon[-1]) > 0.01:
            polygon = [*polygon, polygon[0]]
        closed_polygons.append(polygon)
    if not closed_polygons:
        return []

    angle_rad = math.radians(fill_angle_deg)
    if abs(angle_rad) > 1e-6:
        xs = [x for polygon in closed_polygons for x, _ in polygon]
        ys = [y for polygon in closed_polygons for _, y in polygon]
        origin = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
        closed_polygons = [
            [rotate_point(point, -angle_rad, origin) for point in polygon]
            for polygon in closed_polygons
        ]
    else:
        origin = (0.0, 0.0)

    min_y = min(y for polygon in closed_polygons for _, y in polygon)
    max_y = max(y for polygon in closed_polygons for _, y in polygon)
    rows: list[list[tuple[float, float]]] = []
    y = min_y
    row_index = 0

    while y <= max_y:
        xs: list[float] = []
        for polygon in closed_polygons:
            for (x1, y1), (x2, y2) in zip(polygon, polygon[1:]):
                if abs(y1 - y2) < 1e-9:
                    continue
                crosses = (y1 <= y < y2) or (y2 <= y < y1)
                if crosses:
                    t = (y - y1) / (y2 - y1)
                    xs.append(x1 + (x2 - x1) * t)

        xs.sort()
        for left, right in zip(xs[0::2], xs[1::2]):
            run_width = right - left
            if run_width < MIN_DETAIL_RUN_MM:
                continue
            if run_width < min_stitch_mm:
                center_x = (left + right) / 2
                left = center_x - (min_stitch_mm / 2)
                right = center_x + (min_stitch_mm / 2)
            run = [(left, y), (right, y)]
            if row_index % 2:
                run.reverse()
            row = split_long_stitches(run, max_stitch_mm, min_stitch_mm=min_stitch_mm)
            if len(row) < 2:
                continue
            rows.append(row)
            row_index += 1
        y += max(spacing_mm, 0.1)

    connector_limit_mm: float | None = None
    if side_lanes and abs(angle_rad) <= 1e-6:
        xs = [x for row in rows for x, _ in row]
        ys = [y for row in rows for _, y in row]
        width = max(xs) - min(xs) if xs else 0.0
        height = max(ys) - min(ys) if ys else 0.0
        aspect = width / height if height > 0 else 0.0
        if width >= 5.0 and height >= 5.0 and 0.55 <= aspect <= 1.8:
            rows = add_edge_return_rows(rows, max_stitch_mm, min_stitch_mm)
            connector_limit_mm = max(spacing_mm * 8.0, 2.5)

    clustered_rows: list[list[tuple[float, float]]] = []
    for cluster in cluster_hatch_rows(rows, spacing_mm):
        clustered_rows.extend(connect_adjacent_fill_rows(cluster, closed_polygons, max_stitch_mm, spacing_mm, connector_limit_mm))
    rows = clustered_rows
    if abs(angle_rad) > 1e-6:
        rows = [[rotate_point(point, angle_rad, origin) for point in row] for row in rows]

    return rows


def island_tatami_compound_fill(
    polygons: Iterable[list[tuple[float, float]]],
    spacing_mm: float,
    max_stitch_mm: float,
    fill_angle_deg: float,
    min_stitch_mm: float = MIN_FILL_STITCH_MM,
    prefer_boundary_routes: bool = False,
    allow_boundary_routes: bool = True,
    allow_interior_routes: bool = True,
) -> list[list[tuple[float, float]]]:
    polygon_list = [polygon for polygon in polygons if len(polygon) >= 3]
    if not polygon_list:
        return []
    rows = optimized_hatch_compound_fill(
        polygon_list,
        spacing_mm,
        max_stitch_mm,
        fill_angle_deg,
        min_stitch_mm=min_stitch_mm,
    )
    return connect_fill_rows_as_island(
        rows,
        polygon_list,
        max_stitch_mm,
        min_stitch_mm,
        prefer_boundary_routes=prefer_boundary_routes,
        allow_boundary_routes=allow_boundary_routes,
        allow_interior_routes=allow_interior_routes,
    )


def outline_guided_compound_fill(
    polygons: Iterable[list[tuple[float, float]]],
    spacing_mm: float,
    max_stitch_mm: float,
    fill_angle_deg: float,
    min_stitch_mm: float = MIN_FILL_STITCH_MM,
    prefer_boundary_routes: bool = False,
    allow_boundary_routes: bool = True,
    allow_interior_routes: bool = True,
) -> list[list[tuple[float, float]]]:
    polygon_list = [polygon for polygon in polygons if len(polygon) >= 3]
    if not polygon_list:
        return []
    rows = hatch_compound_fill(
        polygon_list,
        spacing_mm,
        max_stitch_mm,
        fill_angle_deg,
        min_stitch_mm=min_stitch_mm,
    )
    return connect_fill_rows_as_island(
        rows,
        polygon_list,
        max_stitch_mm,
        min_stitch_mm,
        prefer_boundary_routes=prefer_boundary_routes,
        allow_boundary_routes=allow_boundary_routes,
        allow_interior_routes=allow_interior_routes,
    )


def stitch_micro_score(
    runs: Iterable[list[tuple[float, float]]],
    min_stitch_mm: float = MIN_FILL_STITCH_MM,
) -> tuple[int, float, int, float]:
    short_count = 0
    deficit = 0.0
    stitch_count = 0
    travel_total = 0.0
    previous_end: tuple[float, float] | None = None
    for run in runs:
        if previous_end is not None and run:
            travel_total += min(distance(previous_end, run[0]), distance(previous_end, run[-1]))
        if run:
            previous_end = run[-1]
        for start, end in zip(run, run[1:]):
            span = distance(start, end)
            if span <= 0:
                continue
            stitch_count += 1
            if span < min_stitch_mm:
                short_count += 1
                deficit += min_stitch_mm - span
    return short_count, deficit, stitch_count, travel_total


def simplify_contour_points(
    points: list[tuple[float, float]],
    min_stitch_mm: float = MIN_FILL_STITCH_MM,
) -> list[tuple[float, float]]:
    if len(points) < 2:
        return points
    closed = distance(points[0], points[-1]) <= 0.01
    source = points[:-1] if closed else points
    simplified = [source[0]]
    for point in source[1:]:
        if distance(simplified[-1], point) >= min_stitch_mm:
            simplified.append(point)
    if len(simplified) >= 2 and closed:
        if distance(simplified[-1], simplified[0]) >= min_stitch_mm:
            simplified.append(simplified[0])
        else:
            simplified[-1] = simplified[0]
    return simplified


def fill_angle_candidates(fill_angle_deg: float) -> list[float]:
    seen: set[float] = set()
    candidates: list[float] = []
    for offset in (0, -5, 5, -10, 10, -15, 15, -22.5, 22.5, -30, 30, -45, 45, 90):
        angle = ((fill_angle_deg + offset + 90) % 180) - 90
        rounded = round(angle, 3)
        if rounded not in seen:
            seen.add(rounded)
            candidates.append(angle)
    return candidates


def optimized_hatch_compound_fill(
    polygons: Iterable[list[tuple[float, float]]],
    spacing_mm: float,
    max_stitch_mm: float,
    fill_angle_deg: float,
    min_stitch_mm: float = MIN_FILL_STITCH_MM,
) -> list[list[tuple[float, float]]]:
    polygon_list = [polygon for polygon in polygons if len(polygon) >= 3]
    if not polygon_list:
        return []
    best_rows: list[list[tuple[float, float]]] | None = None
    best_score: tuple[bool, float, int, float, int, float, int] | None = None
    baseline_rows = hatch_compound_fill(
        polygon_list,
        spacing_mm,
        max_stitch_mm,
        fill_angle_deg,
        min_stitch_mm=min_stitch_mm,
    )
    baseline_short_count, _, _, _ = stitch_micro_score(baseline_rows, min_stitch_mm)
    for angle in fill_angle_candidates(fill_angle_deg):
        rows = hatch_compound_fill(
            polygon_list,
            spacing_mm,
            max_stitch_mm,
            angle,
            min_stitch_mm=min_stitch_mm,
        )
        short_count, deficit, stitch_count, travel_total = stitch_micro_score(rows, min_stitch_mm)
        safety_improvement = baseline_short_count - short_count
        needs_angle_change = safety_improvement >= max(4, baseline_short_count * 0.25)
        score = (
            not needs_angle_change and abs(angle - fill_angle_deg) > 0.001,
            abs(angle - fill_angle_deg),
            short_count,
            round(deficit, 4),
            len(rows),
            round(travel_total, 3),
            stitch_count,
        )
        if best_score is None or score < best_score:
            best_rows = rows
            best_score = score
    return best_rows or []


def mixed_hatch_compound_fill(
    polygons: Iterable[list[tuple[float, float]]],
    spacing_mm: float,
    max_stitch_mm: float,
    fill_angle_deg: float,
    min_stitch_mm: float = MIN_FILL_STITCH_MM,
) -> list[list[tuple[float, float]]]:
    polygon_list = [polygon for polygon in polygons if len(polygon) >= 3]
    if not polygon_list:
        return []
    return hatch_compound_fill(
        polygon_list,
        spacing_mm,
        max_stitch_mm,
        0.0,
        min_stitch_mm=min_stitch_mm,
        side_lanes=False,
    )


def polygon_centroid(polygon: list[tuple[float, float]]) -> tuple[float, float]:
    if not polygon:
        return 0.0, 0.0
    signed_area = 0.0
    cx = 0.0
    cy = 0.0
    for (x1, y1), (x2, y2) in zip(polygon, polygon[1:]):
        cross = (x1 * y2) - (x2 * y1)
        signed_area += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    if abs(signed_area) < 1e-9:
        return (
            sum(x for x, _ in polygon) / len(polygon),
            sum(y for _, y in polygon) / len(polygon),
        )
    signed_area *= 0.5
    return cx / (6.0 * signed_area), cy / (6.0 * signed_area)


def contour_compound_fill(
    polygons: Iterable[list[tuple[float, float]]],
    spacing_mm: float,
    max_stitch_mm: float,
    min_stitch_mm: float = MIN_FILL_STITCH_MM,
) -> list[list[tuple[float, float]]]:
    rows: list[list[tuple[float, float]]] = []
    for polygon in polygons:
        if len(polygon) < 3:
            continue
        if distance(polygon[0], polygon[-1]) > 0.01:
            polygon = [*polygon, polygon[0]]
        center = polygon_centroid(polygon)
        max_radius = max(distance(center, point) for point in polygon)
        if max_radius < min_stitch_mm:
            continue
        layer = 0
        offset = 0.0
        while offset < max_radius - min_stitch_mm:
            scale = max(0.05, 1.0 - (offset / max_radius))
            contour = [
                (
                    center[0] + (point[0] - center[0]) * scale,
                    center[1] + (point[1] - center[1]) * scale,
                )
                for point in polygon
            ]
            contour = simplify_contour_points(contour, min_stitch_mm=min_stitch_mm)
            contour = split_long_stitches(contour, max_stitch_mm, min_stitch_mm=min_stitch_mm)
            if len(contour) >= 2:
                if layer % 2:
                    contour.reverse()
                rows.append(contour)
            offset += max(spacing_mm, min_stitch_mm)
            layer += 1
    return rows


def outline_compound_fill(
    polygons: Iterable[list[tuple[float, float]]],
    max_stitch_mm: float,
    min_stitch_mm: float = MIN_FILL_STITCH_MM,
) -> list[list[tuple[float, float]]]:
    rows: list[list[tuple[float, float]]] = []
    for polygon in polygons:
        if len(polygon) < 2:
            continue
        if len(polygon) >= 3 and distance(polygon[0], polygon[-1]) > 0.01:
            polygon = [*polygon, polygon[0]]
        outline = simplify_contour_points(polygon, min_stitch_mm=min_stitch_mm)
        outline = split_long_stitches(outline, max_stitch_mm, min_stitch_mm=min_stitch_mm)
        if len(outline) >= 2:
            rows.append(outline)
    return rows


def extract_runs(
    svg_file: Path,
    sample_step_mm: float,
    fill_spacing_mm: float,
    max_stitch_mm: float,
    fill_angle_deg: float = 0.0,
    fill_mode: str = "tatami",
    path_planning: str = "min_cuts",
    min_stitch_mm: float = MIN_FILL_STITCH_MM,
) -> list[StitchRun]:
    svg = SVG.parse(str(svg_file), reify=True)
    sample_step_px = sample_step_mm * SVG_PX_PER_MM
    fill_mode = fill_mode.lower().strip()
    if fill_mode not in {
        "horizontal",
        "tatami",
        "island_tatami",
        "outline_fill",
        "crosshatch",
        "mixed",
        "outline",
        "contour",
    }:
        fill_mode = "tatami"
    fill_angles = [0.0] if fill_mode == "horizontal" else [fill_angle_deg]
    if fill_mode == "crosshatch":
        fill_angles.append(-fill_angle_deg if abs(fill_angle_deg) > 0.001 else 90.0)
    runs: list[StitchRun] = []
    next_component_id = 0

    for element in svg.elements():
        if isinstance(element, SVG):
            continue
        if not isinstance(element, (SvgPath, Shape)):
            continue

        color, filled = path_color(element)
        if color == "none":
            continue

        try:
            path = SvgPath(element)
        except Exception:
            continue

        subpaths_mm = [to_mm(subpath_px) for subpath_px in flatten_path(path, sample_step_px)]
        if filled and fill_mode not in {"outline", "contour"}:
            fill_polygons = [subpath_mm for subpath_mm in subpaths_mm if len(subpath_mm) >= 3]
            for component in compound_fill_components(fill_polygons):
                component_id = next_component_id
                next_component_id += 1
                if fill_mode == "outline_fill":
                    if path_planning == "clean_top":
                        for row in outline_compound_fill(
                            component,
                            max_stitch_mm,
                            min_stitch_mm=min_stitch_mm,
                        ):
                            runs.append(StitchRun(color=color, points_mm=row, component_id=component_id, phase=0))
                    fill_rows = outline_guided_compound_fill(
                        component,
                        fill_spacing_mm,
                        max_stitch_mm,
                        fill_angle_deg,
                        min_stitch_mm=min_stitch_mm,
                        prefer_boundary_routes=path_planning == "clean_top",
                        allow_boundary_routes=True,
                        allow_interior_routes=path_planning != "clean_top",
                    )
                    for row in fill_rows:
                        runs.append(StitchRun(color=color, points_mm=row, component_id=component_id))
                    continue
                if fill_mode == "island_tatami":
                    if path_planning == "clean_top":
                        for row in outline_compound_fill(
                            component,
                            max_stitch_mm,
                            min_stitch_mm=min_stitch_mm,
                        ):
                            runs.append(StitchRun(color=color, points_mm=row, component_id=component_id, phase=0))
                    fill_rows = island_tatami_compound_fill(
                        component,
                        fill_spacing_mm,
                        max_stitch_mm,
                        fill_angle_deg,
                        min_stitch_mm=min_stitch_mm,
                        prefer_boundary_routes=path_planning == "clean_top",
                        allow_boundary_routes=True,
                        allow_interior_routes=path_planning != "clean_top",
                    )
                    for row in fill_rows:
                        runs.append(StitchRun(color=color, points_mm=row, component_id=component_id))
                    continue
                if fill_mode == "mixed":
                    fill_rows = mixed_hatch_compound_fill(
                        component,
                        fill_spacing_mm,
                        max_stitch_mm,
                        fill_angle_deg,
                        min_stitch_mm=min_stitch_mm,
                    )
                    for row in fill_rows:
                        runs.append(StitchRun(color=color, points_mm=row, component_id=component_id))
                    continue
                for angle in fill_angles:
                    fill_rows = optimized_hatch_compound_fill(
                        component,
                        fill_spacing_mm,
                        max_stitch_mm,
                        angle,
                        min_stitch_mm=min_stitch_mm,
                    )
                    for row in fill_rows:
                        runs.append(StitchRun(color=color, points_mm=row, component_id=component_id))
        elif filled and fill_mode == "contour":
            fill_polygons = [subpath_mm for subpath_mm in subpaths_mm if len(subpath_mm) >= 3]
            for component in compound_fill_components(fill_polygons):
                component_id = next_component_id
                next_component_id += 1
                fill_rows = optimized_hatch_compound_fill(
                    component,
                    fill_spacing_mm,
                    max_stitch_mm,
                    fill_angle_deg,
                    min_stitch_mm=min_stitch_mm,
                )
                for row in fill_rows:
                    runs.append(StitchRun(color=color, points_mm=row, component_id=component_id))
                for row in outline_compound_fill(
                    component,
                    max_stitch_mm,
                    min_stitch_mm=min_stitch_mm,
                ):
                    runs.append(StitchRun(color=color, points_mm=row, component_id=component_id, phase=2))
        else:
            for subpath_mm in subpaths_mm:
                component_id = next_component_id
                next_component_id += 1
                runs.append(
                    StitchRun(
                        color=color,
                        points_mm=split_long_stitches(
                            subpath_mm,
                            max_stitch_mm,
                            min_stitch_mm=min_stitch_mm,
                        ),
                        component_id=component_id,
                        phase=0,
                    )
                )

    return route_stitch_runs(runs, mode=path_planning, max_stitch_mm=max_stitch_mm)


def bounds(runs: Iterable[StitchRun]) -> tuple[float, float, float, float]:
    points = [point for run in runs for point in run.points_mm]
    if not points:
        raise ValueError("No stitchable SVG paths were found.")
    return (
        min(x for x, _ in points),
        min(y for _, y in points),
        max(x for x, _ in points),
        max(y for _, y in points),
    )


def transform_runs(
    runs: list[StitchRun],
    fit_width_mm: float | None,
    fit_height_mm: float | None,
    center: bool,
) -> list[StitchRun]:
    min_x, min_y, max_x, max_y = bounds(runs)
    width = max(max_x - min_x, 0.001)
    height = max(max_y - min_y, 0.001)

    scale = 1.0
    if fit_width_mm and fit_height_mm:
        scale = min(fit_width_mm / width, fit_height_mm / height)
    elif fit_width_mm:
        scale = fit_width_mm / width
    elif fit_height_mm:
        scale = fit_height_mm / height

    scaled_width = width * scale
    scaled_height = height * scale
    offset_x = min_x + (scaled_width / (2 * scale) if center else 0)
    offset_y = min_y + (scaled_height / (2 * scale) if center else 0)

    transformed: list[StitchRun] = []
    for run in runs:
        points = [((x - offset_x) * scale, (y - offset_y) * scale) for x, y in run.points_mm]
        transformed.append(
            StitchRun(
                color=run.color,
                points_mm=points,
                component_id=run.component_id,
                phase=run.phase,
            )
        )
    return transformed


def extract_runs_for_final_size(
    svg_file: Path,
    *,
    sample_step_mm: float,
    fill_spacing_mm: float,
    max_stitch_mm: float,
    fit_width_mm: float | None,
    fit_height_mm: float | None,
    center: bool,
    fill_angle_deg: float = 0.0,
    fill_mode: str = "tatami",
    path_planning: str = "min_cuts",
    min_stitch_mm: float = MIN_FILL_STITCH_MM,
) -> list[StitchRun]:
    runs = extract_runs(
        svg_file,
        sample_step_mm=sample_step_mm,
        fill_spacing_mm=fill_spacing_mm,
        max_stitch_mm=max_stitch_mm,
        fill_angle_deg=fill_angle_deg,
        fill_mode=fill_mode,
        path_planning=path_planning,
        min_stitch_mm=min_stitch_mm,
    )
    min_x, min_y, max_x, max_y = bounds(runs)
    width = max(max_x - min_x, 0.001)
    height = max(max_y - min_y, 0.001)
    scale = 1.0
    if fit_width_mm and fit_height_mm:
        scale = min(fit_width_mm / width, fit_height_mm / height)
    elif fit_width_mm:
        scale = fit_width_mm / width
    elif fit_height_mm:
        scale = fit_height_mm / height

    if abs(scale - 1.0) > 0.001:
        runs = extract_runs(
            svg_file,
            sample_step_mm=max(sample_step_mm / scale, 0.01),
            fill_spacing_mm=max(fill_spacing_mm / scale, 0.01),
            max_stitch_mm=max(max_stitch_mm / scale, 0.1),
            fill_angle_deg=fill_angle_deg,
            fill_mode=fill_mode,
            path_planning=path_planning,
            min_stitch_mm=max(min_stitch_mm / scale, 0.01),
        )
    return transform_runs(
        runs,
        fit_width_mm=fit_width_mm,
        fit_height_mm=fit_height_mm,
        center=center,
    )


def make_thread(hex_color: str) -> EmbThread:
    thread = EmbThread()
    thread.set_hex_color(hex_color)
    thread.description = f"SVG {hex_color}"
    return thread


def write_embroidery(runs: list[StitchRun], output: Path) -> None:
    pattern = EmbPattern()
    active_color: str | None = None
    previous_point: tuple[float, float] | None = None

    for run in runs:
        if len(run.points_mm) < 2:
            continue
        if run.color != active_color:
            pattern.add_thread(make_thread(run.color))
            if active_color is not None:
                pattern.color_change()
            active_color = run.color

        first_x, first_y = run.points_mm[0]
        # Runs are separated only when no stitchable route through the filled
        # area exists. Never carry thread across that gap without a trim.
        if previous_point is not None and distance(previous_point, (first_x, first_y)) > 0.001:
            pattern.trim()
        pattern.add_stitch_absolute(
            embroidery.JUMP,
            int(round(first_x * EMB_UNITS_PER_MM)),
            int(round(first_y * EMB_UNITS_PER_MM)),
        )
        for x, y in run.points_mm[1:]:
            pattern.add_stitch_absolute(
                embroidery.STITCH,
                int(round(x * EMB_UNITS_PER_MM)),
                int(round(y * EMB_UNITS_PER_MM)),
            )
        previous_point = run.points_mm[-1]

    if pattern.count_stitches() == 0:
        raise ValueError("No stitches were generated.")
    pattern.end()
    pattern.write(str(output))


def positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return number


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert SVG files into Brother-compatible embroidery files."
    )
    parser.add_argument("input", type=Path, help="SVG file to convert")
    parser.add_argument("-o", "--output", type=Path, help="Output embroidery file")
    parser.add_argument(
        "--format",
        default="pes",
        help="Embroidery format understood by pyembroidery; default: pes",
    )
    parser.add_argument(
        "--fit-width-mm",
        type=positive_float,
        help="Scale the design to this width in millimeters",
    )
    parser.add_argument(
        "--fit-height-mm",
        type=positive_float,
        help="Scale the design to this height in millimeters",
    )
    parser.add_argument(
        "--sample-step-mm",
        type=positive_float,
        default=0.8,
        help="Curve sampling distance in millimeters; default: 0.8",
    )
    parser.add_argument(
        "--fill-spacing-mm",
        type=positive_float,
        default=0.4,
        help="Distance between hatch-fill rows in millimeters; default: 0.5",
    )
    parser.add_argument(
        "--max-stitch-mm",
        type=positive_float,
        default=5.0,
        help="Maximum stitch length in millimeters; default: 3.0",
    )
    parser.add_argument(
        "--fill-angle-deg",
        type=float,
        default=0.0,
        help="Fill stitch angle in degrees for filled SVG shapes; default: 0",
    )
    parser.add_argument(
        "--fill-mode",
        choices=(
            "mixed",
            "island_tatami",
            "outline_fill",
            "contour",
            "tatami",
            "horizontal",
            "crosshatch",
            "outline",
        ),
        default="tatami",
        help="Fill style for filled SVG shapes; default: tatami",
    )
    parser.add_argument(
        "--no-center",
        action="store_true",
        help="Keep the design's top-left origin instead of centering it around the hoop origin",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_file = args.input
    if input_file.suffix.lower() != ".svg":
        raise SystemExit("Input file must be an SVG.")
    if not input_file.exists():
        raise SystemExit(f"Input file not found: {input_file}")

    output = args.output or input_file.with_suffix(f".{args.format.lower()}")
    if output.suffix == "":
        output = output.with_suffix(f".{args.format.lower()}")
    runs = extract_runs_for_final_size(
        input_file,
        sample_step_mm=args.sample_step_mm,
        fill_spacing_mm=args.fill_spacing_mm,
        max_stitch_mm=args.max_stitch_mm,
        fill_angle_deg=args.fill_angle_deg,
        fill_mode=args.fill_mode,
        fit_width_mm=args.fit_width_mm,
        fit_height_mm=args.fit_height_mm,
        center=not args.no_center,
    )
    write_embroidery(runs, output)

    min_x, min_y, max_x, max_y = bounds(runs)
    print(f"Wrote {output}")
    print(f"Design size: {max_x - min_x:.1f} x {max_y - min_y:.1f} mm")
    print(f"Stitch runs: {len(runs)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
