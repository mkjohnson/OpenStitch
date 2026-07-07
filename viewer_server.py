from __future__ import annotations

import argparse
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

from image_digitizer import is_raster_source, write_image_as_pes
from pes_viewer import build_viewer_html, positive_float, write_filtered_pes, write_svg_as_pes
from thread_inventory import add_inventory_item, delete_inventory_item, load_inventory, normalize_hex


RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
DATA_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path.cwd()
OUTPUT_DIR = DATA_DIR / "viewer_output"
TEMPLATE_DIR = RESOURCE_DIR / "templates"
STATIC_DIR = RESOURCE_DIR / "static"
MIN_BROTHER_STITCH_MM = 0.5
MAX_BROTHER_EMBROIDERY_STITCH_MM = 7.0


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

        fit_width = 90.0
        if "fit_width_mm" in form and form["fit_width_mm"].value:
            try:
                fit_width = positive_float(form["fit_width_mm"].value)
            except Exception:
                self.send_app_error("Fit width must be greater than zero")
                return
        fill_spacing = 0.15
        if "fill_spacing_mm" in form and form["fill_spacing_mm"].value:
            try:
                fill_spacing = positive_float(form["fill_spacing_mm"].value)
            except Exception:
                self.send_app_error("Fill spacing must be greater than zero")
                return
        if fill_spacing < 0.1 or fill_spacing > 2:
            self.send_app_error("Fill spacing must be between 0.1 and 2 mm")
            return
        try:
            max_stitch = parse_max_stitch(form)
        except ValueError as error:
            self.send_app_error(str(error))
            return
        fill_mode = form["fill_mode"].value if "fill_mode" in form and form["fill_mode"].value else "tatami"
        if fill_mode not in {"tatami", "horizontal"}:
            self.send_app_error("Fill mode must be Tatami or Horizontal")
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

        OUTPUT_DIR.mkdir(exist_ok=True)
        name = safe_name(upload.filename)
        job_id = uuid.uuid4().hex[:10]
        uploaded_path = OUTPUT_DIR / f"{Path(name).stem}_{job_id}{Path(name).suffix.lower()}"
        uploaded_path.write_bytes(upload.file.read())

        html_path = uploaded_path.with_suffix(".html")
        pes_path = uploaded_path.with_suffix(".pes")
        try:
            if uploaded_path.suffix.lower() == ".svg":
                write_svg_as_pes(
                    uploaded_path,
                    pes_path,
                    fit_width_mm=fit_width,
                    fill_mode=fill_mode,
                    fill_angle_deg=fill_angle_deg,
                    fill_spacing_mm=fill_spacing,
                    max_stitch_mm=max_stitch,
                    max_colors=max_colors,
                    color_merge_distance=color_merge_distance,
                    pdf_page=pdf_page,
                )
                pes_href = pes_path.name
                html_text, _, _ = build_viewer_html(
                    uploaded_path,
                    fit_width_mm=fit_width,
                    fill_mode=fill_mode,
                    fill_angle_deg=fill_angle_deg,
                    fill_spacing_mm=fill_spacing,
                    max_stitch_mm=max_stitch,
                    max_colors=max_colors,
                    color_merge_distance=color_merge_distance,
                    pdf_page=pdf_page,
                    pes_href=pes_href,
                    color_export_action="/recreate-pes",
                    source_name=uploaded_path.name,
                )
            elif is_raster_source(uploaded_path):
                write_image_as_pes(
                    uploaded_path,
                    pes_path,
                    fit_width_mm=fit_width,
                    fill_mode=fill_mode,
                    fill_angle_deg=fill_angle_deg,
                    fill_spacing_mm=fill_spacing,
                    max_stitch_mm=max_stitch,
                    max_colors=max_colors,
                    color_merge_distance=color_merge_distance,
                    pdf_page=pdf_page,
                )
                pes_href = pes_path.name
                html_text, _, _ = build_viewer_html(
                    uploaded_path,
                    fit_width_mm=fit_width,
                    fill_mode=fill_mode,
                    fill_angle_deg=fill_angle_deg,
                    fill_spacing_mm=fill_spacing,
                    max_stitch_mm=max_stitch,
                    max_colors=max_colors,
                    color_merge_distance=color_merge_distance,
                    pdf_page=pdf_page,
                    pes_href=pes_href,
                    color_export_action="/recreate-pes",
                    source_name=uploaded_path.name,
                )
            else:
                html_text, _, _ = build_viewer_html(
                    uploaded_path,
                    color_export_action="/recreate-pes",
                    source_name=uploaded_path.name,
                )
        except Exception as error:
            self.send_app_error(str(error))
            return

        html_path.write_text(html_text, encoding="utf-8")
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
        fill_spacing = 0.5
        if "fill_spacing_mm" in form and form["fill_spacing_mm"].value:
            try:
                fill_spacing = positive_float(form["fill_spacing_mm"].value)
            except Exception:
                self.send_app_error("Fill spacing must be greater than zero")
                return
        if fill_spacing < 0.1 or fill_spacing > 2:
            self.send_app_error("Fill spacing must be between 0.1 and 2 mm")
            return
        try:
            max_stitch = parse_max_stitch(form)
        except ValueError as error:
            self.send_app_error(str(error))
            return
        fill_mode = form["fill_mode"].value if "fill_mode" in form and form["fill_mode"].value else "tatami"
        if fill_mode not in {"tatami", "horizontal"}:
            self.send_app_error("Fill mode must be Tatami or Horizontal")
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

        selected_label = "-".join(str(index + 1) for index in sorted(selected_blocks))
        job_id = uuid.uuid4().hex[:10]
        output_path = source_path.with_name(f"{source_path.stem}_blocks_{selected_label}_{job_id}.pes")
        html_path = output_path.with_suffix(".html")
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
                max_stitch_mm=max_stitch,
                max_colors=max_colors,
                color_merge_distance=color_merge_distance,
                pdf_page=pdf_page,
            )
            html_text, _, _ = build_viewer_html(
                output_path,
                pes_href=output_path.name,
                color_export_action="/recreate-pes",
                source_name=output_path.name,
            )
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

        project_stem = safe_name(pes_path.stem).removesuffix(".pes")
        zip_name = f"{project_stem}_project.zip"
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(pes_path, arcname=pes_path.name)
            archive.writestr("thread-shopping-list.txt", shopping_text)

        message = EmailMessage()
        message["Subject"] = f"Embroidery project: {pes_path.stem}"
        message["To"] = ""
        message["From"] = ""
        message.set_content(
            "Attached is the embroidery project ZIP with the PES file and thread shopping list.\n"
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
