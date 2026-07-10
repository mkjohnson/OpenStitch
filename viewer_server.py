from __future__ import annotations

import argparse
import base64
import cgi
from email.message import EmailMessage
import html
import io
import json
import re
import sys
import uuid
import zipfile
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from image_digitizer import is_raster_source
from pes_viewer import (
    build_viewer_html,
    classify_fill_types,
    positive_float,
    write_filtered_pes,
    write_image_as_pes,
    write_svg_as_pes,
)
from thread_settings import DEFAULT_THREAD_WEIGHT, normalize_thread_weight, recommended_fill_spacing
from thread_inventory import add_inventory_item, delete_inventory_item, load_inventory, normalize_hex


RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
DATA_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path.cwd()
OUTPUT_DIR = DATA_DIR / "viewer_output"
PROJECT_SUFFIX = ".embdproj"
TEMPLATE_DIR = RESOURCE_DIR / "templates"
STATIC_DIR = RESOURCE_DIR / "static"
MIN_BROTHER_STITCH_MM = 0.5
MAX_BROTHER_EMBROIDERY_STITCH_MM = 7.0
BROTHER_DUETTA_MAX_HEIGHT_MM = 300.0
BROTHER_DUETTA_MAX_WIDTH_MM = 180.0
BROTHER_DUETTA_FRAMES = [
    ("small frame", 60.0, 20.0),
    ("medium frame", 100.0, 100.0),
    ("large frame", 130.0, 180.0),
    ("extra large frame", 180.0, 300.0),
]


def safe_name(name: str) -> str:
    stem = Path(name).stem or "design"
    suffix = Path(name).suffix.lower()
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", stem).strip("._") or "design"
    return f"{cleaned}{suffix}"


def render_template(name: str, **context: str) -> bytes:
    text = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
    for key, value in context.items():
        text = text.replace("{{" + key + "}}", value)
    return text.encode("utf-8")


def brother_duetta_frame_note(width_mm: float, height_mm: float) -> str:
    """Return the smallest Duetta frame that can hold the design, or a warning."""
    normal_width = width_mm <= BROTHER_DUETTA_MAX_WIDTH_MM and height_mm <= BROTHER_DUETTA_MAX_HEIGHT_MM
    rotated_width = width_mm <= BROTHER_DUETTA_MAX_HEIGHT_MM and height_mm <= BROTHER_DUETTA_MAX_WIDTH_MM
    if not (normal_width or rotated_width):
        return (
            "Exceeds Brother Duetta design field "
            f"({BROTHER_DUETTA_MAX_WIDTH_MM:.0f} x {BROTHER_DUETTA_MAX_HEIGHT_MM:.0f} mm, "
            "or rotated)."
        )
    for name, frame_width, frame_height in BROTHER_DUETTA_FRAMES:
        if width_mm <= frame_width and height_mm <= frame_height:
            return f"Fits Brother Duetta {name} ({frame_width:.0f} x {frame_height:.0f} mm)."
        if width_mm <= frame_height and height_mm <= frame_width:
            return f"Fits Brother Duetta {name} if rotated ({frame_height:.0f} x {frame_width:.0f} mm)."
    return "Fits Brother Duetta extra large frame if rotated."


def parse_max_stitch(form: cgi.FieldStorage, default: float = 3.0) -> float:
    if "max_stitch_mm" not in form or not form["max_stitch_mm"].value:
        return default
    try:
        max_stitch = positive_float(form["max_stitch_mm"].value)
    except Exception as error:
        raise ValueError("Max stitch length must be greater than zero") from error
    if max_stitch < MIN_BROTHER_STITCH_MM or max_stitch > MAX_BROTHER_EMBROIDERY_STITCH_MM:
        raise ValueError("Max stitch length must be between 0.5 and 7.0 mm")
    return max_stitch


def parse_thread_weight(form: cgi.FieldStorage) -> str:
    value = form["thread_weight"].value if "thread_weight" in form and form["thread_weight"].value else DEFAULT_THREAD_WEIGHT
    return normalize_thread_weight(value)


def parse_fill_spacing(form: cgi.FieldStorage, thread_weight: str, default: float | None = None) -> float:
    fill_spacing = default if default is not None else recommended_fill_spacing(thread_weight)
    if "fill_spacing_mm" in form and form["fill_spacing_mm"].value:
        try:
            fill_spacing = positive_float(form["fill_spacing_mm"].value)
        except Exception as error:
            raise ValueError("Fill spacing must be greater than zero") from error
    if fill_spacing < 0.1 or fill_spacing > 2:
        raise ValueError("Fill spacing must be between 0.1 and 2 mm")
    return fill_spacing


