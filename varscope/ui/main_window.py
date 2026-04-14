from __future__ import annotations

from bisect import bisect_left, bisect_right
import colorsys
from collections import deque
from collections.abc import Iterable
import math
import ctypes
import sys
from pathlib import Path
import threading
import time

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import QEvent, QModelIndex, QRect, QRectF, QSize, QTimer, Qt, Signal
from PySide6.QtGui import QBrush, QCloseEvent, QColor, QCursor, QIcon, QImage, QMouseEvent, QPainter, QPainterPath, QPen, QPixmap, QRegion, QShowEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QCompleter,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QSizePolicy,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QListView,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStatusBar,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from varscope.core.controller import SessionController
from varscope.core.decoder import format_value
from varscope.models import ProbePreset, SampleFrame, WatchDefinition
from varscope.paths import resource_path
from varscope.presets import delete_preset_archive, save_preset_archive, saved_presets


PRESET_ROLE = Qt.UserRole + 1
PLOT_CORNER_RADIUS = 5
ICON_CORNER_RADIUS = 3
DISPLAY_HISTORY_SECONDS = 30.0
DISPLAY_MAX_HZ = 120.0
DISPLAY_MAX_POINTS = 12_000
MAIN_FOLLOW_WINDOW_SECONDS = 5.0
PLOT_REFRESH_INTERVAL_NS = 33_333_333
OVERVIEW_REFRESH_INTERVAL_NS = 1_000_000_000
USER_PLOT_INTERACTION_WINDOW_NS = 1_000_000_000
VALUE_REFRESH_INTERVAL_NS = 100_000_000
MAX_FRAMES_PER_POLL = 256
GUI_POLL_INTERVAL_MS = 16
MAIN_PLOT_MAX_POINTS = 900
OVERVIEW_PLOT_MAX_POINTS = 400
WATCH_COLOR_PALETTE = (
    "#3BA7FF",
    "#FF7A59",
    "#53C267",
    "#FFB000",
    "#9B6DFF",
    "#00B5AD",
    "#FF5C8A",
    "#8D99AE",
)


def _show_message(parent: QWidget, icon: QMessageBox.Icon, title: str, text: object) -> None:
    box = QMessageBox(parent)
    box.setIcon(icon)
    box.setWindowTitle(title)
    box.setText(str(text))
    box.setTextFormat(Qt.PlainText)
    box.setStandardButtons(QMessageBox.Ok)
    box.exec()


def _show_warning(parent: QWidget, title: str, text: object) -> None:
    _show_message(parent, QMessageBox.Warning, title, text)


def _show_critical(parent: QWidget, title: str, text: object) -> None:
    _show_message(parent, QMessageBox.Critical, title, text)


def _show_information(parent: QWidget, title: str, text: object) -> None:
    _show_message(parent, QMessageBox.Information, title, text)


class CheckListWidget(QListWidget):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.NoSelection)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        item = self.itemAt(event.pos())
        if item is None:
            super().mousePressEvent(event)
            return
        item.setCheckState(Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked)
        event.accept()


class PresetItemDelegate(QStyledItemDelegate):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.hovered_row = -1

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        super().paint(painter, option, index)
        if index.row() <= 0:
            return
        if index.row() != self.hovered_row:
            return

        rect = self.delete_rect(option.rect)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#F05A5A"))
        painter.drawRoundedRect(rect.adjusted(0, 0, -1, -1), 6, 6)

        cross_pen = QPen(QColor("#FFFFFF"), 1.8, Qt.SolidLine, Qt.RoundCap)
        painter.setPen(cross_pen)
        inset = max(4, rect.width() // 4)
        left = rect.left() + inset
        right = rect.right() - inset
        top = rect.top() + inset
        bottom = rect.bottom() - inset
        painter.drawLine(left, top, right, bottom)
        painter.drawLine(left, bottom, right, top)
        painter.restore()

    @staticmethod
    def delete_rect(row_rect: QRect) -> QRect:
        size = min(18, max(16, row_rect.height() - 8))
        margin = 4
        return QRect(row_rect.right() - size - margin, row_rect.center().y() - size // 2, size, size)

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        size = super().sizeHint(option, index)
        size.setWidth(size.width() + 24)
        return size


class PresetListView(QListView):
    deleteRequested = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.setStyleSheet("QListView::item { padding-right: 28px; }")
        self._delegate: PresetItemDelegate | None = None
        self._consume_release = False
        self.viewport().installEventFilter(self)

    def bind_delegate(self, delegate: PresetItemDelegate) -> None:
        self._delegate = delegate

    def reset_hover_state(self) -> None:
        self._consume_release = False
        if self._delegate is not None and self._delegate.hovered_row != -1:
            self._delegate.hovered_row = -1
            self.viewport().update()
        self.viewport().unsetCursor()

    def _is_deletable_index(self, index: QModelIndex) -> bool:
        if not index.isValid() or index.row() <= 0:
            return False
        return isinstance(index.data(PRESET_ROLE), ProbePreset)

    def _preset_name_at_index(self, index: QModelIndex) -> str | None:
        preset = index.data(PRESET_ROLE)
        if isinstance(preset, ProbePreset):
            return preset.name
        return None

    def _is_delete_hotspot(self, index: QModelIndex, pos) -> bool:
        if self._delegate is None or not self._is_deletable_index(index):
            return False
        return self._delegate.delete_rect(self.visualRect(index)).contains(pos)

    def _update_hover_state(self, pos) -> None:
        index = self.indexAt(pos)
        hovered_row = index.row() if self._is_deletable_index(index) else -1
        if self._delegate is not None and self._delegate.hovered_row != hovered_row:
            self._delegate.hovered_row = hovered_row
            self.viewport().update()

        if self._is_delete_hotspot(index, pos):
            self.viewport().setCursor(Qt.PointingHandCursor)
        else:
            self.viewport().unsetCursor()

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            index = self.currentIndex()
            name = self._preset_name_at_index(index)
            if name is not None:
                self.deleteRequested.emit(name)
                event.accept()
                return
        super().keyPressEvent(event)

    def eventFilter(self, watched, event):
        if watched is self.viewport():
            event_type = event.type()
            if event_type == QEvent.MouseMove:
                self._update_hover_state(event.pos())
            elif event_type == QEvent.Leave:
                self.reset_hover_state()
            elif event_type == QEvent.MouseButtonPress:
                index = self.indexAt(event.pos())
                if event.button() == Qt.LeftButton and self._is_delete_hotspot(index, event.pos()):
                    name = self._preset_name_at_index(index)
                    if name is not None:
                        self._consume_release = True
                        self.deleteRequested.emit(name)
                    event.accept()
                    return True
            elif event_type == QEvent.MouseButtonRelease:
                if self._consume_release and event.button() == Qt.LeftButton:
                    self._consume_release = False
                    event.accept()
                    return True
                self._consume_release = False
        return super().eventFilter(watched, event)


class PresetComboBox(QComboBox):
    def showPopup(self) -> None:
        super().showPopup()
        view = self.view()
        if view is not None:
            if isinstance(view, PresetListView):
                view.reset_hover_state()
            view.viewport().update()


def _rounded_region(width: int, height: int, radius: int) -> QRegion:
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, width, height), radius, radius)
    return QRegion(path.toFillPolygon().toPolygon())


