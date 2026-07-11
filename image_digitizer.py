from __future__ import annotations

import math
import base64
import colorsys
import io
import re
import tempfile
from pathlib import Path

import fitz
import pyembroidery as embroidery
from PIL import Image, ImageChops, ImageOps

from svg2brother import EMB_UNITS_PER_MM, make_thread
from thread_inventory import hex_to_rgb, load_inventory


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff", ".webp"}
PDF_SUFFIXES = {".pdf"}
RASTER_SUFFIXES = IMAGE_SUFFIXES | PDF_SUFFIXES


def planned_run_bounds(run: tuple[list[tuple[float, float]], int]) -> tuple[float, float, float, float]:
    points, _ = run
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    return min(xs), min(ys), max(xs), max(ys)


def planned_bounds_gap(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    dx = max(first[0] - second[2], second[0] - first[2], 0.0)
    dy = max(first[1] - second[3], second[1] - first[3], 0.0)
    return math.hypot(dx, dy)


def connected_planned_run_groups(
    runs: list[tuple[list[tuple[float, float]], int]],
    gap_mm: float = 1.5,
) -> list[list[tuple[list[tuple[float, float]], int]]]:
    if len(runs) <= 1:
        return [runs] if runs else []
    parents = list(range(len(runs)))
    bounds_list = [planned_run_bounds(run) for run in runs]
    order = sorted(range(len(runs)), key=lambda index: bounds_list[index][0])

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
            if right_bounds[0] > left_bounds[2] + gap_mm:
                break
            if planned_bounds_gap(left_bounds, right_bounds) <= gap_mm:
                union(left_index, right_index)

    grouped: dict[int, list[tuple[list[tuple[float, float]], int]]] = {}
    first_seen: dict[int, int] = {}
    for index, run in enumerate(runs):
        root = find(index)
        grouped.setdefault(root, []).append(run)
        first_seen.setdefault(root, index)
    return [grouped[root] for root in sorted(grouped, key=lambda root: first_seen[root])]


def boundary_pixels(
    active: set[tuple[int, int]],
    width: int,
    height: int,
) -> set[tuple[int, int]]:
    return {
        (col, row)
        for col, row in active
        if (
            col == 0
            or row == 0
            or col == width - 1
            or row == height - 1
            or (col - 1, row) not in active
            or (col + 1, row) not in active
            or (col, row - 1) not in active
            or (col, row + 1) not in active
        )
    }


def boundary_loops_from_active(active: set[tuple[int, int]]) -> list[list[tuple[int, int]]]:
    edges: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    adjacency: dict[tuple[int, int], set[tuple[int, int]]] = {}

    def add_edge(a: tuple[int, int], b: tuple[int, int]) -> None:
        edge = (a, b) if a <= b else (b, a)
        if edge in edges:
            return
        edges.add(edge)
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)

    for col, row in active:
        if (col - 1, row) not in active:
            add_edge((col, row), (col, row + 1))
        if (col + 1, row) not in active:
            add_edge((col + 1, row), (col + 1, row + 1))
        if (col, row - 1) not in active:
            add_edge((col, row), (col + 1, row))
        if (col, row + 1) not in active:
            add_edge((col, row + 1), (col + 1, row + 1))

    def remove_edge(a: tuple[int, int], b: tuple[int, int]) -> None:
        edge = (a, b) if a <= b else (b, a)
        edges.discard(edge)
        if a in adjacency:
            adjacency[a].discard(b)
        if b in adjacency:
            adjacency[b].discard(a)

    loops: list[list[tuple[int, int]]] = []
    while edges:
        start, current = min(edges, key=lambda edge: (edge[0][1], edge[0][0], edge[1][1], edge[1][0]))
        remove_edge(start, current)
        loop = [start, current]
        previous = start
        while current != start:
            candidates = list(adjacency.get(current, ()))
            if not candidates:
                break
            # Prefer continuing through the current boundary rather than doubling
            # back. At 4-way pixel-corner contacts, the deterministic angle order
            # keeps separate contours from being stitched as one long zigzag.
            in_dx = current[0] - previous[0]
            in_dy = current[1] - previous[1]

            def turn_score(candidate: tuple[int, int]) -> tuple[int, int, int]:
                out_dx = candidate[0] - current[0]
                out_dy = candidate[1] - current[1]
                backtrack = 1 if candidate == previous and len(candidates) > 1 else 0
                cross = in_dx * out_dy - in_dy * out_dx
                dot = in_dx * out_dx + in_dy * out_dy
                return backtrack, 0 if cross <= 0 else 1, -dot

            next_point = min(candidates, key=turn_score)
            remove_edge(current, next_point)
            previous, current = current, next_point
            loop.append(current)
            if len(loop) > 200000:
                break
        if len(loop) >= 3:
            loops.append(loop)
    loops.sort(key=len, reverse=True)
    return loops