def project_settings(
    *,
    fit_width: float | None,
    fill_spacing: float,
    thread_weight: str,
    max_stitch: float,
    fill_mode: str,
    fill_angle_deg: float,
    max_colors: int,
    color_merge_distance: float,
    pdf_page: int,
    display_units: str = "metric",
    fabric_color: str = "#fbfcfa",
    stitch_perimeter: bool = False,
    perimeter_offset_mm: float = 0.24,
    perimeter_passes: int = 1,
    path_planning: str = "clean_top",
    min_stitch: float = 0.3,
) -> dict:
    display_units = "sae" if display_units == "sae" else "metric"
    fabric_color = fabric_color if re.match(r"^#[0-9A-Fa-f]{6}$", fabric_color or "") else "#fbfcfa"
    perimeter_offset_mm = max(0.0, min(float(perimeter_offset_mm), 1.5))
    perimeter_passes = max(1, min(int(perimeter_passes), 3))
    path_planning = path_planning if path_planning in {"fast", "clean_top", "min_cuts"} else "clean_top"
    min_stitch = max(0.05, min(float(min_stitch), 1.0))
    return {
        "fit_width_mm": fit_width,
        "fill_spacing_mm": fill_spacing,
        "thread_weight": normalize_thread_weight(thread_weight),
        "max_stitch_mm": max_stitch,
        "min_stitch_mm": min_stitch,
        "fill_mode": fill_mode,
        "fill_angle_deg": fill_angle_deg,
        "max_colors": max_colors,
        "color_merge_distance": color_merge_distance,
        "pdf_page": pdf_page,
        "display_units": display_units,
        "fabric_color": fabric_color,
        "stitch_perimeter": bool(stitch_perimeter),
        "perimeter_offset_mm": perimeter_offset_mm,
        "perimeter_passes": perimeter_passes,
        "path_planning": path_planning,
    }


def write_project_file(project_path: Path, source_path: Path, settings: dict, summary_text: str) -> None:
    project = {
        "format": "openstitch-project",
        "version": 1,
        "source_name": source_path.name,
        "source_data_b64": base64.b64encode(source_path.read_bytes()).decode("ascii"),
        "settings": settings,
    }
    with zipfile.ZipFile(project_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("project.json", json.dumps(project, indent=2))
        archive.write(source_path, arcname=f"source/{source_path.name}")
        archive.writestr("project-summary.txt", summary_text)


def estimate_time_text(counts: dict) -> str:
    stitch_seconds = counts.get("needle_points", 0) / 600.0 * 60.0
    jump_seconds = counts.get("jumps", 0) * 0.25
    trim_seconds = counts.get("trims", 0) * 2.0
    color_seconds = counts.get("color_changes", 0) * 25.0
    total_seconds = max(1, int(round(stitch_seconds + jump_seconds + trim_seconds + color_seconds)))
    minutes, seconds = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} hr {minutes} min"
    if minutes:
        return f"{minutes} min {seconds} sec"
    return f"{seconds} sec"


def project_summary_text(
    source_path: Path,
    settings: dict,
    bounds: tuple[float, float, float, float],
    counts: dict,
    segments: list[dict] | None = None,
    color_blocks: list[dict] | None = None,
) -> str:
    min_x, min_y, max_x, max_y = bounds
    width_mm = max_x - min_x
    height_mm = max_y - min_y
    frame_note = brother_duetta_frame_note(width_mm, height_mm)
    fill_types = "Unknown"
    if segments is not None and color_blocks is not None:
        fill_types = classify_fill_types(segments, color_blocks)["summary"]
    lines = [
        "OpenStitch project summary",
        "",
        f"Design: {source_path.name}",
        f"Size: {width_mm:.1f} x {height_mm:.1f} mm",
        f"Machine fit: {frame_note}",
        f"Fill types: {fill_types}",
        f"Estimated stitch time: {estimate_time_text(counts)}",
        "",
        "Stitch metrics",
        f"Needle points: {counts.get('needle_points', 0)}",
        f"Stitch segments: {counts.get('stitch_segments', 0)}",
        f"Jumps: {counts.get('jumps', 0)}",
        f"Trims: {counts.get('trims', 0)}",
        f"Color changes: {counts.get('color_changes', 0)}",
        "",
        "Initial settings",
        f"Fit width: {settings.get('fit_width_mm') or 'original'} mm",
        f"Fill spacing: {settings.get('fill_spacing_mm')} mm",
        f"Thread weight: {settings.get('thread_weight')}",
        f"Max stitch length: {settings.get('max_stitch_mm')} mm",
        f"Fill mode: {settings.get('fill_mode')}",
        f"Fill angle: {settings.get('fill_angle_deg')} deg",
        f"Max colors: {settings.get('max_colors')}",
        f"Color flattening: {settings.get('color_merge_distance')}",
        f"PDF page: {settings.get('pdf_page')}",
        f"Display units: {settings.get('display_units', 'metric')}",
        f"Preview fabric color: {settings.get('fabric_color', '#fbfcfa')}",
        f"Stitch color-block perimeter: {'yes' if settings.get('stitch_perimeter') else 'no'}",
        f"Perimeter offset: {float(settings.get('perimeter_offset_mm', 0.24)):.2f} mm",
        f"Perimeter passes: {int(settings.get('perimeter_passes', 1))}",
        f"Path planning: {settings.get('path_planning', 'clean_top')}",
        "",
    ]
    return "\n".join(lines)


