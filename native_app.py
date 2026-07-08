from __future__ import annotations

import base64
import json
import math
import shutil
import sys
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pyembroidery as embroidery
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from image_digitizer import image_to_segments, is_raster_source, svg_needs_rasterization
from pes_viewer import (
    apply_thread_metadata,
    collect_segments,
    collect_svg_segments,
    design_bounds,
    estimate_stitch_time,
    estimate_thread_usage,
    group_color_blocks_by_inventory,
    thread_metadata_path,
    write_segments_as_pes,
)
from thread_catalog import load_thread_catalog
from thread_inventory import normalize_hex
from thread_settings import DEFAULT_THREAD_WEIGHT, recommended_fill_spacing
from viewer_server import project_settings, project_summary_text, safe_name, write_project_file


OUTPUT_DIR = Path.cwd() / "viewer_output"
PROJECT_SUFFIX = ".embdproj"


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

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt override
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.zoom = max(0.12, min(30.0, self.zoom * factor))
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.LeftButton:
            self._drag_start = event.pos()
            self._drag_pan = QPointF(self.pan)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt override
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

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.fillRect(self.rect(), QColor("#fbfcfa"))
        self._draw_grid(painter)
        if not self.segments:
            painter.setPen(QColor("#52605a"))
            painter.drawText(self.rect(), Qt.AlignCenter, "Import a design to preview stitches")
            return
        self._draw_segments(painter)
        if self.show_markers:
            self._draw_markers(painter)

    def _draw_grid(self, painter: QPainter) -> None:
        min_x, min_y, max_x, max_y = self.bounds
        scale, offset_x, offset_y = self._transform()
        painter.setPen(QPen(QColor("#edf0ec"), 1))
        step = 10
        start_x = math.floor(min_x / step) * step
        end_x = math.ceil(max_x / step) * step
        start_y = math.floor(min_y / step) * step
        end_y = math.ceil(max_y / step) * step
        x = start_x
        while x <= end_x:
            sx = x * scale + offset_x
            painter.drawLine(QPointF(sx, 0), QPointF(sx, self.height()))
            x += step
        y = start_y
        while y <= end_y:
            sy = y * scale + offset_y
            painter.drawLine(QPointF(0, sy), QPointF(self.width(), sy))
            y += step

    def _draw_segments(self, painter: QPainter) -> None:
        scale, _, _ = self._transform()
        stitch_width = max(1, min(3, int(round(scale * 0.08))))
        point_radius = max(1.2, stitch_width * 1.8)
        for segment in self.segments:
            if segment.get("step", 0) > self.current_step:
                continue
            if self.visible_blocks is not None and segment["blockIndex"] not in self.visible_blocks:
                continue
            kind = segment["kind"]
            if kind != "stitch" and not self.show_jumps:
                continue
            if kind == "stitch":
                color = QColor(segment["color"])
                pen = QPen(color, stitch_width)
            elif kind == "travel_after_color_change":
                pen = QPen(QColor("#2b7fff"), 1, Qt.DashLine)
            elif kind == "travel_after_trim":
                pen = QPen(QColor("#ff8a3d"), 1, Qt.DashLine)
            else:
                pen = QPen(QColor("#a6aaa5"), 1, Qt.DotLine)
            painter.setPen(pen)
            start = self._point(segment["x1"], segment["y1"])
            end = self._point(segment["x2"], segment["y2"])
            painter.drawLine(start, end)
            if self.show_points and kind == "stitch":
                painter.setBrush(QColor(segment["color"]))
                painter.setPen(QPen(QColor("#172026"), 0.6))
                painter.drawEllipse(end, point_radius, point_radius)

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


