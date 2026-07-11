from __future__ import annotations

from PIL import Image, ImageColor, ImageDraw, ImageFilter

from thread_settings import DEFAULT_THREAD_WEIGHT, thread_diameter_mm


def clamp_channel(value: float) -> int:
    return max(0, min(255, int(round(value))))


def blend_rgb(color: tuple[int, int, int], other: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    return tuple(clamp_channel(channel + (other[index] - channel) * amount) for index, channel in enumerate(color))


def realistic_preview_image(
    segments: list[dict],
    bounds: tuple[float, float, float, float],
    *,
    fabric_color: str,
    thread_weight: str = DEFAULT_THREAD_WEIGHT,
    selected_blocks: set[int] | None = None,
    max_width_px: int = 2200,
    include_hoop: bool = True,
) -> Image.Image:
    min_x, min_y, max_x, max_y = bounds
    design_w = max(max_x - min_x, 1.0)
    design_h = max(max_y - min_y, 1.0)
    margin_mm = 8.0
    scale = min(28.0, max(8.0, max_width_px / (design_w + margin_mm * 2)))
    width = int(round((design_w + margin_mm * 2) * scale))
    height = int(round((design_h + margin_mm * 2) * scale))
    offset_x = (margin_mm - min_x) * scale
    offset_y = (margin_mm - min_y) * scale
    fabric_rgb = ImageColor.getrgb(fabric_color if fabric_color.startswith("#") else "#fbfcfa")
    image = Image.new("RGB", (width, height), fabric_rgb)
    weave = ImageDraw.Draw(image, "RGBA")
    light = blend_rgb(fabric_rgb, (255, 255, 255), 0.28)
    dark = blend_rgb(fabric_rgb, (0, 0, 0), 0.10)
    spacing = max(3, int(round(scale * 0.35)))
    for x in range(0, width, spacing):
        weave.line([(x, 0), (x, height)], fill=(*dark, 26), width=1)
        if x + 1 < width:
            weave.line([(x + 1, 0), (x + 1, height)], fill=(*light, 18), width=1)
    for y in range(0, height, spacing):
        weave.line([(0, y), (width, y)], fill=(*dark, 20), width=1)
        if y + 1 < height:
            weave.line([(0, y + 1), (width, y + 1)], fill=(*light, 16), width=1)
    if include_hoop:
        hoop_margin = max(10, int(round(scale * 1.2)))
        hoop_width = max(2, int(round(scale * 0.18)))
        hoop_shadow = blend_rgb(fabric_rgb, (0, 0, 0), 0.22)
        weave.rounded_rectangle(
            [hoop_margin, hoop_margin, width - hoop_margin, height - hoop_margin],
            radius=max(18, int(round(scale * 2.2))),
            outline=(*hoop_shadow, 72),
            width=hoop_width + 2,
        )
        weave.rounded_rectangle(
            [
                hoop_margin + hoop_width,
                hoop_margin + hoop_width,
                width - hoop_margin - hoop_width,
                height - hoop_margin - hoop_width,
            ],
            radius=max(14, int(round(scale * 2.0))),
            outline=(*light, 150),
            width=max(1, hoop_width),
        )

    nominal_thread_width = max(2, int(round(thread_diameter_mm(thread_weight) * scale)))
    coverage_width = max(nominal_thread_width + 2, int(round(nominal_thread_width * 2.15)))
    shadow = Image.new("RGBA", image.size, (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow, "RGBA")
    thread_layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    thread_draw = ImageDraw.Draw(thread_layer, "RGBA")

    def point(x: float, y: float) -> tuple[float, float]:
        return x * scale + offset_x, y * scale + offset_y

    for segment in segments:
        if segment.get("kind") != "stitch":
            continue
        if selected_blocks is not None and segment.get("blockIndex") not in selected_blocks:
            continue
        start = point(segment["x1"], segment["y1"])
        end = point(segment["x2"], segment["y2"])
        color = ImageColor.getrgb(segment.get("color", "#111111"))
        luminance = (color[0] * 0.2126 + color[1] * 0.7152 + color[2] * 0.0722) / 255.0
        highlight_mix = 0.03 + luminance * 0.28
        highlight_alpha = int(round(18 + luminance * 132))
        base = (*color, 248)
        low = (*blend_rgb(color, (0, 0, 0), 0.24), 225)
        high = (*blend_rgb(color, (255, 255, 255), highlight_mix), highlight_alpha)
        shadow_draw.line(
            [
                (start[0] + coverage_width * 0.36, start[1] + coverage_width * 0.44),
                (end[0] + coverage_width * 0.36, end[1] + coverage_width * 0.44),
            ],
            fill=(0, 0, 0, 58),
            width=max(1, coverage_width + 2),
        )
        thread_draw.line([start, end], fill=low, width=max(1, coverage_width + 1))
        thread_draw.line([start, end], fill=base, width=coverage_width)
        if nominal_thread_width >= 3 and highlight_alpha > 24:
            thread_draw.line([start, end], fill=high, width=max(1, nominal_thread_width // 3))

    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=max(0.6, coverage_width * 0.28)))
    image = Image.alpha_composite(image.convert("RGBA"), shadow)
    image = Image.alpha_composite(image, thread_layer)
    return image.convert("RGB")
