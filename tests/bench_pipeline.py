"""Measure the pane's output pipeline: smoothness, CPU and memory.

Drives the real Engine headless, the way the pane uses it, and records every
frame the Engine hands to NDI. NDI is replaced by a stand-in that keeps the
time of each frame and reads the frame numbers drawn into the test clips (see
make_bench_clips.py). From those it reports, for each scenario:

- output frame intervals: mean, standard deviation, p1, p50, p99, max
- for each clip, between consecutive output frames: the share that showed the
  next frame (advance 1), the same frame again (a repeat) or jumped ahead
  (a skip), and where the repeats and skips happened
- CPU as a share of one core: the whole process, the compose thread alone,
  the decoder reader threads, the GUI thread, the ffmpeg processes, the total
- peak memory of the process and of its ffmpeg processes
- the load on the rest of the PC during the run

Each scenario runs in a fresh process, so peak memory and threads belong to
that scenario alone. Results go to a JSON file; --compare prints two of them
side by side.

It measures the package in --code (a folder that holds media_background_pane),
put first on sys.path. Today's code is measured from a frozen copy, so the
work in progress cannot change it:

    robocopy "<main folder>\\media_background_pane" "%TEMP%\\mbp-bench\\baseline\\media_background_pane" /E /XD __pycache__

Safety, because the live pane runs on the same PC:

- The measured package's session log and fallback log are redirected to a
  private folder before anything else, and the child process's LOCALAPPDATA,
  TEMP and TMP point there too. Config.load() and Config.save() are never called.
- NDI is a stand-in unless --ndi real. Any real NDI sender must be named
  "MBP bench ..." and is put in the private group "mbp-bench"; any other is refused.
- No window, no LAN server, no single-instance lock, no main.py.

Usage (always with -B, so no .pyc files are written):

    python -B tests\\bench_pipeline.py --code "%TEMP%\\mbp-bench\\baseline" --label before
    python -B tests\\bench_pipeline.py --code . --label after
    python -B tests\\bench_pipeline.py --code . --scenarios mix50 --quality 1080p --seconds 600 --label long
    python -B tests\\bench_pipeline.py --code . --clips real --label after-real
    python -B tests\\bench_pipeline.py --code . --ndi real --receiver --scenarios program,mix50 --label after-ndi
    python -B tests\\bench_pipeline.py --compare before.json after.json
    python -B tests\\bench_pipeline.py --reanalyse before.json
    python -B tests\\bench_pipeline.py --selftest

--ndi real --receiver uses the package's own NdiSender (named "MBP bench ...",
in the group "mbp-bench") and receives it in a second process
(bench_ndi_receive.py), so the CPU numbers include NDI's own work and the
receiver table shows what ProPresenter would get.

Scenarios: program, mix50, fade, dim, take_black, fade_dimmed (see SCENARIOS).
"""

from __future__ import annotations

import argparse
import ctypes
import importlib
import inspect
import json
import os
import platform
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import make_bench_clips as pattern  # noqa: E402

PKG = "media_background_pane"
# The live pane runs from here. Its files are never measured in place.
LIVE_FOLDERS = [Path(r"C:\Users\avbri\Documents\Media Background Pane")]
LIBRARY = Path(r"G:\My Drive\Video Projection\Evening Service\Media Backgrounds")
REAL_CLIPS = (LIBRARY / "Holy Fire.mov", LIBRARY / "Leaves.mov")
NDI_GROUP = "mbp-bench"
NDI_PREFIX = "MBP bench"
DEFAULT_FFMPEG = ROOT / "vendor" / "ffmpeg" / "ffmpeg.exe"
QUALITIES = {"1080p": (1920, 1080), "720p": (1280, 720), "540p": (960, 540), "360p": (640, 360)}
FMT_CODES = {"bgra": 1, "nv12": 2, "gray": 3, "unknown": 4, "error": 5}
FMT_NAMES = {code: name for name, code in FMT_CODES.items()}
FADE_SECONDS = 10.0


class Scenario:
    def __init__(self, title: str, setup=(), cycle: float | None = None, actions=()) -> None:
        self.title = title
        self.setup = tuple(setup)  # ("mix" | "dim", value), applied before the measured window
        self.cycle = cycle
        self.actions = tuple(actions)  # (seconds into each cycle, "fade" | "black")

    def timeline(self, seconds: float) -> list[tuple[float, str]]:
        if not self.cycle:
            return []
        out = []
        start = 0.0
        while start < seconds:
            for offset, action in self.actions:
                if start + offset < seconds:
                    out.append((start + offset, action))
            start += self.cycle
        return out


# "fade" presses the Fade button: MIX chases 0 -> 1 over 10 s, then Take, as
# MainWindow._start_fade does (when DIM is black, Fade takes at once).
# "black" presses Fade to Black / Fade from Black: DIM chases to 0 or 1 over 10 s.
SCENARIOS = {
    "program": Scenario("Program only, MIX 0"),
    "mix50": Scenario("MIX held at 0.5", setup=(("mix", 0.5),)),
    "fade": Scenario("Fade: MIX 0 to 1 over 10 s, then Take; every 15 s", cycle=15.0, actions=((4.0, "fade"),)),
    "dim": Scenario(
        "DIM 1 to 0 over 10 s, 5 s black, 0 to 1 over 10 s; every 30 s",
        cycle=30.0,
        actions=((4.0, "black"), (19.0, "black")),
    ),
    "take_black": Scenario(
        "DIM to black, Take under black, DIM back up; every 30 s",
        cycle=30.0,
        actions=((4.0, "black"), (16.0, "fade"), (19.0, "black")),
    ),
    "fade_dimmed": Scenario(
        "Fade (MIX 0 to 1, Take) with DIM held at 0.5; every 15 s",
        setup=(("dim", 0.5),),
        cycle=15.0,
        actions=((4.0, "fade"),),
    ),
}
DEFAULT_SCENARIOS = ("program", "mix50", "fade", "dim", "take_black", "fade_dimmed")


# ---------------------------------------------------------------------------
# Windows: CPU times, memory, processes
# ---------------------------------------------------------------------------
WIN = sys.platform == "win32"
if WIN:
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_VM_READ = 0x0010
    SYNCHRONIZE = 0x00100000
    THREAD_QUERY_LIMITED_INFORMATION = 0x0800
    TH32CS_SNAPPROCESS = 0x00000002
    _FT = wintypes.FILETIME
    _PFT = ctypes.POINTER(_FT)

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
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

    _k32.GetProcessTimes.argtypes = [wintypes.HANDLE, _PFT, _PFT, _PFT, _PFT]
    _k32.GetProcessTimes.restype = wintypes.BOOL
    _k32.GetThreadTimes.argtypes = [wintypes.HANDLE, _PFT, _PFT, _PFT, _PFT]
    _k32.GetThreadTimes.restype = wintypes.BOOL
    _k32.GetSystemTimes.argtypes = [_PFT, _PFT, _PFT]
    _k32.GetSystemTimes.restype = wintypes.BOOL
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenThread.restype = wintypes.HANDLE
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.GetCurrentProcess.restype = wintypes.HANDLE
    _k32.GetPriorityClass.argtypes = [wintypes.HANDLE]
    _k32.GetPriorityClass.restype = wintypes.DWORD
    _k32.GetThreadPriority.argtypes = [wintypes.HANDLE]
    _k32.GetThreadPriority.restype = ctypes.c_int
    _k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    _k32.WaitForSingleObject.restype = wintypes.DWORD
    _k32.K32GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX), wintypes.DWORD]
    _k32.K32GetProcessMemoryInfo.restype = wintypes.BOOL
    _k32.QueryThreadCycleTime.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_uint64)]
    _k32.QueryThreadCycleTime.restype = wintypes.BOOL
    _k32.QueryProcessCycleTime.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_uint64)]
    _k32.QueryProcessCycleTime.restype = wintypes.BOOL
    _k32.GetCurrentThread.restype = wintypes.HANDLE
    _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    _k32.Process32FirstW.restype = wintypes.BOOL
    _k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    _k32.Process32NextW.restype = wintypes.BOOL

PRIORITY_CLASS_NAMES = {
    0x40: "idle",
    0x4000: "below normal",
    0x20: "normal",
    0x8000: "above normal",
    0x80: "high",
    0x100: "realtime",
}
THREAD_PRIORITY_NAMES = {-15: "idle", -2: "lowest", -1: "below normal", 0: "normal", 1: "above normal", 2: "highest", 15: "time critical"}


def _ft_seconds(ft) -> float:
    return ((ft.dwHighDateTime << 32) | ft.dwLowDateTime) / 1e7


def _ft_unix(ft) -> float:
    return _ft_seconds(ft) - 11644473600.0


def process_times(handle) -> tuple[float, float] | None:
    """(cpu seconds, creation time as unix time) of a process handle."""
    c, e, k, u = _FT(), _FT(), _FT(), _FT()
    if not _k32.GetProcessTimes(handle, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
        return None
    return _ft_seconds(k) + _ft_seconds(u), _ft_unix(c)


def thread_times(handle) -> tuple[float, float] | None:
    c, e, k, u = _FT(), _FT(), _FT(), _FT()
    if not _k32.GetThreadTimes(handle, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
        return None
    return _ft_seconds(k) + _ft_seconds(u), _ft_unix(c)


def thread_cycles(handle) -> int | None:
    """CPU cycles a thread has used (TSC ticks, counted at every context switch)."""
    value = ctypes.c_uint64()
    if not _k32.QueryThreadCycleTime(handle, ctypes.byref(value)):
        return None
    return int(value.value)


def process_cycles(handle) -> int | None:
    value = ctypes.c_uint64()
    if not _k32.QueryProcessCycleTime(handle, ctypes.byref(value)):
        return None
    return int(value.value)


def calibrate_cycles(seconds: float = 0.2, rounds: int = 3) -> float:
    """Cycle-counter ticks per second, from a busy loop on this thread.

    GetThreadTimes and GetProcessTimes are sampled at the clock tick: a thread
    that wakes, works for a millisecond and sleeps again between two ticks can
    be charged nothing (today's compose thread at 540p read 0%). Cycle counts
    are exact, and the counter runs at a constant rate on this CPU, so cycles
    divided by this rate are CPU seconds.
    """
    me = _k32.GetCurrentThread()
    best = 0.0
    for _ in range(rounds):
        c0 = thread_cycles(me)
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < seconds:
            pass
        c1 = thread_cycles(me)
        t1 = time.perf_counter()
        if c0 is not None and c1 is not None:
            best = max(best, (c1 - c0) / (t1 - t0))
    return best


def system_times() -> tuple[float, float]:
    """(busy cpu seconds, idle cpu seconds) summed over all logical processors."""
    idle, kernel, user = _FT(), _FT(), _FT()
    _k32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
    total = _ft_seconds(kernel) + _ft_seconds(user)
    return total - _ft_seconds(idle), _ft_seconds(idle)


def memory_info(handle) -> dict[str, int]:
    counters = PROCESS_MEMORY_COUNTERS_EX()
    counters.cb = ctypes.sizeof(counters)
    if not _k32.K32GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
        return {}
    return {
        "working_set": counters.WorkingSetSize,
        "peak_working_set": counters.PeakWorkingSetSize,
        "private": counters.PrivateUsage,
        "peak_private": counters.PeakPagefileUsage,
    }


def process_list() -> list[tuple[int, int, str]]:
    """(pid, parent pid, exe name) of every process."""
    snap = _k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == ctypes.c_void_p(-1).value:
        return []
    out = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID), entry.szExeFile))
            ok = _k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _k32.CloseHandle(snap)
    return out


