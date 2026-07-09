from __future__ import annotations

import argparse
import html
import json
import math
import re
from pathlib import Path

import pyembroidery as embroidery

from image_digitizer import image_to_segments, is_raster_source, svg_needs_rasterization
from svg2brother import extract_runs_for_final_size, positive_float, make_thread
from thread_catalog import available_thread_brands, load_thread_catalog
from thread_inventory import closest_inventory_match, load_inventory, normalize_hex, rgb_distance
from thread_settings import (
    DEFAULT_THREAD_WEIGHT,
    minimum_fill_spacing,
    normalize_thread_weight,
    thread_weight_label,
)


EMB_UNITS_PER_MM = 10.0
DEFAULT_COLORS = [
    "#1f77b4",
    "#d62728",
    "#2ca02c",
    "#ff7f0e",
    "#9467bd",
    "#8c564b",
    "#e377c2",
    "#17becf",
]
SEGMENT_KIND_CODES = {
    "jump": 0,
    "stitch": 1,
    "travel_after_trim": 2,
    "travel_after_color_change": 3,
}


def command_name(command: int) -> str:
    command &= embroidery.COMMAND_MASK
    if command == embroidery.STITCH:
        return "stitch"
    if command == embroidery.JUMP:
        return "jump"
    if command == embroidery.TRIM:
        return "trim"
    if command == embroidery.COLOR_CHANGE:
        return "color_change"
    if command == embroidery.END:
        return "end"
    return f"command_{command}"


def thread_color(pattern, color_index: int) -> str:
    if 0 <= color_index < len(pattern.threadlist):
        try:
            return pattern.threadlist[color_index].hex_color()
        except Exception:
            pass
    return DEFAULT_COLORS[color_index % len(DEFAULT_COLORS)]


def thread_label(pattern, color_index: int) -> str:
    if 0 <= color_index < len(pattern.threadlist):
        thread = pattern.threadlist[color_index]
        details = [thread.description, thread.catalog_number, thread.brand]
        label = " ".join(str(part) for part in details if part)
        if label:
            return label
    return f"Thread {color_index + 1}"


def collect_segments(pattern) -> tuple[list[dict], list[dict], list[dict], dict]:
    segments: list[dict] = []
    commands: list[dict] = []
    color_blocks: list[dict] = []
    color_index = 0
    previous: tuple[float, float] | None = None
    pending_travel: str | None = None
    active_block: dict | None = None
    counts = {
        "needle_points": 0,
        "stitch_segments": 0,
        "jumps": 0,
        "trims": 0,
        "color_changes": 0,
        "ends": 0,
    }

    def ensure_block() -> dict:
        nonlocal active_block
        if active_block is None:
            active_block = {
                "index": len(color_blocks),
                "thread": color_index,
                "color": thread_color(pattern, color_index),
                "stitches": 0,
            }
            color_blocks.append(active_block)
        return active_block

    for raw_x, raw_y, raw_command in pattern.stitches:
        command = raw_command & embroidery.COMMAND_MASK
        x = raw_x / EMB_UNITS_PER_MM
        y = raw_y / EMB_UNITS_PER_MM
        name = command_name(command)
        commands.append(
            {
                "x": x,
                "y": y,
                "command": name,
                "color": color_index,
                "step": counts["needle_points"],
            }
        )

        if command == embroidery.COLOR_CHANGE:
            counts["color_changes"] += 1
            color_index += 1
            active_block = None
            previous = (x, y)
            pending_travel = "travel_after_color_change"
            continue
        if command == embroidery.END:
            counts["ends"] += 1
            break
        if command == embroidery.TRIM:
            counts["trims"] += 1
            previous = (x, y)
            pending_travel = "travel_after_trim"
            continue

        if previous is not None and command in {embroidery.STITCH, embroidery.JUMP}:
            is_stitch = command == embroidery.STITCH
            block = ensure_block()
            segment_kind = "stitch" if is_stitch else pending_travel or "jump"
            segments.append(
                {
                    "x1": previous[0],
                    "y1": previous[1],
                    "x2": x,
                    "y2": y,
                    "kind": segment_kind,
                    "color": block["color"],
                    "colorIndex": color_index,
                    "blockIndex": block["index"],
                }
            )
            if is_stitch:
                stitch_step = counts["needle_points"] + 1
                counts["stitch_segments"] += 1
                counts["needle_points"] += 1
                block["stitches"] += 1
            else:
                stitch_step = counts["needle_points"]
                counts["jumps"] += 1
            segments[-1]["step"] = stitch_step
            pending_travel = None

        previous = (x, y)

    return segments, commands, color_blocks, counts


def design_bounds(segments: list[dict], commands: list[dict]) -> tuple[float, float, float, float]:
    points: list[tuple[float, float]] = []
    for segment in segments:
        points.append((segment["x1"], segment["y1"]))
        points.append((segment["x2"], segment["y2"]))
    for command in commands:
        points.append((command["x"], command["y"]))
    if not points:
        raise ValueError("No stitches were found in the embroidery file.")
    min_x = min(x for x, _ in points)
    min_y = min(y for _, y in points)
    max_x = max(x for x, _ in points)
    max_y = max(y for _, y in points)
    return min_x, min_y, max_x, max_y


def render_polyline(points: list[tuple[float, float]], color: str, class_name: str) -> str:
    encoded_points = " ".join(f"{x:.3f},{y:.3f}" for x, y in points)
    return (
        f'<polyline class="{class_name}" points="{encoded_points}" '
        f'stroke="{html.escape(color)}" />'
    )


def render_stitch_layers(segments: list[dict]) -> str:
    parts: list[str] = []
    for segment in segments:
        parts.append(
            '<line class="{kind}" x1="{x1:.3f}" y1="{y1:.3f}" x2="{x2:.3f}" y2="{y2:.3f}" '
            'stroke="{color}" data-thread="{thread}" data-block="{block}" data-step="{step}" />'.format(
                kind=segment["kind"],
                x1=segment["x1"],
                y1=segment["y1"],
                x2=segment["x2"],
                y2=segment["y2"],
                color=html.escape(segment["color"]),
                thread=segment["colorIndex"] + 1,
                block=segment["blockIndex"] + 1,
                step=segment["step"],
            )
        )
    return "\n      ".join(parts)


def render_needle_points(segments: list[dict]) -> str:
    dots: list[str] = []
    for segment in segments:
        if segment["kind"] != "stitch":
            continue
        dots.append(
            '<circle class="needle-point" cx="{x:.3f}" cy="{y:.3f}" r="0.38" fill="{color}" data-step="{step}" />'.format(
                x=segment["x2"],
                y=segment["y2"],
                color=html.escape(segment["color"]),
                step=segment["step"],
            )
        )
    return "\n      ".join(dots)


def render_markers(commands: list[dict]) -> str:
    markers: list[str] = []
    for command in commands:
        if command["command"] not in {"trim", "color_change"}:
            continue
        markers.append(
            '<circle class="marker {kind}" cx="{x:.3f}" cy="{y:.3f}" r="0.8" data-step="{step}">'
            "<title>{title}</title></circle>".format(
                kind=command["command"],
                x=command["x"],
                y=command["y"],
                step=command["step"],
                title=html.escape(command["command"].replace("_", " ").title()),
            )
        )
    return "\n      ".join(markers)


def estimate_thread_usage(segments: list[dict], overhead_ratio: float = 0.12) -> dict[int, float]:
    usage: dict[int, float] = {}
    for segment in segments:
        if segment["kind"] != "stitch":
            continue
        block_index = segment["blockIndex"]
        length_mm = math.hypot(
            segment["x2"] - segment["x1"],
            segment["y2"] - segment["y1"],
        )
        usage[block_index] = usage.get(block_index, 0.0) + length_mm
    return {
        block_index: length_mm * (1.0 + overhead_ratio) / 1000.0
        for block_index, length_mm in usage.items()
    }


def format_usage(meters: float) -> str:
    if meters < 1:
        return f"{meters * 100:.0f} cm"
    return f"{meters:.2f} m"


def estimate_stitch_time(counts: dict, color_blocks: list[dict]) -> str:
    stitch_seconds = counts["needle_points"] / 600.0 * 60.0
    jump_seconds = counts["jumps"] * 0.25
    trim_seconds = counts["trims"] * 2.0
    color_seconds = max(0, len(color_blocks) - 1) * 25.0
    total_seconds = max(1, int(round(stitch_seconds + jump_seconds + trim_seconds + color_seconds)))
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} hr {minutes} min"
    if minutes:
        return f"{minutes} min {seconds} sec"
    return f"{seconds} sec"


def classify_fill_types(segments: list[dict], color_blocks: list[dict]) -> dict:
    by_block: dict[int, list[tuple[float, float]]] = {}
    for segment in segments:
        if segment["kind"] != "stitch":
            continue
        length = math.hypot(segment["x2"] - segment["x1"], segment["y2"] - segment["y1"])
        if length <= 0.05:
            continue
        angle = (math.degrees(math.atan2(segment["y2"] - segment["y1"], segment["x2"] - segment["x1"])) + 180) % 180
        by_block.setdefault(segment["blockIndex"], []).append((length, angle))

    block_types: list[dict] = []
    type_counts: dict[str, int] = {}
    for block in color_blocks:
        values = by_block.get(block["index"], [])
        if not values:
            fill_type = "No stitches"
            confidence = "low"
        else:
            lengths = [value[0] for value in values]
            median_length = sorted(lengths)[len(lengths) // 2]
            long_ratio = sum(1 for length in lengths if length >= 3.0) / len(lengths)
            short_ratio = sum(1 for length in lengths if length < 0.8) / len(lengths)
            bins: dict[int, int] = {}
            for _, angle in values:
                key = int(round(angle / 10.0) * 10) % 180
                bins[key] = bins.get(key, 0) + 1
            dominant_ratio = max(bins.values()) / len(values)
            active_bins = sum(1 for count in bins.values() if count / len(values) >= 0.05)

            if median_length >= 2.2 and dominant_ratio >= 0.55:
                fill_type = "Satin"
                confidence = "high"
            elif median_length >= 2.4 and long_ratio >= 0.25 and active_bins >= 4:
                fill_type = "Mixed satin/fill"
                confidence = "medium"
            elif median_length <= 1.2 and short_ratio >= 0.35 and active_bins <= 3:
                fill_type = "Running stitch"
                confidence = "medium"
            elif active_bins >= 4 or dominant_ratio < 0.45:
                fill_type = "Tatami fill"
                confidence = "high"
            else:
                fill_type = "Fill"
                confidence = "medium"

        type_counts[fill_type] = type_counts.get(fill_type, 0) + 1
        block_types.append(
            {
                "block": block["index"],
                "label": block.get("label", block.get("color", "")),
                "color": block.get("color", ""),
                "fill_type": fill_type,
                "confidence": confidence,
            }
        )

    if not type_counts:
        summary = "Unknown"
    elif len(type_counts) == 1:
        summary = next(iter(type_counts))
    else:
        ordered = sorted(type_counts.items(), key=lambda item: (-item[1], item[0]))
        summary = "Mixed: " + ", ".join(f"{name} ({count})" for name, count in ordered)
    return {"summary": summary, "blocks": block_types}


def render_density_warning(thread_weight: str, fill_spacing_mm: float) -> str:
    minimum = minimum_fill_spacing(thread_weight)
    if fill_spacing_mm >= minimum:
        return ""
    return (
        '<section class="density-warning">'
        "<h2>Density Warning</h2>"
        "<p>"
        f"{html.escape(thread_weight_label(thread_weight))} usually needs at least "
        f"{minimum:.2f} mm fill spacing. This design uses {fill_spacing_mm:.2f} mm."
        "</p>"
        "</section>"
    )


def point_distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def points_match(a: tuple[float, float], b: tuple[float, float], tolerance: float = 0.001) -> bool:
    return abs(a[0] - b[0]) <= tolerance and abs(a[1] - b[1]) <= tolerance


def to_embroidery_units(point: tuple[float, float]) -> tuple[int, int]:
    return int(round(point[0] * EMB_UNITS_PER_MM)), int(round(point[1] * EMB_UNITS_PER_MM))


def split_long_point_span(
    start: tuple[float, float],
    end: tuple[float, float],
    max_stitch_mm: float,
) -> list[tuple[float, float]]:
    span = point_distance(start, end)
    safe_max_stitch_mm = max(max_stitch_mm - 0.2, 0.1)
    steps = max(1, int(math.ceil(span / safe_max_stitch_mm)))
    return [
        (
            start[0] + (end[0] - start[0]) * (step / steps),
            start[1] + (end[1] - start[1]) * (step / steps),
        )
        for step in range(1, steps + 1)
    ]


def simplify_perimeter_loop(loop: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if len(loop) < 4:
        return loop
    simplified: list[tuple[float, float]] = []
    for point in loop:
        simplified.append(point)
        while len(simplified) >= 3:
            a, b, c = simplified[-3], simplified[-2], simplified[-1]
            ab = (round(b[0] - a[0], 6), round(b[1] - a[1], 6))
            bc = (round(c[0] - b[0], 6), round(c[1] - b[1], 6))
            if ab == bc:
                simplified.pop(-2)
            else:
                break
    return simplified


def perpendicular_distance(
    point: tuple[float, float],
    start: tuple[float, float],
    end: tuple[float, float],
) -> float:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    if dx == 0 and dy == 0:
        return point_distance(point, start)
    return abs(dy * point[0] - dx * point[1] + end[0] * start[1] - end[1] * start[0]) / math.hypot(dx, dy)


def rdp_simplify(points: list[tuple[float, float]], tolerance: float) -> list[tuple[float, float]]:
    if len(points) <= 2:
        return points
    max_distance = 0.0
    split_index = 0
    for index in range(1, len(points) - 1):
        distance = perpendicular_distance(points[index], points[0], points[-1])
        if distance > max_distance:
            max_distance = distance
            split_index = index
    if max_distance <= tolerance:
        return [points[0], points[-1]]
    left = rdp_simplify(points[: split_index + 1], tolerance)
    right = rdp_simplify(points[split_index:], tolerance)
    return [*left[:-1], *right]


def simplify_closed_loop(loop: list[tuple[float, float]], tolerance: float) -> list[tuple[float, float]]:
    if len(loop) < 4:
        return loop
    closed = [*loop, loop[0]]
    simplified = rdp_simplify(closed, tolerance)
    if simplified and points_match(simplified[0], simplified[-1], tolerance=0.001):
        simplified = simplified[:-1]
    return simplify_perimeter_loop(simplified)


def stitched_boundary_loops(
    block_segments: list[dict],
    *,
    cell_mm: float = 0.18,
    offset_mm: float = 0.24,
    min_loop_length_mm: float = 0.0,
) -> list[list[tuple[float, float]]]:
    if not block_segments:
        return []
    min_x = min(min(segment["x1"], segment["x2"]) for segment in block_segments) - cell_mm * 2
    min_y = min(min(segment["y1"], segment["y2"]) for segment in block_segments) - cell_mm * 2
    occupied: set[tuple[int, int]] = set()

    def mark_cell(x: float, y: float) -> None:
        cx = int(math.floor((x - min_x) / cell_mm))
        cy = int(math.floor((y - min_y) / cell_mm))
        occupied.add((cx, cy))

    for segment in block_segments:
        start = (segment["x1"], segment["y1"])
        end = (segment["x2"], segment["y2"])
        distance = max(point_distance(start, end), cell_mm)
        steps = max(1, int(math.ceil(distance / (cell_mm * 0.5))))
        for step in range(steps + 1):
            t = step / steps
            mark_cell(start[0] + (end[0] - start[0]) * t, start[1] + (end[1] - start[1]) * t)

    def disk_offsets(radius: int) -> list[tuple[int, int]]:
        return [
            (ox, oy)
            for oy in range(-radius, radius + 1)
            for ox in range(-radius, radius + 1)
            if ox * ox + oy * oy <= radius * radius
        ]

    def dilate(cells: set[tuple[int, int]], radius: int) -> set[tuple[int, int]]:
        offsets = disk_offsets(radius)
        return {
            (cx + ox, cy + oy)
            for cx, cy in cells
            for ox, oy in offsets
        }

    def erode(cells: set[tuple[int, int]], radius: int) -> set[tuple[int, int]]:
        if not cells:
            return set()
        offsets = disk_offsets(radius)
        min_cx = min(cx for cx, _ in cells) - radius
        max_cx = max(cx for cx, _ in cells) + radius
        min_cy = min(cy for _, cy in cells) - radius
        max_cy = max(cy for _, cy in cells) + radius
        return {
            (cx, cy)
            for cy in range(min_cy, max_cy + 1)
            for cx in range(min_cx, max_cx + 1)
            if all((cx + ox, cy + oy) in cells for ox, oy in offsets)
        }

    close_radius = max(1, int(math.ceil(0.24 / cell_mm)))
    occupied = erode(dilate(occupied, close_radius), close_radius)
    offset_radius = max(0, int(round(offset_mm / cell_mm)))
    if offset_radius:
        occupied = dilate(occupied, offset_radius)
    if not occupied:
        return []
    edges: set[tuple[tuple[int, int], tuple[int, int]]] = set()

    def add_edge(a: tuple[int, int], b: tuple[int, int]) -> None:
        edges.add((a, b) if a <= b else (b, a))

    for cx, cy in occupied:
        if (cx, cy - 1) not in occupied:
            add_edge((cx, cy), (cx + 1, cy))
        if (cx, cy + 1) not in occupied:
            add_edge((cx, cy + 1), (cx + 1, cy + 1))
        if (cx - 1, cy) not in occupied:
            add_edge((cx, cy), (cx, cy + 1))
        if (cx + 1, cy) not in occupied:
            add_edge((cx + 1, cy), (cx + 1, cy + 1))

    adjacency: dict[tuple[int, int], set[tuple[int, int]]] = {}
    for a, b in edges:
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)

    paths: list[list[tuple[float, float]]] = []

    def edge_key(a: tuple[int, int], b: tuple[int, int]) -> tuple[tuple[int, int], tuple[int, int]]:
        return (a, b) if a <= b else (b, a)

    def pop_edge_from(vertex: tuple[int, int]) -> tuple[int, int] | None:
        candidates = [
            neighbor
            for neighbor in adjacency.get(vertex, set())
            if edge_key(vertex, neighbor) in edges
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[1], item[0]))
        neighbor = candidates[0]
        edges.remove(edge_key(vertex, neighbor))
        return neighbor

    while edges:
        endpoints = [
            vertex
            for vertex, neighbors in adjacency.items()
            if sum(1 for neighbor in neighbors if edge_key(vertex, neighbor) in edges) == 1
        ]
        start = min(endpoints) if endpoints else min(min(edge) for edge in edges)
        path_vertices = [start]
        current = start
        while True:
            nxt = pop_edge_from(current)
            if nxt is None:
                break
            path_vertices.append(nxt)
            current = nxt
            if current == start:
                break
        if len(path_vertices) < 2:
            continue
        path = [(min_x + vx * cell_mm, min_y + vy * cell_mm) for vx, vy in path_vertices]
        closed = points_match(path[0], path[-1], tolerance=0.001)
        if closed:
            path = simplify_closed_loop(path[:-1], tolerance=max(cell_mm * 1.4, 0.22))
            path = [*path, path[0]]
        else:
            path = rdp_simplify(path, tolerance=max(cell_mm * 1.4, 0.22))
        length = sum(point_distance(start_point, end_point) for start_point, end_point in zip(path, path[1:]))
        if length >= max(min_loop_length_mm, cell_mm):
            paths.append(path)
    paths.sort(
        key=lambda path: (
            min(point[1] for point in path),
            min(point[0] for point in path),
            -point_distance(path[0], path[-1]),
        )
    )
    return paths


