from __future__ import annotations

import argparse
import cgi
import html
import json
import re
import uuid
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from image_digitizer import is_raster_source, write_image_as_pes
from pes_viewer import build_viewer_html, positive_float, write_filtered_pes, write_svg_as_pes
from thread_inventory import add_inventory_item, delete_inventory_item, load_inventory, normalize_hex


OUTPUT_DIR = Path("viewer_output")
TEMPLATE_DIR = Path("templates")
STATIC_DIR = Path("static")


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
    first_preview = f"/viewer_output/{html.escape(html_files[0].name, quote=True)}"
    first_title = html.escape(html_files[0].stem)
    for html_file in html_files:
        pes_file = html_file.with_suffix(".pes")
        preview_url = f"/viewer_output/{html.escape(html_file.name, quote=True)}"
        title = html.escape(html_file.stem)
        rows.append(
            '<div class="library-row">'
            f'<div class="file-name"><strong>{html.escape(html_file.stem)}</strong>'
            f'<span>{html.escape(html_file.name)}</span></div>'
            '<div class="library-actions">'
            f'<button class="button secondary preview-button" type="button" data-preview-url="{preview_url}" data-preview-title="{title}">Preview</button>'
            f'<a class="button secondary" href="{preview_url}">Open</a>'
            + (f'<a class="button" href="/viewer_output/{html.escape(pes_file.name, quote=True)}" download>Download PES</a>' if pes_file.exists() else "")
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
        request_path = urlparse(self.path).path
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
        return super().do_GET()

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
            max_colors = int(form["max_colors"].value) if "max_colors" in form and form["max_colors"].value else 6
            pdf_page = int(form["pdf_page"].value) if "pdf_page" in form and form["pdf_page"].value else 1
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
                    fill_spacing_mm=fill_spacing,
                    max_colors=max_colors,
                    color_merge_distance=color_merge_distance,
                    pdf_page=pdf_page,
                )
                pes_href = pes_path.name
                html_text, _, _ = build_viewer_html(
                    uploaded_path,
                    fit_width_mm=fit_width,
                    fill_spacing_mm=fill_spacing,
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
                    fill_spacing_mm=fill_spacing,
                    max_colors=max_colors,
                    color_merge_distance=color_merge_distance,
                    pdf_page=pdf_page,
                )
                pes_href = pes_path.name
                html_text, _, _ = build_viewer_html(
                    uploaded_path,
                    fit_width_mm=fit_width,
                    fill_spacing_mm=fill_spacing,
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
        self.send_header("Location", f"/{html_path.as_posix()}")
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
            max_colors = int(form["max_colors"].value) if "max_colors" in form and form["max_colors"].value else 6
            pdf_page = int(form["pdf_page"].value) if "pdf_page" in form and form["pdf_page"].value else 1
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

        selected_label = "-".join(str(index + 1) for index in sorted(selected_blocks))
        output_path = source_path.with_name(f"{source_path.stem}_blocks_{selected_label}.pes")
        try:
            write_filtered_pes(
                source_path,
                output_path,
                selected_blocks,
                color_order=color_order,
                color_overrides=color_overrides,
                fit_width_mm=fit_width,
                fill_spacing_mm=fill_spacing,
                max_colors=max_colors,
                color_merge_distance=color_merge_distance,
                pdf_page=pdf_page,
            )
        except Exception as error:
            self.send_app_error(str(error))
            return

        download_href = f"/{OUTPUT_DIR.name}/{output_path.name}"
        body = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Recreated PES</title>
  <style>
    :root {{ font-family: Inter, Segoe UI, Arial, sans-serif; color: #172026; background: #f7f8f5; }}
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; padding: 24px; }}
    main {{ width: min(520px, 100%); background: #ffffff; border: 1px solid #d9ded6; border-radius: 8px; padding: 24px; }}
    h1 {{ margin: 0 0 12px; font-size: 24px; }}
    p {{ color: #52605a; }}
    a {{ min-height: 40px; display: inline-flex; align-items: center; justify-content: center; padding: 0 14px; border: 1px solid #2f6f73; border-radius: 6px; background: #2f6f73; color: #ffffff; font-weight: 700; text-decoration: none; }}
  </style>
</head>
<body>
  <main>
    <h1>Recreated PES</h1>
    <p>Saved selected color blocks as <strong>{html.escape(output_path.name)}</strong>.</p>
    <a href="{html.escape(download_href, quote=True)}" download>Download PES</a>
  </main>
</body>
</html>
"""
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

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
