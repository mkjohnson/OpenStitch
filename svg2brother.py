from __future__ import annotations

import argparse
import math
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
) -> list[list[tuple[float, float]]]:
    remaining = [list(run) for run in runs if len(run) >= 2]
    if not remaining:
        return []
    routed: list[list[tuple[float, float]]] = []
    current = start
    while remaining:
        best_index = 0
        best_reverse = False
        best_distance = float("inf")
        if current is None:
            best_run = min(
                range(len(remaining)),
                key=lambda index: (
                    min(point[1] for point in remaining[index]),
                    min(point[0] for point in remaining[index]),
                ),
            )
            run = remaining.pop(best_run)
            routed.append(run)
            current = run[-1]
            continue
        for index, run in enumerate(remaining):
            forward_distance = distance(current, run[0])
            reverse_distance = distance(current, run[-1])
            if forward_distance < best_distance:
                best_index = index
                best_reverse = False
                best_distance = forward_distance
            if reverse_distance < best_distance:
                best_index = index
                best_reverse = True
                best_distance = reverse_distance
        run = remaining.pop(best_index)
        if best_reverse:
            run.reverse()
        routed.append(run)
        current = run[-1]
    return routed


def route_stitch_runs(runs: Iterable[StitchRun]) -> list[StitchRun]:
    grouped: dict[str, list[list[tuple[float, float]]]] = {}
    color_order: list[str] = []
    for run in runs:
        if len(run.points_mm) < 2:
            continue
        if run.color not in grouped:
            grouped[run.color] = []
            color_order.append(run.color)
        grouped[run.color].append(run.points_mm)

    routed: list[StitchRun] = []
    current: tuple[float, float] | None = None
    for color in color_order:
        for points in route_point_runs(grouped[color], start=current):
            routed.append(StitchRun(color=color, points_mm=points))
            current = points[-1]
    return routed