def _enhance_icon_image(image: QImage) -> QImage:
    image = image.convertToFormat(QImage.Format_ARGB32)
    width = image.width()
    height = image.height()
    if width <= 0 or height <= 0:
        return image

    ptr = image.bits()
    buffer = np.frombuffer(ptr, dtype=np.uint8).reshape((height, image.bytesPerLine() // 4, 4))[:, :width, :].astype(np.float32)
    rgb = buffer[..., :3]

    contrast = 1.28
    rgb = np.clip((rgb - 128.0) * contrast + 128.0, 0.0, 255.0)
    blur = (
        rgb * 4.0
        + np.roll(rgb, 1, axis=0)
        + np.roll(rgb, -1, axis=0)
        + np.roll(rgb, 1, axis=1)
        + np.roll(rgb, -1, axis=1)
    ) / 8.0
    rgb = np.clip(rgb * 1.55 - blur * 0.55, 0.0, 255.0)
    buffer[..., :3] = rgb

    enhanced = np.ascontiguousarray(buffer.astype(np.uint8))
    return QImage(enhanced.data, width, height, enhanced.strides[0], QImage.Format_ARGB32).copy()


def _content_square_image(image: QImage) -> QImage:
    image = image.convertToFormat(QImage.Format_ARGB32)
    width = image.width()
    height = image.height()
    if width <= 0 or height <= 0:
        return image

    ptr = image.bits()
    buffer = np.frombuffer(ptr, dtype=np.uint8).reshape((height, image.bytesPerLine() // 4, 4))[:, :width, :]
    rgb = buffer[..., :3].astype(np.int16)
    alpha = buffer[..., 3]
    non_white = np.any(rgb < 250, axis=2)
    visible = (alpha > 20) & non_white

    coords = np.argwhere(visible)
    if coords.size == 0:
        side = min(width, height)
        x = max(0, (width - side) // 2)
        y = max(0, (height - side) // 2)
        return image.copy(x, y, side, side)

    top = int(coords[:, 0].min())
    bottom = int(coords[:, 0].max())
    left = int(coords[:, 1].min())
    right = int(coords[:, 1].max())

    box_w = right - left + 1
    box_h = bottom - top + 1
    side = max(box_w, box_h)
    pad = max(1, int(round(side * 0.04)))
    side = min(max(side + pad * 2, 1), max(width, height))

    center_x = (left + right) / 2.0
    center_y = (top + bottom) / 2.0
    x = int(round(center_x - side / 2))
    y = int(round(center_y - side / 2))
    x = max(0, min(x, width - side))
    y = max(0, min(y, height - side))
    return image.copy(x, y, side, side)


def _rounded_pixmap(source_path: Path, size: int, radius: int, *, device_pixel_ratio: float = 2.0) -> QPixmap | None:
    image = QImage(str(source_path))
    if image.isNull():
        return None

    scale = max(1.0, float(device_pixel_ratio))
    physical_size = max(1, int(round(size * scale)))
    square = _content_square_image(image)
    scaled = square.scaled(physical_size, physical_size, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    enhanced = _enhance_icon_image(scaled)
    effective_radius = min(radius, max(1, int(round(size * 0.09)))) * scale

    target = QPixmap(physical_size, physical_size)
    target.setDevicePixelRatio(scale)
    target.fill(Qt.transparent)
    painter = QPainter(target)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    clip_path = QPainterPath()
    clip_path.addRoundedRect(QRectF(0, 0, physical_size, physical_size), effective_radius, effective_radius)
    painter.setClipPath(clip_path)
    painter.drawImage(0, 0, enhanced)
    painter.end()
    return target


def _build_window_icon(source_path: Path, radius: int) -> QIcon | None:
    icon = QIcon()
    for size in (16, 20, 24, 32, 40, 48, 64, 96, 128, 256):
        pixmap = _rounded_pixmap(source_path, size, radius)
        if pixmap is not None:
            icon.addPixmap(pixmap)
    return None if icon.isNull() else icon


class PlainAxisItem(pg.AxisItem):
    def tickStrings(self, values, scale, spacing):
        return [_format_plain_tick(float(value) * float(scale), float(spacing) * float(scale)) for value in values]


def _format_plain_tick(value: float, spacing: float) -> str:
    if not np.isfinite(value):
        return ""
    if abs(value) < 1e-12:
        return "0"

    spacing = abs(spacing)
    if spacing <= 0 or not np.isfinite(spacing):
        decimals = 6 if abs(value) < 1 else 3
    elif spacing >= 1:
        decimals = 0 if abs(value) >= 100 else 1
    else:
        decimals = int(min(9, max(0, math.ceil(-math.log10(spacing)) + 1)))

    text = f"{value:.{decimals}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


class NoAutoButtonPlotItem(pg.PlotItem):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, enableMenu=False, **kwargs)
        self.setMenuEnabled(False, enableViewBoxMenu=False)
        self.vb.setMenuEnabled(False)
        self.buttonsHidden = True
        self._remove_auto_button()

    def _remove_auto_button(self) -> None:
        button = getattr(self, "autoBtn", None)
        if button is None:
            return
        try:
            button.clicked.disconnect(self.autoBtnClicked)
        except Exception:
            pass
        try:
            button.hide()
        except Exception:
            pass
        try:
            button.setEnabled(False)
        except Exception:
            pass
        try:
            scene = button.scene()
            if scene is not None:
                scene.removeItem(button)
        except Exception:
            pass
        try:
            button.setParentItem(None)
        except Exception:
            pass
        self.autoBtn = None

    def hideButtons(self) -> None:
        self.buttonsHidden = True

    def showButtons(self) -> None:
        self.buttonsHidden = True

    def updateButtons(self) -> None:
        return

    def autoBtnClicked(self) -> None:
        return


def _make_plot_widget(title: str) -> pg.PlotWidget:
    plot_item = NoAutoButtonPlotItem(
        title=title,
        axisItems={
            "left": PlainAxisItem("left"),
            "bottom": PlainAxisItem("bottom"),
        },
    )
    plot = RoundedPlotWidget(plotItem=plot_item)
    for axis_name in ("left", "bottom"):
        axis = plot.getAxis(axis_name)
        if hasattr(axis, "enableAutoSIPrefix"):
            axis.enableAutoSIPrefix(False)
    return plot

class RoundedPlotWidget(pg.PlotWidget):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFrameShape(QFrame.NoFrame)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setBackground(None)
        self.sceneObj.setBackgroundBrush(Qt.NoBrush)
        self.setStyleSheet('background: transparent; border: none;')
        viewport = self.viewport()
        viewport.setAutoFillBackground(False)
        viewport.setAttribute(Qt.WA_StyledBackground, True)
        viewport.setStyleSheet('background: transparent; border: none;')


class RoundedPlotContainer(QFrame):
    def __init__(self, child: QWidget, *, corner_radius: int = PLOT_CORNER_RADIUS, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName('roundedPlotContainer')
        self.setStyleSheet(
            f"QFrame#roundedPlotContainer {{ background-color: #0b0f14; border: 1px solid #1d2630; border-radius: {corner_radius}px; }}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)
        layout.addWidget(child)


class BatchAddDialog(QDialog):
    def __init__(self, candidates: list[str], *, initial_filter: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self._candidates = sorted(candidates, key=str.lower)
        self._selected_names: set[str] = set()

        self.setWindowTitle("批量添加变量")
        self.resize(860, 560)

        root = QVBoxLayout(self)
        self.search_edit = QLineEdit(initial_filter)
        self.search_edit.setPlaceholderText("输入关键字筛选变量，然后勾选后加入监视")

        self.candidate_list = CheckListWidget()
        self.candidate_list.itemChanged.connect(self._on_candidate_item_changed)

        self.selected_count_label = QLabel("已选：0")
        self.selected_list = QListWidget()
        self.selected_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.remove_button = QPushButton("移除已选")
        self.remove_button.clicked.connect(self._remove_selected_entries)

        self.confirm_button = QPushButton("加入监视")
        self.cancel_button = QPushButton("取消")
        self.confirm_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)

        columns = QHBoxLayout()

        left_column = QVBoxLayout()
        left_column.addWidget(QLabel("候选变量"))
        left_column.addWidget(self.candidate_list, 1)

        right_column = QVBoxLayout()
        right_column.addWidget(self.selected_count_label)
        right_column.addWidget(self.selected_list, 1)
        right_column.addWidget(self.remove_button)

        columns.addLayout(left_column, 3)
        columns.addLayout(right_column, 2)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.confirm_button)
        buttons.addWidget(self.cancel_button)

        root.addWidget(self.search_edit)
        root.addLayout(columns, 1)
        root.addLayout(buttons)

        self.search_edit.textChanged.connect(self._rebuild_candidates)
        self.selected_list.itemDoubleClicked.connect(lambda _: self._remove_selected_entries())
        self._rebuild_candidates()

    def selected_expressions(self) -> list[str]:
        return sorted(self._selected_names, key=str.lower)

    def _on_candidate_item_changed(self, item: QListWidgetItem) -> None:
        name = item.text()
        if item.checkState() == Qt.Checked:
            self._selected_names.add(name)
        else:
            self._selected_names.discard(name)
        self._refresh_selected_list()

    def _refresh_selected_list(self) -> None:
        names = sorted(self._selected_names, key=str.lower)
        self.selected_list.clear()
        self.selected_list.addItems(names)
        self.selected_count_label.setText(f"已选：{len(names)}")

    def _remove_selected_entries(self) -> None:
        for item in self.selected_list.selectedItems():
            self._selected_names.discard(item.text())
        self._rebuild_candidates()

    def _rebuild_candidates(self) -> None:
        keyword = self.search_edit.text().strip().lower()
        self.candidate_list.blockSignals(True)
        self.candidate_list.clear()
        for candidate in self._candidates:
            if keyword and keyword not in candidate.lower():
                continue
            item = QListWidgetItem(candidate)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            item.setCheckState(Qt.Checked if candidate in self._selected_names else Qt.Unchecked)
            self.candidate_list.addItem(item)
        self.candidate_list.blockSignals(False)
        self._refresh_selected_list()


class MainWindow(QMainWindow):
    def __init__(
        self,
        *,
        app_root: Path,
        initial_elf: Path | None = None,
        initial_cfg: Path | None = None,
    ):
        super().__init__()
        self.app_root = Path(app_root)
        self.controller = SessionController(self.app_root)

        self._plot_items: list[tuple[pg.PlotDataItem, pg.PlotDataItem]] = []
        self._watch_values: list[list[float]] = []
        self._watch_times: list[float] = []
        self._latest_value_strings: list[str] = []
        self._updating_table = False
        self._syncing_region = False
        self._refreshing_plots = False
        self._follow_latest = True
        self._manual_y_range = False
        self._last_plot_user_interaction_ns = 0
        self._base_timestamp_ns = 0
        self._cursor_proxy: pg.SignalProxy | None = None
        self._capture_timestamps_ns: deque[int] = deque(maxlen=4096)
        self._plot_refresh_timestamps_ns: deque[int] = deque(maxlen=512)
        self._last_plot_refresh_ns = 0
        self._last_overview_refresh_ns = 0
        self._plot_refresh_pending = False
        self._max_frames_per_poll = MAX_FRAMES_PER_POLL
        self._last_value_refresh_ns = 0
        self._last_display_timestamp_s: float | None = None
        self._display_sample_interval_s = 1.0 / DISPLAY_MAX_HZ
        self._stop_worker: threading.Thread | None = None
        self._stop_error: Exception | None = None

        self.setWindowTitle("VarScope")
        self.setMinimumSize(960, 640)
        self._build_ui()
        self._set_reasonable_startup_size()
        self._refresh_preset_combo()
        self._apply_initial_paths(initial_elf, initial_cfg)

        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.setInterval(GUI_POLL_INTERVAL_MS)
        self.timer.timeout.connect(self._poll_runtime)
        self.timer.start()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._stop_worker is not None:
            self._stop_worker.join(timeout=3.0)
        else:
            self.controller.shutdown()
        super().closeEvent(event)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        self._apply_native_title_bar_style()
        self._remove_native_title_icon()

    def _apply_native_title_bar_style(self) -> None:
        if sys.platform != "win32":
            return
        try:
            hwnd = int(self.winId())
            DWMWA_BORDER_COLOR = 34
            DWMWA_CAPTION_COLOR = 35
            DWMWA_TEXT_COLOR = 36
            white = ctypes.c_uint(0x00FFFFFF)
            black = ctypes.c_uint(0x00000000)
            dwmapi = ctypes.windll.dwmapi
            dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_CAPTION_COLOR, ctypes.byref(white), ctypes.sizeof(white))
            dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_BORDER_COLOR, ctypes.byref(white), ctypes.sizeof(white))
            dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_TEXT_COLOR, ctypes.byref(black), ctypes.sizeof(black))
        except Exception:
            pass

    def _remove_native_title_icon(self) -> None:
        if sys.platform != "win32":
            return
        try:
            hwnd = int(self.winId())
            user32 = ctypes.windll.user32
            GWL_EXSTYLE = -20
            WS_EX_DLGMODALFRAME = 0x00000001
            WM_SETICON = 0x0080
            ICON_SMALL = 0
            ICON_BIG = 1
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_NOZORDER = 0x0004
            SWP_FRAMECHANGED = 0x0020
            GCLP_HICON = -14
            GCLP_HICONSM = -34

            self.setWindowIcon(QIcon())
            exstyle = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE, exstyle | WS_EX_DLGMODALFRAME)
            user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, 0)
            user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, 0)
            if hasattr(user32, "SetClassLongPtrW"):
                user32.SetClassLongPtrW(hwnd, GCLP_HICON, 0)
                user32.SetClassLongPtrW(hwnd, GCLP_HICONSM, 0)
            elif hasattr(user32, "SetClassLongW"):
                user32.SetClassLongW(hwnd, GCLP_HICON, 0)
                user32.SetClassLongW(hwnd, GCLP_HICONSM, 0)
            user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED)
            user32.DrawMenuBar(hwnd)
        except Exception:
            pass

    def _set_reasonable_startup_size(self) -> None:
        screen = self.screen()
        if screen is None:
            self.resize(1120, 720)
            return
        available = screen.availableGeometry()
        width = min(1220, max(980, int(available.width() * 0.68)))
        height = min(760, max(640, int(available.height() * 0.68)))
        self.resize(width, height)
        frame = self.frameGeometry()
        frame.moveCenter(available.center())
        self.move(frame.topLeft())

    def _build_ui(self) -> None:
        root = QWidget(self)
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        brand_row = QHBoxLayout()
        brand_row.setContentsMargins(0, 0, 0, 2)
        brand_row.setSpacing(10)

        self.brand_logo_label = QLabel()
        self.brand_logo_label.setFixedSize(34, 34)
        self.brand_logo_label.setScaledContents(False)
        self.brand_logo_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

        logo_path = resource_path("Wheat.jpg")
        if logo_path.exists():
            brand_pixmap = _rounded_pixmap(logo_path, 34, ICON_CORNER_RADIUS, device_pixel_ratio=1.0)
            if brand_pixmap is not None:
                self.brand_logo_label.setPixmap(brand_pixmap)
        icon_path = resource_path("VarScope.ico")
        if not icon_path.exists():
            icon_path = resource_path("resources", "VarScope.ico")
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        self.brand_title_label = QLabel("VarScope")
        self.brand_title_label.setAlignment(Qt.AlignVCenter | Qt.AlignLeft)
        self.brand_title_label.setStyleSheet("font-size: 19px; font-weight: 700; color: #20262d; padding-top: 11px;")

        brand_row.addWidget(self.brand_logo_label, 0, Qt.AlignVCenter)
        brand_row.addWidget(self.brand_title_label, 0, Qt.AlignVCenter)
        brand_row.addStretch(1)
        outer.addLayout(brand_row)


        connection_box = QGroupBox("连接")
        connection_layout = QGridLayout(connection_box)

        self.elf_path_edit = QLineEdit()
        self.elf_browse_button = QPushButton("打开 elf")
        self.elf_browse_button.clicked.connect(self.open_elf_dialog)

        self.preset_combo = PresetComboBox()
        self.preset_view = PresetListView(self)
        self.preset_delegate = PresetItemDelegate(self.preset_view)
        self.preset_combo.setView(self.preset_view)
        self.preset_view.setItemDelegate(self.preset_delegate)
        self.preset_view.bind_delegate(self.preset_delegate)
        self.preset_view.deleteRequested.connect(self._delete_preset_by_name)
        self.preset_combo.currentIndexChanged.connect(self._on_preset_changed)
        self.archive_button = QPushButton("存档")
        self.archive_button.clicked.connect(self.archive_current_project)

        self.cfg_path_edit = QLineEdit()
        self.cfg_browse_button = QPushButton("选择 cfg")
        self.cfg_browse_button.clicked.connect(self.open_cfg_dialog)

        self.connect_button = QPushButton("连接")
        self.connect_button.setFixedSize(QPushButton("断开").sizeHint())
        self.connect_button.clicked.connect(self.toggle_connection)

        connection_layout.addWidget(QLabel("elf"), 0, 0)
        connection_layout.addWidget(self.elf_path_edit, 0, 1)
        connection_layout.addWidget(self.elf_browse_button, 0, 2)

        connection_layout.addWidget(QLabel("存档"), 1, 0)
        connection_layout.addWidget(self.preset_combo, 1, 1)
        connection_layout.addWidget(self.archive_button, 1, 2)

        connection_layout.addWidget(QLabel("cfg"), 2, 0)
        connection_layout.addWidget(self.cfg_path_edit, 2, 1)
        connection_layout.addWidget(self.cfg_browse_button, 2, 2)

        button_row = QHBoxLayout()
        button_row.addWidget(self.connect_button)
        button_row.addStretch(1)
        connection_layout.addLayout(button_row, 3, 1, 1, 2)

        outer.addWidget(connection_box)

        capture_box = QGroupBox("采集")
        capture_layout = QHBoxLayout(capture_box)

        self.watch_expression_edit = QLineEdit()
        self.watch_expression_edit.setPlaceholderText("输入表达式，例如 yaw_pos_filter.out")
        self.watch_expression_edit.returnPressed.connect(self.add_watch)

        self.add_watch_button = QPushButton("添加变量")
        self.batch_add_button = QPushButton("批量添加")
        self.remove_watch_button = QPushButton("移除勾选")
        self.clear_watches_button = QPushButton("清空变量")
        self.start_button = QPushButton("开始绘图")
        self.stop_button = QPushButton("停止绘图")
        self.export_csv_button = QPushButton("导出 CSV")
        self.follow_latest_button = QPushButton("回到最新")
        self.follow_latest_button.setFixedSize(self.export_csv_button.sizeHint())

        self.add_watch_button.clicked.connect(self.add_watch)
        self.batch_add_button.clicked.connect(self.open_batch_add_dialog)
        self.remove_watch_button.clicked.connect(self.remove_checked_watches)
        self.clear_watches_button.clicked.connect(self.clear_all_watches)
        self.start_button.clicked.connect(self.start_capture)
        self.stop_button.clicked.connect(self.stop_capture)
        self.export_csv_button.clicked.connect(self.export_csv_dialog)
        self.follow_latest_button.clicked.connect(self._jump_to_latest)

        capture_layout.addWidget(self.watch_expression_edit, 3)
        capture_layout.addWidget(self.add_watch_button)
        capture_layout.addWidget(self.batch_add_button)
        capture_layout.addWidget(self.remove_watch_button)
        capture_layout.addWidget(self.clear_watches_button)
        capture_layout.addWidget(self.start_button)
        capture_layout.addWidget(self.stop_button)
        capture_layout.addWidget(self.export_csv_button)
        capture_layout.addWidget(self.follow_latest_button)

        outer.addWidget(capture_box)

        splitter = QSplitter(Qt.Horizontal)
        outer.addWidget(splitter, 1)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)

        column_toggle_row = QHBoxLayout()
        column_toggle_row.addWidget(QLabel("列显示"))

        self.type_column_button = QPushButton("类型")
        self.type_column_button.setCheckable(True)
        self.type_column_button.clicked.connect(self._apply_column_visibility)

        self.address_column_button = QPushButton("地址")
        self.address_column_button.setCheckable(True)
        self.address_column_button.clicked.connect(self._apply_column_visibility)

        self.value_column_button = QPushButton("数值")
        self.value_column_button.setCheckable(True)
        self.value_column_button.setChecked(True)
        self.value_column_button.clicked.connect(self._apply_column_visibility)

        self.point_column_button = QPushButton("点值")
        self.point_column_button.setCheckable(True)
        self.point_column_button.setChecked(True)
        self.point_column_button.clicked.connect(self._apply_column_visibility)

        column_toggle_row.addWidget(self.type_column_button)
        column_toggle_row.addWidget(self.address_column_button)
        column_toggle_row.addWidget(self.value_column_button)
        column_toggle_row.addWidget(self.point_column_button)
        column_toggle_row.addStretch(1)

        self.point_meta_label = QLabel("点值：--")
        self.point_meta_label.setWordWrap(True)

        self.watch_table = QTableWidget(0, 6)
        self.watch_table.setHorizontalHeaderLabels(["绘制", "表达式", "类型", "地址", "数值", "点值"])
        self.watch_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.watch_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.watch_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.watch_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.watch_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.watch_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.watch_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.watch_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.watch_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeToContents)
        self.watch_table.itemChanged.connect(self._on_watch_item_changed)

        left_layout.addLayout(column_toggle_row)
        left_layout.addWidget(self.point_meta_label)
        left_layout.addWidget(self.watch_table, 1)
        splitter.addWidget(left_panel)

        plot_panel = QWidget()
        plot_layout = QVBoxLayout(plot_panel)

        self.main_plot = _make_plot_widget("主图")
        self.main_plot.showGrid(x=True, y=True, alpha=0.25)
        self.main_plot.setLabel("left", "数值")
        self.main_plot.setLabel("bottom", "时间 (s)")
        self.main_view = self.main_plot.getViewBox()
        self.main_view.setMouseMode(pg.ViewBox.RectMode)
        self.main_view.enableAutoRange(axis=pg.ViewBox.XAxis, enable=False)
        self.main_view.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)
        self.main_view.setAutoVisible(y=True)

        self.overview_plot = _make_plot_widget("全局视图")
        self.overview_plot.showGrid(x=True, y=True, alpha=0.15)
        self.overview_plot.setMaximumHeight(180)
        self.overview_plot.setLabel("bottom", "时间 (s)")
        self.overview_view = self.overview_plot.getViewBox()
        self.overview_view.setMouseEnabled(x=False, y=False)
        self.overview_view.enableAutoRange(axis=pg.ViewBox.XAxis, enable=False)
        self.overview_view.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)
        self.main_plot.viewport().installEventFilter(self)
        self.overview_plot.viewport().installEventFilter(self)

        self.region = pg.LinearRegionItem(
            values=(0.0, 1.0),
            brush=(90, 170, 255, 60),
            pen=pg.mkPen("#5AB2FF", width=2),
        )
        self.region.setHoverBrush((110, 190, 255, 80))
        self.region.setZValue(10)
        self.region.sigRegionChanged.connect(self._sync_main_to_region)
        self.overview_plot.addItem(self.region)
        self.main_view.sigXRangeChanged.connect(self._sync_region_to_main)
        self.main_view.sigRangeChanged.connect(self._on_main_view_range_changed)

        self.crosshair_vline = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen("#E8FF6A", width=1))
        self.crosshair_hline = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen("#E8FF6A", width=1))
        self.main_plot.addItem(self.crosshair_vline, ignoreBounds=True)
        self.main_plot.addItem(self.crosshair_hline, ignoreBounds=True)
        self._cursor_proxy = pg.SignalProxy(
            self.main_plot.scene().sigMouseMoved,
            rateLimit=60,
            slot=self._on_plot_mouse_moved,
        )
        self._sync_point_readout_state()

        self.main_plot_container = RoundedPlotContainer(self.main_plot)
        self.overview_plot_container = RoundedPlotContainer(self.overview_plot)
        plot_layout.addWidget(self.main_plot_container, 1)
        plot_layout.addWidget(self.overview_plot_container)
        splitter.addWidget(plot_panel)
        splitter.setSizes([410, 780])

        status = QStatusBar(self)
        self.setStatusBar(status)
        self.elf_status_dot = QLabel("●")
        self.elf_status_text = QLabel("elf 未加载")
        self.status_label = QLabel("就绪")
        self.capture_rate_label = QLabel('采集：-- Hz')
        self.plot_rate_label = QLabel('绘图：-- Hz')
        status.addWidget(self.elf_status_dot)
        status.addWidget(self.elf_status_text)
        status.addPermanentWidget(self.status_label, 1)
        status.addPermanentWidget(self.capture_rate_label)
        status.addPermanentWidget(self.plot_rate_label)

        self._refresh_connection_status()
        self._apply_column_visibility()

    def _refresh_preset_combo(self, select_name: str | None = None) -> None:
        current_name = select_name or self.preset_combo.currentText()
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItem("自定义")
        self.preset_combo.setItemData(0, None, PRESET_ROLE)
        for preset in saved_presets():
            self.preset_combo.addItem(preset.name)
            row = self.preset_combo.count() - 1
            self.preset_combo.setItemData(row, preset, PRESET_ROLE)
        self.preset_combo.blockSignals(False)

        index = self.preset_combo.findText(current_name)
        if index >= 0:
            self.preset_combo.setCurrentIndex(index)
        else:
            self.preset_combo.setCurrentIndex(0)

    def _delete_preset_by_name(self, name: str) -> None:
        name = name.strip()
        if not name:
            return

        row = self.preset_combo.findText(name)
        if row < 0:
            _show_warning(self, "删除失败", f"找不到存档：{name}")
            return

        preset = self.preset_combo.itemData(row, PRESET_ROLE)
        if not isinstance(preset, ProbePreset):
            return
        self.preset_combo.hidePopup()
        answer = QMessageBox.question(
            self,
            "删除存档",
            f"确认删除存档 {name} 吗？\n这会把它从 saved_presets.json 中移除。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        if delete_preset_archive(name):
            self._refresh_preset_combo(select_name="自定义")
            self.status_label.setText(f"已删除存档：{name}")
            return
        _show_warning(self, "删除失败", f"无法删除存档：{name}")

    def _apply_initial_paths(self, initial_elf: Path | None, initial_cfg: Path | None) -> None:
        if initial_cfg:
            self.cfg_path_edit.setText(str(initial_cfg))
        if initial_elf:
            self.load_elf(initial_elf)

    def _apply_column_visibility(self) -> None:
        self.watch_table.setColumnHidden(2, not self.type_column_button.isChecked())
        self.watch_table.setColumnHidden(3, not self.address_column_button.isChecked())
        self.watch_table.setColumnHidden(4, not self.value_column_button.isChecked())
        self.watch_table.setColumnHidden(5, not self.point_column_button.isChecked())
        self.watch_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self._sync_point_readout_state()

    def _refresh_connection_status(self) -> None:
        elf_loaded = self.controller.symbol_db is not None
        connected = self.controller.openocd is not None
        elf_name = Path(self.elf_path_edit.text().strip()).name if self.elf_path_edit.text().strip() else ""
        healthy = elf_loaded and connected
        self.elf_status_dot.setStyleSheet(
            f"color: {'#2DBA4E' if healthy else '#D64545'}; font-size: 16px;"
        )
        self._refresh_connect_button_style(healthy)
        if not elf_loaded:
            self.elf_status_text.setText("elf 未加载")
        elif connected:
            self.elf_status_text.setText(f"elf 已加载并已连接： {elf_name}")
        else:
            self.elf_status_text.setText(f"elf 已加载，未连接： {elf_name}")

    def _refresh_connect_button_style(self, healthy: bool) -> None:
        if healthy:
            background = "#2DBA4E"
            hover = "#34C95A"
            pressed = "#23983F"
            border = "#22963D"
        else:
            background = "#D64545"
            hover = "#E05252"
            pressed = "#B83333"
            border = "#B83333"
        self.connect_button.setText("断开" if healthy else "连接")
        self.connect_button.setStyleSheet(
            "QPushButton {"
            f"background-color: {background};"
            "color: white;"
            f"border: 1px solid {border};"
            "border-radius: 4px;"
            "padding: 3px 12px;"
            "}"
            f"QPushButton:hover {{ background-color: {hover}; }}"
            f"QPushButton:pressed {{ background-color: {pressed}; }}"
        )

    def _point_readout_enabled(self) -> bool:
        return self.point_column_button.isChecked()

    def _sync_point_readout_state(self) -> None:
        enabled = self._point_readout_enabled()
        self.crosshair_vline.setVisible(enabled)
        self.crosshair_hline.setVisible(enabled)
        self.point_meta_label.setVisible(enabled)
        self.point_meta_label.setText("点值：--" if enabled else "点值：关闭")
        self._refresh_point_column(None)

    def open_elf_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择 elf", str(Path.cwd()), "elf (*.elf *.axf *.out)")
        if not path:
            return
        try:
            self.load_elf(Path(path))
        except Exception as exc:
            self._refresh_connection_status()
            _show_critical(self, "elf 加载失败", str(exc))

    def open_cfg_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择 OpenOCD cfg", str(Path.cwd()), "cfg (*.cfg)")
        if not path:
            return
        self.preset_combo.setCurrentIndex(0)
        self.cfg_path_edit.setText(path)

    def archive_current_project(self) -> None:
        cfg_path = self._selected_cfg_path()
        if cfg_path is None or not cfg_path.exists():
            _show_warning(self, "无法存档", "当前 cfg 无效，无法存档。")
            return

        default_name = Path(self.elf_path_edit.text().strip()).stem or cfg_path.stem
        name, accepted = QInputDialog.getText(self, "工程存档", "请输入存档名称：", text=default_name)
        if not accepted:
            return
        name = name.strip()
        if not name:
            _show_warning(self, "无法存档", "存档名称不能为空。")
            return

        elf_text = self.elf_path_edit.text().strip()
        elf_path = Path(elf_text) if elf_text else None
        preset = save_preset_archive(name, cfg_path=cfg_path, elf_path=elf_path)
        self._refresh_preset_combo(select_name=preset.name)
        self.status_label.setText(f"已存档工程：{preset.name}")

    def _on_preset_changed(self, index: int) -> None:
        preset = self.preset_combo.itemData(index, PRESET_ROLE)
        if not isinstance(preset, ProbePreset):
            return
        self.cfg_path_edit.setText(str(preset.cfg_path))
        if preset.elf_path is None:
            return
        self.elf_path_edit.setText(str(preset.elf_path))
        if preset.elf_path.exists():
            try:
                self.load_elf(preset.elf_path)
            except Exception as exc:
                self._refresh_connection_status()
                _show_warning(self, "切换存档失败", f"cfg 已切换，但 elf 加载失败：\n{exc}")
        else:
            self._refresh_connection_status()
            self.status_label.setText(f"已切换到存档 {preset.name}，但 elf 文件不存在。")

    def load_elf(self, path: Path) -> None:
        was_capturing = self.controller.is_capturing()
        elf_path, sha, dropped = self.controller.load_elf(path)
        self.elf_path_edit.setText(str(elf_path))
        self._refresh_connection_status()
        self.status_label.setText(f"elf 符号已加载 / sha256 {sha[:12]}...")

        completer = QCompleter(self.controller.completion_names())
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        completer.setCompletionMode(QCompleter.PopupCompletion)
        self.watch_expression_edit.setCompleter(completer)

        self._rebuild_watch_table()
        self._rebuild_plot_items()

        if was_capturing and self.controller.resolved_watches:
            self._restart_capture_after_watch_change()
        elif was_capturing and not self.controller.resolved_watches:
            self.controller.stop_capture()

        if dropped:
            sample = ", ".join(item.display_name for item in dropped[:5])
            extra = "" if len(dropped) <= 5 else f"等另外 {len(dropped) - 5} 项"
            _show_information(
                self,
                "旧变量已移除",
                f"新的 elf 已加载，但这些旧变量无法解析，已被移除：\n{sample}{extra}",
            )

    def toggle_connection(self) -> None:
        if self._stop_worker is not None:
            return
        if self.controller.openocd is not None:
            self.controller.disconnect()
            self._refresh_connection_status()
            self.status_label.setText("已断开连接。")
            return

        cfg_path = self._selected_cfg_path()
        if cfg_path is None or not cfg_path.exists():
            _show_warning(self, "cfg 缺失", "请先选择有效的 OpenOCD cfg 文件。")
            return
        try:
            self.controller.connect(cfg_path=cfg_path)
        except Exception as exc:
            _show_critical(self, "连接失败", str(exc))
            self._refresh_connection_status()
            return
        self._refresh_connection_status()
        self.status_label.setText(f"已连接 OpenOCD：{cfg_path.name}")

    def add_watch(self) -> None:
        expression = self.watch_expression_edit.text().strip()
        if not expression:
            return
        self._add_watch_definition(
            WatchDefinition(
                expression=expression,
                color=_next_color(definition.color for definition in self.controller.watch_definitions),
            )
        )
        self.watch_expression_edit.clear()

    def open_batch_add_dialog(self) -> None:
        if self.controller.symbol_db is None:
            _show_information(self, "需要 elf", "请先加载 elf，再使用批量添加。")
            return

        dialog = BatchAddDialog(
            self.controller.completion_names(),
            initial_filter=self.watch_expression_edit.text().strip(),
            parent=self,
        )
        if dialog.exec() != QDialog.Accepted:
            return

        existing = {watch.expression for watch in self.controller.watch_definitions}
        added = 0
        failed: list[str] = []
        for expression in dialog.selected_expressions():
            if expression in existing:
                continue
            try:
                self.controller.add_watch(
                    WatchDefinition(
                        expression=expression,
                        color=_next_color(definition.color for definition in self.controller.watch_definitions),
                    )
                )
            except Exception:
                failed.append(expression)
                continue
            existing.add(expression)
            added += 1

        if added:
            self._rebuild_watch_table()
            self._rebuild_plot_items()
            self._restart_capture_after_watch_change()
            self.status_label.setText(f"已批量添加 {added} 个变量。")

        if failed:
            sample = ", ".join(failed[:3])
            extra = "" if len(failed) <= 3 else f"等另外 {len(failed) - 3} 项"
            _show_warning(self, "部分添加失败", f"这些表达式无法添加：\n{sample}{extra}")

    def _add_watch_definition(self, definition: WatchDefinition) -> None:
        try:
            self.controller.add_watch(definition)
        except Exception as exc:
            _show_warning(self, "添加失败", str(exc))
            return
        self._rebuild_watch_table()
        self._rebuild_plot_items()
        self._restart_capture_after_watch_change()

    def remove_checked_watches(self) -> None:
        rows = self._checked_watch_rows()
        if not rows:
            return
        for row in sorted(rows, reverse=True):
            self.controller.remove_watch(row)
        self._rebuild_watch_table()
        self._rebuild_plot_items()
        self._restart_capture_after_watch_change()
        self.status_label.setText(f"已移除 {len(rows)} 个勾选的变量。")

    def clear_all_watches(self) -> None:
        was_capturing = self.controller.is_capturing()
        if was_capturing:
            self.controller.stop_capture()
        self.controller.clear_watches()
        self._rebuild_watch_table()
        self._rebuild_plot_items()
        self._reset_plot_view()
        self.status_label.setText("当前变量已清空。")

    def start_capture(self) -> None:
        if self._stop_worker is not None:
            self._finish_stop_capture_if_ready()
            if self._stop_worker is not None:
                self.status_label.setText("正在停止采集，请稍候...")
                return
        try:
            path = self.controller.start_capture()
        except Exception as exc:
            _show_warning(self, "开始失败", str(exc))
            return
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(True)
        self._set_follow_latest(True)
        self._reset_series()
        self.status_label.setText(f"采集已开始：{path.name}")

    def stop_capture(self) -> None:
        if self._stop_worker is not None:
            return
        self._plot_refresh_pending = False
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.status_label.setText("正在停止采集...")
        self._stop_error = None

        def stop_worker() -> None:
            try:
                self.controller.stop_capture()
            except Exception as exc:
                self._stop_error = exc

        try:
            worker = threading.Thread(target=stop_worker, name="varscope-stop", daemon=True)
            worker.start()
        except Exception as exc:
            self.start_button.setEnabled(True)
            self.stop_button.setEnabled(True)
            self.status_label.setText("停止采集失败。")
            _show_warning(self, "停止失败", str(exc))
            return
        self._stop_worker = worker

    def _finish_stop_capture_if_ready(self) -> bool:
        worker = self._stop_worker
        if worker is None:
            return False
        if worker.is_alive():
            return True

        self._stop_worker = None
        self.controller.drain_frames()
        self._plot_refresh_pending = False
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(True)
        if self._stop_error is not None:
            error = self._stop_error
            self._stop_error = None
            _show_warning(self, "停止失败", str(error))
        else:
            self.status_label.setText("采集已停止。")
        return False

    def export_csv_dialog(self) -> None:
        if self.controller.current_session_path is None:
            _show_information(self, "没有会话", "当前没有可导出的采集数据。")
            return
        default_name = self.controller.current_session_path.with_suffix(".csv")
        path, _ = QFileDialog.getSaveFileName(self, "导出 CSV", str(default_name), "CSV (*.csv)")
        if not path:
            return
        try:
            self.controller.export_csv(Path(path))
        except Exception as exc:
            _show_critical(self, "导出失败", str(exc))
            return
        self.status_label.setText(f"CSV 已导出到 {Path(path).name}")

    def eventFilter(self, watched, event) -> bool:
        if watched in (self.main_plot.viewport(), self.overview_plot.viewport()):
            if self._is_plot_user_interaction(event):
                self._last_plot_user_interaction_ns = time.monotonic_ns()
        return super().eventFilter(watched, event)

    @staticmethod
    def _is_plot_user_interaction(event) -> bool:
        event_type = event.type()
        if event_type in (QEvent.Wheel, QEvent.MouseButtonPress, QEvent.MouseButtonDblClick):
            return True
        if event_type == QEvent.MouseMove and hasattr(event, "buttons"):
            return event.buttons() != Qt.NoButton
        return False

    def _plot_range_change_is_user_driven(self) -> bool:
        if self._last_plot_user_interaction_ns == 0:
            return False
        return time.monotonic_ns() - self._last_plot_user_interaction_ns <= USER_PLOT_INTERACTION_WINDOW_NS

    def _set_follow_latest(self, enabled: bool) -> None:
        self._follow_latest = enabled
        if enabled:
            self._manual_y_range = False
            self._last_plot_user_interaction_ns = 0

    def _jump_to_latest(self) -> None:
        self._set_follow_latest(True)
        self.main_view.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)
        self.main_view.setAutoVisible(y=True)
        if not self._watch_times:
            return
        self._refresh_plots(force=True)
        span = self._latest_follow_span()
        if span is not None:
            self._apply_main_span(span, padding=0)

    def _on_plot_mouse_moved(self, event) -> None:
        self._refresh_cursor_readout(event[0] if event else None)

    def _cursor_scene_pos(self):
        if not self._point_readout_enabled():
            return None
        widget_pos = self.main_plot.mapFromGlobal(QCursor.pos())
        if not self.main_plot.rect().contains(widget_pos):
            return None
        return self.main_plot.mapToScene(widget_pos)

    def _refresh_cursor_readout(self, pos=None) -> None:
        if not self._point_readout_enabled() or not self._watch_times:
            self._refresh_point_column(None)
            return
        if pos is None:
            pos = self._cursor_scene_pos()
        if pos is None or not self.main_plot.sceneBoundingRect().contains(pos):
            self.point_meta_label.setText("点值：--")
            self._refresh_point_column(None)
            return

        view_pos = self.main_view.mapSceneToView(pos)
        cursor_x = float(view_pos.x())
        cursor_y = float(view_pos.y())
        self.crosshair_vline.setPos(cursor_x)
        self.crosshair_hline.setPos(cursor_y)

        index = _nearest_index(self._watch_times, cursor_x)
        if index is None:
            self.point_meta_label.setText("点值：--")
            self._refresh_point_column(None)
            return

        time_value = self._watch_times[index]
        self.point_meta_label.setText(f"点值：t={time_value:.6f}s, y={cursor_y:.6g}")
        self._refresh_point_column(index)

    def _poll_runtime(self) -> None:
        if self._finish_stop_capture_if_ready():
            return

        error = self.controller.pop_error()
        if error:
            _show_warning(self, "采集错误", error)

        frames, frame_timestamps = self.controller.drain_frames_with_timestamps(
            max_frames=self._max_frames_per_poll,
        )
        now_ns = time.monotonic_ns()
        if not frames:
            if self._plot_refresh_pending and now_ns - self._last_plot_refresh_ns >= self._effective_plot_refresh_interval_ns():
                self._refresh_plots()
            return

        self._append_frames(frames, frame_timestamps=frame_timestamps)
        if now_ns - self._last_value_refresh_ns >= VALUE_REFRESH_INTERVAL_NS:
            self._refresh_value_column()
        self._plot_refresh_pending = True
        if now_ns - self._last_plot_refresh_ns >= self._effective_plot_refresh_interval_ns():
            self._refresh_plots()

    def _append_frames(self, frames: list[SampleFrame], *, frame_timestamps: list[int] | None = None) -> None:
        if frame_timestamps:
            self._capture_timestamps_ns.extend(frame_timestamps)
        if not frames:
            return
        if not self._watch_values:
            self._reset_series()
        if self._base_timestamp_ns == 0:
            self._base_timestamp_ns = frames[0].timestamp_ns

        last_decoded_values = None
        last_frame_index = len(frames) - 1
        for frame_index, frame in enumerate(frames):
            timestamp_s = (frame.timestamp_ns - self._base_timestamp_ns) / 1_000_000_000.0
            if frame_timestamps is None:
                self._capture_timestamps_ns.append(frame.timestamp_ns)
            last_decoded_values = frame.decoded_values

            if not self._should_keep_display_frame(timestamp_s, frame_index == last_frame_index):
                continue

            self._watch_times.append(timestamp_s)
            self._last_display_timestamp_s = timestamp_s
            for index, value in enumerate(frame.decoded_values):
                if index >= len(self._watch_values):
                    continue
                numeric = value if isinstance(value, (bool, int, float)) else float("nan")
                if isinstance(numeric, bool):
                    numeric = 1.0 if numeric else 0.0
                self._watch_values[index].append(float(numeric))

        if last_decoded_values is not None:
            self._latest_value_strings = [format_value(value) for value in last_decoded_values]
        self._trim_display_cache()
        self._update_capture_rate_label()

    def _should_keep_display_frame(self, timestamp_s: float, is_last_frame: bool) -> bool:
        if self._last_display_timestamp_s is None:
            return True
        if timestamp_s - self._last_display_timestamp_s >= self._display_sample_interval_s:
            return True
        # Keep the newest sample from each UI poll so the curve and value readout do not look stale.
        return is_last_frame and timestamp_s > self._last_display_timestamp_s

    def _trim_display_cache(self) -> None:
        if not self._watch_times:
            return

        cutoff = self._watch_times[-1] - DISPLAY_HISTORY_SECONDS
        drop_count = bisect_left(self._watch_times, cutoff)
        remaining = len(self._watch_times) - drop_count
        if remaining > DISPLAY_MAX_POINTS:
            drop_count += remaining - DISPLAY_MAX_POINTS
        if drop_count <= 0:
            return

        del self._watch_times[:drop_count]
        for values in self._watch_values:
            del values[:drop_count]

    def _rebuild_watch_table(self) -> None:
        self._updating_table = True
        try:
            self.watch_table.clearContents()
            self.watch_table.setRowCount(len(self.controller.resolved_watches))
            for row, watch in enumerate(self.controller.resolved_watches):
                plot_item = QTableWidgetItem()
                plot_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable)
                plot_item.setCheckState(Qt.Checked if watch.definition.plot_enabled else Qt.Unchecked)

                expr_item = QTableWidgetItem(f"■ {watch.definition.display_name}")
                expr_item.setForeground(QBrush(QColor(watch.definition.color)))
                type_item = QTableWidgetItem(watch.type_name)
                address_item = QTableWidgetItem(f"0x{watch.address:08X}")
                value_item = QTableWidgetItem(
                    self._latest_value_strings[row] if row < len(self._latest_value_strings) else "-"
                )
                cursor_item = QTableWidgetItem("-")

                self.watch_table.setItem(row, 0, plot_item)
                self.watch_table.setItem(row, 1, expr_item)
                self.watch_table.setItem(row, 2, type_item)
                self.watch_table.setItem(row, 3, address_item)
                self.watch_table.setItem(row, 4, value_item)
                self.watch_table.setItem(row, 5, cursor_item)
        finally:
            self._updating_table = False

    def _refresh_value_column(self) -> None:
        for row, value in enumerate(self._latest_value_strings):
            item = self.watch_table.item(row, 4)
            if item is not None and item.text() != value:
                item.setText(value)
        self._last_value_refresh_ns = time.monotonic_ns()

    def _refresh_point_column(self, cursor_index: int | None) -> None:
        for row, watch in enumerate(self.controller.resolved_watches):
            item = self.watch_table.item(row, 5)
            if item is None:
                continue
            if (
                cursor_index is None
                or not watch.definition.plot_enabled
                or row >= len(self._watch_values)
                or cursor_index >= len(self._watch_values[row])
            ):
                item.setText("-")
                continue
            value = self._watch_values[row][cursor_index]
            text = "-" if np.isnan(value) else f"{value:.6g}"
            if item.text() != text:
                item.setText(text)

    def _rebuild_plot_items(self) -> None:
        for main_item, overview_item in self._plot_items:
            self.main_plot.removeItem(main_item)
            self.overview_plot.removeItem(overview_item)
        self._plot_items.clear()

        for watch in self.controller.resolved_watches:
            pen = pg.mkPen(watch.definition.color, width=1.8)
            main_item = self.main_plot.plot([], [], pen=pen)
            overview_item = self.overview_plot.plot([], [], pen=pen)
            main_item.setClipToView(False)
            overview_item.setClipToView(False)
            main_item.setDownsampling(auto=False)
            overview_item.setDownsampling(auto=False)
            self._plot_items.append((main_item, overview_item))

        self.watch_table.clearSelection()
        self._reset_series()

    def _reset_series(self) -> None:
        self._base_timestamp_ns = 0
        self._watch_times = []
        self._watch_values = [[] for _ in self.controller.resolved_watches]
        self._latest_value_strings = ["-"] * len(self.controller.resolved_watches)
        self._capture_timestamps_ns.clear()
        self._plot_refresh_timestamps_ns.clear()
        self._plot_refresh_pending = False
        self._last_plot_refresh_ns = 0
        self._last_overview_refresh_ns = 0
        self._last_value_refresh_ns = 0
        self._last_display_timestamp_s = None
        self._manual_y_range = False
        self.capture_rate_label.setText('采集：-- Hz')
        self.plot_rate_label.setText('绘图：-- Hz')
        self.point_meta_label.setText("点值：--" if self._point_readout_enabled() else "点值：关闭")
        self._reset_plot_view()
        self._refresh_plots(force=True)
        self._refresh_value_column()
        self._refresh_point_column(None)

    def _refresh_plots(self, *, force: bool = False) -> None:
        if not self._plot_items:
            self._reset_plot_view()
            return
        if not self._watch_times:
            for main_item, overview_item in self._plot_items:
                main_item.setData([], [])
                overview_item.setData([], [])
            self._reset_plot_view()
            self._last_plot_refresh_ns = time.monotonic_ns()
            self._plot_refresh_pending = False
            return

        self._refreshing_plots = True
        try:
            main_span = self._current_main_plot_span()
            if main_span is None:
                main_left, main_right = self._watch_times[0], self._watch_times[-1]
            else:
                main_left, main_right = main_span
            main_start = max(0, bisect_left(self._watch_times, main_left))
            main_end = min(len(self._watch_times), bisect_right(self._watch_times, main_right))
            if main_end <= main_start:
                main_start = max(0, len(self._watch_times) - 1)
                main_end = len(self._watch_times)

            now_ns = time.monotonic_ns()
            refresh_overview = (
                force
                or self._follow_latest
                or now_ns - self._last_overview_refresh_ns >= OVERVIEW_REFRESH_INTERVAL_NS
            )
            self._update_overview_range()
            visible_x = np.asarray(self._watch_times[main_start:main_end], dtype=float)
            overview_x = np.asarray(self._watch_times, dtype=float) if refresh_overview else None
            enabled_plot_count = self._enabled_plot_count()
            main_point_budget = _main_plot_point_budget(enabled_plot_count)
            overview_point_budget = _overview_plot_point_budget(enabled_plot_count)

            for index, watch in enumerate(self.controller.resolved_watches):
                if index >= len(self._watch_values):
                    continue
                values = self._watch_values[index]
                if not watch.definition.plot_enabled or not values:
                    self._plot_items[index][0].setData([], [])
                    self._plot_items[index][1].setData([], [])
                    continue

                visible_y = np.asarray(values[main_start:main_end], dtype=float)
                main_x, main_y = _downsample(visible_x, visible_y, main_point_budget)
                self._plot_items[index][0].setData(main_x, main_y)

                if refresh_overview and overview_x is not None:
                    overview_y = np.asarray(values, dtype=float)
                    overview_plot_x, overview_plot_y = _downsample(overview_x, overview_y, overview_point_budget)
                    self._plot_items[index][1].setData(overview_plot_x, overview_plot_y)

            if not self._manual_y_range:
                self.main_view.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)
                self.main_view.setAutoVisible(y=True)
            self.overview_view.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)
            if self._follow_latest:
                self._apply_main_span((main_left, main_right), padding=0)
            self._last_plot_refresh_ns = now_ns
            self._plot_refresh_timestamps_ns.append(now_ns)
            self._update_plot_rate_label()
            self._refresh_cursor_readout()
            if refresh_overview:
                self._last_overview_refresh_ns = now_ns
            self._plot_refresh_pending = False
        finally:
            self._refreshing_plots = False

    def _current_main_plot_span(self) -> tuple[float, float] | None:
        if not self._watch_times:
            return None
        first = self._watch_times[0]
        latest = self._watch_times[-1]
        if self._follow_latest:
            return self._latest_follow_span()

        view_range = self.main_view.viewRange()
        x_range = _normalize_x_range(view_range)
        if x_range is None:
            return first, latest
        left = max(first, float(x_range[0]))
        right = min(latest, float(x_range[1]))
        if right <= left:
            return first, latest
        return left, right


    def _latest_follow_span(self) -> tuple[float, float] | None:
        return _latest_window_span(self._watch_times, MAIN_FOLLOW_WINDOW_SECONDS)

    def _apply_main_span(self, span: tuple[float, float], *, padding: float = 0.0) -> None:
        start, end = span
        self._syncing_region = True
        try:
            self._set_main_x_range(start, end, padding=padding)
            self._set_region_range(start, end)
        finally:
            self._syncing_region = False

    def _set_main_x_range(self, start: float, end: float, *, padding: float = 0.0) -> None:
        self.main_view.enableAutoRange(axis=pg.ViewBox.XAxis, enable=False)
        self.main_view.setRange(xRange=(start, end), padding=padding, disableAutoRange=True)

    def _set_region_range(self, start: float, end: float) -> None:
        self.region.setRegion((start, end))

    def _update_overview_range(self) -> tuple[float, float] | None:
        if not self._watch_times:
            return None
        start = self._watch_times[0]
        end = self._watch_times[-1]
        if end <= start:
            end = start + 1e-3
        self._syncing_region = True
        try:
            self.overview_view.enableAutoRange(axis=pg.ViewBox.XAxis, enable=False)
            self.overview_view.setRange(xRange=(start, end), padding=0, disableAutoRange=True)
            self.region.setBounds((start, end))
        finally:
            self._syncing_region = False
        return start, end

    def _reset_plot_view(self) -> None:
        self._manual_y_range = False
        self._syncing_region = True
        try:
            self.region.setBounds((0.0, 1.0))
            self._set_region_range(0.0, 1.0)
            self._set_main_x_range(0.0, 1.0, padding=0)
            self.overview_view.enableAutoRange(axis=pg.ViewBox.XAxis, enable=False)
            self.overview_view.setRange(xRange=(0.0, 1.0), padding=0, disableAutoRange=True)
        finally:
            self._syncing_region = False
        self.main_view.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)
        self.main_view.setAutoVisible(y=True)
        self.overview_view.enableAutoRange(axis=pg.ViewBox.YAxis, enable=True)

    def _selected_cfg_path(self) -> Path | None:
        text = self.cfg_path_edit.text().strip()
        if not text:
            return None
        return Path(text)

    def _on_watch_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating_table or item.column() != 0:
            return
        row = item.row()
        if row < 0 or row >= len(self.controller.watch_definitions):
            return
        enabled = item.checkState() == Qt.Checked
        self.controller.watch_definitions[row].plot_enabled = enabled
        self.controller.resolved_watches[row].definition.plot_enabled = enabled
        self._plot_refresh_pending = True
        view_range = self.main_view.viewRange()
        x_range = (float(view_range[0][0]), float(view_range[0][1]))
        y_range = (float(view_range[1][0]), float(view_range[1][1]))
        self._refresh_plots()
        self.main_view.enableAutoRange(axis=pg.ViewBox.XAxis, enable=False)
        self.main_view.enableAutoRange(axis=pg.ViewBox.YAxis, enable=False)
        self._syncing_region = True
        try:
            self.main_view.setRange(xRange=x_range, yRange=y_range, padding=0)
            self._set_region_range(x_range[0], x_range[1])
        finally:
            self._syncing_region = False

    def _on_main_view_range_changed(self, _, ranges) -> None:
        if self._syncing_region or self._refreshing_plots or not self._watch_times:
            return
        if not self._plot_range_change_is_user_driven():
            return
        if _normalize_x_range(ranges) is not None:
            self._manual_y_range = True
            self.main_view.enableAutoRange(axis=pg.ViewBox.YAxis, enable=False)

    def _sync_main_to_region(self) -> None:
        left, right = self.region.getRegion()
        if self._syncing_region or not self._watch_times:
            return
        if self._follow_latest and not self._plot_range_change_is_user_driven():
            return
        self._set_follow_latest(False)
        self._syncing_region = True
        try:
            self._set_main_x_range(left, right, padding=0)
        finally:
            self._syncing_region = False
        self._schedule_visible_plot_refresh()

    def _sync_region_to_main(self, _, view_range) -> None:
        x_range = _normalize_x_range(view_range)
        if x_range is None:
            return
        if self._syncing_region or not self._watch_times:
            return
        if self._follow_latest and not self._plot_range_change_is_user_driven():
            return
        self._set_follow_latest(False)
        self._syncing_region = True
        try:
            self._set_region_range(x_range[0], x_range[1])
        finally:
            self._syncing_region = False
        self._schedule_visible_plot_refresh()

    def _enabled_plot_count(self) -> int:
        return sum(1 for watch in self.controller.resolved_watches if watch.definition.plot_enabled)

    def _effective_plot_refresh_interval_ns(self) -> int:
        return _plot_refresh_interval_ns(self._enabled_plot_count())

    def _schedule_visible_plot_refresh(self) -> None:
        if not self._watch_times or not self._plot_items:
            return
        self._plot_refresh_pending = True
        if time.monotonic_ns() - self._last_plot_refresh_ns >= self._effective_plot_refresh_interval_ns():
            self._refresh_plots()

    def _update_capture_rate_label(self) -> None:
        rate = _recent_rate_hz(self._capture_timestamps_ns)
        if rate is None:
            self.capture_rate_label.setText('采集：-- Hz')
            return
        self.capture_rate_label.setText(f"采集：{rate:.1f} Hz")

    def _update_plot_rate_label(self) -> None:
        rate = _recent_rate_hz(self._plot_refresh_timestamps_ns)
        if rate is None:
            self.plot_rate_label.setText('绘图：-- Hz')
            return
        self.plot_rate_label.setText(f"绘图：{rate:.1f} Hz")

    def _checked_watch_rows(self) -> list[int]:
        rows: list[int] = []
        for row in range(self.watch_table.rowCount()):
            item = self.watch_table.item(row, 0)
            if item is not None and item.checkState() == Qt.Checked:
                rows.append(row)
        return rows

    def _restart_capture_after_watch_change(self) -> None:
        if not self.controller.is_capturing():
            return
        if not self.controller.resolved_watches:
            self.controller.stop_capture()
            self.status_label.setText("采集已停止，因为当前变量已清空。")
            return
        try:
            path = self.controller.start_capture()
        except Exception as exc:
            _show_warning(self, "变量更新失败", f"变量列表已变化，但重新开始采集失败：\n{exc}")
            return
        self._set_follow_latest(True)
        self._reset_series()
        self.status_label.setText(f"变量已更新，重新开始采集：{path.name}")


