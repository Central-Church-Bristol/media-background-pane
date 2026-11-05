from __future__ import annotations

import threading
import time

import numpy as np
from PySide6.QtCore import QObject, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QImage

from .bus import Bus, idle_decoder_status
from .config import GUI_FPS, GUI_HEIGHT, GUI_WIDTH, SCALE_FILL, SCALE_MODES, Config
from .frames import Mixer, black, frame_size, nv12_to_bgra
from .hw_decoder import warmup_ffmpeg
from .log import log_event, log_exception, thread_stack
from .ndi_output import NdiSender
from .timing import raise_thread_priority, sleep_until

# The watchdog runs on the GUI thread, outside the threads it watches. On
# 2 Nov 2026 the compose and Program decoder threads ended and nothing
# replaced them, so NDI and the monitors stayed on one picture.
_WATCH_INTERVAL_MS = 1000
# A compose loop that has not ticked for this long is stuck, not slow. It
# cannot be restarted while it is stuck, so the watchdog logs where it is.
_COMPOSE_STUCK = 5.0
# A clip that has been playing but has read no frame for this long is started
# again. This is only a backstop: the decoder kills a silent ffmpeg after 8 s
# and tries again, and opening a clip from Google Drive can take another 15 s.
_DECODER_STUCK = 30.0
# A longer gap between two checks means the PC was asleep or the GUI thread
# was blocked. Times measured across it mean nothing, so measure again.
_CHECK_GAP = 5.0
# How long the compositor waits for a playing bus's next frame. The decoder
# keeps half a second queued, so normally the frame is already there.
_FRAME_WAIT = 0.005
# Ticks where a playing bus had no new frame are counted and logged at most
# this often, so a service leaves a trace of any stutter without a flood.
_LATE_REPORT_INTERVAL = 60.0


def _gui_size(width: int, height: int) -> tuple[int, int]:
    scale = min(GUI_WIDTH / max(1, width), GUI_HEIGHT / max(1, height))
    return max(2, int(round(width * scale))), max(2, int(round(height * scale)))


def _scale_gui_array(array: np.ndarray) -> QImage:
    """A small RGB32 copy of an NV12 frame, for the monitors and the iPad remote."""
    if array.size == 0:
        return QImage()
    width, height = _gui_size(*frame_size(array))
    bgra = nv12_to_bgra(array, width, height)
    image = QImage(bgra.data, width, height, width * 4, QImage.Format.Format_RGB32)
    return image.copy()


