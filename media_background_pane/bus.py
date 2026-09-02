from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QObject, QRectF, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtMultimedia import QMediaPlayer, QVideoFrame, QVideoSink

from .config import GUI_FPS

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".wmv"}


def is_image(path: Path) -> bool:
    return path.suffix.lower() in IMAGE_EXT


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXT


def is_media(path: Path) -> bool:
    return is_image(path) or is_video(path)


def black_frame(width: int, height: int) -> QImage:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(0, 0, 0))
    return image


def fit_rgb(source: QImage, width: int, height: int) -> QImage:
    if source.isNull():
        return black_frame(width, height)
    if source.width() == width and source.height() == height:
        if source.format() == QImage.Format.Format_RGB32:
            return source
        return source.convertToFormat(QImage.Format.Format_RGB32)
    canvas = black_frame(width, height)
    scaled = source.scaled(
        width,
        height,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.FastTransformation,
    )
    if scaled.format() != QImage.Format.Format_RGB32:
        scaled = scaled.convertToFormat(QImage.Format.Format_RGB32)
    painter = QPainter(canvas)
    painter.drawImage((width - scaled.width()) // 2, (height - scaled.height()) // 2, scaled)
    painter.end()
    return canvas


def video_frame_to_rgb(frame: QVideoFrame, width: int, height: int) -> QImage:
    image = frame.toImage()
    if image.isNull():
        canvas = black_frame(width, height)
        fw, fh = frame.width(), frame.height()
        if fw <= 0 or fh <= 0:
            return canvas
        scale = min(width / fw, height / fh)
        dw = max(1, int(fw * scale))
        dh = max(1, int(fh * scale))
        painter = QPainter(canvas)
        frame.paint(painter, QRectF((width - dw) / 2, (height - dh) / 2, dw, dh))
        painter.end()
        return canvas
    return fit_rgb(image, width, height)


class Bus(QObject):
    frame_ready = Signal()

    def __init__(self, name: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.name = name
        self.path: Path | None = None
        self.process_w = 1920
        self.process_h = 1080
        self.frame: QImage = black_frame(self.process_w, self.process_h)
        self._player: QMediaPlayer | None = None
        self._sink: QVideoSink | None = None
        self._still = True
        self._output_fps = 24
        self._full_frames = name == "program"
        self._convert_enabled = True
        self._skip_frames = 0
        self._min_dt = 1 / 24
        self._last_convert = 0.0
        self._paused = False
        self._pause_after_first = False
        self._apply_min_dt()

    @property
    def has_source(self) -> bool:
        return self.path is not None

    @property
    def is_video(self) -> bool:
        return bool(self.path and is_video(self.path))

    @property
    def capturing_first_frame(self) -> bool:
        return self._pause_after_first

    def set_process_size(self, width: int, height: int) -> None:
        self.process_w = width
        self.process_h = height
        if self._still and self.path and is_image(self.path):
            self.frame = fit_rgb(QImage(str(self.path)), width, height)
        elif not self.has_source:
            self.frame = black_frame(width, height)

    def set_fps(self, fps: int) -> None:
        self._output_fps = max(1, fps)
        self._apply_min_dt()

    def set_full_frames(self, full: bool) -> None:
        if self._full_frames == full:
            return
        self._full_frames = full
        self._apply_min_dt()

    def set_convert_enabled(self, enabled: bool) -> None:
        self._convert_enabled = enabled

    def set_paused(self, paused: bool) -> None:
        if self._paused == paused:
            return
        self._paused = paused
        if self._player is None or self._still:
            return
        if paused:
            self._player.pause()
        else:
            self._player.play()

    def _apply_min_dt(self) -> None:
        fps = self._output_fps if self._full_frames else GUI_FPS
        self._min_dt = 1 / max(1, fps)

    def load(self, path: Path, pause_after_first_frame: bool = False) -> None:
        self.clear(keep_frame=False)
        if not path.is_file():
            return
        self.path = path
        self._pause_after_first = False
        self._paused = False
        self._skip_frames = 0
        self._convert_enabled = True
        self._last_convert = 0.0
        if is_image(path):
            self.frame = fit_rgb(QImage(str(path)), self.process_w, self.process_h)
            self._still = True
            self.frame_ready.emit()
            return
        self._still = False
        self._pause_after_first = pause_after_first_frame
        self._player = QMediaPlayer(self)
        self._sink = QVideoSink(self)
        self._player.setLoops(QMediaPlayer.Loops.Infinite)
        self._sink.videoFrameChanged.connect(self._on_video_frame)
        self._player.setVideoSink(self._sink)
        self._player.setSource(QUrl.fromLocalFile(str(path)))
        self._player.errorOccurred.connect(self._on_player_error)
        self._player.play()

    def _on_player_error(self, error, message: str) -> None:
        if self._player is None:
            return
        # Hardware decode can fail silently on some clips; retry playback once.
        self._player.play()

    def clear(self, keep_frame: bool = False) -> None:
        if self._sink is not None:
            try:
                self._sink.videoFrameChanged.disconnect(self._on_video_frame)
            except (RuntimeError, TypeError):
                pass
        if self._player is not None:
            self._player.stop()
            self._player.setVideoSink(None)
            self._player.deleteLater()
            self._player = None
        if self._sink is not None:
            self._sink.deleteLater()
            self._sink = None
        self.path = None
        self._still = True
        self._paused = False
        self._pause_after_first = False
        if not keep_frame:
            self.frame = black_frame(self.process_w, self.process_h)
            self.frame_ready.emit()

    def adopt(self, other: Bus) -> None:
        if other._sink is not None:
            try:
                other._sink.videoFrameChanged.disconnect(other._on_video_frame)
            except (RuntimeError, TypeError):
                pass
        self.clear(keep_frame=True)
        self.path = other.path
        self.frame = other.frame.copy() if not other.frame.isNull() else black_frame(self.process_w, self.process_h)
        self._still = other._still
        self._player = other._player
        self._sink = other._sink
        other._player = None
        other._sink = None
        other.path = None
        other._still = True
        other.frame = black_frame(other.process_w, other.process_h)
        self._full_frames = True
        self._convert_enabled = True
        self._apply_min_dt()
        # Keep the last mixed picture. A sink reconnect can emit a black/glitch
        # frame, so ignore the next couple of callbacks.
        self._skip_frames = 3
        if self._sink is None:
            self._sink = QVideoSink(self)
        try:
            self._sink.videoFrameChanged.connect(self._on_video_frame)
        except (RuntimeError, TypeError):
            pass
        if self._player is not None:
            self._player.setParent(self)
        if self._sink is not None:
            self._sink.setParent(self)
            if self._player is not None and self._player.videoSink() is not self._sink:
                self._player.setVideoSink(self._sink)
        if self._player is not None:
            self._player.play()

    def swap(self, other: Bus) -> None:
        for bus in (self, other):
            if bus._sink is not None:
                try:
                    bus._sink.videoFrameChanged.disconnect(bus._on_video_frame)
                except (RuntimeError, TypeError):
                    pass
        self.path, other.path = other.path, self.path
        self.frame, other.frame = other.frame, self.frame
        self._still, other._still = other._still, self._still
        self._player, other._player = other._player, self._player
        self._sink, other._sink = other._sink, self._sink
        self._paused, other._paused = other._paused, self._paused
        self._full_frames = True
        other._full_frames = False
        self._convert_enabled = True
        other._convert_enabled = True
        self._apply_min_dt()
        other._apply_min_dt()
        self._skip_frames = 2
        other._skip_frames = 2

        def attach(bus: Bus) -> None:
            if bus._sink is None:
                bus._sink = QVideoSink(bus)
            try:
                bus._sink.videoFrameChanged.connect(bus._on_video_frame)
            except (RuntimeError, TypeError):
                pass
            if bus._player is not None:
                bus._player.setParent(bus)
            if bus._sink is not None:
                bus._sink.setParent(bus)
                if bus._player is not None and bus._player.videoSink() is not bus._sink:
                    bus._player.setVideoSink(bus._sink)
            if bus._player is not None and not bus._paused:
                bus._player.play()

        attach(self)
        attach(other)

    @Slot(QVideoFrame)
    def _on_video_frame(self, video_frame: QVideoFrame) -> None:
        if not video_frame.isValid():
            return
        if self._paused and not self._pause_after_first:
            return
        if self._skip_frames > 0:
            self._skip_frames -= 1
            return
        if not self._convert_enabled and not self._full_frames and not self._pause_after_first:
            return
        now = time.perf_counter()
        if not self._pause_after_first and now - self._last_convert < self._min_dt:
            return
        self._last_convert = now
        self.frame = video_frame_to_rgb(video_frame, self.process_w, self.process_h)
        self.frame_ready.emit()
        if self._pause_after_first:
            self._pause_after_first = False
            self.set_paused(True)
