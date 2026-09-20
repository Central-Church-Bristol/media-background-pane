from __future__ import annotations

import re
import shutil
import subprocess
import sys
import threading
import zipfile
from queue import Empty, Full, Queue
from pathlib import Path
from typing import Optional
from urllib.request import Request, urlopen

import numpy as np
from PySide6.QtCore import QObject, Signal

from .config import SCALE_ACTUAL, SCALE_FILL, SCALE_MODES

CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JobObjectExtendedLimitInformation = 9

FFMPEG_ZIP_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
_FIRST_FRAME_TIMEOUT = 3.0
_SIZE_RE = re.compile(r"Video:\s*.*?(\d{2,5})x(\d{2,5})")

_ffmpeg_path: Optional[Path] = None
_has_scale_d3d11: Optional[bool] = None
_job_handle = None


def vendor_ffmpeg_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "vendor" / "ffmpeg"


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
    if find_ffmpeg() is None:
        return
    _has_filter("scale_d3d11")


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
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
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
    kernel32.AssignProcessToJobObject(handle, raw)


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


def _probe_size(path: Path) -> Optional[tuple[int, int]]:
    ffmpeg = find_ffmpeg()
    if ffmpeg is None:
        return None
    try:
        result = subprocess.run(
            [str(ffmpeg), "-hide_banner", "-i", str(path)],
            capture_output=True,
            timeout=8,
            creationflags=_creation_flags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (result.stderr or b"").decode("utf-8", "replace")
    match = _SIZE_RE.search(text)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _fit_inside(src_w: int, src_h: int, out_w: int, out_h: int) -> tuple[int, int]:
    scale = min(out_w / max(1, src_w), out_h / max(1, src_h))
    width = max(2, int(src_w * scale) // 2 * 2)
    height = max(2, int(src_h * scale) // 2 * 2)
    return width, height


def _cover_size(src_w: int, src_h: int, out_w: int, out_h: int) -> tuple[int, int]:
    scale = max(out_w / max(1, src_w), out_h / max(1, src_h))
    width = max(2, int(round(src_w * scale / 2.0)) * 2)
    height = max(2, int(round(src_h * scale / 2.0)) * 2)
    if width < out_w or height < out_h:
        extra = max(out_w / max(1, width), out_h / max(1, height))
        width = max(out_w, int(round(width * extra / 2.0)) * 2)
        height = max(out_h, int(round(height * extra / 2.0)) * 2)
    return width, height


def _crop_pad(width: int, height: int) -> str:
    crop = "crop=min(iw\\," + str(width) + "):min(ih\\," + str(height) + ")"
    return f"{crop},pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"


def _vf_cpu(width: int, height: int, fps: int, flags: str, mode: str) -> str:
    if mode == SCALE_FILL:
        scale = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase:flags={flags},"
            f"crop={width}:{height},"
        )
    elif mode == SCALE_ACTUAL:
        scale = _crop_pad(width, height) + ","
    else:
        scale = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease:flags={flags},"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,"
        )
    return f"{scale}format=bgra,fps={max(1, fps)}"


def _vf_d3d11(
    src_w: int,
    src_h: int,
    width: int,
    height: int,
    fps: int,
    mode: str,
) -> str:
    tail = f"fps={max(1, fps)}"
    if mode == SCALE_FILL:
        cover_w, cover_h = _cover_size(src_w, src_h, width, height)
        return (
            f"scale_d3d11={cover_w}:{cover_h},"
            f"hwdownload,format=bgra,{_crop_pad(width, height)},{tail}"
        )
    if mode == SCALE_ACTUAL:
        return f"hwdownload,format=bgra,{_crop_pad(width, height)},{tail}"
    fit_w, fit_h = _fit_inside(src_w, src_h, width, height)
    return (
        f"scale_d3d11={fit_w}:{fit_h},"
        f"hwdownload,format=bgra,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,{tail}"
    )


class HwDecoder(QObject):
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
        self._unpause = threading.Event()
        self._unpause.set()
        self._lock = threading.Lock()
        self._latest: Optional[np.ndarray] = None
        self._queue: Queue = Queue(maxsize=2)
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._generation = 0

    def copy_frame(self, dest: np.ndarray) -> bool:
        with self._lock:
            if self._latest is None:
                return False
            if self._latest.shape != dest.shape:
                dest.fill(0)
                dest[..., 3] = 255
                h = min(self._latest.shape[0], dest.shape[0])
                w = min(self._latest.shape[1], dest.shape[1])
                dest[:h, :w] = self._latest[:h, :w]
                return True
            np.copyto(dest, self._latest)
            return True

    def discard_queued(self) -> None:
        self._drain_queue(keep_latest=True)

    def pop_frame(self, dest: np.ndarray, timeout: float = 0.0) -> bool:
        try:
            frame = self._queue.get(timeout=max(0.0, timeout))
        except Empty:
            return False
        if frame is None:
            return False
        if frame.shape != dest.shape:
            dest.fill(0)
            dest[..., 3] = 255
            h = min(frame.shape[0], dest.shape[0])
            w = min(frame.shape[1], dest.shape[1])
            dest[:h, :w] = frame[:h, :w]
            return True
        np.copyto(dest, frame)
        dest[..., 3] = 255
        return True

    def _drain_queue(self, keep_latest: bool = False) -> None:
        last = None
        while True:
            try:
                item = self._queue.get_nowait()
            except Empty:
                break
            if item is not None:
                last = item
        if keep_latest and last is not None:
            with self._lock:
                self._latest = last

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
        self._width = max(2, width)
        self._height = max(2, height)
        self._fps = max(1, fps)
        self._want_gpu = gpu
        self._scale_mode = scale_mode if scale_mode in SCALE_MODES else SCALE_FILL
        self._paused = paused
        self._queue = Queue(maxsize=2)
        if paused:
            self._unpause.clear()
        else:
            self._unpause.set()
        self._running = True
        self._generation += 1
        generation = self._generation
        self._set_status("starting")
        self._thread = threading.Thread(
            target=self._run,
            name=f"ffmpeg-{self.name}",
            args=(generation,),
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
        self._paused = paused
        if paused:
            self._unpause.clear()
            self._drain_queue(keep_latest=True)
        else:
            self._unpause.set()

    def stop(self) -> None:
        self._running = False
        self._paused = False
        self._unpause.set()
        self._drain_queue()
        try:
            self._queue.put_nowait(None)
        except Full:
            pass
        proc = self._proc
        self._proc = None
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                pass
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None
        self._set_status("stopped")

    def _set_status(self, status: str) -> None:
        if self.status == status:
            return
        self.status = status
        self.status_changed.emit(status)

    def _run(self, generation: int) -> None:
        attempts: list[str] = []
        if self._want_gpu:
            if _has_filter("scale_d3d11"):
                attempts.append("gpu-d3d11")
            attempts.append("gpu")
        attempts.append("software")
        for mode in attempts:
            if not self._running or generation != self._generation:
                return
            if self._play_mode(mode, generation):
                return
        self._set_status("Software fallback")

    def _play_mode(self, mode: str, generation: int) -> bool:
        ffmpeg = find_ffmpeg()
        path = self._path
        if ffmpeg is None or path is None:
            return False
        cmd = self._command(ffmpeg, path, mode)
        if cmd is None:
            return False
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
        frame_size = self._width * self._height * 4
        got_frame = threading.Event()

        def _watchdog() -> None:
            if not got_frame.wait(_FIRST_FRAME_TIMEOUT) and proc.poll() is None:
                try:
                    proc.kill()
                except OSError:
                    pass

        threading.Thread(target=_watchdog, name=f"ffmpeg-watch-{self.name}", daemon=True).start()
        try:
            while self._running and generation == self._generation:
                while self._paused and self._running and generation == self._generation:
                    if not got_frame.is_set():
                        break
                    self._unpause.wait(0.05)
                if not self._running or generation != self._generation:
                    break
                if self._paused and got_frame.is_set():
                    continue
                data = self._read_exact(proc, frame_size)
                if data is None:
                    break
                if len(data) != frame_size:
                    break
                frame = np.frombuffer(data, dtype=np.uint8).reshape(self._height, self._width, 4).copy()
                frame[..., 3] = 255
                with self._lock:
                    self._latest = frame
                if not self._enqueue(frame):
                    break
                if not got_frame.is_set():
                    got_frame.set()
                    gpu = mode.startswith("gpu")
                    self._set_status("GPU (D3D11VA)" if gpu else "Software fallback")
                self.frame_ready.emit()
            if got_frame.is_set():
                return True
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
        return False

    def _command(self, ffmpeg: Path, path: Path, mode: str) -> Optional[list[str]]:
        cmd = [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-stream_loop",
            "-1",
        ]
        vf: Optional[str] = None
        scale_mode = self._scale_mode
        if mode == "gpu-d3d11":
            probed = _probe_size(path)
            if probed is None:
                return None
            cmd += ["-hwaccel", "d3d11va", "-hwaccel_output_format", "d3d11"]
            vf = _vf_d3d11(probed[0], probed[1], self._width, self._height, self._fps, scale_mode)
        elif mode == "gpu":
            cmd += ["-hwaccel", "d3d11va"]
            vf = _vf_cpu(self._width, self._height, self._fps, "lanczos", scale_mode)
        else:
            vf = _vf_cpu(self._width, self._height, self._fps, "fast_bilinear", scale_mode)
        cmd += [
            "-i",
            str(path),
            "-an",
            "-map",
            "0:v:0",
            "-vf",
            vf,
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgra",
            "pipe:1",
        ]
        return cmd

    def _enqueue(self, frame: np.ndarray) -> bool:
        while self._running:
            if self._paused:
                return True
            try:
                self._queue.put(frame, timeout=0.05)
                return True
            except Full:
                continue
        return False

    def _read_exact(self, proc: subprocess.Popen, size: int) -> Optional[bytes]:
        stdout = proc.stdout
        if stdout is None:
            return None
        buf = bytearray()
        while len(buf) < size:
            if not self._running:
                return None
            chunk = stdout.read(size - len(buf))
            if not chunk:
                return None
            buf += chunk
        return bytes(buf)
