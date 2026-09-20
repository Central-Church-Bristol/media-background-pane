from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, QRectF, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtMultimedia import QMediaPlayer, QVideoFrame, QVideoSink

from .config import GUI_FPS
from .hw_decoder import HwDecoder, find_ffmpeg
from .ndi_output import qimage_to_array

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


def idle_decoder_status(use_gpu: bool) -> str:
    if find_ffmpeg() is None:
        return "Qt fallback (ffmpeg not found)"
    if use_gpu:
        return "GPU (D3D11VA)"
    return "Software fallback"


class Bus(QObject):
    frame_ready = Signal()
    decoder_status_changed = Signal(str)

    def __init__(self, name: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.name = name
        self.path: Path | None = None
        self.process_w = 1920
        self.process_h = 1080
        self.frame: QImage = black_frame(self.process_w, self.process_h)
        self.frame_np = np.zeros((self.process_h, self.process_w, 4), dtype=np.uint8)
        self.frame_np[..., 3] = 255
        self.decoder_status = idle_decoder_status(True)
        self._frame_lock = threading.Lock()
        self._player: QMediaPlayer | None = None
        self._sink: QVideoSink | None = None
        self._decoder: HwDecoder | None = None
        self._still = True
        self._output_fps = 24
        self._full_frames = name == "program"
        self._convert_enabled = True
        self._skip_frames = 0
        self._min_dt = 1 / 24
        self._last_convert = 0.0
        self._paused = False
        self._pause_after_first = False
        self._gpu_decode = True
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

    def copy_array(self, dest: np.ndarray) -> None:
        with self._frame_lock:
            src = self.frame_np
            if src.shape != dest.shape:
                dest.fill(0)
                dest[..., 3] = 255
                h = min(src.shape[0], dest.shape[0])
                w = min(src.shape[1], dest.shape[1])
                dest[:h, :w] = src[:h, :w]
                return
            np.copyto(dest, src)

    def take_frame(self, dest: np.ndarray, timeout: float = 0.0) -> bool:
        if self._decoder is not None and not self._still:
            if self._decoder.pop_frame(dest, timeout):
                dest[..., 3] = 255
                with self._frame_lock:
                    if self.frame_np.shape == dest.shape:
                        np.copyto(self.frame_np, dest)
                return True
            self.copy_array(dest)
            return False
        self.copy_array(dest)
        return False

    def _alloc_frame(self) -> None:
        self.frame_np = np.zeros((self.process_h, self.process_w, 4), dtype=np.uint8)
        self.frame_np[..., 3] = 255
        self.frame = black_frame(self.process_w, self.process_h)

    def _publish_image(self, image: QImage) -> None:
        fitted = image
        if image.width() != self.process_w or image.height() != self.process_h:
            fitted = fit_rgb(image, self.process_w, self.process_h)
        elif fitted.format() != QImage.Format.Format_RGB32:
            fitted = fitted.convertToFormat(QImage.Format.Format_RGB32)
        with self._frame_lock:
            if self.frame_np.shape[0] != self.process_h or self.frame_np.shape[1] != self.process_w:
                self._alloc_frame()
            qimage_to_array(fitted, self.frame_np)
            self.frame_np[..., 3] = 255
            self.frame = fitted

    def _publish_decoder_frame(self) -> bool:
        if self._decoder is None:
            return False
        with self._frame_lock:
            if not self._decoder.copy_frame(self.frame_np):
                return False
            self.frame = QImage(
                self.frame_np.data,
                self.process_w,
                self.process_h,
                int(self.frame_np.strides[0]),
                QImage.Format.Format_RGB32,
            )
        return True

    def _set_decoder_status(self, status: str) -> None:
        if self.decoder_status == status:
            return
        self.decoder_status = status
        self.decoder_status_changed.emit(status)

    def set_gpu_decode(self, enabled: bool) -> None:
        if self._gpu_decode == enabled:
            return
        self._gpu_decode = enabled
        if self.path is not None and self.is_video:
            path = self.path
            pause_after = self._paused
            self.load(path, pause_after_first_frame=pause_after)
        elif not self.is_video:
            self._set_decoder_status(idle_decoder_status(enabled))

    def set_process_size(self, width: int, height: int) -> None:
        if width == self.process_w and height == self.process_h:
            return
        self.process_w = width
        self.process_h = height
        with self._frame_lock:
            self._alloc_frame()
        if self._still and self.path and is_image(self.path):
            self._publish_image(fit_rgb(QImage(str(self.path)), width, height))
            self.frame_ready.emit()
        elif self._decoder is not None:
            self._decoder.restart(width, height)
        elif not self.has_source:
            self.frame_ready.emit()

    def set_fps(self, fps: int) -> None:
        self._output_fps = max(1, fps)
        self._apply_min_dt()
        if self._decoder is not None:
            self._decoder.restart(fps=self._output_fps)

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
        if self._still:
            return
        if self._decoder is not None:
            self._decoder.set_paused(paused)
            return
        if self._player is None:
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
            self._publish_image(fit_rgb(QImage(str(path)), self.process_w, self.process_h))
            self._still = True
            self._set_decoder_status(idle_decoder_status(self._gpu_decode))
            self.frame_ready.emit()
            return
        self._still = False
        self._pause_after_first = pause_after_first_frame
        if find_ffmpeg() is not None:
            self._start_hw_decoder(path)
            return
        self._set_decoder_status("Qt fallback")
        self._player = QMediaPlayer(self)
        self._sink = QVideoSink(self)
        self._player.setLoops(QMediaPlayer.Loops.Infinite)
        self._sink.videoFrameChanged.connect(self._on_video_frame)
        self._player.setVideoSink(self._sink)
        self._player.setSource(QUrl.fromLocalFile(str(path)))
        self._player.errorOccurred.connect(self._on_player_error)
        self._player.play()

    def _start_hw_decoder(self, path: Path) -> None:
        self._decoder = HwDecoder(self.name, parent=self)
        self._decoder.frame_ready.connect(self._on_hw_frame)
        self._decoder.status_changed.connect(self._set_decoder_status)
        self._set_decoder_status("starting")
        self._decoder.start(
            path,
            self.process_w,
            self.process_h,
            self._output_fps,
            self._gpu_decode,
            paused=False,
        )

    def _on_player_error(self, error, message: str) -> None:
        if self._player is None:
            return
        self._player.play()

    def _stop_qt_player(self) -> None:
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

    def _stop_decoder(self) -> None:
        if self._decoder is None:
            return
        try:
            self._decoder.frame_ready.disconnect(self._on_hw_frame)
        except (RuntimeError, TypeError):
            pass
        try:
            self._decoder.status_changed.disconnect(self._set_decoder_status)
        except (RuntimeError, TypeError):
            pass
        self._decoder.stop()
        self._decoder.deleteLater()
        self._decoder = None

    def clear(self, keep_frame: bool = False) -> None:
        self._stop_qt_player()
        self._stop_decoder()
        self.path = None
        self._still = True
        self._paused = False
        self._pause_after_first = False
        self._set_decoder_status(idle_decoder_status(self._gpu_decode))
        if not keep_frame:
            with self._frame_lock:
                self._alloc_frame()
            self.frame_ready.emit()

    def _attach_qt(self) -> None:
        if self._decoder is not None:
            return
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
        if self._player is not None and not self._paused:
            self._player.play()

    def _attach_decoder(self) -> None:
        if self._decoder is None:
            return
        self._decoder.setParent(self)
        try:
            self._decoder.frame_ready.connect(self._on_hw_frame)
        except (RuntimeError, TypeError):
            pass
        try:
            self._decoder.status_changed.connect(self._set_decoder_status)
        except (RuntimeError, TypeError):
            pass
        self._decoder.set_paused(self._paused)
        self._decoder.discard_queued()
        self._set_decoder_status(self._decoder.status)

    def adopt(self, other: Bus) -> None:
        self._detach_sources()
        other._detach_sources()
        self.clear(keep_frame=True)
        self.path = other.path
        with self._frame_lock:
            other.copy_array(self.frame_np)
            self.frame = other.frame.copy() if not other.frame.isNull() else black_frame(self.process_w, self.process_h)
        self._still = other._still
        self._player = other._player
        self._sink = other._sink
        self._decoder = other._decoder
        self.decoder_status = other.decoder_status
        other._player = None
        other._sink = None
        other._decoder = None
        other.path = None
        other._still = True
        with other._frame_lock:
            other._alloc_frame()
        self._full_frames = True
        self._convert_enabled = True
        self._apply_min_dt()
        self._skip_frames = 3
        self._attach_qt()
        self._attach_decoder()
        if self._player is not None:
            self._player.play()
        if self._decoder is not None:
            self._decoder.set_paused(False)

    def _detach_sources(self) -> None:
        if self._sink is not None:
            try:
                self._sink.videoFrameChanged.disconnect(self._on_video_frame)
            except (RuntimeError, TypeError):
                pass
        if self._decoder is not None:
            try:
                self._decoder.frame_ready.disconnect(self._on_hw_frame)
            except (RuntimeError, TypeError):
                pass
            try:
                self._decoder.status_changed.disconnect(self._set_decoder_status)
            except (RuntimeError, TypeError):
                pass

    def swap(self, other: Bus) -> None:
        self._detach_sources()
        other._detach_sources()
        self.path, other.path = other.path, self.path
        self._still, other._still = other._still, self._still
        self._player, other._player = other._player, self._player
        self._sink, other._sink = other._sink, self._sink
        self._decoder, other._decoder = other._decoder, self._decoder
        self._paused, other._paused = other._paused, self._paused
        self.decoder_status, other.decoder_status = other.decoder_status, self.decoder_status
        with self._frame_lock, other._frame_lock:
            self.frame, other.frame = other.frame, self.frame
            self.frame_np, other.frame_np = other.frame_np, self.frame_np
        self._full_frames = True
        other._full_frames = False
        self._convert_enabled = True
        other._convert_enabled = True
        self._apply_min_dt()
        other._apply_min_dt()
        self._skip_frames = 2
        other._skip_frames = 2
        self._attach_qt()
        other._attach_qt()
        self._attach_decoder()
        other._attach_decoder()

    def _should_publish(self) -> bool:
        if self._paused and not self._pause_after_first:
            return False
        if self._skip_frames > 0:
            self._skip_frames -= 1
            return False
        if not self._convert_enabled and not self._full_frames and not self._pause_after_first:
            return False
        now = time.perf_counter()
        if not self._pause_after_first and now - self._last_convert < self._min_dt:
            return False
        self._last_convert = now
        return True

    def _after_publish(self) -> None:
        self.frame_ready.emit()
        if self._pause_after_first:
            self._pause_after_first = False
            self.set_paused(True)

    @Slot()
    def _on_hw_frame(self) -> None:
        if self._skip_frames > 0:
            self._skip_frames -= 1
            return
        if self._pause_after_first:
            if self._decoder is not None:
                with self._frame_lock:
                    self._decoder.copy_frame(self.frame_np)
            self._after_publish()
            return
        self.frame_ready.emit()

    @Slot(QVideoFrame)
    def _on_video_frame(self, video_frame: QVideoFrame) -> None:
        if not video_frame.isValid():
            return
        if not self._should_publish():
            return
        self._publish_image(video_frame_to_rgb(video_frame, self.process_w, self.process_h))
        self._after_publish()
