from __future__ import annotations

import base64
import copy
from email.message import EmailMessage
import html
import json
import math
import os
import shutil
import sys
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pyembroidery as embroidery
from PIL import Image, ImageColor, ImageDraw, ImageFilter
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDockWidget,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from image_digitizer import image_to_segments, is_raster_source, svg_needs_rasterization
from pes_viewer import (
    apply_thread_metadata,
    add_perimeter_segments,
    collect_segments,
    collect_svg_segments,
    design_bounds,
    estimate_stitch_time,
    estimate_thread_usage,
    classify_fill_types,
    group_color_blocks_by_inventory,
    normalize_positive_coordinates,
    thread_metadata_path,
    write_segments_as_pes,
)
from thread_catalog import available_thread_brands, load_thread_catalog
from thread_inventory import add_inventory_item, delete_inventory_item, load_inventory, normalize_hex
from thread_settings import DEFAULT_THREAD_WEIGHT, recommended_fill_spacing, thread_diameter_mm
from viewer_server import project_settings, project_summary_text, safe_name, write_project_file
from viewer_server import (
    BROTHER_DUETTA_MAX_HEIGHT_MM,
    BROTHER_DUETTA_MAX_WIDTH_MM,
    brother_duetta_frame_note,
)


OUTPUT_DIR = Path.cwd() / "viewer_output"
PROJECT_SUFFIX = ".embdproj"


