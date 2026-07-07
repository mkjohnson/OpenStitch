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


def to_mm(points_px: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    return [(x / SVG_PX_PER_MM, y / SVG_PX_PER_MM) for x, y in points_px]


def split_long_stitches(
    points: Iterable[tuple[float, float]], max_stitch_mm: float
) -> list[tuple[float, float]]:
    source = list(points)
    if len(source) < 2:
        return source
    result = [source[0]]
    for start, end in zip(source, source[1:]):
        span = distance(start, end)
        steps = max(1, int(math.ceil(span / max(max_stitch_mm, 0.1))))
        for i in range(1, steps + 1):
            t = i / steps
            result.append(
                (start[0] + (end[0] - start[0]) * t, start[1] + (end[1] - start[1]) * t)
            )
    return result


def hatch_fill(
    polygon: list[tuple[float, float]], spacing_mm: float, max_stitch_mm: float
) -> list[list[tuple[float, float]]]:
    if len(polygon) < 3:
        return []
    if distance(polygon[0], polygon[-1]) > 0.01:
        polygon = [*polygon, polygon[0]]

    min_y = min(y for _, y in polygon)
    max_y = max(y for _, y in polygon)
    rows: list[list[tuple[float, float]]] = []
    y = min_y
    row_index = 0

    while y <= max_y:
        xs: list[float] = []
        for (x1, y1), (x2, y2) in zip(polygon, polygon[1:]):
            if abs(y1 - y2) < 1e-9:
                continue
            crosses = (y1 <= y < y2) or (y2 <= y < y1)
            if crosses:
                t = (y - y1) / (y2 - y1)
                xs.append(x1 + (x2 - x1) * t)

        xs.sort()
        for left, right in zip(xs[0::2], xs[1::2]):
            if right - left <= 0.01:
                continue
            run = [(left, y), (right, y)]
            if row_index % 2:
                run.reverse()
            rows.append(split_long_stitches(run, max_stitch_mm))
            row_index += 1
        y += max(spacing_mm, 0.1)

    return rows


def extract_runs(
    svg_file: Path,
    sample_step_mm: float,
    fill_spacing_mm: float,
    max_stitch_mm: float,
) -> list[StitchRun]:
    svg = SVG.parse(str(svg_file), reify=True)
    sample_step_px = sample_step_mm * SVG_PX_PER_MM
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

        for subpath_px in flatten_path(path, sample_step_px):
            subpath_mm = to_mm(subpath_px)
            if filled and len(subpath_mm) >= 3:
                for row in hatch_fill(subpath_mm, fill_spacing_mm, max_stitch_mm):
                    runs.append(StitchRun(color=color, points_mm=row))
            else:
                runs.append(
                    StitchRun(
                        color=color,
                        points_mm=split_long_stitches(subpath_mm, max_stitch_mm),
                    )
                )

    return runs


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
    runs = extract_runs(
        input_file,
        sample_step_mm=args.sample_step_mm,
        fill_spacing_mm=args.fill_spacing_mm,
        max_stitch_mm=args.max_stitch_mm,
    )
    runs = transform_runs(
        runs,
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
