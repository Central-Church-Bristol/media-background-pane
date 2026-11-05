from __future__ import annotations

import threading
import time
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, QRectF, QSize, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QImage, QImageReader, QPainter
from PySide6.QtMultimedia import QMediaPlayer, QVideoFrame, QVideoSink

from .config import GUI_FPS, SCALE_ACTUAL, SCALE_FILL, SCALE_FIT, SCALE_MODES
from .frames import bgra_to_nv12, black, copy_into
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


def load_still(path: Path, width: int, height: int, mode: str) -> QImage:
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    if mode != SCALE_ACTUAL:
        size = reader.size()
        if size.isValid() and size.width() > 0 and size.height() > 0:
            scaled = QSize(size)
            aspect = (
                Qt.AspectRatioMode.KeepAspectRatio
                if mode == SCALE_FIT
                else Qt.AspectRatioMode.KeepAspectRatioByExpanding
            )
            scaled.scale(width, height, aspect)
            if 0 < scaled.width() < size.width() or 0 < scaled.height() < size.height():
                reader.setScaledSize(scaled)
    image = reader.read()
    if image.isNull():
        image = QImage(str(path))
    return image


def black_frame(width: int, height: int) -> QImage:
    image = QImage(width, height, QImage.Format.Format_RGB32)
    image.fill(QColor(0, 0, 0))
    return image