def other_process_cpu() -> dict[int, tuple[str, float, float]]:
    """pid -> (exe, cpu seconds, creation) for every process this user can query."""
    out = {}
    for pid, _ppid, exe in process_list():
        if pid == 0:
            continue
        handle = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            continue
        try:
            times = process_times(handle)
        finally:
            _k32.CloseHandle(handle)
        if times is not None:
            out[pid] = (exe, times[0], times[1])
    return out


# ---------------------------------------------------------------------------
# The recorder and the NDI stand-in
# ---------------------------------------------------------------------------
class Recorder:
    """Keeps the time, thread and pattern samples of every frame handed to NDI.

    It runs inside the caller (the compose thread, mostly), so it does as
    little as it can: one fancy-index gather of about 1,600 pixels and a few
    attribute reads, some tens of microseconds. Reading the patterns happens
    after the run.
    """

    def __init__(self, capacity: int, width: int, height: int) -> None:
        self.capacity = capacity
        self.t = np.zeros(capacity, np.float64)
        self.tid = np.zeros(capacity, np.int64)
        self.fmt = np.zeros(capacity, np.int8)
        self.mix = np.full(capacity, np.nan, np.float32)
        self.dim = np.full(capacity, np.nan, np.float32)
        self.pgm = np.zeros(capacity, np.int8)
        self.pvw = np.zeros(capacity, np.int8)
        self.samples = np.zeros((capacity, pattern.SAMPLES_PER_FRAME), np.uint8)
        self.n = 0
        self.dropped = 0
        self.lock = threading.Lock()
        self.width = width
        self.height = height
        self.samplers: dict = {}
        self.shapes: dict[str, int] = {}
        self.errors: list[str] = []
        self.probe = None

    @property
    def nbytes(self) -> int:
        return sum(a.nbytes for a in (self.t, self.tid, self.fmt, self.mix, self.dim, self.pgm, self.pvw, self.samples))

    def set_size(self, width: int, height: int) -> None:
        self.width = int(width)
        self.height = int(height)

    def record(self, array) -> None:
        t = time.perf_counter()
        tid = threading.get_ident()
        with self.lock:
            i = self.n
            if i >= self.capacity:
                self.dropped += 1
                return
            self.n = i + 1
        try:
            fmt, w, h = pattern.frame_format(array, self.width, self.height)
            key = (fmt, w, h, array.shape, str(array.dtype))
            sampler = self.samplers.get(key)
            if key not in self.samplers:
                sampler = pattern.luma_sampler(fmt, w, h) if fmt in ("bgra", "nv12", "gray") and w and h else None
                self.samplers[key] = sampler
            label = f"{fmt} {tuple(array.shape)} {array.dtype}"
            self.shapes[label] = self.shapes.get(label, 0) + 1
            if sampler is not None:
                self.samples[i] = sampler(array)
            self.fmt[i] = FMT_CODES.get(fmt, 4)
        except Exception as exc:  # never break the caller
            self.fmt[i] = FMT_CODES["error"]
            if len(self.errors) < 20:
                self.errors.append(f"{type(exc).__name__}: {exc}")
        probe = self.probe
        if probe is not None:
            try:
                mix, dim, pg, pv = probe()
                self.mix[i] = mix
                self.dim[i] = dim
                self.pgm[i] = pg
                self.pvw[i] = pv
            except Exception:
                pass
        self.tid[i] = tid
        self.t[i] = t

    def arrays(self) -> dict[str, np.ndarray]:
        n = min(self.n, self.capacity)
        return {
            "t": self.t[:n].copy(),
            "tid": self.tid[:n].copy(),
            "fmt": self.fmt[:n].copy(),
            "mix": self.mix[:n].copy(),
            "dim": self.dim[:n].copy(),
            "pgm": self.pgm[:n].copy(),
            "pvw": self.pvw[:n].copy(),
            "samples": self.samples[:n].copy(),
        }


class _State:
    recorder: Recorder | None = None
    senders: list = []
    spawned: list = []
    guard_notes: list = []


STATE = _State()


class StandInSender:
    """Stands in for ndi_output.NdiSender. Records each frame instead of sending it.

    Same interface as the real one: NdiSender(name, fps, width, height),
    submit_array, set_fps, set_size, close, connection_count, status, available.
    """

    def __init__(self, name: str, fps: int, width: int = 1920, height: int = 1080) -> None:
        self.name = name
        self.fps = max(1, int(fps))
        self.width = int(width)
        self.height = int(height)
        self.available = True
        self.status = f"NDI stand-in for the benchmark ({self.width}x{self.height})"
        self.calls = {"submit_array": 0, "submit": 0, "set_fps": 0, "set_size": 0, "close": 0}
        STATE.senders.append(self)
        if STATE.recorder is not None:
            STATE.recorder.set_size(self.width, self.height)

    def submit_array(self, array) -> None:
        self.calls["submit_array"] += 1
        if STATE.recorder is not None:
            STATE.recorder.record(array)

    def submit(self, image) -> None:
        # The engine does not use this today. Count it, and record what it can.
        self.calls["submit"] += 1

    def set_fps(self, fps: int) -> None:
        self.calls["set_fps"] += 1
        self.fps = max(1, int(fps))

    def set_size(self, width: int, height: int) -> None:
        self.calls["set_size"] += 1
        self.width = int(width)
        self.height = int(height)
        self.status = f"NDI stand-in for the benchmark ({self.width}x{self.height})"
        if STATE.recorder is not None:
            STATE.recorder.set_size(self.width, self.height)

    def connection_count(self) -> int:
        # As if ProPresenter were connected, so nothing is skipped for want of a receiver.
        return 1

    def close(self) -> None:
        self.calls["close"] += 1
        self.available = False

    def __getattr__(self, name: str):
        # A method the rebuilt sender may have that this stand-in does not know:
        # count it, and record the frame if it looks like a submit. Only names
        # that look like actions, so a flag probed with getattr() stays missing.
        if not name.startswith(("submit", "send", "flush", "set_", "clear", "reset")):
            raise AttributeError(name)

        def call(*args, **kwargs):
            self.calls[name] = self.calls.get(name, 0) + 1
            if name.startswith("submit") and args and isinstance(args[0], np.ndarray) and STATE.recorder is not None:
                STATE.recorder.record(args[0])
            return None

        return call


def recording_sender(real_cls):
    """The package's own NdiSender, also recording each frame (for --ndi real)."""

    class RecordingSender(real_cls):  # type: ignore[misc, valid-type]
        def __init__(self, name, fps, width=1920, height=1080):
            super().__init__(name, fps, width, height)
            STATE.senders.append(self)
            if STATE.recorder is not None:
                STATE.recorder.set_size(width, height)

        def submit_array(self, array):
            if STATE.recorder is not None:
                STATE.recorder.record(array)
            return super().submit_array(array)

        def set_size(self, width, height):
            if STATE.recorder is not None:
                STATE.recorder.set_size(width, height)
            return super().set_size(width, height)

    return RecordingSender


def guard_ndi(real: bool) -> None:
    """Allow only senders named 'MBP bench ...', in group mbp-bench, and only with --ndi real."""
    import NDIlib as ndi

    original = ndi.send_create

    def guarded_send_create(settings):
        name = str(getattr(settings, "ndi_name", "") or "")
        if not real or not name.startswith(NDI_PREFIX):
            STATE.guard_notes.append(f"refused an NDI sender named {name!r}")
            return None
        settings.groups = NDI_GROUP
        STATE.guard_notes.append(f"NDI sender {name!r} in group {NDI_GROUP!r}")
        return original(settings)

    ndi.send_create = guarded_send_create


def record_spawns() -> None:
    """Note every process the measured code starts (the ffmpeg commands)."""
    real_popen = subprocess.Popen

    class RecordingPopen(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, args, *a, **kw):
            super().__init__(args, *a, **kw)
            try:
                argv = [str(x) for x in args] if isinstance(args, (list, tuple)) else [str(args)]
                STATE.spawned.append({"t": time.perf_counter(), "pid": self.pid, "args": argv})
            except Exception:
                pass

    subprocess.Popen = RecordingPopen  # type: ignore[misc]


