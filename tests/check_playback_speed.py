"""How fast does the video decoder play a looping clip?

Runs the app's own decoder (HwDecoder, with the same ffmpeg command as the app)
on a clip, next to a stand-in for the compositor. The stand-in takes one frame
per output tick, the way Engine._compose_run does for a video on Program.

For each decode mode it reports:

- frames per second read from ffmpeg. The ffmpeg command ends in fps=<output fps>,
  so each frame is 1/<output fps> of a second of the clip. That makes
  read rate / output fps the playback speed (1.00x = real time).
- how often the compositor had no new frame (showed one twice) or skipped frames.
- CPU used by ffmpeg and by this script.

It also checks two more things. A compositor that stops taking frames must not
block the decoder. Resuming after a pause must not race to catch up.

It opens no window, no NDI source and not the single-instance lock. That makes it
safe to run while Media Background Pane is open. It is not one of the unit tests
(unittest only collects test*.py).

    .venv\\Scripts\\python.exe tests\\check_playback_speed.py
    .venv\\Scripts\\python.exe tests\\check_playback_speed.py --clip "G:\\My Drive\\Video Projection\\Evening Service\\Media Backgrounds\\Light Tunnel.mov"
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
from PySide6.QtCore import QCoreApplication

from media_background_pane import hw_decoder
from media_background_pane.hw_decoder import HwDecoder, find_ffmpeg
from media_background_pane.timing import begin_precise_timer, end_precise_timer, sleep_until

DEFAULT_CLIP = ROOT / "vendor" / "ffmpeg" / "testsrc.mp4"
MODES = ("software", "gpu", "gpu-d3d11")
WARMUP = 1.0
# Inside this band the clip plays at real time. Timer jitter alone stays well inside it.
SPEED_OK = (0.95, 1.05)


def _quiet_log(context: str, exc: BaseException | None = None, **_kwargs) -> None:
    # Keep check runs out of the app's session.log.
    print(f"  [{context}] {type(exc).__name__ if exc else 'error'}: {exc}", file=sys.stderr)


def _process_cpu(proc) -> float | None:
    """CPU seconds a child process has used so far (Windows only)."""
    handle = getattr(proc, "_handle", None)
    if sys.platform != "win32" or not handle:
        return None
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    times = [wintypes.FILETIME() for _ in range(4)]
    if not kernel32.GetProcessTimes(int(handle), *(ctypes.byref(t) for t in times)):
        return None

    def seconds(ft: wintypes.FILETIME) -> float:
        return ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7

    return seconds(times[2]) + seconds(times[3])


class CountingDecoder(HwDecoder):
    """The app's decoder, held to one decode mode, numbering every frame it reads."""

    def __init__(self, mode: str) -> None:
        super().__init__(f"check-{mode}")
        self.mode = mode
        self.read_times: list[float] = []

    def _run_attempts(self, generation: int) -> bool:
        if not self._running or generation != self._generation:
            return False
        self._play_mode(self.mode, generation)
        return self._running and generation == self._generation

    def _enqueue(self, frame: np.ndarray) -> bool:
        # _play_mode hands every frame it reads to _enqueue exactly once.
        # Write the frame number into the first pixel so the compositor can tell
        # which frame it was given.
        seq = len(self.read_times) + 1
        frame[0, 0, 0] = seq & 0xFF
        frame[0, 0, 1] = (seq >> 8) & 0xFF
        frame[0, 0, 2] = (seq >> 16) & 0xFF
        self.read_times.append(time.perf_counter())
        return super()._enqueue(frame)

    def reads_between(self, start: float, end: float) -> int:
        return sum(1 for t in list(self.read_times) if start <= t < end)


class Compositor(threading.Thread):
    """Takes one frame per output tick, like Engine._compose_run with a clip on Program."""

    def __init__(self, decoder: HwDecoder, fps: int, width: int, height: int) -> None:
        super().__init__(name="check-compose", daemon=True)
        self.decoder = decoder
        self.fps = fps
        self.dest = np.zeros((height, width, 4), dtype=np.uint8)
        self.ticks: list[tuple[float, int | None]] = []
        self.running = True

    def run(self) -> None:
        interval = 1.0 / self.fps
        next_t = time.perf_counter()
        while self.running:
            sleep_until(next_t, lambda: self.running)
            if not self.running:
                break
            # Same wait the engine uses for a video on Program.
            seq = None
            if self.decoder.pop_frame(self.dest, timeout=0.008):
                d = self.dest
                seq = int(d[0, 0, 0]) | (int(d[0, 0, 1]) << 8) | (int(d[0, 0, 2]) << 16)
            self.ticks.append((time.perf_counter(), seq))
            next_t += interval
            now = time.perf_counter()
            if next_t < now - interval * 2:
                next_t = now