def place_rgb(source: QImage, width: int, height: int, mode: str = SCALE_FILL) -> QImage:
    if source.isNull():
        return black_frame(width, height)
    src = source
    if src.format() != QImage.Format.Format_RGB32:
        src = src.convertToFormat(QImage.Format.Format_RGB32)
    if mode == SCALE_ACTUAL:
        canvas = black_frame(width, height)
        painter = QPainter(canvas)
        painter.drawImage((width - src.width()) // 2, (height - src.height()) // 2, src)
        painter.end()
        return canvas
    if src.width() == width and src.height() == height:
        return src
    aspect = (
        Qt.AspectRatioMode.KeepAspectRatio
        if mode == SCALE_FIT
        else Qt.AspectRatioMode.KeepAspectRatioByExpanding
    )
    # Smooth: a still is scaled once, and nearest-pixel scaling showed jaggies.
    scaled = src.scaled(width, height, aspect, Qt.TransformationMode.SmoothTransformation)
    if scaled.format() != QImage.Format.Format_RGB32:
        scaled = scaled.convertToFormat(QImage.Format.Format_RGB32)
    if scaled.width() == width and scaled.height() == height:
        return scaled
    canvas = black_frame(width, height)
    painter = QPainter(canvas)
    painter.drawImage((width - scaled.width()) // 2, (height - scaled.height()) // 2, scaled)
    painter.end()
    return canvas


def fit_rgb(source: QImage, width: int, height: int) -> QImage:
    return place_rgb(source, width, height, SCALE_FIT)


def qimage_to_nv12(image: QImage, width: int, height: int) -> np.ndarray:
    """A width x height RGB32 QImage as a new NV12 frame."""
    bgra = np.zeros((height, width, 4), dtype=np.uint8)
    qimage_to_array(image, bgra)
    return bgra_to_nv12(bgra)


def video_frame_to_rgb(frame: QVideoFrame) -> QImage:
    image = frame.toImage()
    if image.isNull():
        fw, fh = frame.width(), frame.height()
        if fw <= 0 or fh <= 0:
            return QImage()
        canvas = black_frame(fw, fh)
        painter = QPainter(canvas)
        frame.paint(painter, QRectF(0, 0, fw, fh))
        painter.end()
        return canvas
    if image.format() != QImage.Format.Format_RGB32:
        return image.convertToFormat(QImage.Format.Format_RGB32)
    return image


def idle_decoder_status(use_gpu: bool) -> str:
    if find_ffmpeg() is None:
        return "Qt fallback (ffmpeg not found)"
    if use_gpu:
        return "GPU (D3D11VA)"
    return "Software fallback"


class Bus(QObject):
    """One of the two picture sources, Program or Preview.

    frame_np is the frame on show, as NV12. A video bus takes its frames from
    its decoder in order, one per compositor tick, so the picture never skips
    ahead and never repeats while the decoder keeps up.
    """

    frame_ready = Signal()
    decoder_status_changed = Signal(str)

    def __init__(self, name: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.name = name
        self.path: Path | None = None
        self.process_w = 1920
        self.process_h = 1080
        self.frame_np = black(self.process_w, self.process_h)
        self._content_serial = 0
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
        self._scale_mode = SCALE_FILL
        # What check_decoder last saw: which decoder, its frame count, and when that count last moved.
        self._watched: HwDecoder | None = None
        self._watched_frames = 0
        self._watched_since = 0.0
        self._apply_min_dt()

    @property
    def has_source(self) -> bool:
        return self.path is not None

    @property
    def is_video(self) -> bool:
        return bool(self.path and is_video(self.path))

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def capturing_first_frame(self) -> bool:
        return self._pause_after_first

    def playing_frames(self) -> bool:
        """True while this bus should hand on a new frame every tick."""
        decoder = self._decoder
        return (
            decoder is not None
            and not self._still
            and not self._paused
            and not self._pause_after_first
            and decoder.frames_read > 0
        )

    def current_serial(self) -> int:
        with self._frame_lock:
            return self._content_serial

    def copy_array(self, dest: np.ndarray) -> int:
        with self._frame_lock:
            copy_into(dest, self.frame_np)
            return self._content_serial

    def take_frame(self, dest: np.ndarray, timeout: float = 0.0) -> int:
        return self.take_new_frame(dest, timeout)[0]

    def take_new_frame(self, dest: np.ndarray, timeout: float = 0.0) -> tuple[int, bool]:
        """Put the next frame on show and copy it into dest. (serial, was it new?)

        A paused bus, a still, or a decoder that has nothing ready yet gives
        the frame already on show. Frames are never skipped: the queue holds
        them in order until they are taken.
        """
        decoder = self._decoder
        if decoder is not None and not self._still and not self._paused and not self._pause_after_first:
            if decoder.pop_frame(dest, timeout):
                with self._frame_lock:
                    if self.frame_np.shape == dest.shape:
                        np.copyto(self.frame_np, dest)
                    else:
                        copy_into(self.frame_np, dest)
                    self._content_serial += 1
                    return self._content_serial, True
        return self.copy_array(dest), False

    def _alloc_frame(self) -> None:
        self.frame_np = black(self.process_w, self.process_h)

    def _publish_image(self, image: QImage) -> None:
        fitted = place_rgb(image, self.process_w, self.process_h, self._scale_mode)
        frame = qimage_to_nv12(fitted, self.process_w, self.process_h)
        with self._frame_lock:
            if self.frame_np.shape != frame.shape:
                self._alloc_frame()
            np.copyto(self.frame_np, frame)
            self._content_serial += 1

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
            self._publish_image(load_still(self.path, width, height, self._scale_mode))
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

    def set_scale_mode(self, mode: str) -> None:
        next_mode = mode if mode in SCALE_MODES else SCALE_FILL
        if self._scale_mode == next_mode:
            return
        self._scale_mode = next_mode
        if self._still and self.path and is_image(self.path):
            self._publish_image(load_still(self.path, self.process_w, self.process_h, self._scale_mode))
            self.frame_ready.emit()
        elif self._decoder is not None:
            self._decoder.restart(scale_mode=self._scale_mode)

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
        # Keep the last picture until the new one is published. Clearing to
        # black here races the compositor and the monitor can stick on black.
        self.clear(keep_frame=True)
        if not path.is_file():
            return
        self.path = path
        self._pause_after_first = False
        self._paused = False
        self._skip_frames = 0
        self._convert_enabled = True
        self._last_convert = 0.0
        if is_image(path):
            self._publish_image(load_still(path, self.process_w, self.process_h, self._scale_mode))
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
        self._decoder.status_changed.connect(self._on_decoder_status)
        self._set_decoder_status("starting")
        self._decoder.start(
            path,
            self.process_w,
            self.process_h,
            self._output_fps,
            self._gpu_decode,
            paused=False,
            scale_mode=self._scale_mode,
        )

    def check_decoder(self, now: float, stuck_after: float, judge: bool = True) -> str | None:
        """Start this bus's clip again if its decoder has stopped.

        Called every second by the engine's watchdog, on the GUI thread, like
        every other change to the decoder. Returns what was wrong, or None.
        judge=False only notes the current state, after a gap between checks.
        """
        decoder = self._decoder
        if decoder is None or self._still or self.path is None:
            self._watched = None
            return None
        what = f"the {self.name.capitalize()} decoder"
        clip = self.path.name
        if not decoder.thread_alive():
            # It never ends by itself while it is this bus's decoder, so this is
            # a crash or a hang the thread could not survive. Same pause state.
            decoder.restart()
            self._watch_decoder(decoder, now)
            return f"{what} had stopped ({clip}); started it again"
        frames = decoder.frames_read
        if not judge or decoder is not self._watched or decoder.paused or frames != self._watched_frames:
            self._watch_decoder(decoder, now)
            return None
        if frames == 0:
            # Still opening the clip. The decoder's own first-frame timeout covers that.
            return None
        idle = now - self._watched_since
        if idle < stuck_after:
            return None
        decoder.restart()
        self._watch_decoder(decoder, now)
        return f"{what} had read no frame for {idle:.0f} s ({clip}); started it again"

    def _watch_decoder(self, decoder: HwDecoder, now: float) -> None:
        self._watched = decoder
        self._watched_frames = decoder.frames_read
        self._watched_since = now

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
        decoder = self._decoder
        if decoder is None:
            return
        try:
            decoder.frame_ready.disconnect(self._on_hw_frame)
        except (RuntimeError, TypeError):
            pass
        try:
            decoder.status_changed.disconnect(self._on_decoder_status)
        except (RuntimeError, TypeError):
            pass
        self._decoder = None
        thread = decoder.stop()

        def _reap() -> None:
            # Delete the decoder only once its thread has ended. Until then the
            # thread can still be inside a signal emit on it, and deleting a
            # QObject under another thread's feet corrupts memory. The thread
            # ends soon: stop() has killed its ffmpeg and closed its queue.
            if thread is not None:
                thread.join()
            decoder.deleteLater()

        threading.Thread(target=_reap, name="decoder-reap", daemon=True).start()

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
            self._decoder.status_changed.connect(self._on_decoder_status)
        except (RuntimeError, TypeError):
            pass
        self._decoder.set_paused(self._paused)
        # The queued frames are kept: they are what follows the frame on show.
        self._set_decoder_status(self._decoder.status)

    def adopt(self, other: Bus) -> None:
        self._detach_sources()
        other._detach_sources()
        self.clear(keep_frame=True)
        self.path = other.path
        with self._frame_lock:
            other.copy_array(self.frame_np)
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
        self._attach_qt()
        self._attach_decoder()
        if self._player is not None:
            self._player.play()
        if self._decoder is not None:
            self._paused = False
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
                self._decoder.status_changed.disconnect(self._on_decoder_status)
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
        self._pause_after_first, other._pause_after_first = other._pause_after_first, self._pause_after_first
        self.decoder_status, other.decoder_status = other.decoder_status, self.decoder_status
        with self._frame_lock, other._frame_lock:
            self.frame_np, other.frame_np = other.frame_np, self.frame_np
            self._content_serial, other._content_serial = other._content_serial, self._content_serial
            newest = max(self._content_serial, other._content_serial) + 1
            self._content_serial = newest
            other._content_serial = newest
        self._full_frames = True
        other._full_frames = False
        self._convert_enabled = True
        other._convert_enabled = True
        self._apply_min_dt()
        other._apply_min_dt()
        # Only the Qt fallback player still skips its first frames after a swap.
        self._skip_frames = 2 if self._player is not None else 0
        other._skip_frames = 2 if other._player is not None else 0
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
        # Pause before telling anyone. A listener that reacts to the first frame
        # (a MIX that started while it was on its way) then sees the bus as it
        # will stay, and can start it playing.
        if self._pause_after_first:
            self._pause_after_first = False
            self.set_paused(True)
        self.frame_ready.emit()

    @Slot(str)
    def _on_decoder_status(self, status: str) -> None:
        if self.sender() is not self._decoder:
            return
        self._set_decoder_status(status)

    @Slot()
    def _on_hw_frame(self) -> None:
        if self.sender() is not self._decoder:
            return
        if self._pause_after_first:
            # Take the clip's first frame off the front of the queue, so that
            # playing later carries on with the second frame.
            decoder = self._decoder
            if decoder is not None:
                with self._frame_lock:
                    if decoder.pop_frame(self.frame_np, 0.0):
                        self._content_serial += 1
            self._after_publish()
            return
        if self._paused:
            # Frames decoded ahead while nobody is taking them are not news.
            return
        self.frame_ready.emit()

    @Slot(QVideoFrame)
    def _on_video_frame(self, video_frame: QVideoFrame) -> None:
        if not video_frame.isValid():
            return
        if not self._should_publish():
            return
        self._publish_image(video_frame_to_rgb(video_frame))
        self._after_publish()