def add_perimeter_segments(
    segments: list[dict],
    color_blocks: list[dict],
    *,
    max_stitch_mm: float,
    offset_mm: float = 0.24,
    passes: int = 1,
) -> list[dict]:
    next_step = max((segment.get("step", 0) for segment in segments), default=0)
    extra_segments: list[dict] = []
    passes = max(1, min(int(passes), 3))
    for block in color_blocks:
        block_index = block["index"]
        block_segments = [
            segment
            for segment in segments
            if segment.get("kind") == "stitch" and segment.get("blockIndex") == block_index
        ]
        loop_id = 0
        for pass_index in range(passes):
            pass_offset = offset_mm + pass_index * 0.35
            for loop in stitched_boundary_loops(block_segments, offset_mm=pass_offset):
                for start, end in zip(loop, loop[1:]):
                    last = start
                    for point in split_long_point_span(start, end, max_stitch_mm):
                        next_step += 1
                        extra_segments.append(
                            {
                                "x1": last[0],
                                "y1": last[1],
                                "x2": point[0],
                                "y2": point[1],
                                "kind": "stitch",
                                "color": block["color"],
                                "colorIndex": block.get("thread", block_index),
                                "blockIndex": block_index,
                                "step": next_step,
                                "perimeter": True,
                                "perimeterLoop": loop_id,
                            }
                        )
                        last = point
                loop_id += 1
    if not extra_segments:
        return segments
    block_counts = {block["index"]: 0 for block in color_blocks}
    for segment in [*segments, *extra_segments]:
        if segment.get("kind") == "stitch" and segment.get("blockIndex") in block_counts:
            block_counts[segment["blockIndex"]] += 1
    for block in color_blocks:
        block["stitches"] = block_counts.get(block["index"], block.get("stitches", 0))
    return [*segments, *extra_segments]


def clean_run_points(
    points: list[tuple[float, float]],
    *,
    min_stitch_mm: float = 0.18,
    max_stitch_mm: float = 7.0,
    lock_stitch_mm: float = 1.0,
    min_run_length_mm: float = 1.0,
    include_start_lock: bool = True,
) -> list[tuple[float, float]]:
    if len(points) < 2:
        return points

    deduped = [points[0]]
    for point in points[1:]:
        if point_distance(deduped[-1], point) >= min_stitch_mm:
            deduped.append(point)
    if len(deduped) < 2:
        return []
    run_length = sum(point_distance(start, end) for start, end in zip(deduped, deduped[1:]))
    if run_length < min_run_length_mm:
        return []

    split_points = [deduped[0]]
    for start, end in zip(deduped, deduped[1:]):
        split_points.extend(split_long_point_span(start, end, max_stitch_mm))
    if len(split_points) < 2:
        return []

    def lock_point(
        anchor: tuple[float, float],
        neighbor: tuple[float, float],
    ) -> tuple[float, float] | None:
        span = point_distance(anchor, neighbor)
        if span < min_stitch_mm:
            return None
        lock_length = min(lock_stitch_mm, span)
        unit_x = (neighbor[0] - anchor[0]) / span
        unit_y = (neighbor[1] - anchor[1]) / span
        return (
            anchor[0] + unit_x * lock_length,
            anchor[1] + unit_y * lock_length,
        )

    first = split_points[0]
    second = split_points[1]
    if include_start_lock:
        start_lock = lock_point(first, second)
        if start_lock is not None:
            split_points = [first, start_lock, first, start_lock, *split_points[1:]]

    if len(split_points) >= 2:
        last = split_points[-1]
        before_last = split_points[-2]
        end_lock = lock_point(last, before_last)
        if end_lock is not None:
            split_points = [*split_points, end_lock, last, end_lock, last]

    return split_points


def inventory_label(item: dict) -> str:
    label = " ".join(part for part in [item.get("brand", ""), item.get("name", "")] if part).strip()
    return label or item["color"]


def catalog_label(item: dict) -> str:
    return " ".join(
        part
        for part in [item.get("brand", ""), item.get("number", ""), item.get("name", "")]
        if part
    ).strip() or item["color"]


def closest_thread_match(color: str, threads: list[dict]) -> dict | None:
    if not threads:
        return None
    return min(threads, key=lambda item: rgb_distance(color, item["color"]))


def option_text_color(hex_color: str) -> str:
    color = hex_color.lstrip("#")
    try:
        red = int(color[0:2], 16)
        green = int(color[2:4], 16)
        blue = int(color[4:6], 16)
    except (TypeError, ValueError):
        return "#172026"
    luminance = (0.299 * red) + (0.587 * green) + (0.114 * blue)
    return "#ffffff" if luminance < 128 else "#172026"


def thread_option(color: str, label: str) -> str:
    return (
        '<option value="{color}" data-swatch="{color}" '
        'style="background-color:{color};color:{text_color};">{label}</option>'
    ).format(
        color=html.escape(color, quote=True),
        text_color=html.escape(option_text_color(color), quote=True),
        label=html.escape(label),
    )


def render_inventory_options() -> str:
    options = ['<option value="">Known thread colors</option>']
    inventory = load_inventory()
    catalog = load_thread_catalog()
    if inventory:
        options.append('<optgroup label="Your inventory">')
    for item in inventory:
        label = f'{inventory_label(item)} - {item["color"]}'
        options.append(thread_option(item["color"], label))
    if inventory:
        options.append("</optgroup>")
    for brand in available_thread_brands():
        brand_items = [item for item in catalog if item.get("brand") == brand]
        if not brand_items:
            continue
        options.append(f'<optgroup label="{html.escape(brand)}">')
        for item in brand_items:
            label = f'{item["brand"]} {item["number"]} {item["name"]} - {item["color"]}'
            options.append(thread_option(item["color"], label))
        options.append("</optgroup>")
    return "".join(options)


def group_color_blocks_by_inventory(
    segments: list[dict],
    commands: list[dict],
    color_blocks: list[dict],
    counts: dict,
    match_distance: float = 64.0,
) -> tuple[list[dict], list[dict], list[dict], dict]:
    inventory = load_inventory()
    catalog = load_thread_catalog()
    if match_distance <= 0:
        return segments, commands, color_blocks, counts

    block_map: dict[int, int] = {}
    grouped_blocks: list[dict] = []
    group_by_key: dict[tuple[str, str], int] = {}
    for block in color_blocks:
        color = block["color"]
        match = closest_inventory_match(color, inventory)
        if match is not None and rgb_distance(color, match["color"]) <= match_distance:
            key = ("inventory", match["id"])
            display_color = match["color"]
            label = f"Inventory {inventory_label(match)}"
        elif catalog:
            catalog_match = closest_thread_match(color, catalog)
            assert catalog_match is not None
            key = (catalog_match["brand"], catalog_match["number"])
            display_color = catalog_match["color"]
            label = catalog_label(catalog_match)
        else:
            key = ("design", str(block["index"]))
            display_color = color
            label = block.get("label", f"Design {color}")

        if key not in group_by_key:
            group_by_key[key] = len(grouped_blocks)
            grouped_blocks.append(
                {
                    "index": len(grouped_blocks),
                    "thread": len(grouped_blocks),
                    "color": display_color,
                    "label": label,
                    "stitches": 0,
                }
            )
        block_map[block["index"]] = group_by_key[key]

    for segment in segments:
        new_index = block_map.get(segment["blockIndex"], segment["blockIndex"])
        segment["blockIndex"] = new_index
        segment["colorIndex"] = new_index
        segment["color"] = grouped_blocks[new_index]["color"]
        if segment["kind"] == "stitch":
            grouped_blocks[new_index]["stitches"] += 1

    for command in commands:
        if "color" in command:
            command["color"] = block_map.get(command["color"], command["color"])

    counts = dict(counts)
    counts["color_changes"] = max(0, len(grouped_blocks) - 1)
    return segments, commands, grouped_blocks, counts


def thread_metadata_path(pes_file: Path) -> Path:
    return pes_file.with_name(f"{pes_file.stem}.threadmeta.json")


def apply_thread_metadata(
    input_file: Path,
    segments: list[dict],
    color_blocks: list[dict],
) -> tuple[list[dict], list[dict]]:
    if input_file.suffix.lower() != ".pes":
        return segments, color_blocks
    metadata_file = thread_metadata_path(input_file)
    if not metadata_file.exists():
        return segments, color_blocks
    try:
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return segments, color_blocks
    metadata_blocks = metadata.get("blocks", [])
    if not isinstance(metadata_blocks, list):
        return segments, color_blocks
    for block, metadata_block in zip(color_blocks, metadata_blocks):
        if not isinstance(metadata_block, dict):
            continue
        color = metadata_block.get("color")
        label = metadata_block.get("label")
        if isinstance(color, str):
            try:
                block["color"] = normalize_hex(color)
            except ValueError:
                pass
        if isinstance(label, str) and label.strip():
            block["label"] = label.strip()
    block_colors = {block["index"]: block["color"] for block in color_blocks}
    for segment in segments:
        if segment["blockIndex"] in block_colors:
            segment["color"] = block_colors[segment["blockIndex"]]
    return segments, color_blocks


def render_thread_plan(color_blocks: list[dict], usage_by_block: dict[int, float]) -> str:
    inventory = load_inventory()
    catalog = load_thread_catalog()
    rows: list[str] = []
    needed: list[str] = []
    shopping: dict[str, dict] = {}
    total_meters = 0.0
    for block in color_blocks:
        color = block["color"]
        meters = usage_by_block.get(block["index"], 0.0)
        total_meters += meters
        match = closest_inventory_match(color, inventory)
        catalog_match = closest_thread_match(color, catalog)
        catalog_detail = "No known catalog colors loaded."
        if catalog_match is not None:
            catalog_distance = rgb_distance(color, catalog_match["color"])
            catalog_detail = (
                f'<span class="swatch" style="background:{html.escape(catalog_match["color"])}"></span>'
                f'{html.escape(catalog_label(catalog_match))} '
                f'({html.escape(catalog_match["color"])}, match {catalog_distance:.0f})'
            )
        if match is None:
            status = "Need to buy"
            detail = "No inventory colors saved yet."
            status_class = "need"
            needed.append(color)
            if catalog_match is not None:
                key = catalog_match["number"]
                shopping.setdefault(
                    key,
                    {
                        "label": catalog_label(catalog_match),
                        "color": catalog_match["color"],
                        "meters": 0.0,
                        "design_colors": set(),
                    },
                )
                shopping[key]["meters"] += meters
                shopping[key]["design_colors"].add(color)
        else:
            distance = rgb_distance(color, match["color"])
            close_enough = distance <= 64
            status = "In inventory" if close_enough else "Need to buy"
            status_class = "have" if close_enough else "need"
            detail = (
                f'{html.escape(inventory_label(match))} '
                f'({html.escape(match["color"])}, qty {match["quantity"]}, match {distance:.0f})'
            )
            if not close_enough:
                needed.append(color)
                if catalog_match is not None:
                    detail += f"<br>Buy closest known thread: {catalog_detail}"
                    key = catalog_match["number"]
                    shopping.setdefault(
                        key,
                        {
                            "label": catalog_label(catalog_match),
                            "color": catalog_match["color"],
                            "meters": 0.0,
                            "design_colors": set(),
                        },
                    )
                    shopping[key]["meters"] += meters
                    shopping[key]["design_colors"].add(color)
        rows.append(
            '<tr>'
            f'<td><span class="swatch" style="background:{html.escape(color)}"></span>{html.escape(color)}</td>'
            f'<td>{format_usage(meters)}</td>'
            f'<td><span class="thread-status {status_class}">{status}</span></td>'
            f'<td>{detail}</td>'
            f'<td>{catalog_detail}</td>'
            '</tr>'
        )

    summary = (
        f"Estimated total thread: {format_usage(total_meters)} including 12% allowance. "
        f"Colors to buy: {len(set(needed))}."
    )
    shopping_lines = ["Thread shopping list", ""]
    for item in sorted(shopping.values(), key=lambda entry: entry["label"]):
        design_colors = ", ".join(sorted(item["design_colors"]))
        shopping_lines.append(
            f'- {item["label"]} ({item["color"]}) - {format_usage(item["meters"])} estimated - for {design_colors}'
        )
    shopping_text = "\n".join(shopping_lines) if shopping else ""
    shopping_empty_class = "" if not shopping else " hidden"
    shopping_text_class = "" if shopping else " hidden"
    shopping_html = (
        '<div class="shopping-list">'
        '<h3>Shopping List</h3>'
        f'<p id="shopping-list-empty" class="shopping-empty{shopping_empty_class}">All design colors have close inventory matches.</p>'
        f'<textarea id="shopping-list-text" class="{shopping_text_class}" readonly>{html.escape(shopping_text)}</textarea>'
        '<div class="shopping-actions">'
        f'<button id="copy-shopping-list" type="button" {"disabled" if not shopping else ""}>Copy List</button>'
        f'<button id="download-shopping-list" type="button" {"disabled" if not shopping else ""}>Download TXT</button>'
        '</div>'
        '</div>'
    )
    return (
        '<section class="thread-plan">'
        '<h2>Thread Planning</h2>'
        f'<p>{html.escape(summary)}</p>'
        '<table><thead><tr><th>Design color</th><th>Use</th><th>Status</th><th>Closest inventory match</th><th>Closest known thread</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
        f'{shopping_html}'
        '<a class="inventory-link" href="/inventory">Edit Thread Inventory</a>'
        '</section>'
    )


