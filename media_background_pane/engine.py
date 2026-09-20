from __future__ import annotations

import threading
import time

import numpy as np
from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtGui import QColor, QImage

from .bus import Bus, idle_decoder_status
from .config import GUI_HEIGHT, GUI_WIDTH, Config
from .hw_decoder import warmup_ffmpeg
from .ndi_output import NdiSender
from .timing import sleep_until


def _scale_gui(image: QImage) -> QImage:
    if image.isNull():
        return image
    return image.scaled(
        GUI_WIDTH,
        GUI_HEIGHT,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.FastTransformation,
    )


def _scale_gui_array(array: np.ndarray) -> QImage:
    if array.size == 0:
        return QImage()
    wrapper = QImage(
        array.data,
        int(array.shape[1]),
        int(array.shape[0]),
        int(array.strides[0]),
        QImage.Format.Format_RGB32,
    )
    if wrapper.isNull():
        return wrapper
    return _scale_gui(wrapper).copy()


def _opaque_black(dest: np.ndarray) -> np.ndarray:
    dest[..., :3] = 0
    dest[..., 3] = 255
    return dest


# Natural vignetting ≈ cos⁴(θ) ≈ 1/(1+r²)² (cosine-fourth / Kino).
# Cosine develops until fade progress 0.8, then that shape holds and the
# whole frame eases to black so the hotspot never pinches to a dot.
# Radius is shaped so the middle stays open while edges crush.
# Exposure only — chromaticity is left alone.
_VIGNETTE_LIMIT = 0.8
_VIGNETTE_STRENGTH = 2.6
_VIGNETTE_RADIUS = 1.85
_VIGNETTE_GAMMA = 0.5


def _vignette_r2(height: int, width: int) -> np.ndarray:
    aspect = width / max(1, height)
    ys = ((np.arange(height, dtype=np.float32) + 0.5) / height - 0.5) * 2.0
    xs = ((np.arange(width, dtype=np.float32) + 0.5) / width - 0.5) * 2.0 * aspect
    return xs[np.newaxis, :] ** 2 + ys[:, np.newaxis] ** 2


def _smoothstep(edge0: float, edge1: float, x: float) -> float:
    t = min(1.0, max(0.0, (x - edge0) / (edge1 - edge0)))
    return t * t * (3.0 - 2.0 * t)


def _apply_vignette_dim(frame: np.ndarray, dimmer: float, r2: np.ndarray) -> np.ndarray:
    if dimmer <= 0.001:
        return _opaque_black(np.empty_like(frame))
    if dimmer >= 0.999:
        return frame
    progress = 1.0 - dimmer
    vig = min(1.0, progress / _VIGNETTE_LIMIT) ** _VIGNETTE_GAMMA
    falloff = vig * _VIGNETTE_STRENGTH
    rf2 = np.power(r2, _VIGNETTE_RADIUS / 2.0) * (falloff * falloff)
    cosine = 1.0 / np.square(rf2 + 1.0)
    shade = cosine
    tail = 1.0 - _smoothstep(_VIGNETTE_LIMIT - 0.04, 1.0, progress)
    rgb = frame[..., :3].astype(np.float32) * (1.0 / 255.0)
    lin = np.square(rgb)
    lin *= shade[..., None]
    lin *= tail
    dimmed = np.empty_like(frame)
    dimmed[..., :3] = (np.sqrt(np.clip(lin, 0.0, 1.0)) * 255.0).astype(np.uint8)
    dimmed[..., 3] = 255
    return dimmed


def _blend(
    program: np.ndarray,
    preview: np.ndarray,
    mix: float,
    dimmer: float,
    r2: np.ndarray,
) -> np.ndarray:
    if dimmer <= 0.001:
        return _opaque_black(np.empty_like(program))
    if mix <= 0.001:
        out = program
    elif mix >= 0.999:
        out = preview
    else:
        weight = np.uint16(round(mix * 256))
        inv = np.uint16(256 - int(weight))
        out = ((program.astype(np.uint16) * inv + preview.astype(np.uint16) * weight) >> 8).astype(
            np.uint8
        )
    if dimmer < 0.999:
        return _apply_vignette_dim(out, dimmer, r2)
    return out