# ---------------------------------------------------------------------------
# The knobs: widgets.ChaseSlider's 8 ms animation, and MainWindow's handlers
# ---------------------------------------------------------------------------
class Knob:
    """The MIX or DIM knob. Same motion as widgets.ChaseSlider: a PreciseTimer
    every 8 ms on the GUI thread, ease in and out over 10% of the fade, the
    value an integer 0-1000, and a callback each time that integer changes."""

    def __init__(self, qt, value: int, fade_seconds: float, on_value, on_finished) -> None:
        self.minimum, self.maximum = 0, 1000
        self.value = int(value)
        self._pos = float(value)
        self._ghost = int(value)
        self._vel = 0.0
        self._chasing = False
        self._last_tick = 0.0
        self._fade_seconds = max(0.05, float(fade_seconds))
        self._on_value = on_value
        self._on_finished = on_finished
        self._timer = qt.QTimer()
        self._timer.setTimerType(qt.Qt.TimerType.PreciseTimer)
        self._timer.setInterval(8)
        self._timer.timeout.connect(self._tick)
        self.ticks = 0

    def is_chasing(self) -> bool:
        return self._chasing

    def _set_value(self, value: int) -> None:
        if value == self.value:
            return
        self.value = value
        self._on_value(value)

    def snap_to(self, value: int) -> None:
        self._timer.stop()
        self._chasing = False
        self._vel = 0.0
        value = max(self.minimum, min(self.maximum, int(value)))
        self._ghost = value
        self._pos = float(value)
        self._set_value(value)

    def stop_chase(self) -> None:
        if not self._chasing:
            self._vel = 0.0
            return
        self._timer.stop()
        self._chasing = False
        self._vel = 0.0
        self._ghost = int(round(self._pos))
        snapped = max(self.minimum, min(self.maximum, self._ghost))
        self._pos = float(snapped)
        self._set_value(snapped)

    def set_target(self, value: int) -> None:
        value = max(self.minimum, min(self.maximum, int(value)))
        self._ghost = value
        if abs(self._pos - value) < 0.5 and abs(self._vel) < 0.05:
            self._pos = float(value)
            self._set_value(value)
            if self._chasing:
                self._finish_chase()
            return
        if not self._chasing:
            self._chasing = True
            self._last_tick = time.monotonic()
            self._timer.start()

    def _finish_chase(self) -> None:
        self._timer.stop()
        self._chasing = False
        self._vel = 0.0
        snapped = max(self.minimum, min(self.maximum, int(round(self._ghost))))
        self._pos = float(snapped)
        self._ghost = snapped
        self._set_value(snapped)
        self._on_finished()

    def _tick(self) -> None:
        self.ticks += 1
        now = time.monotonic()
        dt = min(0.05, max(0.001, now - self._last_tick))
        self._last_tick = now
        fade = max(0.05, self._fade_seconds)
        ease_time = max(0.06, 0.10 * fade)
        max_v = 1000.0 / max(fade - ease_time, 0.05)
        accel = max_v / ease_time
        remaining = self._ghost - self._pos
        dist = abs(remaining)
        if dist < 0.4 and abs(self._vel) < max_v * 0.02:
            self._finish_chase()
            return
        direction = 1.0 if remaining > 0 else -1.0
        stop_dist = (self._vel * self._vel) / (2.0 * accel) if accel > 0 else 0.0
        if self._vel * remaining < 0 or dist <= stop_dist:
            dv = accel * dt
            if abs(self._vel) <= dv:
                self._vel = 0.0
            else:
                self._vel -= dv if self._vel > 0 else -dv
        else:
            self._vel += direction * accel * dt
            self._vel = max(-max_v, min(max_v, self._vel))
        self._pos += self._vel * dt
        if (direction > 0 and self._pos >= self._ghost) or (direction < 0 and self._pos <= self._ghost):
            self._finish_chase()
            return
        self._pos = max(float(self.minimum), min(float(self.maximum), self._pos))
        new_val = int(round(self._pos))
        if new_val != self.value:
            self.value = new_val
            self._on_value(new_val)

    def stop(self) -> None:
        self._timer.stop()
        self._chasing = False


class Operator:
    """What MainWindow does with the knobs and the Fade and Fade to Black buttons."""

    def __init__(self, qt, engine, events: list) -> None:
        self.engine = engine
        self.events = events
        try:
            self._dim_takes_pause = "allow_pause" in inspect.signature(engine.set_dimmer).parameters
        except (TypeError, ValueError):
            self._dim_takes_pause = False
        self.mix_knob = Knob(qt, 0, FADE_SECONDS, self._on_mix_changed, self._on_mix_chase_finished)
        dim_now = int(round(float(getattr(engine, "dimmer", 1.0)) * 1000))
        self.dim_knob = Knob(qt, dim_now, FADE_SECONDS, self._on_dim_changed, self._on_dim_chase_finished)
        self._mix_anim_mode: str | None = None
        engine.taken.connect(self._on_taken)

    def _event(self, name: str) -> None:
        self.events.append((time.perf_counter(), name))

    # MainWindow._on_mix_changed
    def _on_mix_changed(self, value: int) -> None:
        self.engine.set_mix(value / 1000.0)

    # MainWindow._on_mix_chase_finished (mode "up")
    def _on_mix_chase_finished(self) -> None:
        self._mix_anim_mode = None
        self._event("fade end")
        if self.mix_knob.value >= 1000 and self.engine.preview.has_source:
            self.take()

    # MainWindow._on_taken
    def _on_taken(self) -> None:
        self.mix_knob.snap_to(0)

    # MainWindow._on_dim_changed / _on_dim_chase_finished / _apply_dim_value
    def _on_dim_changed(self, value: int) -> None:
        self._apply_dim(value, allow_pause=not self.dim_knob.is_chasing())

    def _on_dim_chase_finished(self) -> None:
        self._event("dim fade end")
        self._apply_dim(self.dim_knob.value, allow_pause=True)

    def _apply_dim(self, value: int, allow_pause: bool) -> None:
        was_black = self.engine.dimmer <= 0.001
        if self._dim_takes_pause:
            self.engine.set_dimmer(value / 1000.0, allow_pause=allow_pause)
        else:
            self.engine.set_dimmer(value / 1000.0)
        now_black = self.engine.dimmer <= 0.001
        if now_black and not was_black:
            self._event("black")
        elif was_black and not now_black:
            self._event("unblack")

    def take(self) -> None:
        self._event("take")
        self.engine.take()

    # MainWindow._start_fade
    def press_fade(self) -> None:
        if not self.engine.preview.has_source:
            return
        self.dim_knob.stop_chase() if self.dim_knob.is_chasing() else None
        if self.engine.dimmer <= 0.001:
            self.take()
            return
        if self.mix_knob.value / 1000.0 >= 0.999:
            self.take()
            return
        self._event("fade start")
        self._mix_anim_mode = "up"
        self.mix_knob.set_target(1000)

    # MainWindow._start_black_fade
    def press_black(self) -> None:
        self._mix_anim_mode = None
        self.mix_knob.stop_chase()
        current = float(self.engine.dimmer)
        target = 1.0 if current <= 0.001 else 0.0
        self._event("dim fade start " + ("up" if target > 0.5 else "down"))
        if abs(target - current) < 0.001:
            self.dim_knob.snap_to(int(round(target * 1000)))
            self._apply_dim(int(round(target * 1000)), allow_pause=True)
            return
        self.dim_knob.set_target(int(round(target * 1000)))

    def hold(self, what: str, value: float) -> None:
        if what == "mix":
            self.mix_knob.snap_to(int(round(value * 1000)))
        else:
            self.dim_knob.snap_to(int(round(value * 1000)))

    def stop(self) -> None:
        self.mix_knob.stop()
        self.dim_knob.stop()


# ---------------------------------------------------------------------------
# CPU and memory tracking inside the measured process
# ---------------------------------------------------------------------------
class Tracker:
    """CPU of this process, its threads and its child processes over the measured window.

    Each is read two ways: cycle counts (exact; the numbers reported) and
    GetThreadTimes/GetProcessTimes (sampled at the clock tick; kept as
    cpu_sampled for comparison)."""

    def __init__(self, cycle_rate: float = 0.0) -> None:
        self.pid = os.getpid()
        self.me = _k32.GetCurrentProcess()
        self.rate = cycle_rate
        self.children: dict[int, dict] = {}
        self.threads: dict[int, dict] = {}
        self.ws_max = 0
        self.poll_seconds = 0.0
        self.polls = 0
        self.window_unix: float | None = None
        self.t0 = self.t1 = 0.0

    def _set_base(self, info: dict, cpu: float, cycles: int | None, created: float) -> None:
        if self.window_unix is None:
            return
        if created >= self.window_unix:
            info["base"], info["base_cyc"] = 0.0, 0
        else:
            info["base"], info["base_cyc"] = cpu, cycles
            info["late"] = True

    def _cyc_seconds(self, now: int | None, base: int | None) -> float | None:
        if not self.rate or now is None or base is None:
            return None
        return (now - base) / self.rate

    def poll(self) -> None:
        started = time.perf_counter()
        self._find_children()
        self._find_threads()
        mem = memory_info(self.me)
        if self.window_unix is not None:
            self.ws_max = max(self.ws_max, mem.get("working_set", 0))
        self.polls += 1
        self.poll_seconds += time.perf_counter() - started

    def _find_children(self) -> None:
        for pid, ppid, exe in process_list():
            if ppid != self.pid or pid in self.children:
                continue
            handle = _k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ | SYNCHRONIZE, False, pid)
            if not handle:
                continue
            times = process_times(handle)
            if times is None:
                _k32.CloseHandle(handle)
                continue
            info = {"handle": handle, "exe": exe, "created": times[1], "base": None, "base_cyc": None, "late": False}
            self._set_base(info, times[0], process_cycles(handle), times[1])
            self.children[pid] = info

    def _find_threads(self) -> None:
        for thread in threading.enumerate():
            tid = thread.native_id
            if tid is None or tid in self.threads:
                continue
            handle = _k32.OpenThread(THREAD_QUERY_LIMITED_INFORMATION, False, tid)
            if not handle:
                continue
            times = thread_times(handle)
            info = {"handle": handle, "name": thread.name, "ident": thread.ident, "base": None, "base_cyc": None, "late": False}
            if times is not None:
                self._set_base(info, times[0], thread_cycles(handle), times[1])
            self.threads[tid] = info

    def start_window(self) -> dict:
        self._find_children()
        self._find_threads()
        self.window_unix = time.time()
        self.t0 = time.perf_counter()
        self.proc0 = process_times(self.me)[0]
        self.proc0_cyc = process_cycles(self.me)
        for info in self.children.values():
            times = process_times(info["handle"])
            info["base"] = times[0] if times else 0.0
            info["base_cyc"] = process_cycles(info["handle"])
        for info in self.threads.values():
            times = thread_times(info["handle"])
            info["base"] = times[0] if times else 0.0
            info["base_cyc"] = thread_cycles(info["handle"])
        self.sys0 = system_times()
        self.others0 = other_process_cpu()
        self.ws_max = memory_info(self.me).get("working_set", 0)
        return {"t0": self.t0}

    def end_window(self) -> dict:
        self._find_children()
        self._find_threads()
        self.t1 = time.perf_counter()
        proc1 = process_times(self.me)[0]
        proc1_cyc = process_cycles(self.me)
        threads = []
        for tid, info in self.threads.items():
            times = thread_times(info["handle"])
            if times is None or info["base"] is None:
                continue
            threads.append(
                {
                    "tid": tid,
                    "ident": info["ident"],
                    "name": info["name"],
                    "cpu": times[0] - info["base"],
                    "cpu_cyc": self._cyc_seconds(thread_cycles(info["handle"]), info["base_cyc"]),
                    "late": info["late"],
                }
            )
        children = []
        for pid, info in self.children.items():
            times = process_times(info["handle"])
            if times is None or info["base"] is None:
                continue
            exited = _k32.WaitForSingleObject(info["handle"], 0) == 0
            children.append(
                {
                    "pid": pid,
                    "exe": info["exe"],
                    "cpu": times[0] - info["base"],
                    "cpu_cyc": self._cyc_seconds(process_cycles(info["handle"]), info["base_cyc"]),
                    "created_in_window": info["created"] >= (self.window_unix or 0),
                    "exited": exited,
                    "late": info["late"],
                    "peak_working_set": memory_info(info["handle"]).get("peak_working_set", 0),
                }
            )
        sys1 = system_times()
        others1 = other_process_cpu()
        wall = self.t1 - self.t0
        my_children = {c["pid"] for c in children}
        other = []
        for pid, (exe, cpu1, created) in others1.items():
            if pid == self.pid or pid in my_children:
                continue
            before = self.others0.get(pid)
            if before is None or abs(before[2] - created) > 1e-3:
                continue
            delta = cpu1 - before[1]
            if delta > 0.005 * wall:
                other.append({"pid": pid, "exe": exe, "cores": delta / wall})
        other.sort(key=lambda x: -x["cores"])
        return {
            "wall": wall,
            "process_cpu": proc1 - self.proc0,
            "process_cpu_cyc": self._cyc_seconds(proc1_cyc, self.proc0_cyc),
            "threads": threads,
            "children": children,
            "system_busy_cpu": sys1[0] - self.sys0[0],
            "top_other": other[:8],
            "memory": memory_info(self.me),
            "window_max_working_set": self.ws_max,
        }

    def thread_priority(self, native_id: int) -> str | None:
        handle = _k32.OpenThread(THREAD_QUERY_LIMITED_INFORMATION, False, native_id)
        if not handle:
            return None
        try:
            value = _k32.GetThreadPriority(handle)
        finally:
            _k32.CloseHandle(handle)
        return THREAD_PRIORITY_NAMES.get(value, str(value))


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
def interval_stats(times: np.ndarray, nominal_ms: float) -> dict | None:
    if len(times) < 2:
        return None
    dt = np.diff(times) * 1000.0
    return {
        "n": int(len(dt)),
        "mean": float(dt.mean()),
        "sd": float(dt.std()),
        "p1": float(np.percentile(dt, 1)),
        "p50": float(np.percentile(dt, 50)),
        "p99": float(np.percentile(dt, 99)),
        "max": float(dt.max()),
        "min": float(dt.min()),
        "late": int(np.sum(dt > 1.5 * nominal_ms)),
        "early": int(np.sum(dt < 0.5 * nominal_ms)),
        "within_4ms": float(np.mean(np.abs(dt - nominal_ms) <= 4.0)),
    }