def to_mm(points_px: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    return [(x / SVG_PX_PER_MM, y / SVG_PX_PER_MM) for x, y in points_px]


MIN_FILL_STITCH_MM = 0.3
MIN_DETAIL_RUN_MM = 0.15


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
            if abs(angle_rad) > 1e-6:
                row = [rotate_point(point, angle_rad, origin) for point in row]
            rows.append(row)
            row_index += 1
        y += max(spacing_mm, 0.1)

    return rows


def stitch_micro_score(
    runs: Iterable[list[tuple[float, float]]],
    min_stitch_mm: float = MIN_FILL_STITCH_MM,
) -> tuple[int, float, int]:
    short_count = 0
    deficit = 0.0
    stitch_count = 0
    for run in runs:
        for start, end in zip(run, run[1:]):
            span = distance(start, end)
            if span <= 0:
                continue
            stitch_count += 1
            if span < min_stitch_mm:
                short_count += 1
                deficit += min_stitch_mm - span
    return short_count, deficit, stitch_count


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
    best_score: tuple[int, float, int, float] | None = None
    for angle in fill_angle_candidates(fill_angle_deg):
        rows = hatch_compound_fill(
            polygon_list,
            spacing_mm,
            max_stitch_mm,
            angle,
            min_stitch_mm=min_stitch_mm,
        )
        short_count, deficit, stitch_count = stitch_micro_score(rows, min_stitch_mm)
        score = (short_count, round(deficit, 4), -stitch_count, abs(angle - fill_angle_deg))
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
    plans: list[tuple[str, list[list[tuple[float, float]]]]] = [
        (
            "tatami",
            optimized_hatch_compound_fill(
                polygon_list,
                spacing_mm,
                max_stitch_mm,
                fill_angle_deg,
                min_stitch_mm=min_stitch_mm,
            ),
        ),
        (
            "horizontal",
            hatch_compound_fill(
                polygon_list,
                spacing_mm,
                max_stitch_mm,
                0.0,
                min_stitch_mm=min_stitch_mm,
            ),
        ),
    ]
    crosshatch_rows = optimized_hatch_compound_fill(
        polygon_list,
        spacing_mm * 1.4,
        max_stitch_mm,
        fill_angle_deg,
        min_stitch_mm=min_stitch_mm,
    )
    crosshatch_rows.extend(
        optimized_hatch_compound_fill(
            polygon_list,
            spacing_mm * 1.4,
            max_stitch_mm,
            -fill_angle_deg if abs(fill_angle_deg) > 0.001 else 90.0,
            min_stitch_mm=min_stitch_mm,
        )
    )
    plans.append(("crosshatch", crosshatch_rows))

    best_rows: list[list[tuple[float, float]]] | None = None
    best_score: tuple[int, float, int, int] | None = None
    for plan_index, (_, rows) in enumerate(plans):
        short_count, deficit, stitch_count = stitch_micro_score(rows, min_stitch_mm)
        score = (short_count, round(deficit, 4), stitch_count, plan_index)
        if best_score is None or score < best_score:
            best_score = score
            best_rows = rows
    return best_rows or []


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


def extract_runs(
    svg_file: Path,
    sample_step_mm: float,
    fill_spacing_mm: float,
    max_stitch_mm: float,
    fill_angle_deg: float = 0.0,
    fill_mode: str = "tatami",
    min_stitch_mm: float = MIN_FILL_STITCH_MM,
) -> list[StitchRun]:
    svg = SVG.parse(str(svg_file), reify=True)
    sample_step_px = sample_step_mm * SVG_PX_PER_MM
    fill_mode = fill_mode.lower().strip()
    if fill_mode not in {"horizontal", "tatami", "crosshatch", "mixed", "outline", "contour"}:
        fill_mode = "tatami"
    fill_angles = [0.0] if fill_mode == "horizontal" else [fill_angle_deg]
    if fill_mode == "crosshatch":
        fill_angles.append(-fill_angle_deg if abs(fill_angle_deg) > 0.001 else 90.0)
    runs: list[StitchRun] = []

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
            if fill_mode == "mixed":
                fill_rows = mixed_hatch_compound_fill(
                    fill_polygons,
                    fill_spacing_mm,
                    max_stitch_mm,
                    fill_angle_deg,
                    min_stitch_mm=min_stitch_mm,
                )
                for row in fill_rows:
                    runs.append(StitchRun(color=color, points_mm=row))
            else:
                for angle in fill_angles:
                    fill_rows = optimized_hatch_compound_fill(
                        fill_polygons,
                        fill_spacing_mm,
                        max_stitch_mm,
                        angle,
                        min_stitch_mm=min_stitch_mm,
                    )
                    for row in fill_rows:
                        runs.append(StitchRun(color=color, points_mm=row))
        elif filled and fill_mode == "contour":
            fill_polygons = [subpath_mm for subpath_mm in subpaths_mm if len(subpath_mm) >= 3]
            for row in contour_compound_fill(
                fill_polygons,
                fill_spacing_mm,
                max_stitch_mm,
                min_stitch_mm=min_stitch_mm,
            ):
                runs.append(StitchRun(color=color, points_mm=row))
        else:
            for subpath_mm in subpaths_mm:
                runs.append(
                    StitchRun(
                        color=color,
                        points_mm=split_long_stitches(
                            subpath_mm,
                            max_stitch_mm,
                            min_stitch_mm=min_stitch_mm,
                        ),
                    )
                )

    return route_stitch_runs(runs)


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
        transformed.append(StitchRun(color=run.color, points_mm=points))
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
) -> list[StitchRun]:
    runs = extract_runs(
        svg_file,
        sample_step_mm=sample_step_mm,
        fill_spacing_mm=fill_spacing_mm,
        max_stitch_mm=max_stitch_mm,
        fill_angle_deg=fill_angle_deg,
        fill_mode=fill_mode,
        min_stitch_mm=MIN_FILL_STITCH_MM,
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
            min_stitch_mm=max(MIN_FILL_STITCH_MM / scale, 0.01),
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

    for run in runs:
        if len(run.points_mm) < 2:
            continue
        if run.color != active_color:
            pattern.add_thread(make_thread(run.color))
            if active_color is not None:
                pattern.color_change()
            active_color = run.color

        first_x, first_y = run.points_mm[0]
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
        default=0.5,
        help="Distance between hatch-fill rows in millimeters; default: 0.5",
    )
    parser.add_argument(
        "--max-stitch-mm",
        type=positive_float,
        default=3.0,
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
        choices=("mixed", "contour", "tatami", "horizontal", "crosshatch", "outline"),
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