@dataclass
class Playback:
    reader_fps: float
    speed: float
    shown_speed: float | None
    ticks: int
    repeats: int
    skips: int
    ffmpeg_cores: float | None
    python_cores: float


def _tick_stats(ticks: list[tuple[float, int | None]], start: float, end: float, fps: int):
    last: int | None = None
    repeats = skips = counted = 0
    first_shown: tuple[float, int] | None = None
    last_shown: tuple[float, int] | None = None
    for t, seq in ticks:
        inside = start <= t < end
        if seq is None or seq == last:
            if inside:
                repeats += 1
                counted += 1
            continue
        if inside:
            counted += 1
            if last is not None and seq - last > 1:
                skips += 1
            if first_shown is None:
                first_shown = (t, seq)
            last_shown = (t, seq)
        last = seq
    shown_speed = None
    if first_shown and last_shown and last_shown[0] > first_shown[0]:
        shown_speed = (last_shown[1] - first_shown[1]) / ((last_shown[0] - first_shown[0]) * fps)
    return counted, repeats, skips, shown_speed


def _start(mode: str, clip: Path, fps: int, width: int, height: int) -> CountingDecoder | None:
    decoder = CountingDecoder(mode)
    decoder.start(clip, width, height, fps, gpu=mode != "software")
    deadline = time.perf_counter() + 6.0
    while time.perf_counter() < deadline:
        if decoder.read_times:
            return decoder
        time.sleep(0.02)
    _stop(decoder)
    return None


def _stop(decoder: HwDecoder) -> None:
    thread = decoder.stop()
    if thread is not None:
        thread.join(3.0)


def measure_playback(
    mode: str, clip: Path, fps: int, width: int, height: int, seconds: float
) -> Playback | None:
    decoder = _start(mode, clip, fps, width, height)
    if decoder is None:
        return None
    compositor = Compositor(decoder, fps, width, height)
    compositor.start()
    try:
        time.sleep(WARMUP)
        proc = decoder._proc
        cpu0 = _process_cpu(proc)
        py0 = time.process_time()
        t0 = time.perf_counter()
        time.sleep(seconds)
        t1 = time.perf_counter()
        cpu1 = _process_cpu(proc) if decoder._proc is proc else None
        py1 = time.process_time()
    finally:
        compositor.running = False
        compositor.join(1.0)
        _stop(decoder)
    window = t1 - t0
    reader_fps = decoder.reads_between(t0, t1) / window
    ticks, repeats, skips, shown_speed = _tick_stats(compositor.ticks, t0, t1, fps)
    ffmpeg_cores = (cpu1 - cpu0) / window if cpu0 is not None and cpu1 is not None else None
    return Playback(
        reader_fps=reader_fps,
        speed=reader_fps / fps,
        shown_speed=shown_speed,
        ticks=ticks,
        repeats=repeats,
        skips=skips,
        ffmpeg_cores=ffmpeg_cores,
        python_cores=(py1 - py0) / window,
    )


def measure_stalled_consumer(
    mode: str, clip: Path, fps: int, width: int, height: int, seconds: float
) -> float | None:
    """Frames per second the decoder keeps reading when nothing takes frames from it."""
    decoder = _start(mode, clip, fps, width, height)
    if decoder is None:
        return None
    try:
        time.sleep(WARMUP)
        t0 = time.perf_counter()
        time.sleep(seconds)
        t1 = time.perf_counter()
    finally:
        _stop(decoder)
    return decoder.reads_between(t0, t1) / (t1 - t0)


def measure_pause_resume(
    mode: str, clip: Path, fps: int, width: int, height: int, pause: float = 2.0
) -> tuple[int, int] | None:
    """(frames read while paused, frames read in the first second after resuming)."""
    decoder = _start(mode, clip, fps, width, height)
    if decoder is None:
        return None
    compositor = Compositor(decoder, fps, width, height)
    compositor.start()
    try:
        time.sleep(WARMUP)
        decoder.set_paused(True)
        paused_at = time.perf_counter()
        time.sleep(pause)
        resumed_at = time.perf_counter()
        decoder.set_paused(False)
        time.sleep(1.0)
        done = time.perf_counter()
    finally:
        compositor.running = False
        compositor.join(1.0)
        _stop(decoder)
    # Allow for one frame that was already being read when the pause landed.
    during = decoder.reads_between(paused_at + 0.1, resumed_at)
    after = decoder.reads_between(resumed_at, done)
    return during, after


