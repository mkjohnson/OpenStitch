from __future__ import annotations

import argparse
import html
import json
import math
from pathlib import Path

import pyembroidery as embroidery

from image_digitizer import image_to_segments, is_raster_source, svg_needs_rasterization, write_image_as_pes
from svg2brother import extract_runs, transform_runs, positive_float, write_embroidery, make_thread
from thread_inventory import closest_inventory_match, load_inventory, normalize_hex, rgb_distance


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
            continue
        if command == embroidery.END:
            counts["ends"] += 1
            break
        if command == embroidery.TRIM:
            counts["trims"] += 1
            previous = (x, y)
            continue

        if previous is not None and command in {embroidery.STITCH, embroidery.JUMP}:
            is_stitch = command == embroidery.STITCH
            block = ensure_block()
            segments.append(
                {
                    "x1": previous[0],
                    "y1": previous[1],
                    "x2": x,
                    "y2": y,
                    "kind": "stitch" if is_stitch else "jump",
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


def inventory_label(item: dict) -> str:
    label = " ".join(part for part in [item.get("brand", ""), item.get("name", "")] if part).strip()
    return label or item["color"]


def render_inventory_options() -> str:
    options = ['<option value="">Known thread colors</option>']
    for item in load_inventory():
        label = f'{inventory_label(item)} - {item["color"]}'
        options.append(
            '<option value="{color}">{label}</option>'.format(
                color=html.escape(item["color"], quote=True),
                label=html.escape(label),
            )
        )
    return "".join(options)


def group_color_blocks_by_inventory(
    segments: list[dict],
    commands: list[dict],
    color_blocks: list[dict],
    counts: dict,
    match_distance: float = 64.0,
) -> tuple[list[dict], list[dict], list[dict], dict]:
    inventory = load_inventory()
    if not inventory or match_distance <= 0:
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


def render_thread_plan(color_blocks: list[dict], usage_by_block: dict[int, float]) -> str:
    inventory = load_inventory()
    rows: list[str] = []
    needed: list[str] = []
    total_meters = 0.0
    for block in color_blocks:
        color = block["color"]
        meters = usage_by_block.get(block["index"], 0.0)
        total_meters += meters
        match = closest_inventory_match(color, inventory)
        if match is None:
            status = "Need to buy"
            detail = "No inventory colors saved yet."
            status_class = "need"
            needed.append(color)
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
        rows.append(
            '<tr>'
            f'<td><span class="swatch" style="background:{html.escape(color)}"></span>{html.escape(color)}</td>'
            f'<td>{format_usage(meters)}</td>'
            f'<td><span class="thread-status {status_class}">{status}</span></td>'
            f'<td>{detail}</td>'
            '</tr>'
        )

    summary = (
        f"Estimated total thread: {format_usage(total_meters)} including 12% allowance. "
        f"Colors to buy: {len(set(needed))}."
    )
    return (
        '<section class="thread-plan">'
        '<h2>Thread Planning</h2>'
        f'<p>{html.escape(summary)}</p>'
        '<table><thead><tr><th>Design color</th><th>Use</th><th>Status</th><th>Closest inventory match</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
        '<a class="inventory-link" href="/inventory">Edit Thread Inventory</a>'
        '</section>'
    )


def render_legend(pattern, color_blocks: list[dict], color_controls: bool = False) -> str:
    items: list[str] = []
    inventory_options = render_inventory_options() if color_controls else ""
    for block in color_blocks:
        index = block["thread"]
        color = block["color"]
        if pattern is None:
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
                '<select class="block-thread-select" data-block-select="{index}" '
                'aria-label="Known thread color for block {label_index}">{options}</select>'
                '</span>'
            ).format(
                color=html.escape(color, quote=True),
                index=block["index"],
                label_index=block["index"] + 1,
                options=inventory_options,
            )
        items.append(
            f'<li data-block-row="{block["index"]}"><span class="swatch" style="background:{html.escape(color)}"></span>'
            f'<span>{checkbox}Block {block["index"] + 1}: {label}'
            f'<small>{block["stitches"]} stitches</small></span>'
            f'<code>{html.escape(color)}</code>{color_editor}{reorder}</li>'
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
    fit_width_mm: float | None = None,
    max_colors: int = 6,
    color_merge_distance: float = 56.0,
    pdf_page: int = 1,
) -> str:
    min_x, min_y, max_x, max_y = design_bounds(segments, commands)
    width = max(max_x - min_x, 1.0)
    height = max(max_y - min_y, 1.0)
    padding = max(width, height) * 0.08
    legend = render_legend(pattern, color_blocks, color_controls=True)
    usage_by_block = estimate_thread_usage(segments)
    thread_plan = render_thread_plan(color_blocks, usage_by_block)
    stats = {
        "Design": input_file.name,
        "Source": source_label,
        "Size": f"{width:.1f} x {height:.1f} mm",
        "Needle points": counts["needle_points"],
        "Stitch segments": counts["stitch_segments"],
        "Jumps": counts["jumps"],
        "Trims": counts["trims"],
        "Color changes": counts["color_changes"],
        "Color blocks": len(color_blocks),
        "Threads": len(pattern.threadlist) if pattern is not None else len(color_blocks),
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
            1 if segment["kind"] == "stitch" else 0,
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
    embedded_segments = html.escape(json.dumps(segment_data, separators=(",", ":")), quote=False)
    embedded_markers = html.escape(json.dumps(marker_data, separators=(",", ":")), quote=False)
    embedded_palette = html.escape(json.dumps(palette_data, separators=(",", ":")), quote=False)
    pes_download = ""
    if pes_href:
        pes_download = (
            '<a class="download-action" href="{href}" download>Download PES</a>'.format(
                href=html.escape(pes_href, quote=True)
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
            '<input type="hidden" name="max_colors" value="{max_colors}">'
            '<input type="hidden" name="color_merge_distance" value="{color_merge_distance}">'
            '<input type="hidden" name="pdf_page" value="{pdf_page}">'
            '<input id="selected-blocks" type="hidden" name="selected_blocks" value="">'
            '<input id="color-order" type="hidden" name="color_order" value="">'
            '<input id="color-overrides" type="hidden" name="color_overrides" value="">'
        ).format(
            action=html.escape(color_export_action, quote=True),
            source=html.escape(source_name, quote=True),
            fit_width=html.escape(fit_value, quote=True),
            max_colors=max_colors,
            color_merge_distance=color_merge_distance,
            pdf_page=pdf_page,
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
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      grid-template-columns: minmax(280px, 340px) 1fr;
    }}
    aside {{
      background: #ffffff;
      border-right: 1px solid #d9ded6;
      padding: 24px;
      overflow: auto;
    }}
    main {{
      min-width: 0;
      padding: 18px;
      display: flex;
      align-items: stretch;
    }}
    h1 {{
      margin: 0 0 18px;
      font-size: 21px;
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
      display: flex;
      justify-content: space-between;
      gap: 14px;
      border-bottom: 1px solid #edf0eb;
      padding-bottom: 8px;
      font-size: 14px;
    }}
    .stats span {{ color: #52605a; }}
    .stats strong {{ text-align: right; }}
    .thread-plan {{
      display: grid;
      gap: 10px;
      margin: 16px 0 18px;
      padding: 12px;
      border: 1px solid #d9ded6;
      border-radius: 8px;
      background: #fbfcfa;
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
      display: grid;
      grid-template-columns: 18px minmax(0, 1fr);
      align-items: center;
      gap: 9px;
      font-size: 13px;
    }}
    li span:nth-child(2) {{
      display: grid;
      gap: 2px;
      min-width: 0;
    }}
    li code {{
      grid-column: 2;
      width: fit-content;
    }}
    .block-color-controls {{
      grid-column: 2;
      display: grid;
      grid-template-columns: minmax(82px, 0.55fr) minmax(110px, 1fr);
      gap: 6px;
      min-width: 0;
    }}
    .block-color-input,
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
    .block-thread-select {{
      padding: 0 6px;
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
      width: 220px;
      display: none;
      gap: 6px;
      padding: 8px;
      border: 1px solid #d9ded6;
      border-radius: 8px;
      background: #ffffff;
      box-shadow: 0 12px 28px rgba(23, 32, 38, 0.18);
    }}
    body.viewer-menu-open .viewer-menu-panel {{
      display: grid;
    }}
    .viewer-menu-panel a {{
      min-height: 36px;
      display: flex;
      align-items: center;
      padding: 0 10px;
      border-radius: 6px;
      color: #26332f;
      text-decoration: none;
    }}
    .viewer-menu-panel a:hover {{
      background: #eaf2ef;
    }}
    .block-toggle {{
      width: 16px;
      height: 16px;
      margin: 0 6px 0 0;
      accent-color: #2f6f73;
      vertical-align: -2px;
    }}
    .block-order-controls {{
      grid-column: 2;
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 4px;
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
      background:
        linear-gradient(#edf0eb 1px, transparent 1px),
        linear-gradient(90deg, #edf0eb 1px, transparent 1px),
        #fbfcfa;
      background-size: 10mm 10mm;
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
    .trim {{ fill: #f6a623; stroke: #6c4710; }}
    .color_change {{ fill: #ffffff; stroke: #243335; }}
    body.hide-jumps .jump {{ display: none; }}
    body.hide-markers .marker {{ display: none; }}
    body.hide-points .needle-point {{ display: none; }}
    @media (max-width: 820px) {{
      body {{
        grid-template-columns: 1fr;
      }}
      aside {{
        border-right: 0;
        border-bottom: 1px solid #d9ded6;
      }}
      main {{
        min-height: 62vh;
      }}
      .stage, canvas {{
        min-height: 62vh;
      }}
    }}
  </style>
</head>
<body data-stats="{embedded_stats}">
  <div class="viewer-menu">
    <button id="viewer-menu-toggle" class="viewer-menu-button" type="button" aria-label="Open menu">&#9776;</button>
    <nav class="viewer-menu-panel" aria-label="Application menu">
      <a href="/">Convert Another</a>
      <a href="/library">Library</a>
      <a href="/inventory">Thread Inventory</a>
      {pes_download}
    </nav>
  </div>
  <aside>
    <h1>{html.escape(input_file.name)}</h1>
    <section class="stats">
      {stats_html}
    </section>
    {thread_plan}
    {pes_download}
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
      <label><input id="toggle-markers" type="checkbox" checked> Show trims and color changes</label>
    </section>
    <section class="toolpath-actions" aria-label="Toolpath zoom controls">
      <button id="zoom-out" type="button">Zoom -</button>
      <button id="zoom-reset" type="button">Reset</button>
      <button id="zoom-in" type="button">Zoom +</button>
    </section>
    <p id="zoom-readout" class="zoom-readout">Zoom 100%</p>
    {export_open}
    <h2>Threads</h2>
    <ul>
      {legend}
    </ul>
    {export_button}
    {export_close}
  </aside>
  <main>
    <div id="stage" class="stage">
      <canvas id="toolpath" data-initial-viewbox="{embedded_view_box}" aria-label="Embroidery stitch preview"></canvas>
    </div>
  </main>
  <script id="segment-data" type="application/json">{embedded_segments}</script>
  <script id="marker-data" type="application/json">{embedded_markers}</script>
  <script id="palette-data" type="application/json">{embedded_palette}</script>
  <script>
    const jumps = document.getElementById("toggle-jumps");
    const points = document.getElementById("toggle-points");
    const markers = document.getElementById("toggle-markers");
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
    const viewerMenuToggle = document.getElementById("viewer-menu-toggle");
    const ctx = toolpath.getContext("2d");
    const segments = JSON.parse(document.getElementById("segment-data").textContent);
    const markerData = JSON.parse(document.getElementById("marker-data").textContent);
    const palette = JSON.parse(document.getElementById("palette-data").textContent);
    const blockToggles = [...document.querySelectorAll(".block-toggle")];
    const selectedBlocksInput = document.getElementById("selected-blocks");
    const colorOrderInput = document.getElementById("color-order");
    const colorOverridesInput = document.getElementById("color-overrides");
    const threadList = document.querySelector(".color-export ul");
    const orderButtons = [...document.querySelectorAll("[data-order-move]")];
    const colorInputs = [...document.querySelectorAll("[data-block-color]")];
    const threadSelects = [...document.querySelectorAll("[data-block-select]")];
    let selectedBlocks = new Set(blockToggles.map((toggle) => Number(toggle.value)));
    const maxStep = {max_step};
    const initialViewBox = JSON.parse(toolpath.dataset.initialViewbox);
    let viewBoxState = {{ ...initialViewBox }};
    let currentStep = maxStep;
    let playing = false;
    let lastFrame = 0;
    let carry = 0;
    let showJumps = true;
    let showPoints = true;
    let showMarkers = true;
    let deviceScale = 1;

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

    function toCanvasX(x) {{
      return ((x - viewBoxState.x) / viewBoxState.width) * toolpath.width;
    }}

    function toCanvasY(y) {{
      return ((y - viewBoxState.y) / viewBoxState.height) * toolpath.height;
    }}

    function renderScene() {{
      if (!ctx) return;
      ctx.clearRect(0, 0, toolpath.width, toolpath.height);
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
        ctx.beginPath();
        ctx.strokeStyle = isStitch ? palette[segment[5]] : "#66736f";
        ctx.globalAlpha = isStitch ? 1 : 0.45;
        ctx.lineWidth = isStitch ? stitchWidth : jumpWidth;
        if (!isStitch) ctx.setLineDash([deviceScale * 5, deviceScale * 4]);
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

      if (showMarkers) {{
        for (const marker of markerData) {{
          if (marker[3] > currentStep) continue;
          const x = toCanvasX(marker[0]);
          const y = toCanvasY(marker[1]);
          ctx.beginPath();
          ctx.fillStyle = marker[2] === "trim" ? "#f6a623" : "#ffffff";
          ctx.strokeStyle = marker[2] === "trim" ? "#6c4710" : "#243335";
          ctx.lineWidth = Math.max(0.8, deviceScale);
          ctx.arc(x, y, Math.max(3, deviceScale * 3.2), 0, Math.PI * 2);
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
      const px = (event.clientX - rect.left) / Math.max(rect.width, 1);
      const py = (event.clientY - rect.top) / Math.max(rect.height, 1);
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

    function applyBlockColor(blockIndex, color) {{
      const normalized = normalizeHex(color);
      if (!normalized) return;
      palette[blockIndex] = normalized;
      const row = document.querySelector(`[data-block-row="${{blockIndex}}"]`);
      if (row) {{
        const swatch = row.querySelector(".swatch");
        const code = row.querySelector("code");
        const input = row.querySelector("[data-block-color]");
        if (swatch) swatch.style.background = normalized;
        if (code) code.textContent = normalized;
        if (input) input.value = normalized;
      }}
      syncColorOverrides();
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
      const dx = ((event.clientX - dragStart.clientX) / Math.max(rect.width, 1)) * dragStart.viewBox.width;
      const dy = ((event.clientY - dragStart.clientY) / Math.max(rect.height, 1)) * dragStart.viewBox.height;
      setViewBox({{
        x: dragStart.viewBox.x - dx,
        y: dragStart.viewBox.y - dy,
        width: dragStart.viewBox.width,
        height: dragStart.viewBox.height,
      }});
    }});
    function endDrag(event) {{
      if (!dragStart) return;
      dragStart = null;
      stage.classList.remove("dragging");
      if (stage.hasPointerCapture(event.pointerId)) {{
        stage.releasePointerCapture(event.pointerId);
      }}
    }}
    stage.addEventListener("pointerup", endDrag);
    stage.addEventListener("pointercancel", endDrag);
    jumps.addEventListener("change", () => {{
      showJumps = jumps.checked;
      renderScene();
    }});
    points.addEventListener("change", () => {{
      showPoints = points.checked;
      renderScene();
    }});
    markers.addEventListener("change", () => {{
      showMarkers = markers.checked;
      renderScene();
    }});
    for (const toggle of blockToggles) {{
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
    fit_width_mm: float | None,
    fit_height_mm: float | None,
    center: bool,
) -> tuple[list[dict], list[dict], list[dict], dict]:
    runs = extract_runs(
        svg_file,
        sample_step_mm=sample_step_mm,
        fill_spacing_mm=fill_spacing_mm,
        max_stitch_mm=max_stitch_mm,
    )
    runs = transform_runs(
        runs,
        fit_width_mm=fit_width_mm,
        fit_height_mm=fit_height_mm,
        center=center,
    )

    segments: list[dict] = []
    commands: list[dict] = []
    color_blocks: list[dict] = []
    active_color: str | None = None
    active_block: dict | None = None
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
                commands.append(
                    {
                        "x": run.points_mm[0][0],
                        "y": run.points_mm[0][1],
                        "command": "color_change",
                        "color": len(color_blocks),
                        "step": counts["needle_points"],
                    }
                )
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
            counts["needle_points"] += 1
            counts["stitch_segments"] += 1
            active_block["stitches"] += 1
            segments.append(
                {
                    "x1": start[0],
                    "y1": start[1],
                    "x2": end[0],
                    "y2": end[1],
                    "kind": "stitch",
                    "color": active_block["color"],
                    "colorIndex": active_block["thread"],
                    "blockIndex": active_block["index"],
                    "step": counts["needle_points"],
                }
            )
            commands.append(
                {
                    "x": end[0],
                    "y": end[1],
                    "command": "stitch",
                    "color": active_block["thread"],
                    "step": counts["needle_points"],
                }
            )

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
    fill_spacing_mm: float = 0.5,
    max_stitch_mm: float = 3.0,
    max_colors: int = 6,
    color_merge_distance: float = 56.0,
    pdf_page: int = 1,
    pdf_dpi: int = 180,
    center: bool = True,
    pes_href: str | None = None,
    color_export_action: str | None = None,
    source_name: str | None = None,
) -> tuple[str, tuple[float, float, float, float], dict]:
    pattern = None
    if input_file.suffix.lower() == ".svg" and not svg_needs_rasterization(input_file):
        segments, commands, color_blocks, counts = collect_svg_segments(
            input_file,
            sample_step_mm=sample_step_mm,
            fill_spacing_mm=fill_spacing_mm,
            max_stitch_mm=max_stitch_mm,
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
            color_merge_distance=color_merge_distance,
            fill_spacing_mm=fill_spacing_mm,
            max_stitch_mm=max_stitch_mm,
            pdf_page=pdf_page,
            pdf_dpi=pdf_dpi,
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
        fit_width_mm=fit_width_mm,
        max_colors=max_colors,
        color_merge_distance=color_merge_distance,
        pdf_page=pdf_page,
    )
    return html_text, design_bounds(segments, commands), counts


def write_segments_as_pes(
    segments: list[dict],
    color_blocks: list[dict],
    output_file: Path,
    selected_blocks: set[int] | None = None,
    color_order: list[int] | None = None,
    color_overrides: dict[int, str] | None = None,
) -> None:
    selected_blocks = selected_blocks if selected_blocks is not None else {
        block["index"] for block in color_blocks
    }
    available_blocks = {block["index"] for block in color_blocks}
    if color_order is None:
        ordered_blocks = [block["index"] for block in color_blocks]
    else:
        ordered_blocks = [
            block_index
            for block_index in color_order
            if block_index in available_blocks
        ]
        ordered_blocks.extend(
            block["index"]
            for block in color_blocks
            if block["index"] not in ordered_blocks
        )
    pattern = embroidery.EmbPattern()
    active_block: int | None = None
    previous_point: tuple[float, float] | None = None

    for ordered_block in ordered_blocks:
        if ordered_block not in selected_blocks:
            continue
        for segment in segments:
            block_index = segment["blockIndex"]
            if block_index != ordered_block or segment["kind"] != "stitch":
                continue
            if block_index != active_block:
                block = color_blocks[block_index]
                color = color_overrides.get(block_index, block["color"]) if color_overrides else block["color"]
                pattern.add_thread(make_thread(normalize_hex(color)))
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


def write_filtered_pes(
    input_file: Path,
    output_file: Path,
    selected_blocks: set[int],
    color_order: list[int] | None = None,
    color_overrides: dict[int, str] | None = None,
    *,
    fit_width_mm: float | None = None,
    fit_height_mm: float | None = None,
    sample_step_mm: float = 0.8,
    fill_spacing_mm: float = 0.5,
    max_stitch_mm: float = 3.0,
    max_colors: int = 6,
    color_merge_distance: float = 56.0,
    pdf_page: int = 1,
    pdf_dpi: int = 180,
    center: bool = True,
) -> None:
    if input_file.suffix.lower() == ".svg" and not svg_needs_rasterization(input_file):
        segments, _, color_blocks, _ = collect_svg_segments(
            input_file,
            sample_step_mm=sample_step_mm,
            fill_spacing_mm=fill_spacing_mm,
            max_stitch_mm=max_stitch_mm,
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
    segments, _, color_blocks, _ = group_color_blocks_by_inventory(
        segments,
        [],
        color_blocks,
        {},
    )
    write_segments_as_pes(
        segments,
        color_blocks,
        output_file,
        selected_blocks,
        color_order,
        color_overrides,
    )


def write_svg_as_pes(
    input_file: Path,
    output_file: Path,
    *,
    fit_width_mm: float | None = None,
    fit_height_mm: float | None = None,
    sample_step_mm: float = 0.8,
    fill_spacing_mm: float = 0.5,
    max_stitch_mm: float = 3.0,
    max_colors: int = 6,
    color_merge_distance: float = 56.0,
    pdf_page: int = 1,
    pdf_dpi: int = 180,
    center: bool = True,
) -> None:
    if svg_needs_rasterization(input_file):
        raster_fit_width = fit_width_mm if fit_width_mm is not None else 90.0
        write_image_as_pes(
            input_file,
            output_file,
            fit_width_mm=raster_fit_width,
            fit_height_mm=fit_height_mm,
            max_colors=max_colors,
            color_merge_distance=color_merge_distance,
            fill_spacing_mm=fill_spacing_mm,
            max_stitch_mm=max_stitch_mm,
            pdf_page=pdf_page,
            pdf_dpi=pdf_dpi,
        )
        return
    runs = extract_runs(
        input_file,
        sample_step_mm=sample_step_mm,
        fill_spacing_mm=fill_spacing_mm,
        max_stitch_mm=max_stitch_mm,
    )
    runs = transform_runs(
        runs,
        fit_width_mm=fit_width_mm,
        fit_height_mm=fit_height_mm,
        center=center,
    )
    write_embroidery(runs, output_file)


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
            fill_spacing_mm=args.fill_spacing_mm,
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
                fill_spacing_mm=args.fill_spacing_mm,
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