def simplify_grid_path(points: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if len(points) <= 2:
        return points
    closed = points[0] == points[-1]
    source = points[:-1] if closed else points
    simplified: list[tuple[int, int]] = []
    for point in source:
        simplified.append(point)
        while len(simplified) >= 3:
            a, b, c = simplified[-3], simplified[-2], simplified[-1]
            if (b[0] - a[0]) * (c[1] - b[1]) == (b[1] - a[1]) * (c[0] - b[0]):
                simplified.pop(-2)
            else:
                break
    if closed and simplified:
        simplified.append(simplified[0])
    return simplified


def connected_pixel_components(active: set[tuple[int, int]]) -> list[set[tuple[int, int]]]:
    remaining = set(active)
    components: list[set[tuple[int, int]]] = []
    neighbors = [
        (dx, dy)
        for dy in (-1, 0, 1)
        for dx in (-1, 0, 1)
        if dx != 0 or dy != 0
    ]
    while remaining:
        start = remaining.pop()
        component = {start}
        stack = [start]
        while stack:
            col, row = stack.pop()
            for dx, dy in neighbors:
                neighbor = (col + dx, row + dy)
                if neighbor in remaining:
                    remaining.remove(neighbor)
                    component.add(neighbor)
                    stack.append(neighbor)
        components.append(component)
    components.sort(key=len, reverse=True)
    return components


def component_centroid(component: set[tuple[int, int]]) -> tuple[float, float]:
    if not component:
        return 0.0, 0.0
    total_x = sum(col for col, _ in component)
    total_y = sum(row for _, row in component)
    return total_x / len(component), total_y / len(component)


def route_planned_runs(
    runs: list[tuple[list[tuple[float, float]], int]],
    start: tuple[float, float] | None = None,
    mode: str = "min_cuts",
) -> list[tuple[list[tuple[float, float]], int]]:
    ordered = [(list(points), row_index) for points, row_index in runs if len(points) >= 2]
    if mode == "fast":
        return ordered
    if mode == "clean_top":
        routed: list[tuple[list[tuple[float, float]], int]] = []
        current = start
        for points, row_index in ordered:
            if current is not None:
                forward = math.hypot(current[0] - points[0][0], current[1] - points[0][1])
                reverse = math.hypot(current[0] - points[-1][0], current[1] - points[-1][1])
                if reverse < forward:
                    points.reverse()
            routed.append((points, row_index))
            current = points[-1]
        return routed

    routed: list[tuple[list[tuple[float, float]], int]] = []
    current = start
    remaining_groups = connected_planned_run_groups(ordered)
    while remaining_groups:
        if current is None:
            group = remaining_groups.pop(0)
        else:
            best_group_index = min(
                range(len(remaining_groups)),
                key=lambda index: min(
                    min(
                        math.hypot(current[0] - points[0][0], current[1] - points[0][1]),
                        math.hypot(current[0] - points[-1][0], current[1] - points[-1][1]),
                    )
                    for points, _ in remaining_groups[index]
                ),
            )
            group = remaining_groups.pop(best_group_index)
        remaining = group
        while remaining:
            if current is None:
                points, row_index = remaining.pop(0)
            else:
                best_index = 0
                best_reversed = False
                best_distance = float("inf")
                for index, (candidate, _) in enumerate(remaining):
                    forward = math.hypot(current[0] - candidate[0][0], current[1] - candidate[0][1])
                    reverse = math.hypot(current[0] - candidate[-1][0], current[1] - candidate[-1][1])
                    if forward < best_distance:
                        best_index = index
                        best_reversed = False
                        best_distance = forward
                    if reverse < best_distance:
                        best_index = index
                        best_reversed = True
                        best_distance = reverse
                points, row_index = remaining.pop(best_index)
                if best_reversed:
                    points.reverse()
            routed.append((points, row_index))
            current = points[-1]
    return routed


def is_raster_source(path: Path) -> bool:
    return path.suffix.lower() in RASTER_SUFFIXES


def svg_needs_rasterization(path: Path) -> bool:
    if path.suffix.lower() != ".svg":
        return False
    text = path.read_text(encoding="utf-8", errors="ignore").lower()
    return "<image" in text or "<pattern" in text or "url(#" in text


def render_pdf_page(pdf_file: Path, page_index: int = 0, dpi: int = 180) -> Image.Image:
    document = fitz.open(str(pdf_file))
    if page_index < 0 or page_index >= len(document):
        raise ValueError(f"PDF page must be between 1 and {len(document)}.")
    page = document[page_index]
    matrix = fitz.Matrix(dpi / 72, dpi / 72)
    pixmap = page.get_pixmap(matrix=matrix, alpha=True)
    mode = "RGBA" if pixmap.alpha else "RGB"
    image = Image.frombytes(mode, (pixmap.width, pixmap.height), pixmap.samples)
    document.close()
    return image.convert("RGBA")


def render_svg_file(svg_file: Path, dpi: int = 180) -> Image.Image:
    document = fitz.open(str(svg_file))
    page = document[0]
    matrix = fitz.Matrix(dpi / 72, dpi / 72)
    pixmap = page.get_pixmap(matrix=matrix, alpha=True)
    mode = "RGBA" if pixmap.alpha else "RGB"
    image = Image.frombytes(mode, (pixmap.width, pixmap.height), pixmap.samples)
    document.close()
    return image.convert("RGBA")


def extract_first_embedded_svg_image(svg_file: Path) -> Image.Image | None:
    text = svg_file.read_text(encoding="utf-8", errors="ignore")
    match = re.search(
        r"data:image/(?:png|jpeg|jpg|webp);base64,([^\"')\s]+)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    data = base64.b64decode(match.group(1))
    return Image.open(io.BytesIO(data)).convert("RGBA")


def load_raster_source(path: Path, pdf_page: int = 1, pdf_dpi: int = 180) -> Image.Image:
    if path.suffix.lower() == ".pdf":
        return render_pdf_page(path, page_index=pdf_page - 1, dpi=pdf_dpi)
    if path.suffix.lower() == ".svg":
        embedded = extract_first_embedded_svg_image(path)
        if embedded is not None:
            return embedded
        return render_svg_file(path, dpi=pdf_dpi)
    return Image.open(path).convert("RGBA")


def trim_transparent_or_white(image: Image.Image, tolerance: int = 8) -> Image.Image:
    rgba = image.convert("RGBA")
    alpha_bbox = rgba.getchannel("A").getbbox()
    if alpha_bbox:
        rgba = rgba.crop(alpha_bbox)
    background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    diff = ImageChops.difference(rgba, background).convert("L")
    mask = diff.point(lambda value: 255 if value > tolerance else 0)
    bbox = mask.getbbox()
    return rgba.crop(bbox) if bbox else rgba


def resize_for_fit(image: Image.Image, fit_width_mm: float | None, fit_height_mm: float | None, px_per_mm: float) -> Image.Image:
    if fit_width_mm:
        target_width_px = max(1, int(round(fit_width_mm * px_per_mm)))
        scale = target_width_px / image.width
    elif fit_height_mm:
        target_height_px = max(1, int(round(fit_height_mm * px_per_mm)))
        scale = target_height_px / image.height
    else:
        return image
    target = (max(1, int(round(image.width * scale))), max(1, int(round(image.height * scale))))
    return image.resize(target, Image.Resampling.LANCZOS)


def color_to_hex(color: tuple[int, int, int]) -> str:
    return f"#{color[0]:02x}{color[1]:02x}{color[2]:02x}"


def is_background_color(color: tuple[int, int, int], threshold: int) -> bool:
    return min(color) >= threshold


def color_distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    return math.sqrt(
        (a[0] - b[0]) ** 2
        + (a[1] - b[1]) ** 2
        + (a[2] - b[2]) ** 2
    )


def colors_can_merge(a: tuple[int, int, int], b: tuple[int, int, int], merge_distance: float) -> bool:
    if color_distance(a, b) > merge_distance:
        return False
    a_lightness = max(a)
    b_lightness = max(b)
    if a_lightness < 55 and b_lightness < 55:
        return True
    ah, al, asat = colorsys.rgb_to_hls(a[0] / 255, a[1] / 255, a[2] / 255)
    bh, bl, bsat = colorsys.rgb_to_hls(b[0] / 255, b[1] / 255, b[2] / 255)
    if asat < 0.16 or bsat < 0.16:
        return abs(al - bl) <= 0.18
    hue_delta = abs(ah - bh)
    hue_delta = min(hue_delta, 1 - hue_delta)
    return hue_delta <= 0.08


def merge_similar_palette_colors(
    palette_image: Image.Image,
    colors: list[tuple[int, int, int]],
    merge_distance: float,
) -> tuple[Image.Image, list[tuple[int, int, int]]]:
    if merge_distance <= 0:
        return palette_image, colors

    counts = palette_image.histogram()
    used_indices = [
        index
        for index, color in enumerate(colors)
        if index < len(counts) and counts[index] > 0
    ]
    used_indices.sort(key=lambda index: counts[index], reverse=True)
    if not used_indices:
        return palette_image, colors

    groups: list[dict] = []
    old_to_new: dict[int, int] = {}
    for index in used_indices:
        color = colors[index]
        count = counts[index]
        best_group: int | None = None
        best_distance = merge_distance
        for group_index, group in enumerate(groups):
            distance = color_distance(color, group["color"])
            if distance <= best_distance and colors_can_merge(color, group["color"], merge_distance):
                best_group = group_index
                best_distance = distance

        if best_group is None:
            old_to_new[index] = len(groups)
            groups.append({"color": color, "count": count})
            continue

        group = groups[best_group]
        total = group["count"] + count
        group["color"] = tuple(
            int(round((group["color"][channel] * group["count"] + color[channel] * count) / total))
            for channel in range(3)
        )
        group["count"] = total
        old_to_new[index] = best_group

    lookup = [old_to_new.get(index, 0) for index in range(256)]
    return palette_image.point(lookup), [group["color"] for group in groups]


def snap_palette_to_inventory_threads(
    palette_image: Image.Image,
    colors: list[tuple[int, int, int]],
    match_distance: float = 64.0,
    background_threshold: int = 245,
) -> tuple[Image.Image, list[tuple[int, int, int]]]:
    if match_distance <= 0:
        return palette_image, colors
    inventory = load_inventory()
    if not inventory:
        return palette_image, colors

    inventory_colors = [
        (item, hex_to_rgb(item["color"]))
        for item in inventory
    ]
    counts = palette_image.histogram()
    groups: list[dict] = []
    group_by_key: dict[tuple[str, str], int] = {}
    old_to_new: dict[int, int] = {}

    for index, color in enumerate(colors):
        if index >= len(counts) or counts[index] == 0:
            continue
        if is_background_color(color, background_threshold):
            key = ("palette", str(index))
            target_color = color
        else:
            match_item = None
            match_color = None
            match_distance_value = match_distance
            for item, inventory_color in inventory_colors:
                distance = color_distance(color, inventory_color)
                if distance <= match_distance_value:
                    match_item = item
                    match_color = inventory_color
                    match_distance_value = distance
            if match_item is None or match_color is None:
                key = ("palette", str(index))
                target_color = color
            else:
                key = ("inventory", match_item["id"])
                target_color = match_color

        if key not in group_by_key:
            group_by_key[key] = len(groups)
            groups.append({"color": target_color, "count": counts[index]})
        else:
            groups[group_by_key[key]]["count"] += counts[index]
        old_to_new[index] = group_by_key[key]

    if not groups:
        return palette_image, colors
    lookup = [old_to_new.get(index, 0) for index in range(256)]
    return palette_image.point(lookup), [group["color"] for group in groups]


def quantized_pixels(
    image: Image.Image,
    max_colors: int,
    color_merge_distance: float = 56.0,
    background_threshold: int = 245,
) -> tuple[Image.Image, list[tuple[int, int, int]]]:
    rgb = Image.alpha_composite(Image.new("RGBA", image.size, (255, 255, 255, 255)), image).convert("RGB")
    source_pixels = list(rgb.getdata())
    foreground_pixels = [
        pixel
        for pixel in source_pixels
        if not is_background_color(pixel, background_threshold)
    ]
    if not foreground_pixels:
        palette_image = rgb.quantize(colors=max(2, max_colors), method=Image.Quantize.MEDIANCUT)
        palette = palette_image.getpalette() or []
        colors = []
        for index in range(max(2, max_colors)):
            offset = index * 3
            if offset + 2 >= len(palette):
                break
            colors.append((palette[offset], palette[offset + 1], palette[offset + 2]))
        return merge_similar_palette_colors(palette_image, colors, color_merge_distance)

    sample = Image.new("RGB", (len(foreground_pixels), 1))
    sample.putdata(foreground_pixels)
    foreground_palette = sample.quantize(colors=max(2, max_colors), method=Image.Quantize.MEDIANCUT)
    palette = foreground_palette.getpalette() or []
    palette_counts = foreground_palette.histogram()
    colors: list[tuple[int, int, int]] = []
    for index in range(max(2, max_colors)):
        offset = index * 3
        if offset + 2 >= len(palette):
            break
        if index < len(palette_counts) and palette_counts[index] == 0:
            continue
        colors.append((palette[offset], palette[offset + 1], palette[offset + 2]))

    if not colors:
        colors = [(0, 0, 0)]
    background_index = len(colors)

    def nearest_index(pixel: tuple[int, int, int]) -> int:
        if is_background_color(pixel, background_threshold):
            return background_index
        return min(range(len(colors)), key=lambda index: color_distance(pixel, colors[index]))

    indexed = Image.new("L", rgb.size)
    indexed.putdata([nearest_index(pixel) for pixel in source_pixels])
    indexed, merged_colors = merge_similar_palette_colors(indexed, [*colors, (255, 255, 255)], color_merge_distance)
    return snap_palette_to_inventory_threads(
        indexed,
        merged_colors,
        background_threshold=background_threshold,
    )


def image_to_segments(
    source_file: Path,
    *,
    fit_width_mm: float | None = 90.0,
    fit_height_mm: float | None = None,
    max_colors: int = 6,
    fill_mode: str = "tatami",
    fill_angle_deg: float = 45.0,
    fill_spacing_mm: float = 0.5,
    min_run_mm: float = 0.3,
    background_threshold: int = 245,
    color_merge_distance: float = 56.0,
    max_stitch_mm: float = 3.0,
    path_planning: str = "min_cuts",
    trim_after_mm: float = 12.0,
    detail_px_per_mm: float = 8.0,
    pdf_page: int = 1,
    pdf_dpi: int = 180,
) -> tuple[list[dict], list[dict], list[dict], dict]:
    px_per_mm = max(detail_px_per_mm, 1 / max(fill_spacing_mm, 0.1))
    image = load_raster_source(source_file, pdf_page=pdf_page, pdf_dpi=pdf_dpi)
    image = trim_transparent_or_white(image)
    image = resize_for_fit(image, fit_width_mm, fit_height_mm, px_per_mm)
    palette_image, colors = quantized_pixels(
        image,
        max_colors=max_colors,
        color_merge_distance=color_merge_distance,
        background_threshold=background_threshold,
    )
    width_mm = image.width / px_per_mm
    height_mm = image.height / px_per_mm
    origin_x = -width_mm / 2
    origin_y = -height_mm / 2
    min_run_px = max(1, int(math.ceil(min_run_mm * px_per_mm)))
    min_detail_run_px = max(1, int(math.ceil(0.15 * px_per_mm)))
    row_step_px = max(1, int(math.floor(fill_spacing_mm * px_per_mm)))
    fill_mode = fill_mode.lower().strip()
    if fill_mode not in {"horizontal", "tatami", "crosshatch", "mixed", "outline", "contour"}:
        fill_mode = "tatami"
    if path_planning not in {"fast", "clean_top", "min_cuts"}:
        path_planning = "min_cuts"
    background_fill_index = next(
        (index for index, color in enumerate(colors) if is_background_color(color, background_threshold)),
        0,
    )
    source_cx = (image.width - 1) / 2
    source_cy = (image.height - 1) / 2
    def raster_fill_angle_candidates(base_angle: float) -> list[float]:
        if fill_mode == "horizontal":
            return [0.0]
        if fill_mode != "mixed":
            return [base_angle]
        seen: set[float] = set()
        candidates: list[float] = []
        for offset in (0, -5, 5, -10, 10, -15, 15, -22.5, 22.5, -30, 30, -45, 45, 90):
            angle = ((base_angle + offset + 90) % 180) - 90
            rounded = round(angle, 3)
            if rounded not in seen:
                seen.add(rounded)
                candidates.append(angle)
        return candidates

    def score_raster_angle(palette_index: int, stitch_angle: float) -> tuple[int, float, int, int]:
        working_image = palette_image
        if abs(stitch_angle) > 0.001:
            working_image = palette_image.rotate(
                stitch_angle,
                resample=Image.Resampling.NEAREST,
                expand=True,
                fillcolor=background_fill_index,
            )
        pixels = working_image.load()
        working_width, working_height = working_image.size
        short_remainders = 0
        run_count = 0
        stitch_estimate = 0
        for row in range(0, working_height, row_step_px):
            start: int | None = None
            for col in range(working_width):
                if pixels[col, row] == palette_index:
                    if start is None:
                        start = col
                elif start is not None:
                    span = col - start
                    if span >= min_detail_run_px:
                        run_count += 1
                        stitch_estimate += max(1, int(math.ceil((span / px_per_mm) / max(max_stitch_mm, 0.1))))
                        if span < min_run_px * 1.5:
                            short_remainders += 1
                    start = None
            if start is not None:
                span = working_width - start
                if span >= min_detail_run_px:
                    run_count += 1
                    stitch_estimate += max(1, int(math.ceil((span / px_per_mm) / max(max_stitch_mm, 0.1))))
                    if span < min_run_px * 1.5:
                        short_remainders += 1
        return short_remainders, abs(stitch_angle - fill_angle_deg), run_count, stitch_estimate

    def best_raster_angle(palette_index: int) -> float:
        candidates = raster_fill_angle_candidates(fill_angle_deg)
        return min(candidates, key=lambda angle: score_raster_angle(palette_index, angle))

    stitch_angles = [0.0] if fill_mode == "horizontal" else [fill_angle_deg]
    if fill_mode == "crosshatch":
        stitch_angles.append(-fill_angle_deg if abs(fill_angle_deg) > 0.001 else 90.0)

    segments: list[dict] = []
    commands: list[dict] = []
    color_blocks: list[dict] = []
    counts = {
        "needle_points": 0,
        "stitch_segments": 0,
        "jumps": 0,
        "trims": 0,
        "color_changes": 0,
        "ends": 0,
    }
    previous_point: tuple[float, float] | None = None
    pending_travel: str | None = None

    color_counts: dict[int, int] = {}
    palette_pixels = palette_image.load()
    for y in range(palette_image.height):
        for x in range(palette_image.width):
            index = palette_pixels[x, y]
            color_counts[index] = color_counts.get(index, 0) + 1
    color_order = sorted(color_counts, key=lambda index: color_counts[index], reverse=True)

    def to_design_mm(
        col: float,
        row: float,
        working_cx: float,
        working_cy: float,
        cos_angle: float,
        sin_angle: float,
    ) -> tuple[float, float]:
        rotated_x = col - working_cx
        rotated_y = row - working_cy
        source_x = rotated_x * cos_angle - rotated_y * sin_angle + source_cx
        source_y = rotated_x * sin_angle + rotated_y * cos_angle + source_cy
        return origin_x + source_x / px_per_mm, origin_y + source_y / px_per_mm

    def append_run(
        block: dict,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        row_index: int,
    ) -> None:
        nonlocal previous_point, pending_travel
        connected_to_start = False
        if previous_point is not None and math.hypot(previous_point[0] - x1, previous_point[1] - y1) > 0.001:
            travel_distance = math.hypot(previous_point[0] - x1, previous_point[1] - y1)
            should_trim = pending_travel == "travel_after_color_change" or travel_distance >= trim_after_mm
            connector_limit_mm = min(max_stitch_mm, max(fill_spacing_mm * 4.0, 0.9))
            can_stitch_travel = path_planning == "min_cuts" and not should_trim and travel_distance <= connector_limit_mm
            if pending_travel:
                commands.append(
                    {
                        "x": previous_point[0],
                        "y": previous_point[1],
                        "command": "color_change",
                        "color": block["thread"],
                        "step": counts["needle_points"],
                    }
                )
            if should_trim:
                counts["trims"] += 1
                commands.append(
                    {
                        "x": previous_point[0],
                        "y": previous_point[1],
                        "command": "trim",
                        "color": block["thread"],
                        "step": counts["needle_points"],
                    }
                )
                pending_travel = pending_travel or "travel_after_trim"
            if can_stitch_travel:
                counts["needle_points"] += 1
                counts["stitch_segments"] += 1
                block["stitches"] += 1
                segments.append(
                    {
                        "x1": previous_point[0],
                        "y1": previous_point[1],
                        "x2": x1,
                        "y2": y1,
                        "kind": "stitch",
                        "color": block["color"],
                        "colorIndex": block["thread"],
                        "blockIndex": block["index"],
                        "step": counts["needle_points"],
                    }
                )
                commands.append({"x": x1, "y": y1, "command": "stitch", "color": block["thread"], "step": counts["needle_points"]})
                connected_to_start = True
            else:
                commands.append({"x": x1, "y": y1, "command": "jump", "color": block["thread"], "step": counts["needle_points"]})
                counts["jumps"] += 1
                segments.append(
                    {
                        "x1": previous_point[0],
                        "y1": previous_point[1],
                        "x2": x1,
                        "y2": y1,
                        "kind": pending_travel or "jump",
                        "color": block["color"],
                        "colorIndex": block["thread"],
                        "blockIndex": block["index"],
                        "step": counts["needle_points"],
                    }
                )
            pending_travel = None
        if previous_point is None and not connected_to_start:
            commands.append({"x": x1, "y": y1, "command": "jump", "color": block["thread"], "step": counts["needle_points"]})
        span = math.hypot(x2 - x1, y2 - y1)
        if span <= 0.001:
            return
        max_len = max(max_stitch_mm, 0.1)
        min_len = max(min_run_mm, 0.1)
        phase = ((row_index % 3) / 3.0) * max_len if fill_mode in {"tatami", "crosshatch"} else 0.0
        distances = [0.0]
        first = max_len - phase
        if first < max_len * 0.35:
            first += max_len
        position = first
        while position < span:
            distances.append(position)
            position += max_len
        distances.append(span)
        if len(distances) >= 3 and distances[-1] - distances[-2] < min_len:
            segment_count = max(1, len(distances) - 1)
            even_step = span / segment_count
            if even_step < min_len and segment_count > 1:
                segment_count -= 1
                even_step = span / segment_count
            distances = [even_step * index for index in range(segment_count + 1)]

        last_x = x1
        last_y = y1
        for distance in distances[1:]:
            ratio = distance / span
            x = x1 + (x2 - x1) * ratio
            y = y1 + (y2 - y1) * ratio
            counts["needle_points"] += 1
            counts["stitch_segments"] += 1
            block["stitches"] += 1
            segments.append(
                {
                    "x1": last_x,
                    "y1": last_y,
                    "x2": x,
                    "y2": y,
                    "kind": "stitch",
                    "color": block["color"],
                    "colorIndex": block["thread"],
                    "blockIndex": block["index"],
                    "step": counts["needle_points"],
                }
            )
            commands.append({"x": x, "y": y, "command": "stitch", "color": block["thread"], "step": counts["needle_points"]})
            last_x = x
            last_y = y
            previous_point = (x, y)

    def add_preserved_range(ranges: list[tuple[int, int]], left: int, right: int, width: int) -> None:
        span = right - left + 1
        if span < min_detail_run_px:
            return
        if span < min_run_px:
            center = (left + right) / 2
            left = int(round(center - ((min_run_px - 1) / 2)))
            right = left + min_run_px - 1
            if left < 0:
                right -= left
                left = 0
            if right >= width:
                left -= right - width + 1
                right = width - 1
            left = max(0, left)
        ranges.append((left, right))

    def stitch_scan_fill(
        block: dict,
        palette_index: int,
        stitch_angles_for_block: list[float],
        source_image: Image.Image | None = None,
    ) -> None:
        for pass_index, stitch_angle in enumerate(stitch_angles_for_block):
            working_image = source_image or palette_image
            if abs(stitch_angle) > 0.001:
                working_image = working_image.rotate(
                    stitch_angle,
                    resample=Image.Resampling.NEAREST,
                    expand=True,
                    fillcolor=background_fill_index,
                )
            pixels = working_image.load()
            working_width, working_height = working_image.size
            working_cx = (working_width - 1) / 2
            working_cy = (working_height - 1) / 2
            angle = math.radians(stitch_angle)
            cos_angle = math.cos(angle)
            sin_angle = math.sin(angle)

            planned_runs: list[tuple[list[tuple[float, float]], int]] = []
            for row in range(0, working_height, row_step_px):
                ranges: list[tuple[int, int]] = []
                start: int | None = None
                for col in range(working_width):
                    if pixels[col, row] == palette_index:
                        if start is None:
                            start = col
                    elif start is not None:
                        add_preserved_range(ranges, start, col - 1, working_width)
                        start = None
                if start is not None:
                    add_preserved_range(ranges, start, working_width - 1, working_width)

                if row % 2:
                    ranges.reverse()
                for left, right in ranges:
                    x1, y1 = to_design_mm(left, row, working_cx, working_cy, cos_angle, sin_angle)
                    x2, y2 = to_design_mm(right, row, working_cx, working_cy, cos_angle, sin_angle)
                    if row % 2:
                        x1, x2 = x2, x1
                        y1, y2 = y2, y1
                    planned_runs.append(([(x1, y1), (x2, y2)], row + pass_index))

            for points, row_index in route_planned_runs(planned_runs, start=previous_point, mode=path_planning):
                append_run(block, points[0][0], points[0][1], points[-1][0], points[-1][1], row_index)

    def stitch_component_scan_fill(block: dict, component: set[tuple[int, int]]) -> None:
        if not component:
            return
        rows_by_y: dict[int, list[int]] = {}
        for col, row in component:
            rows_by_y.setdefault(row, []).append(col)
        planned_runs: list[tuple[list[tuple[float, float]], int]] = []
        min_row = min(rows_by_y)
        selected_row_index = 0
        for row in sorted(rows_by_y):
            if (row - min_row) % row_step_px:
                continue
            columns = sorted(rows_by_y[row])
            ranges: list[tuple[int, int]] = []
            start = columns[0]
            previous = columns[0]
            for col in columns[1:]:
                if col > previous + 1:
                    add_preserved_range(ranges, start, previous, palette_image.width)
                    start = col
                previous = col
            add_preserved_range(ranges, start, previous, palette_image.width)
            if selected_row_index % 2:
                ranges.reverse()
            for left, right in ranges:
                x1 = origin_x + left / px_per_mm
                y1 = origin_y + row / px_per_mm
                x2 = origin_x + right / px_per_mm
                y2 = y1
                if selected_row_index % 2:
                    x1, x2 = x2, x1
                planned_runs.append(([(x1, y1), (x2, y2)], selected_row_index))
            selected_row_index += 1
        for points, row_index in route_planned_runs(planned_runs, start=previous_point, mode="clean_top"):
            append_run(block, points[0][0], points[0][1], points[-1][0], points[-1][1], row_index)

    def stitch_boundary_passes(block: dict, active_pixels: set[tuple[int, int]], pass_count: int) -> None:
        nonlocal previous_point
        active = set(active_pixels)
        for layer_index in range(pass_count):
            if not active:
                break
            boundary = boundary_pixels(active, palette_image.width, palette_image.height)
            if not boundary:
                break
            loops = [simplify_grid_path(loop) for loop in boundary_loops_from_active(active)]
            loops = [loop for loop in loops if len(loop) >= 2]
            remaining_loops = loops
            while remaining_loops:
                if previous_point is None:
                    loop_index = 0
                    reverse_loop = False
                else:
                    best: tuple[float, int, bool] | None = None
                    for candidate_index, loop in enumerate(remaining_loops):
                        first = (origin_x + loop[0][0] / px_per_mm, origin_y + loop[0][1] / px_per_mm)
                        last = (origin_x + loop[-1][0] / px_per_mm, origin_y + loop[-1][1] / px_per_mm)
                        forward = math.hypot(previous_point[0] - first[0], previous_point[1] - first[1])
                        reverse = math.hypot(previous_point[0] - last[0], previous_point[1] - last[1])
                        option = (min(forward, reverse), candidate_index, reverse < forward)
                        if best is None or option < best:
                            best = option
                    _, loop_index, reverse_loop = best or (0.0, 0, False)
                loop = remaining_loops.pop(loop_index)
                if reverse_loop:
                    loop = list(reversed(loop))
                for point_index in range(len(loop) - 1):
                    start = loop[point_index]
                    end = loop[point_index + 1]
                    x1 = origin_x + start[0] / px_per_mm
                    y1 = origin_y + start[1] / px_per_mm
                    x2 = origin_x + end[0] / px_per_mm
                    y2 = origin_y + end[1] / px_per_mm
                    append_run(block, x1, y1, x2, y2, layer_index + point_index)
            active.difference_update(boundary)

    for palette_index in color_order:
        if palette_index >= len(colors):
            continue
        rgb = colors[palette_index]
        alpha_like_background = is_background_color(rgb, background_threshold)
        if alpha_like_background:
            continue

        block = {
            "index": len(color_blocks),
            "thread": len(color_blocks),
            "color": color_to_hex(rgb),
            "label": f"Image {color_to_hex(rgb)}",
            "stitches": 0,
        }
        color_blocks.append(block)
        if block["index"] > 0:
            counts["color_changes"] += 1
            pending_travel = "travel_after_color_change"

        if fill_mode in {"outline", "contour"}:
            pixels = palette_image.load()
            active = {
                (col, row)
                for row in range(palette_image.height)
                for col in range(palette_image.width)
                if pixels[col, row] == palette_index
            }
            components = connected_pixel_components(active)
            remaining_components = components
            while remaining_components:
                if previous_point is None:
                    component_index = 0
                else:
                    component_index = min(
                        range(len(remaining_components)),
                        key=lambda index: math.hypot(
                            previous_point[0]
                            - (origin_x + component_centroid(remaining_components[index])[0] / px_per_mm),
                            previous_point[1]
                            - (origin_y + component_centroid(remaining_components[index])[1] / px_per_mm),
                        ),
                    )
                component = remaining_components.pop(component_index)
                if fill_mode == "contour":
                    stitch_component_scan_fill(block, component)
                stitch_boundary_passes(block, component, 1)
            continue

        block_stitch_angles = [best_raster_angle(palette_index)] if fill_mode == "mixed" else stitch_angles
        stitch_scan_fill(block, palette_index, block_stitch_angles)

    if not segments:
        raise ValueError("No stitchable image regions were found.")
    return segments, commands, color_blocks, counts


def write_segments_as_pes(
    segments: list[dict],
    color_blocks: list[dict],
    output_file: Path,
    selected_blocks: set[int] | None = None,
) -> None:
    selected_blocks = selected_blocks if selected_blocks is not None else {
        block["index"] for block in color_blocks
    }
    pattern = embroidery.EmbPattern()
    active_block: int | None = None
    previous_point: tuple[float, float] | None = None

    for segment in segments:
        block_index = segment["blockIndex"]
        if block_index not in selected_blocks:
            continue
        if segment["kind"] == "travel_after_trim":
            pattern.trim()
            previous_point = None
            continue
        if segment["kind"] != "stitch":
            continue
        if block_index != active_block:
            block = color_blocks[block_index]
            pattern.add_thread(make_thread(block["color"]))
            if active_block is not None:
                pattern.color_change()
            active_block = block_index
            previous_point = None

        start = (segment["x1"], segment["y1"])
        end = (segment["x2"], segment["y2"])
        if (
            previous_point is None
            or abs(previous_point[0] - start[0]) > 0.001
            or abs(previous_point[1] - start[1]) > 0.001
        ):
            pattern.add_stitch_absolute(
                embroidery.JUMP,
                int(round(start[0] * EMB_UNITS_PER_MM)),
                int(round(start[1] * EMB_UNITS_PER_MM)),
            )
        pattern.add_stitch_absolute(
            embroidery.STITCH,
            int(round(end[0] * EMB_UNITS_PER_MM)),
            int(round(end[1] * EMB_UNITS_PER_MM)),
        )
        previous_point = end

    if pattern.count_stitches() == 0:
        raise ValueError("No selected stitches to write.")
    pattern.end()
    pattern.write(str(output_file))


def write_image_as_pes(source_file: Path, output_file: Path, **settings) -> None:
    segments, _, color_blocks, _ = image_to_segments(source_file, **settings)
    write_segments_as_pes(segments, color_blocks, output_file)