def _speed_verdict(speed: float) -> str:
    if speed > SPEED_OK[1]:
        return "TOO FAST"
    if speed < SPEED_OK[0]:
        return "too slow (this machine could not keep up)"
    return "OK, real time"


def _cores(value: float | None) -> str:
    return "  n/a" if value is None else f"{value:5.2f}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--clip", type=Path, default=DEFAULT_CLIP, help="video to loop (default: vendor/ffmpeg/testsrc.mp4)")
    parser.add_argument("--fps", type=int, default=24, help="output fps (the app default is 24)")
    parser.add_argument("--size", default="960x540", help="output size (the app default, 540p, is 960x540)")
    parser.add_argument("--seconds", type=float, default=5.0, help="length of each measurement")
    parser.add_argument("--modes", default=",".join(MODES), help="decode modes to try")
    parser.add_argument("--quick", action="store_true", help="only measure playback speed")
    args = parser.parse_args()

    width, height = (int(part) for part in args.size.lower().split("x"))
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    if find_ffmpeg() is None:
        print("ffmpeg not found (expected vendor\\ffmpeg\\ffmpeg.exe)")
        return 2
    if not args.clip.is_file():
        print(f"clip not found: {args.clip}")
        return 2
    if "gpu-d3d11" in modes and not hw_decoder._has_filter("scale_d3d11"):
        print("gpu-d3d11: this ffmpeg has no scale_d3d11 filter, skipping")
        modes.remove("gpu-d3d11")

    hw_decoder.log_exception = _quiet_log
    _app = QCoreApplication.instance() or QCoreApplication([])
    begin_precise_timer()
    failures = 0
    try:
        print(f"clip:   {args.clip}")
        print(f"output: {width}x{height} at {args.fps} fps, {args.seconds:g} s per measurement\n")
        print("Playback with a compositor taking one frame per tick")
        print("  mode         read fps   speed   shown   no new frame   skipped   ffmpeg cpu   python cpu")
        for mode in modes:
            result = measure_playback(mode, args.clip, args.fps, width, height, args.seconds)
            if result is None:
                print(f"  {mode:<11}  no frames (mode not available here)")
                continue
            shown = "  n/a" if result.shown_speed is None else f"{result.shown_speed:4.2f}x"
            print(
                f"  {mode:<11}  {result.reader_fps:8.1f}   {result.speed:4.2f}x  {shown}"
                f"   {result.repeats:4d}/{result.ticks:<4d}     {result.skips:4d}/{result.ticks:<4d}"
                f"   {_cores(result.ffmpeg_cores)} cores  {_cores(result.python_cores)} cores"
                f"   {_speed_verdict(result.speed)}"
            )
            if result.speed > SPEED_OK[1]:
                failures += 1
        if args.quick:
            return 1 if failures else 0

        print("\nCompositor stops taking frames: the decoder must keep reading at real time")
        for mode in modes:
            fps_read = measure_stalled_consumer(mode, args.clip, args.fps, width, height, 3.0)
            if fps_read is None:
                continue
            speed = fps_read / args.fps
            if speed < 0.5:
                verdict = "BLOCKED (picture would freeze)"
                failures += 1
            elif speed > SPEED_OK[1]:
                verdict = "TOO FAST"
                failures += 1
            else:
                verdict = "OK, still reading"
            print(f"  {mode:<11}  {fps_read:8.1f} frames/s   {speed:4.2f}x   {verdict}")

        print("\nPause for 2 s, then resume: no catch-up rush after the pause")
        for mode in modes:
            result = measure_pause_resume(mode, args.clip, args.fps, width, height)
            if result is None:
                continue
            during, after = result
            rushed = after > args.fps * SPEED_OK[1] + 2
            verdict = "RUSHED to catch up" if rushed else "OK"
            if rushed or during > 1:
                failures += 1
            print(
                f"  {mode:<11}  read {during} frames while paused, "
                f"{after} in the first second after resuming (real time is {args.fps})   {verdict}"
            )
    finally:
        end_precise_timer()
    print("\nAll checks passed." if not failures else f"\n{failures} check(s) failed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