class Engine(QObject):
    preview_changed = Signal(QImage)
    program_changed = Signal(QImage)
    mix_changed = Signal(float)
    dimmer_changed = Signal(float)
    taken = Signal()
    ndi_status_changed = Signal(str, bool)
    decoder_status_changed = Signal(str)

    def __init__(self, config: Config, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.config = config
        width, height = config.process_size
        self.preview = Bus("preview", parent=self)
        self.program = Bus("program", parent=self)
        self.preview.set_gpu_decode(config.use_gpu_decode)
        self.program.set_gpu_decode(config.use_gpu_decode)
        self.preview.set_process_size(width, height)
        self.program.set_process_size(width, height)
        self.preview.set_fps(config.fps)
        self.program.set_fps(config.fps)
        self.mix = 0.0
        self.dimmer = 1.0
        self._pgm_np = np.zeros((height, width, 4), dtype=np.uint8)
        self._pvw_np = np.zeros((height, width, 4), dtype=np.uint8)
        self._black_np = np.zeros((height, width, 4), dtype=np.uint8)
        _opaque_black(self._black_np)
        self._vignette_r2 = _vignette_r2(height, width)
        self._sender = NdiSender(config.ndi_name, config.fps, width, height)
        self._gui_enabled = True
        self._taking = False
        self._gui_n = 0
        self._preview_gui_dirty = False
        self._compose_lock = threading.Lock()
        self._compose_event = threading.Event()
        self._compose_running = True
        self.program.frame_ready.connect(self._on_program_frame)
        self.preview.frame_ready.connect(self._on_preview_frame)
        self.program.decoder_status_changed.connect(self._on_decoder_status)
        self.preview.decoder_status_changed.connect(self._on_decoder_status)
        self._compose_thread = threading.Thread(target=self._compose_run, name="compose", daemon=True)
        self._compose_thread.start()
        threading.Thread(target=warmup_ffmpeg, name="ffmpeg-warmup", daemon=True).start()
        self.ndi_status_changed.emit(self._sender.status, self._sender.available)
        self.decoder_status_changed.emit(self.decoder_status)

    @property
    def ndi_status(self) -> tuple[str, bool]:
        return self._sender.status, self._sender.available

    @property
    def decoder_status(self) -> str:
        live: list[str] = []
        for bus in (self.program, self.preview):
            if bus.is_video and bus.decoder_status and bus.decoder_status != "stopped":
                live.append(bus.decoder_status)
        unique: list[str] = []
        for status in live:
            if status not in unique:
                unique.append(status)
        if unique:
            return " / ".join(unique)
        return idle_decoder_status(self.config.use_gpu_decode)

    @property
    def gui_enabled(self) -> bool:
        return self._gui_enabled

    @gui_enabled.setter
    def gui_enabled(self, enabled: bool) -> None:
        self._gui_enabled = enabled
        self._sync_preview_convert()
        self._wake_compose()

    def _blacked_out(self) -> bool:
        return self.dimmer <= 0.001

    def _gui_black(self) -> QImage:
        image = QImage(GUI_WIDTH, GUI_HEIGHT, QImage.Format.Format_RGB32)
        image.fill(QColor(0, 0, 0))
        return image

    def _submit_opaque_black(self) -> None:
        self._sender.submit_array(self._black_np)
        if self._gui_enabled:
            self.program_changed.emit(self._gui_black())

    def _wake_compose(self) -> None:
        self._compose_event.set()

    def _push_dimmed_now(self) -> None:
        with self._compose_lock:
            self._compose_unlocked()

    def _set_playback_blackout(self, black: bool) -> None:
        self.program.set_paused(black)
        if black:
            self.program.set_convert_enabled(False)
            if not self.preview.capturing_first_frame:
                self.preview.set_paused(True)
                self.preview.set_convert_enabled(False)
        else:
            self._sync_preview_convert()

    def _idle(self) -> bool:
        return self.mix <= 0.001 and self.dimmer >= 0.999

    def _sync_preview_convert(self) -> None:
        mixing = self.mix > 0.001
        self.preview.set_full_frames(mixing)
        if self.preview.capturing_first_frame:
            self.preview.set_convert_enabled(True)
            return
        if self._blacked_out():
            self.program.set_paused(True)
            self.program.set_convert_enabled(False)
            self.preview.set_paused(True)
            self.preview.set_convert_enabled(False)
            return
        if mixing:
            self.preview.set_paused(False)
            self.preview.set_convert_enabled(True)
            return
        self.preview.set_paused(True)
        self.preview.set_convert_enabled(False)

    def set_fps(self, fps: int) -> None:
        self.config.fps = fps
        self.preview.set_fps(fps)
        self.program.set_fps(fps)
        self._sender.set_fps(fps)
        self._wake_compose()

    def set_quality(self, quality: str) -> None:
        self.config.quality = quality
        width, height = self.config.process_size
        with self._compose_lock:
            self.preview.set_process_size(width, height)
            self.program.set_process_size(width, height)
            self._pgm_np = np.zeros((height, width, 4), dtype=np.uint8)
            self._pvw_np = np.zeros((height, width, 4), dtype=np.uint8)
            self._black_np = np.zeros((height, width, 4), dtype=np.uint8)
            _opaque_black(self._black_np)
            self._vignette_r2 = _vignette_r2(height, width)
            self._sender.set_size(width, height)
        self.ndi_status_changed.emit(self._sender.status, self._sender.available)
        self._wake_compose()

    def set_gpu_decode(self, enabled: bool) -> None:
        self.config.use_gpu_decode = enabled
        self.preview.set_gpu_decode(enabled)
        self.program.set_gpu_decode(enabled)
        self.decoder_status_changed.emit(self.decoder_status)

    @Slot(float)
    def set_mix(self, value: float) -> None:
        self.mix = min(1.0, max(0.0, value))
        if not self._blacked_out():
            self._sync_preview_convert()
        self.mix_changed.emit(self.mix)
        self._wake_compose()

    @Slot(float)
    def set_dimmer(self, value: float, allow_pause: bool = True) -> None:
        was_black = self._blacked_out()
        self.dimmer = min(1.0, max(0.0, value))
        now_black = self._blacked_out()
        if now_black:
            if allow_pause:
                self._submit_opaque_black()
                self._set_playback_blackout(True)
        elif was_black:
            self._set_playback_blackout(False)
            self._push_dimmed_now()
        self.dimmer_changed.emit(self.dimmer)
        self._wake_compose()

    def load_preview(self, path) -> None:
        if self.mix > 0:
            self.set_mix(0.0)
        if self._blacked_out() or self.mix <= 0.001:
            self.preview.set_convert_enabled(True)
            self.preview.load(path, pause_after_first_frame=True)
        else:
            self.preview.load(path)
            self._sync_preview_convert()

    def take(self) -> None:
        with self._compose_lock:
            self._taking = True
            had_preview = self.preview.has_source
            try:
                if not had_preview:
                    self.mix = 0.0
                    self._sync_preview_convert()
                else:
                    self.program.swap(self.preview)
                    self.mix = 0.0
                    self._sync_preview_convert()
                    if self._blacked_out():
                        self._submit_opaque_black()
                        self._set_playback_blackout(True)
                        if self._gui_enabled:
                            self.preview.copy_array(self._pvw_np)
                            self.preview_changed.emit(_scale_gui_array(self._pvw_np))
            finally:
                self._taking = False
            if had_preview and not self._blacked_out():
                self._compose_unlocked()
        self.mix_changed.emit(0.0)
        self.taken.emit()
        self._wake_compose()

    def _compose_run(self) -> None:
        next_t = time.perf_counter()
        while self._compose_running:
            sleep_until(next_t, lambda: self._compose_running)
            if not self._compose_running:
                break
            with self._compose_lock:
                self._compose_unlocked()
            interval = 1.0 / max(1, self.config.fps)
            next_t += interval
            now = time.perf_counter()
            if next_t < now - interval * 2:
                next_t = now

    def _compose_unlocked(self) -> None:
        if not self._compose_running or self._taking:
            return
        gui = self._gui_enabled
        mix = self.mix
        dimmer = self.dimmer
        if dimmer <= 0.001:
            self._sender.submit_array(self._black_np)
            if gui:
                self.program_changed.emit(self._gui_black())
            return
        wait = 0.008 if self.program.is_video else 0.0
        self.program.take_frame(self._pgm_np, timeout=wait)
        if mix <= 0.001 and dimmer >= 0.999:
            self._sender.submit_array(self._pgm_np)
            if gui:
                self.program_changed.emit(_scale_gui_array(self._pgm_np))
                if self._preview_gui_dirty:
                    self._preview_gui_dirty = False
                    self.preview.copy_array(self._pvw_np)
                    self.preview_changed.emit(_scale_gui_array(self._pvw_np))
            return
        if mix > 0.001:
            self.preview.take_frame(self._pvw_np, timeout=0.0)
            mixed = _blend(self._pgm_np, self._pvw_np, mix, dimmer, self._vignette_r2)
        else:
            mixed = _blend(self._pgm_np, self._pgm_np, 0.0, dimmer, self._vignette_r2)
        self._sender.submit_array(mixed)
        if not gui:
            return
        self.program_changed.emit(_scale_gui_array(mixed))
        self.preview_changed.emit(_scale_gui_array(self._pvw_np))

    @Slot()
    def _on_program_frame(self) -> None:
        self._wake_compose()

    @Slot()
    def _on_preview_frame(self) -> None:
        self._preview_gui_dirty = True
        self._wake_compose()

    @Slot(str)
    def _on_decoder_status(self, _status: str) -> None:
        self.decoder_status_changed.emit(self.decoder_status)

    def close(self) -> None:
        self._compose_running = False
        self._wake_compose()
        if self._compose_thread is not None:
            self._compose_thread.join(timeout=1.5)
        self.preview.clear()
        self.program.clear()
        self._sender.close()
