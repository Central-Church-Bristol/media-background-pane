from __future__ import annotations

from pathlib import Path
from time import monotonic

from PySide6.QtCore import QEvent, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractButton,
    QButtonGroup,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QStyle,
    QStyleOptionSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .bus import is_media
from .config import SCALE_FILL, SCALE_MODE_LABELS

PP_BLUE = QColor("#0a84ff")
PP_TRACK_OFF = QColor("#48484a")
PP_KNOB = QColor("#ffffff")

class FlowLayout(QLayout):
    def __init__(self, parent: QWidget | None = None, spacing: int = 6) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self.setContentsMargins(4, 4, 4, 4)
        self.setSpacing(spacing)

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)
        self.invalidate()

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index: int) -> QLayoutItem | None:
        if 0 <= index < len(self._items):
            self.invalidate()
            return self._items.pop(index)
        return None

    def set_widgets(self, widgets: list[QWidget]) -> None:
        while self.count():
            self.takeAt(0)
        for widget in widgets:
            self.addWidget(widget)
        self.invalidate()

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        margins = self.contentsMargins()
        effective = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x = effective.x()
        y = effective.y()
        line_height = 0
        space = self.spacing()
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + space
            if next_x - space > effective.right() + 1 and line_height > 0:
                x = effective.x()
                y = y + line_height + space
                next_x = x + hint.width() + space
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + margins.bottom()


COLLAPSED_BAR_WIDTH = 26
SIDE_TAB_SETTINGS_SPACE = 30


