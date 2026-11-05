"""Check the watchdog with the real decoder, ffmpeg and a real clip. Not a unit test.

Runs the app's own Engine, decoders and watchdog timer in this process. NDI is
replaced by a recorder that only notes each picture; nothing is sent on the
network. The session log goes to a temporary folder. A running pane is not
touched, so this is safe to run while it is open.

It breaks playback on purpose in the ways seen on 31 Oct and 2 Nov, and times
how long the picture takes to move again:

    .venv\\Scripts\\python.exe tests\\check_watchdog.py
    .venv\\Scripts\\python.exe tests\\check_watchdog.py --clip "G:\\My Drive\\Video Projection\\Evening Service\\Media Backgrounds\\Light Tunnel.mov"
    .venv\\Scripts\\python.exe tests\\check_watchdog.py --backstop   (adds about 40 s)
"""
from __future__ import annotations

import argparse
import ctypes
import re
import shutil
import sys
import tempfile
import threading
import time
import warnings
from ctypes import wintypes
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# Bus tries to disconnect a slot that was never connected. Harmless, and the app (pythonw) never shows it.
warnings.filterwarnings("ignore", message=".*Failed to disconnect", category=RuntimeWarning)

from PySide6.QtCore import QCoreApplication

from media_background_pane import engine as engine_module
from media_background_pane import hw_decoder, log
from media_background_pane.config import Config
from media_background_pane.engine import Engine
from media_background_pane.timing import begin_precise_timer, end_precise_timer

DEFAULT_CLIP = REPO / "vendor" / "ffmpeg" / "testsrc.mp4"


class PictureRecorder:
    """Stands in for NdiSender. Notes when each picture would have gone out, and a hash of it."""

    def __init__(self, name: str, fps: int, width: int = 0, height: int = 0) -> None:
        self.status = "recording only, nothing sent"
        self.available = True
        self._lock = threading.Lock()
        self._seen: list[tuple[float, int]] = []

    def submit_array(self, array) -> None:
        # NV12 since 5 Nov 2026: one byte plane, luma on top.
        key = hash(array.reshape(array.shape[0], -1)[::16, ::16].tobytes())
        with self._lock:
            self._seen.append((time.monotonic(), key))

    def since(self, start: float) -> list[tuple[float, int]]:
        with self._lock:
            return [item for item in self._seen if item[0] >= start]

    def set_fps(self, fps: int) -> None:
        pass

    def set_size(self, width: int, height: int) -> None:
        pass

    def close(self) -> None:
        pass


