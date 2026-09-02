from __future__ import annotations

import os
import threading
import time
from typing import Optional

os.environ.setdefault("NDI_DISABLE_HARDWARE_ACCELERATION", "1")

import NDIlib as ndi
import numpy as np
from PySide6.QtGui import QImage

from .config import OUTPUT_HEIGHT, OUTPUT_WIDTH


def qimage_to_array(image: QImage, dest: np.ndarray) -> None:
    converted = image
    if converted.format() != QImage.Format.Format_RGB32:
        converted = converted.convertToFormat(QImage.Format.Format_RGB32)
    width = converted.width()
    height = converted.height()
    stride = converted.bytesPerLine()
    bits = converted.constBits()
    src = np.frombuffer(bits, dtype=np.uint8, count=converted.sizeInBytes())
    dest_h, dest_w = dest.shape[:2]
    copy_h = min(height, dest_h)
    copy_w = min(width, dest_w)
    if copy_h != dest_h or copy_w != dest_w:
        dest.fill(0)
    if stride == width * 4 and copy_h == dest_h and copy_w == dest_w:
        dest.reshape(-1)[:] = src[: dest_w * dest_h * 4]
        return
    src2d = src.reshape(height, stride)
    dest[:copy_h, :copy_w] = src2d[:copy_h, : copy_w * 4].reshape(copy_h, copy_w, 4)


class NdiSender:
    def __init__(self, name: str, fps: int, width: int = OUTPUT_WIDTH, height: int = OUTPUT_HEIGHT) -> None:
        self.name = name
        self.fps = max(1, fps)
        self.width = width
        self.height = height
        self.available = False
        self.status = "NDI runtime not found"
        self._send = None
        self._frame: Optional[ndi.VideoFrameV2] = None
        self._staging = np.zeros((height, width, 4), dtype=np.uint8)
        self._bufs = [
            np.zeros((height, width, 4), dtype=np.uint8),
            np.zeros((height, width, 4), dtype=np.uint8),
        ]
        self._buf_i = 0
        self._lock = threading.Lock()
        self._dirty = False
        self._running = False
        self._thread: threading.Thread | None = None
        self._open()

    def _rate(self) -> tuple[int, int]:
        table = {24: (24, 1), 25: (25, 1), 30: (30000, 1000), 50: (50, 1), 60: (60000, 1000)}
        return table.get(self.fps, (self.fps, 1))

    def _configure_frame(self) -> None:
        if self._frame is None:
            return
        rate_n, rate_d = self._rate()
        self._frame.data = self._bufs[0]
        self._frame.FourCC = ndi.FOURCC_VIDEO_TYPE_BGRX
        self._frame.xres = self.width
        self._frame.yres = self.height
        self._frame.frame_rate_N = rate_n
        self._frame.frame_rate_D = rate_d
        self._frame.picture_aspect_ratio = 16.0 / 9.0
        self._frame.frame_format_type = ndi.FRAME_FORMAT_TYPE_PROGRESSIVE
        self._frame.line_stride_in_bytes = self.width * 4
        self._frame.timecode = 0

    def _open(self) -> None:
        try:
            if not ndi.initialize():
                self.status = "NDI initialize failed"
                return
            settings = ndi.SendCreate()
            settings.ndi_name = self.name
            settings.clock_video = False
            settings.clock_audio = False
            send = ndi.send_create(settings)
            if send is None:
                self.status = "NDI sender create failed"
                return
            self._send = send
            self._frame = ndi.VideoFrameV2()
            self._configure_frame()
            self.available = True
            self.status = f"NDI sending as {self.name} ({self.width}x{self.height})"
            self._running = True
            self._thread = threading.Thread(target=self._run, name="ndi-send", daemon=True)
            self._thread.start()
        except OSError as exc:
            self.status = f"NDI error: {exc}"

    def set_fps(self, fps: int) -> None:
        self.fps = max(1, fps)
        self._configure_frame()

    def set_size(self, width: int, height: int) -> None:
        if width == self.width and height == self.height:
            return
        with self._lock:
            self.width = width
            self.height = height
            self._staging = np.zeros((height, width, 4), dtype=np.uint8)
            self._bufs = [
                np.zeros((height, width, 4), dtype=np.uint8),
                np.zeros((height, width, 4), dtype=np.uint8),
            ]
            self._buf_i = 0
            self._configure_frame()
            self.status = f"NDI sending as {self.name} ({width}x{height})"

    def connection_count(self) -> int:
        if not self.available or self._send is None:
            return 0
        try:
            return int(ndi.send_get_no_connections(self._send, 0))
        except OSError:
            return 0

    def submit(self, image: QImage) -> None:
        if not self.available:
            return
        with self._lock:
            qimage_to_array(image, self._staging)
            self._dirty = True

    def submit_array(self, array: np.ndarray) -> None:
        if not self.available:
            return
        with self._lock:
            if array.shape[0] != self.height or array.shape[1] != self.width:
                return
            np.copyto(self._staging, array)
            self._dirty = True

    def _run(self) -> None:
        next_t = time.perf_counter()
        while self._running:
            with self._lock:
                if self._dirty:
                    np.copyto(self._bufs[self._buf_i], self._staging)
                    self._dirty = False
                    send_i = self._buf_i
                    self._buf_i = 1 - self._buf_i
                else:
                    send_i = 1 - self._buf_i
                sendbuf = self._bufs[send_i]
                fps = self.fps
            if self._frame is not None and self._send is not None:
                self._frame.data = sendbuf
                ndi.send_send_video_async_v2(self._send, self._frame)
            interval = 1.0 / max(1, fps)
            next_t += interval
            delay = next_t - time.perf_counter()
            if delay > 0:
                threading.Event().wait(delay)
            else:
                next_t = time.perf_counter()

    def close(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._send is not None:
            try:
                ndi.send_send_video_async_v2(self._send, None)
            except (OSError, TypeError):
                pass
            ndi.send_destroy(self._send)
            self._send = None
        self.available = False
