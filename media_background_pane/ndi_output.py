from __future__ import annotations

import os
import threading
from typing import Optional

os.environ.setdefault("NDI_DISABLE_HARDWARE_ACCELERATION", "1")

import NDIlib as ndi
import numpy as np
from PySide6.QtGui import QImage

from .config import OUTPUT_HEIGHT, OUTPUT_WIDTH
from .frames import black, nv12_shape


def qimage_to_array(image: QImage, dest: np.ndarray) -> None:
    """Copy a QImage into a BGRA array of shape (height, width, 4)."""
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
    """Sends NV12 frames over NDI, without waiting for NDI to compress them.

    NDI keeps reading a frame sent with send_send_video_async_v2 until the next
    send. So each frame is copied into one of two buffers this object owns, and
    a buffer is only reused, or replaced, once NDI has moved on to the other.
    """

    def __init__(self, name: str, fps: int, width: int = OUTPUT_WIDTH, height: int = OUTPUT_HEIGHT) -> None:
        self.name = name
        self.fps = max(1, fps)
        self.width = width
        self.height = height
        self.available = False
        self.status = "NDI runtime not found"
        self._send = None
        self._frame: Optional[ndi.VideoFrameV2] = None
        self._bufs = [black(width, height), black(width, height)]
        self._buf_i = 0
        self._lock = threading.Lock()
        self._open()

    def _rate(self) -> tuple[int, int]:
        table = {24: (24, 1), 25: (25, 1), 30: (30000, 1000), 50: (50, 1), 60: (60000, 1000)}
        return table.get(self.fps, (self.fps, 1))

    def _set_frame_fields(self) -> None:
        # The ndi-python data setter sets xres, yres and line_stride_in_bytes
        # from the array's shape. An NV12 array has 1.5x as many rows as the
        # picture, so NDI would read past the end of the buffer: on 5 Nov 2026
        # that crashed a test process. Set them again after every data =.
        frame = self._frame
        if frame is None:
            return
        frame.FourCC = ndi.FOURCC_VIDEO_TYPE_NV12
        frame.xres = self.width
        frame.yres = self.height
        frame.line_stride_in_bytes = self.width

    def _configure_frame(self) -> None:
        if self._frame is None:
            return
        rate_n, rate_d = self._rate()
        self._frame.data = self._bufs[0]
        self._set_frame_fields()
        self._frame.frame_rate_N = rate_n
        self._frame.frame_rate_D = rate_d
        self._frame.picture_aspect_ratio = self.width / max(1, self.height)
        self._frame.frame_format_type = ndi.FRAME_FORMAT_TYPE_PROGRESSIVE
        # Let NDI stamp each frame with the time it was sent. A fixed 0 made
        # every frame look alike to anything that orders frames by timecode.
        self._frame.timecode = ndi.SEND_TIMECODE_SYNTHESIZE

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
        except OSError as exc:
            self.status = f"NDI error: {exc}"

    def _flush(self) -> None:
        """Wait until NDI has finished with the last frame. Call with the lock held."""
        if self._send is None:
            return
        try:
            ndi.send_send_video_async_v2(self._send, None)
        except (OSError, TypeError):
            pass

    def set_fps(self, fps: int) -> None:
        with self._lock:
            self.fps = max(1, fps)
            self._flush()
            self._configure_frame()

    def set_size(self, width: int, height: int) -> None:
        if width == self.width and height == self.height:
            return
        with self._lock:
            # NDI may still be reading the old buffer; let it finish before it goes.
            self._flush()
            self.width = width
            self.height = height
            self._bufs = [black(width, height), black(width, height)]
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

    def submit_array(self, array: np.ndarray) -> None:
        """Send one NV12 frame of the current size. Other sizes are skipped."""
        if not self.available or self._send is None or self._frame is None:
            return
        with self._lock:
            if array.shape != nv12_shape(self.width, self.height):
                return
            sendbuf = self._bufs[self._buf_i]
            np.copyto(sendbuf, array)
            self._buf_i = 1 - self._buf_i
            self._frame.data = sendbuf
            self._set_frame_fields()
            ndi.send_send_video_async_v2(self._send, self._frame)

    def close(self) -> None:
        with self._lock:
            if self._send is not None:
                self._flush()
                ndi.send_destroy(self._send)
                self._send = None
            self.available = False