class Run:
    def __init__(self, app: QCoreApplication, engine: Engine, recorder: PictureRecorder, log_path: Path) -> None:
        self.app = app
        self.engine = engine
        self.recorder = recorder
        self.log_path = log_path
        self.rows: list[tuple[str, str, bool]] = []

    def pump(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while True:
            self.app.processEvents()
            if time.monotonic() >= end:
                return
            time.sleep(0.005)

    def wait_for(self, condition, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if condition():
                return True
            self.pump(0.02)
        return bool(condition())

    def pictures_per_second(self, seconds: float) -> float:
        """How often the NDI picture changed. Counts changes, not distinct
        pictures: testsrc.mp4 is a 2 s loop, so its pictures come round again."""
        start = time.monotonic()
        self.pump(seconds)
        return max(0, len(self._changes(start)) - 1) / seconds

    def _changes(self, start: float) -> list[float]:
        """Times at which the NDI picture changed."""
        times: list[float] = []
        last = None
        for at, key in self.recorder.since(start):
            if key != last:
                times.append(at)
                last = key
        return times

    def freeze_after(self, start: float, timeout: float) -> float | None:
        """How long the NDI picture stood still after start, once it moves again.

        None if it has not moved again by the timeout. Frames still queued when
        playback broke do not count as moving again: the picture has to be
        changing now, after its longest still stretch.
        """
        end = start + timeout
        while True:
            self.pump(0.05)
            now = time.monotonic()
            changes = self._changes(start - 0.2)
            if len(changes) >= 2 and now - start >= 0.5:
                gaps = [(later - earlier, index) for index, (earlier, later) in enumerate(zip(changes, changes[1:]))]
                longest, index = max(gaps)
                moving_since = len(changes) - (index + 2)
                if moving_since >= 5 and now - changes[-1] < 0.25:
                    return longest
            if now >= end:
                return None

    def log_lines(self) -> list[str]:
        """One line per log entry. A traceback entry ends with its last line, the error."""
        if not self.log_path.is_file():
            return []
        entries: list[list[str]] = []
        for line in self.log_path.read_text(encoding="utf-8").splitlines():
            if line[:2] == "20":
                entries.append([line])
            elif entries and line.strip():
                entries[-1].append(line.strip())
        return [entry[0] if len(entry) == 1 else f"{entry[0]}: {entry[-1]}" for entry in entries]

    def record(self, phase: str, result: str, ok: bool) -> None:
        self.rows.append((phase, result, ok))
        print(f"  {'PASS' if ok else 'FAIL'}  {phase}: {result}", flush=True)


def _raise(error: BaseException):
    def compose() -> None:
        raise error

    return compose


def healthy(run: Run, fps: int) -> None:
    rate = run.pictures_per_second(3.0)
    run.record("playing normally", f"{rate:.1f} new pictures a second (clip rate {fps})", rate >= fps * 0.8)


def compositor_ends_like_2_nov(run: Run) -> None:
    """Three times, at different points in the watchdog's one-second cycle."""
    engine = run.engine
    gaps: list[float | None] = []
    lines: list[str] = []
    for wait in (0.0, 1.35, 1.7):
        run.pump(wait)
        old = engine._compose_thread
        before = len(run.log_lines())
        # End the compose thread and stop its own restart too, as on 2 Nov.
        engine._on_compose_thread_finished = lambda: None
        engine._compose_unlocked = _raise(SystemExit())
        ended = time.monotonic()
        old.join(2.0)
        del engine._compose_unlocked
        del engine._on_compose_thread_finished
        gaps.append(run.freeze_after(ended, 6.0))
        lines += [line for line in run.log_lines()[before:] if "watchdog:" in line]
    ok = (
        all(gap is not None and gap < 2.0 for gap in gaps)
        and sum("the compositor had stopped" in line for line in lines) == len(gaps)
    )
    run.record(
        "compositor ended, own restart blocked (3 times)",
        f"picture still for {', '.join(_secs(gap) for gap in gaps)}; log each time: {_quote(lines[:1])}",
        ok,
    )


def compositor_error_with_logging_broken(run: Run) -> None:
    engine = run.engine
    old = engine._compose_thread
    raised = {"done": False}

    def bad_frame_once() -> None:
        if not raised["done"]:
            raised["done"] = True
            raise ValueError("bad frame")
        Engine._compose_unlocked(engine)

    with mock.patch.object(engine_module, "log_exception", side_effect=RuntimeError("logging is broken")), \
            mock.patch.object(threading, "excepthook", lambda args: None):
        engine._compose_unlocked = bad_frame_once
        started = time.monotonic()
        old.join(2.0)
        gap = run.freeze_after(started, 6.0)
    del engine._compose_unlocked
    replaced = engine._compose_thread is not old and engine._compose_thread is not None
    ok = gap is not None and gap < 1.0 and replaced
    run.record("compositor error while logging is broken", f"new compose thread at once; picture still for {_secs(gap)}", ok)


def program_decoder_ends(run: Run) -> None:
    engine = run.engine
    decoder = engine.program._decoder
    old = decoder._thread
    before = len(run.log_lines())

    def crash_once(proc, buffer):
        del decoder._read_into
        raise SystemExit()

    decoder._read_into = crash_once
    ended = time.monotonic()
    old.join(3.0)
    gap = run.freeze_after(ended, 8.0)
    lines = [line for line in run.log_lines()[before:] if "watchdog:" in line or "decoder program" in line]
    ok = gap is not None and gap < 3.5 and any("the Program decoder had stopped" in line for line in lines)
    run.record("Program decoder thread ended", f"picture still for {_secs(gap)}; log: {_quote(lines)}", ok)


def program_ffmpeg_hangs(run: Run, backstop: bool) -> None:
    """Freeze the real ffmpeg process, as a Google Drive file that stops answering would."""
    engine = run.engine
    decoder = engine.program._decoder
    proc = decoder._proc
    before = len(run.log_lines())
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtSuspendProcess.argtypes = [wintypes.HANDLE]
    ntdll.NtSuspendProcess(int(proc._handle))
    hung = time.monotonic()
    if backstop:
        label = "Program ffmpeg hung, decoder's own stall kill off"
        gap = run.freeze_after(hung, 45.0)
        lines = [line for line in run.log_lines()[before:] if "watchdog:" in line]
        # The line says 30 or 31 s, depending on where the one-second check falls.
        ok = gap is not None and 29.0 < gap < 35.0 and any(
            re.search(r"had read no frame for 3[01] s", line) for line in lines
        )
    else:
        label = "Program ffmpeg hung"
        gap = run.freeze_after(hung, 20.0)
        lines = [line for line in run.log_lines()[before:] if "watchdog:" in line]
        # The decoder's own stall kill (8 s) handles this. The watchdog stays out of it.
        ok = gap is not None and 6.0 < gap < 13.0 and not lines
    run.record(label, f"picture still for {_secs(gap)}; watchdog lines: {_quote(lines)}", ok)
    try:
        proc.kill()
    except OSError:
        pass


def normal_use(run: Run, second_clip: Path) -> None:
    """Ordinary use with the timer running. The watchdog must do nothing."""
    engine = run.engine
    before = len(run.log_lines())
    engine.load_preview(second_clip)
    run.wait_for(lambda: not engine.preview.capturing_first_frame, 5.0)
    run.pump(2.0)
    engine.set_mix(0.5)
    run.pump(2.0)
    engine.take()
    run.pump(2.0)
    engine.set_dimmer(0.0)
    run.pump(3.0)
    engine.set_dimmer(1.0)
    rate = run.pictures_per_second(2.0)
    lines = [line for line in run.log_lines()[before:] if "watchdog:" in line]
    ok = not lines and rate > 15
    run.record("preview, mix, take, blackout, back up", f"no watchdog lines; {rate:.1f} new pictures a second after", ok)


def _secs(gap: float | None) -> str:
    return "never" if gap is None else f"{gap:.2f} s"


def _quote(lines: list[str]) -> str:
    if not lines:
        return "none"
    return " | ".join(line.split(" ", 2)[2] for line in lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--clip", type=Path, default=DEFAULT_CLIP, help="video to play on Program")
    parser.add_argument("--backstop", action="store_true", help="also time the 30 s backstop with the decoder's stall kill off")
    args = parser.parse_args()
    if hw_decoder.find_ffmpeg() is None:
        print("ffmpeg not found in vendor\\ffmpeg")
        return 2
    if not args.clip.is_file():
        print(f"clip not found: {args.clip}")
        return 2

    work = Path(tempfile.mkdtemp(prefix="mbp-watchdog-check-"))
    log_path = work / "session.log"
    # A second copy, so Preview has a clip of its own.
    second_clip = work / f"second copy{args.clip.suffix}"
    shutil.copyfile(args.clip, second_clip)
    patches = [
        mock.patch.object(engine_module, "NdiSender", PictureRecorder),
        mock.patch.object(log, "session_log_path", return_value=log_path),
        mock.patch.object(log, "fallback_log_path", return_value=work / "fallback.log"),
    ]
    for patch in patches:
        patch.start()
    begin_precise_timer()
    app = QCoreApplication.instance() or QCoreApplication([])
    config = Config()
    engine = Engine(config)
    run = Run(app, engine, engine._sender, log_path)
    print(f"Clip: {args.clip}  ({config.quality}, {config.fps} fps, GPU decode {'on' if config.use_gpu_decode else 'off'})")
    print(f"Session log for this check: {log_path}\n")
    try:
        engine.program.load(args.clip)
        if not run.wait_for(lambda: engine.program._decoder is not None and engine.program._decoder.frames_read > 5, 15.0):
            print("The clip did not start playing.")
            return 1
        healthy(run, config.fps)
        compositor_ends_like_2_nov(run)
        run.pump(1.0)
        compositor_error_with_logging_broken(run)
        run.pump(1.0)
        program_decoder_ends(run)
        run.pump(1.0)
        program_ffmpeg_hangs(run, backstop=False)
        if args.backstop:
            run.pump(2.0)
            with mock.patch.object(hw_decoder, "_STALL_TIMEOUT", 1000.0):
                engine.program._decoder.restart()
                run.wait_for(lambda: engine.program._decoder.frames_read > 5, 15.0)
                program_ffmpeg_hangs(run, backstop=True)
        run.pump(1.0)
        normal_use(run, second_clip)
    finally:
        engine.close()
        end_precise_timer()
        for patch in reversed(patches):
            patch.stop()

    print("\nWhat the session log said during the check:")
    for line in run.log_lines():
        print("  " + line)
    passed = all(ok for _, _, ok in run.rows)
    print(f"\n{'ALL PASSED' if passed else 'SOMETHING FAILED'}: {sum(ok for *_, ok in run.rows)} of {len(run.rows)}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