def render_legend(
    pattern,
    color_blocks: list[dict],
    color_controls: bool = False,
    fill_type_info: dict | None = None,
) -> str:
    items: list[str] = []
    inventory_options = render_inventory_options() if color_controls else ""
    fill_by_block = {
        item["block"]: item
        for item in (fill_type_info or {}).get("blocks", [])
        if isinstance(item, dict) and "block" in item
    }
    for block in color_blocks:
        index = block["thread"]
        color = block["color"]
        if block.get("label"):
            label = html.escape(block["label"])
        elif pattern is None:
            label = html.escape(block.get("label", f"SVG {color}"))
        else:
            label = html.escape(thread_label(pattern, index))
        checkbox = ""
        if color_controls:
            checkbox = (
                f'<input class="block-toggle" type="checkbox" value="{block["index"]}" '
                f'checked aria-label="Include block {block["index"] + 1}">'
            )
        reorder = ""
        if color_controls:
            reorder = (
                '<span class="block-order-controls">'
                '<button class="order-button" type="button" data-order-move="up" aria-label="Move color earlier">Up</button>'
                '<button class="order-button" type="button" data-order-move="down" aria-label="Move color later">Down</button>'
                '</span>'
            )
        color_editor = ""
        if color_controls:
            color_editor = (
                '<span class="block-color-controls">'
                '<input class="block-color-input" type="text" value="{color}" '
                'data-block-color="{index}" aria-label="Thread hex color for block {label_index}">'
                '<input class="block-thread-search" type="search" placeholder="Search thread" '
                'data-block-search="{index}" aria-label="Search known thread colors for block {label_index}">'
                '<select class="block-thread-select" data-block-select="{index}" '
                'aria-label="Known thread color for block {label_index}">{options}</select>'
                '</span>'
            ).format(
                color=html.escape(color, quote=True),
                index=block["index"],
                label_index=block["index"] + 1,
                options=inventory_options,
            )
        fill_type = fill_by_block.get(block["index"], {}).get("fill_type", "")
        fill_text = f' <span class="fill-type">Fill: {html.escape(fill_type)}</span>' if fill_type else ""
        items.append(
            f'<li data-block-row="{block["index"]}">'
            '<details class="thread-details">'
            '<summary class="thread-summary">'
            f'{checkbox}<span class="swatch" style="background:{html.escape(color)}"></span>'
            '<span class="thread-summary-text">'
            f'<strong>Block {block["index"] + 1}: {label}</strong>'
            f'<small>{block["stitches"]} stitches{fill_text}</small>'
            '</span>'
            f'<code>{html.escape(color)}</code>'
            '<span class="thread-edit-label">Edit</span>'
            '</summary>'
            f'{color_editor}{reorder}'
            '</details>'
            '</li>'
        )
    return "\n          ".join(items)


