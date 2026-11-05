"""A long run of the real pipeline, the way a service uses it, looking for anything going wrong.

Not a unit test (unittest only collects test*.py). It plays library clips on
Program and Preview at the pane's settings and keeps doing what an operator
does: a new clip on Preview, a fade across, Take, now and then a fade to black
and back, now and then a still. All the while it checks:

- that every output tick had a new frame from each playing clip;
- the session log of this run, for any error or watchdog repair;
- memory, handles and threads, for leaks.

Run it with Python's memory checks on, so a native library writing where it
should not is caught (5 Nov 2026: the running pane's own objects were found
corrupted):

    set PYTHONMALLOC=debug
    .venv\\Scripts\\python.exe -X faulthandler tests\\soak_pipeline.py --minutes 60

It opens no window and does not take the single-instance lock. NDI goes only
to the private group mbp-bench as "MBP bench soak", or to a stand-in with
--no-ndi. The session log goes to a temporary folder, never the pane's own.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import random
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from media_background_pane import engine as engine_module  # noqa: E402
from media_background_pane import log, ndi_output  # noqa: E402
from media_background_pane.bus import is_media, is_video  # noqa: E402
from media_background_pane.config import Config  # noqa: E402
from media_background_pane.timing import begin_precise_timer, end_precise_timer  # noqa: E402

LIBRARY = Path(r"G:\My Drive\Video Projection\Evening Service\Media Backgrounds")


class _PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


def _process_stats() -> tuple[float, int, int]:
    """(private memory in MB, open handles, threads) of this process."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESS_MEMORY_COUNTERS_EX), wintypes.DWORD]
    process = kernel32.GetCurrentProcess()
    counters = _PROCESS_MEMORY_COUNTERS_EX()
    counters.cb = ctypes.sizeof(counters)
    psapi.GetProcessMemoryInfo(process, ctypes.byref(counters), counters.cb)
    handles = wintypes.DWORD(0)
    kernel32.GetProcessHandleCount(process, ctypes.byref(handles))
    import threading

    return counters.PrivateUsage / 2**20, int(handles.value), threading.active_count()