def _near_event(t: float, events: list[tuple[float, str]], window: float = 0.35) -> str | None:
    best = None
    for et, name in events:
        if abs(et - t) <= window and (best is None or abs(et - t) < best[0]):
            best = (abs(et - t), name)
    return best[1] if best else None


def clip_series(dec: dict, region: str, code: int) -> tuple[np.ndarray, np.ndarray]:
    readable = dec[f"clip_{region}"] == code
    return readable, dec[f"index_{region}"]


def analyse_clip(
    name: str,
    info: dict,
    dec: dict,
    t: np.ndarray,
    t0: float,
    fps: int,
    pgm: np.ndarray,
    pvw: np.ndarray,
    weight: np.ndarray,
    dim: np.ndarray,
    black: np.ndarray,
    events: list[tuple[float, str]],
) -> dict:
    code = pattern.CLIP_CODES[name]
    region = info["region"]
    readable, idx = clip_series(dec, region, code)
    clip_fps = int(info["fps"])
    frames = int(info["frames"])
    if clip_fps == fps:
        loop = frames
        slot = idx.copy()
    else:
        loop = int(round(frames * fps / clip_fps))
        slot = np.where(idx >= 0, np.round(idx * fps / clip_fps).astype(np.int64) % loop, -1)
    n = len(t)
    pair = np.zeros(n, bool)
    pair[1:] = readable[1:] & readable[:-1]
    adv = np.zeros(n, np.int64)
    adv[1:] = (slot[1:] - slot[:-1]) % loop
    ii = np.nonzero(pair)[0]
    a = adv[ii]
    nxt = a == 1
    rep = a == 0
    skip = (a >= 2) & (a <= loop // 2)
    back = a > loop // 2
    counts = {
        "pairs": int(len(ii)),
        "next": int(nxt.sum()),
        "repeat": int(rep.sum()),
        "skip": int(skip.sum()),
        "back": int(back.sum()),
        "skipped_frames": int(np.sum(a[skip] - 1)),
    }
    shares = {k: (counts[k] / counts["pairs"] if counts["pairs"] else None) for k in ("next", "repeat", "skip", "back")}
    # Where each repeat and skip happened.
    by_where: dict[str, dict[str, int]] = {}
    by_bus: dict[str, dict[str, int]] = {}
    listed = []
    for j, i in enumerate(ii):
        kind = "next" if nxt[j] else "repeat" if rep[j] else "skip" if skip[j] else "back"
        if pgm[i] == code and pgm[i - 1] == code:
            bus = "program"
        elif pvw[i] == code and pvw[i - 1] == code:
            bus = "preview"
        else:
            bus = "across take"
        slot_bus = by_bus.setdefault(bus, {"pairs": 0, "next": 0, "repeat": 0, "skip": 0, "back": 0})
        slot_bus["pairs"] += 1
        slot_bus[kind] += 1
        if kind == "next":
            continue
        if slot[i] < slot[i - 1] or slot[i - 1] >= loop - 2 or slot[i] <= 1:
            where = "loop point"
        else:
            where = _near_event(t[i], events) or "steady"
        cell = by_where.setdefault(where, {"repeat": 0, "skip": 0, "back": 0})
        cell[kind] += 1
        if len(listed) < 60:
            listed.append(
                {
                    "t": round(float(t[i] - t0), 3),
                    "kind": kind,
                    "from": int(idx[i - 1]),
                    "to": int(idx[i]),
                    "where": where,
                    "bus": bus,
                    "mix": round(float(weight[i]), 3) if not np.isnan(weight[i]) else None,
                    "dim": round(float(dim[i]), 3) if not np.isnan(dim[i]) else None,
                }
            )
    # Coverage: was the pattern read wherever it should be easy to read?
    easy = (weight >= 0.25) & (dim >= 0.3) & ~black
    coverage = float(np.mean(readable[easy])) if easy.any() else None
    # Where the clip disappeared (too faint, black, or not on air) and came back.
    gaps = []
    ri = np.nonzero(readable)[0]
    for k in range(1, len(ri)):
        a_i, b_i = ri[k - 1], ri[k]
        if b_i - a_i > 1 and len(gaps) < 30:
            jump = int((slot[b_i] - slot[a_i]) % loop)
            between = int(b_i - a_i - 1)
            blacks = int(black[a_i + 1 : b_i].sum())
            gaps.append(
                {
                    "t": round(float(t[a_i] - t0), 3),
                    "until": round(float(t[b_i] - t0), 3),
                    "frames_between": between,
                    "black_between": blacks,
                    "from": int(idx[a_i]),
                    "to": int(idx[b_i]),
                    "advance": jump,
                    # For a clip that stayed on air (too faint to read, or black):
                    # clip frames that went by unseen beyond one per non-black output
                    # frame. Above 0: frames lost; below 0: the clip was held.
                    "unseen_frames": jump - 1 - (between - blacks),
                }
            )
    first = int(idx[ri[0]]) if len(ri) else None
    # Loop points: consecutive readable frames where the frame number went back to the start.
    wrapped = (slot[ii] < slot[ii - 1]) if len(ii) else np.zeros(0, bool)
    wraps = {"clean": int(np.sum(wrapped & nxt)), "not clean": int(np.sum(wrapped & ~nxt))}
    return {
        "clip": name,
        "file": info.get("file"),
        "readable_frames": int(readable.sum()),
        "coverage": coverage,
        "loop_frames_at_output_fps": loop,
        **counts,
        "share": shares,
        "where": by_where,
        "by_bus": by_bus,
        "loop_wraps": wraps,
        "events": listed,
        "gaps": gaps,
        "first_index": first,
    }


def bus_totals(clips: dict) -> dict:
    """Repeats and skips per bus (Program, Preview, across a Take), summed over the clips."""
    out: dict[str, dict] = {}
    for c in clips.values():
        for bus, cell in c["by_bus"].items():
            total = out.setdefault(bus, {"pairs": 0, "next": 0, "repeat": 0, "skip": 0, "back": 0})
            for k in total:
                total[k] += cell[k]
    for total in out.values():
        pairs = total["pairs"]
        total["share"] = {k: (total[k] / pairs if pairs else None) for k in ("next", "repeat", "skip", "back")}
    return out


def analyse_frames(
    frames: dict[str, np.ndarray],
    t0: float,
    t1: float,
    fps: int,
    clips: dict[str, dict],
    compose_idents: set[int],
    events: list[tuple[float, str]],
) -> dict:
    nominal = 1000.0 / fps
    t_all = frames["t"]
    sel = (t_all >= t0) & (t_all <= t1)
    t = t_all[sel]
    tid = frames["tid"][sel]
    fmt = frames["fmt"][sel]
    mix = frames["mix"][sel]
    dim = frames["dim"][sel]
    pgm = frames["pgm"][sel]
    pvw = frames["pvw"][sel]
    samples = frames["samples"][sel]
    out: dict = {"frames": int(len(t))}
    if len(t) == 0:
        return out
    order = np.argsort(t, kind="stable")
    t, tid, fmt, mix, dim, pgm, pvw, samples = (x[order] for x in (t, tid, fmt, mix, dim, pgm, pvw, samples))
    from_compose = np.isin(tid, list(compose_idents)) if compose_idents else np.ones(len(t), bool)
    out["formats"] = {FMT_NAMES.get(int(k), str(k)): int(v) for k, v in zip(*np.unique(fmt, return_counts=True))}
    out["intervals"] = interval_stats(t, nominal)
    out["compose_intervals"] = interval_stats(t[from_compose], nominal)
    out["extra_frames"] = int((~from_compose).sum())
    out["rate_fps"] = float((len(t) - 1) / (t[-1] - t[0])) if len(t) > 1 and t[-1] > t[0] else None
    dec = pattern.decode_samples(samples)
    grid = dec["grid"]
    black_level = np.where(fmt == FMT_CODES["bgra"], 0, 16)
    black = grid.max(axis=1) <= black_level + 2
    out["black_frames"] = int(black.sum())
    same = np.zeros(len(t), bool)
    same[1:] = np.all(grid[1:] == grid[:-1], axis=1) & ~(black[1:] & black[:-1])
    out["unchanged_frames"] = int(same.sum())
    shown = int((~(black[1:] & black[:-1])).sum()) if len(t) > 1 else 0
    out["unchanged_share"] = float(same.sum() / shown) if shown else None
    out["unchanged_at"] = [round(float(x - t0), 3) for x in t[same][:40]]
    clip_results = {}
    for name, info in clips.items():
        code = pattern.CLIP_CODES[name]
        weight = np.where(pgm == code, 1.0 - mix, np.where(pvw == code, mix, 0.0))
        weight = np.where(dim <= 0.001, 0.0, weight)
        clip_results[name] = analyse_clip(name, info, dec, t, t0, fps, pgm, pvw, weight, dim, black, events)
    out["clips"] = clip_results
    out["buses"] = bus_totals(clip_results)
    return out


# ---------------------------------------------------------------------------
# One scenario, in its own process
# ---------------------------------------------------------------------------
def run_child(spec: dict) -> dict:
    private = Path(spec["private"])
    private.mkdir(parents=True, exist_ok=True)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["QT_FFMPEG_DECODING_HW_ACCEL"] = "0"
    os.environ.setdefault("NDI_DISABLE_HARDWARE_ACCELERATION", "1")
    sys.dont_write_bytecode = True
    code = Path(spec["code"]).resolve()
    sys.path.insert(0, str(code))
    warnings: list[str] = []

    # 1. The session log, before anything else.
    log = importlib.import_module(f"{PKG}.log")
    session_log = private / "session.log"
    fallback_log = private / "fallback session.log"
    log.session_log_path = lambda: session_log
    log.fallback_log_path = lambda: fallback_log
    pkg_dir = Path(importlib.import_module(PKG).__file__).resolve().parent
    if pkg_dir.parent != code:
        raise RuntimeError(f"imported {PKG} from {pkg_dir}, not from --code {code}")

    record_spawns()
    guard_ndi(spec["ndi"] == "real")
    config_mod = importlib.import_module(f"{PKG}.config")
    hw = importlib.import_module(f"{PKG}.hw_decoder")
    ndi_mod = importlib.import_module(f"{PKG}.ndi_output")
    engine_mod = importlib.import_module(f"{PKG}.engine")
    # Anything else that copied the log functions by name gets the private ones too.
    for mod_name, mod in list(sys.modules.items()):
        if mod_name.startswith(PKG) and mod is not None:
            if getattr(mod, "session_log_path", None) not in (None, log.session_log_path):
                mod.session_log_path = log.session_log_path
            if getattr(mod, "fallback_log_path", None) not in (None, log.fallback_log_path):
                mod.fallback_log_path = log.fallback_log_path
    for path in (log.session_log_path(), log.fallback_log_path()):
        if private not in Path(path).resolve().parents:
            raise RuntimeError(f"log path not private: {path}")
    if hasattr(hw, "_ffmpeg_path") and spec.get("ffmpeg"):
        hw._ffmpeg_path = Path(spec["ffmpeg"])
    ffmpeg_used = str(hw.find_ffmpeg()) if hasattr(hw, "find_ffmpeg") else None

    if spec["ndi"] == "real":
        sender_cls = recording_sender(engine_mod.NdiSender)
    else:
        sender_cls = StandInSender
    engine_mod.NdiSender = sender_cls
    ndi_mod.NdiSender = sender_cls

    from PySide6 import QtCore
    from PySide6.QtCore import QEventLoop, Qt, QTimer
    from PySide6.QtWidgets import QApplication

    class _Qt:
        pass

    qt = _Qt()
    qt.QTimer = QTimer
    qt.Qt = Qt

    # 2. What app.main() does before the window opens.
    timer_1ms = False
    timing = importlib.import_module(f"{PKG}.timing")
    if spec["app_setup"] and hasattr(timing, "begin_precise_timer"):
        timing.begin_precise_timer()
        timer_1ms = True
    if spec["app_setup"]:
        try:
            priority = importlib.import_module(f"{PKG}.priority")
            if hasattr(priority, "prefer_livestream_apps"):
                priority.prefer_livestream_apps()
        except ImportError:
            warnings.append("no priority module; process priority left alone")
    try:
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_CompressHighFrequencyEvents, True)
    except AttributeError:
        pass
    app = QApplication(["mbp-bench"])

    def later(seconds: float, fn) -> QTimer:
        timer = QTimer()
        timer.setSingleShot(True)
        timer.setTimerType(Qt.TimerType.PreciseTimer)
        timer.timeout.connect(fn)
        timer.start(max(0, int(seconds * 1000)))
        return timer

    def spin(seconds: float) -> None:
        loop = QEventLoop()
        timer = later(seconds, loop.quit)
        loop.exec()
        timer.stop()

    width, height = QUALITIES[spec["quality"]]
    fps = int(spec["fps"])
    capacity = int((spec["warmup"] + spec["settle"] + spec["seconds"] + 40) * fps * 2) + 2000
    STATE.recorder = Recorder(capacity, width, height)

    cfg = config_mod.Config()
    cfg.quality = spec["quality"]
    cfg.fps = fps
    cfg.ndi_name = f"{NDI_PREFIX} {spec['tag']}"
    if hasattr(cfg, "use_gpu_decode"):
        cfg.use_gpu_decode = spec["decode"] == "gpu"
    if hasattr(cfg, "scale_mode"):
        cfg.scale_mode = "fill"
    if hasattr(cfg, "remote_port"):
        cfg.remote_port = 0  # never used here; the LAN server is not started

    # Before any other thread is busy: how fast the cycle counter runs.
    tracker = Tracker(calibrate_cycles())
    events: list[tuple[float, str]] = []
    engine = engine_mod.Engine(cfg)
    clip_codes = {}
    program_path = Path(spec["program"])
    preview_path = Path(spec["preview"]) if spec.get("preview") else None
    for name, info in spec["clips"].items():
        clip_codes[str(Path(info["path"]))] = pattern.CLIP_CODES[name]

    def code_of(path) -> int:
        if path is None:
            return 0
        return clip_codes.get(str(path), 9)

    def probe():
        return engine.mix, engine.dimmer, code_of(engine.program.path), code_of(engine.preview.path)

    STATE.recorder.probe = probe

    # Time spent in each compose call, if the engine has the method.
    compose_calls: list[tuple[float, float, int]] = []
    method = spec.get("compose_method") or "_compose_unlocked"
    inner = getattr(engine, method, None)
    if callable(inner):

        def timed_compose(*a, **kw):
            start = time.perf_counter()
            try:
                return inner(*a, **kw)
            finally:
                compose_calls.append((start, time.perf_counter(), threading.get_ident()))

        setattr(engine, method, timed_compose)
    else:
        warnings.append(f"engine has no {method}; compose call times not measured")

    operator = Operator(qt, engine, events)
    poll_timer = QTimer()
    poll_timer.setInterval(500)
    poll_timer.timeout.connect(tracker.poll)
    poll_timer.start()

    events.append((time.perf_counter(), "load program"))
    engine.program.load(program_path)
    if preview_path is not None:
        events.append((time.perf_counter(), "load preview"))
        engine.load_preview(preview_path)
    spin(spec["warmup"])
    if STATE.recorder.n < fps:
        spin(10.0)
    if STATE.recorder.n < fps:
        warnings.append(f"only {STATE.recorder.n} frames during the warm-up")
    scenario = SCENARIOS[spec["scenario"]]
    for what, value in scenario.setup:
        events.append((time.perf_counter(), f"hold {what} {value}"))
        operator.hold(what, value)
    spin(spec["settle"])

    def decoders() -> list[dict]:
        out = []
        for bus_name in ("program", "preview"):
            bus = getattr(engine, bus_name)
            dec = getattr(bus, "_decoder", None)
            if dec is None:
                continue
            out.append(
                {
                    "bus": bus_name,
                    "id": id(dec),
                    "path": str(getattr(dec, "_path", None) or getattr(bus, "path", "")),
                    "frames_read": int(getattr(dec, "frames_read", -1)),
                    "paused": bool(getattr(dec, "paused", False)),
                }
            )
        return out

    status0 = getattr(engine, "decoder_status", None)
    dec0 = decoders()
    compose_idents = {t.ident for t in threading.enumerate() if t.name == "compose"}
    compose_native = [t.native_id for t in threading.enumerate() if t.name == "compose"]
    compose_priority = tracker.thread_priority(compose_native[0]) if compose_native else None
    window = tracker.start_window()
    t0 = window["t0"]
    events.append((t0, "window start"))
    actions = scenario.timeline(spec["seconds"])
    action_timers = []
    for offset, action in actions:
        fn = operator.press_fade if action == "fade" else operator.press_black
        action_timers.append(later(offset, fn))
    spin(spec["seconds"])
    usage = tracker.end_window()
    t1 = tracker.t1
    events.append((t1, "window end"))
    dec1 = decoders()
    status1 = getattr(engine, "decoder_status", None)
    compose_idents |= {t.ident for t in threading.enumerate() if t.name == "compose"}
    operator.stop()
    poll_timer.stop()
    frames = STATE.recorder.arrays()
    switch_interval = sys.getswitchinterval()

    engine.close()
    deadline = time.perf_counter() + 5.0
    while time.perf_counter() < deadline:
        spin(0.1)
        alive = [c for c in tracker.children.values() if _k32.WaitForSingleObject(c["handle"], 0) != 0]
        if not alive:
            break

    # ---- results ----
    wall = usage["wall"]
    ffmpeg_children = [c for c in usage["children"] if c["exe"].lower().startswith("ffmpeg")]
    analysis_ticks = None

    def breakdown(key: str) -> dict | None:
        """CPU shares of one core, from cycle counts (key 'cpu_cyc') or sampled times ('cpu')."""
        process = usage["process_cpu_cyc"] if key == "cpu_cyc" else usage["process_cpu"]
        if process is None or any(th.get(key) is None for th in usage["threads"]):
            return None
        by_name: dict[str, float] = {}
        compose = readers = gui = other = 0.0
        for th in usage["threads"]:
            value = th[key]
            by_name[th["name"]] = by_name.get(th["name"], 0.0) + value
            if th["name"] == "compose":
                compose += value
            elif th["name"] == "MainThread":
                gui += value
            elif th["name"].startswith("ffmpeg-") and not th["name"].startswith(("ffmpeg-watch", "ffmpeg-warmup")):
                readers += value
            else:
                other += value
        ffmpeg = sum((c[key] or 0.0) for c in ffmpeg_children)
        n_ticks = analysis_ticks or 0
        return {
            "process": process / wall,
            "compose": compose / wall,
            "readers": readers / wall,
            "gui": gui / wall,
            "other_python": other / wall,
            "native": (process - compose - readers - gui - other) / wall,
            "ffmpeg": ffmpeg / wall,
            "total": (process + ffmpeg) / wall,
            "ffmpeg_processes": len(ffmpeg_children),
            "compose_ms_per_tick": compose * 1000.0 / n_ticks if n_ticks > 1 else None,
            "by_thread": {k: v / wall for k, v in sorted(by_name.items(), key=lambda kv: -kv[1])},
            "method": "cycle counts" if key == "cpu_cyc" else "GetThreadTimes/GetProcessTimes (sampled)",
        }

    clips_on = {}
    for name, info in spec["clips"].items():
        if str(Path(info["path"])) in (str(program_path), str(preview_path)):
            clips_on[name] = info
    analysis = analyse_frames(frames, t0, t1, fps, clips_on, compose_idents, [(e - 0, n) for e, n in events])
    window_calls = [(s, e, tid) for s, e, tid in compose_calls if t0 <= s <= t1]
    compose_stats = None
    if window_calls:
        d = np.array([(e - s) * 1000.0 for s, e, tid in window_calls if tid in compose_idents] or [0.0])
        compose_stats = {
            "calls": int(len(window_calls)),
            "from_compose_thread": int(sum(1 for c in window_calls if c[2] in compose_idents)),
            "mean_ms": float(d.mean()),
            "p50_ms": float(np.percentile(d, 50)),
            "p99_ms": float(np.percentile(d, 99)),
            "max_ms": float(d.max()),
            "over_budget": int(np.sum(d > 1000.0 / fps)),
        }
    ticks = analysis.get("compose_intervals", {}) or {}
    analysis_ticks = (ticks.get("n") or 0) + 1
    cpu_cycles = breakdown("cpu_cyc")
    cpu_sampled = breakdown("cpu")
    cpu_main = cpu_cycles or cpu_sampled
    cpu_main["bench_poll"] = tracker.poll_seconds / max(1e-9, time.perf_counter() - t0)
    cpu_main["cycle_rate_ghz"] = tracker.rate / 1e9 if tracker.rate else None
    decoders_out = []
    for d1 in dec1:
        d0 = next((d for d in dec0 if d["id"] == d1["id"]), None)
        read = d1["frames_read"] - d0["frames_read"] if d0 and d1["frames_read"] >= d0["frames_read"] else None
        decoders_out.append(
            {
                "bus_at_end": d1["bus"],
                "clip": Path(d1["path"]).name,
                "frames_read": read,
                "read_fps": read / wall if read is not None else None,
                "paused_at_end": d1["paused"],
                "restarted_or_new": d0 is None,
            }
        )
    try:
        log_lines = session_log.read_text(encoding="utf-8", errors="replace").splitlines() if session_log.exists() else []
    except OSError:
        log_lines = []
    mem = usage["memory"]
    result = {
        "label": spec["label"],
        "scenario": spec["scenario"],
        "scenario_title": scenario.title,
        "quality": spec["quality"],
        "size": [width, height],
        "fps": fps,
        "decode": spec["decode"],
        "ndi": spec["ndi"],
        "code": str(code),
        "program": str(program_path),
        "preview": str(preview_path) if preview_path else None,
        "window_s": wall,
        "t0_perf": t0,
        "t1_perf": t1,
        "ffmpeg": ffmpeg_used,
        "decoder_status": [status0, status1],
        "intervals": analysis.get("intervals"),
        "compose_intervals": analysis.get("compose_intervals"),
        "frames": analysis.get("frames"),
        "rate_fps": analysis.get("rate_fps"),
        "extra_frames": analysis.get("extra_frames"),
        "black_frames": analysis.get("black_frames"),
        "unchanged_frames": analysis.get("unchanged_frames"),
        "unchanged_share": analysis.get("unchanged_share"),
        "unchanged_at": analysis.get("unchanged_at"),
        "formats": analysis.get("formats"),
        "frame_shapes": STATE.recorder.shapes,
        "buses": analysis.get("buses"),
        "clips": analysis.get("clips"),
        "cpu": cpu_main,
        "cpu_sampled": cpu_sampled,
        "compose_calls": compose_stats,
        "compose_thread_priority": compose_priority,
        "memory_mb": {
            "peak_working_set": mem.get("peak_working_set", 0) / 2**20,
            "peak_private": mem.get("peak_private", 0) / 2**20,
            "window_max_working_set": usage["window_max_working_set"] / 2**20,
            "ffmpeg_peak_working_set": sum(c["peak_working_set"] for c in ffmpeg_children) / 2**20,
            "bench_buffers": STATE.recorder.nbytes / 2**20,
        },
        "decoders": decoders_out,
        "conditions": {
            "system_busy_cores": usage["system_busy_cpu"] / wall,
            # System-wide busy time is itself sampled, so take the sampled total off it.
            "other_busy_cores": usage["system_busy_cpu"] / wall - (cpu_sampled or cpu_main)["total"],
            "top_other": usage["top_other"],
            "priority_class": PRIORITY_CLASS_NAMES.get(int(_k32.GetPriorityClass(_k32.GetCurrentProcess())), "?"),
            "timer_1ms": timer_1ms,
            "switch_interval_s": switch_interval,
            "logical_cpus": os.cpu_count(),
        },
        "events": [(round(t - t0, 3), name) for t, name in events],
        "ffmpeg_spawned": [
            {
                "t": round(s["t"] - t0, 3),
                "pid": s["pid"],
                "clip": next((Path(a).name for i, a in enumerate(s["args"][1:]) if s["args"][i] == "-i"), None),
                "vf": next((a for i, a in enumerate(s["args"][1:]) if s["args"][i] == "-vf"), None),
                "cpu_in_window": next(
                    (c["cpu_cyc"] if c.get("cpu_cyc") is not None else c["cpu"] for c in usage["children"] if c["pid"] == s["pid"]),
                    None,
                ),
            }
            for s in STATE.spawned
        ][:40],
        "ffmpeg_commands": [
            s["args"]
            for s in STATE.spawned
            if any(c["pid"] == s["pid"] and c["cpu"] > 0.05 * wall for c in usage["children"])
        ][:6],
        "senders": [type(s).__name__ for s in STATE.senders],
        "sender_calls": [dict(getattr(s, "calls", {})) for s in STATE.senders if isinstance(s, StandInSender)],
        "ndi_guard": STATE.guard_notes,
        "recorder": {"capacity": STATE.recorder.capacity, "dropped": STATE.recorder.dropped, "errors": STATE.recorder.errors},
        "session_log_tail": log_lines[-30:],
        "warnings": warnings,
    }
    if spec.get("frames_out"):
        np.savez_compressed(
            spec["frames_out"],
            t=frames["t"] - t0,
            tid=frames["tid"],
            fmt=frames["fmt"],
            mix=frames["mix"],
            dim=frames["dim"],
            pgm=frames["pgm"],
            pvw=frames["pvw"],
            samples=frames["samples"],
            compose_idents=np.array(sorted(compose_idents), dtype=np.int64),
        )
    if timer_1ms and hasattr(timing, "end_precise_timer"):
        timing.end_precise_timer()
    del app
    return result


