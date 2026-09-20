from __future__ import annotations

from pathlib import Path
from queue import Empty
from time import monotonic

from PySide6.QtCore import (
    QBuffer,
    QFileSystemWatcher,
    QIODevice,
    QPoint,
    QPointF,
    QRect,
    QSize,
    Qt,
    QTimer,
)
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QImage, QPainter, QPixmap, QPolygonF
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLayout,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSystemTrayIcon,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .config import Config
from .engine import Engine
from .lan_server import LanHub, LanServer, lan_urls
from .library import Thumbnailer, image_thumbnail, media_files, thumb_cache_path
from .priority import prefer_livestream_apps
from .propresenter import (
    PATH_SETTINGS,
    clear_video_input_layer,
    is_propresenter_running,
    native_cursor_pos,
    native_window_rect,
    propresenter_window_rect,
    set_visible_window_rect,
    trigger_first_video_input,
    video_input_layer_active,
    workspace_is_open,
)
from .settings_dialog import SettingsDialog
from .startup import set_enabled as set_startup_enabled
from .widgets import (
    COLLAPSED_BAR_WIDTH,
    CollapseBar,
    LabeledSlider,
    LabeledSwitch,
    MonitorWidget,
    ThumbnailStrip,
    TopRightGrip,
)


def make_app_icon() -> QIcon:
    pix = QPixmap(64, 64)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(Qt.GlobalColor.black)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawRoundedRect(4, 10, 28, 20, 3, 3)
    painter.setBrush(Qt.GlobalColor.darkGreen)
    painter.drawRoundedRect(8, 14, 20, 12, 2, 2)
    painter.setBrush(Qt.GlobalColor.black)
    painter.drawRoundedRect(32, 10, 28, 20, 3, 3)
    painter.setBrush(Qt.GlobalColor.red)
    painter.drawRoundedRect(36, 14, 20, 12, 2, 2)
    painter.end()
    return QIcon(pix)


def make_settings_icon_pixmap(color: str) -> QPixmap:
    pix = QPixmap(36, 36)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.translate(18, 18)
    pen = painter.pen()
    pen.setColor(QColor(color))
    pen.setWidthF(2.2)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(QPoint(0, 0), 5, 5)
    for i in range(8):
        painter.save()
        painter.rotate(i * 45)
        painter.drawRoundedRect(-1.4, -11.5, 2.8, 4.2, 1.0, 1.0)
        painter.restore()
    painter.end()
    return pix


def make_settings_icon() -> QIcon:
    icon = QIcon()
    icon.addPixmap(make_settings_icon_pixmap("#6e6e73"), QIcon.Mode.Normal)
    hover = make_settings_icon_pixmap("#98989f")
    icon.addPixmap(hover, QIcon.Mode.Active)
    icon.addPixmap(hover, QIcon.Mode.Selected)
    return icon


def make_video_input_icon_pixmap(color: str) -> QPixmap:
    # Camcorder: rounded body, then a smaller lens wedge with a clear gap.
    # Extra width on the right leaves space before the button label.
    pix = QPixmap(84, 64)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    painter.drawRoundedRect(4, 16, 30, 32, 6, 6)
    painter.drawPolygon(
        QPolygonF(
            [
                QPointF(42, 24.8),
                QPointF(59, 19.2),
                QPointF(59, 44.8),
                QPointF(42, 39.2),
            ]
        )
    )
    painter.end()
    return pix


def make_video_input_icon(color: str = "#ebebf0") -> QIcon:
    icon = QIcon()
    icon.addPixmap(make_video_input_icon_pixmap(color), QIcon.Mode.Normal)
    icon.addPixmap(make_video_input_icon_pixmap(color), QIcon.Mode.Active)
    icon.addPixmap(make_video_input_icon_pixmap("#64d2ff"), QIcon.Mode.Selected)
    return icon


def _qimage_jpeg(image: QImage, quality: int = 70) -> bytes:
    frame = image
    if frame is None or frame.isNull():
        frame = QImage(8, 8, QImage.Format.Format_RGB32)
        frame.fill(QColor(0, 0, 0))
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    frame.save(buf, "JPG", quality)
    return bytes(buf.data())