def load_project_upload(upload) -> tuple[str, bytes, dict]:
    raw = upload.file.read()
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            project = json.loads(archive.read("project.json").decode("utf-8"))
    except (KeyError, zipfile.BadZipFile, UnicodeDecodeError, json.JSONDecodeError):
        try:
            project = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Project file could not be read.") from error
    if project.get("format") not in {"openstitch-project", "embroidery-utility-project"}:
        raise ValueError("Project file format was not recognized.")
    source_name = safe_name(str(project.get("source_name") or "project.svg"))
    source_data = project.get("source_data_b64")
    settings = project.get("settings")
    if not isinstance(source_data, str) or not isinstance(settings, dict):
        raise ValueError("Project file is missing source data or settings.")
    try:
        source_bytes = base64.b64decode(source_data, validate=True)
    except ValueError as error:
        raise ValueError("Project source data was invalid.") from error
    return source_name, source_bytes, settings


def coerce_project_settings(settings: dict) -> dict:
    thread_weight = normalize_thread_weight(str(settings.get("thread_weight") or DEFAULT_THREAD_WEIGHT))
    fill_spacing = float(settings.get("fill_spacing_mm", recommended_fill_spacing(thread_weight)))
    if fill_spacing < 0.1 or fill_spacing > 2:
        raise ValueError("Project fill spacing must be between 0.1 and 2 mm")
    max_stitch = float(settings.get("max_stitch_mm", 3.0))
    if max_stitch < MIN_BROTHER_STITCH_MM or max_stitch > MAX_BROTHER_EMBROIDERY_STITCH_MM:
        raise ValueError("Project max stitch length must be between 0.5 and 7.0 mm")
    min_stitch = float(settings.get("min_stitch_mm", 0.3))
    if min_stitch < 0.05 or min_stitch > 1.0:
        raise ValueError("Project min stitch length must be between 0.05 and 1.0 mm")
    fill_mode = str(settings.get("fill_mode") or "tatami")
    if fill_mode not in {"tatami", "horizontal", "crosshatch", "mixed", "outline", "contour"}:
        fill_mode = "tatami"
    fill_angle_deg = float(settings.get("fill_angle_deg", 45.0))
    max_colors = int(settings.get("max_colors", 6))
    color_merge_distance = float(settings.get("color_merge_distance", 56.0))
    pdf_page = int(settings.get("pdf_page", 1))
    display_units = str(settings.get("display_units") or "metric")
    fabric_color = str(settings.get("fabric_color") or "#fbfcfa")
    stitch_perimeter = bool(settings.get("stitch_perimeter", False))
    path_planning = str(settings.get("path_planning") or "clean_top")
    fit_width = settings.get("fit_width_mm", 90.0)
    fit_width = None if fit_width in {"", None} else float(fit_width)
    if fit_width is not None and fit_width <= 0:
        raise ValueError("Project fit width must be greater than zero")
    return project_settings(
        fit_width=fit_width,
        fill_spacing=fill_spacing,
        thread_weight=thread_weight,
        max_stitch=max_stitch,
        min_stitch=min_stitch,
        fill_mode=fill_mode,
        fill_angle_deg=fill_angle_deg,
        max_colors=max_colors,
        color_merge_distance=color_merge_distance,
        pdf_page=pdf_page,
        display_units=display_units,
        fabric_color=fabric_color,
        stitch_perimeter=stitch_perimeter,
        path_planning=path_planning,
    )