# ---------------------------------------------------------------------------
# The driver
# ---------------------------------------------------------------------------
def _clips_for(args) -> tuple[Path, Path | None, dict[str, dict]]:
    if args.program:
        program = Path(args.program)
        preview = Path(args.preview) if args.preview else None
        return program, preview, _known_clips(args, [program, preview])
    if args.clips == "real":
        return REAL_CLIPS[0], REAL_CLIPS[1], {}
    manifest = pattern.load_manifest(args.media)
    clips = {name: {**info, "path": str(args.media / info["file"])} for name, info in manifest["clips"].items()}
    second = "C" if args.clips == "bench30" else "B"
    return Path(clips["A"]["path"]), Path(clips[second]["path"]), clips


def _known_clips(args, paths) -> dict[str, dict]:
    try:
        manifest = pattern.load_manifest(args.media)
    except OSError:
        return {}
    out = {}
    for name, info in manifest["clips"].items():
        path = args.media / info["file"]
        if any(p is not None and Path(p).resolve() == path.resolve() for p in paths):
            out[name] = {**info, "path": str(path)}
    return out


def _check_code_folder(code: Path, allow_live: bool) -> None:
    if not (code / PKG / "engine.py").is_file():
        raise SystemExit(f"--code {code}: no {PKG}\\engine.py there")
    for live in LIVE_FOLDERS:
        if code.resolve() == live.resolve() and not allow_live:
            raise SystemExit(
                f"--code {code} is the live pane's folder. Measure a copy instead "
                f"(see the top of this file), or pass --allow-live-folder."
            )


