from __future__ import annotations

import ctypes
import queue
from ctypes import wintypes
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty
from time import monotonic

from PySide6.QtCore import (
    QBuffer,
    QFileSystemWatcher,
    QIODevice,
    QObject,
    QPoint,
    QPointF,
    QRect,
    QSize,
    Qt,
    QTimer,
    Signal,
    Slot,
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
from .library import (
    Thumbnailer,
    copy_into_folder,
    dropped_media_paths,
    image_thumbnail,
    media_files,
    thumb_cache_path,
)
from .log import Throttle, log_event, log_exception
from .priority import prefer_livestream_apps
from .propresenter import (
    PATH_SETTINGS,
    clear_video_input_layer,
    apply_tool_window_style,
    native_cursor_pos,
    native_window_rect,
    native_window_visible,
    open_workspace,
    propresenter_main_window,
    propresenter_pids,
    propresenter_window_rect,
    restore_window_no_activate,
    set_visible_window_rect,
    set_window_owner,
    show_window_no_activate,
    stack_window_above,
    window_is_cloaked,
    window_is_minimized,
    window_owner,
    trigger_first_video_input,
    video_input_layer_active,
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


_APP_ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def app_icon_path() -> Path:
    return Path(__file__).resolve().parents[1] / "icon.png"


def make_app_icon() -> QIcon:
    image = QImage(str(app_icon_path()))
    if image.isNull():
        return _drawn_app_icon()
    icon = QIcon()
    for size in _APP_ICON_SIZES:
        scaled = image.scaled(
            size,
            size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        icon.addPixmap(QPixmap.fromImage(scaled))
    return icon


def _drawn_app_icon() -> QIcon:
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


_WM_GETMINMAXINFO = 0x0024


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _MINMAXINFO(ctypes.Structure):
    _fields_ = [
        ("ptReserved", _POINT),
        ("ptMaxSize", _POINT),
        ("ptMaxPosition", _POINT),
        ("ptMinTrackSize", _POINT),
        ("ptMaxTrackSize", _POINT),
    ]


def _pid_list(pids) -> str:
    return ", ".join(f"P{pid}" for pid in pids)


# The MIX and DIM fades tick every 8 ms on the GUI thread, so anything that
# blocks it makes a fade stop and start. notes/gui-thread.md has what used to
# block it and for how long.


class BackgroundCalls(QObject):
    """Slow calls, run one at a time on a thread of their own instead of the GUI thread.

    submit() is called on the GUI thread. The call runs on this object's
    thread, in the order submitted, and on_done(result) then runs back on the
    GUI thread, through a queued signal.

    A call of a kind that is already waiting or running is skipped (single,
    the default), so a slow ProPresenter or Google Drive cannot make requests
    pile up. If the call raises, the error is logged and on_done gets
    `failed` instead. If the thread itself has ended, the next submit()
    starts a new one and says so in the session log.
    """

    _finished = Signal(object)

    def __init__(self, name: str) -> None:
        # No Qt parent, so Qt never deletes this object while its thread can
        # still be in a request and emit _finished afterwards.
        super().__init__()
        self.name = name
        self._jobs: queue.SimpleQueue = queue.SimpleQueue()
        self._waiting: dict[str, int] = {}  # kind -> calls waiting or running; GUI thread only
        self._stopped = False
        self._finished.connect(self._deliver, Qt.ConnectionType.QueuedConnection)
        self._thread = self._start_thread()

    def busy(self, *kinds: str) -> bool:
        """Whether a call of any of these kinds is waiting or running."""
        return any(self._waiting.get(kind, 0) > 0 for kind in kinds)

    def submit(self, kind: str, call, on_done=None, *, single: bool = True, failed=None) -> bool:
        """Queue call(). False if it was skipped: one of its kind is already waiting or running, or stop() was called."""
        if self._stopped:
            return False
        self._keep_alive()
        if single and self.busy(kind):
            return False
        self._waiting[kind] = self._waiting.get(kind, 0) + 1
        self._jobs.put((kind, call, on_done, failed))
        return True

    def stop(self, wait: float = 0.0) -> None:
        """Take no more calls. One already running finishes, but its result is dropped."""
        self._stopped = True
        self._jobs.put(None)
        if wait > 0:
            self._thread.join(wait)

    def thread_alive(self) -> bool:
        return self._thread.is_alive()

    def _start_thread(self) -> threading.Thread:
        thread = threading.Thread(target=self._run, args=(self._jobs,), name=f"{self.name} calls", daemon=True)
        thread.start()
        return thread

    def _keep_alive(self) -> None:
        try:
            alive = self._thread.is_alive()
        except Exception:
            return  # cannot tell; leave it
        if alive:
            return
        # Calls it had taken are lost with it, so nothing of theirs is waiting any more.
        log_event(f"{self.name} thread", "it had stopped; started it again")
        self._jobs = queue.SimpleQueue()
        self._waiting.clear()
        self._thread = self._start_thread()

    def _run(self, jobs: queue.SimpleQueue) -> None:
        while True:
            job = jobs.get()
            if job is None or self._stopped:
                return
            kind, call, on_done, failed = job
            try:
                result = call()
            except Exception as exc:
                log_exception(f"{self.name} thread: {kind}", exc)
                result = failed
            if self._stopped:
                return
            try:
                self._finished.emit((kind, on_done, result))
            except RuntimeError:
                return  # this object is gone: the pane is closing

    @Slot(object)
    def _deliver(self, finished: tuple) -> None:
        kind, on_done, result = finished
        self._waiting[kind] = max(0, self._waiting.get(kind, 0) - 1)
        if on_done is not None:
            on_done(result)


# Thumbnails the library thread makes itself, with QImageReader. A video's
# first frame needs QMediaPlayer, which stays on the GUI thread (Thumbnailer).
_STILL_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"}


@dataclass
class LibraryScan:
    """What the library thread found in the media folder."""

    folder: Path | None
    folder_ok: bool  # set, and a folder
    files: list[Path] = field(default_factory=list)
    thumb_names: list[str] = field(default_factory=list)  # each file's thumbnail cache name, for the iPad
    thumbs: dict[str, QImage] = field(default_factory=dict)  # thumbnails already made, by path


def scan_library(folder: Path | None, recursive: bool, extra: list[Path] | None = None) -> LibraryScan:
    """Everything the library needs from the disk: the media files and their thumbnails.

    Runs on the library thread. The folder is on Google Drive. Each file
    costs a stat and a resolve() for its thumbnail's cache name, and the
    cached thumbnail is read here too. extra: files just copied in, kept
    even if the folder listing does not show them yet.
    """
    folder_ok = folder is not None and folder.is_dir()
    paths = media_files(folder, recursive) if folder_ok else []
    if extra:
        known = {str(path) for path in paths}
        for path in extra:
            if str(path) in known or not path.is_file():
                continue
            paths.append(path)
            known.add(str(path))
        paths.sort(key=lambda item: item.name.casefold())
    scan = LibraryScan(folder, folder_ok, paths)
    for path in paths:
        cache = thumb_cache_path(path)
        scan.thumb_names.append(cache.name)
        image = QImage(str(cache)) if cache.is_file() else QImage()
        if image.isNull() and path.suffix.lower() in _STILL_SUFFIXES:
            image = image_thumbnail(path)
        if not image.isNull():
            scan.thumbs[str(path)] = image
    return scan


def _make_awake_clock():
    """A clock in seconds that stops while the PC sleeps (QueryUnbiasedInterruptTime).

    So a suspend in the middle of a service is not taken for a blocked GUI
    thread. Falls back to monotonic() if Windows will not give it.
    """
    try:
        query = ctypes.WinDLL("kernel32").QueryUnbiasedInterruptTime
        query.argtypes = [ctypes.POINTER(ctypes.c_ulonglong)]
        query.restype = ctypes.c_int
        if not query(ctypes.byref(ctypes.c_ulonglong())):
            return monotonic
    except (AttributeError, OSError):
        return monotonic

    def awake() -> float:
        ticks = ctypes.c_ulonglong()  # one per call: the watcher thread reads the clock too
        query(ctypes.byref(ticks))
        return ticks.value / 10_000_000  # 100 ns units

    return awake


awake_seconds = _make_awake_clock()


def _thread_calls(ident: int, stop_at=None, limit: int = 5) -> str:
    """The Python calls a running thread is in, innermost first: "f (a.py:3) < g (b.py:9)".

    Stops at the frame stop_at (where the thread entered Qt's event loop), so
    "" means the thread is running no Python code of its own. Never raises.
    """
    try:
        frame = sys._current_frames().get(ident)
        calls = []
        while frame is not None and frame is not stop_at and len(calls) < limit:
            code = frame.f_code
            name = code.co_filename.replace("/", "\\").rsplit("\\", 1)[-1]
            calls.append(f"{code.co_name} ({name}:{frame.f_lineno})")
            frame = frame.f_back
        return " < ".join(calls)
    except Exception:
        return "?"


def _seconds(value: float) -> str:
    return f"{value:.1f} s" if value < 10 else f"{value:.0f} s"


class GuiStallDetector(QObject):
    """Logs a line when the GUI thread was blocked: for how long, and where.

    A timer here should fire every interval. When it fires much later, the
    GUI thread was busy with something else for that long, and the MIX and
    DIM fades, which tick on the GUI thread, stood still. Meanwhile a watcher
    thread looks at the GUI thread and notes the Python code it is in.

    The block began somewhere in the interval before the late beat, so it
    lasted the gap between beats less half an interval, give or take half an
    interval (0.05 s). A line is written when that is over threshold. Lines
    go through a Throttle: five a minute at most; beyond that they are
    counted, and the count goes in with the next line that has room. Time
    with the PC asleep does not count.
    """

    def __init__(
        self,
        parent: QObject | None = None,
        *,
        interval: float = 0.1,
        threshold: float = 0.25,
        events: Throttle | None = None,
        clock=None,
    ) -> None:
        super().__init__(parent)
        self.interval = interval
        self.threshold = threshold
        self._events = events or Throttle(60.0, burst=5)
        self._clock = clock or awake_seconds
        self._last = self._clock()
        self._gui_ident = threading.get_ident()  # made on the GUI thread
        self._loop_frame = None  # where the GUI thread is in Python while it waits in Qt's event loop
        self._sample: tuple[float, str] | None = None  # (the late beat's start, where the GUI thread was)
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)  # like the fades' own timer
        self._timer.setInterval(max(1, round(interval * 1000)))
        self._timer.timeout.connect(self._beat)
        self._stop = threading.Event()
        self._watcher: threading.Thread | None = None

    def start(self, *, watch: bool = True) -> None:
        self._last = self._clock()
        self._timer.start()
        if watch and self._watcher is None:
            self._stop.clear()
            self._watcher = threading.Thread(target=self._watch, name="gui stall watch", daemon=True)
            self._watcher.start()

    def stop(self) -> None:
        self._timer.stop()
        self._stop.set()
        watcher, self._watcher = self._watcher, None
        if watcher is not None:
            watcher.join(1.0)

    def _beat(self) -> None:
        now = self._clock()
        last, self._last = self._last, now
        try:
            # Called from Qt's event loop, so the frame below is the one waiting in it.
            self._loop_frame = sys._getframe(1)
        except ValueError:
            self._loop_frame = None
        blocked = now - last - self.interval / 2
        if blocked > self.threshold:
            sample = self._sample
            where = sample[1] if sample is not None and sample[0] == last else None
            self._events.event("blocked", "gui", self._message(blocked, where))
        self._events.flush()

    @staticmethod
    def _message(blocked: float, where: str | None) -> str:
        text = f"the window thread was blocked for {_seconds(blocked)}"
        if where is None:
            return text
        if not where:
            return f"{text}; it was running no Python code (Qt's own work, or waiting for another thread)"
        return f"{text}; it was in {where}"

    def _watch(self) -> None:
        while not self._stop.wait(self.interval / 2):
            last = self._last
            if self._clock() - last <= self.threshold:
                continue
            sample = self._sample
            if sample is not None and sample[0] == last:
                continue  # this one is looked at already
            self._sample = (last, _thread_calls(self._gui_ident, self._loop_frame))


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
        # Tray "Show pane": keep the pane up whatever the workspace check says,
        # until ProPresenter closes or restarts, or the check agrees.
        self._shown_by_hand = False
        self._pp_pids: tuple[int, ...] | None = None
        self._last_decision: tuple | None = None
        self._attached: tuple | None = None
        self._window_state: tuple | None = None
        # Session-log lines about the pane's window, which could otherwise
        # repeat many times a second if something kept hiding it.
        self._events = Throttle(10.0)
        self._files: list[Path] = []
        self._ignore_folder_scan_until = 0.0
        self._resizing = False
        self._resize_left = 0
        self._resize_bottom = 0
        self._resize_start_w = 0
        self._resize_start_h = 0
        self._resize_cursor = (0, 0)
        self._pending_auto_fade = False
        self._pending_preview_path: str | None = None
        self._fade_after_black = False
        self._mix_anim_mode: str | None = None
        self._dim_animating = False
        self._video_input_triggered = False
        self._video_input_ignore_until = 0.0
        # Moves on whenever the Video Input state may have changed: the
        # operator's click, the start-up switch-on, a new ProPresenter run.
        # Answers to requests sent before that are out of date.
        self._video_input_serial = 0
        # What ProPresenter last said, or the last change it took.
        self._video_input_known = False
        self._lan_server = None
        self._lan_hub = None
        self._lan_files: list[dict] = []
        # ProPresenter's API and the process snapshot for priorities, off the
        # GUI thread. A status request can wait 0.8 s, and finding the API
        # while ProPresenter starts waited 2.4 s.
        self._pp_calls = BackgroundCalls("propresenter")
        # The media folder is on Google Drive: its scans and copies.
        self._library_calls = BackgroundCalls("library")
        self._library_rescan = False
        self._library_extra: list[Path] = []
        self._stall_detector = GuiStallDetector(self)

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
        self.auto_fade.setToolTip(
            "Clicking a clip sends it to Preview, then fades to Program. "
            "While DIM is black, Preview shows that clip until DIM comes up"
        )
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
        self.thumbs.scale_mode_changed.connect(self._on_scale_mode)
        self.thumbs.files_dropped.connect(self._import_dropped_files)
        self.thumbs.set_scale(self.config.thumb_scale)
        self.thumbs.set_scale_mode(self.config.scale_mode)
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
        self._z_timer = QTimer(self)
        self._z_timer.setInterval(100)
        self._z_timer.timeout.connect(self._stack_above_propresenter)
        self._z_timer.start()
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
            app.aboutToQuit.connect(self._stop_background_work)
        self._restore_geometry()
        self.refresh_library()
        self._update_fade_enabled()
        self._update_black_button()

        if self.config.start_with_windows:
            set_startup_enabled(True)
        if not self.config.media_folder:
            QTimer.singleShot(500, self._open_settings)

        show, why = self._workspace_decision(self._read_propresenter())
        self._last_decision = (show, why, False)
        if show is True:
            self._shown_for_workspace = True
            self.show()
            self._hide_from_taskbar()
            self._snap_to_propresenter()
            self._stack_above_propresenter()
            if self.config.collapsed:
                QTimer.singleShot(0, lambda: self.set_collapsed(True))
            log_event("pane", f"shown at start: {why}")
        else:
            self._shown_for_workspace = False
            log_event("pane", f"not shown at start: {why}")

        QTimer.singleShot(700, self._try_trigger_video_input)
        self._video_input_timer.start()
        QTimer.singleShot(400, self._sync_video_input_status)
        self._video_input_status_timer.start()
        self._start_lan_remote()
        self._stall_detector.start()

    def _stop_background_work(self) -> None:
        self._stall_detector.stop()
        self._pp_calls.stop()
        self._library_calls.stop()

    def _apply_window_flags(self) -> None:
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        self.setWindowFlags(flags)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        QTimer.singleShot(0, self._hide_from_taskbar)
        QTimer.singleShot(0, self._stack_above_propresenter)
        QTimer.singleShot(0, self.thumbs._fit_flow)

    def nativeEvent(self, eventType, message):
        # Runs for every Windows message to the window, on the GUI thread. The
        # two structures used to be defined here, on each call: about 50 us a
        # message (5 Nov 2026). They are defined once now, below the class.
        try:
            if bytes(eventType) == b"windows_generic_MSG":
                msg = wintypes.MSG.from_address(int(message))
                if msg.message == _WM_GETMINMAXINFO:
                    info = _MINMAXINFO.from_address(int(msg.lParam))
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
        apply_tool_window_style(hwnd)
        if not self.config.always_on_top:
            set_window_owner(hwnd, int(self._taskbar_owner.winId()))

    # The window code below looks for ProPresenter's window up to ten times a
    # second. It looks among the process ids the workspace check found last
    # (self._pp_pids, once a second): a process snapshot each time cost 7 ms
    # of the GUI thread (5 Nov 2026, 256 processes), and looking through the
    # windows alone costs 0.4 ms. Before the first check, None takes a snapshot.

    def _pp_rect(self) -> QRect | None:
        raw = propresenter_window_rect(self._pp_pids)
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
        pp = propresenter_window_rect(self._pp_pids)
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
        pp = propresenter_window_rect(self._pp_pids)
        if pp is None:
            return False
        our = native_window_rect(self._native_hwnd())
        if our is None:
            return False
        self._resize_left = pp[0]
        self._resize_bottom = pp[3]
        self._set_native_size_pinned(our[2] - our[0], our[3] - our[1])
        self._stack_above_propresenter()
        return True

    def _detach_hidden_owner(self) -> None:
        hwnd = self._native_hwnd()
        if not hwnd:
            return
        owner = window_owner(hwnd)
        # Windows will not show a window whose owner is hidden.
        if owner and not native_window_visible(owner):
            set_window_owner(hwnd, 0)

    def _pane_is_on_screen(self) -> bool:
        return self.isVisible() and native_window_visible(self._native_hwnd())

    def _stack_above_propresenter(self) -> None:
        hwnd = self._native_hwnd()
        if not hwnd:
            return
        dummy = int(self._taskbar_owner.winId()) if self._taskbar_owner is not None else 0
        if self.config.always_on_top and (self._shown_for_workspace or self._shown_by_hand):
            found = propresenter_main_window(self._pp_pids)
            if found:
                stack_window_above(hwnd, found[0])
            else:
                self._detach_hidden_owner()
            self._note_attached(found)
            self._show_if_windows_hid_it()
            return
        if dummy:
            set_window_owner(hwnd, dummy)

    def _note_attached(self, found: tuple[int, tuple[int, int, int, int]] | None) -> None:
        """Log which ProPresenter window the pane sits on, when that changes (runs ten times a second)."""
        if found is None:
            key: tuple = (0,)
            message = "no ProPresenter window to stack above"
        else:
            pp, (left, top, right, bottom) = found
            key = (pp, right - left, bottom - top)
            message = f"stacked above ProPresenter window 0x{pp:X}, {right - left}x{bottom - top} at {left},{top}"
        if key != self._attached:
            self._attached = key
            self._events.event("attach", "pane", message)

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

    def _menu_actions(self) -> list:
        """The pane's right-click menu, as (label, action) pairs; None is a separator."""
        if self._collapsed:
            first = ("Expand", lambda: self.set_collapsed(False))
        else:
            first = ("Collapse", lambda: self.set_collapsed(True))
        return [first, ("Settings…", self._open_settings), None, ("Quit", QApplication.quit)]

    def _tray_actions(self) -> list:
        return [("Show pane", self.show_pane_on_top), None] + self._menu_actions()

    @staticmethod
    def _fill_menu(menu: QMenu, actions: list) -> QMenu:
        menu.clear()
        for item in actions:
            if item is None:
                menu.addSeparator()
            else:
                menu.addAction(item[0], item[1])
        return menu

    def _menu(self) -> QMenu:
        return self._fill_menu(QMenu(self), self._menu_actions())

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip("Media Background Pane")
        self._tray_menu = QMenu(self)
        # Rebuilt each time it opens, so Expand/Collapse matches the pane.
        self._tray_menu.aboutToShow.connect(lambda: self._fill_menu(self._tray_menu, self._tray_actions()))
        self._fill_menu(self._tray_menu, self._tray_actions())
        self.tray.setContextMenu(self._tray_menu)
        self.tray.activated.connect(self._on_tray)
        self.tray.show()

    def show_pane_on_top(self) -> None:
        """Tray "Show pane": bring the pane back above ProPresenter, whatever the workspace check says.

        It stays up until ProPresenter closes or restarts, or the check agrees
        the workspace is open, so the check does not hide it again a second later.
        """
        show, why = self._workspace_decision(self._pp_pids or ())
        self._shown_by_hand = True
        self._shown_for_workspace = True
        self._detach_hidden_owner()
        self.set_collapsed(False)
        restore_window_no_activate(self._native_hwnd())
        self._show_if_windows_hid_it()
        self._bring_on_screen()
        self._hide_from_taskbar()
        self._snap_to_propresenter()
        self._stack_above_propresenter()
        self._sync_gui_enabled()
        check = "it agrees" if show is True else "it says: " + why
        log_event("pane", f"shown from the tray menu, whatever the workspace check says ({check})")

    def _bring_on_screen(self) -> None:
        """Move the pane back onto a screen if it is off all of them."""
        frame = self.frameGeometry()
        if any(screen.availableGeometry().intersects(frame) for screen in QGuiApplication.screens()):
            return
        screen = QGuiApplication.primaryScreen().availableGeometry()
        x = screen.x() + max(0, (screen.width() - frame.width()) // 2)
        y = screen.y() + max(0, screen.height() - frame.height() - 48)
        log_event("pane", f"was off every screen at {frame.x()},{frame.y()}; moved to {x},{y}")
        self.move(x, y)

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

    def refresh_library(self, extra: list[Path] | None = None) -> None:
        """Scan the media folder again, on the library thread; the strip and the iPad follow when it is back.

        The folder is on Google Drive. On the GUI thread a scan took 60 ms,
        up to 200 ms (5 Nov 2026, 28 files), and it runs whenever the folder
        or ProPresenter's settings folder changes. A refresh asked for during
        a scan runs once that scan is back. extra: files just copied in,
        shown even if the folder listing does not have them yet.
        """
        if extra:
            self._library_extra.extend(extra)
        folder = Path(self.config.media_folder) if self.config.media_folder else None
        recursive = bool(self.config.include_subfolders)
        wanted = list(self._library_extra)
        if self._library_calls.submit("scan", lambda: scan_library(folder, recursive, wanted), self._show_library):
            self._library_extra = []
            self._library_rescan = False
        else:
            self._library_rescan = True

    def _show_library(self, scan: LibraryScan | None) -> None:
        """Show a finished scan. None: it failed (the error is in the session log), so keep what is shown."""
        if scan is not None:
            for path in self._folder_watcher.directories():
                if path != str(PATH_SETTINGS.parent):
                    self._folder_watcher.removePath(path)
            if scan.folder_ok:
                self._folder_watcher.addPath(str(scan.folder))
                self.thumbs.set_empty_hint("Drop images or videos here")
            else:
                self.thumbs.set_empty_hint("Set a media folder in Settings, or drop files here")
            self._files = scan.files
            self.thumbs.set_files(scan.files)
            self._lan_files = [
                {
                    "path": str(path),
                    "name": path.name,
                    "thumb": f"/thumbs/{name}",
                }
                for path, name in zip(scan.files, scan.thumb_names)
            ]
            self._sync_thumb_roles()
            for path in scan.files:
                image = scan.thumbs.get(str(path))
                if image is not None:
                    self.thumbs.set_thumb(str(path), image)
                elif path.suffix.lower() not in _STILL_SUFFIXES:
                    self.thumbnailer.request(path)  # a video with no thumbnail yet
        if self._library_rescan:
            self.refresh_library()

    def _import_dropped_files(self, paths: list[str]) -> None:
        folder = Path(self.config.media_folder) if self.config.media_folder else None
        if folder is None or not folder.is_dir():
            QMessageBox.information(
                self,
                "Media folder",
                "Set a media folder in Settings first, then drop files onto the library.",
            )
            self._open_settings()
            return
        sources = dropped_media_paths([Path(path) for path in paths])
        if not sources:
            QMessageBox.information(
                self,
                "Drop files",
                "Drop images or videos the pane can play (jpg, png, mp4, mov, and similar).",
            )
            return
        # Copying a clip onto Google Drive can take many seconds: on the
        # library thread, with the wait cursor up until it is done.
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        if not self._library_calls.submit(
            "copy",
            lambda: copy_into_folder(sources, folder),
            self._dropped_files_copied,
            single=False,
            failed=([], [source.name for source in sources]),
        ):
            QApplication.restoreOverrideCursor()

    def _dropped_files_copied(self, result: tuple[list[Path], list[str]]) -> None:
        QApplication.restoreOverrideCursor()
        copied, errors = result
        self._ignore_folder_scan_until = monotonic() + 1.0
        self._scan_timer.stop()
        self.refresh_library(extra=copied)
        if errors:
            names = ", ".join(errors[:8])
            extra = "" if len(errors) <= 8 else f" and {len(errors) - 8} more"
            QMessageBox.warning(
                self,
                "Couldn’t copy some files",
                f"These files weren’t added: {names}{extra}",
            )

    def _on_watched_path(self, path: str = "") -> None:
        if "PathSettings" in path or Path(path) == PATH_SETTINGS.parent:
            self._check_workspace()
            if PATH_SETTINGS.is_file() and str(PATH_SETTINGS) not in self._folder_watcher.files():
                self._folder_watcher.addPath(str(PATH_SETTINGS))
        self._schedule_scan()

    def _schedule_scan(self, _path: str = "") -> None:
        if monotonic() < self._ignore_folder_scan_until:
            return
        self._scan_timer.start()

    # Video Input. Every request to ProPresenter runs on the ProPresenter
    # thread (self._pp_calls), one at a time, in the order asked; the answers
    # are applied here on the GUI thread. An answer to a request sent before
    # self._video_input_serial moved on is out of date, and is dropped.

    def _try_trigger_video_input(self) -> None:
        """Once per ProPresenter run: switch its first video input on (retried every 2 s).

        An attempt still waiting or running is not started again, and none
        starts while the operator's own switch is on its way.
        """
        if self._video_input_triggered:
            self._video_input_timer.stop()
            return
        if self._pp_calls.busy("toggle"):
            return
        serial = self._video_input_serial

        def still_wanted() -> bool:
            # Read on the ProPresenter thread. False once the operator has
            # switched the input, or ProPresenter has restarted, since.
            return self._video_input_serial == serial

        def attempt() -> str | None:
            if not still_wanted():
                return None
            if video_input_layer_active() is True:
                return "already on"
            if not still_wanted():
                return None
            return "switched on" if trigger_first_video_input() else None

        self._pp_calls.submit("attempt", attempt, lambda outcome: self._video_input_attempted(outcome, serial))

    def _video_input_attempted(self, outcome: str | None, serial: int) -> None:
        if outcome is None or serial != self._video_input_serial or self._video_input_triggered:
            return
        self._video_input_triggered = True
        self._video_input_timer.stop()
        self._video_input_serial += 1  # a status asked for before this is out of date
        self._video_input_known = True
        self._set_video_input_button(True)
        if outcome == "already on":
            log_event("video input", "already on in ProPresenter")
        else:
            log_event("video input", "switched ProPresenter's first video input on")

    def _rearm_video_input(self) -> None:
        """A new ProPresenter run: switch its video input on again, once, as at start-up.

        Within a run, once it is on (or the operator has switched it off), it
        is left alone.
        """
        self._video_input_triggered = False
        self._video_input_ignore_until = 0.0
        self._video_input_serial += 1  # answers about the run that ended no longer count
        if not self._video_input_timer.isActive():
            self._video_input_timer.start()
        log_event("video input", "new ProPresenter run: will switch its first video input on")

    def _sync_video_input_status(self) -> None:
        """Once a second: the button follows ProPresenter. Skipped while the last request is still out."""
        if monotonic() < self._video_input_ignore_until:
            return
        if self._pp_calls.busy("toggle"):
            return
        serial = self._video_input_serial
        self._pp_calls.submit(
            "status",
            lambda: video_input_layer_active(),
            lambda active: self._video_input_status(active, serial),
        )

    def _video_input_status(self, active: bool | None, serial: int) -> None:
        if active is None or serial != self._video_input_serial:
            return
        if monotonic() < self._video_input_ignore_until:
            return
        self._video_input_known = active
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
        """The operator's click on Video Input (or the iPad's): switch it on or off.

        The button shows the choice at once. The request goes after any
        already on the ProPresenter thread; if ProPresenter does not take it,
        the button goes back to what ProPresenter last said.
        """
        want_on = self.video_input_button.isChecked()
        duration = self.config.fade_seconds
        self._video_input_serial += 1  # earlier status answers and attempts no longer count
        serial = self._video_input_serial
        self._set_video_input_button(want_on)
        if want_on:
            call = lambda: trigger_first_video_input(duration)  # noqa: E731
        else:
            # The operator's choice holds for this ProPresenter run: do not
            # switch the input back on, even if the start-up attempt is still retrying.
            self._video_input_triggered = True
            self._video_input_timer.stop()
            call = lambda: clear_video_input_layer(duration)  # noqa: E731
        self._pp_calls.submit(
            "toggle",
            call,
            lambda ok: self._video_input_toggled(want_on, bool(ok), serial, duration),
            single=False,
            failed=False,
        )

    def _video_input_toggled(self, want_on: bool, ok: bool, serial: int, duration: float) -> None:
        latest = serial == self._video_input_serial  # no later click, and the same ProPresenter run
        if ok:
            self._video_input_known = want_on
            log_event("video input", "switched on by the operator" if want_on else "switched off by the operator")
            if latest:
                if want_on:
                    self._video_input_triggered = True
                    self._video_input_timer.stop()
                self._video_input_ignore_until = monotonic() + max(1.5, duration + 0.3)
        if latest:
            self._set_video_input_button(self._video_input_known)

    def _on_thumb_scale(self, width: int) -> None:
        self.config.thumb_scale = width

    def _on_scale_mode(self, mode: str) -> None:
        self.engine.set_scale_mode(mode)
        self.config.save()

    def _on_thumb(self, path: str) -> None:
        self._fade_after_black = False
        self.thumbs.set_selected(path)
        self._thumb_pick = path
        if self.mix_slider.slider.value() <= 1:
            thumb = self.thumbs.thumb_image(path)
            if thumb is not None:
                self.preview_monitor.set_image(thumb)
        QTimer.singleShot(0, lambda picked=path: self._on_thumb_now(picked))

    def _on_thumb_now(self, path: str) -> None:
        if self._thumb_pick != path:
            return
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
        if self.engine.dimmer <= 0.001:
            # A take here would swap the new clip off Preview before it can be seen.
            # Commit it once DIM leaves black, so fade-up still reveals this clip.
            self._fade_after_black = True
            return
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
        self._fade_after_black = False
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
        self._apply_dim_value(value, allow_pause=not chasing)

    def _on_dim_chase_finished(self) -> None:
        self._dim_animating = False
        self._apply_dim_value(self.dim_slider.slider.value(), allow_pause=True)

    def _apply_dim_value(self, value: int, allow_pause: bool) -> None:
        if (
            self._fade_after_black
            and value > 1
            and self.engine.dimmer <= 0.001
            and self.engine.preview.has_source
        ):
            self._fade_after_black = False
            self.engine.take()
        was_black = self.engine.dimmer <= 0.001
        self.engine.set_dimmer(value / 1000.0, allow_pause=allow_pause)
        self._update_black_button()
        if was_black and self.engine.dimmer > 0.001:
            self._sync_thumb_roles()

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
        self._fade_after_black = False
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
        if self._fade_after_black and self.engine.dimmer <= 0.001 and self.engine.preview.has_source:
            self._fade_after_black = False
            self.engine.take()
        else:
            self._fade_after_black = False
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

    def _read_propresenter(self) -> tuple[int, ...]:
        """The running ProPresenter process ids. Logs starts and stops.

        A new run gets its video input switched on again, and ends a tray
        "Show pane", as does ProPresenter closing.
        """
        pids = tuple(sorted(propresenter_pids()))
        previous = self._pp_pids
        self._pp_pids = pids
        if previous is None:
            log_event("propresenter", f"running: {_pid_list(pids)}" if pids else "not running")
            return pids
        if pids == previous:
            return pids
        stopped = [pid for pid in previous if pid not in pids]
        started = [pid for pid in pids if pid not in previous]
        if stopped:
            self._events.event("propresenter stop", "propresenter", f"stopped: {_pid_list(stopped)}")
        if started:
            self._events.event("propresenter start", "propresenter", f"started: {_pid_list(started)}")
        if self._shown_by_hand:
            self._shown_by_hand = False
            log_event("pane", "tray \"Show pane\" ends: ProPresenter closed or restarted")
        if started:
            self._rearm_video_input()
        return pids

    def _workspace_decision(self, pids: tuple[int, ...]) -> tuple[bool | None, str]:
        """Whether the pane should be up, and why. None: not known yet, leave it as it is."""
        if not self.config.auto_show_on_workspace:
            if pids:
                return True, f"ProPresenter is running ({_pid_list(pids)})"
            return False, "ProPresenter is not running"
        reading = open_workspace(list(pids))
        if reading.source == "not running":
            return False, reading.detail
        if reading.source == "unknown":
            return None, reading.detail
        wanted = self.config.workspace_name
        how = "from its log" if reading.source == "log" else "from PathSettings Base=, as the log had no answer"
        if reading.is_named(wanted):
            return True, f"{reading.detail} ({how})"
        return False, f"{reading.detail}, not {wanted} ({how})"

    def _note_decision(self, show: bool | None, why: str) -> None:
        decision = (show, why, self._shown_by_hand)
        if decision == self._last_decision:
            return
        self._last_decision = decision
        verdict = {True: "show", False: "hide", None: "no change"}[show]
        if show is False and self._shown_by_hand:
            verdict = 'hide, but the tray "Show pane" keeps it up'
        self._events.event("decision", "workspace check", f"{verdict}: {why}")

    def _show_if_windows_hid_it(self) -> None:
        hwnd = self._native_hwnd()
        if not hwnd or native_window_visible(hwnd):
            return
        # Qt still believing the window is up means Windows hid it, not the pane.
        windows_did_it = self.isVisible()
        owner = window_owner(hwnd)
        # Qt ignores show() while it still believes the window is visible.
        if self.isVisible():
            self.hide()
        self.show()
        show_window_no_activate(self._native_hwnd())
        if windows_did_it:
            if owner:
                whose = "visible" if native_window_visible(owner) else "hidden"
                detail = f"its owner window 0x{owner:X} is {whose}"
            else:
                detail = "it has no owner window"
            self._events.event("reshow", "pane", f"Windows had hidden the pane; showed it again ({detail})")

    def _note_window_state(self) -> None:
        """Log what the pane's own window looks like to Windows, when that changes.

        If the pane vanishes without the workspace check hiding it, this says
        how: hidden, minimised, cloaked (kept off screen by Windows), or moved.
        """
        hwnd = self._native_hwnd()
        if not hwnd:
            return
        rect = native_window_rect(hwnd)
        state = (
            self.isVisible(),
            native_window_visible(hwnd),
            window_is_minimized(hwnd),
            window_is_cloaked(hwnd),
            rect,
            window_owner(hwnd),
        )
        if state == self._window_state:
            return
        self._window_state = state
        wanted, shown, minimised, cloaked, rect, owner = state
        if not wanted:
            look = "hidden by the pane"
        elif minimised:
            look = "minimised"
        elif cloaked:
            look = "cloaked: Windows keeps it off screen"
        elif not shown:
            look = "hidden by Windows"
        else:
            look = "on screen"
        where = f"{rect[2] - rect[0]}x{rect[3] - rect[1]} at {rect[0]},{rect[1]}" if rect else "no position"
        whose = f"owner 0x{owner:X}" if owner else "no owner"
        self._events.event("window", "pane window", f"{look}; {where}; {whose}")

    def _check_workspace(self) -> None:
        # Its process snapshot cost 7 ms of the GUI thread a second.
        self._pp_calls.submit("priority", lambda: prefer_livestream_apps())
        pids = self._read_propresenter()
        show, why = self._workspace_decision(pids)
        if show is True and self._shown_by_hand:
            self._shown_by_hand = False
            log_event("pane", "tray \"Show pane\" ends: the workspace check agrees")
        self._note_decision(show, why)
        if show is True and not self._pane_is_on_screen():
            opening = not self._shown_for_workspace
            self._shown_for_workspace = True
            self._detach_hidden_owner()
            if opening:
                self.set_collapsed(False)
            self._show_if_windows_hid_it()
            self._hide_from_taskbar()
            self._snap_to_propresenter()
            self._stack_above_propresenter()
            self._sync_gui_enabled()
            self._events.event("show", "pane", "shown" if opening else "put back on screen")
        elif show is False and (self._shown_for_workspace or self.isVisible()) and not self._shown_by_hand:
            self._shown_for_workspace = False
            self._detach_hidden_owner()
            self.hide()
            self._sync_gui_enabled()
            self._events.event("hide", "pane", "hidden")
        self._note_window_state()
        self._events.flush()

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
        if self.mix_slider.slider.is_dragging() or self.dim_slider.slider.is_dragging():
            return
        self._lan_hub.set_jpeg("preview", _qimage_jpeg(image))

    def _snapshot_program(self, image: QImage) -> None:
        if self._lan_server is None:
            return
        if self.mix_slider.slider.is_dragging() or self.dim_slider.slider.is_dragging():
            return
        self._lan_hub.set_jpeg("program", _qimage_jpeg(image))

    def _publish_lan_state(self) -> None:
        if self._lan_server is None:
            return
        preview = str(self.engine.preview.path) if self.engine.preview.path else ""
        program = str(self.engine.program.path) if self.engine.program.path else ""
        self._lan_hub.publish_state(
            {
                "mix": self.mix_slider.visual() / 1000.0,
                "dim": self.dim_slider.visual() / 1000.0,
                "mix_target": self.mix_slider.ghost() / 1000.0,
                "dim_target": self.dim_slider.ghost() / 1000.0,
                # How fast each knob is moving, so the iPad can keep its knob
                # moving between reports instead of guessing.
                "mix_speed": self.mix_slider.speed() / 1000.0,
                "dim_speed": self.dim_slider.speed() / 1000.0,
                "duration": self.config.fade_seconds,
                "auto_fade": self.auto_fade.isChecked(),
                "video_input": self.video_input_button.isChecked(),
                "preview": preview,
                "program": program,
                "fade_enabled": self.engine.preview.has_source,
                "files": self._lan_files,
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
