from __future__ import annotations

import numpy as np
from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QImage

from .bus import Bus, fit_rgb
from .config import GUI_FPS, GUI_HEIGHT, GUI_WIDTH, Config
from .ndi_output import NdiSender, qimage_to_array


def _scale_gui(image: QImage) -> QImage:
    if image.isNull():
        return image
    return image.scaled(
        GUI_WIDTH,
        GUI_HEIGHT,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.FastTransformation,
    )


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

    def __init__(self, config: Config, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.config = config
        width, height = config.process_size
        self.preview = Bus("preview", parent=self)
        self.program = Bus("program", parent=self)
        self.preview.set_process_size(width, height)
        self.program.set_process_size(width, height)
        self.preview.set_fps(config.fps)
        self.program.set_fps(config.fps)
        self.mix = 0.0
        self.dimmer = 1.0
        self._pgm_np = np.zeros((height, width, 4), dtype=np.uint8)
        self._pvw_np = np.zeros((height, width, 4), dtype=np.uint8)
        self._vignette_r2 = _vignette_r2(height, width)
        self._sender = NdiSender(config.ndi_name, config.fps, width, height)
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.CoarseTimer)
        self._timer.timeout.connect(self._tick)
        self._gui_enabled = True
        self._taking = False
        self._gui_n = 0
        self.program.frame_ready.connect(self._on_program_frame)
        self.preview.frame_ready.connect(self._on_preview_frame)
        self._sync_timer()
        self._timer.start()
        self.ndi_status_changed.emit(self._sender.status, self._sender.available)

    @property
    def ndi_status(self) -> tuple[str, bool]:
        return self._sender.status, self._sender.available

    @property
    def gui_enabled(self) -> bool:
        return self._gui_enabled

    @gui_enabled.setter
    def gui_enabled(self, enabled: bool) -> None:
        self._gui_enabled = enabled
        self._sync_preview_convert()
        self._sync_timer()

    def _blacked_out(self) -> bool:
        return self.dimmer <= 0.001

    def _gui_black(self) -> QImage:
        image = QImage(GUI_WIDTH, GUI_HEIGHT, QImage.Format.Format_RGB32)
        image.fill(QColor(0, 0, 0))
        return image

    def _submit_opaque_black(self) -> None:
        _opaque_black(self._pgm_np)
        self._sender.submit_array(self._pgm_np)
        if self._gui_enabled:
            self.program_changed.emit(self._gui_black())

    def _push_dimmed_now(self) -> None:
        if self._blacked_out():
            self._submit_opaque_black()
            return
        self._copy_frame(self.program.frame, self._pgm_np)
        if self.mix > 0.001:
            self._copy_frame(self.preview.frame, self._pvw_np)
            mixed = _blend(self._pgm_np, self._pvw_np, self.mix, self.dimmer, self._vignette_r2)
        else:
            mixed = _blend(self._pgm_np, self._pgm_np, 0.0, self.dimmer, self._vignette_r2)
        self._sender.submit_array(mixed)
        if self._gui_enabled:
            wrapper = QImage(
                mixed.data,
                mixed.shape[1],
                mixed.shape[0],
                mixed.strides[0],
                QImage.Format.Format_RGB32,
            )
            self.program_changed.emit(_scale_gui(wrapper.copy()))
            self.preview_changed.emit(_scale_gui(self.preview.frame))

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
            self.preview.set_paused(True)
            self.preview.set_convert_enabled(False)
            return
        if mixing:
            self.preview.set_paused(False)
            self.preview.set_convert_enabled(True)
            return
        self.preview.set_paused(True)
        self.preview.set_convert_enabled(False)

    def _sync_timer(self) -> None:
        if self._idle() or self._blacked_out():
            interval = max(50, round(1000 / GUI_FPS))
            timer_type = Qt.TimerType.CoarseTimer
        else:
            interval = max(1, round(1000 / max(1, self.config.fps)))
            timer_type = Qt.TimerType.PreciseTimer
        if self._timer.timerType() != timer_type:
            self._timer.setTimerType(timer_type)
        if self._timer.interval() != interval:
            self._timer.setInterval(interval)

    def set_fps(self, fps: int) -> None:
        self.config.fps = fps
        self.preview.set_fps(fps)
        self.program.set_fps(fps)
        self._sender.set_fps(fps)
        self._sync_timer()

    def set_quality(self, quality: str) -> None:
        self.config.quality = quality
        width, height = self.config.process_size
        self.preview.set_process_size(width, height)
        self.program.set_process_size(width, height)
        self._pgm_np = np.zeros((height, width, 4), dtype=np.uint8)
        self._pvw_np = np.zeros((height, width, 4), dtype=np.uint8)
        self._vignette_r2 = _vignette_r2(height, width)
        self._sender.set_size(width, height)
        self.ndi_status_changed.emit(self._sender.status, self._sender.available)

    @Slot(float)
    def set_mix(self, value: float) -> None:
        was_idle = self._idle()
        self.mix = min(1.0, max(0.0, value))
        self._sync_preview_convert()
        if self._idle() != was_idle:
            self._sync_timer()
        self.mix_changed.emit(self.mix)

    @Slot(float)
    def set_dimmer(self, value: float, allow_pause: bool = True) -> None:
        was_idle = self._idle()
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
        if self._idle() != was_idle or now_black != was_black:
            self._sync_timer()
        self.dimmer_changed.emit(self.dimmer)

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
        self._taking = True
        try:
            if not self.preview.has_source:
                self.mix = 0.0
                self._sync_preview_convert()
                self._sync_timer()
            else:
                self.program.swap(self.preview)
                self.mix = 0.0
                self._sync_preview_convert()
                self._sync_timer()
                if self._blacked_out():
                    self._submit_opaque_black()
                    self._set_playback_blackout(True)
                else:
                    # Keep the current dimmer on the swapped frame. Submitting
                    # program.frame raw here flashed full brightness at fade end.
                    self._push_dimmed_now()
        finally:
            self._taking = False
        self.mix_changed.emit(0.0)
        self.taken.emit()

    def _copy_frame(self, image: QImage, dest: np.ndarray) -> None:
        dest_h, dest_w = dest.shape[:2]
        if image.isNull():
            _opaque_black(dest)
            return
        if image.width() != dest_w or image.height() != dest_h:
            image = fit_rgb(image, dest_w, dest_h)
        qimage_to_array(image, dest)
        dest[..., 3] = 255

    def _on_program_frame(self) -> None:
        if self._taking or not self._idle() or self._blacked_out():
            return
        self._copy_frame(self.program.frame, self._pgm_np)
        self._sender.submit_array(self._pgm_np)

    def _on_preview_frame(self) -> None:
        if self._taking or not self._gui_enabled:
            return
        if self._idle() or self._blacked_out() or self.mix <= 0.001:
            self.preview_changed.emit(_scale_gui(self.preview.frame))

    def _tick(self) -> None:
        if self._taking:
            return
        if self._blacked_out():
            return
        if not self._idle():
            self._copy_frame(self.program.frame, self._pgm_np)
            if self.mix > 0.001:
                self._copy_frame(self.preview.frame, self._pvw_np)
                mixed = _blend(self._pgm_np, self._pvw_np, self.mix, self.dimmer, self._vignette_r2)
            else:
                mixed = _blend(self._pgm_np, self._pgm_np, 0.0, self.dimmer, self._vignette_r2)
            self._sender.submit_array(mixed)
            if self._gui_enabled:
                self._gui_n += 1
                gui_every = max(2, round(self.config.fps / GUI_FPS))
                if self._gui_n >= gui_every:
                    self._gui_n = 0
                    wrapper = QImage(
                        mixed.data,
                        mixed.shape[1],
                        mixed.shape[0],
                        mixed.strides[0],
                        QImage.Format.Format_RGB32,
                    )
                    self.program_changed.emit(_scale_gui(wrapper.copy()))
                    self.preview_changed.emit(_scale_gui(self.preview.frame))
            return
        if not self._gui_enabled:
            return
        self._gui_n += 1
        if self._gui_n < 2:
            return
        self._gui_n = 0
        self.program_changed.emit(_scale_gui(self.program.frame))

    def close(self) -> None:
        self._timer.stop()
        self.preview.clear()
        self.program.clear()
        self._sender.close()