def _plot_refresh_interval_ns(series_count: int) -> int:
    if series_count >= 16:
        return 100_000_000
    if series_count >= 8:
        return 66_666_667
    return PLOT_REFRESH_INTERVAL_NS


def _main_plot_point_budget(series_count: int) -> int:
    if series_count >= 16:
        return 450
    if series_count >= 8:
        return 650
    return MAIN_PLOT_MAX_POINTS


def _overview_plot_point_budget(series_count: int) -> int:
    if series_count >= 16:
        return 220
    if series_count >= 8:
        return 300
    return OVERVIEW_PLOT_MAX_POINTS


def _recent_rate_hz(timestamps_ns: deque[int], *, window_s: float = 1.0) -> float | None:
    while len(timestamps_ns) >= 2:
        span_s = (timestamps_ns[-1] - timestamps_ns[0]) / 1_000_000_000.0
        if span_s <= window_s:
            break
        timestamps_ns.popleft()

    if len(timestamps_ns) < 2:
        return None

    span_s = (timestamps_ns[-1] - timestamps_ns[0]) / 1_000_000_000.0
    if span_s <= 0:
        return None
    return (len(timestamps_ns) - 1) / span_s


def _next_color(used_colors: Iterable[str]) -> str:
    used = {_normalize_color(color) for color in used_colors}
    for color in WATCH_COLOR_PALETTE:
        if _normalize_color(color) not in used:
            return color

    index = 0
    while True:
        color = _generated_watch_color(index)
        if _normalize_color(color) not in used:
            return color
        index += 1