def _pc(x, digits=0) -> str:
    if x is None:
        return "  -  "
    if -0.005 < x < 0:
        x = 0.0  # thread times are sampled; a hair below zero is zero
    return f"{x * 100:.{digits}f}%"


def _ms(x) -> str:
    return "   - " if x is None else f"{x:5.1f}"


def print_tables(runs: list[dict]) -> None:
    if not runs:
        return
    print()
    print("Smoothness. Intervals between output frames in ms (nominal 41.7 at 24 fps); late = intervals over 62.5 ms.")
    print("  Per bus, between consecutive output frames where its clip could be read: 'next' = its next frame,")
    print("  'rep' = the same frame again, 'skip' = jumped ahead. 'take' = the pairs across a Take.")
    head = (
        f"{'quality':<7} {'scenario':<12} {'frames':>6} {'mean':>5} {'sd':>5} {'p1':>5} {'p50':>5} {'p99':>5} {'max':>5}"
        f" {'late':>4} {'extra':>5}  {'Program':>7} {'rep':>5} {'skip':>5}  {'Preview':>7} {'rep':>5} {'skip':>5}"
        f"  {'take':>4} {'rep':>3} {'skip':>4}"
    )
    print(head)
    for r in runs:
        iv = r.get("intervals") or {}
        line = (
            f"{r['quality']:<7} {r['scenario']:<12} {r.get('frames') or 0:>6} {_ms(iv.get('mean'))} {_ms(iv.get('sd'))}"
            f" {_ms(iv.get('p1'))} {_ms(iv.get('p50'))} {_ms(iv.get('p99'))} {_ms(iv.get('max'))}"
            f" {iv.get('late', 0):>4} {r.get('extra_frames') or 0:>5}"
        )
        buses = r.get("buses") or {}
        if r.get("clips"):
            for bus in ("program", "preview"):
                b = buses.get(bus)
                if not b or not b["pairs"]:
                    line += f"  {'-':>7} {'-':>5} {'-':>5}"
                    continue
                s = b["share"]
                line += f"  {_pc(s['next'], 1):>7} {_pc(s['repeat'], 1):>5} {_pc(s['skip'], 1):>5}"
            t = buses.get("across take")
            line += f"  {t['pairs']:>4} {t['repeat']:>3} {t['skip']:>4}" if t else f"  {'-':>4} {'-':>3} {'-':>4}"
        else:
            line += f"  (no patterns in these clips; unchanged frames {_pc(r.get('unchanged_share'), 1)})"
        print(line)
    print()
    method = (runs[0].get("cpu") or {}).get("method", "GetThreadTimes/GetProcessTimes (sampled)")
    print(f"CPU as a share of one core (100% = one of the 16 threads), from {method}; and memory.")
    head = (
        f"{'quality':<7} {'scenario':<12} {'process':>7} {'compose':>7} {'readers':>7} {'gui':>5} {'native':>6}"
        f" {'ffmpeg':>7} {'total':>6} {'TaskMgr':>7} {'cmp ms/tick':>11} {'cmp p99':>7} {'peak MB':>7} {'ffm MB':>6} {'others':>6}"
    )
    print(head)
    for r in runs:
        c = r["cpu"]
        cc = r.get("compose_calls") or {}
        m = r["memory_mb"]
        cpus = r["conditions"].get("logical_cpus") or 16
        print(
            f"{r['quality']:<7} {r['scenario']:<12} {_pc(c['process']):>7} {_pc(c['compose']):>7} {_pc(c['readers']):>7}"
            f" {_pc(c['gui']):>5} {_pc(c['native']):>6} {_pc(c['ffmpeg']):>7} {_pc(c['total']):>6}"
            f" {c['total'] * 100 / cpus:6.1f}% {_ms(c.get('compose_ms_per_tick')):>11} {_ms(cc.get('p99_ms')):>7}"
            f" {m['peak_working_set']:7.0f} {m['ffmpeg_peak_working_set']:6.0f} {r['conditions']['other_busy_cores']:6.2f}"
        )
    print("  readers = decoder reader threads; native = threads Python did not start (Qt, NDI);")
    print("  TaskMgr = total as Task Manager shows it (share of all logical CPUs);")
    print("  cmp ms/tick = compose thread CPU per output tick; cmp p99 = 99th percentile wall time of one compose call;")
    print("  others = cores busy in other processes during the run.")
    received = [r for r in runs if r.get("receiver")]
    if received:
        print()
        print("Through real NDI, as the receiver saw it (arrival intervals in ms).")
        print(
            f"{'quality':<7} {'scenario':<12} {'sent':>5} {'recvd':>5} {'mean':>5} {'sd':>5} {'p1':>5} {'p50':>5} {'p99':>5} {'max':>5}"
            f" {'late':>4}  {'clip':<4} {'next':>6} {'rep':>6} {'skip':>6}  {'clip':<4} {'next':>6} {'rep':>6} {'skip':>6}  format"
        )
        for r in received:
            rv = r["receiver"]
            iv = rv.get("intervals") or {}
            line = (
                f"{r['quality']:<7} {r['scenario']:<12} {rv.get('sent_frames') or 0:>5} {rv.get('frames') or 0:>5}"
                f" {_ms(iv.get('mean'))} {_ms(iv.get('sd'))} {_ms(iv.get('p1'))} {_ms(iv.get('p50'))} {_ms(iv.get('p99'))}"
                f" {_ms(iv.get('max'))} {iv.get('late', 0):>4}"
            )
            for name, c in sorted((rv.get("clips") or {}).items())[:2]:
                s = c["share"]
                line += f"  {name:<4} {_pc(s['next'], 1):>6} {_pc(s['repeat'], 1):>6} {_pc(s['skip'], 1):>6}"
            line += f"  {rv.get('fourccs')}"
            if rv.get("error"):
                line += f"  {rv['error']}"
            print(line)