class Engine(QObject):
    preview_changed = Signal(QImage)
    preview_frame = Signal(QImage, int)
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
        self.preview.set_scale_mode(config.scale_mode)
        self.program.set_scale_mode(config.scale_mode)
        self.mix = 0.0
        self.dimmer = 1.0
        self._mixer = Mixer(width, height)
        self._pgm_np = black(width, height)
        self._pvw_np = black(width, height)
        self._sender = NdiSender(config.ndi_name, config.fps, width, height)
        self._gui_enabled = True
        self._taking = False
        self._gui_n = 0
        self._preview_gui_dirty = False
        self._shown_preview_serial = 0
        self.preview_frame.connect(self._present_preview)
        self._compose_lock = threading.Lock()
        self._compose_thread_lock = threading.Lock()
        self._compose_event = threading.Event()
        self._compose_running = True
        self._compose_thread: threading.Thread | None = None
        # Counted by the compose loop, watched by the watchdog.
        self._compose_ticks = 0
        self._watched_ticks = -1
        self._watched_ticks_since = 0.0
        self._compose_stuck_reported = False
        self._last_check: float | None = None
        # Ticks where a playing bus had no new frame ready, since late_since.
        self.late_frames = {"program": 0, "preview": 0}
        self._late_since = time.monotonic()
        self.program.frame_ready.connect(self._on_program_frame)
        self.preview.frame_ready.connect(self._on_preview_frame)
        self.program.decoder_status_changed.connect(self._on_decoder_status)
        self.preview.decoder_status_changed.connect(self._on_decoder_status)
        self._start_compose_thread()
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(_WATCH_INTERVAL_MS)
        self._watchdog.timeout.connect(self._watch)
        self._watchdog.start()
        log_event("watchdog", "on: checks the compositor and both decoders every second")
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
        self._sender.submit_array(self._mixer.black)
        if self._gui_enabled:
            self.program_changed.emit(self._gui_black())

    def _wake_compose(self) -> None:
        self._compose_event.set()

    def _push_dimmed_now(self) -> None:
        with self._compose_lock:
            self._compose_unlocked()

    def _set_playback_blackout(self, black_out: bool) -> None:
        self.program.set_paused(black_out)
        if black_out:
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
            self._mixer = Mixer(width, height)
            self._pgm_np = black(width, height)
            self._pvw_np = black(width, height)
            self._sender.set_size(width, height)
        self.ndi_status_changed.emit(self._sender.status, self._sender.available)
        self._wake_compose()

    def set_gpu_decode(self, enabled: bool) -> None:
        self.config.use_gpu_decode = enabled
        self.preview.set_gpu_decode(enabled)
        self.program.set_gpu_decode(enabled)
        self.decoder_status_changed.emit(self.decoder_status)

    def set_scale_mode(self, mode: str) -> None:
        next_mode = mode if mode in SCALE_MODES else SCALE_FILL
        self.config.scale_mode = next_mode
        self.preview.set_scale_mode(next_mode)
        self.program.set_scale_mode(next_mode)
        self._wake_compose()

    @Slot(float)
    def set_mix(self, value: float) -> None:
        was_mixing = self.mix > 0.001
        self.mix = min(1.0, max(0.0, value))
        mixing = self.mix > 0.001
        if not self._blacked_out() and was_mixing != mixing:
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
            # The monitor may have been holding the clip picked under black.
            # Now that output is live again, show the real preview bus.
            self._emit_preview_frame()
        self.dimmer_changed.emit(self.dimmer)
        self._wake_compose()

    def load_preview(self, path) -> None:
        with self._compose_lock:
            self._preview_gui_dirty = False
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
                            serial = self.preview.copy_array(self._pvw_np)
                            self._post_preview(_scale_gui_array(self._pvw_np), serial)
            finally:
                self._taking = False
            if had_preview and not self._blacked_out():
                self._compose_unlocked()
        self.mix_changed.emit(0.0)
        self.taken.emit()
        self._wake_compose()

    def _start_compose_thread(self) -> bool:
        """Start the compose thread unless one is running. True if it started one."""
        with self._compose_thread_lock:
            if not self._compose_running:
                return False
            current = self._compose_thread
            if current is not None and _thread_alive(current):
                return False
            thread = threading.Thread(target=self._compose_thread_main, name="compose", daemon=True)
            self._compose_thread = thread
            thread.start()
            return True

    def _compose_thread_main(self) -> None:
        try:
            # Its timing is the output's timing, and it needs only a few ms a frame.
            raise_thread_priority()
            self._compose_run()
        except BaseException as exc:
            log_exception("compose thread", exc)
        finally:
            # In finally, so a new thread starts even if the error could not
            # be logged. On 1 Nov this call came after the logging, and a
            # logging failure would have skipped it.
            self._on_compose_thread_finished()

    def _on_compose_thread_finished(self) -> None:
        with self._compose_thread_lock:
            if self._compose_thread is threading.current_thread():
                self._compose_thread = None
            restart = self._compose_running
        if restart:
            log_event("compose", "the compose thread ended while the pane was running; starting a new one")
            self._start_compose_thread()

    @Slot()
    def _watch(self) -> None:
        try:
            self.check_playback()
        except Exception as exc:
            log_exception("watchdog", exc)

    def check_playback(self, now: float | None = None) -> list[str]:
        """Restart the compositor or a decoder that has stopped.

        Runs every second on the GUI thread, so it keeps working when those
        threads die. Returns what it found; each line is also written to the
        session log. A failure in one check does not skip the others.
        """
        if now is None:
            now = time.monotonic()
        judge = self._last_check is not None and 0.0 <= now - self._last_check <= _CHECK_GAP
        self._last_check = now
        found: list[str] = []
        if self._compose_running:
            try:
                found += self._check_compose(now, judge)
            except Exception as exc:
                log_exception("watchdog compositor check", exc)
            for bus in (self.program, self.preview):
                try:
                    line = bus.check_decoder(now, _DECODER_STUCK, judge)
                except Exception as exc:
                    log_exception(f"watchdog {bus.name} check", exc)
                    line = None
                if line:
                    found.append(line)
        for line in found:
            log_event("watchdog", line)
        return found

    def _check_compose(self, now: float, judge: bool) -> list[str]:
        thread = self._compose_thread
        if thread is None or not _thread_alive(thread):
            if not self._start_compose_thread():
                return []
            self._watched_ticks = -1
            return ["the compositor had stopped; started it again"]
        ticks = self._compose_ticks
        if not judge or ticks != self._watched_ticks:
            self._watched_ticks = ticks
            self._watched_ticks_since = now
            self._compose_stuck_reported = False
            return []
        idle = now - self._watched_ticks_since
        if idle < _COMPOSE_STUCK or self._compose_stuck_reported:
            return []
        self._compose_stuck_reported = True
        return [
            f"the compositor has been on the same frame for {idle:.0f} s. It cannot be restarted "
            f"while it is stuck. Where it is waiting:\n{thread_stack(thread).rstrip()}"
        ]

    def _compose_run(self) -> None:
        # The only clock in playback. Each tick takes one frame from each
        # playing bus, so the clips play at the output rate exactly.
        next_t = time.perf_counter()
        while self._compose_running:
            self._compose_ticks += 1
            sleep_until(next_t, lambda: self._compose_running)
            if not self._compose_running:
                break
            try:
                with self._compose_lock:
                    self._compose_unlocked()
            except Exception as exc:
                # One bad frame used to kill this thread for the rest of the
                # session. Preview, Program, and NDI then stayed on the last picture.
                log_exception("compose", exc)
            interval = 1.0 / max(1, self.config.fps)
            next_t += interval
            now = time.perf_counter()
            if next_t < now - interval * 2:
                next_t = now

    def _emit_gui(self) -> bool:
        if not self._gui_enabled:
            return False
        self._gui_n += 1
        stride = max(1, int(round(self.config.fps / float(GUI_FPS))))
        if self._gui_n < stride:
            return False
        self._gui_n = 0
        return True

    def _take(self, bus: Bus, dest: np.ndarray) -> int:
        """Take bus's next frame into dest, counting a tick it had none ready."""
        playing = bus.playing_frames()
        serial, new = bus.take_new_frame(dest, _FRAME_WAIT if playing else 0.0)
        if playing and not new:
            self.late_frames[bus.name] += 1
        return serial

    def _report_late_frames(self) -> None:
        now = time.monotonic()
        if now - self._late_since < _LATE_REPORT_INTERVAL:
            return
        program, preview = self.late_frames["program"], self.late_frames["preview"]
        if program or preview:
            log_event(
                "compose",
                f"no new frame ready in time: Program {program}, Preview {preview} "
                f"times in the last {now - self._late_since:.0f} s",
            )
        self.late_frames = {"program": 0, "preview": 0}
        self._late_since = now

    def _compose_unlocked(self) -> None:
        if not self._compose_running or self._taking:
            return
        mix = self.mix
        dimmer = self.dimmer
        mixer = self._mixer
        emit_gui = self._emit_gui()
        if dimmer <= 0.001:
            self._sender.submit_array(mixer.black)
            if emit_gui:
                self.program_changed.emit(self._gui_black())
            return
        self._take(self.program, self._pgm_np)
        if mix <= 0.001:
            shown = mixer.dim(self._pgm_np, dimmer)
            self._sender.submit_array(shown)
            if emit_gui:
                self.program_changed.emit(_scale_gui_array(shown))
            if self._preview_gui_dirty and self._gui_enabled:
                self._preview_gui_dirty = False
                serial = self.preview.copy_array(self._pvw_np)
                self._post_preview(_scale_gui_array(self._pvw_np), serial)
            self._report_late_frames()
            return
        serial = self._take(self.preview, self._pvw_np)
        mixed = self._pvw_np if mix >= 0.999 else mixer.blend(self._pgm_np, self._pvw_np, mix)
        shown = mixer.dim(mixed, dimmer)
        self._sender.submit_array(shown)
        if emit_gui:
            self.program_changed.emit(_scale_gui_array(shown))
            self._post_preview(_scale_gui_array(self._pvw_np), serial)
        self._report_late_frames()

    @Slot()
    def _on_program_frame(self) -> None:
        self._wake_compose()

    def _post_preview(self, image: QImage, serial: int) -> None:
        if image.isNull() or serial != self.preview.current_serial():
            return
        self.preview_frame.emit(image, serial)

    @Slot(QImage, int)
    def _present_preview(self, image: QImage, serial: int) -> None:
        # The compositor can still be handing the previous mix frame, including
        # the cleared buffer, after the new still has already been published.
        if image.isNull() or serial < self._shown_preview_serial:
            return
        self._shown_preview_serial = serial
        self.preview_changed.emit(image)

    def _emit_preview_frame(self) -> None:
        if not self.preview.has_source:
            return
        with self._compose_lock:
            if self._taking:
                return
            self._preview_gui_dirty = False
            serial = self.preview.copy_array(self._pvw_np)
            image = _scale_gui_array(self._pvw_np)
        self._post_preview(image, serial)

    @Slot()
    def _on_preview_frame(self) -> None:
        if self.mix <= 0.001:
            self._emit_preview_frame()
            return
        if not self._blacked_out() and self.preview.paused:
            # MIX started while the clip's first frame was on its way, and
            # the bus paused itself after it. Mixing needs it playing.
            self._sync_preview_convert()
        self._preview_gui_dirty = True
        self._wake_compose()

    @Slot(str)
    def _on_decoder_status(self, _status: str) -> None:
        self.decoder_status_changed.emit(self.decoder_status)

    def close(self) -> None:
        self._watchdog.stop()
        log_event("stop", "the pane is closing")
        self._compose_running = False
        self._wake_compose()
        with self._compose_thread_lock:
            thread = self._compose_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.5)
        self.preview.clear()
        self.program.clear()
        self._sender.close()


def _thread_alive(thread: threading.Thread) -> bool:
    try:
        return thread.is_alive()
    except Exception:
        # 5 Nov 2026: in the running pane, is_alive() on the live compose
        # thread raised "Thread.__init__() not called" every second, and the
        # watchdog stopped there. Say "alive": the tick count still catches a
        # compositor that has stopped moving.
        return True