def _normalize_color(color: str) -> str:
    parsed = QColor(color)
    if parsed.isValid():
        return parsed.name().upper()
    return color.strip().upper()


def _generated_watch_color(index: int) -> str:
    hue = (0.08 + index * 0.618033988749895) % 1.0
    lightness = (0.58, 0.68, 0.48, 0.74, 0.42)[index % 5]
    saturation = (0.78, 0.66, 0.88, 0.58)[index % 4]
    red, green, blue = colorsys.hls_to_rgb(hue, lightness, saturation)
    return f"#{round(red * 255):02X}{round(green * 255):02X}{round(blue * 255):02X}"


def _downsample(x: np.ndarray, y: np.ndarray, max_points: int) -> tuple[np.ndarray, np.ndarray]:
    if x.size <= max_points or max_points <= 0:
        return x, y

    # Use min/max bucket sampling so periodic signals do not collapse into flat lines.
    bucket_count = max(1, max_points // 2)
    edges = np.linspace(0, x.size, bucket_count + 1, dtype=np.int64)
    sampled_x = np.empty(bucket_count * 2, dtype=float)
    sampled_y = np.empty(bucket_count * 2, dtype=float)
    out = 0

    for i in range(bucket_count):
        start = int(edges[i])
        end = int(edges[i + 1])
        if end <= start:
            continue

        bucket_x = x[start:end]
        bucket_y = y[start:end]
        finite_mask = np.isfinite(bucket_y)
        if not np.any(finite_mask):
            sampled_x[out] = float(bucket_x[-1])
            sampled_y[out] = float("nan")
            out += 1
            continue

        finite_indices = np.flatnonzero(finite_mask)
        finite_values = bucket_y[finite_mask]
        min_idx = int(finite_indices[int(np.argmin(finite_values))])
        max_idx = int(finite_indices[int(np.argmax(finite_values))])

        first, second = (min_idx, max_idx) if min_idx <= max_idx else (max_idx, min_idx)
        sampled_x[out] = float(bucket_x[first])
        sampled_y[out] = float(bucket_y[first])
        out += 1

        if second != first and out < sampled_x.size:
            sampled_x[out] = float(bucket_x[second])
            sampled_y[out] = float(bucket_y[second])
            out += 1

    if out == 0:
        return np.asarray([x[-1]], dtype=float), np.asarray([float("nan")], dtype=float)

    sampled_x = sampled_x[:out]
    sampled_y = sampled_y[:out]
    order = np.argsort(sampled_x, kind="stable")
    return sampled_x[order], sampled_y[order]


def _latest_window_span(times: list[float], window_seconds: float) -> tuple[float, float] | None:
    if not times:
        return None
    first = times[0]
    latest = times[-1]
    window = window_seconds if math.isfinite(window_seconds) and window_seconds > 0 else 1e-3
    left = max(first, latest - window)
    right = max(latest, left + 1e-3)
    return left, right


def _normalize_x_range(view_range) -> tuple[float, float] | None:
    if isinstance(view_range, (tuple, list)):
        if len(view_range) == 2 and all(isinstance(item, (int, float)) for item in view_range):
            return float(view_range[0]), float(view_range[1])
        if len(view_range) > 0 and isinstance(view_range[0], (tuple, list)) and len(view_range[0]) == 2:
            nested = view_range[0]
            if all(isinstance(item, (int, float)) for item in nested):
                return float(nested[0]), float(nested[1])
    return None


def _nearest_index(times: list[float], target: float) -> int | None:
    if not times:
        return None
    index = bisect_left(times, target)
    if index <= 0:
        return 0
    if index >= len(times):
        return len(times) - 1
    left = times[index - 1]
    right = times[index]
    return index - 1 if abs(target - left) <= abs(right - target) else index