def run_driver(args) -> int:
    code = Path(args.code).resolve()
    _check_code_folder(code, args.allow_live_folder)
    program, preview, clips = _clips_for(args)
    for p in (program, preview):
        if p is not None and not p.is_file():
            raise SystemExit(f"clip not found: {p}")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    results_dir = Path(args.results) if args.results else pattern.bench_root() / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out) if args.out else results_dir / f"{args.label}_{stamp}.json"
    frames_dir = out_path.with_suffix("")
    frames_dir.mkdir(parents=True, exist_ok=True)
    qualities = [q.strip() for q in args.quality.split(",") if q.strip()]
    scenarios = [s.strip() for s in args.scenarios.split(",") if s.strip()]
    for s in scenarios:
        if s not in SCENARIOS:
            raise SystemExit(f"unknown scenario {s!r}; choose from {', '.join(SCENARIOS)}")
    for q in qualities:
        if q not in QUALITIES:
            raise SystemExit(f"unknown quality {q!r}")
    meta = {
        "label": args.label,
        "code": str(code),
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "python": sys.version.split()[0],
        "machine": platform.processor(),
        "logical_cpus": os.cpu_count(),
        "clips": {"program": str(program), "preview": str(preview) if preview else None, "patterns": sorted(clips)},
        "seconds": args.seconds,
        "decode": args.decode,
        "ndi": args.ndi,
        "app_setup": not args.no_app_setup,
    }
    print(f"Measuring {code}")
    print(f"  clips: Program {program.name}, Preview {preview.name if preview else '-'}; {args.seconds:g} s per scenario")
    runs: list[dict] = []
    failures = 0
    for quality in qualities:
        for scenario in scenarios:
            tag = f"{args.label}-{quality}-{scenario}"
            private = pattern.bench_root() / "runs" / f"{stamp}_{tag}"
            private.mkdir(parents=True, exist_ok=True)
            spec = {
                "label": args.label,
                "tag": tag,
                "code": str(code),
                "quality": quality,
                "scenario": scenario,
                "seconds": float(args.seconds),
                "warmup": float(args.warmup),
                "settle": float(args.settle),
                "fps": int(args.fps),
                "decode": args.decode,
                "ndi": args.ndi,
                "program": str(program),
                "preview": str(preview) if preview else None,
                "clips": clips,
                "private": str(private),
                "ffmpeg": str(args.ffmpeg) if args.ffmpeg else None,
                "app_setup": not args.no_app_setup,
                "compose_method": args.compose_method,
                "frames_out": str(frames_dir / f"{quality}_{scenario}.npz"),
            }
            spec_path = private / "spec.json"
            result_path = private / "result.json"
            spec_path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
            env = dict(os.environ)
            env["LOCALAPPDATA"] = str(private / "localappdata")
            env["TEMP"] = env["TMP"] = str(private / "temp")
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["QT_QPA_PLATFORM"] = "offscreen"
            for sub in ("localappdata", "temp"):
                (private / sub).mkdir(exist_ok=True)
            cmd = [sys.executable, "-B", str(Path(__file__).resolve()), "--child", str(spec_path), str(result_path)]
            started = time.perf_counter()
            print(f"  {quality:<6} {scenario:<12} ...", end="", flush=True)
            timeout = args.warmup + args.settle + args.seconds + 120
            receiver = None
            recv_out = private / "receiver.json"
            if args.receiver:
                # A real NDI receiver in its own process, connected before the window starts.
                rcmd = [
                    sys.executable, "-B", str(HERE / "bench_ndi_receive.py"),
                    "--source", f"{NDI_PREFIX} {tag}", "--seconds", str(timeout), "--wait", "60",
                    "--fps", str(args.fps), "--media", str(args.media), "--out", str(recv_out),
                ]
                receiver = subprocess.Popen(
                    rcmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace"
                )
            try:
                proc = subprocess.run(
                    cmd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout
                )
            except subprocess.TimeoutExpired:
                print(" TIMED OUT")
                failures += 1
                if receiver is not None:
                    receiver.kill()
                continue
            receiver_text = ""
            if receiver is not None:
                try:
                    receiver_text, _ = receiver.communicate(timeout=30)
                except subprocess.TimeoutExpired:
                    receiver.kill()
                    receiver_text, _ = receiver.communicate()
            if proc.returncode != 0 or not result_path.is_file():
                print(f" FAILED (exit {proc.returncode})")
                tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-15:]
                for line in tail:
                    print(f"      {line}")
                failures += 1
                continue
            result = json.loads(result_path.read_text(encoding="utf-8"))
            result["private"] = str(private)
            if receiver is not None:
                result["receiver"] = _receiver_summary(recv_out, result, receiver_text)
            runs.append(result)
            iv = result.get("intervals") or {}
            print(
                f" {time.perf_counter() - started:4.0f} s; p99 {_ms(iv.get('p99'))} ms, total CPU {_pc(result['cpu']['total'])}"
                + (f"; warnings: {result['warnings']}" if result.get("warnings") else "")
            )
            out_path.write_text(json.dumps({"meta": meta, "runs": runs}, indent=1), encoding="utf-8")
    out_path.write_text(json.dumps({"meta": meta, "runs": runs}, indent=1), encoding="utf-8")
    print_tables(runs)
    print(f"\nResults: {out_path}")
    print(f"Frame records: {frames_dir}")
    return 1 if failures else 0


