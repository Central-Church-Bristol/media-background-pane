"""How the decoder hands frames on (5 Nov 2026 design).

The compositor is the only clock. The decoder keeps up to half a second of
frames decoded ahead, in order, and waits when that queue is full. So:

- playback speed is the compositor's rate, never ffmpeg's;
- no frame is dropped or repeated while the decoder keeps up;
- a pause carries on with the very next frame;
- waiting for the compositor is never taken for a stalled ffmpeg.

Before 5 Nov the decoder paced itself with its own clock and dropped the
oldest frame when its 2-frame queue was full. That clock drifted against the
compositor's, which showed as a frame repeated here and skipped there.
"""

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from PySide6.QtCore import QCoreApplication

from media_background_pane import hw_decoder
from media_background_pane.frames import nv12_shape
from media_background_pane.hw_decoder import HwDecoder

WIDTH, HEIGHT = 4, 2


class FakeFfmpeg:
    """An ffmpeg that always has the next frame ready, far faster than real time.

    Every byte of frame n is n % 256. With hang_after set, reads block after
    that many frames, like a read from a Google Drive file that has stopped
    answering. Only kill() releases them.
    """

    def __init__(self, hang_after: int | None = None) -> None:
        self.stdout = self
        self.frames = 0
        self.hang_after = hang_after
        self._dead = threading.Event()

    @property
    def killed(self) -> bool:
        return self._dead.is_set()

    def read(self, size: int) -> bytes:
        if self.hang_after is not None and self.frames >= self.hang_after:
            self._dead.wait()
        if self._dead.is_set():
            return b""
        self.frames += 1
        return bytes([self.frames % 256]) * size

    def poll(self) -> int | None:
        return 0 if self._dead.is_set() else None

    def kill(self) -> None:
        self._dead.set()

    def wait(self, timeout: float | None = None) -> int:
        return 0


