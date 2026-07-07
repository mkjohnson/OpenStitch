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
    min_run_mm: float = 0.35,
    background_threshold: int = 245,
    color_merge_distance: float = 56.0,
    max_stitch_mm: float = 3.0,
    pdf_page: int = 1,
    pdf_dpi: int = 180,
) -> tuple[list[dict], list[dict], list[dict], dict]:
    px_per_mm = 1 / max(fill_spacing_mm, 0.1)
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
    fill_mode = fill_mode.lower().strip()
    if fill_mode not in {"horizontal", "tatami"}:
        fill_mode = "tatami"
    stitch_angle = fill_angle_deg if fill_mode == "tatami" else 0.0
    working_image = palette_image
    background_fill_index = next(
        (index for index, color in enumerate(colors) if is_background_color(color, background_threshold)),
        0,
    )
    if abs(stitch_angle) > 0.001:
        working_image = palette_image.rotate(
            stitch_angle,
            resample=Image.Resampling.NEAREST,
            expand=True,
            fillcolor=background_fill_index,
        )
    pixels = working_image.load()
    working_width, working_height = working_image.size
    source_cx = (image.width - 1) / 2
    source_cy = (image.height - 1) / 2
    working_cx = (working_width - 1) / 2
    working_cy = (working_height - 1) / 2
    angle = math.radians(stitch_angle)
    cos_angle = math.cos(angle)
    sin_angle = math.sin(angle)

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
    for y in range(working_height):
        for x in range(working_width):
            index = pixels[x, y]
            color_counts[index] = color_counts.get(index, 0) + 1
    color_order = sorted(color_counts, key=lambda index: color_counts[index], reverse=True)

    def to_design_mm(col: float, row: float) -> tuple[float, float]:
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
        if previous_point is not None and math.hypot(previous_point[0] - x1, previous_point[1] - y1) > 0.001:
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
        commands.append({"x": x1, "y": y1, "command": "jump", "color": block["thread"], "step": counts["needle_points"]})
        span = math.hypot(x2 - x1, y2 - y1)
        if span <= 0.001:
            return
        max_len = max(max_stitch_mm, 0.1)
        phase = ((row_index % 3) / 3.0) * max_len if fill_mode == "tatami" else 0.0
        distances = [0.0]
        first = max_len - phase
        if first < max_len * 0.35:
            first += max_len
        position = first
        while position < span:
            distances.append(position)
            position += max_len
        distances.append(span)

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

        for row in range(working_height):
            ranges: list[tuple[int, int]] = []
            start: int | None = None
            for col in range(working_width):
                if pixels[col, row] == palette_index:
                    if start is None:
                        start = col
                elif start is not None:
                    if col - start >= min_run_px:
                        ranges.append((start, col - 1))
                    start = None
            if start is not None and working_width - start >= min_run_px:
                ranges.append((start, working_width - 1))

            if row % 2:
                ranges.reverse()
            for left, right in ranges:
                x1, y1 = to_design_mm(left, row)
                x2, y2 = to_design_mm(right, row)
                if row % 2:
                    x1, x2 = x2, x1
                    y1, y2 = y2, y1
                append_run(block, x1, y1, x2, y2, row)

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
        if block_index not in selected_blocks or segment["kind"] != "stitch":
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