class ThreadRow(QWidget):
    def __init__(self, block: dict, on_changed) -> None:
        super().__init__()
        self.block = block
        self.on_changed = on_changed
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        self.checkbox = QCheckBox()
        self.checkbox.setChecked(True)
        self.checkbox.stateChanged.connect(on_changed)
        self.swatch = QPushButton()
        self.swatch.setFixedSize(28, 24)
        self.swatch.clicked.connect(self.choose_color)
        self.label = QLabel()
        self.label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.checkbox)
        layout.addWidget(self.swatch)
        layout.addWidget(self.label, 1)
        self.refresh()

    def refresh(self) -> None:
        color = self.block["color"]
        self.swatch.setStyleSheet(f"background:{color}; border:1px solid #89958f;")
        self.label.setText(
            f"Block {self.block['index'] + 1}: {self.block.get('label', color)}\n"
            f"{color} - {self.block.get('stitches', 0)} stitches"
        )

    def choose_color(self) -> None:
        chosen = QColorDialog.getColor(QColor(self.block["color"]), self, "Choose thread color")
        if not chosen.isValid():
            return
        self.block["color"] = normalize_hex(chosen.name())
        self.refresh()
        self.on_changed()


class OpenStitchWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("OpenStitch")
        self.resize(1320, 820)
        OUTPUT_DIR.mkdir(exist_ok=True)
        self.state: DesignState | None = None
        self.thread_rows: list[ThreadRow] = []
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
        root = QSplitter(Qt.Horizontal)
        self.setCentralWidget(root)

        left = QTabWidget()
        left.setMinimumWidth(310)
        left.addTab(self._conversion_tab(), "Convert")
        left.addTab(self._library_tab(), "Library")
        root.addWidget(left)

        self.canvas = StitchCanvas()
        root.addWidget(self.canvas)

        right = self._thread_panel()
        right.setMinimumWidth(320)
        root.addWidget(right)
        root.setSizes([330, 700, 330])

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
        file_menu.addSeparator()
        exit_action = QAction("Exit", self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        view_menu = self.menuBar().addMenu("&View")
        reset_action = QAction("Reset Zoom", self)
        reset_action.triggered.connect(lambda: self.canvas.reset_view())
        view_menu.addAction(reset_action)

    def _conversion_tab(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        form = QFormLayout()
        self.fit_width = QDoubleSpinBox()
        self.fit_width.setRange(1, 300)
        self.fit_width.setValue(90)
        self.fit_width.setSuffix(" mm")
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
        self.fill_mode = QComboBox()
        self.fill_mode.addItems(["tatami", "crosshatch", "horizontal"])
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
        self.pdf_page = QSpinBox()
        self.pdf_page.setRange(1, 999)
        self.pdf_page.setValue(1)
        form.addRow("Fit width", self.fit_width)
        form.addRow("Fill spacing", self.fill_spacing)
        form.addRow("Max stitch", self.max_stitch)
        form.addRow("Fill mode", self.fill_mode)
        form.addRow("Fill angle", self.fill_angle)
        form.addRow("Max colors", self.max_colors)
        form.addRow("Color flattening", self.color_merge)
        form.addRow("PDF page", self.pdf_page)
        layout.addLayout(form)
        open_button = QPushButton("Open and Convert")
        open_button.clicked.connect(self.open_design)
        layout.addWidget(open_button)
        safe_density = QPushButton("Apply Safer Density")
        safe_density.clicked.connect(self.apply_safer_density)
        layout.addWidget(safe_density)
        self.stats_label = QLabel("No design loaded.")
        self.stats_label.setWordWrap(True)
        self.stats_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.stats_label)
        layout.addStretch(1)
        return panel

    def _connect_setting_refresh(self) -> None:
        for widget in [
            self.fit_width,
            self.fill_spacing,
            self.max_stitch,
            self.fill_angle,
            self.color_merge,
        ]:
            widget.valueChanged.connect(self.schedule_refresh)
        self.max_colors.valueChanged.connect(self.schedule_refresh)
        self.pdf_page.valueChanged.connect(self.schedule_refresh)
        self.fill_mode.currentTextChanged.connect(self.schedule_refresh)

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

    def _thread_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.addWidget(QLabel("Threads"))
        self.thread_container = QWidget()
        self.thread_layout = QVBoxLayout(self.thread_container)
        self.thread_layout.addStretch(1)
        layout.addWidget(self.thread_container, 1)
        self.shopping_label = QLabel("")
        self.shopping_label.setWordWrap(True)
        self.shopping_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.shopping_label)
        buttons = QHBoxLayout()
        save = QPushButton("Save PES")
        save.clicked.connect(self.save_pes_as)
        project = QPushButton("Save Project")
        project.clicked.connect(self.save_project_as)
        buttons.addWidget(save)
        buttons.addWidget(project)
        layout.addLayout(buttons)
        controls = QFrame()
        control_layout = QVBoxLayout(controls)
        self.step_slider = QSlider(Qt.Horizontal)
        self.step_slider.valueChanged.connect(self.canvas.set_step)
        self.step_label = QLabel("0 / 0")
        self.step_slider.valueChanged.connect(lambda value: self.step_label.setText(f"{value} / {self.step_slider.maximum()}"))
        play = QPushButton("Play")
        play.clicked.connect(self.toggle_playback)
        self.show_jumps = QCheckBox("Show jumps")
        self.show_jumps.setChecked(False)
        self.show_jumps.stateChanged.connect(self.update_canvas_flags)
        self.show_points = QCheckBox("Show needle points")
        self.show_points.setChecked(False)
        self.show_points.stateChanged.connect(self.update_canvas_flags)
        self.show_markers = QCheckBox("Show trims/color changes")
        self.show_markers.setChecked(False)
        self.show_markers.stateChanged.connect(self.update_canvas_flags)
        control_layout.addWidget(play)
        control_layout.addWidget(self.step_label)
        control_layout.addWidget(self.step_slider)
        control_layout.addWidget(self.show_jumps)
        control_layout.addWidget(self.show_points)
        control_layout.addWidget(self.show_markers)
        layout.addWidget(controls)
        return panel

    def current_settings(self) -> dict:
        return project_settings(
            fit_width=self.fit_width.value(),
            fill_spacing=self.fill_spacing.value(),
            thread_weight=DEFAULT_THREAD_WEIGHT,
            max_stitch=self.max_stitch.value(),
            fill_mode=self.fill_mode.currentText(),
            fill_angle_deg=self.fill_angle.value(),
            max_colors=self.max_colors.value(),
            color_merge_distance=self.color_merge.value(),
            pdf_page=self.pdf_page.value(),
        )

    def apply_settings_to_controls(self, settings: dict) -> None:
        self._loading_settings = True
        try:
            if settings.get("fit_width_mm") not in {"", None}:
                self.fit_width.setValue(float(settings["fit_width_mm"]))
            self.fill_spacing.setValue(float(settings.get("fill_spacing_mm", self.fill_spacing.value())))
            self.max_stitch.setValue(float(settings.get("max_stitch_mm", self.max_stitch.value())))
            mode = str(settings.get("fill_mode", self.fill_mode.currentText()))
            index = self.fill_mode.findText(mode)
            if index >= 0:
                self.fill_mode.setCurrentIndex(index)
            self.fill_angle.setValue(float(settings.get("fill_angle_deg", self.fill_angle.value())))
            self.max_colors.setValue(int(settings.get("max_colors", self.max_colors.value())))
            self.color_merge.setValue(float(settings.get("color_merge_distance", self.color_merge.value())))
            self.pdf_page.setValue(int(settings.get("pdf_page", self.pdf_page.value())))
        finally:
            self._loading_settings = False

    def schedule_refresh(self, *args) -> None:
        if self._loading_settings or self.state is None:
            return
        self.stats_label.setText("Updating stitches...")
        self.refresh_timer.start()

    def apply_safer_density(self) -> None:
        self._loading_settings = True
        try:
            self.fill_mode.setCurrentText("tatami")
            self.fill_spacing.setValue(max(self.fill_spacing.value(), 0.45))
        finally:
            self._loading_settings = False
        self.schedule_refresh()

    def refresh_current_design(self) -> None:
        if self.state is None:
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
        working_source = source_path
        if source_path.parent.resolve() != OUTPUT_DIR.resolve():
            name = safe_name(source_path.name)
            job_id = uuid.uuid4().hex[:10]
            working_source = OUTPUT_DIR / f"{Path(name).stem}_{job_id}{Path(name).suffix.lower()}"
            shutil.copy2(source_path, working_source)
        segments, commands, blocks, counts = self.collect_design(working_source, settings)
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
            )
            thread_metadata_path(pes_path).write_text(json.dumps({"blocks": written}, indent=2), encoding="utf-8")
            summary = project_summary_text(working_source, settings, bounds, counts)
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
        self.refresh_library()

    def collect_design(self, path: Path, settings: dict) -> tuple[list[dict], list[dict], list[dict], dict]:
        suffix = path.suffix.lower()
        if suffix == ".svg" and not svg_needs_rasterization(path):
            result = collect_svg_segments(
                path,
                sample_step_mm=0.8,
                fill_spacing_mm=float(settings["fill_spacing_mm"]),
                max_stitch_mm=float(settings["max_stitch_mm"]),
                fill_angle_deg=float(settings["fill_angle_deg"]),
                fill_mode=str(settings["fill_mode"]),
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
                color_merge_distance=float(settings["color_merge_distance"]),
                fill_spacing_mm=float(settings["fill_spacing_mm"]),
                max_stitch_mm=float(settings["max_stitch_mm"]),
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
        return group_color_blocks_by_inventory(*result)

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
        if command_density > 3.0:
            quality_notes.append(
                "High saturation risk. Try Apply Safer Density or increase fill spacing."
            )
        if micro_segments:
            quality_notes.append(f"{micro_segments} preview stitch segments are under 0.30 mm.")
        quality_text = "\n".join(quality_notes) if quality_notes else "Quality checks: no obvious density warning."
        self.stats_label.setText(
            f"{self.state.working_source.name}\n"
            f"Size: {max_x - min_x:.1f} x {max_y - min_y:.1f} mm\n"
            f"Needle points: {counts.get('needle_points', 0)}\n"
            f"Jumps: {counts.get('jumps', 0)}  Trims: {counts.get('trims', 0)}  "
            f"Color changes: {counts.get('color_changes', 0)}\n"
            f"Density: {stitch_density:.2f} st/mm2, {command_density:.2f} commands/mm2\n"
            f"Estimated stitch time: {estimate_stitch_time(counts, self.state.color_blocks)}\n"
            f"PES: {self.state.pes_path.name}\n"
            f"{quality_text}"
        )

    def populate_threads(self) -> None:
        while self.thread_layout.count() > 1:
            item = self.thread_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.thread_rows = []
        if self.state is None:
            return
        for block in self.state.color_blocks:
            row = ThreadRow(block, self.thread_changed)
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
            )
            write_project_file(Path(target), self.state.working_source, self.state.settings, summary)
            Path(target).with_suffix(".summary.txt").write_text(summary, encoding="utf-8")
            QMessageBox.information(self, "OpenStitch", f"Saved {target}")
        except Exception as error:
            QMessageBox.critical(self, "OpenStitch", str(error))

    def update_shopping_list(self) -> None:
        if self.state is None:
            self.shopping_label.setText("")
            return
        usage = estimate_thread_usage(self.state.segments)
        catalog = self.catalog
        lines = ["Shopping list"]
        for row in self.thread_rows:
            if not row.checkbox.isChecked():
                continue
            color = row.block["color"]
            if not catalog:
                continue
            match = min(catalog, key=lambda item: self._rgb_distance(color, item["color"]))
            meters = usage.get(row.block["index"], 0.0)
            lines.append(f"{match['brand']} {match['number']} {match['name']} ({match['color']}) - {meters:.2f} m")
        self.shopping_label.setText("\n".join(lines[:12]))

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
        self.canvas.update()

    def toggle_playback(self) -> None:
        if self.play_timer.isActive():
            self.play_timer.stop()
        else:
            if self.step_slider.value() >= self.step_slider.maximum():
                self.step_slider.setValue(0)
            self.play_timer.start(40)

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