class ReaderRun:
    """Runs the real HwDecoder reader loop against FakeFfmpeg. Tests take frames themselves."""

    def __init__(self, fps: int, proc: FakeFfmpeg | None = None) -> None:
        self.proc = proc or FakeFfmpeg()
        self.decoder = HwDecoder("pacing-test")
        decoder = self.decoder
        decoder._path = Path("clip.mp4")
        decoder._width, decoder._height, decoder._fps = WIDTH, HEIGHT, fps
        decoder._running = True
        decoder._generation = 1
        self.depth = hw_decoder.queue_depth(fps)
        self.frames = hw_decoder._Frames(nv12_shape(WIDTH, HEIGHT), self.depth)
        decoder._frames = self.frames
        self.result: bool | None = None
        self._patches = [
            mock.patch.object(hw_decoder, "find_ffmpeg", return_value=Path("ffmpeg.exe")),
            mock.patch.object(hw_decoder, "_assign_job"),
            mock.patch.object(hw_decoder.subprocess, "Popen", return_value=self.proc),
        ]
        self.thread = threading.Thread(target=self._play, daemon=True)

    def _play(self) -> None:
        self.result = self.decoder._play_mode("software", 1, self.frames)

    def __enter__(self) -> "ReaderRun":
        for patch in self._patches:
            patch.start()
        self.thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.decoder.stop()
        self.thread.join(2.0)
        for patch in reversed(self._patches):
            patch.stop()

    def take(self, timeout: float = 0.5) -> int | None:
        """The next frame's number, or None if none came."""
        dest = np.zeros(nv12_shape(WIDTH, HEIGHT), dtype=np.uint8)
        if not self.decoder.pop_frame(dest, timeout):
            return None
        return int(dest[0, 0])

    def wait_for(self, condition, timeout: float = 2.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if condition():
                return True
            time.sleep(0.01)
        return bool(condition())


class PacingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QCoreApplication.instance() or QCoreApplication([])

    def test_the_reader_fills_the_queue_then_waits(self) -> None:
        # Nothing takes frames, as when Preview is paused or the compositor has
        # died. The reader decodes half a second ahead and then waits; ffmpeg
        # waits on the pipe behind it. Nothing is dropped.
        with ReaderRun(fps=24) as run:
            self.assertTrue(run.wait_for(lambda: run.decoder.frames_read >= run.depth))
            time.sleep(0.3)
            self.assertEqual(run.decoder.frames_read, run.depth)
            self.assertEqual(run.proc.frames, run.depth)
            self.assertEqual(run.decoder.queued(), run.depth)
            self.assertTrue(run.thread.is_alive())
            # The first frame decoded is the first frame handed on.
            self.assertEqual(run.take(), 1)

    def test_frames_come_out_in_order_with_none_skipped(self) -> None:
        with ReaderRun(fps=30) as run:
            taken = [run.take() for _ in range(300)]
        self.assertEqual(taken, [n % 256 for n in range(1, 301)])

    def test_playback_speed_is_set_by_whoever_takes_the_frames(self) -> None:
        # FakeFfmpeg could deliver thousands of frames a second. The reader
        # reads only as fast as frames are taken, plus the queue it keeps.
        rate = 40
        with ReaderRun(fps=24) as run:
            self.assertTrue(run.wait_for(lambda: run.decoder.queued() == run.depth))
            taken = 0
            start = time.perf_counter()
            while time.perf_counter() - start < 1.0:
                self.assertIsNotNone(run.take())
                taken += 1
                time.sleep(1.0 / rate)
            self.assertTrue(run.wait_for(lambda: run.decoder.queued() == run.depth))
            read = run.decoder.frames_read
        self.assertGreater(taken, rate * 0.7)
        self.assertLessEqual(abs(read - (taken + run.depth)), 1)

    def test_a_pause_carries_on_with_the_next_frame(self) -> None:
        with ReaderRun(fps=24) as run:
            seen = [run.take() for _ in range(5)]
            run.decoder.set_paused(True)
            time.sleep(0.5)  # nobody takes frames; the reader tops the queue up and waits
            read_while_paused = run.decoder.frames_read
            time.sleep(0.3)
            self.assertEqual(run.decoder.frames_read, read_while_paused)
            run.decoder.set_paused(False)
            after = [run.take() for _ in range(5)]
        self.assertEqual(seen, [1, 2, 3, 4, 5])
        # Not a jump ahead to what ffmpeg reached, and not a rush to catch up.
        self.assertEqual(after, [6, 7, 8, 9, 10])

    def test_stop_releases_a_reader_waiting_on_a_full_queue(self) -> None:
        run = ReaderRun(fps=24)
        with run:
            self.assertTrue(run.wait_for(lambda: run.decoder.queued() == run.depth))
            started = time.perf_counter()
            run.decoder.stop()
            run.thread.join(1.0)
            self.assertFalse(run.thread.is_alive())
            self.assertLess(time.perf_counter() - started, 0.5)

    def test_waiting_for_the_compositor_is_not_a_stall_but_a_hung_ffmpeg_is(self) -> None:
        # The real _STALL_TIMEOUT is 8 s. A smaller one keeps the test short.
        with mock.patch.object(hw_decoder, "_STALL_TIMEOUT", 0.5):
            with ReaderRun(fps=24) as run:
                time.sleep(1.5)  # queue full, nobody taking frames
                killed = run.proc.killed
                self.assertEqual(run.decoder.frames_read, run.depth)
            self.assertFalse(killed)

            # A read that hangs after frames have been flowing (Drive dropping
            # out) still gets ffmpeg killed so the clip can be opened again.
            with ReaderRun(fps=24, proc=FakeFfmpeg(hang_after=5)) as run:
                run.thread.join(3.0)
                finished = not run.thread.is_alive()
                killed = run.proc.killed
            self.assertTrue(killed)
            self.assertTrue(finished)
            self.assertTrue(run.result)


if __name__ == "__main__":
    unittest.main()
