from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
import zipfile
from collections import deque
from pathlib import Path
from typing import Callable, Optional
from urllib.request import Request, urlopen

import numpy as np
from PySide6.QtCore import QObject, Signal

from .config import SCALE_ACTUAL, SCALE_FILL, SCALE_MODES
from .frames import copy_into, nv12_shape
from .log import log_exception

CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JobObjectExtendedLimitInformation = 9

FFMPEG_ZIP_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
_FIRST_FRAME_TIMEOUT = 3.0
# A read that waits this long for ffmpeg, while there is room for the frame,
# is a stuck ffmpeg or a hung media file (Google Drive dropping out). Kill it
# and open the clip again. Waiting for the compositor to take frames, or being
# paused, never counts: that is the normal way playback is paced.
_STALL_TIMEOUT = 8.0
# Frames decoded ahead of the compositor: about half a second, so a slow read
# from Google Drive or the jump back at a loop point does not show.
_MIN_DEPTH = 4
_MAX_DEPTH = 16

_ffmpeg_path: Optional[Path] = None
_has_scale_d3d11: Optional[bool] = None
_job_handle = None


def vendor_ffmpeg_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "vendor" / "ffmpeg"


def _kill_proc(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:
            pass


def find_ffmpeg() -> Optional[Path]:
    global _ffmpeg_path
    if _ffmpeg_path is not None and _ffmpeg_path.is_file():
        return _ffmpeg_path
    candidates: list[Path] = []
    root = vendor_ffmpeg_dir()
    candidates.append(root / "ffmpeg.exe")
    candidates.append(root / "bin" / "ffmpeg.exe")
    which = shutil.which("ffmpeg")
    if which:
        candidates.append(Path(which))
    candidates.append(Path(r"C:\ffmpeg\bin\ffmpeg.exe"))
    for path in candidates:
        if path.is_file():
            _ffmpeg_path = path
            return path
    return None


def ensure_ffmpeg() -> Optional[Path]:
    existing = find_ffmpeg()
    if existing is not None:
        print(f"ffmpeg: {existing}")
        return existing
    dest_dir = vendor_ffmpeg_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    zip_path = dest_dir / "ffmpeg-release-essentials.zip"
    exe_path = dest_dir / "ffmpeg.exe"
    print("Downloading ffmpeg (one-time, GPU decode)...")
    try:
        request = Request(
            FFMPEG_ZIP_URL,
            headers={"User-Agent": "MediaBackgroundPane/1.0"},
        )
        with urlopen(request, timeout=120) as response, zip_path.open("wb") as out:
            shutil.copyfileobj(response, out)
        with zipfile.ZipFile(zip_path) as archive:
            names = [
                info.filename
                for info in archive.infolist()
                if Path(info.filename).name.lower() == "ffmpeg.exe" and not info.is_dir()
            ]
            if not names:
                print("ffmpeg: zip did not contain ffmpeg.exe")
                return None
            names.sort(key=len)
            with archive.open(names[0]) as src, exe_path.open("wb") as out:
                shutil.copyfileobj(src, out)
        try:
            zip_path.unlink()
        except OSError:
            pass
    except (OSError, zipfile.BadZipFile, TimeoutError, ValueError) as exc:
        print(f"ffmpeg: download failed ({exc})")
        return None
    if exe_path.is_file():
        global _ffmpeg_path
        _ffmpeg_path = exe_path
        print(f"ffmpeg: {exe_path}")
        warmup_ffmpeg()
        return exe_path
    print("ffmpeg: extract failed")
    return None


def warmup_ffmpeg() -> None:
    """Run ffmpeg once, so the first clip does not wait for it to load from disk."""
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        return
    try:
        subprocess.run(
            [str(ffmpeg), "-hide_banner", "-version"],
            capture_output=True,
            timeout=8,
            creationflags=_creation_flags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def _creation_flags() -> int:
    if sys.platform != "win32":
        return 0
    return CREATE_NO_WINDOW | CREATE_BREAKAWAY_FROM_JOB


def _ensure_job():
    global _job_handle
    if sys.platform != "win32":
        return None
    if _job_handle:
        return _job_handle
    import ctypes
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        return None
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        handle,
        JobObjectExtendedLimitInformation,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        kernel32.CloseHandle(handle)
        return None
    _job_handle = handle
    return _job_handle


def _assign_job(proc: subprocess.Popen) -> None:
    handle = _ensure_job()
    if handle is None or sys.platform != "win32":
        return
    raw = getattr(proc, "_handle", None)
    if not raw:
        return
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    kernel32.AssignProcessToJobObject(handle, int(raw))


def _has_filter(name: str) -> bool:
    global _has_scale_d3d11
    if name == "scale_d3d11" and _has_scale_d3d11 is not None:
        return _has_scale_d3d11
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        return False
    try:
        result = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=_creation_flags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        if name == "scale_d3d11":
            _has_scale_d3d11 = False
        return False
    found = name in (result.stdout or "")
    if name == "scale_d3d11":
        _has_scale_d3d11 = found
    return found


def queue_depth(fps: int) -> int:
    """How many decoded frames wait for the compositor: about half a second."""
    return max(_MIN_DEPTH, min(_MAX_DEPTH, int(fps) // 2))


def _crop_pad(width: int, height: int) -> str:
    # Even sizes and offsets, so the half-resolution chroma lines up.
    crop = (
        "crop=trunc(min(iw\\," + str(width) + ")/2)*2:trunc(min(ih\\," + str(height) + ")/2)*2"
    )
    return f"{crop},pad={width}:{height}:trunc((ow-iw)/4)*2:trunc((oh-ih)/4)*2:black"


def _vf(width: int, height: int, fps: int, mode: str, flags: str) -> str:
    """The ffmpeg filter chain: rate first, then size, then NV12 in BT.709.

    fps comes first so frames it drops are never scaled. When the clip is
    already the output size, scale and crop pass frames straight through at
    no cost (measured 5 Nov 2026). out_color_matrix converts clips tagged with
    another matrix (SD BT.601, say); BT.709 clips pass through unchanged.
    """
    rate = f"fps={max(1, fps)}"
    if mode == SCALE_FILL:
        size = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase:force_divisible_by=2:"
            f"flags={flags}:out_color_matrix=bt709,crop={width}:{height}"
        )
    elif mode == SCALE_ACTUAL:
        size = f"scale=out_color_matrix=bt709,{_crop_pad(width, height)}"
    else:
        size = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease:force_divisible_by=2:"
            f"flags={flags}:out_color_matrix=bt709,"
            f"pad={width}:{height}:trunc((ow-iw)/4)*2:trunc((oh-ih)/4)*2:black"
        )
    return f"{rate},{size},format=nv12"


class _Frames:
    """The decoded frames of one clip start: a pool of buffers and a queue in order.

    The reader takes a free buffer, fills it from ffmpeg and queues it. When
    `depth` frames are queued it waits for the compositor to take one. That
    wait is what paces playback: ffmpeg decodes ahead, then waits on the pipe.
    """

    def __init__(self, shape: tuple[int, int], depth: int) -> None:
        self.shape = shape
        self.depth = depth
        self.cond = threading.Condition()
        self.ready: deque[np.ndarray] = deque()
        self.free: list[np.ndarray] = []
        self.made = 0
        self.closed = False
        # When the reader began waiting on ffmpeg for the frame it is reading.
        # None while it waits for the compositor instead. The stall watch reads it.
        self.reading_since: float | None = None

    def take_free(self, keep_going: Callable[[], bool]) -> np.ndarray | None:
        with self.cond:
            while not self.closed and keep_going():
                if len(self.ready) < self.depth:
                    if self.free:
                        return self.free.pop()
                    if self.made < self.depth + 2:
                        self.made += 1
                        return np.empty(self.shape, dtype=np.uint8)
                self.cond.wait(0.1)
            return None

    def put(self, buffer: np.ndarray) -> bool:
        with self.cond:
            if self.closed:
                return False
            self.ready.append(buffer)
            self.cond.notify_all()
            return True

    def give_back(self, buffer: np.ndarray) -> None:
        with self.cond:
            if not self.closed and buffer.shape == self.shape:
                self.free.append(buffer)
            self.cond.notify_all()

    def pop(self, timeout: float) -> np.ndarray | None:
        with self.cond:
            if not self.ready and timeout > 0 and not self.closed:
                self.cond.wait_for(lambda: bool(self.ready) or self.closed, timeout)
            if not self.ready:
                return None
            return self.ready.popleft()

    def queued(self) -> int:
        with self.cond:
            return len(self.ready)

    def drain(self) -> None:
        with self.cond:
            while self.ready:
                self.free.append(self.ready.popleft())
            self.cond.notify_all()

    def close(self) -> None:
        with self.cond:
            self.closed = True
            self.ready.clear()
            self.free.clear()
            self.cond.notify_all()


class HwDecoder(QObject):
    """Plays one clip through ffmpeg and hands its frames on in order, as NV12.

    It has no clock of its own. The compositor takes one frame per output
    tick, and the decoder keeps up to half a second decoded ahead of it.
    """

    frame_ready = Signal()
    status_changed = Signal(str)

    def __init__(self, name: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.name = name
        self.status = "stopped"
        self._path: Optional[Path] = None
        self._width = 960
        self._height = 540
        self._fps = 24
        self._want_gpu = True
        self._scale_mode = SCALE_FILL
        self._running = False
        self._paused = False
        self._frames: _Frames | None = None
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._generation = 0
        # Frames read since the last start(). The engine's watchdog uses it.
        self.frames_read = 0

    @property
    def paused(self) -> bool:
        return self._paused

    def thread_alive(self) -> bool:
        thread = self._thread
        if thread is None:
            return False
        try:
            return thread.is_alive()
        except Exception:
            # On 5 Nov 2026 the running pane's Thread objects started failing
            # their own checks. Say "alive": the frame count still shows a stall.
            return True

    def queued(self) -> int:
        frames = self._frames
        return frames.queued() if frames is not None else 0

    def pop_frame(self, dest: np.ndarray, timeout: float = 0.0) -> bool:
        """Copy the next frame, in order, into dest. False if none came within timeout."""
        frames = self._frames
        if frames is None:
            return False
        buffer = frames.pop(max(0.0, timeout))
        if buffer is None:
            return False
        try:
            copy_into(dest, buffer)
        finally:
            frames.give_back(buffer)
        return True

    def copy_frame(self, dest: np.ndarray) -> bool:
        """Copy the newest decoded frame without taking it. Breaks continuity: avoid."""
        frames = self._frames
        if frames is None:
            return False
        with frames.cond:
            if not frames.ready:
                return False
            copy_into(dest, frames.ready[-1])
        return True

    def discard_queued(self) -> None:
        frames = self._frames
        if frames is not None:
            frames.drain()

    def start(
        self,
        path: Path,
        width: int,
        height: int,
        fps: int,
        gpu: bool,
        paused: bool = False,
        scale_mode: str = SCALE_FILL,
    ) -> None:
        self.stop()
        self._path = path
        self._width = max(2, int(width) // 2 * 2)
        self._height = max(2, int(height) // 2 * 2)
        self._fps = max(1, fps)
        self._want_gpu = gpu
        self._scale_mode = scale_mode if scale_mode in SCALE_MODES else SCALE_FILL
        self._paused = paused
        frames = _Frames(nv12_shape(self._width, self._height), queue_depth(self._fps))
        self._frames = frames
        self._running = True
        self._generation += 1
        generation = self._generation
        self.frames_read = 0
        self._set_status("starting")
        self._thread = threading.Thread(
            target=self._run,
            name=f"ffmpeg-{self.name}",
            args=(generation, frames),
            daemon=True,
        )
        self._thread.start()

    def restart(
        self,
        width: int | None = None,
        height: int | None = None,
        fps: int | None = None,
        scale_mode: str | None = None,
    ) -> None:
        if self._path is None:
            return
        self.start(
            self._path,
            width if width is not None else self._width,
            height if height is not None else self._height,
            fps if fps is not None else self._fps,
            self._want_gpu,
            paused=self._paused,
            scale_mode=scale_mode if scale_mode is not None else self._scale_mode,
        )

    def set_paused(self, paused: bool) -> None:
        # Paused means nobody is taking frames. The queue is kept, so playing
        # again carries on with the very next frame; the reader simply waits
        # once the queue is full, and ffmpeg waits on the pipe behind it.
        self._paused = paused

    def stop(self) -> threading.Thread | None:
        # A GPU ffmpeg often takes about a second to die. Waiting here stalls
        # the click that is trying to put the next clip on Preview.
        self._running = False
        self._paused = False
        frames = self._frames
        if frames is not None:
            frames.close()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        thread = self._thread
        self._thread = None
        self._set_status("stopped")
        if thread is None or thread is threading.current_thread():
            return None
        return thread

    def _set_status(self, status: str) -> None:
        if self.status == status:
            return
        self.status = status
        self.status_changed.emit(status)

    def _current(self, generation: int) -> bool:
        return self._running and generation == self._generation

    def _run(self, generation: int, frames: _Frames) -> None:
        try:
            while self._current(generation):
                try:
                    if not self._run_attempts(generation, frames):
                        return
                except Exception as exc:
                    log_exception(f"decoder {self.name}", exc)
                if not self._wait_retry(1.0, generation):
                    return
        except BaseException as exc:
            # Anything else ends this thread. Log it here; the engine's
            # watchdog starts the clip again.
            log_exception(f"decoder {self.name} thread", exc)

    def _run_attempts(self, generation: int, frames: _Frames) -> bool:
        """Try each decode mode once. True means keep trying while this clip is current."""
        attempts = ["gpu", "software"] if self._want_gpu else ["software"]
        produced = False
        for mode in attempts:
            if not self._current(generation):
                return False
            if self._play_mode(mode, generation, frames):
                produced = True
                break
        if not self._current(generation):
            return False
        if not produced:
            self._set_status("Software fallback")
        return True

    def _wait_retry(self, seconds: float, generation: int) -> bool:
        deadline = time.monotonic() + seconds
        while self._current(generation) and time.monotonic() < deadline:
            time.sleep(0.05)
        return self._current(generation)

    def _play_mode(self, mode: str, generation: int, frames: _Frames) -> bool:
        ffmpeg = find_ffmpeg()
        path = self._path
        if ffmpeg is None or path is None:
            return False
        cmd = self._command(ffmpeg, path, mode)
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                bufsize=0,
                creationflags=_creation_flags(),
            )
        except OSError:
            return False
        _assign_job(proc)
        self._proc = proc
        got_frame = threading.Event()

        def keep_going() -> bool:
            return self._current(generation)

        threading.Thread(
            target=self._watch_playback,
            name=f"ffmpeg-watch-{self.name}",
            args=(proc, generation, got_frame, frames),
            daemon=True,
        ).start()
        try:
            while self._current(generation):
                buffer = frames.take_free(keep_going)
                if buffer is None:
                    break
                frames.reading_since = time.monotonic()
                try:
                    complete = self._read_into(proc, buffer)
                finally:
                    frames.reading_since = None
                if not complete or not self._current(generation):
                    frames.give_back(buffer)
                    break
                if not frames.put(buffer):
                    break
                self.frames_read += 1
                if not got_frame.is_set():
                    got_frame.set()
                    self._set_status("GPU (D3D11VA)" if mode == "gpu" else "Software fallback")
                if self._current(generation):
                    self.frame_ready.emit()
            return got_frame.is_set()
        finally:
            if self._proc is proc:
                self._proc = None
            if proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    pass

    def _command(self, ffmpeg: Path, path: Path, mode: str) -> list[str]:
        cmd = [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-stream_loop",
            "-1",
        ]
        if mode == "gpu":
            # Decoded on the GPU and handed back as NV12, the format it decodes to.
            cmd += ["-hwaccel", "d3d11va"]
            flags = "bicubic"
        else:
            flags = "bilinear"
        cmd += [
            "-i",
            str(path),
            "-an",
            "-map",
            "0:v:0",
            "-vf",
            _vf(self._width, self._height, self._fps, self._scale_mode, flags),
            "-f",
            "rawvideo",
            "-pix_fmt",
            "nv12",
            "pipe:1",
        ]
        return cmd

    def _watch_playback(
        self,
        proc: subprocess.Popen,
        generation: int,
        got_frame: threading.Event,
        frames: _Frames,
    ) -> None:
        if not got_frame.wait(_FIRST_FRAME_TIMEOUT) and proc.poll() is None:
            _kill_proc(proc)
            return
        while proc.poll() is None and self._current(generation):
            time.sleep(0.25)
            since = frames.reading_since
            if since is not None and time.monotonic() - since > _STALL_TIMEOUT:
                _kill_proc(proc)
                return

    def _read_into(self, proc: subprocess.Popen, buffer: np.ndarray) -> bool:
        """Fill buffer with one frame from ffmpeg. False at the end of the pipe."""
        stdout = proc.stdout
        if stdout is None:
            return False
        view = memoryview(buffer).cast("B")
        size = len(view)
        readinto = getattr(stdout, "readinto", None)
        got = 0
        while got < size:
            if not self._running:
                return False
            if readinto is not None:
                count = readinto(view[got:])
            else:
                chunk = stdout.read(size - got)
                count = len(chunk) if chunk else 0
                if count:
                    view[got : got + count] = chunk
            if not count:
                return False
            got += count
        return True