class MainWindow(QWidget):
    def __init__(self, config: Config, startup_mode: bool = False) -> None:
        super().__init__()
        self.config = config
        self.startup_mode = startup_mode
        self.engine = Engine(config, self)
        self.thumbnailer = Thumbnailer(self)
        self._taskbar_owner: QWidget | None = None
        self._drag_offset: QPoint | None = None
        self._expanded_geo: QRect | None = None
        self._collapsed = False
        self._animating = False
        self._shown_for_workspace = False
        self._files: list[Path] = []
        self._resizing = False
        self._resize_left = 0
        self._resize_bottom = 0
        self._resize_start_w = 0
        self._resize_start_h = 0
        self._resize_cursor = (0, 0)
        self._pending_auto_fade = False
        self._pending_preview_path: str | None = None
        self._mix_anim_mode: str | None = None
        self._dim_animating = False
        self._video_input_triggered = False
        self._video_input_ignore_until = 0.0
        self._lan_server = None
        self._lan_hub = None

        self.setObjectName("Root")
        self.setWindowTitle("Media Background Pane")
        self.setWindowIcon(make_app_icon())
        self.setMinimumSize(20, 160)
        self._apply_window_flags()

        self.collapse = CollapseBar()
        self.collapse.clicked.connect(self.toggle_collapsed)
        self.thumbs = ThumbnailStrip()
        self.preview_monitor = MonitorWidget("PREVIEW", "preview")
        self.program_monitor = MonitorWidget("PROGRAM", "program")
        self.mix_slider = LabeledSlider("MIX", Qt.Orientation.Horizontal)
        self.dim_slider = LabeledSlider("DIM")
        self.mix_slider.set_fade_seconds(self.config.fade_seconds)
        self.dim_slider.set_fade_seconds(self.config.fade_seconds)
        self.dim_slider.snap_to(1000)
        self.fade_button = QPushButton("FADE")
        self.fade_button.setObjectName("FadeButton")
        self.fade_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.black_button = QPushButton("FADE TO\nBLACK")
        self.black_button.setObjectName("BlackButton")
        self.black_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.black_button.setToolTip("Fade DIM to black using the fade duration")
        self.video_input_button = QToolButton()
        self.video_input_button.setObjectName("VideoInputButton")
        self.video_input_button.setCheckable(True)
        self.video_input_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.video_input_button.setIcon(make_video_input_icon("#c7c7cc"))
        self.video_input_button.setIconSize(QSize(26, 19))
        self.video_input_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.video_input_button.setToolTip("Fade the first ProPresenter video input on or off")
        self._set_video_input_button(False)
        self.auto_fade = LabeledSwitch("Auto Fade")
        self.auto_fade.setChecked(True)
        self.auto_fade.setToolTip("Clicking a clip sends it to Preview, then fades to Program")
        self.duration_button = QPushButton(f"{self.config.fade_seconds}s")
        self.duration_button.setObjectName("DurationButton")
        self.settings_button = QPushButton()
        self.settings_button.setObjectName("SettingsButton")
        self.settings_button.setIcon(make_settings_icon())
        self.settings_button.setIconSize(QSize(20, 20))
        self.settings_button.setFixedSize(24, 24)
        self.settings_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.settings_button.setToolTip("Settings")
        self.settings_button.setFlat(True)

        self.thumbs.chosen.connect(self._on_thumb)
        self.thumbs.scale_changed.connect(self._on_thumb_scale)
        self.thumbs.set_scale(self.config.thumb_scale)
        self.thumbs.set_footer_leading(self.settings_button)
        self.thumbnailer.ready.connect(self._on_thumb_ready)
        self.fade_button.clicked.connect(self._start_fade)
        self.black_button.clicked.connect(self._start_black_fade)
        self.video_input_button.clicked.connect(self._toggle_video_input)
        self.duration_button.clicked.connect(self._cycle_duration)
        self.settings_button.clicked.connect(self._open_settings)
        self.mix_slider.pressed.connect(self._on_mix_pressed)
        self.mix_slider.released.connect(self._on_mix_released)
        self.mix_slider.value_changed.connect(self._on_mix_changed)
        self.mix_slider.chase_finished.connect(self._on_mix_chase_finished)
        self.dim_slider.pressed.connect(self._on_dim_pressed)
        self.dim_slider.value_changed.connect(self._on_dim_changed)
        self.dim_slider.chase_finished.connect(self._on_dim_chase_finished)

        self.engine.preview_changed.connect(self.preview_monitor.set_image)
        self.engine.program_changed.connect(self.program_monitor.set_image)
        self.engine.preview_changed.connect(self._snapshot_preview)
        self.engine.program_changed.connect(self._snapshot_program)
        self.engine.taken.connect(self._on_taken)

        self.preview_monitor.setMaximumWidth(200)
        self.program_monitor.setMaximumWidth(200)
        self.preview_monitor.setMinimumWidth(120)
        self.program_monitor.setMinimumWidth(120)

        controls = QFrame()
        controls.setObjectName("Controls")
        controls_layout = QHBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(6)
        controls_layout.addWidget(self.video_input_button, 1)
        controls_layout.addWidget(self.auto_fade, 0, Qt.AlignmentFlag.AlignVCenter)
        controls_layout.addWidget(self.fade_button, 1)
        controls_layout.addWidget(self.duration_button)
        controls_layout.addWidget(self.black_button, 1)

        stage = QWidget()
        stage.setObjectName("Stage")
        stage.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        stage.setMinimumWidth(260)
        stage.setMaximumWidth(420)
        stage_layout = QVBoxLayout(stage)
        stage_layout.setContentsMargins(0, 0, 0, 0)
        stage_layout.setSpacing(6)
        monitors = QHBoxLayout()
        monitors.setContentsMargins(0, 0, 0, 0)
        monitors.setSpacing(6)
        monitors.addWidget(self.preview_monitor, 1)
        monitors.addWidget(self.program_monitor, 1)
        stage_layout.addLayout(monitors, 1)
        stage_layout.addWidget(self.mix_slider)
        stage_layout.addWidget(controls)

        self._content = QWidget()
        content_layout = QHBoxLayout(self._content)
        content_layout.setContentsMargins(6, 6, 8, 6)
        content_layout.setSpacing(6)
        content_layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        content_layout.addWidget(self.thumbs, 1)
        content_layout.addWidget(stage, 0)
        content_layout.addWidget(self.dim_slider)
        self._content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        root.addWidget(self.collapse)
        root.addWidget(self._content, 1)

        self._grip = TopRightGrip(self)
        self._grip.pressed.connect(self._on_resize_pressed)
        self._grip.dragged.connect(self._on_resize_dragged)
        self._grip.resized.connect(self._on_resized)

        self._folder_watcher = QFileSystemWatcher(self)
        self._folder_watcher.directoryChanged.connect(self._on_watched_path)
        self._folder_watcher.fileChanged.connect(self._on_watched_path)
        self._scan_timer = QTimer(self)
        self._scan_timer.setSingleShot(True)
        self._scan_timer.setInterval(400)
        self._scan_timer.timeout.connect(self.refresh_library)

        self._workspace_timer = QTimer(self)
        self._workspace_timer.setInterval(1000)
        self._workspace_timer.timeout.connect(self._check_workspace)
        self._workspace_timer.start()
        self._snap_timer = QTimer(self)
        self._snap_timer.setInterval(400)
        self._snap_timer.timeout.connect(self._snap_to_propresenter)
        self._snap_timer.start()
        self._video_input_timer = QTimer(self)
        self._video_input_timer.setInterval(2000)
        self._video_input_timer.timeout.connect(self._try_trigger_video_input)
        self._video_input_status_timer = QTimer(self)
        self._video_input_status_timer.setInterval(1000)
        self._video_input_status_timer.timeout.connect(self._sync_video_input_status)
        prefer_livestream_apps()
        if PATH_SETTINGS.exists():
            self._folder_watcher.addPath(str(PATH_SETTINGS.parent))
            self._folder_watcher.addPath(str(PATH_SETTINGS))

        self._build_tray()
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self._stop_lan_remote)
        self._restore_geometry()
        self.refresh_library()
        self._update_fade_enabled()
        self._update_black_button()

        if self.config.start_with_windows:
            set_startup_enabled(True)
        if not self.config.media_folder:
            QTimer.singleShot(500, self._open_settings)

        if self._pane_should_be_visible():
            self.show()
            self._hide_from_taskbar()
            self._snap_to_propresenter()
            self._shown_for_workspace = True
            if self.config.collapsed:
                QTimer.singleShot(0, lambda: self.set_collapsed(True))
        else:
            self._shown_for_workspace = False

        QTimer.singleShot(700, self._try_trigger_video_input)
        self._video_input_timer.start()
        QTimer.singleShot(400, self._sync_video_input_status)
        self._video_input_status_timer.start()
        self._start_lan_remote()

    def _apply_window_flags(self) -> None:
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if self.config.always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self._hide_from_taskbar)
        QTimer.singleShot(0, self.thumbs._fit_flow)

    def nativeEvent(self, eventType, message):
        try:
            if bytes(eventType) == b"windows_generic_MSG":
                import ctypes
                from ctypes import wintypes

                class POINT(ctypes.Structure):
                    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

                class MINMAXINFO(ctypes.Structure):
                    _fields_ = [
                        ("ptReserved", POINT),
                        ("ptMaxSize", POINT),
                        ("ptMaxPosition", POINT),
                        ("ptMinTrackSize", POINT),
                        ("ptMaxTrackSize", POINT),
                    ]

                msg = wintypes.MSG.from_address(int(message))
                if msg.message == 0x0024:
                    info = MINMAXINFO.from_address(int(msg.lParam))
                    min_w, min_h = self._native_min_size()
                    if self._collapsed:
                        min_w = max(
                            1,
                            round(COLLAPSED_BAR_WIDTH * float(self.devicePixelRatioF())),
                        )
                    info.ptMinTrackSize.x = min_w
                    info.ptMinTrackSize.y = min_h
                    return True, 0
        except (TypeError, ValueError, OSError):
            pass
        return super().nativeEvent(eventType, message)

    def _hide_from_taskbar(self) -> None:
        import ctypes
        from ctypes import wintypes

        hwnd = int(self.winId())
        if not hwnd:
            return
        if self._taskbar_owner is None:
            owner = QWidget()
            owner.setWindowFlags(Qt.WindowType.Tool)
            owner.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
            owner.resize(1, 1)
            owner.createWinId()
            self._taskbar_owner = owner
        owner_hwnd = int(self._taskbar_owner.winId())
        GWL_EXSTYLE = -20
        GWLP_HWNDPARENT = -8
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_APPWINDOW = 0x00040000
        SWP_NOSIZE = 0x0001
        SWP_NOMOVE = 0x0002
        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        SWP_FRAMECHANGED = 0x0020
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.SetWindowPos.argtypes = [
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
        ]
        user32.SetWindowPos.restype = wintypes.BOOL
        user32.SetWindowLongPtrW(hwnd, GWLP_HWNDPARENT, owner_hwnd)
        ex = int(user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE))
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, (ex | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW)
        user32.SetWindowPos(
            hwnd,
            None,
            0,
            0,
            0,
            0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
        )

    def _pp_rect(self) -> QRect | None:
        raw = propresenter_window_rect()
        if not raw:
            return None
        left, top, right, bottom = raw
        screen = QGuiApplication.screenAt(QPoint(left, top)) or QGuiApplication.primaryScreen()
        dpr = float(screen.devicePixelRatio()) if screen else 1.0
        return QRect(
            round(left / dpr),
            round(top / dpr),
            round((right - left) / dpr),
            round((bottom - top) / dpr),
        )

    def _native_hwnd(self) -> int:
        return int(self.winId())

    def _native_min_size(self) -> tuple[int, int]:
        dpr = float(self.devicePixelRatioF())
        return max(1, round(720 * dpr)), max(1, round(160 * dpr))

    def _dock_anchor_native(self) -> tuple[int, int]:
        our = native_window_rect(self._native_hwnd())
        pp = propresenter_window_rect()
        if pp is not None:
            return pp[0], pp[3]
        if our is not None:
            return our[0], our[3]
        geo = self.geometry()
        dpr = float(self.devicePixelRatioF())
        return round(geo.x() * dpr), round((geo.y() + geo.height()) * dpr)

    def _set_native_size_pinned(self, width: int, height: int) -> None:
        min_w, min_h = self._native_min_size()
        width = max(min_w, width)
        height = max(min_h, height)
        if self._collapsed:
            width = max(1, round(COLLAPSED_BAR_WIDTH * float(self.devicePixelRatioF())))
        left = self._resize_left
        top = self._resize_bottom - height
        set_visible_window_rect(self._native_hwnd(), left, top, left + width, top + height)
        our = native_window_rect(self._native_hwnd())
        if our is None:
            return
        actual_h = our[3] - our[1]
        actual_w = our[2] - our[0]
        pinned_top = self._resize_bottom - actual_h
        if our[0] != left or our[1] != pinned_top:
            set_visible_window_rect(
                self._native_hwnd(),
                left,
                pinned_top,
                left + actual_w,
                self._resize_bottom,
            )

    def _on_resize_pressed(self) -> None:
        self._resizing = True
        our = native_window_rect(self._native_hwnd())
        self._resize_left, self._resize_bottom = self._dock_anchor_native()
        if our is None:
            geo = self.geometry()
            dpr = float(self.devicePixelRatioF())
            self._resize_start_w = round(geo.width() * dpr)
            self._resize_start_h = round(geo.height() * dpr)
        else:
            self._resize_start_w = our[2] - our[0]
            self._resize_start_h = our[3] - our[1]
        self._resize_cursor = native_cursor_pos()

    def _on_resize_dragged(self) -> None:
        cx, cy = native_cursor_pos()
        width = self._resize_start_w + (cx - self._resize_cursor[0])
        height = self._resize_start_h - (cy - self._resize_cursor[1])
        self._set_native_size_pinned(width, height)

    def _snap_to_propresenter(self) -> bool:
        if self._resizing:
            return False
        pp = propresenter_window_rect()
        if pp is None:
            return False
        our = native_window_rect(self._native_hwnd())
        if our is None:
            return False
        self._resize_left = pp[0]
        self._resize_bottom = pp[3]
        self._set_native_size_pinned(our[2] - our[0], our[3] - our[1])
        return True

    def _restore_geometry(self) -> None:
        geo = self.config.geometry
        if geo and len(geo) == 4:
            self.setGeometry(*geo)
        else:
            screen = QGuiApplication.primaryScreen().availableGeometry()
            width, height = 1200, 250
            x = screen.x() + max(0, (screen.width() - width) // 2)
            y = screen.y() + screen.height() - height - 48
            self.setGeometry(x, y, width, height)
        self._snap_to_propresenter()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._grip.move(self.width() - self._grip.width(), 0)
        self._grip.setVisible(not self._collapsed)

    def _on_resized(self) -> None:
        self._resizing = False
        self._snap_to_propresenter()
        self._persist_geometry()

    def _persist_geometry(self) -> None:
        geo = self._expanded_geo if self._collapsed else self.geometry()
        self.config.geometry = [geo.x(), geo.y(), geo.width(), geo.height()]
        self.config.collapsed = self._collapsed
        self.config.save()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            child = self.childAt(event.position().toPoint())
            if child in (self.mix_slider.slider, self.dim_slider.slider, self._grip) or self._pp_rect() is not None:
                return
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event) -> None:
        self._menu().exec(event.globalPos())

    def _menu(self) -> QMenu:
        menu = QMenu(self)
        if self._collapsed:
            menu.addAction("Expand", lambda: self.set_collapsed(False))
        else:
            menu.addAction("Collapse", lambda: self.set_collapsed(True))
        menu.addAction("Settings…", self._open_settings)
        menu.addSeparator()
        menu.addAction("Quit", QApplication.quit)
        return menu

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip("Media Background Pane")
        self.tray.setContextMenu(self._menu())
        self.tray.activated.connect(self._on_tray)
        self.tray.show()

    def _on_tray(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self.isVisible() and not self._collapsed:
                self.set_collapsed(True)
            else:
                self.set_collapsed(False)
                self.show()
                self.raise_()
                self.activateWindow()

    def toggle_collapsed(self) -> None:
        self.set_collapsed(not self._collapsed)

    def set_collapsed(self, collapsed: bool) -> None:
        if collapsed and not self._collapsed:
            self._expanded_geo = self.geometry()
        self._collapsed = collapsed
        self.config.collapsed = collapsed
        self._sync_gui_enabled()
        self.collapse.set_collapsed(collapsed)
        self._content.setVisible(not collapsed)
        self._grip.setVisible(not collapsed)
        if collapsed:
            geo = self._expanded_geo or self.geometry()
            self.setMinimumWidth(COLLAPSED_BAR_WIDTH)
            self.setMaximumWidth(COLLAPSED_BAR_WIDTH)
            self.setGeometry(geo.x(), geo.y(), COLLAPSED_BAR_WIDTH, geo.height())
            self._snap_to_propresenter()
        else:
            self.setMinimumWidth(720)
            self.setMinimumHeight(160)
            self.setMaximumWidth(16777215)
            self.setMaximumHeight(16777215)
            if self._expanded_geo is not None:
                self.setGeometry(self._expanded_geo)
            self.show()
            self.raise_()
            self._hide_from_taskbar()
            self._snap_to_propresenter()

    def refresh_library(self) -> None:
        folder = Path(self.config.media_folder) if self.config.media_folder else None
        paths: list[Path] = []
        watched = set(self._folder_watcher.directories())
        for path in list(watched):
            if path != str(PATH_SETTINGS.parent):
                self._folder_watcher.removePath(path)
        if folder and folder.is_dir():
            paths = media_files(folder, self.config.include_subfolders)
            self._folder_watcher.addPath(str(folder))
        self._files = paths
        self.thumbs.set_files(paths)
        self._sync_thumb_roles()
        for path in paths:
            image = self.thumbnailer.request(path)
            if image is not None:
                self.thumbs.set_thumb(str(path), image)
            elif path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}:
                image = image_thumbnail(path)
                if not image.isNull():
                    self.thumbs.set_thumb(str(path), image)

    def _on_watched_path(self, path: str = "") -> None:
        if "PathSettings" in path or Path(path) == PATH_SETTINGS.parent:
            self._check_workspace()
            if PATH_SETTINGS.is_file() and str(PATH_SETTINGS) not in self._folder_watcher.files():
                self._folder_watcher.addPath(str(PATH_SETTINGS))
        self._schedule_scan()

    def _schedule_scan(self, _path: str = "") -> None:
        self._scan_timer.start()

    def _try_trigger_video_input(self) -> None:
        if self._video_input_triggered:
            self._video_input_timer.stop()
            return
        status = video_input_layer_active()
        if status is True:
            self._video_input_triggered = True
            self._video_input_timer.stop()
            self._set_video_input_button(True)
            return
        if trigger_first_video_input():
            self._video_input_triggered = True
            self._video_input_timer.stop()
            self._set_video_input_button(True)

    def _sync_video_input_status(self) -> None:
        if monotonic() < self._video_input_ignore_until:
            return
        active = video_input_layer_active()
        if active is None:
            return
        if active != self.video_input_button.isChecked():
            self._set_video_input_button(active)

    def _set_video_input_button(self, active: bool) -> None:
        self.video_input_button.blockSignals(True)
        self.video_input_button.setChecked(active)
        self.video_input_button.blockSignals(False)
        if active:
            self.video_input_button.setText("VIDEO\nINPUT ON")
            self.video_input_button.setIcon(make_video_input_icon("#64d2ff"))
            self.video_input_button.setToolTip("Video input is live — click to fade it off")
        else:
            self.video_input_button.setText("VIDEO\nINPUT OFF")
            self.video_input_button.setIcon(make_video_input_icon("#c7c7cc"))
            self.video_input_button.setToolTip("Fade on the first ProPresenter video input")

    def _toggle_video_input(self) -> None:
        want_on = self.video_input_button.isChecked()
        duration = self.config.fade_seconds
        if want_on:
            ok = trigger_first_video_input(duration)
            if ok:
                self._video_input_triggered = True
                self._video_input_timer.stop()
                self._video_input_ignore_until = monotonic() + max(1.5, duration + 0.3)
                self._set_video_input_button(True)
            else:
                self._set_video_input_button(False)
        else:
            ok = clear_video_input_layer(duration)
            if ok:
                self._video_input_ignore_until = monotonic() + max(1.5, duration + 0.3)
                self._set_video_input_button(False)
            else:
                self._set_video_input_button(True)

    def _on_thumb_scale(self, width: int) -> None:
        self.config.thumb_scale = width

    def _on_thumb(self, path: str) -> None:
        self.thumbs.set_selected(path)
        try:
            self.engine.preview.frame_ready.disconnect(self._kick_auto_fade)
        except (RuntimeError, TypeError):
            pass
        self._stop_mix_animation()
        self._pending_auto_fade = self.auto_fade.isChecked()
        mix = self.mix_slider.slider.value() / 1000.0
        if mix > 0.001:
            self._pending_preview_path = path
            self.thumbs.set_selected(path)
            if mix >= 0.5:
                self._start_mix_up_for_swap()
            else:
                self._start_mix_down()
            return
        self._pending_preview_path = None
        self._load_preview_path(path)
        self._arm_auto_fade_if_needed()

    def _load_preview_path(self, path: str) -> None:
        self.engine.load_preview(Path(path))
        self._sync_thumb_roles()
        self._update_fade_enabled()

    def _arm_auto_fade_if_needed(self) -> None:
        if not self._pending_auto_fade:
            return
        if self.engine.preview.is_video:
            self.engine.preview.frame_ready.connect(self._kick_auto_fade)
        else:
            self._kick_auto_fade()

    def _kick_auto_fade(self) -> None:
        try:
            self.engine.preview.frame_ready.disconnect(self._kick_auto_fade)
        except (RuntimeError, TypeError):
            pass
        if not self._pending_auto_fade:
            return
        self._pending_auto_fade = False
        self._start_fade()

    def _start_mix_down(self) -> None:
        self._stop_dim_animation()
        current = self.mix_slider.slider.value() / 1000.0
        if current <= 0.001 or self.engine.dimmer <= 0.001:
            self._finish_mix_down()
            return
        self._mix_anim_mode = "down"
        self._animating = True
        self.mix_slider.set_target(0)

    def _start_mix_up_for_swap(self) -> None:
        self._stop_dim_animation()
        current = self.mix_slider.slider.value() / 1000.0
        if current >= 0.999 or self.engine.dimmer <= 0.001:
            self._finish_mix_up_then_swap()
            return
        self._mix_anim_mode = "up_swap"
        self._animating = True
        self.mix_slider.set_target(1000)

    def _finish_mix_down(self) -> None:
        self.mix_slider.snap_to(0)
        self.engine.set_mix(0.0)
        path = self._pending_preview_path
        self._pending_preview_path = None
        if not path:
            return
        self._load_preview_path(path)
        self._arm_auto_fade_if_needed()

    def _finish_mix_up_then_swap(self) -> None:
        self.mix_slider.snap_to(1000)
        self.engine.set_mix(1.0)
        self.engine.take()
        path = self._pending_preview_path
        self._pending_preview_path = None
        if not path:
            return
        self._load_preview_path(path)
        self._arm_auto_fade_if_needed()

    def _on_thumb_ready(self, path: str, image: QImage) -> None:
        self.thumbs.set_thumb(path, image)

    def _sync_thumb_roles(self) -> None:
        preview = str(self.engine.preview.path) if self.engine.preview.path else None
        program = str(self.engine.program.path) if self.engine.program.path else None
        self.thumbs.set_selected(preview)
        self.thumbs.set_program(program)

    def _on_taken(self) -> None:
        self._sync_thumb_roles()
        self.mix_slider.snap_to(0)
        self._update_fade_enabled()

    def _update_fade_enabled(self) -> None:
        self.fade_button.setEnabled(self.engine.preview.has_source)

    def _cycle_duration(self) -> None:
        seconds = self.config.cycle_fade()
        self.duration_button.setText(f"{seconds}s")
        self.mix_slider.set_fade_seconds(seconds)
        self.dim_slider.set_fade_seconds(seconds)
        self.config.save()

    def _stop_mix_animation(self) -> None:
        self._animating = False
        self._mix_anim_mode = None
        self._pending_preview_path = None
        self.mix_slider.slider.stop_chase()

    def _stop_dim_animation(self) -> None:
        if not self._dim_animating:
            return
        self._dim_animating = False
        self.dim_slider.slider.stop_chase()

    def _on_mix_pressed(self) -> None:
        try:
            self.engine.preview.frame_ready.disconnect(self._kick_auto_fade)
        except (RuntimeError, TypeError):
            pass
        self._pending_auto_fade = False
        self._stop_mix_animation()

    def _on_mix_released(self) -> None:
        if self.mix_slider.slider.is_chasing():
            return
        if self.mix_slider.slider.value() >= 1000 and self.engine.preview.has_source:
            self.engine.take()

    def _on_mix_changed(self, value: int) -> None:
        self.engine.set_mix(value / 1000.0)

    def _on_mix_chase_finished(self) -> None:
        mode = self._mix_anim_mode
        self._mix_anim_mode = None
        self._animating = False
        if mode == "down":
            self._finish_mix_down()
            return
        if mode == "up_swap":
            self._finish_mix_up_then_swap()
            return
        if self.mix_slider.slider.value() >= 1000 and self.engine.preview.has_source:
            self.engine.take()

    def _on_dim_pressed(self) -> None:
        self._dim_animating = False
        self.dim_slider.slider.stop_chase()

    def _on_dim_changed(self, value: int) -> None:
        chasing = self.dim_slider.slider.is_chasing()
        self.engine.set_dimmer(value / 1000.0, allow_pause=not chasing)
        self._update_black_button()

    def _on_dim_chase_finished(self) -> None:
        self._dim_animating = False
        amount = self.dim_slider.slider.value() / 1000.0
        self.engine.set_dimmer(amount, allow_pause=True)
        self._update_black_button()

    def _fully_black(self) -> bool:
        return self.dim_slider.slider.value() <= 1

    def _update_black_button(self) -> None:
        if self._fully_black():
            self.black_button.setText("FADE FROM\nBLACK")
            self.black_button.setProperty("fromBlack", True)
            self.black_button.setToolTip("Fade DIM up from black using the fade duration")
        else:
            self.black_button.setText("FADE TO\nBLACK")
            self.black_button.setProperty("fromBlack", False)
            self.black_button.setToolTip("Fade DIM to black using the fade duration")
        self.black_button.style().unpolish(self.black_button)
        self.black_button.style().polish(self.black_button)
        self.black_button.update()

    def _start_fade(self) -> None:
        if not self.engine.preview.has_source:
            return
        self._stop_dim_animation()
        self._pending_preview_path = None
        if self.engine.dimmer <= 0.001:
            self.engine.take()
            return
        current = self.mix_slider.slider.value() / 1000.0
        if current >= 0.999:
            self.engine.take()
            return
        self._mix_anim_mode = "up"
        self._animating = True
        self.mix_slider.set_target(1000)

    def _start_black_fade(self) -> None:
        try:
            self.engine.preview.frame_ready.disconnect(self._kick_auto_fade)
        except (RuntimeError, TypeError):
            pass
        self._pending_auto_fade = False
        self._stop_mix_animation()
        current = float(self.engine.dimmer)
        target = 1.0 if current <= 0.001 else 0.0
        if abs(target - current) < 0.001:
            self.dim_slider.snap_to(int(round(target * 1000)))
            self.engine.set_dimmer(target, allow_pause=True)
            self._update_black_button()
            return
        self._dim_animating = True
        self.dim_slider.set_target(int(round(target * 1000)))

    def _open_settings(self) -> None:
        status, _ok = self.engine.ndi_status
        dialog = SettingsDialog(
            self.config,
            status,
            self,
            remote_url=self._lan_url_text(),
            decoder_status=self.engine.decoder_status,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return
        old_name = self.config.ndi_name
        old_quality = self.config.quality
        dialog.apply_to(self.config)
        self.config.save()
        set_startup_enabled(self.config.start_with_windows)
        self.engine.set_fps(self.config.fps)
        self.engine.set_quality(self.config.quality)
        self.engine.set_gpu_decode(self.config.use_gpu_decode)
        self._apply_window_flags()
        self.show()
        self._hide_from_taskbar()
        self.refresh_library()
        if self.config.ndi_name != old_name:
            QMessageBox.information(
                self,
                "NDI name",
                "The NDI source name will fully update the next time the app starts.",
            )
        elif self.config.quality != old_quality:
            QMessageBox.information(
                self,
                "Quality",
                "Output resolution changed. If ProPresenter goes black, re-select the Media Background Pane input mode.",
            )

    def _pane_should_be_visible(self) -> bool:
        if self.config.auto_show_on_workspace:
            return workspace_is_open(self.config.workspace_name)
        return is_propresenter_running()

    def _check_workspace(self) -> None:
        prefer_livestream_apps()
        should_show = self._pane_should_be_visible()
        if should_show and not self._shown_for_workspace:
            self._shown_for_workspace = True
            self.set_collapsed(False)
            self.show()
            self.raise_()
            self._hide_from_taskbar()
            self._snap_to_propresenter()
            self._sync_gui_enabled()
        elif not should_show and (self._shown_for_workspace or self.isVisible()):
            self._shown_for_workspace = False
            self.hide()
            self._sync_gui_enabled()

    def _sync_gui_enabled(self) -> None:
        lan = self._lan_server is not None
        self.engine.gui_enabled = lan or (self.isVisible() and not self._collapsed)

    def _lan_url_text(self) -> str:
        if self._lan_server is not None:
            urls = self._lan_server.urls()
        else:
            urls = lan_urls(self.config.remote_port)
        return urls[0] if urls else f"http://127.0.0.1:{self.config.remote_port}"

    def _start_lan_remote(self) -> None:
        self._lan_hub = LanHub()
        self._lan_server = LanServer(self._lan_hub, self.config.remote_port)
        url = self._lan_server.start()
        if url is None:
            self._lan_server = None
            self.tray.setToolTip("Media Background Pane")
            return
        self._snapshot_preview(self.preview_monitor._image)
        self._snapshot_program(self.program_monitor._image)
        self._lan_timer = QTimer(self)
        self._lan_timer.setInterval(50)
        self._lan_timer.timeout.connect(self._pump_lan)
        self._lan_timer.start()
        self._sync_gui_enabled()
        self._publish_lan_state()
        urls = "\n".join(self._lan_server.urls())
        self.tray.setToolTip(f"Media Background Pane\n{urls}")

    def _stop_lan_remote(self) -> None:
        timer = getattr(self, "_lan_timer", None)
        if timer is not None:
            timer.stop()
        if self._lan_server is not None:
            self._lan_server.stop()
            self._lan_server = None

    def _snapshot_preview(self, image: QImage) -> None:
        if self._lan_server is None:
            return
        self._lan_hub.set_jpeg("preview", _qimage_jpeg(image))

    def _snapshot_program(self, image: QImage) -> None:
        if self._lan_server is None:
            return
        self._lan_hub.set_jpeg("program", _qimage_jpeg(image))

    def _publish_lan_state(self) -> None:
        if self._lan_server is None:
            return
        files = []
        for path in self._files:
            files.append(
                {
                    "path": str(path),
                    "name": path.name,
                    "thumb": f"/thumbs/{thumb_cache_path(path).name}",
                }
            )
        preview = str(self.engine.preview.path) if self.engine.preview.path else ""
        program = str(self.engine.program.path) if self.engine.program.path else ""
        self._lan_hub.publish_state(
            {
                "mix": self.mix_slider.slider.value() / 1000.0,
                "dim": self.dim_slider.slider.value() / 1000.0,
                "mix_target": self.mix_slider.ghost() / 1000.0,
                "dim_target": self.dim_slider.ghost() / 1000.0,
                "duration": self.config.fade_seconds,
                "auto_fade": self.auto_fade.isChecked(),
                "video_input": self.video_input_button.isChecked(),
                "preview": preview,
                "program": program,
                "fade_enabled": self.engine.preview.has_source,
                "files": files,
            }
        )

    def _pump_lan(self) -> None:
        if self._lan_hub is None or self._lan_server is None:
            return
        mix_cmd = None
        dim_cmd = None
        others: list[dict] = []
        while True:
            try:
                cmd = self._lan_hub.commands.get_nowait()
            except Empty:
                break
            op = cmd.get("op")
            if op == "mix_target":
                mix_cmd = cmd
            elif op == "dim_target":
                dim_cmd = cmd
            else:
                others.append(cmd)
        for cmd in others:
            self._apply_lan_command(cmd)
        if mix_cmd is not None:
            self._apply_lan_command(mix_cmd)
        if dim_cmd is not None:
            self._apply_lan_command(dim_cmd)
        self._publish_lan_state()

    def _apply_lan_command(self, cmd: dict) -> None:
        op = cmd.get("op")
        if op == "preview":
            path = cmd.get("path")
            if path:
                self._on_thumb(str(path))
        elif op == "fade":
            self._start_fade()
        elif op == "take":
            if self.engine.preview.has_source:
                self.engine.take()
        elif op == "mix_target":
            try:
                value = max(0.0, min(1.0, float(cmd.get("value", 0))))
            except (TypeError, ValueError):
                return
            self._pending_auto_fade = False
            self.mix_slider.set_target(int(round(value * 1000)))
        elif op == "dim_target":
            try:
                value = max(0.0, min(1.0, float(cmd.get("value", 1))))
            except (TypeError, ValueError):
                return
            self.dim_slider.set_target(int(round(value * 1000)))
        elif op == "black":
            self._start_black_fade()
        elif op == "duration":
            self._cycle_duration()
        elif op == "auto_fade":
            self.auto_fade.setChecked(bool(cmd.get("value")))
        elif op == "video_input":
            want = bool(cmd.get("value"))
            if want != self.video_input_button.isChecked():
                self.video_input_button.setChecked(want)
                self._toggle_video_input()

    def closeEvent(self, event) -> None:
        self._persist_geometry()
        self._stop_lan_remote()
        self.engine.close()
        event.accept()