def _receiver_summary(recv_out: Path, result: dict, text: str) -> dict:
    """What the NDI receiver saw during the measured window."""
    raw = recv_out.with_suffix(".npz")
    if not raw.is_file():
        return {"error": "the receiver wrote no records", "output": text.strip().splitlines()[-8:]}
    import bench_ndi_receive as receiver_mod

    z = np.load(raw)
    rec = {"arrival": z["arrival"], "stamp": z["stamp"], "samples": z["samples"]}
    try:
        info = json.loads(recv_out.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        info = {}
    rec["fourccs"] = info.get("fourccs")
    rec["source"] = info.get("source")
    clips = {}
    for name, c in (result.get("clips") or {}).items():
        clips[name] = {"region": pattern.CLIPS[name]["region"], "fps": pattern.CLIPS[name]["fps"], "frames": pattern.CLIPS[name]["frames"], "file": c.get("file")}
    summary = receiver_mod.summarise(rec, result["t0_perf"], result["t1_perf"], result["fps"], clips)
    summary["sent_frames"] = result.get("frames")
    summary["output"] = text.strip().splitlines()[-4:]
    for c in (summary.get("clips") or {}).values():
        c.pop("events", None)
        c.pop("gaps", None)
    return summary


def run_reanalyse(path: Path, media: Path, out: Path | None) -> int:
    """Run the analysis again on saved frame records (after the analysis code changed)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    frames_dir = path.with_suffix("")
    try:
        manifest = pattern.load_manifest(media)
    except OSError:
        manifest = {"clips": {}}
    for r in data["runs"]:
        npz = frames_dir / f"{r['quality']}_{r['scenario']}.npz"
        if not npz.is_file():
            print(f"  {r['quality']} {r['scenario']}: no frame records")
            continue
        z = np.load(npz)
        frames = {k: z[k] for k in ("t", "tid", "fmt", "mix", "dim", "pgm", "pvw", "samples")}
        idents = {int(x) for x in z["compose_idents"]}
        on_air = {Path(p).name for p in (r.get("program"), r.get("preview")) if p}
        clips = {
            name: {**info, "path": str(media / info["file"])}
            for name, info in manifest["clips"].items()
            if info["file"] in on_air
        }
        events = [(float(t), str(n)) for t, n in r.get("events", [])]
        analysis = analyse_frames(frames, 0.0, float(r["window_s"]), int(r["fps"]), clips, idents, events)
        for key in ("intervals", "compose_intervals", "frames", "rate_fps", "extra_frames", "black_frames",
                    "unchanged_frames", "unchanged_share", "unchanged_at", "formats", "clips", "buses"):
            r[key] = analysis.get(key)
    target = out or path
    target.write_text(json.dumps(data, indent=1), encoding="utf-8")
    print_tables(data["runs"])
    print(f"\nWrote {target}")
    return 0


def run_compare(paths: list[str]) -> int:
    data = [json.loads(Path(p).read_text(encoding="utf-8")) for p in paths]
    labels = [d["meta"]["label"] for d in data]
    keys = []
    for d in data:
        for r in d["runs"]:
            k = (r["quality"], r["scenario"])
            if k not in keys:
                keys.append(k)
    rows = [
        ("p99 interval ms", lambda r: (r.get("intervals") or {}).get("p99"), "{:.1f}"),
        ("max interval ms", lambda r: (r.get("intervals") or {}).get("max"), "{:.1f}"),
        ("interval sd ms", lambda r: (r.get("intervals") or {}).get("sd"), "{:.1f}"),
        ("repeats, worst clip", lambda r: max([c["share"]["repeat"] or 0 for c in (r.get("clips") or {}).values()] or [None]) if r.get("clips") else None, "{:.1%}"),
        ("skips, worst clip", lambda r: max([c["share"]["skip"] or 0 for c in (r.get("clips") or {}).values()] or [None]) if r.get("clips") else None, "{:.1%}"),
        ("unchanged frames", lambda r: r.get("unchanged_share"), "{:.1%}"),
        ("CPU process", lambda r: r["cpu"]["process"], "{:.0%}"),
        ("CPU compose", lambda r: r["cpu"]["compose"], "{:.0%}"),
        ("CPU ffmpeg", lambda r: r["cpu"]["ffmpeg"], "{:.0%}"),
        ("CPU total", lambda r: r["cpu"]["total"], "{:.0%}"),
        ("compose ms/tick", lambda r: r["cpu"].get("compose_ms_per_tick"), "{:.1f}"),
        ("compose call p99 ms", lambda r: (r.get("compose_calls") or {}).get("p99_ms"), "{:.1f}"),
        ("peak working set MB", lambda r: r["memory_mb"]["peak_working_set"], "{:.0f}"),
    ]
    width = max(10, max(len(x) for x in labels) + 2)
    for quality, scenario in keys:
        print(f"\n{quality} {scenario}")
        print(f"  {'':<22}" + "".join(f"{lab:>{width}}" for lab in labels))
        for title, get, fmt in rows:
            cells = []
            for d in data:
                r = next((r for r in d["runs"] if r["quality"] == quality and r["scenario"] == scenario), None)
                v = get(r) if r else None
                cells.append("-" if v is None else fmt.format(v))
            print(f"  {title:<22}" + "".join(f"{c:>{width}}" for c in cells))
    return 0


# ---------------------------------------------------------------------------
# Self-test: the stand-in and the analysis on synthetic BGRA and NV12 frames
# ---------------------------------------------------------------------------
def _synthetic_frame(fmt: str, w: int, h: int, a_index: int | None, b_index: int | None, a_weight: float, bg) -> np.ndarray:
    y = np.full((pattern.REF_H, pattern.REF_W), 16, np.uint8)
    y_a = y.copy()
    y_b = y.copy()
    if a_index is not None:
        pattern.paint_pattern_y(y_a, a_index, "A", "A")
    if b_index is not None:
        pattern.paint_pattern_y(y_b, b_index, "B", "B")
    mixed = (y_a.astype(np.float32) - 16) * a_weight + (y_b.astype(np.float32) - 16) * (1 - a_weight) + 16
    fy = pattern.REF_W // w
    small = mixed.reshape(h, fy, w, fy).mean(axis=(1, 3))
    if fmt == "nv12":
        out = np.empty((h * 3 // 2, w), np.uint8)
        out[:h] = np.clip(small, 0, 255).astype(np.uint8)
        out[h:] = 128
        return out
    rgb = np.clip((small - 16) * 255.0 / 219.0, 0, 255).astype(np.uint8)
    out = np.empty((h, w, 4), np.uint8)
    out[..., 0] = rgb
    out[..., 1] = rgb
    out[..., 2] = rgb
    out[..., 3] = 255
    return out


def run_selftest() -> int:
    ok = True
    if not pattern.selftest():
        ok = False
    fps = 24
    for fmt, (w, h) in (("bgra", (960, 540)), ("nv12", (1920, 1080)), ("nv12", (960, 540))):
        STATE.recorder = Recorder(400, w, h)
        sender = StandInSender("MBP bench selftest", fps, w, h)
        # A on Program, B on Preview, blended 50%. Program has one repeat (frame 10
        # twice) and one skip (frame 20 to 22); Preview has a repeat at 30.
        a_seq = list(range(0, 11)) + [10] + list(range(11, 21)) + list(range(22, 60))
        b_seq = list(range(0, 31)) + [30] + list(range(31, 70))
        n = min(len(a_seq), len(b_seq), 60)
        state = {"mix": 0.5}
        STATE.recorder.probe = lambda: (state["mix"], 1.0, 1, 2)
        frames = [_synthetic_frame(fmt, w, h, a_seq[i], b_seq[i], 0.5, None) for i in range(n)]
        extra = _synthetic_frame(fmt, w, h, a_seq[40], b_seq[40], 0.5, None)
        idents: set[int] = set()

        def compose() -> None:
            idents.add(threading.get_ident())
            next_t = time.perf_counter()
            for i in range(n):
                while time.perf_counter() < next_t:
                    time.sleep(0.001)
                sender.submit_array(frames[i])
                next_t += 1.0 / fps

        thread = threading.Thread(target=compose, name="compose")
        thread.start()
        time.sleep(40 / fps + 0.01)
        sender.submit_array(extra)  # an extra frame from another thread, a repeat of frame 40
        thread.join()
        rec = STATE.recorder.arrays()
        clips = {
            "A": {"region": "A", "fps": 24, "frames": 600, "file": "synthetic"},
            "B": {"region": "B", "fps": 24, "frames": 528, "file": "synthetic"},
        }
        res = analyse_frames(rec, rec["t"][0] - 1, rec["t"][-1] + 1, fps, clips, idents, [])
        a = res["clips"]["A"]
        b = res["clips"]["B"]
        good = (
            res["formats"] == {fmt: n + 1}
            and res["extra_frames"] == 1
            and a["repeat"] == 2  # the scripted one, plus the extra frame
            and a["skip"] == 1
            and a["skipped_frames"] == 1
            and b["repeat"] == 2
            and b["skip"] == 0
            and a["coverage"] == 1.0
            and abs(res["compose_intervals"]["mean"] - 1000 / fps) < 3
        )
        print(
            f"  stand-in + analysis, {fmt} {w}x{h}: {'OK' if good else 'FAILED'}"
            f" (A: {a['repeat']} repeats, {a['skip']} skips; B: {b['repeat']} repeats, {b['skip']} skips;"
            f" extra frames {res['extra_frames']}; formats {res['formats']};"
            f" compose interval mean {res['compose_intervals']['mean']:.1f} ms)"
        )
        ok &= good
    print("Self-test passed." if ok else "SELF-TEST FAILED.")
    return 0 if ok else 1


def main() -> int:
    if len(sys.argv) >= 4 and sys.argv[1] == "--child":
        spec = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        result = run_child(spec)
        Path(sys.argv[3]).write_text(json.dumps(result, indent=1), encoding="utf-8")
        sys.stdout.flush()
        os._exit(0)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--code", default=str(ROOT), help="folder that holds media_background_pane (default: this checkout)")
    parser.add_argument("--label", default="run", help="name for this set of results, e.g. before or after")
    parser.add_argument("--quality", default="540p,1080p", help="comma list: 1080p, 720p, 540p, 360p")
    parser.add_argument("--scenarios", default=",".join(DEFAULT_SCENARIOS), help="comma list of " + ", ".join(SCENARIOS))
    parser.add_argument("--seconds", type=float, default=60.0, help="measured seconds per scenario (default 60)")
    parser.add_argument("--warmup", type=float, default=4.0, help="seconds after loading before setup (default 4)")
    parser.add_argument("--settle", type=float, default=2.0, help="seconds after the setup before measuring (default 2)")
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--clips", default="bench", choices=("bench", "bench30", "real"), help="bench: A+B; bench30: A + the 30 fps H.264 clip; real: Holy Fire + Leaves")
    parser.add_argument("--program", help="clip for Program (instead of --clips)")
    parser.add_argument("--preview", help="clip for Preview (instead of --clips)")
    parser.add_argument("--media", type=Path, default=pattern.DEFAULT_MEDIA, help="folder with the bench clips")
    parser.add_argument("--decode", default="gpu", choices=("gpu", "software"))
    parser.add_argument("--ndi", default="standin", choices=("standin", "real"), help="real: the package's NdiSender, group mbp-bench")
    parser.add_argument("--receiver", action="store_true", help="with --ndi real: also receive through NDI (bench_ndi_receive.py)")
    parser.add_argument("--ffmpeg", type=Path, default=DEFAULT_FFMPEG if DEFAULT_FFMPEG.is_file() else None)
    parser.add_argument("--no-app-setup", action="store_true", help="skip the 1 ms timer and below-normal priority that app.main sets")
    parser.add_argument("--compose-method", default="_compose_unlocked", help="engine method timed per call")
    parser.add_argument("--out", help="results JSON (default %%TEMP%%\\mbp-bench\\results\\<label>_<time>.json)")
    parser.add_argument("--results", help="folder for results")
    parser.add_argument("--allow-live-folder", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--compare", nargs="+", metavar="JSON", help="print results files side by side")
    parser.add_argument("--table", metavar="JSON", help="print the tables of a results file")
    parser.add_argument("--reanalyse", metavar="JSON", help="analyse a results file's saved frame records again (writes --out, or in place)")
    parser.add_argument("--selftest", action="store_true", help="check the stand-in and analysis on synthetic BGRA and NV12 frames")
    args = parser.parse_args()
    if args.selftest:
        return run_selftest()
    if args.compare:
        return run_compare(args.compare)
    if args.table:
        print_tables(json.loads(Path(args.table).read_text(encoding="utf-8"))["runs"])
        return 0
    if args.reanalyse:
        return run_reanalyse(Path(args.reanalyse), args.media, Path(args.out) if args.out else None)
    if args.receiver and args.ndi != "real":
        raise SystemExit("--receiver needs --ndi real")
    return run_driver(args)


if __name__ == "__main__":
    sys.exit(main())