class Recorder:
    """Stands in for NdiSender with --no-ndi. Only counts frames."""

    def __init__(self, name, fps, width=0, height=0):
        self.status = "recording only"
        self.available = True
        self.frames = 0

    def submit_array(self, array):
        self.frames += 1

    def set_fps(self, fps):
        pass

    def set_size(self, width, height):
        pass

    def connection_count(self):
        return 0

    def close(self):
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--minutes", type=float, default=60.0)
    parser.add_argument("--quality", default="1080p")
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--library", type=Path, default=LIBRARY)
    parser.add_argument("--no-ndi", action="store_true", help="use a stand-in instead of real NDI")
    parser.add_argument("--seed", type=int, default=5)
    args = parser.parse_args()

    clips = sorted(p for p in args.library.iterdir() if p.is_file() and is_media(p))
    videos = [p for p in clips if is_video(p)]
    stills = [p for p in clips if not is_video(p)]
    if len(videos) < 2:
        print(f"need at least two videos in {args.library}")
        return 2
    rng = random.Random(args.seed)
    work = Path(tempfile.mkdtemp(prefix="mbp-soak-"))
    patches = [
        mock.patch.object(log, "session_log_path", return_value=work / "session.log"),
        mock.patch.object(log, "fallback_log_path", return_value=work / "fallback.log"),
    ]
    if args.no_ndi:
        patches.append(mock.patch.object(engine_module, "NdiSender", Recorder))
    else:
        original_create = ndi_output.ndi.SendCreate

        def private_create():
            settings = original_create()
            settings.groups = "mbp-bench"
            return settings

        patches.append(mock.patch.object(ndi_output.ndi, "SendCreate", private_create))
    for patch in patches:
        patch.start()
    begin_precise_timer()
    app = QApplication.instance() or QApplication([])
    config = Config()
    config.quality = args.quality
    config.fps = args.fps
    config.ndi_name = "MBP bench soak"
    engine = engine_module.Engine(config)
    print(f"soak: {args.minutes:g} min at {args.quality} {args.fps} fps, "
          f"{'NDI stand-in' if args.no_ndi else 'real NDI (group mbp-bench)'}, "
          f"PYTHONMALLOC={os.environ.get('PYTHONMALLOC', '(default)')}; log in {work}", flush=True)

    state = {"fade": None, "events": 0, "late": {"program": 0, "preview": 0}}
    started = time.monotonic()
    samples: list[tuple[float, float, int, int]] = []

    def chase(what: str, start: float, end: float, seconds: float, then=None) -> None:
        state["fade"] = (what, start, end, time.monotonic(), seconds, then)

    def tick() -> None:
        fade = state["fade"]
        if fade is None:
            return
        what, start, end, t0, seconds, then = fade
        progress = min(1.0, (time.monotonic() - t0) / seconds)
        eased = progress * progress * (3 - 2 * progress)
        value = start + (end - start) * eased
        if what == "mix":
            engine.set_mix(value)
        else:
            engine.set_dimmer(value)
        if progress >= 1.0:
            state["fade"] = None
            if then is not None:
                then()

    def take_after_fade() -> None:
        engine.set_mix(1.0)
        engine.take()

    def next_step() -> None:
        if state["fade"] is not None:
            return
        state["events"] += 1
        roll = rng.random()
        if roll < 0.12 and engine.dimmer > 0.5:
            chase("dim", engine.dimmer, 0.0, rng.uniform(2, 6), lambda: chase("dim", 0.0, 1.0, rng.uniform(2, 6)))
            return
        pool = stills if stills and roll < 0.2 else videos
        clip = rng.choice(pool)
        engine.load_preview(clip)
        delay = int(rng.uniform(1.5, 4.0) * 1000)
        QTimer.singleShot(delay, lambda: chase("mix", 0.0, 1.0, rng.uniform(2, 10), take_after_fade))

    def sample() -> None:
        for name in ("program", "preview"):
            state["late"][name] += engine.late_frames[name]
        engine.late_frames = {"program": 0, "preview": 0}
        private, handles, threads = _process_stats()
        minutes = (time.monotonic() - started) / 60
        samples.append((minutes, private, handles, threads))
        print(f"  {minutes:5.1f} min: {state['events']} operator steps, private memory {private:6.0f} MB, "
              f"handles {handles}, threads {threads}, ticks without a new frame "
              f"Program {state['late']['program']}, Preview {state['late']['preview']}", flush=True)

    engine.program.load(videos[0])
    fade_timer = QTimer()
    fade_timer.setInterval(8)
    fade_timer.timeout.connect(tick)
    fade_timer.start()
    step_timer = QTimer()
    step_timer.setInterval(20_000)
    step_timer.timeout.connect(next_step)
    step_timer.start()
    sample_timer = QTimer()
    sample_timer.setInterval(60_000)
    sample_timer.timeout.connect(sample)
    sample_timer.start()
    QTimer.singleShot(int(args.minutes * 60_000), app.quit)
    QTimer.singleShot(3000, next_step)
    try:
        app.exec()
    finally:
        fade_timer.stop()
        step_timer.stop()
        sample_timer.stop()
        engine.close()
        end_precise_timer()
        for patch in reversed(patches):
            patch.stop()
    sample()
    text = (work / "session.log").read_text(encoding="utf-8") if (work / "session.log").exists() else ""
    problems = [
        line
        for line in text.splitlines()
        if line[:4].isdigit() and not any(ok in line for ok in ("watchdog: on:", "stop: the pane is closing"))
    ]
    print("\nsession log lines other than start and stop:")
    print("\n".join("  " + line for line in problems) if problems else "  none")
    if len(samples) >= 3:
        first, last = samples[1], samples[-1]
        print(f"\nmemory {first[1]:.0f} MB at {first[0]:.0f} min, {last[1]:.0f} MB at {last[0]:.0f} min; "
              f"handles {first[2]} -> {last[2]}; threads {first[3]} -> {last[3]}")
    late = state["late"]["program"] + state["late"]["preview"]
    verdict = "PASSED" if not problems and late == 0 else "LOOK AT THE LINES ABOVE"
    print(f"\n{verdict}")
    return 0 if verdict == "PASSED" else 1


if __name__ == "__main__":
    sys.exit(main())