def library_data() -> tuple[str, str, str]:
    OUTPUT_DIR.mkdir(exist_ok=True)
    html_files = sorted(OUTPUT_DIR.glob("*.html"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not html_files:
        return (
            '<div class="empty">No generated designs yet.</div>',
            "No Preview",
            '<div class="empty">Convert a design to see a live preview here.</div>',
        )
    rows: list[str] = ['<div class="library">']
    first_preview = f"/viewer_output/{html.escape(html_files[0].name, quote=True)}?embed=thumbnail"
    first_title = html.escape(html_files[0].stem)
    for html_file in html_files:
        pes_file = html_file.with_suffix(".pes")
        full_url = f"/viewer_output/{html.escape(html_file.name, quote=True)}"
        preview_url = f"{full_url}?embed=thumbnail"
        title = html.escape(html_file.stem)
        rows.append(
            '<div class="library-row">'
            f'<div class="file-name"><strong>{html.escape(html_file.stem)}</strong>'
            f'<span>{html.escape(html_file.name)}</span></div>'
            '<div class="library-actions">'
            f'<button class="button secondary compact-action preview-button" type="button" data-preview-url="{preview_url}" data-preview-title="{title}" title="Preview">View</button>'
            f'<a class="button secondary compact-action" href="{full_url}" title="Open full viewer">Open</a>'
            + (f'<a class="button compact-action" href="/viewer_output/{html.escape(pes_file.name, quote=True)}" download title="Download PES">PES</a>' if pes_file.exists() else "")
            + (
                '<form method="post" action="/library/delete">'
                f'<input type="hidden" name="file" value="{html.escape(html_file.name, quote=True)}">'
                '<button class="button danger compact-action" type="submit" title="Delete from library">Delete</button>'
                '</form>'
            )
            + "</div>"
            + "</div>"
        )
    rows.append("</div>")
    preview = (
        f'<iframe class="preview-frame" data-library-preview src="{first_preview}" '
        f'title="Preview {first_title}"></iframe>'
    )
    return "\n".join(rows), first_title, preview


def inventory_markup() -> str:
    items = load_inventory()
    if not items:
        return '<div class="empty">No thread colors saved yet.</div>'
    rows: list[str] = ['<div class="inventory-grid">']
    for item in sorted(items, key=lambda entry: (entry["brand"].lower(), entry["name"].lower(), entry["color"])):
        label = " ".join(part for part in [item["brand"], item["name"]] if part).strip() or item["color"]
        rows.append(
            '<div class="inventory-row">'
            '<div class="inventory-thread">'
            f'<span class="swatch" style="background:{html.escape(item["color"])}"></span>'
            f'<div><strong>{html.escape(label)}</strong>'
            f'<span>{html.escape(item["color"])} - Qty {item["quantity"]}</span></div>'
            '</div>'
            '<form method="post" action="/inventory/delete">'
            f'<input type="hidden" name="id" value="{html.escape(item["id"], quote=True)}">'
            '<button class="button secondary" type="submit">Delete</button>'
            '</form>'
            '</div>'
        )
    rows.append("</div>")
    return "\n".join(rows)


class ViewerHandler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed_url = urlparse(self.path)
        request_path = parsed_url.path
        if request_path in {"/", "/index.html"}:
            self.send_html(render_template("index.html"), no_cache=True)
            return
        if request_path == "/library":
            library, preview_title, preview = library_data()
            self.send_html(
                render_template(
                    "library.html",
                    library=library,
                    preview_title=preview_title,
                    preview=preview,
                ),
                no_cache=True,
            )
            return
        if request_path == "/inventory":
            self.send_html(
                render_template("inventory.html", inventory=inventory_markup()),
                no_cache=True,
            )
            return
        if request_path.startswith("/static/"):
            self.serve_static(request_path)
            return
        if request_path.startswith(f"/{OUTPUT_DIR.name}/") and parsed_url.query == "embed=thumbnail":
            self.serve_viewer_thumbnail(request_path)
            return
        return super().do_GET()

    def serve_viewer_thumbnail(self, request_path: str) -> None:
        relative_name = unquote(request_path.removeprefix(f"/{OUTPUT_DIR.name}/"))
        candidate = (OUTPUT_DIR / relative_name).resolve()
        output_root = OUTPUT_DIR.resolve()
        if candidate.suffix.lower() != ".html" or not candidate.is_file() or output_root not in candidate.parents:
            self.send_error(404, "Preview not found")
            return
        text = candidate.read_text(encoding="utf-8", errors="replace")
        if "thumbnail-mode" not in text:
            thumbnail_head = """
  <style>
    html.thumbnail-mode body {
      display: block !important;
      min-height: 100vh !important;
      overflow: hidden !important;
    }
    html.thumbnail-mode aside,
    html.thumbnail-mode .sidebar-resizer,
    html.thumbnail-mode .thread-floater,
    html.thumbnail-mode .viewer-menu {
      display: none !important;
    }
    html.thumbnail-mode main {
      min-height: 100vh !important;
      padding: 0 !important;
      display: block !important;
    }
    html.thumbnail-mode .stage {
      min-height: 100vh !important;
      height: 100vh !important;
      border: 0 !important;
      border-radius: 0 !important;
      cursor: default !important;
    }
    html.thumbnail-mode canvas {
      min-height: 100vh !important;
      height: 100vh !important;
    }
  </style>
  <script>
    document.documentElement.classList.add("thumbnail-mode");
    document.addEventListener("DOMContentLoaded", () => {
      document.body.classList.add("thumbnail-mode", "hide-jumps", "hide-markers");
    });
  </script>
"""
            text = text.replace("</head>", thumbnail_head + "</head>", 1)
        self.send_html(text.encode("utf-8"), no_cache=True)

    def send_html(self, body: bytes, status: int = 200, no_cache: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if no_cache:
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def send_app_error(self, message: str, status: int = 400) -> None:
        self.send_html(
            render_template("error.html", message=html.escape(message)),
            status=status,
            no_cache=True,
        )

    def render_design_files(
        self,
        source_path: Path,
        html_path: Path,
        pes_path: Path,
        project_path: Path,
        settings: dict,
    ) -> None:
        fit_width = settings["fit_width_mm"]
        fill_spacing = settings["fill_spacing_mm"]
        thread_weight = settings["thread_weight"]
        max_stitch = settings["max_stitch_mm"]
        fill_mode = settings["fill_mode"]
        fill_angle_deg = settings["fill_angle_deg"]
        max_colors = settings["max_colors"]
        color_merge_distance = settings["color_merge_distance"]
        pdf_page = settings["pdf_page"]
        display_units = settings.get("display_units", "metric")
        fabric_color = settings.get("fabric_color", "#fbfcfa")
        stitch_perimeter = bool(settings.get("stitch_perimeter", False))
        path_planning = str(settings.get("path_planning", "clean_top"))
        pes_href = None
        viewer_source = source_path
        if source_path.suffix.lower() == ".svg":
            write_svg_as_pes(
                source_path,
                pes_path,
                fit_width_mm=fit_width,
                fill_mode=fill_mode,
                fill_angle_deg=fill_angle_deg,
                fill_spacing_mm=fill_spacing,
                thread_weight=thread_weight,
                max_stitch_mm=max_stitch,
                max_colors=max_colors,
                color_merge_distance=color_merge_distance,
                pdf_page=pdf_page,
                stitch_perimeter=stitch_perimeter,
                path_planning=path_planning,
            )
            pes_href = pes_path.name
            viewer_source = pes_path
        elif is_raster_source(source_path):
            write_image_as_pes(
                source_path,
                pes_path,
                fit_width_mm=fit_width,
                fill_mode=fill_mode,
                fill_angle_deg=fill_angle_deg,
                fill_spacing_mm=fill_spacing,
                thread_weight=thread_weight,
                max_stitch_mm=max_stitch,
                max_colors=max_colors,
                color_merge_distance=color_merge_distance,
                pdf_page=pdf_page,
                stitch_perimeter=stitch_perimeter,
                path_planning=path_planning,
            )
            pes_href = pes_path.name
            viewer_source = pes_path
        elif source_path.suffix.lower() == ".pes":
            pes_href = source_path.name
            viewer_source = source_path
        html_text, bounds, counts = build_viewer_html(
            viewer_source,
            fit_width_mm=fit_width,
            fill_mode=fill_mode,
            fill_angle_deg=fill_angle_deg,
            fill_spacing_mm=fill_spacing,
            thread_weight=thread_weight,
            max_stitch_mm=max_stitch,
            max_colors=max_colors,
            color_merge_distance=color_merge_distance,
            pdf_page=pdf_page,
            pes_href=pes_href,
            project_href=project_path.name,
            color_export_action="/recreate-pes",
            source_name=viewer_source.name,
            display_units=display_units,
            fabric_color=fabric_color,
            path_planning=path_planning,
        )
        html_path.write_text(html_text, encoding="utf-8")
        project_source = viewer_source
        summary_text = project_summary_text(project_source, settings, bounds, counts)
        project_path.with_suffix(".summary.txt").write_text(summary_text, encoding="utf-8")
        write_project_file(project_path, project_source, settings, summary_text)

    def serve_static(self, request_path: str) -> None:
        relative = unquote(request_path.removeprefix("/static/"))
        static_path = (STATIC_DIR / relative).resolve()
        static_root = STATIC_DIR.resolve()
        if static_root not in static_path.parents or not static_path.exists():
            self.send_error(404)
            return
        if static_path.suffix == ".css":
            content_type = "text/css"
        elif static_path.suffix == ".js":
            content_type = "application/javascript"
        else:
            content_type = "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(static_path.read_bytes())

    def do_POST(self) -> None:
        if self.path == "/recreate-pes":
            self.recreate_pes()
            return
        if self.path == "/inventory/add":
            self.add_thread_inventory()
            return
        if self.path == "/inventory/delete":
            self.delete_thread_inventory()
            return
        if self.path == "/library/delete":
            self.delete_library_item()
            return
        if self.path == "/email-project":
            self.email_project()
            return
        if self.path != "/convert":
            self.send_error(404)
            return

        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            },
        )
        upload = form["design"] if "design" in form else None
        if upload is None or not getattr(upload, "filename", ""):
            self.send_app_error("No file uploaded")
            return

        OUTPUT_DIR.mkdir(exist_ok=True)
        is_project_upload = Path(upload.filename).suffix.lower() == PROJECT_SUFFIX
        if is_project_upload:
            try:
                project_source_name, project_source_bytes, loaded_settings = load_project_upload(upload)
                settings = coerce_project_settings(loaded_settings)
            except ValueError as error:
                self.send_app_error(str(error))
                return
            job_id = uuid.uuid4().hex[:10]
            source_name = safe_name(project_source_name)
            uploaded_path = OUTPUT_DIR / f"{Path(source_name).stem}_{job_id}{Path(source_name).suffix.lower()}"
            uploaded_path.write_bytes(project_source_bytes)
            html_path = uploaded_path.with_suffix(".html")
            pes_path = uploaded_path.with_suffix(".pes")
            project_path = uploaded_path.with_suffix(PROJECT_SUFFIX)
            try:
                self.render_design_files(uploaded_path, html_path, pes_path, project_path, settings)
            except Exception as error:
                self.send_app_error(str(error))
                return
            self.send_response(303)
            self.send_header("Location", f"/{OUTPUT_DIR.name}/{html_path.name}")
            self.end_headers()
            return

        fit_width = 90.0
        if "fit_width_mm" in form and form["fit_width_mm"].value:
            try:
                fit_width = positive_float(form["fit_width_mm"].value)
            except Exception:
                self.send_app_error("Fit width must be greater than zero")
                return
        thread_weight = parse_thread_weight(form)
        try:
            fill_spacing = parse_fill_spacing(form, thread_weight)
        except ValueError as error:
            self.send_app_error(str(error))
            return
        try:
            max_stitch = parse_max_stitch(form)
        except ValueError as error:
            self.send_app_error(str(error))
            return
        fill_mode = form["fill_mode"].value if "fill_mode" in form and form["fill_mode"].value else "tatami"
        if fill_mode not in {"tatami", "horizontal", "crosshatch", "mixed", "outline", "contour"}:
            self.send_app_error("Fill mode must be Mixed, Contour, Tatami, Crosshatch, Horizontal, or Outline")
            return
        try:
            max_colors = int(form["max_colors"].value) if "max_colors" in form and form["max_colors"].value else 6
            pdf_page = int(form["pdf_page"].value) if "pdf_page" in form and form["pdf_page"].value else 1
            fill_angle_deg = float(form["fill_angle_deg"].value) if "fill_angle_deg" in form and form["fill_angle_deg"].value else 45.0
            color_merge_distance = (
                float(form["color_merge_distance"].value)
                if "color_merge_distance" in form and form["color_merge_distance"].value
                else 56.0
            )
        except ValueError:
            self.send_app_error("Max colors, color flattening, and PDF page must be valid numbers")
            return
        if max_colors < 2 or max_colors > 16:
            self.send_app_error("Max colors must be between 2 and 16")
            return
        if color_merge_distance < 0 or color_merge_distance > 255:
            self.send_app_error("Color flattening must be between 0 and 255")
            return
        if fill_angle_deg < -90 or fill_angle_deg > 90:
            self.send_app_error("Fill angle must be between -90 and 90 degrees")
            return
        if pdf_page < 1:
            self.send_app_error("PDF page must be 1 or greater")
            return
        display_units = form["display_units"].value if "display_units" in form and form["display_units"].value else "metric"
        fabric_color = form["fabric_color"].value if "fabric_color" in form and form["fabric_color"].value else "#fbfcfa"
        stitch_perimeter = "stitch_perimeter" in form and form["stitch_perimeter"].value not in {"", "0", "false", "False"}

        name = safe_name(upload.filename)
        job_id = uuid.uuid4().hex[:10]
        uploaded_path = OUTPUT_DIR / f"{Path(name).stem}_{job_id}{Path(name).suffix.lower()}"
        uploaded_path.write_bytes(upload.file.read())

        html_path = uploaded_path.with_suffix(".html")
        pes_path = uploaded_path.with_suffix(".pes")
        project_path = uploaded_path.with_suffix(PROJECT_SUFFIX)
        settings = project_settings(
            fit_width=fit_width,
            fill_spacing=fill_spacing,
            thread_weight=thread_weight,
            max_stitch=max_stitch,
            fill_mode=fill_mode,
            fill_angle_deg=fill_angle_deg,
            max_colors=max_colors,
            color_merge_distance=color_merge_distance,
            pdf_page=pdf_page,
            display_units=display_units,
            fabric_color=fabric_color,
            stitch_perimeter=stitch_perimeter,
        )
        try:
            self.render_design_files(uploaded_path, html_path, pes_path, project_path, settings)
        except Exception as error:
            self.send_app_error(str(error))
            return

        self.send_response(303)
        self.send_header("Location", f"/{OUTPUT_DIR.name}/{html_path.name}")
        self.end_headers()

    def recreate_pes(self) -> None:
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            },
        )
        source_name = Path(form["source"].value).name if "source" in form else ""
        source_path = (OUTPUT_DIR / source_name).resolve()
        output_root = OUTPUT_DIR.resolve()
        if not source_name or output_root not in source_path.parents or not source_path.exists():
            self.send_app_error("Source file not found")
            return

        selected_text = form["selected_blocks"].value if "selected_blocks" in form else ""
        try:
            selected_blocks = {
                int(value)
                for value in selected_text.split(",")
                if value.strip() != ""
            }
        except ValueError:
            self.send_app_error("Selected color blocks were invalid")
            return
        if not selected_blocks:
            self.send_app_error("Select at least one color block")
            return
        try:
            color_order = [
                int(value)
                for value in (form["color_order"].value if "color_order" in form else "").split(",")
                if value.strip() != ""
            ]
        except ValueError:
            self.send_app_error("Color order was invalid")
            return
        try:
            raw_overrides = json.loads(form["color_overrides"].value) if "color_overrides" in form and form["color_overrides"].value else {}
            color_overrides = {
                int(block_index): normalize_hex(str(color))
                for block_index, color in raw_overrides.items()
            }
        except (TypeError, ValueError, json.JSONDecodeError):
            self.send_app_error("Color overrides were invalid")
            return
        try:
            raw_label_overrides = (
                json.loads(form["thread_label_overrides"].value)
                if "thread_label_overrides" in form and form["thread_label_overrides"].value
                else {}
            )
            thread_label_overrides = {
                int(block_index): str(label).strip()
                for block_index, label in raw_label_overrides.items()
                if str(label).strip()
            }
        except (TypeError, ValueError, json.JSONDecodeError):
            self.send_app_error("Thread label overrides were invalid")
            return

        fit_width = None
        if "fit_width_mm" in form and form["fit_width_mm"].value:
            try:
                fit_width = positive_float(form["fit_width_mm"].value)
            except Exception:
                self.send_app_error("Fit width must be greater than zero")
                return
        thread_weight = parse_thread_weight(form)
        try:
            fill_spacing = parse_fill_spacing(form, thread_weight, default=0.5)
        except ValueError as error:
            self.send_app_error(str(error))
            return
        try:
            max_stitch = parse_max_stitch(form)
        except ValueError as error:
            self.send_app_error(str(error))
            return
        fill_mode = form["fill_mode"].value if "fill_mode" in form and form["fill_mode"].value else "tatami"
        if fill_mode not in {"tatami", "horizontal", "crosshatch", "mixed", "outline", "contour"}:
            self.send_app_error("Fill mode must be Mixed, Contour, Tatami, Crosshatch, Horizontal, or Outline")
            return
        try:
            max_colors = int(form["max_colors"].value) if "max_colors" in form and form["max_colors"].value else 6
            pdf_page = int(form["pdf_page"].value) if "pdf_page" in form and form["pdf_page"].value else 1
            fill_angle_deg = float(form["fill_angle_deg"].value) if "fill_angle_deg" in form and form["fill_angle_deg"].value else 45.0
            color_merge_distance = (
                float(form["color_merge_distance"].value)
                if "color_merge_distance" in form and form["color_merge_distance"].value
                else 56.0
            )
        except ValueError:
            self.send_app_error("Max colors, color flattening, and PDF page must be valid numbers")
            return
        if color_merge_distance < 0 or color_merge_distance > 255:
            self.send_app_error("Color flattening must be between 0 and 255")
            return
        if fill_angle_deg < -90 or fill_angle_deg > 90:
            self.send_app_error("Fill angle must be between -90 and 90 degrees")
            return
        stitch_perimeter = "stitch_perimeter" in form and form["stitch_perimeter"].value not in {"", "0", "false", "False"}

        selected_label = "-".join(str(index + 1) for index in sorted(selected_blocks))
        job_id = uuid.uuid4().hex[:10]
        output_path = source_path.with_name(f"{source_path.stem}_blocks_{selected_label}_{job_id}.pes")
        html_path = output_path.with_suffix(".html")
        project_path = output_path.with_suffix(PROJECT_SUFFIX)
        settings = project_settings(
            fit_width=fit_width,
            fill_spacing=fill_spacing,
            thread_weight=thread_weight,
            max_stitch=max_stitch,
            fill_mode=fill_mode,
            fill_angle_deg=fill_angle_deg,
            max_colors=max_colors,
            color_merge_distance=color_merge_distance,
            pdf_page=pdf_page,
            stitch_perimeter=stitch_perimeter,
        )
        try:
            write_filtered_pes(
                source_path,
                output_path,
                selected_blocks,
                color_order=color_order,
                color_overrides=color_overrides,
                thread_label_overrides=thread_label_overrides,
                fit_width_mm=fit_width,
                fill_mode=fill_mode,
                fill_angle_deg=fill_angle_deg,
                fill_spacing_mm=fill_spacing,
                thread_weight=thread_weight,
                max_stitch_mm=max_stitch,
                max_colors=max_colors,
                color_merge_distance=color_merge_distance,
                pdf_page=pdf_page,
                stitch_perimeter=stitch_perimeter,
            )
            html_text, bounds, counts = build_viewer_html(
                output_path,
                pes_href=output_path.name,
                project_href=project_path.name,
                color_export_action="/recreate-pes",
                source_name=output_path.name,
                fill_spacing_mm=fill_spacing,
                thread_weight=thread_weight,
                max_stitch_mm=max_stitch,
                stitch_perimeter=stitch_perimeter,
            )
            summary_text = project_summary_text(output_path, settings, bounds, counts)
            project_path.with_suffix(".summary.txt").write_text(summary_text, encoding="utf-8")
            write_project_file(project_path, output_path, settings, summary_text)
        except Exception as error:
            self.send_app_error(str(error))
            return

        html_path.write_text(html_text, encoding="utf-8")
        self.send_response(303)
        self.send_header("Location", f"/{OUTPUT_DIR.name}/{html_path.name}")
        self.end_headers()

    def add_thread_inventory(self) -> None:
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            },
        )
        try:
            quantity = int(form["quantity"].value) if "quantity" in form and form["quantity"].value else 1
            add_inventory_item(
                brand=form["brand"].value if "brand" in form else "",
                name=form["name"].value if "name" in form else "",
                color=form["color"].value if "color" in form else "",
                quantity=quantity,
            )
        except Exception as error:
            self.send_app_error(str(error))
            return
        self.send_response(303)
        self.send_header("Location", "/inventory")
        self.end_headers()

    def delete_thread_inventory(self) -> None:
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            },
        )
        item_id = form["id"].value if "id" in form else ""
        if item_id:
            delete_inventory_item(item_id)
        self.send_response(303)
        self.send_header("Location", "/inventory")
        self.end_headers()

    def delete_library_item(self) -> None:
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            },
        )
        file_name = form["file"].value if "file" in form and form["file"].value else ""
        target = (OUTPUT_DIR / Path(file_name).name).resolve()
        output_root = OUTPUT_DIR.resolve()
        if target.suffix.lower() != ".html" or output_root not in target.parents:
            self.send_app_error("Library item was invalid")
            return
        for path in OUTPUT_DIR.glob(f"{target.stem}.*"):
            candidate = path.resolve()
            if output_root in candidate.parents and candidate.is_file():
                candidate.unlink()
        self.send_response(303)
        self.send_header("Location", "/library")
        self.end_headers()

    def email_project(self) -> None:
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            },
        )
        pes_name = Path(form["pes_file"].value).name if "pes_file" in form else ""
        pes_path = (OUTPUT_DIR / pes_name).resolve()
        output_root = OUTPUT_DIR.resolve()
        if not pes_name or output_root not in pes_path.parents or pes_path.suffix.lower() != ".pes" or not pes_path.exists():
            self.send_app_error("PES file not found")
            return

        shopping_text = form["shopping_list"].value if "shopping_list" in form else ""
        if not shopping_text.strip():
            shopping_text = "Thread shopping list\n\nAll design colors have close inventory matches.\n"
        recipient_email = form["recipient_email"].value.strip() if "recipient_email" in form else ""

        project_stem = safe_name(pes_path.stem).removesuffix(".pes")
        zip_name = f"{project_stem}_project.zip"
        summary_path = pes_path.with_suffix(".summary.txt")
        if summary_path.exists():
            summary_text = summary_path.read_text(encoding="utf-8", errors="replace")
        else:
            summary_text = f"OpenStitch project summary\n\nDesign: {pes_path.name}\n"
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(pes_path, arcname=pes_path.name)
            archive.writestr("thread-shopping-list.txt", shopping_text)
            archive.writestr("project-summary.txt", summary_text)

        message = EmailMessage()
        message["Subject"] = f"OpenStitch project: {pes_path.stem}"
        if recipient_email:
            message["To"] = recipient_email
        message["X-Unsent"] = "1"
        message.set_content(
            "Attached is the OpenStitch project ZIP with the PES file, thread shopping list, and project summary.\n"
        )
        message.add_attachment(
            zip_buffer.getvalue(),
            maintype="application",
            subtype="zip",
            filename=zip_name,
        )
        body = message.as_bytes()
        eml_name = f"{project_stem}_email_project.eml"
        self.send_response(200)
        self.send_header("Content-Type", "message/rfc822")
        self.send_header("Content-Disposition", f'attachment; filename="{eml_name}"')
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.end_headers()
        self.wfile.write(body)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a local SVG/PES import server for the embroidery viewer.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    server = ThreadingHTTPServer((args.host, args.port), ViewerHandler)
    print(f"Open http://{args.host}:{args.port}/")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