def resource_path(relative_path: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative_path


@dataclass
class DesignState:
    source_path: Path
    working_source: Path
    pes_path: Path
    project_path: Path
    settings: dict
    segments: list[dict]
    commands: list[dict]
    color_blocks: list[dict]
    counts: dict
    bounds: tuple[float, float, float, float]


def clamp_channel(value: float) -> int:
    return max(0, min(255, int(round(value))))


def blend_rgb(color: tuple[int, int, int], other: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    return tuple(clamp_channel(channel + (other[index] - channel) * amount) for index, channel in enumerate(color))


def realistic_preview_image(
    segments: list[dict],
    bounds: tuple[float, float, float, float],
    *,
    fabric_color: str,
    thread_weight: str,
    selected_blocks: set[int] | None = None,
    max_width_px: int = 2600,
    include_hoop: bool = False,
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
            [hoop_margin + hoop_width, hoop_margin + hoop_width, width - hoop_margin - hoop_width, height - hoop_margin - hoop_width],
            radius=max(14, int(round(scale * 2.0))),
            outline=(*light, 150),
            width=max(1, hoop_width),
        )

    nominal_thread_width = max(2, int(round(thread_diameter_mm(thread_weight) * scale)))
    # Embroidery thread is round on the spool but flattens under stitch tension.
    # Use the nominal diameter for the highlight and a wider coverage pass so
    # photo exports do not show artificial fabric gaps between dense fill rows.
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
            [(start[0] + coverage_width * 0.36, start[1] + coverage_width * 0.44), (end[0] + coverage_width * 0.36, end[1] + coverage_width * 0.44)],
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


class StitchCanvas(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.segments: list[dict] = []
        self.commands: list[dict] = []
        self.bounds = (-10.0, -10.0, 10.0, 10.0)
        self.max_step = 0
        self.current_step = 0
        self.zoom = 1.0
        self.pan = QPointF(0, 0)
        self.show_jumps = False
        self.show_points = False
        self.show_markers = False
        self.edit_mode = False
        self.on_add_stitch = None
        self.on_delete_stitch = None
        self.background_color = "#fbfcfa"
        self.measurement_units = "metric"
        self.visible_blocks: set[int] | None = None
        self._drag_start: QPoint | None = None
        self._drag_pan = QPointF(0, 0)
        self.setMinimumSize(560, 420)
        self.setMouseTracking(True)

    def set_design(
        self,
        segments: list[dict],
        commands: list[dict],
        bounds: tuple[float, float, float, float],
    ) -> None:
        self.segments = segments
        self.commands = commands
        self.bounds = bounds
        self.max_step = max((segment.get("step", 0) for segment in segments), default=0)
        self.current_step = self.max_step
        self.visible_blocks = None
        self.zoom = 1.0
        self.pan = QPointF(0, 0)
        self.update()

    def set_visible_blocks(self, visible_blocks: set[int] | None) -> None:
        self.visible_blocks = set(visible_blocks) if visible_blocks is not None else None
        self.update()

    def set_step(self, step: int) -> None:
        self.current_step = max(0, min(self.max_step, int(step)))
        self.update()

    def reset_view(self) -> None:
        self.zoom = 1.0
        self.pan = QPointF(0, 0)
        self.update()

    def set_background_color(self, color: str) -> None:
        self.background_color = color
        self.update()

    def set_measurement_units(self, units: str) -> None:
        self.measurement_units = "sae" if units == "sae" else "metric"
        self.update()

    def set_edit_mode(self, enabled: bool) -> None:
        self.edit_mode = bool(enabled)
        self.setCursor(Qt.CrossCursor if self.edit_mode else Qt.ArrowCursor)

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt override
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.zoom = max(0.12, min(30.0, self.zoom * factor))
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self.edit_mode:
            point = self._design_point(event.pos())
            if event.button() == Qt.LeftButton and self.on_add_stitch:
                self.on_add_stitch(point)
                return
            if event.button() == Qt.RightButton and self.on_delete_stitch:
                self.on_delete_stitch(point)
                return
        if event.button() == Qt.LeftButton:
            self._drag_start = event.pos()
            self._drag_pan = QPointF(self.pan)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self.edit_mode:
            return
        if self._drag_start is None:
            return
        delta = event.pos() - self._drag_start
        self.pan = self._drag_pan + QPointF(delta.x(), delta.y())
        self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.LeftButton:
            self._drag_start = None

    def _transform(self) -> tuple[float, float, float]:
        min_x, min_y, max_x, max_y = self.bounds
        design_w = max(max_x - min_x, 1.0)
        design_h = max(max_y - min_y, 1.0)
        margin = 32
        scale = min(
            max((self.width() - margin * 2) / design_w, 0.01),
            max((self.height() - margin * 2) / design_h, 0.01),
        ) * self.zoom
        offset_x = (self.width() - design_w * scale) / 2 - min_x * scale + self.pan.x()
        offset_y = (self.height() - design_h * scale) / 2 - min_y * scale + self.pan.y()
        return scale, offset_x, offset_y

    def _point(self, x: float, y: float) -> QPointF:
        scale, offset_x, offset_y = self._transform()
        return QPointF(x * scale + offset_x, y * scale + offset_y)

    def _design_point(self, point: QPoint) -> tuple[float, float]:
        scale, offset_x, offset_y = self._transform()
        return ((point.x() - offset_x) / scale, (point.y() - offset_y) / scale)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(self.rect(), QColor(self.background_color))
        self._draw_grid(painter)
        if not self.segments:
            painter.setPen(QColor("#52605a"))
            painter.drawText(self.rect(), Qt.AlignCenter, "Import a design to preview stitches")
            return
        self._draw_segments(painter)
        if self.show_points:
            self._draw_needle_points(painter)
        if self.show_markers:
            self._draw_markers(painter)
        self._draw_playback_needle(painter)

    def _draw_grid(self, painter: QPainter) -> None:
        min_x, min_y, max_x, max_y = self.bounds
        scale, offset_x, offset_y = self._transform()
        grid_color, label_color = self._grid_colors()
        painter.setPen(QPen(grid_color, 1))
        step = self._grid_step(scale)
        start_x = math.floor(min_x / step) * step
        end_x = math.ceil(max_x / step) * step
        start_y = math.floor(min_y / step) * step
        end_y = math.ceil(max_y / step) * step
        painter.setFont(self.font())
        x = start_x
        while x <= end_x:
            sx = x * scale + offset_x
            painter.drawLine(QPointF(sx, 0), QPointF(sx, self.height()))
            if 4 <= sx <= self.width() - 48:
                painter.setPen(label_color)
                painter.drawText(QPointF(sx + 4, 16), self._format_measure(x))
                painter.setPen(QPen(grid_color, 1))
            x += step
        y = start_y
        while y <= end_y:
            sy = y * scale + offset_y
            painter.drawLine(QPointF(0, sy), QPointF(self.width(), sy))
            if 22 <= sy <= self.height() - 8:
                painter.setPen(label_color)
                painter.drawText(QPointF(6, sy + 14), self._format_measure(y))
                painter.setPen(QPen(grid_color, 1))
            y += step

    def _grid_colors(self) -> tuple[QColor, QColor]:
        background = QColor(self.background_color)
        brightness = background.red() * 0.299 + background.green() * 0.587 + background.blue() * 0.114
        if brightness < 128:
            return QColor(255, 255, 255, 42), QColor(255, 255, 255, 200)
        return QColor("#edf0ec"), QColor("#5c6b63")

    def _grid_step(self, scale: float) -> float:
        candidates = [3.175, 6.35, 12.7, 25.4, 50.8, 101.6] if self.measurement_units == "sae" else [1, 2, 5, 10, 20, 50, 100]
        for step in candidates:
            if step * scale >= 44:
                return step
        return candidates[-1]

    def _format_measure(self, value_mm: float) -> str:
        if self.measurement_units == "sae":
            value = value_mm / 25.4
            return f"{value:.3f} in" if abs(value) < 1 else f"{value:.2f} in"
        return f"{round(value_mm)} mm"

    def _draw_segments(self, painter: QPainter) -> None:
        scale, _, _ = self._transform()
        stitch_width = max(1, min(3, int(round(scale * 0.08))))
        for segment in self.segments:
            if segment.get("step", 0) > self.current_step:
                continue
            if self.visible_blocks is not None and segment["blockIndex"] not in self.visible_blocks:
                continue
            kind = segment["kind"]
            if kind == "travel_after_trim":
                continue
            if kind != "stitch" and not self.show_jumps:
                continue
            if kind == "stitch":
                color = QColor(segment["color"])
                pen = QPen(color, stitch_width)
            elif kind == "travel_after_color_change":
                pen = QPen(QColor("#2b7fff"), 1, Qt.DashLine)
            else:
                pen = QPen(QColor("#a6aaa5"), 1, Qt.DotLine)
            painter.setPen(pen)
            start = self._point(segment["x1"], segment["y1"])
            end = self._point(segment["x2"], segment["y2"])
            painter.drawLine(start, end)

    def _draw_needle_points(self, painter: QPainter) -> None:
        scale, _, _ = self._transform()
        stitch_width = max(1, min(3, int(round(scale * 0.08))))
        point_radius = max(2.0, stitch_width * 2.0)
        painter.setPen(QPen(QColor("#172026"), max(0.6, stitch_width * 0.45)))
        for segment in self.segments:
            if segment.get("step", 0) > self.current_step:
                continue
            if segment.get("kind") != "stitch":
                continue
            if self.visible_blocks is not None and segment["blockIndex"] not in self.visible_blocks:
                continue
            point = self._point(segment["x2"], segment["y2"])
            painter.setBrush(QColor(segment["color"]))
            painter.drawEllipse(point, point_radius, point_radius)

    def _draw_markers(self, painter: QPainter) -> None:
        for command in self.commands:
            if command.get("step", 0) > self.current_step:
                continue
            if self.visible_blocks is not None and command.get("color") not in self.visible_blocks:
                continue
            if command.get("command") not in {"trim", "color_change"}:
                continue
            point = self._point(command["x"], command["y"])
            color = QColor("#ff8a3d") if command["command"] == "trim" else QColor("#2b7fff")
            painter.setBrush(color)
            painter.setPen(QPen(QColor("#172026"), 1))
            painter.drawEllipse(point, 4, 4)

    def _current_needle_segment(self) -> dict | None:
        best: dict | None = None
        best_step = -1
        for segment in self.segments:
            if segment.get("kind") != "stitch":
                continue
            step = int(segment.get("step", 0))
            if step > self.current_step or step < best_step:
                continue
            if self.visible_blocks is not None and segment["blockIndex"] not in self.visible_blocks:
                continue
            best = segment
            best_step = step
        return best

    def _draw_playback_needle(self, painter: QPainter) -> None:
        if self.current_step <= 0:
            return
        segment = self._current_needle_segment()
        if segment is None:
            return
        scale, _, _ = self._transform()
        point = self._point(segment["x2"], segment["y2"])
        size = max(8.0, min(22.0, scale * 0.42))
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(QPen(QColor(0, 0, 0, 95), max(2, int(size * 0.22))))
        painter.drawLine(QPointF(point.x() + size * 0.28, point.y() - size * 2.0), QPointF(point.x() + size * 0.28, point.y() + size * 0.35))
        painter.setPen(QPen(QColor("#d8dde2"), max(2, int(size * 0.18))))
        painter.drawLine(QPointF(point.x(), point.y() - size * 2.15), QPointF(point.x(), point.y() + size * 0.25))
        painter.setPen(QPen(QColor("#5e6770"), max(1, int(size * 0.06))))
        painter.drawLine(QPointF(point.x(), point.y() - size * 2.15), QPointF(point.x(), point.y() + size * 0.25))
        painter.setBrush(QColor(segment.get("color", "#2b7fff")))
        painter.setPen(QPen(QColor("#172026"), 1))
        painter.drawEllipse(point, size * 0.34, size * 0.34)
        painter.setBrush(QColor("#f7fbff"))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(QPointF(point.x() - size * 0.10, point.y() - size * 0.12), size * 0.08, size * 0.08)
        painter.restore()


class ThreadRow(QWidget):
    def __init__(self, block: dict, catalog: list[dict], on_changed, on_add_inventory) -> None:
        super().__init__()
        self.block = block
        self.catalog = catalog
        self.on_changed = on_changed
        self.on_add_inventory = on_add_inventory
        self._syncing = False
        self.setObjectName("threadRow")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        self.setStyleSheet("#threadRow { border: 1px solid #3a3a3a; border-radius: 6px; }")

        header = QHBoxLayout()
        self.checkbox = QCheckBox()
        self.checkbox.setChecked(True)
        self.checkbox.stateChanged.connect(on_changed)
        self.swatch = QPushButton()
        self.swatch.setFixedSize(34, 28)
        self.swatch.clicked.connect(self.choose_color)
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        header.addWidget(self.checkbox)
        header.addWidget(self.swatch)
        header.addWidget(self.label, 1)
        layout.addLayout(header)

        edit_row = QHBoxLayout()
        self.hex_input = QLineEdit()
        self.hex_input.setMinimumWidth(84)
        self.hex_input.editingFinished.connect(self.apply_hex)
        self.thread_select = QComboBox()
        self.thread_select.setMinimumWidth(160)
        self.thread_select.addItem("Known threads", "")
        for item in catalog:
            label = f"{item['brand']} {item['number']} {item['name']}"
            self.thread_select.addItem(label, dict(item))
        self.thread_select.currentIndexChanged.connect(self.apply_thread_choice)
        edit_row.addWidget(self.hex_input)
        edit_row.addWidget(self.thread_select, 1)
        layout.addLayout(edit_row)

        actions = QHBoxLayout()
        edit_button = QPushButton("Edit")
        edit_button.clicked.connect(self.choose_color)
        add_inventory = QPushButton("Add to Inventory")
        add_inventory.clicked.connect(lambda: self.on_add_inventory(self))
        actions.addStretch(1)
        actions.addWidget(edit_button)
        actions.addWidget(add_inventory)
        layout.addLayout(actions)
        self.refresh()

    def refresh(self) -> None:
        color = self.block["color"]
        self.swatch.setStyleSheet(f"background:{color}; border:1px solid #89958f;")
        if self.hex_input.text().lower() != color.lower():
            self.hex_input.setText(color)
        self.label.setText(
            f"Block {self.block['index'] + 1}: {self.block.get('label', color)}\n"
            f"{color} - {self.block.get('stitches', 0)} stitches"
        )

    def set_color(self, color: str, label: str | None = None) -> None:
        try:
            normalized = normalize_hex(color)
        except ValueError:
            QMessageBox.warning(self, "OpenStitch", "Thread color must be a valid hex color like #ffcc00.")
            self.hex_input.setText(self.block["color"])
            return
        self.block["color"] = normalized
        if label:
            self.block["label"] = label
        self.refresh()
        self.on_changed()

    def choose_color(self) -> None:
        chosen = QColorDialog.getColor(QColor(self.block["color"]), self, "Choose thread color")
        if not chosen.isValid():
            return
        self.set_color(chosen.name())

    def apply_hex(self) -> None:
        self.set_color(self.hex_input.text())

    def apply_thread_choice(self) -> None:
        item = self.thread_select.currentData()
        if not item:
            return
        if isinstance(item, dict):
            self.set_color(str(item["color"]), self.thread_select.currentText())
        else:
            self.set_color(str(item), self.thread_select.currentText())

    def inventory_details(self, fallback_brand: str) -> tuple[str, str, str]:
        item = self.thread_select.currentData()
        if isinstance(item, dict) and item.get("color"):
            return (
                str(item.get("brand") or fallback_brand),
                f"{item.get('number', '')} {item.get('name', '')}".strip(),
                self.block["color"],
            )
        return fallback_brand, self.block.get("label", self.block["color"]), self.block["color"]


class OpenStitchWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("OpenStitch")
        icon_path = resource_path("static/openstitch.ico")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        self.resize(1320, 820)
        OUTPUT_DIR.mkdir(exist_ok=True)
        self.state: DesignState | None = None
        self.thread_rows: list[ThreadRow] = []
        self.baseline_snapshot: dict | None = None
        self.undo_stack: list[dict] = []
        self._loading_settings = False
        self.play_timer = QTimer(self)
        self.play_timer.timeout.connect(self.advance_playback)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setSingleShot(True)
        self.refresh_timer.setInterval(350)
        self.refresh_timer.timeout.connect(self.refresh_current_design)
        self.catalog = load_thread_catalog()
        self._build_ui()
        self._connect_setting_refresh()
        self.refresh_library()

    def _build_ui(self) -> None:
        self._build_menu()

        left = QTabWidget()
        left.setMinimumWidth(310)
        left.addTab(self._conversion_tab(), "Convert")
        left.addTab(self._library_tab(), "Library")
        left.addTab(self._inventory_tab(), "Inventory")
        left.addTab(self._settings_tab(), "Settings")

        self.canvas = StitchCanvas()
        self.canvas.on_add_stitch = self.add_manual_stitch
        self.canvas.on_delete_stitch = self.delete_nearest_stitch
        preview = QWidget()
        preview_layout = QVBoxLayout(preview)
        preview_layout.setContentsMargins(0, 0, 0, 0)
        preview_layout.setSpacing(0)
        preview_layout.addWidget(self.canvas, 1)
        preview_layout.addWidget(self._playback_bar(), 0, Qt.AlignHCenter)
        self.setCentralWidget(preview)

        right = self._thread_panel()
        right.setMinimumWidth(320)
        view_options = self._view_options_panel()
        view_options.setMinimumWidth(240)
        report = self._report_panel()
        report.setMinimumWidth(300)
        self.left_dock = QDockWidget("Workspace", self)
        self.left_dock.setObjectName("workspaceDock")
        self.left_dock.setWidget(left)
        self.left_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.left_dock.setFeatures(
            QDockWidget.DockWidgetMovable
            | QDockWidget.DockWidgetFloatable
            | QDockWidget.DockWidgetClosable
        )
        self.addDockWidget(Qt.LeftDockWidgetArea, self.left_dock)

        self.right_dock = QDockWidget("Threads", self)
        self.right_dock.setObjectName("threadsDock")
        self.right_dock.setWidget(right)
        self.right_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        self.right_dock.setFeatures(
            QDockWidget.DockWidgetMovable
            | QDockWidget.DockWidgetFloatable
            | QDockWidget.DockWidgetClosable
        )
        self.addDockWidget(Qt.RightDockWidgetArea, self.right_dock)
        self.options_dock = QDockWidget("View Options", self)
        self.options_dock.setObjectName("viewOptionsDock")
        self.options_dock.setWidget(view_options)
        self.options_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea | Qt.BottomDockWidgetArea)
        self.options_dock.setFeatures(
            QDockWidget.DockWidgetMovable
            | QDockWidget.DockWidgetFloatable
            | QDockWidget.DockWidgetClosable
        )
        self.addDockWidget(Qt.RightDockWidgetArea, self.options_dock)
        self.report_dock = QDockWidget("Design Report", self)
        self.report_dock.setObjectName("designReportDock")
        self.report_dock.setWidget(report)
        self.report_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea | Qt.BottomDockWidgetArea)
        self.report_dock.setFeatures(
            QDockWidget.DockWidgetMovable
            | QDockWidget.DockWidgetFloatable
            | QDockWidget.DockWidgetClosable
        )
        self.addDockWidget(Qt.RightDockWidgetArea, self.report_dock)
        self.tabifyDockWidget(self.right_dock, self.options_dock)
        self.tabifyDockWidget(self.right_dock, self.report_dock)
        self.right_dock.raise_()
        self.resizeDocks([self.left_dock, self.right_dock], [330, 340], Qt.Horizontal)
        self._build_panel_menu()

    def _build_menu(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        open_action = QAction("Open Design...", self)
        open_action.triggered.connect(self.open_design)
        file_menu.addAction(open_action)
        save_pes = QAction("Save PES As...", self)
        save_pes.triggered.connect(self.save_pes_as)
        file_menu.addAction(save_pes)
        save_project = QAction("Save Project As...", self)
        save_project.triggered.connect(self.save_project_as)
        file_menu.addAction(save_project)
        export_preview = QAction("Save Realistic Screenshot...", self)
        export_preview.triggered.connect(self.export_realistic_preview)
        file_menu.addAction(export_preview)
        file_menu.addSeparator()
        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        self.view_menu = self.menuBar().addMenu("&View")
        reset_action = QAction("Reset Zoom", self)
        reset_action.triggered.connect(lambda: self.canvas.reset_view())
        self.view_menu.addAction(reset_action)

    def _build_panel_menu(self) -> None:
        self.view_menu.addSeparator()
        self.view_menu.addAction(self.left_dock.toggleViewAction())
        self.view_menu.addAction(self.right_dock.toggleViewAction())
        self.view_menu.addAction(self.options_dock.toggleViewAction())
        self.view_menu.addAction(self.report_dock.toggleViewAction())
        self.view_menu.addSeparator()

        float_left = QAction("Float Workspace Panel", self)
        float_left.triggered.connect(lambda: self._float_panel(self.left_dock))
        self.view_menu.addAction(float_left)
        float_right = QAction("Float Threads Panel", self)
        float_right.triggered.connect(lambda: self._float_panel(self.right_dock))
        self.view_menu.addAction(float_right)
        float_options = QAction("Float View Options Panel", self)
        float_options.triggered.connect(lambda: self._float_panel(self.options_dock))
        self.view_menu.addAction(float_options)
        float_report = QAction("Float Design Report Panel", self)
        float_report.triggered.connect(lambda: self._float_panel(self.report_dock))
        self.view_menu.addAction(float_report)

        dock_all = QAction("Dock All Panels", self)
        dock_all.triggered.connect(self._dock_all_panels)
        self.view_menu.addAction(dock_all)

    def _float_panel(self, dock: QDockWidget) -> None:
        dock.show()
        dock.setFloating(True)
        dock.raise_()

    def _dock_all_panels(self) -> None:
        self.left_dock.setFloating(False)
        self.right_dock.setFloating(False)
        self.options_dock.setFloating(False)
        self.report_dock.setFloating(False)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.left_dock)
        self.addDockWidget(Qt.RightDockWidgetArea, self.right_dock)
        self.addDockWidget(Qt.RightDockWidgetArea, self.options_dock)
        self.addDockWidget(Qt.RightDockWidgetArea, self.report_dock)
        self.tabifyDockWidget(self.right_dock, self.options_dock)
        self.tabifyDockWidget(self.right_dock, self.report_dock)
        self.left_dock.show()
        self.right_dock.show()
        self.options_dock.show()
        self.report_dock.show()

    def _playback_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("playbackBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(12, 8, 12, 8)
        self.play_button = QPushButton("Play")
        self.play_button.clicked.connect(self.start_playback)
        pause = QPushButton("Pause")
        pause.clicked.connect(self.pause_playback)
        stop = QPushButton("Stop")
        stop.clicked.connect(self.stop_playback)
        self.step_label = QLabel("0 / 0")
        self.step_slider = QSlider(Qt.Horizontal)
        self.step_slider.setMinimumWidth(360)
        self.step_slider.valueChanged.connect(self.canvas.set_step)
        self.step_slider.valueChanged.connect(
            lambda value: self.step_label.setText(f"{value} / {self.step_slider.maximum()}")
        )
        layout.addWidget(self.play_button)
        layout.addWidget(pause)
        layout.addWidget(stop)
        layout.addWidget(self.step_slider)
        layout.addWidget(self.step_label)
        return bar

    def _legend_row(self, color: str, label: str, style: str = "dot") -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        swatch = QLabel()
        swatch.setFixedSize(34, 18)
        if style == "line":
            swatch.setStyleSheet(f"border-bottom:2px dashed {color};")
        else:
            swatch.setStyleSheet(
                f"background:{color}; border:1px solid #172026; border-radius:7px; margin:2px 10px;"
            )
        layout.addWidget(swatch)
        layout.addWidget(QLabel(label), 1)
        return row

    def _view_options_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.addWidget(QLabel("Visibility"))
        self.show_jumps = QCheckBox("Show jumps")
        self.show_jumps.setChecked(False)
        self.show_jumps.stateChanged.connect(self.update_canvas_flags)
        self.show_points = QCheckBox("Show needle points")
        self.show_points.setChecked(False)
        self.show_points.stateChanged.connect(self.update_canvas_flags)
        self.show_markers = QCheckBox("Show trims/color changes")
        self.show_markers.setChecked(False)
        self.show_markers.stateChanged.connect(self.update_canvas_flags)
        self.edit_stitches = QCheckBox("Edit stitches: left draw, right delete")
        self.edit_stitches.setChecked(False)
        self.edit_stitches.stateChanged.connect(self.update_canvas_flags)
        presets = QHBoxLayout()
        clean_preview = QPushButton("Clean Preview")
        clean_preview.clicked.connect(self.apply_clean_preview)
        diagnostics = QPushButton("Diagnostics")
        diagnostics.clicked.connect(self.apply_diagnostics_preview)
        presets.addWidget(clean_preview)
        presets.addWidget(diagnostics)
        layout.addLayout(presets)
        layout.addWidget(self.show_jumps)
        layout.addWidget(self.show_points)
        layout.addWidget(self.show_markers)
        layout.addWidget(self.edit_stitches)
        edit_buttons = QHBoxLayout()
        undo_edit = QPushButton("Undo Edit")
        undo_edit.clicked.connect(self.undo_stitch_edit)
        reset_edits = QPushButton("Reset Edits")
        reset_edits.clicked.connect(self.reset_stitch_edits)
        edit_buttons.addWidget(undo_edit)
        edit_buttons.addWidget(reset_edits)
        layout.addLayout(edit_buttons)
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        layout.addWidget(line)
        layout.addWidget(QLabel("Legend"))
        layout.addWidget(self._legend_row("#17201c", "Stitches", "line"))
        layout.addWidget(self._legend_row("#66736f", "Jump travel", "line"))
        layout.addWidget(self._legend_row("#ff8a3d", "Trim marker"))
        layout.addWidget(self._legend_row("#2b7fff", "Color change marker"))
        layout.addWidget(self._legend_row("#ffc446", "Needle point"))
        layout.addStretch(1)
        return panel

    def _conversion_tab(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        form = QFormLayout()
        self.fit_width = QDoubleSpinBox()
        self.fit_width.setRange(1, 300)
        self.fit_width.setValue(90)
        self.fit_width.setSuffix(" mm")
        self.fit_width.setToolTip(
            "Brother Duetta NV4500D design field is 180 x 300 mm, or 300 x 180 mm when rotated."
        )
        self.fill_spacing = QDoubleSpinBox()
        self.fill_spacing.setRange(0.1, 2.0)
        self.fill_spacing.setDecimals(2)
        self.fill_spacing.setSingleStep(0.05)
        self.fill_spacing.setValue(recommended_fill_spacing(DEFAULT_THREAD_WEIGHT))
        self.max_stitch = QDoubleSpinBox()
        self.max_stitch.setRange(0.5, 7.0)
        self.max_stitch.setDecimals(1)
        self.max_stitch.setValue(3.0)
        self.max_stitch.setSuffix(" mm")
        self.min_stitch = QDoubleSpinBox()
        self.min_stitch.setRange(0.05, 1.0)
        self.min_stitch.setDecimals(2)
        self.min_stitch.setSingleStep(0.05)
        self.min_stitch.setValue(0.30)
        self.min_stitch.setSuffix(" mm")
        self.min_stitch.setToolTip("Shortest generated stitch/run. Higher values reduce micro-stitches but can remove fine detail.")
        self.stitch_perimeter = QCheckBox("Stitch perimeter of each color block")
        self.perimeter_offset = QDoubleSpinBox()
        self.perimeter_offset.setRange(0.0, 1.5)
        self.perimeter_offset.setDecimals(2)
        self.perimeter_offset.setSingleStep(0.05)
        self.perimeter_offset.setValue(0.24)
        self.perimeter_offset.setSuffix(" mm")
        self.perimeter_passes = QSpinBox()
        self.perimeter_passes.setRange(1, 3)
        self.perimeter_passes.setValue(1)
        self.fill_mode = QComboBox()
        self.fill_mode.addItems(["mixed", "contour", "tatami", "crosshatch", "horizontal", "outline"])
        self.path_planning = QComboBox()
        self.path_planning.addItem("Fast", "fast")
        self.path_planning.addItem("Clean Top Stitch", "clean_top")
        self.path_planning.addItem("Min Cuts", "min_cuts")
        self.path_planning.setCurrentIndex(2)
        self.fill_angle = QDoubleSpinBox()
        self.fill_angle.setRange(-90, 90)
        self.fill_angle.setSingleStep(5)
        self.fill_angle.setValue(45)
        self.fill_angle.setSuffix(" deg")
        self.max_colors = QSpinBox()
        self.max_colors.setRange(2, 16)
        self.max_colors.setValue(6)
        self.color_merge = QDoubleSpinBox()
        self.color_merge.setRange(0, 255)
        self.color_merge.setValue(56)
        self.collapse_edge_shades = QCheckBox("Collapse edge shades")
        self.collapse_edge_shades.setChecked(True)
        self.collapse_edge_shades.setToolTip(
            "Merge anti-aliased edge colors into the nearest real thread color. "
            "Turn this off when those extra shades are intentional."
        )
        self.pdf_page = QSpinBox()
        self.pdf_page.setRange(1, 999)
        self.pdf_page.setValue(1)
        form.addRow("Fit width", self.fit_width)
        form.addRow("Fill spacing", self.fill_spacing)
        form.addRow("Max stitch", self.max_stitch)
        form.addRow("Min stitch", self.min_stitch)
        form.addRow("", self.stitch_perimeter)
        form.addRow("Perimeter offset", self.perimeter_offset)
        form.addRow("Perimeter passes", self.perimeter_passes)
        form.addRow("Fill mode", self.fill_mode)
        form.addRow("Path planning", self.path_planning)
        form.addRow("Fill angle", self.fill_angle)
        form.addRow("Max colors", self.max_colors)
        form.addRow("Color flattening", self.color_merge)
        form.addRow("", self.collapse_edge_shades)
        form.addRow("PDF page", self.pdf_page)
        layout.addLayout(form)
        apply_settings = QPushButton("Apply Settings")
        apply_settings.clicked.connect(self.apply_current_settings)
        layout.addWidget(apply_settings)
        actions_box = QFrame()
        actions_layout = QVBoxLayout(actions_box)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.addWidget(QLabel("File Actions"))
        open_button = QPushButton("Open and Convert")
        open_button.clicked.connect(self.open_design)
        save_pes = QPushButton("Save PES")
        save_pes.clicked.connect(self.save_pes_as)
        save_project = QPushButton("Save Project")
        save_project.clicked.connect(self.save_project_as)
        export_preview = QPushButton("Realistic Screenshot")
        export_preview.clicked.connect(self.export_realistic_preview)
        email_project = QPushButton("Email Project")
        email_project.clicked.connect(self.email_project)
        actions_layout.addWidget(open_button)
        actions_layout.addWidget(save_pes)
        actions_layout.addWidget(save_project)
        actions_layout.addWidget(export_preview)
        actions_layout.addWidget(email_project)
        layout.addWidget(actions_box)
        safe_density = QPushButton("Apply Safer Density")
        safe_density.clicked.connect(self.apply_safer_density)
        layout.addWidget(safe_density)
        optimize = QPushButton("Analyze && Optimize")
        optimize.clicked.connect(self.analyze_and_optimize)
        layout.addWidget(optimize)
        self.color_preview_label = QLabel("No color blocks yet.")
        self.color_preview_label.setWordWrap(True)
        self.color_preview_label.setTextFormat(Qt.RichText)
        layout.addWidget(self.color_preview_label)
        layout.addStretch(1)
        return panel

    def _report_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.addWidget(QLabel("Design Report"))
        self.stats_label = QLabel("No design loaded.")
        self.stats_label.setWordWrap(True)
        self.stats_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        report_scroll = QScrollArea()
        report_scroll.setWidgetResizable(True)
        report_scroll.setFrameShape(QFrame.NoFrame)
        report_body = QWidget()
        report_body_layout = QVBoxLayout(report_body)
        report_body_layout.setContentsMargins(0, 0, 0, 0)
        report_body_layout.addWidget(self.stats_label)
        report_body_layout.addStretch(1)
        report_scroll.setWidget(report_body)
        layout.addWidget(report_scroll, 1)
        return panel

    def apply_clean_preview(self) -> None:
        self.show_jumps.setChecked(False)
        self.show_points.setChecked(False)
        self.show_markers.setChecked(False)
        self.edit_stitches.setChecked(False)
        self.update_canvas_flags()

    def apply_diagnostics_preview(self) -> None:
        self.show_jumps.setChecked(True)
        self.show_points.setChecked(True)
        self.show_markers.setChecked(True)
        self.edit_stitches.setChecked(False)
        self.update_canvas_flags()

    def _connect_setting_refresh(self) -> None:
        for widget in [
            self.fit_width,
            self.fill_spacing,
            self.max_stitch,
            self.min_stitch,
            self.perimeter_offset,
            self.fill_angle,
            self.color_merge,
        ]:
            widget.valueChanged.connect(self.schedule_refresh)
        self.perimeter_passes.valueChanged.connect(self.schedule_refresh)
        self.stitch_perimeter.stateChanged.connect(self.toggle_perimeter_preview)
        self.max_colors.valueChanged.connect(self.schedule_refresh)
        self.collapse_edge_shades.stateChanged.connect(self.schedule_refresh)
        self.pdf_page.valueChanged.connect(self.schedule_refresh)
        self.fill_mode.currentTextChanged.connect(self.schedule_refresh)
        self.path_planning.currentIndexChanged.connect(self.schedule_refresh)

    def _library_tab(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        self.library_list = QListWidget()
        self.library_list.itemDoubleClicked.connect(self.load_library_item)
        layout.addWidget(self.library_list, 1)
        buttons = QHBoxLayout()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_library)
        load = QPushButton("Load")
        load.clicked.connect(lambda: self.load_library_item(self.library_list.currentItem()))
        delete = QPushButton("Delete")
        delete.clicked.connect(self.delete_library_item)
        buttons.addWidget(refresh)
        buttons.addWidget(load)
        buttons.addWidget(delete)
        layout.addLayout(buttons)
        return panel

    def _inventory_tab(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        self.inventory_table = QTableWidget(0, 4)
        self.inventory_table.setHorizontalHeaderLabels(["Brand", "Name/Number", "Color", "Qty"])
        layout.addWidget(self.inventory_table, 1)

        form = QFormLayout()
        self.inventory_brand = QLineEdit()
        self.inventory_name = QLineEdit()
        self.inventory_color = QLineEdit("#000000")
        self.inventory_qty = QSpinBox()
        self.inventory_qty.setRange(0, 999)
        self.inventory_qty.setValue(1)
        form.addRow("Brand", self.inventory_brand)
        form.addRow("Name/number", self.inventory_name)
        form.addRow("Hex color", self.inventory_color)
        form.addRow("Quantity", self.inventory_qty)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        add_button = QPushButton("Add Thread")
        add_button.clicked.connect(self.add_inventory_thread)
        delete_button = QPushButton("Delete Selected")
        delete_button.clicked.connect(self.delete_inventory_thread)
        buttons.addWidget(add_button)
        buttons.addWidget(delete_button)
        layout.addLayout(buttons)
        self.refresh_inventory()
        return panel

    def _settings_tab(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        form = QFormLayout()
        self.units_select = QComboBox()
        self.units_select.addItem("Metric (mm)", "metric")
        self.units_select.addItem("SAE (inches)", "sae")
        self.units_select.currentIndexChanged.connect(self.update_view_settings)
        self.fabric_color_input = QLineEdit("#fbfcfa")
        self.fabric_color_input.editingFinished.connect(self.update_view_settings)
        fabric_button = QPushButton("Choose Fabric Color")
        fabric_button.clicked.connect(self.choose_fabric_color)
        form.addRow("Measurement units", self.units_select)
        form.addRow("Fabric color", self.fabric_color_input)
        layout.addLayout(form)
        layout.addWidget(fabric_button)
        hint = QLabel("These settings change the preview background and measurement grid. PES files are still generated in machine units.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch(1)
        return panel

    def _thread_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.addWidget(QLabel("Threads"))
        brand_row = QHBoxLayout()
        brand_row.addWidget(QLabel("Match brand"))
        self.thread_brand = QComboBox()
        for brand in available_thread_brands():
            self.thread_brand.addItem(brand, brand)
        self.thread_brand.currentIndexChanged.connect(self.thread_brand_changed)
        brand_row.addWidget(self.thread_brand, 1)
        layout.addLayout(brand_row)
        self.thread_container = QWidget()
        self.thread_layout = QVBoxLayout(self.thread_container)
        self.thread_layout.addStretch(1)
        thread_scroll = QScrollArea()
        thread_scroll.setWidgetResizable(True)
        thread_scroll.setFrameShape(QFrame.NoFrame)
        thread_scroll.setWidget(self.thread_container)
        layout.addWidget(thread_scroll, 1)
        self.shopping_label = QLabel("")
        self.shopping_label.setWordWrap(True)
        self.shopping_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.shopping_label)
        buttons = QHBoxLayout()
        apply_threads = QPushButton("Apply")
        apply_threads.clicked.connect(self.apply_thread_changes)
        reset_threads = QPushButton("Reset")
        reset_threads.clicked.connect(self.reset_thread_changes)
        buttons.addWidget(apply_threads)
        buttons.addWidget(reset_threads)
        layout.addLayout(buttons)
        return panel

    def choose_fabric_color(self) -> None:
        current = QColor(self.fabric_color_input.text())
        color = QColorDialog.getColor(current if current.isValid() else QColor("#fbfcfa"), self, "Choose Fabric Color")
        if color.isValid():
            self.fabric_color_input.setText(color.name())
            self.update_view_settings()

    def update_view_settings(self) -> None:
        if not hasattr(self, "canvas"):
            return
        units = self.units_select.currentData() if hasattr(self, "units_select") else "metric"
        self.canvas.set_measurement_units(str(units or "metric"))
        color_text = self.fabric_color_input.text().strip() if hasattr(self, "fabric_color_input") else "#fbfcfa"
        color = QColor(color_text)
        if color.isValid():
            self.fabric_color_input.setText(color.name())
            self.canvas.set_background_color(color.name())

    def state_snapshot(self) -> dict | None:
        if self.state is None:
            return None
        return {
            "segments": copy.deepcopy(self.state.segments),
            "commands": copy.deepcopy(self.state.commands),
            "color_blocks": copy.deepcopy(self.state.color_blocks),
            "counts": copy.deepcopy(self.state.counts),
            "bounds": tuple(self.state.bounds),
        }

    def set_baseline_snapshot(self) -> None:
        self.baseline_snapshot = self.state_snapshot()
        self.undo_stack.clear()
        self._manual_stitch_anchor = None

    def push_undo_snapshot(self) -> None:
        snapshot = self.state_snapshot()
        if snapshot is None:
            return
        self.undo_stack.append(snapshot)
        if len(self.undo_stack) > 60:
            self.undo_stack.pop(0)

    def restore_snapshot(self, snapshot: dict) -> None:
        if self.state is None:
            return
        self.state.segments = copy.deepcopy(snapshot["segments"])
        self.state.commands = copy.deepcopy(snapshot["commands"])
        self.state.color_blocks = copy.deepcopy(snapshot["color_blocks"])
        self.state.counts = copy.deepcopy(snapshot["counts"])
        self.state.bounds = tuple(snapshot["bounds"])
        self._manual_stitch_anchor = None
        self.refresh_state_view(preserve_view=True)

    def reset_stitch_edits(self) -> None:
        if self.state is None:
            return
        if self.baseline_snapshot is None:
            QMessageBox.information(self, "OpenStitch", "No reset point is available yet.")
            return
        self.restore_snapshot(self.baseline_snapshot)
        self.undo_stack.clear()

    def undo_stitch_edit(self) -> None:
        if self.state is None:
            return
        if not self.undo_stack:
            QMessageBox.information(self, "OpenStitch", "No stitch edit to undo.")
            return
        self.restore_snapshot(self.undo_stack.pop())

    def reset_thread_changes(self) -> None:
        if self.state is None:
            return
        if self.baseline_snapshot is None:
            QMessageBox.information(self, "OpenStitch", "No thread reset point is available yet.")
            return
        self.restore_snapshot(self.baseline_snapshot)
        self.undo_stack.clear()

    def current_settings(self) -> dict:
        settings = project_settings(
            fit_width=self.fit_width.value(),
            fill_spacing=self.fill_spacing.value(),
            thread_weight=DEFAULT_THREAD_WEIGHT,
            max_stitch=self.max_stitch.value(),
            min_stitch=self.min_stitch.value() if hasattr(self, "min_stitch") else 0.30,
            fill_mode=self.fill_mode.currentText(),
            fill_angle_deg=self.fill_angle.value(),
            max_colors=self.max_colors.value(),
            color_merge_distance=self.color_merge.value(),
            collapse_edge_shades=self.collapse_edge_shades.isChecked() if hasattr(self, "collapse_edge_shades") else True,
            pdf_page=self.pdf_page.value(),
            display_units=self.units_select.currentData() if hasattr(self, "units_select") else "metric",
            fabric_color=self.fabric_color_input.text().strip() if hasattr(self, "fabric_color_input") else "#fbfcfa",
            stitch_perimeter=self.stitch_perimeter.isChecked() if hasattr(self, "stitch_perimeter") else False,
            perimeter_offset_mm=self.perimeter_offset.value() if hasattr(self, "perimeter_offset") else 0.24,
            perimeter_passes=self.perimeter_passes.value() if hasattr(self, "perimeter_passes") else 1,
            path_planning=str(self.path_planning.currentData() or "min_cuts") if hasattr(self, "path_planning") else "min_cuts",
        )
        settings["thread_brand"] = self.selected_thread_brand()
        if self.state is not None and self.state.settings.get("_preserve_block_filter"):
            settings["_preserve_block_filter"] = True
        return settings

    def selected_thread_brand(self) -> str:
        if hasattr(self, "thread_brand"):
            brand = self.thread_brand.currentData()
            if brand:
                return str(brand)
        return "Floriani"

    def selected_catalog(self) -> list[dict]:
        return [item for item in self.catalog if item.get("brand") == self.selected_thread_brand()]

    def thread_brand_changed(self, *args) -> None:
        if self.state is not None:
            self.state.settings["thread_brand"] = self.selected_thread_brand()
        self.populate_threads()
        self.update_stats()
        self.update_shopping_list()

    def apply_settings_to_controls(self, settings: dict) -> None:
        self._loading_settings = True
        try:
            if settings.get("fit_width_mm") not in {"", None}:
                self.fit_width.setValue(float(settings["fit_width_mm"]))
            self.fill_spacing.setValue(float(settings.get("fill_spacing_mm", self.fill_spacing.value())))
            self.max_stitch.setValue(float(settings.get("max_stitch_mm", self.max_stitch.value())))
            if hasattr(self, "min_stitch"):
                self.min_stitch.setValue(float(settings.get("min_stitch_mm", self.min_stitch.value())))
            mode = str(settings.get("fill_mode", self.fill_mode.currentText()))
            index = self.fill_mode.findText(mode)
            if index >= 0:
                self.fill_mode.setCurrentIndex(index)
            self.fill_angle.setValue(float(settings.get("fill_angle_deg", self.fill_angle.value())))
            self.max_colors.setValue(int(settings.get("max_colors", self.max_colors.value())))
            self.color_merge.setValue(float(settings.get("color_merge_distance", self.color_merge.value())))
            if hasattr(self, "collapse_edge_shades"):
                self.collapse_edge_shades.setChecked(bool(settings.get("collapse_edge_shades", True)))
            self.pdf_page.setValue(int(settings.get("pdf_page", self.pdf_page.value())))
            units = str(settings.get("display_units", "metric"))
            if hasattr(self, "units_select"):
                index = self.units_select.findData(units)
                if index >= 0:
                    self.units_select.setCurrentIndex(index)
            if hasattr(self, "fabric_color_input"):
                self.fabric_color_input.setText(str(settings.get("fabric_color", "#fbfcfa")))
                self.update_view_settings()
            if hasattr(self, "stitch_perimeter"):
                self.stitch_perimeter.setChecked(bool(settings.get("stitch_perimeter", False)))
            if hasattr(self, "perimeter_offset"):
                self.perimeter_offset.setValue(float(settings.get("perimeter_offset_mm", 0.24)))
            if hasattr(self, "perimeter_passes"):
                self.perimeter_passes.setValue(int(settings.get("perimeter_passes", 1)))
            if hasattr(self, "path_planning"):
                planning = str(settings.get("path_planning", "min_cuts"))
                index = self.path_planning.findData(planning)
                if index >= 0:
                    self.path_planning.setCurrentIndex(index)
            if hasattr(self, "thread_brand"):
                index = self.thread_brand.findData(str(settings.get("thread_brand", "Floriani")))
                if index >= 0:
                    self.thread_brand.setCurrentIndex(index)
        finally:
            self._loading_settings = False

    def schedule_refresh(self, *args) -> None:
        if self._loading_settings or self.state is None:
            return
        self.stats_label.setText("Updating stitches...")
        self.color_preview_label.setText("Updating colors...")
        self.refresh_timer.start()

    def toggle_perimeter_preview(self, *args) -> None:
        if self._loading_settings or self.state is None:
            return
        self.state.settings["stitch_perimeter"] = self.stitch_perimeter.isChecked()
        base_segments = [segment for segment in self.state.segments if not segment.get("perimeter")]
        if self.stitch_perimeter.isChecked():
            self.state.segments = add_perimeter_segments(
                base_segments,
                self.state.color_blocks,
                max_stitch_mm=float(self.state.settings["max_stitch_mm"]),
                offset_mm=float(self.state.settings.get("perimeter_offset_mm", 0.24)),
                passes=int(self.state.settings.get("perimeter_passes", 1)),
            )
        else:
            self.state.segments = base_segments
            self.recount_block_stitches()
        self.recompute_state_after_manual_edit()
        self.set_baseline_snapshot()

    def apply_safer_density(self) -> None:
        self._loading_settings = True
        try:
            self.fill_mode.setCurrentText("mixed")
            self.fill_spacing.setValue(max(self.fill_spacing.value(), 0.50))
        finally:
            self._loading_settings = False
        self.schedule_refresh()

    def analyze_and_optimize(self) -> None:
        if self.state is None:
            QMessageBox.information(self, "OpenStitch", "Open a design first.")
            return
        min_x, min_y, max_x, max_y = self.state.bounds
        width_mm = max_x - min_x
        height_mm = max_y - min_y
        area = max(width_mm * height_mm, 0.001)
        counts = self.state.counts
        command_density = (
            counts.get("needle_points", 0)
            + counts.get("jumps", 0)
            + counts.get("trims", 0)
            + counts.get("color_changes", 0)
        ) / area
        stitch_density = counts.get("needle_points", 0) / area
        max_stitch = float(self.max_stitch.value())
        stitch_lengths = [
            math.hypot(segment["x2"] - segment["x1"], segment["y2"] - segment["y1"])
            for segment in self.state.segments
            if segment["kind"] == "stitch"
        ]
        travel_lengths = [
            math.hypot(segment["x2"] - segment["x1"], segment["y2"] - segment["y1"])
            for segment in self.state.segments
            if segment["kind"] != "stitch"
        ]
        long_stitches = sum(1 for length in stitch_lengths if length > max_stitch)
        long_travels = sum(1 for length in travel_lengths if length > 12.0)
        frame_note = brother_duetta_frame_note(width_mm, height_mm)
        changes: list[str] = []

        self._loading_settings = True
        try:
            if self.fill_mode.currentText() in {"horizontal", "outline"}:
                self.fill_mode.setCurrentText("mixed")
                changes.append("changed fill mode to mixed for a less directional fill")
            if command_density > 2.4:
                density_scale = math.sqrt(command_density / 2.4)
                new_spacing = min(2.0, max(self.fill_spacing.value() * density_scale, 0.50))
                new_spacing = round(new_spacing / 0.05) * 0.05
                if new_spacing != self.fill_spacing.value():
                    self.fill_spacing.setValue(new_spacing)
                    changes.append(f"increased fill spacing to {new_spacing:.2f} mm to reduce saturation")
            elif stitch_density < 1.2 and self.fill_spacing.value() > 0.25:
                new_spacing = max(0.25, self.fill_spacing.value() - 0.05)
                if new_spacing != self.fill_spacing.value():
                    self.fill_spacing.setValue(new_spacing)
                    changes.append(f"reduced fill spacing to {new_spacing:.2f} mm to add definition")
            if frame_note.startswith("Exceeds") and self.fit_width.value():
                normal_scale = min(
                    BROTHER_DUETTA_MAX_WIDTH_MM / max(width_mm, 0.001),
                    BROTHER_DUETTA_MAX_HEIGHT_MM / max(height_mm, 0.001),
                )
                rotated_scale = min(
                    BROTHER_DUETTA_MAX_HEIGHT_MM / max(width_mm, 0.001),
                    BROTHER_DUETTA_MAX_WIDTH_MM / max(height_mm, 0.001),
                )
                scale = max(normal_scale, rotated_scale)
                if 0 < scale < 1:
                    new_width = max(1.0, self.fit_width.value() * scale * 0.98)
                    self.fit_width.setValue(new_width)
                    changes.append(f"reduced fit width to {new_width:.1f} mm so the design can fit the Duetta field")
        finally:
            self._loading_settings = False

        analysis = [
            f"Machine fit: {frame_note}",
            f"Stitches: {counts.get('needle_points', 0)}",
            f"Long stitch spans over {max_stitch:.1f} mm: {long_stitches}",
            f"Long travel/jump spans over 12.0 mm: {long_travels}",
            f"Density: {stitch_density:.2f} st/mm2, {command_density:.2f} commands/mm2",
            f"Estimated stitch time: {estimate_stitch_time(counts, self.state.color_blocks)}",
        ]
        if changes:
            self.schedule_refresh()
            analysis.append("")
            analysis.append("Applied:")
            analysis.extend(f"- {change}" for change in changes)
        else:
            analysis.append("")
            analysis.append("No automatic setting changes were needed.")
        QMessageBox.information(self, "OpenStitch Analyzer", "\n".join(analysis))

    def refresh_current_design(self) -> None:
        if self.state is None:
            return
        settings = self.current_settings()
        if self.state.working_source.suffix.lower() in {".pes", ".dst", ".exp"}:
            self.state.settings = settings
            self.update_stats()
            self.update_color_block_preview()
            self.update_shopping_list()
            return
        try:
            self.convert_path(
                self.state.working_source,
                settings,
                reset_view=False,
                write_outputs=False,
            )
        except Exception as error:
            QMessageBox.critical(self, "OpenStitch", str(error))

    def apply_current_settings(self) -> None:
        if self.state is None:
            QMessageBox.information(self, "OpenStitch", "Open a design first.")
            return
        try:
            self.convert_path(
                self.state.working_source,
                self.current_settings(),
                reset_view=False,
                write_outputs=False,
            )
        except Exception as error:
            QMessageBox.critical(self, "OpenStitch", str(error))

    def open_design(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open design",
            str(Path.home()),
            "Embroidery and images (*.svg *.pes *.dst *.exp *.png *.jpg *.jpeg *.pdf *.embdproj);;All files (*.*)",
        )
        if path:
            self.load_path(Path(path))

    def load_path(self, path: Path) -> None:
        try:
            if path.suffix.lower() == PROJECT_SUFFIX:
                path, settings = self.unpack_project(path)
            else:
                settings = self.current_settings()
            self.apply_settings_to_controls(settings)
            self.convert_path(path, settings)
        except Exception as error:
            QMessageBox.critical(self, "OpenStitch", str(error))

    def unpack_project(self, project_path: Path) -> tuple[Path, dict]:
        with zipfile.ZipFile(project_path) as archive:
            project = json.loads(archive.read("project.json").decode("utf-8"))
        source_name = safe_name(project.get("source_name") or "project.svg")
        source_data = base64.b64decode(project["source_data_b64"])
        job_id = uuid.uuid4().hex[:10]
        source_path = OUTPUT_DIR / f"{Path(source_name).stem}_{job_id}{Path(source_name).suffix}"
        source_path.write_bytes(source_data)
        return source_path, project.get("settings", self.current_settings())

    def convert_path(
        self,
        source_path: Path,
        settings: dict,
        reset_view: bool = True,
        write_outputs: bool = True,
    ) -> None:
        OUTPUT_DIR.mkdir(exist_ok=True)
        previous_blocks = {
            block["index"]: {
                "color": block.get("color"),
                "label": block.get("label"),
            }
            for block in self.state.color_blocks
        } if self.state is not None and not reset_view else {}
        working_source = source_path
        if source_path.parent.resolve() != OUTPUT_DIR.resolve():
            name = safe_name(source_path.name)
            job_id = uuid.uuid4().hex[:10]
            working_source = OUTPUT_DIR / f"{Path(name).stem}_{job_id}{Path(name).suffix.lower()}"
            shutil.copy2(source_path, working_source)
        segments, commands, blocks, counts = self.collect_design(working_source, settings)
        if previous_blocks and settings.get("_preserve_block_filter"):
            kept_blocks = set(previous_blocks)
            segments = [segment for segment in segments if segment["blockIndex"] in kept_blocks]
            commands = [
                command
                for command in commands
                if not isinstance(command.get("color"), int) or command.get("color") in kept_blocks
            ]
            blocks = [block for block in blocks if block["index"] in kept_blocks]
            counts["needle_points"] = sum(1 for segment in segments if segment["kind"] == "stitch")
            counts["stitch_segments"] = counts["needle_points"]
            counts["jumps"] = sum(1 for segment in segments if segment["kind"] != "stitch")
            counts["trims"] = sum(1 for command in commands if command.get("command") == "trim")
            counts["color_changes"] = max(0, len(blocks) - 1)
        if previous_blocks:
            for block in blocks:
                previous = previous_blocks.get(block["index"])
                if not previous or not previous.get("color"):
                    continue
                block["color"] = previous["color"]
                if previous.get("label"):
                    block["label"] = previous["label"]
            block_colors = {block["index"]: block["color"] for block in blocks}
            for segment in segments:
                if segment["blockIndex"] in block_colors:
                    segment["color"] = block_colors[segment["blockIndex"]]
        segments, commands = normalize_positive_coordinates(segments, commands)
        bounds = design_bounds(segments, commands)
        if write_outputs or self.state is None:
            if working_source.suffix.lower() == ".pes":
                pes_path = working_source.with_name(f"{working_source.stem}_native_{uuid.uuid4().hex[:10]}.pes")
            else:
                pes_path = working_source.with_suffix(".pes")
            project_path = working_source.with_suffix(PROJECT_SUFFIX)
            written = write_segments_as_pes(
                segments,
                blocks,
                pes_path,
                max_stitch_mm=float(settings["max_stitch_mm"]),
                connect_short_gaps=True,
                perimeter_offset_mm=float(settings.get("perimeter_offset_mm", 0.24)),
                perimeter_passes=int(settings.get("perimeter_passes", 1)),
            )
            thread_metadata_path(pes_path).write_text(json.dumps({"blocks": written}, indent=2), encoding="utf-8")
            summary = project_summary_text(working_source, settings, bounds, counts, segments, blocks)
            project_path.with_suffix(".summary.txt").write_text(summary, encoding="utf-8")
            write_project_file(project_path, working_source, settings, summary)
        else:
            pes_path = self.state.pes_path
            project_path = self.state.project_path
        self.state = DesignState(
            source_path=source_path,
            working_source=working_source,
            pes_path=pes_path,
            project_path=project_path,
            settings=settings,
            segments=segments,
            commands=commands,
            color_blocks=blocks,
            counts=counts,
            bounds=bounds,
        )
        if reset_view:
            self.canvas.set_design(segments, commands, bounds)
        else:
            zoom = self.canvas.zoom
            pan = QPointF(self.canvas.pan)
            self.canvas.set_design(segments, commands, bounds)
            self.canvas.zoom = zoom
            self.canvas.pan = pan
            self.canvas.update()
        self.step_slider.setRange(0, counts.get("needle_points", 0))
        self.step_slider.setValue(counts.get("needle_points", 0))
        self.update_stats()
        self.populate_threads()
        self.update_color_block_preview()
        self.refresh_library()
        self.set_baseline_snapshot()

    def collect_design(self, path: Path, settings: dict) -> tuple[list[dict], list[dict], list[dict], dict]:
        suffix = path.suffix.lower()
        if suffix == ".svg" and not svg_needs_rasterization(path):
            result = collect_svg_segments(
                path,
                sample_step_mm=0.8,
                fill_spacing_mm=float(settings["fill_spacing_mm"]),
                max_stitch_mm=float(settings["max_stitch_mm"]),
                min_stitch_mm=float(settings.get("min_stitch_mm", 0.30)),
                fill_angle_deg=float(settings["fill_angle_deg"]),
                fill_mode=str(settings["fill_mode"]),
                path_planning=str(settings.get("path_planning", "min_cuts")),
                fit_width_mm=settings.get("fit_width_mm"),
                fit_height_mm=None,
                center=True,
            )
        elif is_raster_source(path) or (suffix == ".svg" and svg_needs_rasterization(path)):
            result = image_to_segments(
                path,
                fit_width_mm=settings.get("fit_width_mm") or 90.0,
                fit_height_mm=None,
                max_colors=int(settings["max_colors"]),
                fill_mode=str(settings["fill_mode"]),
                fill_angle_deg=float(settings["fill_angle_deg"]),
                path_planning=str(settings.get("path_planning", "min_cuts")),
                color_merge_distance=float(settings["color_merge_distance"]),
                collapse_edge_shades=bool(settings.get("collapse_edge_shades", True)),
                fill_spacing_mm=float(settings["fill_spacing_mm"]),
                max_stitch_mm=float(settings["max_stitch_mm"]),
                min_run_mm=float(settings.get("min_stitch_mm", 0.30)),
                pdf_page=int(settings["pdf_page"]),
            )
        else:
            pattern = embroidery.read(str(path))
            if pattern is None:
                raise ValueError(f"Could not read embroidery file: {path}")
            result = collect_segments(pattern)
            segments, commands, blocks, counts = result
            segments, blocks = apply_thread_metadata(path, segments, blocks)
            result = segments, commands, blocks, counts
        segments, commands, blocks, counts = group_color_blocks_by_inventory(*result)
        if settings.get("stitch_perimeter"):
            segments = add_perimeter_segments(
                segments,
                blocks,
                max_stitch_mm=float(settings["max_stitch_mm"]),
                offset_mm=float(settings.get("perimeter_offset_mm", 0.24)),
                passes=int(settings.get("perimeter_passes", 1)),
            )
            counts["needle_points"] = sum(1 for segment in segments if segment["kind"] == "stitch")
            counts["stitch_segments"] = counts["needle_points"]
        return segments, commands, blocks, counts

    def update_stats(self) -> None:
        if self.state is None:
            return
        min_x, min_y, max_x, max_y = self.state.bounds
        counts = self.state.counts
        area = max((max_x - min_x) * (max_y - min_y), 0.001)
        command_density = (
            counts.get("needle_points", 0)
            + counts.get("jumps", 0)
            + counts.get("trims", 0)
            + counts.get("color_changes", 0)
        ) / area
        stitch_density = counts.get("needle_points", 0) / area
        micro_segments = sum(
            1
            for segment in self.state.segments
            if segment["kind"] == "stitch"
            and 0 < math.hypot(segment["x2"] - segment["x1"], segment["y2"] - segment["y1"]) < 0.3
        )
        quality_notes: list[str] = []
        width_mm = max_x - min_x
        height_mm = max_y - min_y
        frame_note = brother_duetta_frame_note(width_mm, height_mm)
        if frame_note.startswith("Exceeds"):
            quality_notes.append(frame_note)
        elif (
            width_mm > BROTHER_DUETTA_MAX_WIDTH_MM * 0.9
            or height_mm > BROTHER_DUETTA_MAX_HEIGHT_MM * 0.9
        ):
            quality_notes.append(f"Near machine limit. {frame_note}")
        if command_density > 2.4:
            quality_notes.append(
                "High saturation risk. Try Apply Safer Density or increase fill spacing."
            )
        if micro_segments:
            quality_notes.append(f"{micro_segments} preview stitch segments are under 0.30 mm.")
        quality_text = "\n".join(quality_notes) if quality_notes else "Quality checks: no obvious density warning."
        fill_types = classify_fill_types(self.state.segments, self.state.color_blocks)
        self.stats_label.setText(
            f"{self.state.working_source.name}\n"
            f"Size: {width_mm:.1f} x {height_mm:.1f} mm\n"
            f"Machine fit: {frame_note}\n"
            f"Fill types: {fill_types['summary']}\n"
            f"Path planning: {self.state.settings.get('path_planning', 'min_cuts')}\n"
            f"Needle points: {counts.get('needle_points', 0)}\n"
            f"Jumps: {counts.get('jumps', 0)}  Trims: {counts.get('trims', 0)}  "
            f"Color changes: {counts.get('color_changes', 0)}\n"
            f"Density: {stitch_density:.2f} st/mm2, {command_density:.2f} commands/mm2\n"
            f"Estimated stitch time: {estimate_stitch_time(counts, self.state.color_blocks)}\n"
            f"PES: {self.state.pes_path.name}\n"
            f"{quality_text}"
        )

    def update_color_block_preview(self) -> None:
        if self.state is None:
            self.color_preview_label.setText("No color blocks yet.")
            return
        blocks = self.state.color_blocks
        if not blocks:
            self.color_preview_label.setText("No color blocks yet.")
            return
        swatches = []
        for block in blocks[:16]:
            color = html.escape(block["color"])
            label = html.escape(str(block.get("label", color)))
            stitches = int(block.get("stitches", 0))
            swatches.append(
                "<span style='white-space:nowrap; margin-right:8px;'>"
                f"<span style='display:inline-block; width:18px; height:18px; "
                f"background:{color}; border:1px solid #6d7871; vertical-align:middle;'></span> "
                f"{color} <small>({stitches})</small>"
                f"<span title='{label}'></span>"
                "</span>"
            )
        more = f" +{len(blocks) - 16} more" if len(blocks) > 16 else ""
        self.color_preview_label.setText(
            f"<b>Color blocks: {len(blocks)}</b>{html.escape(more)}<br>{''.join(swatches)}"
        )

    def populate_threads(self) -> None:
        while self.thread_layout.count() > 1:
            item = self.thread_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.thread_rows = []
        if self.state is None:
            return
        catalog = self.selected_catalog()
        for block in self.state.color_blocks:
            row = ThreadRow(block, catalog, self.thread_changed, self.add_thread_row_to_inventory)
            self.thread_rows.append(row)
            self.thread_layout.insertWidget(self.thread_layout.count() - 1, row)
        self.canvas.set_visible_blocks(self.selected_blocks())
        self.update_shopping_list()

    def thread_changed(self) -> None:
        if self.state is None:
            return
        block_colors = {block["index"]: block["color"] for block in self.state.color_blocks}
        for segment in self.state.segments:
            if segment["blockIndex"] in block_colors:
                segment["color"] = block_colors[segment["blockIndex"]]
        self.canvas.set_visible_blocks(self.selected_blocks())
        self.canvas.update()
        self.update_shopping_list()
        self.update_color_block_preview()

    def apply_thread_changes(self) -> None:
        if self.state is None:
            QMessageBox.information(self, "OpenStitch", "Open a design first.")
            return
        selected_rows = [row for row in self.thread_rows if row.checkbox.isChecked()]
        if not selected_rows:
            QMessageBox.warning(self, "OpenStitch", "Select at least one thread color to keep.")
            return

        old_to_new: dict[int, int] = {}
        color_to_new: dict[str, int] = {}
        new_blocks: list[dict] = []
        dropped_blocks = {row.block["index"] for row in self.thread_rows if not row.checkbox.isChecked()}

        for row in selected_rows:
            old_block = row.block
            color = normalize_hex(old_block["color"])
            label = old_block.get("label", color)
            if color not in color_to_new:
                color_to_new[color] = len(new_blocks)
                new_blocks.append(
                    {
                        "index": len(new_blocks),
                        "thread": len(new_blocks),
                        "color": color,
                        "label": label,
                        "stitches": 0,
                    }
                )
            new_index = color_to_new[color]
            old_to_new[old_block["index"]] = new_index
            if label and label not in new_blocks[new_index].get("label", ""):
                if new_blocks[new_index].get("label") in {"", color}:
                    new_blocks[new_index]["label"] = label
                else:
                    new_blocks[new_index]["label"] += f" / {label}"

        new_segments: list[dict] = []
        for segment in self.state.segments:
            old_index = segment["blockIndex"]
            if old_index not in old_to_new:
                continue
            new_index = old_to_new[old_index]
            new_segment = dict(segment)
            new_segment["blockIndex"] = new_index
            new_segment["colorIndex"] = new_index
            new_segment["color"] = new_blocks[new_index]["color"]
            if new_segment["kind"] == "stitch":
                new_blocks[new_index]["stitches"] += 1
            new_segments.append(new_segment)

        new_commands: list[dict] = []
        for command in self.state.commands:
            if command.get("command") == "color_change":
                continue
            old_color = command.get("color")
            if isinstance(old_color, int):
                if old_color in dropped_blocks:
                    continue
                if old_color in old_to_new:
                    new_command = dict(command)
                    new_command["color"] = old_to_new[old_color]
                    new_commands.append(new_command)
                    continue
            new_commands.append(dict(command))
        previous_block: int | None = None
        for segment in new_segments:
            block_index = segment["blockIndex"]
            if previous_block is not None and block_index != previous_block:
                new_commands.append(
                    {
                        "x": segment["x1"],
                        "y": segment["y1"],
                        "command": "color_change",
                        "color": block_index,
                        "step": segment.get("step", 0),
                    }
                )
            previous_block = block_index

        if not new_segments:
            QMessageBox.warning(self, "OpenStitch", "No stitches remain after applying thread changes.")
            return

        counts = dict(self.state.counts)
        counts["needle_points"] = sum(1 for segment in new_segments if segment["kind"] == "stitch")
        counts["stitch_segments"] = counts["needle_points"]
        counts["jumps"] = sum(1 for segment in new_segments if segment["kind"] != "stitch")
        counts["trims"] = sum(1 for command in new_commands if command.get("command") == "trim")
        counts["color_changes"] = max(0, len(new_blocks) - 1)

        new_segments, new_commands = normalize_positive_coordinates(new_segments, new_commands)
        bounds = design_bounds(new_segments, new_commands)
        rendered_pes = self.state.pes_path.with_name(f"{self.state.pes_path.stem}_applied_{uuid.uuid4().hex[:10]}.pes")
        previous_source = self.state.source_path
        previous_working_source = self.state.working_source
        state_settings = dict(self.state.settings)
        state_settings["_preserve_block_filter"] = True
        written = write_segments_as_pes(
            new_segments,
            new_blocks,
            rendered_pes,
            max_stitch_mm=float(self.state.settings["max_stitch_mm"]),
            connect_short_gaps=True,
            perimeter_offset_mm=float(self.state.settings.get("perimeter_offset_mm", 0.24)),
            perimeter_passes=int(self.state.settings.get("perimeter_passes", 1)),
        )
        thread_metadata_path(rendered_pes).write_text(json.dumps({"blocks": written}, indent=2), encoding="utf-8")
        self.state = DesignState(
            source_path=previous_source,
            working_source=previous_working_source,
            pes_path=rendered_pes,
            project_path=rendered_pes.with_suffix(PROJECT_SUFFIX),
            settings=state_settings,
            segments=new_segments,
            commands=new_commands,
            color_blocks=new_blocks,
            counts=counts,
            bounds=bounds,
        )
        zoom = self.canvas.zoom
        pan = QPointF(self.canvas.pan)
        self.canvas.set_design(new_segments, new_commands, bounds)
        self.canvas.zoom = zoom
        self.canvas.pan = pan
        self.step_slider.setRange(0, counts.get("needle_points", 0))
        self.step_slider.setValue(counts.get("needle_points", 0))
        self.populate_threads()
        self.update_stats()
        self.update_color_block_preview()
        self.canvas.update()
        self.set_baseline_snapshot()
        merged_count = len(selected_rows) - len(new_blocks)
        dropped_count = len(dropped_blocks)
        QMessageBox.information(
            self,
            "OpenStitch",
            f"Applied thread changes. Dropped {dropped_count} color block(s), merged {merged_count} like-color block(s).",
        )

    def selected_blocks(self) -> set[int]:
        return {row.block["index"] for row in self.thread_rows if row.checkbox.isChecked()}

    def color_overrides(self) -> dict[int, str]:
        return {row.block["index"]: row.block["color"] for row in self.thread_rows}

    def label_overrides(self) -> dict[int, str]:
        return {row.block["index"]: row.block.get("label", row.block["color"]) for row in self.thread_rows}

    def save_pes_as(self) -> None:
        if self.state is None:
            QMessageBox.information(self, "OpenStitch", "Open a design first.")
            return
        target, _ = QFileDialog.getSaveFileName(
            self,
            "Save PES",
            str(self.state.pes_path),
            "Brother PES (*.pes)",
        )
        if not target:
            return
        try:
            written = write_segments_as_pes(
                self.state.segments,
                self.state.color_blocks,
                Path(target),
                selected_blocks=self.selected_blocks(),
                color_overrides=self.color_overrides(),
                thread_label_overrides=self.label_overrides(),
                max_stitch_mm=float(self.state.settings["max_stitch_mm"]),
                connect_short_gaps=True,
                perimeter_offset_mm=float(self.state.settings.get("perimeter_offset_mm", 0.24)),
                perimeter_passes=int(self.state.settings.get("perimeter_passes", 1)),
            )
            thread_metadata_path(Path(target)).write_text(json.dumps({"blocks": written}, indent=2), encoding="utf-8")
            QMessageBox.information(self, "OpenStitch", f"Saved {target}")
        except Exception as error:
            QMessageBox.critical(self, "OpenStitch", str(error))

    def save_project_as(self) -> None:
        if self.state is None:
            QMessageBox.information(self, "OpenStitch", "Open a design first.")
            return
        target, _ = QFileDialog.getSaveFileName(
            self,
            "Save Project",
            str(self.state.project_path),
            "OpenStitch Project (*.embdproj)",
        )
        if not target:
            return
        try:
            summary = project_summary_text(
                self.state.working_source,
                self.state.settings,
                self.state.bounds,
                self.state.counts,
                self.state.segments,
                self.state.color_blocks,
            )
            write_project_file(Path(target), self.state.working_source, self.state.settings, summary)
            Path(target).with_suffix(".summary.txt").write_text(summary, encoding="utf-8")
            QMessageBox.information(self, "OpenStitch", f"Saved {target}")
        except Exception as error:
            QMessageBox.critical(self, "OpenStitch", str(error))

    def export_realistic_preview(self) -> None:
        if self.state is None:
            QMessageBox.information(self, "OpenStitch", "Open a design first.")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Realistic Screenshot")
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        fabric_input = QLineEdit(str(self.state.settings.get("fabric_color", "#fbfcfa")))
        fabric_button = QPushButton("Choose")
        fabric_row = QHBoxLayout()
        fabric_row.addWidget(fabric_input, 1)
        fabric_row.addWidget(fabric_button)
        thread_weight = QComboBox()
        thread_weight.addItem("40 wt polyester/rayon", "40wt")
        thread_weight.addItem("30 wt thicker thread", "30wt")
        thread_weight.addItem("60 wt fine thread", "60wt")
        current_weight = str(self.state.settings.get("thread_weight", DEFAULT_THREAD_WEIGHT))
        weight_index = thread_weight.findData(current_weight)
        if weight_index >= 0:
            thread_weight.setCurrentIndex(weight_index)
        output_width = QSpinBox()
        output_width.setRange(900, 4200)
        output_width.setSingleStep(100)
        output_width.setSuffix(" px")
        output_width.setValue(2600)
        include_hoop = QCheckBox("Show hoop/fabric frame")
        include_hoop.setChecked(True)

        def choose_export_fabric() -> None:
            color = QColorDialog.getColor(QColor(fabric_input.text()), dialog, "Choose screenshot fabric color")
            if color.isValid():
                fabric_input.setText(color.name())

        fabric_button.clicked.connect(choose_export_fabric)
        form.addRow("Fabric color", fabric_row)
        form.addRow("Thread thickness", thread_weight)
        form.addRow("Image width", output_width)
        form.addRow("", include_hoop)
        layout.addLayout(form)
        hint = QLabel("Exports the current selected color blocks as a presentation PNG with simulated thread thickness, shadow, sheen, and fabric texture.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            return
        target, _ = QFileDialog.getSaveFileName(
            self,
            "Export realistic preview",
            str(self.state.pes_path.with_suffix(".realistic.png")),
            "PNG image (*.png)",
        )
        if not target:
            return
        try:
            image = realistic_preview_image(
                self.state.segments,
                self.state.bounds,
                fabric_color=fabric_input.text().strip(),
                thread_weight=str(thread_weight.currentData() or DEFAULT_THREAD_WEIGHT),
                selected_blocks=self.selected_blocks(),
                max_width_px=output_width.value(),
                include_hoop=include_hoop.isChecked(),
            )
            image.save(target)
            QMessageBox.information(self, "OpenStitch", f"Saved {target}")
        except Exception as error:
            QMessageBox.critical(self, "OpenStitch", str(error))

    def update_shopping_list(self) -> None:
        if self.state is None:
            self.shopping_label.setText("")
            return
        self.shopping_label.setText(self.shopping_list_text(max_lines=12))

    def shopping_list_text(self, max_lines: int | None = None) -> str:
        if self.state is None:
            return "Thread shopping list\n\nNo design loaded.\n"
        usage = estimate_thread_usage(self.state.segments)
        catalog = self.selected_catalog()
        inventory = load_inventory()
        lines = ["Shopping list"]
        for row in self.thread_rows:
            if not row.checkbox.isChecked():
                continue
            color = row.block["color"]
            if inventory:
                closest_owned = min(inventory, key=lambda item: self._rgb_distance(color, item["color"]))
                if self._rgb_distance(color, closest_owned["color"]) <= 64:
                    continue
            if not catalog:
                continue
            match = min(catalog, key=lambda item: self._rgb_distance(color, item["color"]))
            meters = usage.get(row.block["index"], 0.0)
            lines.append(f"{match['brand']} {match['number']} {match['name']} ({match['color']}) - {meters:.2f} m")
        if len(lines) == 1:
            lines.append("All selected colors have close inventory matches.")
        return "\n".join(lines[:max_lines] if max_lines else lines)

    def add_thread_row_to_inventory(self, row: ThreadRow) -> None:
        brand, name, color = row.inventory_details(self.selected_thread_brand())
        try:
            add_inventory_item(
                brand=brand,
                name=name,
                color=color,
                quantity=1,
            )
        except Exception as error:
            QMessageBox.critical(self, "OpenStitch", str(error))
            return
        self.refresh_inventory()
        self.update_shopping_list()
        QMessageBox.information(self, "OpenStitch", f"Added {brand} {name} ({normalize_hex(color)}) to inventory.")

    def refresh_inventory(self) -> None:
        if not hasattr(self, "inventory_table"):
            return
        items = load_inventory()
        self.inventory_table.setRowCount(len(items))
        for row, item in enumerate(items):
            brand = QTableWidgetItem(item["brand"])
            brand.setData(Qt.UserRole, item["id"])
            self.inventory_table.setItem(row, 0, brand)
            self.inventory_table.setItem(row, 1, QTableWidgetItem(item["name"]))
            self.inventory_table.setItem(row, 2, QTableWidgetItem(item["color"]))
            self.inventory_table.setItem(row, 3, QTableWidgetItem(str(item["quantity"])))

    def add_inventory_thread(self) -> None:
        try:
            add_inventory_item(
                brand=self.inventory_brand.text(),
                name=self.inventory_name.text(),
                color=self.inventory_color.text(),
                quantity=self.inventory_qty.value(),
            )
        except Exception as error:
            QMessageBox.critical(self, "OpenStitch", str(error))
            return
        self.inventory_brand.clear()
        self.inventory_name.clear()
        self.inventory_color.setText("#000000")
        self.inventory_qty.setValue(1)
        self.refresh_inventory()
        self.update_shopping_list()

    def delete_inventory_thread(self) -> None:
        row = self.inventory_table.currentRow()
        if row < 0:
            return
        item = self.inventory_table.item(row, 0)
        item_id = item.data(Qt.UserRole) if item else ""
        if item_id:
            delete_inventory_item(str(item_id))
        self.refresh_inventory()
        self.update_shopping_list()

    def email_project(self) -> None:
        if self.state is None:
            QMessageBox.information(self, "OpenStitch", "Open a design first.")
            return
        target, _ = QFileDialog.getSaveFileName(
            self,
            "Save Email Draft",
            str(self.state.pes_path.with_name(f"{self.state.pes_path.stem}_email_project.eml")),
            "Email message (*.eml)",
        )
        if not target:
            return
        recipient, ok = QInputDialog.getText(self, "Email Project", "Recipient email address:")
        if not ok:
            return
        eml_path = Path(target)
        zip_name = f"{self.state.pes_path.stem}_project.zip"
        zip_path = eml_path.with_name(zip_name)
        summary_path = self.state.project_path.with_suffix(".summary.txt")
        if summary_path.exists():
            summary_text = summary_path.read_text(encoding="utf-8", errors="replace")
        else:
            summary_text = project_summary_text(
                self.state.working_source,
                self.state.settings,
                self.state.bounds,
                self.state.counts,
                self.state.segments,
                self.state.color_blocks,
            )
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(self.state.pes_path, arcname=self.state.pes_path.name)
            archive.writestr("thread-shopping-list.txt", self.shopping_list_text())
            archive.writestr("project-summary.txt", summary_text)
        message = EmailMessage()
        message["Subject"] = f"OpenStitch project: {self.state.pes_path.stem}"
        if recipient.strip():
            message["To"] = recipient.strip()
        message["X-Unsent"] = "1"
        message.set_content(
            "Attached is the OpenStitch project ZIP with the PES file, thread shopping list, and project summary.\n"
        )
        message.add_attachment(
            zip_path.read_bytes(),
            maintype="application",
            subtype="zip",
            filename=zip_name,
        )
        eml_path.write_bytes(message.as_bytes())
        try:
            os.startfile(str(eml_path))
        except OSError:
            pass
        QMessageBox.information(self, "OpenStitch", f"Saved email draft and ZIP:\n{eml_path}\n{zip_path}")

    def refresh_library(self) -> None:
        self.library_list.clear()
        OUTPUT_DIR.mkdir(exist_ok=True)
        files = sorted(
            [*OUTPUT_DIR.glob("*.pes"), *OUTPUT_DIR.glob(f"*{PROJECT_SUFFIX}")],
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in files[:80]:
            item = QListWidgetItem(path.name)
            item.setData(Qt.UserRole, str(path))
            self.library_list.addItem(item)

    def load_library_item(self, item: QListWidgetItem | None) -> None:
        if item is None:
            return
        self.load_path(Path(item.data(Qt.UserRole)))

    def delete_library_item(self) -> None:
        item = self.library_list.currentItem()
        if item is None:
            return
        path = Path(item.data(Qt.UserRole))
        if QMessageBox.question(self, "OpenStitch", f"Delete {path.name}?") != QMessageBox.Yes:
            return
        for related in [path, thread_metadata_path(path), path.with_suffix(".summary.txt")]:
            if related.exists():
                related.unlink()
        self.refresh_library()

    def update_canvas_flags(self) -> None:
        self.canvas.show_jumps = self.show_jumps.isChecked()
        self.canvas.show_points = self.show_points.isChecked()
        self.canvas.show_markers = self.show_markers.isChecked()
        self.canvas.set_edit_mode(self.edit_stitches.isChecked())
        self.canvas.update()

    def active_edit_block(self) -> dict | None:
        if self.state is None or not self.state.color_blocks:
            return None
        for row in getattr(self, "thread_rows", []):
            if row.checkbox.isChecked():
                return row.block
        return self.state.color_blocks[0]

    def add_manual_stitch(self, point: tuple[float, float]) -> None:
        if self.state is None:
            return
        block = self.active_edit_block()
        if block is None:
            return
        anchor = getattr(self, "_manual_stitch_anchor", None)
        self._manual_stitch_anchor = point
        if anchor is None:
            self.stats_label.setText(self.stats_label.text() + "\nManual stitch start set.")
            return
        self.push_undo_snapshot()
        step = max((segment.get("step", 0) for segment in self.state.segments), default=0) + 1
        segment = {
            "x1": anchor[0],
            "y1": anchor[1],
            "x2": point[0],
            "y2": point[1],
            "kind": "stitch",
            "color": block["color"],
            "colorIndex": block.get("thread", block["index"]),
            "blockIndex": block["index"],
            "step": step,
        }
        self.state.segments.append(segment)
        block["stitches"] = int(block.get("stitches", 0)) + 1
        self.state.commands.append(
            {
                "x": point[0],
                "y": point[1],
                "command": "stitch",
                "color": block["index"],
                "step": step,
            }
        )
        self.recompute_state_after_manual_edit()

    def delete_nearest_stitch(self, point: tuple[float, float]) -> None:
        if self.state is None:
            return
        best_index = None
        best_distance = float("inf")
        for index, segment in enumerate(self.state.segments):
            if segment.get("kind") != "stitch":
                continue
            distance = self._distance_to_segment(
                point,
                (segment["x1"], segment["y1"]),
                (segment["x2"], segment["y2"]),
            )
            if distance < best_distance:
                best_distance = distance
                best_index = index
        if best_index is None or best_distance > 1.5:
            return
        self.push_undo_snapshot()
        removed = self.state.segments.pop(best_index)
        for block in self.state.color_blocks:
            if block["index"] == removed.get("blockIndex"):
                block["stitches"] = max(0, int(block.get("stitches", 0)) - 1)
                break
        self._manual_stitch_anchor = None
        self.recompute_state_after_manual_edit()

    def recompute_state_after_manual_edit(self) -> None:
        if self.state is None:
            return
        self.recount_block_stitches()
        counts = dict(self.state.counts)
        counts["needle_points"] = sum(1 for segment in self.state.segments if segment["kind"] == "stitch")
        counts["stitch_segments"] = counts["needle_points"]
        counts["jumps"] = sum(1 for segment in self.state.segments if segment["kind"] != "stitch")
        self.state.counts = counts
        self.state.segments, self.state.commands = normalize_positive_coordinates(
            self.state.segments,
            self.state.commands,
        )
        self.state.bounds = design_bounds(self.state.segments, self.state.commands)
        self.refresh_state_view(preserve_view=True)

    def refresh_state_view(self, preserve_view: bool = True) -> None:
        if self.state is None:
            return
        zoom = self.canvas.zoom
        pan = QPointF(self.canvas.pan)
        self.canvas.set_design(self.state.segments, self.state.commands, self.state.bounds)
        if preserve_view:
            self.canvas.zoom = zoom
            self.canvas.pan = pan
        self.step_slider.setRange(0, self.state.counts.get("needle_points", 0))
        self.step_slider.setValue(self.state.counts.get("needle_points", 0))
        self.populate_threads()
        self.canvas.set_visible_blocks(self.selected_blocks())
        self.update_stats()
        self.update_shopping_list()
        self.update_color_block_preview()

    def recount_block_stitches(self) -> None:
        if self.state is None:
            return
        counts_by_block = {block["index"]: 0 for block in self.state.color_blocks}
        for segment in self.state.segments:
            if segment.get("kind") == "stitch" and segment.get("blockIndex") in counts_by_block:
                counts_by_block[segment["blockIndex"]] += 1
        for block in self.state.color_blocks:
            block["stitches"] = counts_by_block.get(block["index"], 0)

    def _distance_to_segment(
        self,
        point: tuple[float, float],
        start: tuple[float, float],
        end: tuple[float, float],
    ) -> float:
        dx = end[0] - start[0]
        dy = end[1] - start[1]
        if dx == 0 and dy == 0:
            return math.hypot(point[0] - start[0], point[1] - start[1])
        t = max(0.0, min(1.0, ((point[0] - start[0]) * dx + (point[1] - start[1]) * dy) / (dx * dx + dy * dy)))
        nearest = (start[0] + dx * t, start[1] + dy * t)
        return math.hypot(point[0] - nearest[0], point[1] - nearest[1])

    def toggle_playback(self) -> None:
        if self.play_timer.isActive():
            self.pause_playback()
        else:
            self.start_playback()

    def start_playback(self) -> None:
        if self.step_slider.value() >= self.step_slider.maximum():
            self.step_slider.setValue(0)
        self.play_timer.start(40)

    def pause_playback(self) -> None:
        self.play_timer.stop()

    def stop_playback(self) -> None:
        self.play_timer.stop()
        self.step_slider.setValue(0)

    def advance_playback(self) -> None:
        next_value = self.step_slider.value() + 20
        if next_value >= self.step_slider.maximum():
            self.step_slider.setValue(self.step_slider.maximum())
            self.play_timer.stop()
        else:
            self.step_slider.setValue(next_value)

    @staticmethod
    def _rgb_distance(first: str, second: str) -> float:
        def rgb(value: str) -> tuple[int, int, int]:
            text = value.lstrip("#")
            return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)

        a = rgb(first)
        b = rgb(second)
        return math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2])


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("OpenStitch")
    window = OpenStitchWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