class CollapseBar(QPushButton):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("CollapseBar")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedWidth(20)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.setToolTip("Collapse to a side bar")
        self._collapsed = False
        self.setText("‹")

    def set_collapsed(self, collapsed: bool) -> None:
        self._collapsed = collapsed
        self.setChecked(collapsed)
        self.setFixedWidth(COLLAPSED_BAR_WIDTH if collapsed else 20)
        self.setText("" if collapsed else "‹")
        self.setToolTip("Expand" if collapsed else "Collapse to a side bar")
        self.update()

    def paintEvent(self, event) -> None:
        super().paintEvent(event)
        if not self._collapsed:
            return
        color = QColor("#ff9f0a" if self.underMouse() else "#ebebf0")
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(color)
        font = self.font()
        font.setPointSize(13)
        font.setWeight(QFont.Weight.Bold)
        painter.setFont(font)
        arrow_h = 22
        painter.drawText(QRect(0, 2, self.width(), arrow_h), Qt.AlignmentFlag.AlignCenter, "›")
        font.setPointSize(9)
        font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(font)
        # Leave room at the bottom for the settings cog on the side tab.
        label_h = max(1, self.height() - arrow_h)
        painter.translate(self.width() / 2, arrow_h + label_h / 2)
        painter.rotate(-90)
        text_rect = QRect(-label_h // 2, -self.width() // 2, label_h, self.width())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, "Media Backgrounds")
        painter.end()

    def enterEvent(self, event) -> None:
        super().enterEvent(event)
        self.update()

    def leaveEvent(self, event) -> None:
        super().leaveEvent(event)
        self.update()


class TopRightGrip(QWidget):
    resized = Signal()
    pressed = Signal()
    dragged = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("TopRightGrip")
        self.setFixedSize(16, 16)
        self.setCursor(Qt.CursorShape.SizeBDiagCursor)
        self.setToolTip("Resize")
        self._origin: QPoint | None = None

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setPen(QColor("#636366"))
        for i in range(3):
            painter.drawLine(15 - i * 4, 1, 15, 1 + i * 4)
        painter.end()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._origin = event.globalPosition().toPoint()
            self.pressed.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._origin is None:
            return
        self.dragged.emit()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        self._origin = None
        self.resized.emit()
        super().mouseReleaseEvent(event)


class MonitorWidget(QFrame):
    def __init__(self, title: str, accent: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Monitor")
        self.setProperty("accent", accent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._image = QImage()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(3)
        self._title = QLabel(title)
        self._title.setObjectName("MonitorTitle")
        self._title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title.setProperty("accent", accent)
        self._view = QLabel()
        self._view.setObjectName("MonitorView")
        self._view.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._view.setMinimumSize(72, 40)
        self._view.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self._title)
        layout.addWidget(self._view, 1)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return int(width * 9 / 16) + 22

    def set_image(self, image: QImage) -> None:
        self._image = image
        self._paint()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._paint()

    def _paint(self) -> None:
        size = self._view.size()
        if size.width() < 2 or size.height() < 2:
            return
        canvas = QPixmap(size)
        canvas.fill(QColor("#000000"))
        painter = QPainter(canvas)
        if not self._image.isNull():
            scaled = QPixmap.fromImage(self._image).scaled(
                size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
            x = (size.width() - scaled.width()) // 2
            y = (size.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
        painter.end()
        self._view.setPixmap(canvas)


class ThumbnailButton(QToolButton):
    chosen = Signal(str)

    def __init__(self, path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.path = path
        self.setObjectName("Thumb")
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.setToolTip(path.stem)
        self._pixmap = QPixmap(128, 72)
        self._pixmap.fill(QColor("#2c2c2e"))
        self.set_thumb_size(112)
        self.clicked.connect(lambda: self.chosen.emit(str(self.path)))

    def set_image(self, image: QImage) -> None:
        if image.isNull():
            return
        self._pixmap = QPixmap.fromImage(image)
        self._refresh_icon()

    def set_thumb_size(self, width: int) -> None:
        height = max(40, round(width * 9 / 16))
        self.setIconSize(QSize(width, height))
        self.setFixedSize(width + 8, height + 8)
        self._refresh_icon()

    def _refresh_icon(self) -> None:
        size = self.iconSize()
        if size.width() < 2:
            return
        canvas = QPixmap(size)
        canvas.fill(QColor("#252528"))
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        if not self._pixmap.isNull():
            scaled = self._pixmap.scaled(
                size,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation,
            )
            x = (size.width() - scaled.width()) // 2
            y = (size.height() - scaled.height()) // 2
            painter.drawPixmap(x, y, scaled)
        painter.end()
        self.setIcon(QIcon(canvas))


class ThumbnailStrip(QWidget):
    chosen = Signal(str)
    scale_changed = Signal(int)
    scale_mode_changed = Signal(str)
    files_dropped = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("ThumbPanel")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setAcceptDrops(True)
        self._buttons: dict[str, ThumbnailButton] = {}
        self._file_keys: list[str] = []
        self._thumb_width = 112
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)

        self._scroll = QScrollArea()
        self._scroll.setObjectName("ThumbStrip")
        self._scroll.setWidgetResizable(False)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._scroll.setMinimumHeight(0)

        self._inner = QWidget()
        self._layout = FlowLayout(self._inner, spacing=8)
        self._placeholder = QLabel("Set a media folder in Settings, or drop files here")
        self._placeholder.setObjectName("Hint")
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setWordWrap(True)
        self._layout.addWidget(self._placeholder)
        self._scroll.setWidget(self._inner)
        self._inner.setAutoFillBackground(False)
        self._inner.setAcceptDrops(True)
        self._scroll.setAcceptDrops(True)
        self._scroll.viewport().setAcceptDrops(True)
        self._scroll.viewport().installEventFilter(self)
        self._inner.installEventFilter(self)

        self._scale = QSlider(Qt.Orientation.Horizontal)
        self._scale.setObjectName("ThumbScale")
        self._scale.setRange(64, 200)
        self._scale.setValue(112)
        self._scale.setFixedHeight(16)
        self._scale.setMaximumWidth(120)
        self._scale.valueChanged.connect(self._on_scale)
        scale_label = QLabel("SIZE")
        scale_label.setObjectName("SliderLabel")
        self._scale_mode = QComboBox()
        self._scale_mode.setObjectName("ScaleMode")
        self._scale_mode.setCursor(Qt.CursorShape.PointingHandCursor)
        self._scale_mode.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._scale_mode.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        for value, label in SCALE_MODE_LABELS:
            self._scale_mode.addItem(label, value)
        index = self._scale_mode.findData(SCALE_FILL)
        if index >= 0:
            self._scale_mode.setCurrentIndex(index)
        self._scale_mode.currentIndexChanged.connect(self._on_scale_mode)
        mode_label = QLabel("SCALE")
        mode_label.setObjectName("SliderLabel")
        self._scale_row = QHBoxLayout()
        self._scale_row.setContentsMargins(2, 0, 4, 2)
        self._scale_row.setSpacing(6)
        self._scale_row.addWidget(scale_label)
        self._scale_row.addWidget(self._scale, 0)
        self._scale_row.addWidget(mode_label)
        self._scale_row.addWidget(self._scale_mode, 0)
        self._scale_row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self._scroll, 1)
        layout.addLayout(self._scale_row)

    def set_footer_leading(self, widget: QWidget) -> None:
        self._scale_row.insertWidget(0, widget)

    def wheelEvent(self, event) -> None:
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.value() - int(event.angleDelta().y()))
        event.accept()

    def set_scale(self, width: int) -> None:
        self._scale.blockSignals(True)
        self._scale.setValue(width)
        self._scale.blockSignals(False)
        self._apply_scale(width)

    def _on_scale(self, width: int) -> None:
        self._apply_scale(width)
        self.scale_changed.emit(width)

    def set_scale_mode(self, mode: str) -> None:
        index = self._scale_mode.findData(mode)
        if index < 0:
            index = self._scale_mode.findData(SCALE_FILL)
        if index < 0:
            return
        self._scale_mode.blockSignals(True)
        self._scale_mode.setCurrentIndex(index)
        self._scale_mode.blockSignals(False)

    def _on_scale_mode(self, _index: int) -> None:
        mode = self._scale_mode.currentData()
        if mode:
            self.scale_mode_changed.emit(str(mode))

    def _apply_scale(self, width: int) -> None:
        self._thumb_width = width
        for button in self._buttons.values():
            button.set_thumb_size(width)
        self._inner.updateGeometry()
        self._fit_flow()

    def set_files(self, paths: list[Path]) -> None:
        keys = [str(path) for path in paths]
        wanted = set(keys)
        if keys == self._file_keys and wanted == set(self._buttons):
            return
        self._inner.setUpdatesEnabled(False)
        try:
            for key, button in list(self._buttons.items()):
                if key not in wanted:
                    self._group.removeButton(button)
                    self._layout.removeWidget(button)
                    button.deleteLater()
                    del self._buttons[key]
            new_buttons: list[ThumbnailButton] = []
            existing = set(self._buttons)
            for path in paths:
                key = str(path)
                if key in existing:
                    continue
                button = ThumbnailButton(path, self._inner)
                button.setVisible(False)
                button.move(-10000, -10000)
                button.set_thumb_size(self._thumb_width)
                button.chosen.connect(self.chosen.emit)
                self._group.addButton(button)
                self._buttons[key] = button
                new_buttons.append(button)
            ordered: list[QWidget] = []
            if not paths:
                self._placeholder.setVisible(True)
                ordered.append(self._placeholder)
            else:
                self._placeholder.setVisible(False)
            for path in paths:
                button = self._buttons.get(str(path))
                if button is not None:
                    ordered.append(button)
            self._layout.set_widgets(ordered)
            self._file_keys = keys
            self._fit_flow()
            for button in new_buttons:
                button.setVisible(True)
        finally:
            self._inner.setUpdatesEnabled(True)
            self._inner.update()

    def set_empty_hint(self, text: str) -> None:
        self._placeholder.setText(text)

    def set_thumb(self, path: str, image: QImage) -> None:
        button = self._buttons.get(path)
        if button is not None:
            button.set_image(image)

    def set_selected(self, path: str | None) -> None:
        for key, button in self._buttons.items():
            button.setChecked(key == path)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self._fit_flow)

    def eventFilter(self, watched, event) -> bool:
        if event.type() in (
            QEvent.Type.DragEnter,
            QEvent.Type.DragMove,
            QEvent.Type.DragLeave,
            QEvent.Type.Drop,
        ) and watched in (self._scroll.viewport(), self._inner):
            if event.type() == QEvent.Type.DragEnter:
                self.dragEnterEvent(event)
            elif event.type() == QEvent.Type.DragMove:
                self.dragMoveEvent(event)
            elif event.type() == QEvent.Type.DragLeave:
                self.dragLeaveEvent(event)
            else:
                self.dropEvent(event)
            return event.isAccepted()
        if watched is self._scroll.viewport() and event.type() == QEvent.Type.Resize:
            self._fit_flow()
        return super().eventFilter(watched, event)

    def dragEnterEvent(self, event) -> None:
        if self._drop_paths(event):
            event.acceptProposedAction()
            self._set_drop_ready(True)
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if self._drop_paths(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self._set_drop_ready(False)
        event.accept()

    def dropEvent(self, event) -> None:
        self._set_drop_ready(False)
        paths = self._drop_paths(event)
        if not paths:
            event.ignore()
            return
        event.acceptProposedAction()
        self.files_dropped.emit([str(path) for path in paths])

    def _drop_paths(self, event) -> list[Path]:
        mime = event.mimeData()
        if mime is None or not mime.hasUrls():
            return []
        paths: list[Path] = []
        for url in mime.urls():
            if not url.isLocalFile():
                continue
            path = Path(url.toLocalFile())
            if path.is_file() and is_media(path):
                paths.append(path)
            elif path.is_dir():
                paths.append(path)
        return paths

    def _set_drop_ready(self, on: bool) -> None:
        if bool(self.property("dropReady")) == on:
            return
        self.setProperty("dropReady", on)
        self.style().unpolish(self)
        self.style().polish(self)
        self._scroll.style().unpolish(self._scroll)
        self._scroll.style().polish(self._scroll)
        self.update()

    def _fit_flow(self) -> None:
        width = max(1, self._scroll.viewport().width())
        if width < 8:
            return
        height = max(1, self._layout.heightForWidth(width))
        self._inner.setMinimumHeight(0)
        self._inner.resize(width, height)
        self._layout.activate()
        self._layout.setGeometry(QRect(0, 0, width, height))
        self._inner.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit_flow()

    def set_program(self, path: str | None) -> None:
        for key, button in self._buttons.items():
            on = bool(path) and key == path
            if button.property("program") == on:
                continue
            button.setProperty("program", on)
            button.style().unpolish(button)
            button.style().polish(button)
            button.update()


class ToggleSwitch(QAbstractButton):
    """ProPresenter-style iOS toggle."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(40, 22)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def sizeHint(self) -> QSize:
        return QSize(40, 22)

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        track = QRect(0, 2, self.width(), self.height() - 4)
        painter.setBrush(PP_BLUE if self.isChecked() else PP_TRACK_OFF)
        painter.drawRoundedRect(track, track.height() / 2, track.height() / 2)
        knob = 16
        margin = 3
        x = self.width() - knob - margin if self.isChecked() else margin
        y = (self.height() - knob) // 2
        painter.setBrush(PP_KNOB)
        painter.drawEllipse(x, y, knob, knob)
        painter.end()


class LabeledSwitch(QWidget):
    def __init__(self, label: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("LabeledSwitch")
        self._switch = ToggleSwitch(self)
        caption = QLabel(label)
        caption.setObjectName("SliderLabel")
        caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(caption)
        layout.addWidget(self._switch, 0, Qt.AlignmentFlag.AlignHCenter)

    def isChecked(self) -> bool:
        return self._switch.isChecked()

    def setChecked(self, checked: bool) -> None:
        self._switch.setChecked(checked)

    def setToolTip(self, tip: str) -> None:
        super().setToolTip(tip)
        self._switch.setToolTip(tip)


class ChaseSlider(QSlider):
    """Slider whose handle chases a ghost target at fade-duration-limited speed."""

    chase_finished = Signal()

    def __init__(self, orientation: Qt.Orientation, parent: QWidget | None = None) -> None:
        super().__init__(orientation, parent)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._ghost = 0
        self._pos = 0.0
        self._vel = 0.0
        self._fade_seconds = 10.0
        self._dragging = False
        self._chasing = False
        self._show_ghost = False
        self._last_tick = 0.0
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(8)
        self._timer.timeout.connect(self._tick)
        self._pos = float(self.value())
        self._ghost = self.value()

    def set_fade_seconds(self, seconds: float) -> None:
        self._fade_seconds = max(0.05, float(seconds))

    def is_chasing(self) -> bool:
        return self._chasing

    def is_dragging(self) -> bool:
        return self._dragging

    def ghost_value(self) -> int:
        return int(self._ghost)

    def visual_value(self) -> float:
        return float(self._pos)

    def setValue(self, value: int) -> None:
        super().setValue(value)
        if not self._chasing:
            self._pos = float(self.value())
            if not self._dragging:
                self._ghost = self.value()

    def snap_to(self, value: int) -> None:
        self._timer.stop()
        self._chasing = False
        self._vel = 0.0
        value = max(self.minimum(), min(self.maximum(), int(value)))
        self._ghost = value
        self._pos = float(value)
        self._show_ghost = False
        if self.value() != value:
            super().setValue(value)
        self.update()

    def stop_chase(self) -> None:
        if not self._chasing and not self._show_ghost:
            self._vel = 0.0
            return
        self._timer.stop()
        self._chasing = False
        self._vel = 0.0
        self._ghost = int(round(self._pos))
        self._show_ghost = False
        snapped = max(self.minimum(), min(self.maximum(), self._ghost))
        self._pos = float(snapped)
        if self.value() != snapped:
            super().setValue(snapped)
        self.update()

    def set_target(self, value: int) -> None:
        value = max(self.minimum(), min(self.maximum(), int(value)))
        self._ghost = value
        if abs(self._pos - value) < 0.5 and abs(self._vel) < 0.05:
            self._pos = float(value)
            if self.value() != value:
                super().setValue(value)
            if not self._dragging:
                self._show_ghost = False
                if self._chasing:
                    self._finish_chase(emit_finished=True)
            self.update()
            return
        self._show_ghost = True
        self._ensure_chase()
        self.update()

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            event.ignore()
            return
        self._dragging = True
        self.sliderPressed.emit()
        self._ghost = self._value_from_pos(event.position())
        self._show_ghost = True
        self._ensure_chase()
        self.grabMouse()
        self.repaint()
        event.accept()

    def mouseMoveEvent(self, event) -> None:
        if not self._dragging:
            event.ignore()
            return
        self._ghost = self._value_from_pos(event.position())
        self._show_ghost = True
        self._ensure_chase()
        self.repaint()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or not self._dragging:
            event.ignore()
            return
        self._dragging = False
        self.set_target(self._value_from_pos(event.position()))
        if QWidget.mouseGrabber() is self:
            self.releaseMouse()
        self.sliderReleased.emit()
        event.accept()

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        if delta == 0:
            event.ignore()
            return
        step = 40
        origin = self._ghost if (self._chasing or self._dragging) else self.value()
        self.set_target(origin + (step if delta > 0 else -step))
        event.accept()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        groove = self._groove_rect()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#3a3a3c"))
        painter.drawRoundedRect(groove, 2, 2)
        live = self._handle_center(self._pos)
        painter.setBrush(QColor("#636366"))
        if self.orientation() == Qt.Orientation.Horizontal:
            fill = QRect(groove.x(), groove.y(), max(0, int(round(live.x() - groove.x()))), groove.height())
        else:
            fill = QRect(
                groove.x(),
                int(round(live.y())),
                groove.width(),
                max(0, groove.bottom() - int(round(live.y())) + 1),
            )
        painter.drawRoundedRect(fill, 2, 2)
        if self._show_ghost and abs(self._ghost - self._pos) > 0.6:
            self._draw_knob(painter, self._handle_center(float(self._ghost)), QColor("#8e8e93"))
        self._draw_knob(painter, live, QColor("#ebebf0"))
        painter.end()

    def _groove_rect(self) -> QRect:
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        return self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider,
            opt,
            QStyle.SubControl.SC_SliderGroove,
            self,
        )

    def _handle_center(self, value: float) -> QPointF:
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider,
            opt,
            QStyle.SubControl.SC_SliderGroove,
            self,
        )
        radius = 7.0
        mn = float(self.minimum())
        mx = float(self.maximum())
        t = 0.0 if mx <= mn else (value - mn) / (mx - mn)
        t = max(0.0, min(1.0, t))
        if opt.upsideDown:
            t = 1.0 - t
        if self.orientation() == Qt.Orientation.Horizontal:
            x = groove.x() + radius + t * max(0.0, groove.width() - 2 * radius)
            return QPointF(x, groove.center().y())
        y = groove.y() + radius + t * max(0.0, groove.height() - 2 * radius)
        return QPointF(groove.center().x(), y)

    def _draw_knob(self, painter: QPainter, center: QPointF, color: QColor) -> None:
        painter.setBrush(color)
        painter.setPen(QPen(QColor("#636366"), 1))
        painter.drawEllipse(QRectF(center.x() - 7, center.y() - 7, 14, 14))

    def _value_from_pos(self, pos) -> int:
        point = pos.toPoint() if hasattr(pos, "toPoint") else QPoint(int(pos.x()), int(pos.y()))
        opt = QStyleOptionSlider()
        self.initStyleOption(opt)
        groove = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider,
            opt,
            QStyle.SubControl.SC_SliderGroove,
            self,
        )
        handle = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider,
            opt,
            QStyle.SubControl.SC_SliderHandle,
            self,
        )
        if self.orientation() == Qt.Orientation.Horizontal:
            slider_min = groove.x()
            slider_max = groove.right() - handle.width() + 1
            pos_val = point.x() - handle.width() // 2
        else:
            slider_min = groove.y()
            slider_max = groove.bottom() - handle.height() + 1
            pos_val = point.y() - handle.height() // 2
        span = max(1, int(slider_max - slider_min))
        return QStyle.sliderValueFromPosition(
            self.minimum(),
            self.maximum(),
            int(pos_val - slider_min),
            span,
            opt.upsideDown,
        )

    def _ensure_chase(self) -> None:
        if self._chasing:
            return
        self._chasing = True
        self._last_tick = monotonic()
        self._timer.start()

    def _finish_chase(self, emit_finished: bool = True) -> None:
        self._timer.stop()
        self._chasing = False
        self._vel = 0.0
        snapped = max(self.minimum(), min(self.maximum(), int(round(self._ghost))))
        self._pos = float(snapped)
        self._ghost = snapped
        if not self._dragging:
            self._show_ghost = False
        if self.value() != snapped:
            super().setValue(snapped)
        self.update()
        if emit_finished and not self._dragging:
            self.chase_finished.emit()

    def _tick(self) -> None:
        now = monotonic()
        dt = min(0.05, max(0.001, now - self._last_tick))
        self._last_tick = now
        fade = max(0.05, self._fade_seconds)
        ease_time = max(0.06, 0.10 * fade)
        max_v = 1000.0 / max(fade - ease_time, 0.05)
        accel = max_v / ease_time
        remaining = self._ghost - self._pos
        dist = abs(remaining)
        if dist < 0.4 and abs(self._vel) < max_v * 0.02:
            self._finish_chase()
            return
        direction = 1.0 if remaining > 0 else -1.0
        stop_dist = (self._vel * self._vel) / (2.0 * accel) if accel > 0 else 0.0
        if self._vel * remaining < 0:
            dv = accel * dt
            if abs(self._vel) <= dv:
                self._vel = 0.0
            else:
                self._vel -= dv if self._vel > 0 else -dv
        elif dist <= stop_dist:
            dv = accel * dt
            if abs(self._vel) <= dv:
                self._vel = 0.0
            else:
                self._vel -= dv if self._vel > 0 else -dv
        else:
            self._vel += direction * accel * dt
            self._vel = max(-max_v, min(max_v, self._vel))
        self._pos += self._vel * dt
        if (direction > 0 and self._pos >= self._ghost) or (direction < 0 and self._pos <= self._ghost):
            self._finish_chase()
            return
        self._pos = max(float(self.minimum()), min(float(self.maximum()), self._pos))
        new_val = int(round(self._pos))
        if new_val != self.value():
            blocked = self.blockSignals(True)
            super().setValue(new_val)
            self.blockSignals(blocked)
            self.valueChanged.emit(new_val)
        self.update()


class LabeledSlider(QWidget):
    value_changed = Signal(int)
    pressed = Signal()
    released = Signal()
    chase_finished = Signal()

    def __init__(
        self,
        label: str,
        orientation: Qt.Orientation = Qt.Orientation.Vertical,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        caption = QLabel(label)
        caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        caption.setObjectName("SliderLabel")
        self.slider = ChaseSlider(orientation)
        self.slider.setRange(0, 1000)
        self.slider.setValue(0)
        self.slider.setTickPosition(QSlider.TickPosition.NoTicks)
        self.slider.valueChanged.connect(self.value_changed.emit)
        self.slider.sliderPressed.connect(self.pressed.emit)
        self.slider.sliderReleased.connect(self.released.emit)
        self.slider.chase_finished.connect(self.chase_finished.emit)
        if orientation == Qt.Orientation.Horizontal:
            self.setObjectName("MixSlider")
            layout = QHBoxLayout(self)
            layout.setContentsMargins(0, 2, 0, 2)
            layout.setSpacing(6)
            layout.addWidget(caption)
            layout.addWidget(self.slider, 1)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            self.setFixedHeight(28)
        else:
            self.setObjectName("DimSlider")
            layout = QVBoxLayout(self)
            layout.setContentsMargins(0, 8, 0, 10)
            layout.setSpacing(4)
            layout.addWidget(caption)
            layout.addWidget(self.slider, 1)
            self.setFixedWidth(36)

    def set_fade_seconds(self, seconds: float) -> None:
        self.slider.set_fade_seconds(seconds)

    def set_target(self, value: int) -> None:
        self.slider.set_target(value)

    def snap_to(self, value: int) -> None:
        self.slider.snap_to(value)

    def ghost(self) -> int:
        return self.slider.ghost_value()

    def visual(self) -> float:
        return self.slider.visual_value()