def render_html(
    input_file: Path,
    pattern,
    segments: list[dict],
    commands: list[dict],
    color_blocks: list[dict],
    counts: dict,
    source_label: str,
    pes_href: str | None = None,
    color_export_action: str | None = None,
    source_name: str | None = None,
    project_href: str | None = None,
    fit_width_mm: float | None = None,
    fill_mode: str = "tatami",
    fill_angle_deg: float = 45.0,
    fill_spacing_mm: float = 0.5,
    thread_weight: str = DEFAULT_THREAD_WEIGHT,
    max_stitch_mm: float = 3.0,
    max_colors: int = 6,
    color_merge_distance: float = 56.0,
    pdf_page: int = 1,
    display_units: str = "metric",
    fabric_color: str = "#fbfcfa",
    stitch_perimeter: bool = False,
) -> str:
    min_x, min_y, max_x, max_y = design_bounds(segments, commands)
    width = max(max_x - min_x, 1.0)
    height = max(max_y - min_y, 1.0)
    padding = max(width, height) * 0.08
    fill_type_info = classify_fill_types(segments, color_blocks)
    legend = render_legend(pattern, color_blocks, color_controls=True, fill_type_info=fill_type_info)
    usage_by_block = estimate_thread_usage(segments)
    thread_plan = render_thread_plan(color_blocks, usage_by_block)
    thread_weight = normalize_thread_weight(thread_weight)
    density_warning = render_density_warning(thread_weight, fill_spacing_mm)
    stats = {
        "Design": input_file.name,
        "Source": source_label,
        "Size": f"{width:.1f} x {height:.1f} mm",
        "Fill types": fill_type_info["summary"],
        "Needle points": counts["needle_points"],
        "Stitch segments": counts["stitch_segments"],
        "Jumps": counts["jumps"],
        "Trims": counts["trims"],
        "Color changes": counts["color_changes"],
        "Color blocks": len(color_blocks),
        "Threads": len(pattern.threadlist) if pattern is not None else len(color_blocks),
        "Thread weight": thread_weight_label(thread_weight),
        "Max stitch length": f"{max_stitch_mm:.1f} mm",
        "Perimeter stitch": "On" if stitch_perimeter else "Off",
        "Estimated stitch time": estimate_stitch_time(counts, color_blocks),
    }
    max_step = counts["needle_points"]
    stats_html = "\n          ".join(
        f"<div><span>{html.escape(label)}</span><strong>{html.escape(str(value))}</strong></div>"
        for label, value in stats.items()
    )
    embedded_stats = html.escape(json.dumps(stats), quote=True)
    view_box_data = {
        "x": min_x - padding,
        "y": min_y - padding,
        "width": width + padding * 2,
        "height": height + padding * 2,
    }
    embedded_view_box = html.escape(json.dumps(view_box_data), quote=True)
    segment_data = [
        [
            round(segment["x1"], 3),
            round(segment["y1"], 3),
            round(segment["x2"], 3),
            round(segment["y2"], 3),
            SEGMENT_KIND_CODES.get(segment["kind"], 0),
            segment["blockIndex"],
            segment["step"],
        ]
        for segment in segments
    ]
    marker_data = [
        [
            round(command["x"], 3),
            round(command["y"], 3),
            command["command"],
            command["step"],
        ]
        for command in commands
        if command["command"] in {"trim", "color_change"}
    ]
    palette_data = [block["color"] for block in color_blocks]
    usage_data = {
        str(block["index"]): round(usage_by_block.get(block["index"], 0.0), 4)
        for block in color_blocks
    }
    inventory_data = [
        {
            "label": inventory_label(item),
            "color": item["color"],
            "quantity": item["quantity"],
        }
        for item in load_inventory()
    ]
    embedded_segments = html.escape(json.dumps(segment_data, separators=(",", ":")), quote=False)
    embedded_markers = html.escape(json.dumps(marker_data, separators=(",", ":")), quote=False)
    embedded_palette = html.escape(json.dumps(palette_data, separators=(",", ":")), quote=False)
    embedded_usage = html.escape(json.dumps(usage_data, separators=(",", ":")), quote=False)
    embedded_inventory = html.escape(json.dumps(inventory_data, separators=(",", ":")), quote=False)
    display_units = "sae" if display_units == "sae" else "metric"
    if not re.match(r"^#[0-9A-Fa-f]{6}$", fabric_color or ""):
        fabric_color = "#fbfcfa"
    embedded_display_units = json.dumps(display_units)
    embedded_fabric_color = json.dumps(fabric_color)
    pes_download = ""
    email_project = ""
    project_download = ""
    if pes_href:
        pes_download = (
            '<a class="download-action" href="{href}" download>Download PES</a>'.format(
                href=html.escape(pes_href, quote=True)
            )
        )
        email_project = (
            '<form id="email-project-form" class="menu-email-form" method="post" action="/email-project">'
            '<input class="menu-email-input" type="email" name="recipient_email" '
            'placeholder="Email address" aria-label="Email project recipient">'
            '<input type="hidden" name="pes_file" value="{href}">'
            '<input id="email-shopping-list" type="hidden" name="shopping_list" value="">'
            '<button class="download-action" type="submit">Email Project</button>'
            '</form>'
        ).format(href=html.escape(pes_href, quote=True))
    if project_href:
        project_download = (
            '<a class="download-action" href="{href}" download>Save Project</a>'.format(
                href=html.escape(project_href, quote=True)
            )
        )
    export_open = ""
    export_close = ""
    export_button = ""
    if color_export_action and source_name:
        fit_value = "" if fit_width_mm is None else str(fit_width_mm)
        export_open = (
            '<form class="color-export" method="post" action="{action}">'
            '<input type="hidden" name="source" value="{source}">'
            '<input type="hidden" name="fit_width_mm" value="{fit_width}">'
            '<input type="hidden" name="fill_mode" value="{fill_mode}">'
            '<input type="hidden" name="fill_angle_deg" value="{fill_angle_deg}">'
            '<input type="hidden" name="fill_spacing_mm" value="{fill_spacing}">'
            '<input type="hidden" name="thread_weight" value="{thread_weight}">'
            '<input type="hidden" name="max_stitch_mm" value="{max_stitch}">'
            '<input type="hidden" name="max_colors" value="{max_colors}">'
            '<input type="hidden" name="color_merge_distance" value="{color_merge_distance}">'
            '<input type="hidden" name="pdf_page" value="{pdf_page}">'
            '<input type="hidden" name="stitch_perimeter" value="{stitch_perimeter}">'
            '<input id="selected-blocks" type="hidden" name="selected_blocks" value="">'
            '<input id="color-order" type="hidden" name="color_order" value="">'
            '<input id="color-overrides" type="hidden" name="color_overrides" value="">'
            '<input id="thread-label-overrides" type="hidden" name="thread_label_overrides" value="">'
        ).format(
            action=html.escape(color_export_action, quote=True),
            source=html.escape(source_name, quote=True),
            fit_width=html.escape(fit_value, quote=True),
            fill_mode=html.escape(fill_mode, quote=True),
            fill_angle_deg=fill_angle_deg,
            fill_spacing=fill_spacing_mm,
            thread_weight=html.escape(thread_weight, quote=True),
            max_stitch=max_stitch_mm,
            max_colors=max_colors,
            color_merge_distance=color_merge_distance,
            pdf_page=pdf_page,
            stitch_perimeter="1" if stitch_perimeter else "",
        )
        export_button = '<button class="export-button" type="submit">Recreate PES</button>'
        export_close = "</form>"

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(input_file.name)} - Embroidery Viewer</title>
  <style>
    :root {{
      color-scheme: light;
      font-family: Inter, Segoe UI, Arial, sans-serif;
      color: #172026;
      background: #f7f8f5;
      --sidebar-width: 340px;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      grid-template-columns: var(--sidebar-width) 1fr;
    }}
    body.left-panel-collapsed,
    body.left-panel-floating {{
      grid-template-columns: 1fr;
    }}
    aside {{
      position: sticky;
      top: 0;
      height: 100vh;
      max-height: 100vh;
      background: #ffffff;
      border-right: 1px solid #d9ded6;
      padding: 24px;
      overflow-y: auto;
      overscroll-behavior: contain;
    }}
    body.left-panel-collapsed aside {{
      display: none;
    }}
    body.left-panel-floating aside {{
      position: fixed;
      z-index: 17;
      left: 12px;
      top: 12px;
      width: min(var(--sidebar-width), calc(100vw - 24px));
      height: calc(100vh - 24px);
      border: 1px solid #d9ded6;
      border-radius: 8px;
      box-shadow: 0 12px 32px rgba(23, 32, 38, 0.16);
    }}
    .sidebar-resizer {{
      position: fixed;
      z-index: 16;
      top: 0;
      bottom: 0;
      left: calc(var(--sidebar-width) - 4px);
      width: 8px;
      border: 0;
      border-radius: 0;
      padding: 0;
      background: transparent;
      cursor: col-resize;
    }}
    .sidebar-resizer::after {{
      content: "";
      position: absolute;
      top: 0;
      bottom: 0;
      left: 3px;
      width: 2px;
      background: transparent;
      transition: background 120ms ease;
    }}
    .sidebar-resizer:hover::after,
    body.resizing-sidebar .sidebar-resizer::after {{
      background: #2f6f73;
    }}
    body.resizing-sidebar {{
      cursor: col-resize;
      user-select: none;
    }}
    body.left-panel-collapsed .sidebar-resizer,
    body.left-panel-floating .sidebar-resizer {{
      display: none;
    }}
    main {{
      min-width: 0;
      padding: 18px 382px 18px 18px;
      display: flex;
      align-items: stretch;
    }}
    h1 {{
      margin: 0 0 18px;
      font-size: 19px;
      line-height: 1.2;
      overflow-wrap: anywhere;
    }}
    h2 {{
      margin: 24px 0 10px;
      font-size: 13px;
      text-transform: uppercase;
      letter-spacing: 0;
      color: #52605a;
    }}
    .stats {{
      display: grid;
      gap: 8px;
    }}
    .stats div {{
      display: grid;
      grid-template-columns: minmax(96px, 0.8fr) minmax(0, 1.2fr);
      gap: 10px;
      border-bottom: 1px solid #edf0eb;
      padding-bottom: 8px;
      font-size: 14px;
    }}
    .stats span {{ color: #52605a; }}
    .stats strong {{
      min-width: 0;
      text-align: right;
      overflow-wrap: anywhere;
    }}
    .thread-plan {{
      display: grid;
      gap: 10px;
      margin: 16px 0 18px;
      padding: 12px;
      border: 1px solid #d9ded6;
      border-radius: 8px;
      background: #fbfcfa;
      overflow: auto;
    }}
    .density-warning {{
      margin: 16px 0 18px;
      padding: 12px;
      border: 1px solid #f4b05e;
      border-left: 4px solid #f97316;
      border-radius: 8px;
      background: #fff7ed;
    }}
    .density-warning h2 {{
      margin: 0 0 8px;
      font-size: 12px;
      text-transform: uppercase;
      color: #7c2d12;
    }}
    .density-warning p {{
      margin: 0;
      color: #7c2d12;
      font-size: 13px;
      line-height: 1.4;
    }}
    .thread-plan h2 {{
      margin-top: 0;
    }}
    .thread-plan p {{
      margin: 0;
      color: #52605a;
      font-size: 13px;
      line-height: 1.4;
    }}
    .thread-plan table {{
      width: 100%;
      min-width: 560px;
      border-collapse: collapse;
      font-size: 12px;
    }}
    .thread-plan th,
    .thread-plan td {{
      padding: 7px 5px;
      border-top: 1px solid #e2e7df;
      text-align: left;
      vertical-align: top;
    }}
    .thread-plan th {{
      color: #52605a;
      font-weight: 700;
    }}
    .thread-status {{
      display: inline-flex;
      min-height: 22px;
      align-items: center;
      padding: 0 7px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 700;
      white-space: nowrap;
    }}
    .thread-status.have {{
      background: #e3f1ea;
      color: #245a39;
    }}
    .thread-status.need {{
      background: #fff0df;
      color: #7b3f00;
    }}
    .shopping-list {{
      display: grid;
      gap: 8px;
      margin-top: 4px;
    }}
    .shopping-list h3 {{
      margin: 0;
      font-size: 13px;
      color: #29332f;
    }}
    .shopping-list textarea {{
      width: 100%;
      min-height: 118px;
      resize: vertical;
      border: 1px solid #cbd4cf;
      border-radius: 6px;
      padding: 9px;
      background: #ffffff;
      color: #172026;
      font: 12px Consolas, monospace;
      line-height: 1.45;
    }}
    .shopping-actions {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
    }}
    .shopping-actions button {{
      min-height: 32px;
      padding: 0 8px;
      font-size: 12px;
    }}
    .shopping-actions button:disabled {{
      opacity: 0.55;
      cursor: not-allowed;
    }}
    .hidden {{
      display: none !important;
    }}
    .inventory-link {{
      color: #2f6f73;
      font-size: 13px;
      font-weight: 700;
    }}
    .controls {{
      display: grid;
      gap: 10px;
      margin-top: 20px;
    }}
    .view-legend {{
      display: grid;
      gap: 8px;
      margin-top: 18px;
      padding: 12px;
      border: 1px solid #d9ded6;
      border-radius: 8px;
      background: #fbfcfa;
      font-size: 13px;
    }}
    .view-legend h2 {{
      margin: 0;
      font-size: 13px;
      color: #52605a;
      text-transform: uppercase;
      letter-spacing: 0;
    }}
    .legend-item {{
      display: grid;
      grid-template-columns: 34px 1fr;
      gap: 8px;
      align-items: center;
      color: #29332f;
    }}
    .legend-symbol {{
      position: relative;
      height: 18px;
      border-radius: 4px;
    }}
    .legend-symbol.stitch-line::before,
    .legend-symbol.jump-line::before {{
      content: "";
      position: absolute;
      left: 2px;
      right: 2px;
      top: 8px;
      border-top: 3px solid #111827;
    }}
    .legend-symbol.jump-line::before {{
      border-top: 2px dashed #66736f;
    }}
    .legend-symbol.needle-dot::before,
    .legend-symbol.trim-dot::before,
    .legend-symbol.change-dot::before {{
      content: "";
      position: absolute;
      left: 10px;
      top: 3px;
      width: 12px;
      height: 12px;
      border-radius: 999px;
      background: #111827;
      border: 1px solid #111827;
    }}
    .legend-symbol.trim-dot::before {{
      background: #f97316;
      border-color: #7c2d12;
    }}
    .legend-symbol.change-dot::before {{
      background: #2563eb;
      border-color: #172554;
    }}
    .playback {{
      display: grid;
      gap: 12px;
      margin: 20px 0 4px;
      padding: 14px;
      border: 1px solid #d9ded6;
      border-radius: 8px;
      background: #fbfcfa;
    }}
    .toolpath-actions {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
      margin: 14px 0 0;
    }}
    .toolpath-actions button {{
      min-height: 34px;
      padding: 0 8px;
      border-color: #9aa9a3;
      background: #ffffff;
      color: #29332f;
      font-size: 13px;
      font-weight: 700;
    }}
    .toolpath-actions button:hover {{
      background: #eef4f1;
    }}
    .zoom-readout {{
      color: #52605a;
      font-size: 13px;
      font-variant-numeric: tabular-nums;
    }}
    .transport {{
      display: grid;
      grid-template-columns: 76px 1fr;
      gap: 10px;
      align-items: center;
    }}
    button {{
      min-height: 36px;
      border: 1px solid #2f6f73;
      border-radius: 6px;
      background: #2f6f73;
      color: #ffffff;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
    }}
    button:hover {{
      background: #275f62;
    }}
    .download-action {{
      min-height: 36px;
      display: flex;
      align-items: center;
      justify-content: center;
      border: 1px solid #2f6f73;
      border-radius: 6px;
      background: #2f6f73;
      color: #ffffff;
      font-size: 14px;
      font-weight: 700;
      text-decoration: none;
    }}
    .download-action:hover {{
      background: #275f62;
    }}
    .color-export {{
      display: grid;
      gap: 12px;
    }}
    .thread-floater {{
      position: fixed;
      z-index: 18;
      top: 12px;
      right: 12px;
      bottom: 12px;
      width: min(354px, calc(100vw - 24px));
      display: grid;
      grid-template-rows: auto minmax(0, 1fr) auto;
      gap: 12px;
      padding: 16px;
      border: 1px solid #d9ded6;
      border-radius: 8px;
      background: #ffffff;
      box-shadow: 0 12px 32px rgba(23, 32, 38, 0.16);
      overflow: hidden;
    }}
    body.thread-floater-collapsed main {{
      padding-right: 76px;
    }}
    body.thread-floater-collapsed .thread-floater {{
      bottom: auto;
      width: 58px;
      min-height: 58px;
      padding: 8px;
      grid-template-rows: auto;
    }}
    body.thread-floater-collapsed .thread-floater ul,
    body.thread-floater-collapsed .export-button {{
      display: none;
    }}
    body.thread-floater-collapsed .thread-floater-title span {{
      display: none;
    }}
    .thread-floater h2 {{
      margin: 0;
    }}
    .thread-floater-title {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }}
    .thread-floater-toggle {{
      width: 38px;
      min-height: 34px;
      flex: 0 0 auto;
      padding: 0;
      border-color: #9aa9a3;
      background: #ffffff;
      color: #26332f;
      font-size: 18px;
      line-height: 1;
    }}
    .thread-floater-toggle:hover {{
      background: #eef4f1;
    }}
    .thread-floater ul {{
      overflow: auto;
      padding-right: 2px;
    }}
    .export-button {{
      margin-top: 4px;
    }}
    .counter {{
      text-align: right;
      color: #52605a;
      font-size: 13px;
      font-variant-numeric: tabular-nums;
    }}
    .range-row {{
      display: grid;
      gap: 6px;
    }}
    .range-row header {{
      display: flex;
      justify-content: space-between;
      gap: 12px;
      font-size: 13px;
      color: #52605a;
    }}
    input[type="range"] {{
      width: 100%;
      accent-color: #2f6f73;
    }}
    label {{
      display: flex;
      align-items: center;
      gap: 10px;
      font-size: 14px;
      color: #29332f;
    }}
    input[type="checkbox"] {{
      width: 18px;
      height: 18px;
      accent-color: #2f6f73;
    }}
    ul {{
      list-style: none;
      margin: 0;
      padding: 0;
      display: grid;
      gap: 10px;
    }}
    li {{
      display: block;
      font-size: 13px;
    }}
    .thread-details {{
      border: 1px solid #e2e7df;
      border-radius: 8px;
      background: #fbfcfa;
      overflow: hidden;
    }}
    .thread-details[open] {{
      background: #ffffff;
      border-color: #cbd4cf;
    }}
    .thread-summary {{
      display: grid;
      grid-template-columns: 18px 18px minmax(0, 1fr) auto auto;
      align-items: center;
      gap: 8px;
      padding: 9px;
      cursor: pointer;
      list-style: none;
    }}
    .thread-summary::-webkit-details-marker {{
      display: none;
    }}
    .thread-summary-text {{
      display: grid;
      gap: 2px;
      min-width: 0;
    }}
    .thread-summary-text strong,
    .thread-summary-text small {{
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }}
    .fill-type {{
      color: #52605a;
    }}
    li code {{
      width: fit-content;
      color: #52605a;
      font-size: 12px;
    }}
    .thread-edit-label {{
      color: #2f6f73;
      font-size: 12px;
      font-weight: 700;
    }}
    .thread-details[open] .thread-edit-label {{
      color: #52605a;
    }}
    .block-color-controls {{
      display: grid;
      grid-template-columns: minmax(82px, 0.52fr) minmax(110px, 1fr);
      gap: 6px;
      padding: 0 9px 9px;
      min-width: 0;
    }}
    .block-color-input,
    .block-thread-search,
    .block-thread-select {{
      width: 100%;
      min-height: 30px;
      border: 1px solid #cbd4cf;
      border-radius: 6px;
      background: #fbfcfa;
      color: #172026;
      font: inherit;
      font-size: 12px;
    }}
    .block-color-input {{
      padding: 0 7px;
      font-family: Consolas, monospace;
    }}
    .block-thread-search {{
      padding: 0 8px;
    }}
    .block-thread-select {{
      grid-column: 1 / -1;
      padding: 0 6px;
    }}
    .block-thread-select option {{
      font-weight: 700;
    }}
    .viewer-menu {{
      position: fixed;
      z-index: 30;
      top: 12px;
      right: 12px;
    }}
    .viewer-menu-button {{
      width: 42px;
      min-height: 38px;
      padding: 0;
      border-color: #9aa9a3;
      background: #ffffff;
      color: #26332f;
      font-size: 22px;
      line-height: 1;
      box-shadow: 0 4px 14px rgba(23, 32, 38, 0.12);
    }}
    .viewer-menu-panel {{
      position: absolute;
      top: 46px;
      right: 0;
      width: min(720px, calc(100vw - 24px));
      max-height: calc(100vh - 72px);
      display: none;
      gap: 10px;
      padding: 10px;
      border: 1px solid #d9ded6;
      border-radius: 8px;
      background: #ffffff;
      box-shadow: 0 12px 28px rgba(23, 32, 38, 0.18);
      overflow: auto;
    }}
    body.viewer-menu-open .viewer-menu-panel {{
      display: grid;
    }}
    .viewer-menu-actions {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(132px, 1fr));
      gap: 6px;
    }}
    .viewer-menu-panel a,
    .menu-panel-button {{
      min-height: 36px;
      display: flex;
      justify-content: center;
      align-items: center;
      padding: 0 10px;
      border: 1px solid #c9d2cd;
      border-radius: 6px;
      color: #26332f;
      text-decoration: none;
      font-size: 13px;
      font-weight: 700;
      text-align: center;
      background: #ffffff;
    }}
    .viewer-menu-panel a:hover,
    .menu-panel-button:hover {{
      background: #eaf2ef;
    }}
    .viewer-menu-panel .download-action {{
      width: auto;
      margin: 0;
    }}
    .menu-email-form {{
      display: grid;
      gap: 6px;
    }}
    .menu-email-input {{
      width: 100%;
      min-height: 36px;
      padding: 0 10px;
      border: 1px solid #c9d2cd;
      border-radius: 6px;
      color: #172026;
      font: inherit;
      font-size: 13px;
    }}
    .menu-email-form button {{
      width: 100%;
    }}
    .viewer-menu-panel .thread-plan {{
      margin: 0;
      max-height: none;
    }}
    .viewer-settings {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 8px;
      padding: 10px;
      border: 1px solid #e2e7df;
      border-radius: 8px;
      background: #fbfcfa;
    }}
    .viewer-settings label {{
      display: grid;
      gap: 5px;
      color: #52605a;
      font-size: 12px;
      font-weight: 700;
    }}
    .viewer-settings select,
    .viewer-settings input[type="color"] {{
      width: 100%;
      min-height: 34px;
      border: 1px solid #cbd4cf;
      border-radius: 6px;
      background: #ffffff;
      color: #172026;
      font: inherit;
      font-size: 13px;
    }}
    .viewer-settings input[type="color"] {{
      padding: 3px;
    }}
    .block-toggle {{
      width: 16px;
      height: 16px;
      margin: 0;
      accent-color: #2f6f73;
    }}
    .block-order-controls {{
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 4px;
      padding: 0 9px 9px;
    }}
    .order-button {{
      min-height: 26px;
      padding: 0 6px;
      border-color: #9aa9a3;
      background: #ffffff;
      color: #29332f;
      font-size: 11px;
      font-weight: 700;
    }}
    .order-button:hover {{
      background: #eef4f1;
    }}
    small {{
      color: #6a746f;
      font-size: 12px;
    }}
    code {{
      color: #52605a;
      font-size: 12px;
    }}
    .swatch {{
      width: 18px;
      height: 18px;
      border-radius: 50%;
      border: 1px solid rgba(0, 0, 0, 0.18);
    }}
    .stage {{
      width: 100%;
      min-height: calc(100vh - 36px);
      background: #fbfcfa;
      border: 1px solid #d9ded6;
      border-radius: 8px;
      overflow: hidden;
      cursor: grab;
      touch-action: none;
    }}
    .stage.dragging {{
      cursor: grabbing;
    }}
    canvas {{
      width: 100%;
      height: 100%;
      min-height: calc(100vh - 38px);
      display: block;
    }}
    .canvas-tooltip {{
      position: fixed;
      z-index: 50;
      max-width: 260px;
      padding: 7px 9px;
      border: 1px solid #c9d2cd;
      border-radius: 6px;
      background: #ffffff;
      color: #172026;
      box-shadow: 0 8px 22px rgba(23, 32, 38, 0.16);
      font-size: 12px;
      line-height: 1.35;
      pointer-events: none;
    }}
    body.thumbnail-mode {{
      display: block;
      min-height: 100vh;
      overflow: hidden;
    }}
    body.thumbnail-mode aside,
    body.thumbnail-mode .sidebar-resizer,
    body.thumbnail-mode .thread-floater,
    body.thumbnail-mode .viewer-menu {{
      display: none;
    }}
    body.thumbnail-mode main {{
      min-height: 100vh;
      padding: 0;
      display: block;
    }}
    body.thumbnail-mode .stage {{
      min-height: 100vh;
      height: 100vh;
      border: 0;
      border-radius: 0;
      cursor: default;
    }}
    body.thumbnail-mode canvas {{
      min-height: 100vh;
      height: 100vh;
    }}
    .stitch {{
      fill: none;
      stroke-width: 0.52;
      stroke-linecap: round;
      stroke-linejoin: round;
      vector-effect: non-scaling-stroke;
    }}
    .jump {{
      fill: none;
      stroke: #66736f;
      stroke-width: 0.32;
      stroke-dasharray: 1.6 1.2;
      opacity: 0.45;
      vector-effect: non-scaling-stroke;
    }}
    .marker {{
      vector-effect: non-scaling-stroke;
      stroke-width: 0.24;
      opacity: 0.85;
    }}
    .needle-point {{
      vector-effect: non-scaling-stroke;
      stroke: rgba(23, 32, 38, 0.65);
      stroke-width: 0.12;
      opacity: 0.85;
    }}
    .trim {{ fill: #f97316; stroke: #7c2d12; }}
    .color_change {{ fill: #2563eb; stroke: #172554; }}
    body.hide-jumps .jump {{ display: none; }}
    body.hide-trims .trim {{ display: none; }}
    body.hide-color-changes .color_change {{ display: none; }}
    body.hide-points .needle-point {{ display: none; }}
    @media (max-width: 820px) {{
      body {{
        grid-template-columns: 1fr;
      }}
      aside {{
        position: static;
        height: auto;
        max-height: none;
        overflow: visible;
        border-right: 0;
        border-bottom: 1px solid #d9ded6;
      }}
      .sidebar-resizer {{
        display: none;
      }}
      main {{
        padding: 18px;
        min-height: 62vh;
      }}
      .thread-floater {{
        position: static;
        width: auto;
        max-height: none;
        margin: 0 18px 18px;
        overflow: visible;
      }}
      .thread-floater ul {{
        overflow: visible;
      }}
      .stage, canvas {{
        min-height: 62vh;
      }}
    }}
  </style>
</head>
<body class="has-thread-floater" data-stats="{embedded_stats}">
  <div class="viewer-menu">
    <button id="viewer-menu-toggle" class="viewer-menu-button" type="button" aria-label="Open menu">&#9776;</button>
    <div class="viewer-menu-panel" aria-label="Application menu">
      <nav class="viewer-menu-actions" aria-label="Application actions">
        <a href="/">Convert Another</a>
        <a href="/library">Library</a>
        <a href="/inventory">Thread Inventory</a>
        {pes_download}
        {project_download}
        {email_project}
        <button id="left-panel-toggle" class="menu-panel-button" type="button">Hide Left Panel</button>
        <button id="left-panel-float" class="menu-panel-button" type="button">Float Left Panel</button>
        <button id="thread-panel-toggle" class="menu-panel-button" type="button">Hide Threads</button>
      </nav>
      <section class="viewer-settings" aria-label="Viewer settings">
        <label>
          Units
          <select id="measurement-units">
            <option value="metric">Metric (mm)</option>
            <option value="sae">SAE (inches)</option>
          </select>
        </label>
        <label>
          Fabric color
          <input id="fabric-color" type="color" value="#fbfcfa">
        </label>
      </section>
      {thread_plan}
    </div>
  </div>
  <aside>
    <h1>{html.escape(input_file.name)}</h1>
    <section class="stats">
      {stats_html}
    </section>
    {density_warning}
    {pes_download}
    {project_download}
    <section class="playback" aria-label="Stitch playback controls">
      <div class="transport">
        <button id="play-toggle" type="button">Play</button>
        <div id="step-counter" class="counter">0 / {max_step}</div>
      </div>
      <div class="range-row">
        <header><span>Stitch step</span><span id="step-label">{max_step}</span></header>
        <input id="stitch-slider" type="range" min="0" max="{max_step}" value="{max_step}" step="1">
      </div>
      <div class="range-row">
        <header><span>Speed</span><span id="speed-label">40 stitches/sec</span></header>
        <input id="speed-slider" type="range" min="1" max="160" value="40" step="1">
      </div>
    </section>
    <section class="controls">
      <label><input id="toggle-jumps" type="checkbox" checked> Show jumps</label>
      <label><input id="toggle-points" type="checkbox" checked> Show needle points</label>
      <label><input id="toggle-trims" type="checkbox" checked> Show trims</label>
      <label><input id="toggle-color-changes" type="checkbox" checked> Show color changes</label>
    </section>
    <section class="view-legend" aria-label="Preview symbol legend">
      <h2>Legend</h2>
      <div class="legend-item" title="Stitches are the thread paths sewn into the fabric.">
        <span class="legend-symbol stitch-line"></span><span>Stitches</span>
      </div>
      <div class="legend-item" title="Jumps are travel moves where the needle moves without forming visible thread.">
        <span class="legend-symbol jump-line"></span><span>Jumps</span>
      </div>
      <div class="legend-item" title="Needle points show individual stitch endpoints when enabled.">
        <span class="legend-symbol needle-dot"></span><span>Needle points</span>
      </div>
      <div class="legend-item" title="Orange dots mark trim commands.">
        <span class="legend-symbol trim-dot"></span><span>Trims</span>
      </div>
      <div class="legend-item" title="Blue dots mark color-change commands.">
        <span class="legend-symbol change-dot"></span><span>Color changes</span>
      </div>
    </section>
    <section class="toolpath-actions" aria-label="Toolpath zoom controls">
      <button id="zoom-out" type="button">Zoom -</button>
      <button id="zoom-reset" type="button">Reset</button>
      <button id="zoom-in" type="button">Zoom +</button>
    </section>
    <p id="zoom-readout" class="zoom-readout">Zoom 100%</p>
  </aside>
  <button id="sidebar-resizer" class="sidebar-resizer" type="button" aria-label="Resize left panel"></button>
  <main>
    <div id="stage" class="stage">
      <canvas id="toolpath" data-initial-viewbox="{embedded_view_box}" aria-label="Embroidery stitch preview"></canvas>
    </div>
  </main>
  <div id="canvas-tooltip" class="canvas-tooltip hidden" role="tooltip"></div>
  {export_open}
    <section class="thread-floater" aria-label="Thread color controls">
      <div class="thread-floater-title">
        <h2><span>Threads</span></h2>
        <button id="thread-floater-toggle" class="thread-floater-toggle" type="button" aria-label="Collapse thread controls">-</button>
      </div>
      <ul>
        {legend}
      </ul>
      {export_button}
    </section>
  {export_close}
  <script id="segment-data" type="application/json">{embedded_segments}</script>
  <script id="marker-data" type="application/json">{embedded_markers}</script>
  <script id="palette-data" type="application/json">{embedded_palette}</script>
  <script id="usage-data" type="application/json">{embedded_usage}</script>
  <script id="inventory-data" type="application/json">{embedded_inventory}</script>
  <script>
    const jumps = document.getElementById("toggle-jumps");
    const points = document.getElementById("toggle-points");
    const trims = document.getElementById("toggle-trims");
    const colorChanges = document.getElementById("toggle-color-changes");
    const playToggle = document.getElementById("play-toggle");
    const stitchSlider = document.getElementById("stitch-slider");
    const speedSlider = document.getElementById("speed-slider");
    const stepCounter = document.getElementById("step-counter");
    const stepLabel = document.getElementById("step-label");
    const speedLabel = document.getElementById("speed-label");
    const stage = document.getElementById("stage");
    const toolpath = document.getElementById("toolpath");
    const zoomIn = document.getElementById("zoom-in");
    const zoomOut = document.getElementById("zoom-out");
    const zoomReset = document.getElementById("zoom-reset");
    const zoomReadout = document.getElementById("zoom-readout");
    const canvasTooltip = document.getElementById("canvas-tooltip");
    const viewerMenuToggle = document.getElementById("viewer-menu-toggle");
    const leftPanelToggle = document.getElementById("left-panel-toggle");
    const leftPanelFloat = document.getElementById("left-panel-float");
    const threadPanelToggle = document.getElementById("thread-panel-toggle");
    const measurementUnits = document.getElementById("measurement-units");
    const fabricColor = document.getElementById("fabric-color");
    const sidebarResizer = document.getElementById("sidebar-resizer");
    const threadFloaterToggle = document.getElementById("thread-floater-toggle");
    const shoppingListText = document.getElementById("shopping-list-text");
    const shoppingListEmpty = document.getElementById("shopping-list-empty");
    const copyShoppingList = document.getElementById("copy-shopping-list");
    const downloadShoppingList = document.getElementById("download-shopping-list");
    const emailProjectForm = document.getElementById("email-project-form");
    const emailShoppingList = document.getElementById("email-shopping-list");
    const ctx = toolpath.getContext("2d");
    const segments = JSON.parse(document.getElementById("segment-data").textContent);
    const markerData = JSON.parse(document.getElementById("marker-data").textContent);
    const palette = JSON.parse(document.getElementById("palette-data").textContent);
    const usageByBlock = JSON.parse(document.getElementById("usage-data").textContent);
    const inventoryThreads = JSON.parse(document.getElementById("inventory-data").textContent);
    const markerColors = {{
      trim: {{ fill: "#f97316", stroke: "#7c2d12" }},
      color_change: {{ fill: "#2563eb", stroke: "#172554" }},
    }};
    const travelColors = {{
      0: {{ stroke: "#66736f", alpha: 0.45, dash: [5, 4] }},
      2: {{ stroke: "#f97316", alpha: 0.78, dash: [7, 4, 2, 4] }},
      3: {{ stroke: "#2563eb", alpha: 0.78, dash: [7, 4, 2, 4] }},
    }};
    const thumbnailMode = new URLSearchParams(window.location.search).get("embed") === "thumbnail";
    if (thumbnailMode) {{
      document.body.classList.add("thumbnail-mode", "hide-jumps", "hide-trims", "hide-color-changes");
    }}
    const savedLeftPanelMode = localStorage.getItem("openstitch-left-panel-mode") || "docked";
    if (savedLeftPanelMode === "collapsed") {{
      document.body.classList.add("left-panel-collapsed");
    }} else if (savedLeftPanelMode === "floating") {{
      document.body.classList.add("left-panel-floating");
    }}
    const blockToggles = [...document.querySelectorAll(".block-toggle")];
    const selectedBlocksInput = document.getElementById("selected-blocks");
    const colorOrderInput = document.getElementById("color-order");
    const colorOverridesInput = document.getElementById("color-overrides");
    const threadLabelOverridesInput = document.getElementById("thread-label-overrides");
    const threadList = document.querySelector(".color-export ul");
    const orderButtons = [...document.querySelectorAll("[data-order-move]")];
    const colorInputs = [...document.querySelectorAll("[data-block-color]")];
    const threadSearches = [...document.querySelectorAll("[data-block-search]")];
    const threadSelects = [...document.querySelectorAll("[data-block-select]")];
    const knownThreadOptions = threadSelects.length
      ? [...threadSelects[0].querySelectorAll("option")]
          .filter((option) => option.value)
          .map((option) => ({{
            value: option.value,
            label: option.textContent,
            group: option.parentElement && option.parentElement.tagName === "OPTGROUP" ? option.parentElement.label : "",
          }}))
      : [];
    let selectedBlocks = new Set(blockToggles.map((toggle) => Number(toggle.value)));
    const maxStep = {max_step};
    const initialViewBox = JSON.parse(toolpath.dataset.initialViewbox);
    let viewBoxState = {{ ...initialViewBox }};
    let currentStep = maxStep;
    let playing = false;
    let lastFrame = 0;
    let carry = 0;
    let showJumps = !thumbnailMode;
    let showPoints = true;
    let showTrims = !thumbnailMode;
    let showColorChanges = !thumbnailMode;
    let deviceScale = 1;
    let sidebarDrag = null;
    let displayUnits = localStorage.getItem("openstitch-measurement-units") || {embedded_display_units};
    let fabricBackground = localStorage.getItem("openstitch-fabric-color") || {embedded_fabric_color};
    if (!/^#[0-9a-f]{{6}}$/i.test(fabricBackground)) fabricBackground = "#fbfcfa";
    if (!["metric", "sae"].includes(displayUnits)) displayUnits = "metric";
    if (measurementUnits) measurementUnits.value = displayUnits;
    if (fabricColor) fabricColor.value = fabricBackground;

    function setSidebarWidth(width) {{
      const clamped = Math.max(260, Math.min(560, Math.round(width)));
      document.documentElement.style.setProperty("--sidebar-width", `${{clamped}}px`);
      try {{
        localStorage.setItem("embroideryViewerSidebarWidth", String(clamped));
      }} catch (error) {{}}
      resizeCanvas();
    }}

    try {{
      const savedSidebarWidth = Number(localStorage.getItem("embroideryViewerSidebarWidth"));
      if (Number.isFinite(savedSidebarWidth) && savedSidebarWidth > 0) {{
        document.documentElement.style.setProperty("--sidebar-width", `${{Math.max(260, Math.min(560, savedSidebarWidth))}}px`);
      }}
    }} catch (error) {{}}

    function resizeCanvas() {{
      const rect = toolpath.getBoundingClientRect();
      deviceScale = window.devicePixelRatio || 1;
      const width = Math.max(1, Math.round(rect.width * deviceScale));
      const height = Math.max(1, Math.round(rect.height * deviceScale));
      if (toolpath.width !== width || toolpath.height !== height) {{
        toolpath.width = width;
        toolpath.height = height;
      }}
      renderScene();
    }}

    function canvasViewport() {{
      const canvasAspect = toolpath.width / Math.max(toolpath.height, 1);
      const viewAspect = viewBoxState.width / Math.max(viewBoxState.height, 0.001);
      if (canvasAspect > viewAspect) {{
        const width = toolpath.height * viewAspect;
        return {{
          x: (toolpath.width - width) / 2,
          y: 0,
          width,
          height: toolpath.height,
        }};
      }}
      const height = toolpath.width / Math.max(viewAspect, 0.001);
      return {{
        x: 0,
        y: (toolpath.height - height) / 2,
        width: toolpath.width,
        height,
      }};
    }}

    function toCanvasX(x) {{
      const viewport = canvasViewport();
      return viewport.x + ((x - viewBoxState.x) / viewBoxState.width) * viewport.width;
    }}

    function toCanvasY(y) {{
      const viewport = canvasViewport();
      return viewport.y + ((y - viewBoxState.y) / viewBoxState.height) * viewport.height;
    }}

    function formatPoint(x, y) {{
      if (displayUnits === "sae") {{
        return `${{(x / 25.4).toFixed(3)}} in, ${{(y / 25.4).toFixed(3)}} in`;
      }}
      return `${{x.toFixed(1)}} mm, ${{y.toFixed(1)}} mm`;
    }}

    function gridSpec() {{
      const viewport = canvasViewport();
      if (displayUnits === "sae") {{
        const candidates = [0.125, 0.25, 0.5, 1, 2, 4];
        for (const inches of candidates) {{
          const mm = inches * 25.4;
          const px = (mm / Math.max(viewBoxState.width, 0.001)) * viewport.width;
          if (px >= 44) {{
            return {{
              stepMm: mm,
              label: (value) => `${{(value / 25.4).toFixed(inches < 1 ? 3 : inches < 2 ? 2 : 1)}} in`,
            }};
          }}
        }}
        return {{ stepMm: 101.6, label: (value) => `${{(value / 25.4).toFixed(1)}} in` }};
      }}
      const candidates = [1, 2, 5, 10, 20, 50, 100];
      for (const mm of candidates) {{
        const px = (mm / Math.max(viewBoxState.width, 0.001)) * viewport.width;
        if (px >= 44) return {{ stepMm: mm, label: (value) => `${{Math.round(value)}} mm` }};
      }}
      return {{ stepMm: 100, label: (value) => `${{Math.round(value)}} mm` }};
    }}

    function drawMeasuredGrid() {{
      const viewport = canvasViewport();
      const spec = gridSpec();
      const bg = fabricBackground.replace("#", "");
      const r = parseInt(bg.slice(0, 2), 16);
      const g = parseInt(bg.slice(2, 4), 16);
      const b = parseInt(bg.slice(4, 6), 16);
      const darkFabric = (r * 0.299 + g * 0.587 + b * 0.114) < 128;
      ctx.save();
      ctx.fillStyle = fabricBackground;
      ctx.fillRect(0, 0, toolpath.width, toolpath.height);
      ctx.beginPath();
      ctx.rect(viewport.x, viewport.y, viewport.width, viewport.height);
      ctx.clip();
      ctx.lineWidth = Math.max(1, deviceScale);
      ctx.strokeStyle = darkFabric ? "rgba(255, 255, 255, 0.16)" : "rgba(92, 107, 99, 0.16)";
      ctx.fillStyle = darkFabric ? "rgba(255, 255, 255, 0.78)" : "rgba(23, 32, 38, 0.72)";
      ctx.font = `${{Math.max(10, 11 * deviceScale)}}px Segoe UI, Arial, sans-serif`;
      ctx.textAlign = "left";
      ctx.textBaseline = "top";
      const startX = Math.floor(viewBoxState.x / spec.stepMm) * spec.stepMm;
      const endX = viewBoxState.x + viewBoxState.width;
      const startY = Math.floor(viewBoxState.y / spec.stepMm) * spec.stepMm;
      const endY = viewBoxState.y + viewBoxState.height;
      for (let x = startX; x <= endX + 0.001; x += spec.stepMm) {{
        const sx = toCanvasX(x);
        ctx.beginPath();
        ctx.moveTo(sx, viewport.y);
        ctx.lineTo(sx, viewport.y + viewport.height);
        ctx.stroke();
        if (sx >= viewport.x + 4 && sx <= viewport.x + viewport.width - 4) {{
          ctx.fillText(spec.label(x), sx + 3 * deviceScale, viewport.y + 3 * deviceScale);
        }}
      }}
      for (let y = startY; y <= endY + 0.001; y += spec.stepMm) {{
        const sy = toCanvasY(y);
        ctx.beginPath();
        ctx.moveTo(viewport.x, sy);
        ctx.lineTo(viewport.x + viewport.width, sy);
        ctx.stroke();
        if (sy >= viewport.y + 18 * deviceScale && sy <= viewport.y + viewport.height - 4) {{
          ctx.fillText(spec.label(y), viewport.x + 4 * deviceScale, sy + 3 * deviceScale);
        }}
      }}
      ctx.restore();
    }}

    function segmentKindName(kind) {{
      if (kind === 1) return "Stitch";
      if (kind === 2) return "Travel after trim";
      if (kind === 3) return "Travel after color change";
      return "Jump";
    }}

    function markerKindName(kind) {{
      return kind === "color_change" ? "Color change" : "Trim";
    }}

    function markerRadiusFor(kind) {{
      return kind === "color_change" ? Math.max(4.8, deviceScale * 4.8) : Math.max(3, deviceScale * 3.2);
    }}

    function shouldShowMarker(kind) {{
      if (kind === "trim") return showTrims;
      if (kind === "color_change") return showColorChanges;
      return false;
    }}

    function distanceToSegment(px, py, x1, y1, x2, y2) {{
      const dx = x2 - x1;
      const dy = y2 - y1;
      if (dx === 0 && dy === 0) return Math.hypot(px - x1, py - y1);
      const t = Math.max(0, Math.min(1, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)));
      return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
    }}

    function describeCanvasHit(point) {{
      const px = toCanvasX(point.x);
      const py = toCanvasY(point.y);
      const zoom = initialViewBox.width / viewBoxState.width;
      for (const markerKind of ["color_change", "trim"]) {{
        for (const marker of markerData) {{
          if (marker[2] !== markerKind || !shouldShowMarker(marker[2]) || marker[3] > currentStep) continue;
          const distance = Math.hypot(px - toCanvasX(marker[0]), py - toCanvasY(marker[1]));
          if (distance <= Math.max(8, markerRadiusFor(marker[2]) + 3)) {{
            return `${{markerKindName(marker[2])}}<br>Step ${{marker[3]}}<br>${{formatPoint(marker[0], marker[1])}}`;
          }}
        }}
      }}
      if (showPoints && (currentStep <= 20000 || zoom >= 2)) {{
        let nearestPoint = null;
        let nearestDistance = Number.POSITIVE_INFINITY;
        for (const segment of segments) {{
          if (segment[4] !== 1 || segment[6] > currentStep || !selectedBlocks.has(segment[5])) continue;
          const distance = Math.hypot(px - toCanvasX(segment[2]), py - toCanvasY(segment[3]));
          if (distance < nearestDistance) {{
            nearestDistance = distance;
            nearestPoint = segment;
          }}
        }}
        if (nearestPoint && nearestDistance <= Math.max(8, deviceScale * 5)) {{
          return `Needle point<br>Step ${{nearestPoint[6]}}<br>Thread block ${{nearestPoint[5] + 1}}<br>${{formatPoint(nearestPoint[2], nearestPoint[3])}}`;
        }}
      }}
      let nearestSegment = null;
      let nearestDistance = Number.POSITIVE_INFINITY;
      for (const segment of segments) {{
        if (segment[6] > currentStep || !selectedBlocks.has(segment[5])) continue;
        const isStitch = segment[4] === 1;
        if (!isStitch && !showJumps) continue;
        const distance = distanceToSegment(
          px,
          py,
          toCanvasX(segment[0]),
          toCanvasY(segment[1]),
          toCanvasX(segment[2]),
          toCanvasY(segment[3])
        );
        if (distance < nearestDistance) {{
          nearestDistance = distance;
          nearestSegment = segment;
        }}
      }}
      if (nearestSegment && nearestDistance <= Math.max(7, deviceScale * 4)) {{
        return `${{segmentKindName(nearestSegment[4])}}<br>Step ${{nearestSegment[6]}}<br>Thread block ${{nearestSegment[5] + 1}}`;
      }}
      return "";
    }}

    function showCanvasTooltip(event) {{
      if (!canvasTooltip) return;
      const text = describeCanvasHit(svgPointFromEvent(event));
      if (!text) {{
        canvasTooltip.classList.add("hidden");
        return;
      }}
      canvasTooltip.innerHTML = text;
      canvasTooltip.style.left = `${{event.clientX + 14}}px`;
      canvasTooltip.style.top = `${{event.clientY + 14}}px`;
      canvasTooltip.classList.remove("hidden");
    }}

    function renderScene() {{
      if (!ctx) return;
      ctx.clearRect(0, 0, toolpath.width, toolpath.height);
      drawMeasuredGrid();
      ctx.lineCap = "round";
      ctx.lineJoin = "round";

      const stitchWidth = Math.max(0.8, deviceScale * 1.2);
      const jumpWidth = Math.max(0.6, deviceScale * 0.8);
      for (const segment of segments) {{
        const step = segment[6];
        if (step > currentStep) continue;
        if (!selectedBlocks.has(segment[5])) continue;
        const isStitch = segment[4] === 1;
        if (!isStitch && !showJumps) continue;
        const travelStyle = travelColors[segment[4]] || travelColors[0];
        ctx.beginPath();
        ctx.strokeStyle = isStitch ? palette[segment[5]] : travelStyle.stroke;
        ctx.globalAlpha = isStitch ? 1 : travelStyle.alpha;
        ctx.lineWidth = isStitch ? stitchWidth : jumpWidth;
        if (!isStitch) ctx.setLineDash(travelStyle.dash.map((value) => value * deviceScale));
        else ctx.setLineDash([]);
        ctx.moveTo(toCanvasX(segment[0]), toCanvasY(segment[1]));
        ctx.lineTo(toCanvasX(segment[2]), toCanvasY(segment[3]));
        ctx.stroke();
      }}
      ctx.setLineDash([]);
      ctx.globalAlpha = 1;

      const zoom = initialViewBox.width / viewBoxState.width;
      if (showPoints && (currentStep <= 20000 || zoom >= 2)) {{
        const radius = stitchWidth * 2;
        for (const segment of segments) {{
          if (segment[4] !== 1 || segment[6] > currentStep) continue;
          if (!selectedBlocks.has(segment[5])) continue;
          ctx.beginPath();
          ctx.fillStyle = palette[segment[5]];
          ctx.strokeStyle = palette[segment[5]];
          ctx.lineWidth = Math.max(0.5, stitchWidth * 0.25);
          ctx.arc(toCanvasX(segment[2]), toCanvasY(segment[3]), radius, 0, Math.PI * 2);
          ctx.fill();
          ctx.stroke();
        }}
      }}

      for (const markerKind of ["trim", "color_change"]) {{
        for (const marker of markerData) {{
          if (marker[2] !== markerKind || !shouldShowMarker(marker[2]) || marker[3] > currentStep) continue;
          const x = toCanvasX(marker[0]);
          const y = toCanvasY(marker[1]);
          const markerColor = markerColors[marker[2]] || {{ fill: "#64748b", stroke: "#1f2937" }};
          ctx.beginPath();
          ctx.fillStyle = markerColor.fill;
          ctx.strokeStyle = markerColor.stroke;
          ctx.lineWidth = marker[2] === "color_change" ? Math.max(1.4, deviceScale * 1.4) : Math.max(0.8, deviceScale);
          ctx.arc(x, y, markerRadiusFor(marker[2]), 0, Math.PI * 2);
          ctx.fill();
          ctx.stroke();
        }}
      }}
    }}

    function setViewBox(next) {{
      viewBoxState = next;
      const zoomPercent = Math.round((initialViewBox.width / viewBoxState.width) * 100);
      zoomReadout.textContent = `Zoom ${{zoomPercent}}%`;
      renderScene();
    }}

    function svgPointFromEvent(event) {{
      const rect = toolpath.getBoundingClientRect();
      const viewport = canvasViewport();
      const canvasX = ((event.clientX - rect.left) / Math.max(rect.width, 1)) * toolpath.width;
      const canvasY = ((event.clientY - rect.top) / Math.max(rect.height, 1)) * toolpath.height;
      const px = (canvasX - viewport.x) / Math.max(viewport.width, 1);
      const py = (canvasY - viewport.y) / Math.max(viewport.height, 1);
      return {{
        x: viewBoxState.x + px * viewBoxState.width,
        y: viewBoxState.y + py * viewBoxState.height,
      }};
    }}

    function zoomAt(factor, center) {{
      const nextWidth = Math.min(initialViewBox.width * 8, Math.max(initialViewBox.width / 80, viewBoxState.width * factor));
      const nextHeight = Math.min(initialViewBox.height * 8, Math.max(initialViewBox.height / 80, viewBoxState.height * factor));
      const anchor = center || {{
        x: viewBoxState.x + viewBoxState.width / 2,
        y: viewBoxState.y + viewBoxState.height / 2,
      }};
      const xRatio = (anchor.x - viewBoxState.x) / viewBoxState.width;
      const yRatio = (anchor.y - viewBoxState.y) / viewBoxState.height;
      setViewBox({{
        x: anchor.x - nextWidth * xRatio,
        y: anchor.y - nextHeight * yRatio,
        width: nextWidth,
        height: nextHeight,
      }});
    }}

    function resetZoom() {{
      setViewBox({{ ...initialViewBox }});
    }}

    function syncSelectedBlocks() {{
      const orderedRows = [...document.querySelectorAll("[data-block-row]")];
      const orderedBlocks = orderedRows.map((row) => Number(row.dataset.blockRow));
      const checkedBlocks = orderedRows
        .map((row) => row.querySelector(".block-toggle"))
        .filter((toggle) => toggle && toggle.checked)
        .map((toggle) => Number(toggle.value));
      selectedBlocks = new Set(checkedBlocks);
      if (selectedBlocksInput) {{
        selectedBlocksInput.value = checkedBlocks.join(",");
      }}
      if (colorOrderInput) {{
        colorOrderInput.value = orderedBlocks.join(",");
      }}
      syncColorOverrides();
      syncThreadLabelOverrides();
      updateShoppingList();
      renderScene();
    }}

    function normalizeHex(value) {{
      const text = value.trim();
      const full = /^#[0-9a-f]{{6}}$/i;
      const short = /^#[0-9a-f]{{3}}$/i;
      if (full.test(text)) return text.toLowerCase();
      if (short.test(text)) {{
        return "#" + [...text.slice(1)].map((char) => char + char).join("").toLowerCase();
      }}
      return null;
    }}

    function hexToRgb(value) {{
      const color = normalizeHex(value);
      if (!color) return null;
      return [
        Number.parseInt(color.slice(1, 3), 16),
        Number.parseInt(color.slice(3, 5), 16),
        Number.parseInt(color.slice(5, 7), 16),
      ];
    }}

    function colorDistance(a, b) {{
      const first = hexToRgb(a);
      const second = hexToRgb(b);
      if (!first || !second) return Number.POSITIVE_INFINITY;
      return Math.hypot(first[0] - second[0], first[1] - second[1], first[2] - second[2]);
    }}

    function readableTextColor(color) {{
      const rgb = hexToRgb(color);
      if (!rgb) return "#172026";
      const luminance = (0.299 * rgb[0]) + (0.587 * rgb[1]) + (0.114 * rgb[2]);
      return luminance < 128 ? "#ffffff" : "#172026";
    }}

    function decorateThreadOption(node, color) {{
      node.dataset.swatch = color;
      node.style.backgroundColor = color;
      node.style.color = readableTextColor(color);
      node.title = color;
      return node;
    }}

    function formatUsage(meters) {{
      if (meters < 1) return `${{Math.round(meters * 100)}} cm`;
      return `${{meters.toFixed(2)}} m`;
    }}

    function closestThreadOption(color, options) {{
      if (!options.length) return null;
      return options
        .map((option) => ({{
          ...option,
          distance: colorDistance(color, option.value),
        }}))
        .sort((a, b) => a.distance - b.distance || a.label.localeCompare(b.label))[0];
    }}

    function cleanThreadLabel(label) {{
      return label.replace(/ - match \\d+$/, "");
    }}

    function updateShoppingList() {{
      if (!shoppingListText) return;
      const inventoryOptions = inventoryThreads.map((item) => ({{
        value: item.color,
        label: `${{item.label}} - ${{item.color}}`,
        group: "Your inventory",
      }}));
      const shopping = new Map();
      for (const blockIndex of selectedBlocks) {{
        const color = palette[blockIndex];
        const inventoryMatch = closestThreadOption(color, inventoryOptions);
        if (inventoryMatch && inventoryMatch.distance <= 64) continue;
        const catalogMatch = closestThreadOption(color, knownThreadOptions);
        if (!catalogMatch) continue;
        const label = cleanThreadLabel(catalogMatch.label);
        const existing = shopping.get(label) || {{
          label,
          color: catalogMatch.value,
          meters: 0,
          designColors: new Set(),
        }};
        existing.meters += Number(usageByBlock[String(blockIndex)] || 0);
        existing.designColors.add(color);
        shopping.set(label, existing);
      }}
      const items = [...shopping.values()].sort((a, b) => a.label.localeCompare(b.label));
      const hasItems = items.length > 0;
      shoppingListText.value = hasItems
        ? [
            "Thread shopping list",
            "",
            ...items.map((item) => (
              `- ${{item.label}} (${{item.color}}) - ${{formatUsage(item.meters)}} estimated - for ${{[...item.designColors].sort().join(", ")}}`
            )),
          ].join("\\n")
        : "";
      shoppingListText.classList.toggle("hidden", !hasItems);
      if (shoppingListEmpty) shoppingListEmpty.classList.toggle("hidden", hasItems);
      if (copyShoppingList) copyShoppingList.disabled = !hasItems;
      if (downloadShoppingList) downloadShoppingList.disabled = !hasItems;
    }}

    function sortThreadSelect(select, targetColor) {{
      if (!select || knownThreadOptions.length === 0) return;
      const currentValue = select.value;
      const row = select.closest("[data-block-row]");
      const search = row ? row.querySelector("[data-block-search]") : null;
      const query = search ? search.value.trim().toLowerCase() : "";
      const sorted = knownThreadOptions
        .filter((option) => {{
          if (!query) return true;
          return `${{option.label}} ${{option.value}} ${{option.group}}`.toLowerCase().includes(query);
        }})
        .map((option) => ({{
          ...option,
          distance: colorDistance(targetColor, option.value),
        }}))
        .sort((a, b) => a.distance - b.distance || a.label.localeCompare(b.label));
      const placeholder = query ? `Closest matches for "${{query}}"` : "Closest known thread colors";
      select.replaceChildren(new Option(placeholder, ""));
      for (const option of sorted) {{
        const distance = Number.isFinite(option.distance) ? Math.round(option.distance) : "?";
        const label = `${{option.label}} - match ${{distance}}`;
        const node = new Option(label, option.value);
        if (option.group) node.dataset.group = option.group;
        decorateThreadOption(node, option.value);
        select.add(node);
      }}
      if (sorted.length === 0) {{
        select.add(new Option("No matching thread colors", ""));
      }}
      select.value = currentValue;
    }}

    function sortAllThreadSelects() {{
      for (const select of threadSelects) {{
        const blockIndex = Number(select.dataset.blockSelect);
        sortThreadSelect(select, palette[blockIndex]);
      }}
    }}

    function syncColorOverrides() {{
      if (!colorOverridesInput) return;
      const overrides = {{}};
      for (const input of colorInputs) {{
        const blockIndex = Number(input.dataset.blockColor);
        const color = normalizeHex(input.value);
        if (color) overrides[blockIndex] = color;
      }}
      colorOverridesInput.value = JSON.stringify(overrides);
    }}

    function labelForBlock(blockIndex) {{
      const color = palette[blockIndex];
      const row = document.querySelector(`[data-block-row="${{blockIndex}}"]`);
      const select = row ? row.querySelector("[data-block-select]") : null;
      if (select && select.value && normalizeHex(select.value) === color) {{
        const selected = select.selectedOptions && select.selectedOptions[0];
        if (selected && selected.textContent) return cleanThreadLabel(selected.textContent);
      }}
      const closest = closestThreadOption(color, knownThreadOptions);
      return closest ? cleanThreadLabel(closest.label) : `Thread ${{color}}`;
    }}

    function syncThreadLabelOverrides() {{
      if (!threadLabelOverridesInput) return;
      const labels = {{}};
      for (const blockIndex of selectedBlocks) {{
        labels[blockIndex] = labelForBlock(blockIndex);
      }}
      threadLabelOverridesInput.value = JSON.stringify(labels);
    }}

    function applyBlockColor(blockIndex, color) {{
      const normalized = normalizeHex(color);
      if (!normalized) return;
      palette[blockIndex] = normalized;
      const row = document.querySelector(`[data-block-row="${{blockIndex}}"]`);
      if (row) {{
        const swatch = row.querySelector(".swatch");
        const code = row.querySelector("code");
        const input = row.querySelector("[data-block-color]");
        const select = row.querySelector("[data-block-select]");
        if (swatch) swatch.style.background = normalized;
        if (code) code.textContent = normalized;
        if (input) input.value = normalized;
        sortThreadSelect(select, normalized);
      }}
      syncColorOverrides();
      syncThreadLabelOverrides();
      updateShoppingList();
      renderScene();
    }}

    function moveColorBlock(button) {{
      const row = button.closest("[data-block-row]");
      if (!row || !threadList) return;
      if (button.dataset.orderMove === "up" && row.previousElementSibling) {{
        threadList.insertBefore(row, row.previousElementSibling);
      }}
      if (button.dataset.orderMove === "down" && row.nextElementSibling) {{
        threadList.insertBefore(row.nextElementSibling, row);
      }}
      syncSelectedBlocks();
    }}

    function applyTimeline(step) {{
      currentStep = Math.max(0, Math.min(maxStep, Math.round(step)));
      stitchSlider.value = String(currentStep);
      stepLabel.textContent = String(currentStep);
      stepCounter.textContent = `${{currentStep}} / ${{maxStep}}`;
      renderScene();
      if (currentStep >= maxStep && playing) {{
        stopPlayback();
      }}
    }}

    function stopPlayback() {{
      playing = false;
      playToggle.textContent = "Play";
      lastFrame = 0;
      carry = 0;
    }}

    function tick(timestamp) {{
      if (!playing) return;
      if (!lastFrame) lastFrame = timestamp;
      const elapsed = (timestamp - lastFrame) / 1000;
      lastFrame = timestamp;
      carry += elapsed * Number(speedSlider.value);
      const advance = Math.floor(carry);
      if (advance > 0) {{
        carry -= advance;
        applyTimeline(currentStep + advance);
      }}
      if (playing) requestAnimationFrame(tick);
    }}

    function startPlayback() {{
      if (currentStep >= maxStep) {{
        applyTimeline(0);
      }}
      playing = true;
      playToggle.textContent = "Pause";
      requestAnimationFrame(tick);
    }}

    playToggle.addEventListener("click", () => {{
      if (playing) {{
        stopPlayback();
      }} else {{
        startPlayback();
      }}
    }});
    viewerMenuToggle.addEventListener("click", () => {{
      document.body.classList.toggle("viewer-menu-open");
    }});
    function syncPanelMenuLabels() {{
      if (leftPanelToggle) {{
        leftPanelToggle.textContent = document.body.classList.contains("left-panel-collapsed")
          ? "Show Left Panel"
          : "Hide Left Panel";
      }}
      if (leftPanelFloat) {{
        leftPanelFloat.textContent = document.body.classList.contains("left-panel-floating")
          ? "Dock Left Panel"
          : "Float Left Panel";
      }}
      if (threadPanelToggle) {{
        threadPanelToggle.textContent = document.body.classList.contains("thread-floater-collapsed")
          ? "Show Threads"
          : "Hide Threads";
      }}
    }}
    if (leftPanelToggle) {{
      leftPanelToggle.addEventListener("click", () => {{
        const collapsed = !document.body.classList.contains("left-panel-collapsed");
        document.body.classList.toggle("left-panel-collapsed", collapsed);
        if (collapsed) {{
          document.body.classList.remove("left-panel-floating");
          localStorage.setItem("openstitch-left-panel-mode", "collapsed");
        }} else {{
          localStorage.setItem("openstitch-left-panel-mode", "docked");
        }}
        syncPanelMenuLabels();
        resizeCanvas();
      }});
    }}
    if (leftPanelFloat) {{
      leftPanelFloat.addEventListener("click", () => {{
        const floating = !document.body.classList.contains("left-panel-floating");
        document.body.classList.remove("left-panel-collapsed");
        document.body.classList.toggle("left-panel-floating", floating);
        localStorage.setItem("openstitch-left-panel-mode", floating ? "floating" : "docked");
        syncPanelMenuLabels();
        resizeCanvas();
      }});
    }}
    if (threadPanelToggle) {{
      threadPanelToggle.addEventListener("click", () => {{
        const collapsed = document.body.classList.toggle("thread-floater-collapsed");
        if (threadFloaterToggle) {{
          threadFloaterToggle.textContent = collapsed ? "T" : "-";
          threadFloaterToggle.setAttribute(
            "aria-label",
            collapsed ? "Expand thread controls" : "Collapse thread controls"
          );
        }}
        syncPanelMenuLabels();
        resizeCanvas();
      }});
    }}
    if (threadFloaterToggle) {{
      threadFloaterToggle.addEventListener("click", () => {{
        const collapsed = document.body.classList.toggle("thread-floater-collapsed");
        threadFloaterToggle.textContent = collapsed ? "T" : "-";
        threadFloaterToggle.setAttribute(
          "aria-label",
          collapsed ? "Expand thread controls" : "Collapse thread controls"
        );
        syncPanelMenuLabels();
        resizeCanvas();
      }});
    }}
    syncPanelMenuLabels();
    document.addEventListener("click", (event) => {{
      if (!event.target.closest(".viewer-menu")) {{
        document.body.classList.remove("viewer-menu-open");
      }}
    }});
    stitchSlider.addEventListener("input", () => {{
      stopPlayback();
      applyTimeline(Number(stitchSlider.value));
    }});
    speedSlider.addEventListener("input", () => {{
      speedLabel.textContent = `${{speedSlider.value}} stitches/sec`;
    }});
    if (measurementUnits) {{
      measurementUnits.addEventListener("change", () => {{
        displayUnits = measurementUnits.value === "sae" ? "sae" : "metric";
        localStorage.setItem("openstitch-measurement-units", displayUnits);
        renderScene();
      }});
    }}
    if (fabricColor) {{
      fabricColor.addEventListener("input", () => {{
        fabricBackground = /^#[0-9a-f]{{6}}$/i.test(fabricColor.value) ? fabricColor.value : "#fbfcfa";
        localStorage.setItem("openstitch-fabric-color", fabricBackground);
        renderScene();
      }});
    }}
    if (copyShoppingList && shoppingListText) {{
      copyShoppingList.addEventListener("click", async () => {{
        shoppingListText.select();
        try {{
          await navigator.clipboard.writeText(shoppingListText.value);
          copyShoppingList.textContent = "Copied";
          window.setTimeout(() => {{
            copyShoppingList.textContent = "Copy List";
          }}, 1400);
        }} catch (error) {{
          document.execCommand("copy");
        }}
      }});
    }}
    if (downloadShoppingList && shoppingListText) {{
      downloadShoppingList.addEventListener("click", () => {{
        const blob = new Blob([shoppingListText.value], {{ type: "text/plain" }});
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = "thread-shopping-list.txt";
        document.body.appendChild(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(url);
      }});
    }}
    if (emailProjectForm && emailShoppingList) {{
      emailProjectForm.addEventListener("submit", () => {{
        emailShoppingList.value = shoppingListText ? shoppingListText.value : "";
      }});
    }}
    zoomIn.addEventListener("click", () => zoomAt(0.75));
    zoomOut.addEventListener("click", () => zoomAt(1.35));
    zoomReset.addEventListener("click", resetZoom);
    stage.addEventListener("wheel", (event) => {{
      event.preventDefault();
      zoomAt(event.deltaY < 0 ? 0.88 : 1.14, svgPointFromEvent(event));
    }}, {{ passive: false }});

    let dragStart = null;
    stage.addEventListener("pointerdown", (event) => {{
      if (event.button !== 0) return;
      dragStart = {{
        clientX: event.clientX,
        clientY: event.clientY,
        viewBox: {{ ...viewBoxState }},
      }};
      stage.classList.add("dragging");
      stage.setPointerCapture(event.pointerId);
    }});
    stage.addEventListener("pointermove", (event) => {{
      if (!dragStart) return;
      const rect = toolpath.getBoundingClientRect();
      const viewport = canvasViewport();
      const dxCanvas = ((event.clientX - dragStart.clientX) / Math.max(rect.width, 1)) * toolpath.width;
      const dyCanvas = ((event.clientY - dragStart.clientY) / Math.max(rect.height, 1)) * toolpath.height;
      const dx = (dxCanvas / Math.max(viewport.width, 1)) * dragStart.viewBox.width;
      const dy = (dyCanvas / Math.max(viewport.height, 1)) * dragStart.viewBox.height;
      setViewBox({{
        x: dragStart.viewBox.x - dx,
        y: dragStart.viewBox.y - dy,
        width: dragStart.viewBox.width,
        height: dragStart.viewBox.height,
      }});
    }});
    stage.addEventListener("pointermove", (event) => {{
      if (!dragStart) showCanvasTooltip(event);
    }});
    stage.addEventListener("pointerleave", () => {{
      if (canvasTooltip) canvasTooltip.classList.add("hidden");
    }});
    function endDrag(event) {{
      if (!dragStart) return;
      dragStart = null;
      stage.classList.remove("dragging");
      if (canvasTooltip) canvasTooltip.classList.add("hidden");
      if (stage.hasPointerCapture(event.pointerId)) {{
        stage.releasePointerCapture(event.pointerId);
      }}
    }}
    stage.addEventListener("pointerup", endDrag);
    stage.addEventListener("pointercancel", endDrag);
    if (sidebarResizer) {{
      sidebarResizer.addEventListener("pointerdown", (event) => {{
        if (event.button !== 0) return;
        sidebarDrag = {{
          startX: event.clientX,
          startWidth: Number.parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--sidebar-width")) || 340,
        }};
        document.body.classList.add("resizing-sidebar");
        sidebarResizer.setPointerCapture(event.pointerId);
      }});
      sidebarResizer.addEventListener("pointermove", (event) => {{
        if (!sidebarDrag) return;
        setSidebarWidth(sidebarDrag.startWidth + event.clientX - sidebarDrag.startX);
      }});
      function endSidebarResize(event) {{
        if (!sidebarDrag) return;
        sidebarDrag = null;
        document.body.classList.remove("resizing-sidebar");
        if (sidebarResizer.hasPointerCapture(event.pointerId)) {{
          sidebarResizer.releasePointerCapture(event.pointerId);
        }}
        resizeCanvas();
      }}
      sidebarResizer.addEventListener("pointerup", endSidebarResize);
      sidebarResizer.addEventListener("pointercancel", endSidebarResize);
    }}
    jumps.addEventListener("change", () => {{
      showJumps = jumps.checked;
      renderScene();
    }});
    points.addEventListener("change", () => {{
      showPoints = points.checked;
      renderScene();
    }});
    trims.addEventListener("change", () => {{
      showTrims = trims.checked;
      renderScene();
    }});
    colorChanges.addEventListener("change", () => {{
      showColorChanges = colorChanges.checked;
      renderScene();
    }});
    for (const toggle of blockToggles) {{
      toggle.addEventListener("click", (event) => event.stopPropagation());
      toggle.addEventListener("change", syncSelectedBlocks);
    }}
    for (const button of orderButtons) {{
      button.addEventListener("click", () => moveColorBlock(button));
    }}
    for (const input of colorInputs) {{
      input.addEventListener("change", () => {{
        const color = normalizeHex(input.value);
        if (color) {{
          applyBlockColor(Number(input.dataset.blockColor), color);
        }} else {{
          input.value = palette[Number(input.dataset.blockColor)];
        }}
      }});
    }}
    for (const select of threadSelects) {{
      select.addEventListener("change", () => {{
        if (select.value) {{
          applyBlockColor(Number(select.dataset.blockSelect), select.value);
        }}
      }});
    }}
    for (const search of threadSearches) {{
      search.addEventListener("input", () => {{
        const blockIndex = Number(search.dataset.blockSearch);
        const select = document.querySelector(`[data-block-select="${{blockIndex}}"]`);
        sortThreadSelect(select, palette[blockIndex]);
      }});
    }}
    sortAllThreadSelects();
    window.addEventListener("resize", resizeCanvas);
    syncSelectedBlocks();
    resetZoom();
    applyTimeline(maxStep);
    resizeCanvas();
  </script>
</body>
</html>
"""


def collect_svg_segments(
    svg_file: Path,
    sample_step_mm: float,
    fill_spacing_mm: float,
    max_stitch_mm: float,
    fill_angle_deg: float,
    fill_mode: str,
    fit_width_mm: float | None,
    fit_height_mm: float | None,
    center: bool,
) -> tuple[list[dict], list[dict], list[dict], dict]:
    runs = extract_runs_for_final_size(
        svg_file,
        sample_step_mm=sample_step_mm,
        fill_spacing_mm=fill_spacing_mm,
        max_stitch_mm=max_stitch_mm,
        fill_angle_deg=fill_angle_deg,
        fill_mode=fill_mode,
        fit_width_mm=fit_width_mm,
        fit_height_mm=fit_height_mm,
        center=center,
    )

    segments: list[dict] = []
    commands: list[dict] = []
    color_blocks: list[dict] = []
    active_color: str | None = None
    active_block: dict | None = None
    previous_point: tuple[float, float] | None = None
    pending_travel: str | None = None
    counts = {
        "needle_points": 0,
        "stitch_segments": 0,
        "jumps": 0,
        "trims": 0,
        "color_changes": 0,
        "ends": 0,
    }

    for run in runs:
        if len(run.points_mm) < 2:
            continue
        if run.color != active_color:
            if active_color is not None:
                counts["color_changes"] += 1
                marker_x, marker_y = previous_point or run.points_mm[0]
                commands.append(
                    {
                        "x": marker_x,
                        "y": marker_y,
                        "command": "color_change",
                        "color": len(color_blocks),
                        "step": counts["needle_points"],
                    }
                )
                pending_travel = "travel_after_color_change"
            active_color = run.color
            active_block = {
                "index": len(color_blocks),
                "thread": len(color_blocks),
                "color": run.color,
                "label": f"SVG {run.color}",
                "stitches": 0,
            }
            color_blocks.append(active_block)

        assert active_block is not None
        first = run.points_mm[0]
        if previous_point is not None and math.hypot(previous_point[0] - first[0], previous_point[1] - first[1]) > 0.001:
            is_color_travel = pending_travel == "travel_after_color_change"
            if is_color_travel:
                counts["trims"] += 1
                commands.append(
                    {
                        "x": previous_point[0],
                        "y": previous_point[1],
                        "command": "trim",
                        "color": active_block["thread"],
                        "step": counts["needle_points"],
                    }
                )
            counts["jumps"] += 1
            segments.append(
                {
                    "x1": previous_point[0],
                    "y1": previous_point[1],
                    "x2": first[0],
                    "y2": first[1],
                    "kind": pending_travel or "jump",
                    "color": active_block["color"],
                    "colorIndex": active_block["thread"],
                    "blockIndex": active_block["index"],
                    "step": counts["needle_points"],
                }
            )
            pending_travel = None
        commands.append(
            {
                "x": first[0],
                "y": first[1],
                "command": "jump",
                "color": active_block["thread"],
                "step": counts["needle_points"],
            }
        )
        for start, end in zip(run.points_mm, run.points_mm[1:]):
            segment_start = start
            for point in split_long_point_span(start, end, max_stitch_mm):
                counts["needle_points"] += 1
                counts["stitch_segments"] += 1
                active_block["stitches"] += 1
                segments.append(
                    {
                        "x1": segment_start[0],
                        "y1": segment_start[1],
                        "x2": point[0],
                        "y2": point[1],
                        "kind": "stitch",
                        "color": active_block["color"],
                        "colorIndex": active_block["thread"],
                        "blockIndex": active_block["index"],
                        "step": counts["needle_points"],
                    }
                )
                commands.append(
                    {
                        "x": point[0],
                        "y": point[1],
                        "command": "stitch",
                        "color": active_block["thread"],
                        "step": counts["needle_points"],
                    }
                )
                segment_start = point
                previous_point = point

    if not segments:
        raise ValueError("No stitches were generated from the SVG.")
    return segments, commands, color_blocks, counts


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render a PES, SVG, image, or PDF as an animated HTML stitch viewer."
    )
    parser.add_argument("input", type=Path, help="PES, SVG, JPG, PNG, or PDF file to view")
    parser.add_argument("-o", "--output", type=Path, help="Output HTML file")
    parser.add_argument(
        "--pes-output",
        type=Path,
        help="For SVG input, save the converted Brother PES file here; default: beside the HTML",
    )
    parser.add_argument(
        "--fit-width-mm",
        type=positive_float,
        help="For SVG/image/PDF input, scale the design to this width in millimeters",
    )
    parser.add_argument(
        "--fit-height-mm",
        type=positive_float,
        help="For SVG/image/PDF input, scale the design to this height in millimeters",
    )
    parser.add_argument(
        "--sample-step-mm",
        type=positive_float,
        default=0.8,
        help="For SVG input, curve sampling distance in millimeters; default: 0.8",
    )
    parser.add_argument(
        "--fill-spacing-mm",
        type=positive_float,
        default=0.5,
        help="For SVG/image/PDF input, distance between hatch-fill rows in millimeters; default: 0.5",
    )
    parser.add_argument(
        "--thread-weight",
        choices=("40wt", "30wt", "60wt"),
        default=DEFAULT_THREAD_WEIGHT,
        help="Thread weight profile for density warnings; default: 40wt",
    )
    parser.add_argument(
        "--fill-mode",
        choices=("mixed", "contour", "tatami", "horizontal", "crosshatch", "outline"),
        default="tatami",
        help="For SVG/image/PDF input, fill style; default: tatami",
    )
    parser.add_argument(
        "--fill-angle-deg",
        type=float,
        default=45.0,
        help="For tatami image/PDF fill, stitch angle in degrees; default: 45",
    )
    parser.add_argument(
        "--max-stitch-mm",
        type=positive_float,
        default=3.0,
        help="For SVG/image/PDF input, maximum stitch length in millimeters; default: 3.0",
    )
    parser.add_argument(
        "--max-colors",
        type=int,
        default=6,
        help="For image/PDF input, maximum thread colors to quantize; default: 6",
    )
    parser.add_argument(
        "--color-merge-distance",
        type=float,
        default=56.0,
        help="For image/PDF input, merge similar palette colors by RGB distance; default: 56",
    )
    parser.add_argument(
        "--pdf-page",
        type=int,
        default=1,
        help="For PDF input, 1-based page number to convert; default: 1",
    )
    parser.add_argument(
        "--pdf-dpi",
        type=int,
        default=180,
        help="For PDF input, rasterization DPI before digitizing; default: 180",
    )
    parser.add_argument(
        "--no-center",
        action="store_true",
        help="For SVG/image/PDF input, keep the top-left origin instead of centering around the hoop origin",
    )
    return parser.parse_args()


def build_viewer_html(
    input_file: Path,
    *,
    fit_width_mm: float | None = None,
    fit_height_mm: float | None = None,
    sample_step_mm: float = 0.8,
    fill_mode: str = "tatami",
    fill_angle_deg: float = 45.0,
    fill_spacing_mm: float = 0.5,
    thread_weight: str = DEFAULT_THREAD_WEIGHT,
    max_stitch_mm: float = 3.0,
    max_colors: int = 6,
    color_merge_distance: float = 56.0,
    pdf_page: int = 1,
    pdf_dpi: int = 180,
    center: bool = True,
    pes_href: str | None = None,
    color_export_action: str | None = None,
    source_name: str | None = None,
    project_href: str | None = None,
    display_units: str = "metric",
    fabric_color: str = "#fbfcfa",
    stitch_perimeter: bool = False,
) -> tuple[str, tuple[float, float, float, float], dict]:
    pattern = None
    if input_file.suffix.lower() == ".svg" and not svg_needs_rasterization(input_file):
        segments, commands, color_blocks, counts = collect_svg_segments(
            input_file,
            sample_step_mm=sample_step_mm,
            fill_spacing_mm=fill_spacing_mm,
            max_stitch_mm=max_stitch_mm,
            fill_angle_deg=fill_angle_deg,
            fill_mode=fill_mode,
            fit_width_mm=fit_width_mm,
            fit_height_mm=fit_height_mm,
            center=center,
        )
        source_label = "SVG converted preview"
    elif is_raster_source(input_file) or svg_needs_rasterization(input_file):
        raster_fit_width = fit_width_mm if fit_width_mm is not None else 90.0
        segments, commands, color_blocks, counts = image_to_segments(
            input_file,
            fit_width_mm=raster_fit_width,
            fit_height_mm=fit_height_mm,
            max_colors=max_colors,
            fill_mode=fill_mode,
            fill_angle_deg=fill_angle_deg,
            color_merge_distance=color_merge_distance,
            fill_spacing_mm=fill_spacing_mm,
            max_stitch_mm=max_stitch_mm,
            pdf_page=pdf_page,
            pdf_dpi=pdf_dpi,
            stitch_perimeter=stitch_perimeter,
        )
        source_label = "Image converted preview"
    else:
        pattern = embroidery.read(str(input_file))
        if pattern is None:
            raise ValueError(f"Could not read embroidery file: {input_file}")
        segments, commands, color_blocks, counts = collect_segments(pattern)
        source_label = input_file.suffix.upper().lstrip(".") or "Embroidery file"

    segments, commands, color_blocks, counts = group_color_blocks_by_inventory(
        segments,
        commands,
        color_blocks,
        counts,
    )
    segments, color_blocks = apply_thread_metadata(input_file, segments, color_blocks)

    html_text = render_html(
        input_file,
        pattern,
        segments,
        commands,
        color_blocks,
        counts,
        source_label,
        pes_href=pes_href,
        color_export_action=color_export_action,
        source_name=source_name,
        project_href=project_href,
        fit_width_mm=fit_width_mm,
        fill_mode=fill_mode,
        fill_angle_deg=fill_angle_deg,
        fill_spacing_mm=fill_spacing_mm,
        thread_weight=thread_weight,
        max_stitch_mm=max_stitch_mm,
        max_colors=max_colors,
        color_merge_distance=color_merge_distance,
        pdf_page=pdf_page,
        display_units=display_units,
        fabric_color=fabric_color,
        stitch_perimeter=stitch_perimeter,
    )
    return html_text, design_bounds(segments, commands), counts


def write_segments_as_pes(
    segments: list[dict],
    color_blocks: list[dict],
    output_file: Path,
    selected_blocks: set[int] | None = None,
    color_order: list[int] | None = None,
    color_overrides: dict[int, str] | None = None,
    thread_label_overrides: dict[int, str] | None = None,
    *,
    max_stitch_mm: float = 7.0,
    min_stitch_mm: float = 0.3,
    lock_stitch_mm: float = 1.0,
    connect_short_gaps: bool = True,
    max_connect_gap_mm: float = 0.45,
    min_run_length_mm: float = 0.3,
    stitch_perimeter: bool = False,
    perimeter_offset_mm: float = 0.24,
    perimeter_passes: int = 1,
) -> list[dict]:
    selected_blocks = selected_blocks if selected_blocks is not None else {
        block["index"] for block in color_blocks
    }
    block_by_index = {block["index"]: block for block in color_blocks}
    available_blocks = set(block_by_index)
    natural_order: list[int] = []
    for segment in segments:
        block_index = segment["blockIndex"]
        if (
            segment["kind"] == "stitch"
            and block_index in selected_blocks
            and block_index not in natural_order
        ):
            natural_order.append(block_index)
    if color_order is None:
        ordered_blocks = natural_order
    else:
        ordered_blocks = [
            block_index
            for block_index in color_order
            if block_index in available_blocks and block_index in selected_blocks
        ]
        ordered_blocks.extend(
            block_index
            for block_index in natural_order
            if block_index not in ordered_blocks
        )
    preserve_sequence = ordered_blocks == natural_order
    pattern = embroidery.EmbPattern()
    active_block: int | None = None
    previous_point: tuple[float, float] | None = None
    written_blocks: list[dict] = []
    if stitch_perimeter:
        segments = add_perimeter_segments(
            segments,
            color_blocks,
            max_stitch_mm=max_stitch_mm,
            offset_mm=perimeter_offset_mm,
            passes=perimeter_passes,
        )

    def ensure_active_block(block_index: int) -> None:
        nonlocal active_block
        if block_index == active_block:
            return
        block = block_by_index[block_index]
        color = color_overrides.get(block_index, block["color"]) if color_overrides else block["color"]
        normalized_color = normalize_hex(color)
        pattern.add_thread(make_thread(normalized_color))
        label = ""
        if thread_label_overrides:
            label = thread_label_overrides.get(block_index, "")
        label = label or block.get("label", "") or f"Thread {normalized_color}"
        written_blocks.append(
            {
                "color": normalized_color,
                "label": label,
                "source_block": block_index,
            }
        )
        if active_block is not None:
            pattern.color_change()
        active_block = block_index

    def write_run(block_index: int, points: list[tuple[float, float]], is_perimeter: bool = False) -> None:
        nonlocal active_block, previous_point
        clean_points = clean_run_points(
            points,
            min_stitch_mm=min(min_stitch_mm, 0.30) if is_perimeter else min_stitch_mm,
            max_stitch_mm=max_stitch_mm,
            lock_stitch_mm=lock_stitch_mm,
            min_run_length_mm=min(min_run_length_mm, 0.30) if is_perimeter else min_run_length_mm,
            include_start_lock=False,
        )
        if len(clean_points) < 2:
            return
        rounded_points: list[tuple[int, int]] = []
        for point in clean_points:
            rounded = to_embroidery_units(point)
            if not rounded_points or rounded_points[-1] != rounded:
                rounded_points.append(rounded)
        if len(rounded_points) < 2:
            return
        old_active_block = active_block
        ensure_active_block(block_index)
        start_units = rounded_points[0]
        current_units: tuple[int, int] | None = None
        start_needs_tie_in = old_active_block != block_index or previous_point is None

        def emit_tie_in() -> None:
            nonlocal current_units
            if len(rounded_points) < 2:
                return
            next_units = rounded_points[1]
            dx = next_units[0] - start_units[0]
            dy = next_units[1] - start_units[1]
            span = math.hypot(dx, dy)
            min_units = max(1.0, min_stitch_mm * EMB_UNITS_PER_MM)
            if span < min_units:
                return
            lock_units = min(lock_stitch_mm * EMB_UNITS_PER_MM, span)
            lock_x = int(round(start_units[0] + (dx / span) * lock_units))
            lock_y = int(round(start_units[1] + (dy / span) * lock_units))
            lock_units_point = (lock_x, lock_y)
            if lock_units_point == start_units:
                return
            for tie_point in (lock_units_point, start_units, lock_units_point, start_units):
                if current_units == tie_point:
                    continue
                pattern.add_stitch_absolute(embroidery.STITCH, tie_point[0], tie_point[1])
                current_units = tie_point

        if previous_point is None or to_embroidery_units(previous_point) != start_units:
            start_needs_tie_in = True
            if (
                connect_short_gaps
                and
                previous_point is not None
                and old_active_block == block_index
                and point_distance(
                    previous_point,
                    (start_units[0] / EMB_UNITS_PER_MM, start_units[1] / EMB_UNITS_PER_MM),
                ) <= max(max_connect_gap_mm, min_stitch_mm)
            ):
                pattern.add_stitch_absolute(embroidery.STITCH, start_units[0], start_units[1])
                current_units = start_units
            elif previous_point is None:
                if old_active_block is None:
                    pattern.add_stitch_absolute(embroidery.STITCH, start_units[0], start_units[1])
                else:
                    pattern.add_stitch_absolute(embroidery.JUMP, start_units[0], start_units[1])
                    pattern.add_stitch_absolute(embroidery.STITCH, start_units[0], start_units[1])
                current_units = start_units
            else:
                pattern.add_stitch_absolute(embroidery.JUMP, start_units[0], start_units[1])
                pattern.add_stitch_absolute(embroidery.STITCH, start_units[0], start_units[1])
                current_units = start_units
        else:
            current_units = start_units
        if start_needs_tie_in and current_units == start_units:
            emit_tie_in()
        for x, y in rounded_points[1:]:
            if current_units == (x, y):
                continue
            pattern.add_stitch_absolute(embroidery.STITCH, x, y)
            current_units = (x, y)
        if current_units is None or current_units == start_units:
            return
        previous_point = (current_units[0] / EMB_UNITS_PER_MM, current_units[1] / EMB_UNITS_PER_MM)

    def write_segment_sequence(sequence: list[dict]) -> None:
        current_block: int | None = None
        current_perimeter = False
        current_perimeter_loop: int | None = None
        run_points: list[tuple[float, float]] = []

        def flush() -> None:
            nonlocal run_points, previous_point
            if current_block is not None and run_points:
                write_run(current_block, run_points, current_perimeter)
                if current_perimeter:
                    previous_point = None
            run_points = []

        for segment in sequence:
            nonlocal_current_perimeter = bool(segment.get("perimeter"))
            nonlocal_perimeter_loop = segment.get("perimeterLoop") if nonlocal_current_perimeter else None
            block_index = segment["blockIndex"]
            start = (segment["x1"], segment["y1"])
            end = (segment["x2"], segment["y2"])
            if (
                current_block != block_index
                or current_perimeter != nonlocal_current_perimeter
                or current_perimeter_loop != nonlocal_perimeter_loop
                or not run_points
                or not points_match(run_points[-1], start)
            ):
                flush()
                current_block = block_index
                current_perimeter = nonlocal_current_perimeter
                current_perimeter_loop = nonlocal_perimeter_loop
                run_points = [start, end]
            else:
                run_points.append(end)
        flush()

    if preserve_sequence:
        write_segment_sequence(
            [
                segment
                for segment in segments
                if segment["kind"] == "stitch" and segment["blockIndex"] in selected_blocks
            ]
        )
    else:
        for ordered_block in ordered_blocks:
            write_segment_sequence(
                [
                    segment
                    for segment in segments
                    if segment["kind"] == "stitch" and segment["blockIndex"] == ordered_block
                ]
            )

    if pattern.count_stitches() == 0:
        raise ValueError("No selected stitches to write.")
    pattern.end()
    pattern.write(
        str(output_file),
        max_jump=int(5.0 * EMB_UNITS_PER_MM),
        full_jump=True,
        tie_on=True,
        tie_off=True,
    )
    return written_blocks


def write_filtered_pes(
    input_file: Path,
    output_file: Path,
    selected_blocks: set[int],
    color_order: list[int] | None = None,
    color_overrides: dict[int, str] | None = None,
    thread_label_overrides: dict[int, str] | None = None,
    *,
    fit_width_mm: float | None = None,
    fit_height_mm: float | None = None,
    sample_step_mm: float = 0.8,
    fill_mode: str = "tatami",
    fill_angle_deg: float = 45.0,
    fill_spacing_mm: float = 0.5,
    thread_weight: str = DEFAULT_THREAD_WEIGHT,
    max_stitch_mm: float = 3.0,
    max_colors: int = 6,
    color_merge_distance: float = 56.0,
    pdf_page: int = 1,
    pdf_dpi: int = 180,
    center: bool = True,
    stitch_perimeter: bool = False,
) -> None:
    if input_file.suffix.lower() == ".svg" and not svg_needs_rasterization(input_file):
        segments, _, color_blocks, _ = collect_svg_segments(
            input_file,
            sample_step_mm=sample_step_mm,
            fill_spacing_mm=fill_spacing_mm,
            max_stitch_mm=max_stitch_mm,
            fill_angle_deg=fill_angle_deg,
            fill_mode=fill_mode,
            fit_width_mm=fit_width_mm,
            fit_height_mm=fit_height_mm,
            center=center,
        )
    elif is_raster_source(input_file) or svg_needs_rasterization(input_file):
        raster_fit_width = fit_width_mm if fit_width_mm is not None else 90.0
        segments, _, color_blocks, _ = image_to_segments(
            input_file,
            fit_width_mm=raster_fit_width,
            fit_height_mm=fit_height_mm,
            max_colors=max_colors,
            fill_mode=fill_mode,
            fill_angle_deg=fill_angle_deg,
            color_merge_distance=color_merge_distance,
            fill_spacing_mm=fill_spacing_mm,
            max_stitch_mm=max_stitch_mm,
            pdf_page=pdf_page,
            pdf_dpi=pdf_dpi,
        )
    else:
        pattern = embroidery.read(str(input_file))
        if pattern is None:
            raise ValueError(f"Could not read embroidery file: {input_file}")
        segments, _, color_blocks, _ = collect_segments(pattern)
    if color_overrides:
        for block in color_blocks:
            block_index = block["index"]
            if block_index in color_overrides:
                block["color"] = color_overrides[block_index]
        block_colors = {block["index"]: block["color"] for block in color_blocks}
        for segment in segments:
            if segment["blockIndex"] in block_colors:
                segment["color"] = block_colors[segment["blockIndex"]]
    written_blocks = write_segments_as_pes(
        segments,
        color_blocks,
        output_file,
        selected_blocks,
        color_order,
        color_overrides,
        thread_label_overrides,
        max_stitch_mm=max_stitch_mm,
        stitch_perimeter=stitch_perimeter,
    )
    thread_metadata_path(output_file).write_text(
        json.dumps({"blocks": written_blocks}, indent=2),
        encoding="utf-8",
    )


def write_svg_as_pes(
    input_file: Path,
    output_file: Path,
    *,
    fit_width_mm: float | None = None,
    fit_height_mm: float | None = None,
    sample_step_mm: float = 0.8,
    fill_mode: str = "tatami",
    fill_angle_deg: float = 45.0,
    fill_spacing_mm: float = 0.5,
    thread_weight: str = DEFAULT_THREAD_WEIGHT,
    max_stitch_mm: float = 3.0,
    max_colors: int = 6,
    color_merge_distance: float = 56.0,
    pdf_page: int = 1,
    pdf_dpi: int = 180,
    center: bool = True,
    stitch_perimeter: bool = False,
) -> None:
    if svg_needs_rasterization(input_file):
        raster_fit_width = fit_width_mm if fit_width_mm is not None else 90.0
        write_image_as_pes(
            input_file,
            output_file,
            fit_width_mm=raster_fit_width,
            fit_height_mm=fit_height_mm,
            max_colors=max_colors,
            fill_mode=fill_mode,
            fill_angle_deg=fill_angle_deg,
            color_merge_distance=color_merge_distance,
            fill_spacing_mm=fill_spacing_mm,
            max_stitch_mm=max_stitch_mm,
            pdf_page=pdf_page,
            pdf_dpi=pdf_dpi,
        )
        return
    segments, _, color_blocks, _ = collect_svg_segments(
        input_file,
        sample_step_mm=sample_step_mm,
        fill_spacing_mm=fill_spacing_mm,
        max_stitch_mm=max_stitch_mm,
        fill_angle_deg=fill_angle_deg,
        fill_mode=fill_mode,
        fit_width_mm=fit_width_mm,
        fit_height_mm=fit_height_mm,
        center=center,
    )
    write_segments_as_pes(
        segments,
        color_blocks,
        output_file,
        max_stitch_mm=max_stitch_mm,
        stitch_perimeter=stitch_perimeter,
    )


def write_image_as_pes(source_file: Path, output_file: Path, **settings) -> None:
    settings.pop("thread_weight", None)
    max_stitch_mm = float(settings.get("max_stitch_mm", 3.0))
    stitch_perimeter = bool(settings.pop("stitch_perimeter", False))
    segments, _, color_blocks, _ = image_to_segments(source_file, **settings)
    write_segments_as_pes(
        segments,
        color_blocks,
        output_file,
        max_stitch_mm=max_stitch_mm,
        stitch_perimeter=stitch_perimeter,
    )


def main() -> int:
    args = parse_args()
    input_file = args.input
    if not input_file.exists():
        raise SystemExit(f"Input file not found: {input_file}")

    output = args.output or input_file.with_suffix(".html")
    is_convertible_input = input_file.suffix.lower() == ".svg" or is_raster_source(input_file)
    pes_output = None
    pes_href = None
    if is_convertible_input:
        pes_output = args.pes_output or output.with_suffix(".pes")
        if pes_output.parent.resolve() == output.parent.resolve():
            pes_href = pes_output.name
        else:
            pes_href = pes_output.resolve().as_uri()

    try:
        html_text, design_box, counts = build_viewer_html(
            input_file,
            fit_width_mm=args.fit_width_mm,
            fit_height_mm=args.fit_height_mm,
            sample_step_mm=args.sample_step_mm,
            fill_mode=args.fill_mode,
            fill_angle_deg=args.fill_angle_deg,
            fill_spacing_mm=args.fill_spacing_mm,
            thread_weight=args.thread_weight,
            max_stitch_mm=args.max_stitch_mm,
            max_colors=args.max_colors,
            color_merge_distance=args.color_merge_distance,
            pdf_page=args.pdf_page,
            pdf_dpi=args.pdf_dpi,
            center=not args.no_center,
            pes_href=pes_href,
        )
    except ValueError as error:
        raise SystemExit(str(error)) from error

    if pes_output is not None:
        if input_file.suffix.lower() == ".svg":
            write_svg_as_pes(
                input_file,
                pes_output,
                fit_width_mm=args.fit_width_mm,
                fit_height_mm=args.fit_height_mm,
                sample_step_mm=args.sample_step_mm,
                fill_mode=args.fill_mode,
                fill_angle_deg=args.fill_angle_deg,
                fill_spacing_mm=args.fill_spacing_mm,
                thread_weight=args.thread_weight,
                max_stitch_mm=args.max_stitch_mm,
                max_colors=args.max_colors,
                color_merge_distance=args.color_merge_distance,
                pdf_page=args.pdf_page,
                pdf_dpi=args.pdf_dpi,
                center=not args.no_center,
            )
        else:
            raster_fit_width = args.fit_width_mm if args.fit_width_mm is not None else 90.0
            write_image_as_pes(
                input_file,
                pes_output,
                fit_width_mm=raster_fit_width,
                fit_height_mm=args.fit_height_mm,
                max_colors=args.max_colors,
                color_merge_distance=args.color_merge_distance,
                fill_spacing_mm=args.fill_spacing_mm,
                thread_weight=args.thread_weight,
                max_stitch_mm=args.max_stitch_mm,
                pdf_page=args.pdf_page,
                pdf_dpi=args.pdf_dpi,
            )

    output.write_text(html_text, encoding="utf-8")
    min_x, min_y, max_x, max_y = design_box
    print(f"Wrote {output}")
    if pes_output is not None:
        print(f"Wrote {pes_output}")
    print(f"Design size: {max_x - min_x:.1f} x {max_y - min_y:.1f} mm")
    print(f"Rendered needle points: {counts['needle_points']}")
    print(f"Rendered stitch segments: {counts['stitch_segments']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
