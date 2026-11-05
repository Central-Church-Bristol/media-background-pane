import sys
import threading
import time
import unittest
from pathlib import Path
from queue import Queue
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from PySide6.QtCore import QCoreApplication, Qt

from media_background_pane import hw_decoder
from media_background_pane.hw_decoder import HwDecoder

WIDTH, HEIGHT = 4, 2


class FakeFfmpeg:
    """An ffmpeg that always has the next frame ready, far faster than real time.

    With hang_after set, reads block after that many frames, like a read from a
    Google Drive file that has stopped answering. Only kill() releases them.
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
    """Runs the real HwDecoder._play_mode loop against FakeFfmpeg.

    Records when each frame is handed to the queue and how often frame_ready fires.
    Nothing takes frames from the queue unless a test does.
    """

    def __init__(self, fps: int, proc: FakeFfmpeg | None = None) -> None:
        self.proc = proc or FakeFfmpeg()
        self.decoder = HwDecoder("pacing-test")
        decoder = self.decoder
        decoder._path = Path("clip.mp4")
        decoder._width, decoder._height, decoder._fps = WIDTH, HEIGHT, fps
        decoder._queue = Queue(maxsize=2)
        decoder._running = True
        decoder._generation = 1
        self.handed_on: list[float] = []
        self.signals = 0
        self.result: bool | None = None
        enqueue = decoder._enqueue

        def recording_enqueue(frame: np.ndarray) -> bool:
            self.handed_on.append(time.perf_counter())
            return enqueue(frame)

        decoder._enqueue = recording_enqueue
        decoder.frame_ready.connect(self._count_signal, Qt.ConnectionType.DirectConnection)
        self._patches = [
            mock.patch.object(hw_decoder, "find_ffmpeg", return_value=Path("ffmpeg.exe")),
            mock.patch.object(hw_decoder, "_assign_job"),
            mock.patch.object(hw_decoder.subprocess, "Popen", return_value=self.proc),
        ]
        self.thread = threading.Thread(target=self._play, daemon=True)
        self.started = 0.0

    def _count_signal(self) -> None:
        self.signals += 1

    def _play(self) -> None:
        self.result = self.decoder._play_mode("software", 1)

    def __enter__(self) -> "ReaderRun":
        for patch in self._patches:
            patch.start()
        self.started = time.perf_counter()
        self.thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.decoder._running = False
        self.thread.join(2.0)
        for patch in reversed(self._patches):
            patch.stop()

    def frames_between(self, start: float, end: float) -> int:
        return sum(1 for t in list(self.handed_on) if start <= t < end)


class PacingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QCoreApplication.instance() or QCoreApplication([])

    def test_frames_are_handed_on_at_the_output_rate(self) -> None:
        # The queue drops frames instead of blocking, so it no longer slows the
        # reader. Without its own pacing the reader took frames as fast as
        # ffmpeg decoded them and clips played fast: about 2.7x on Holy Fire.
        fps = 40
        with ReaderRun(fps) as run:
            time.sleep(1.0)
            end = time.perf_counter()
        count = run.frames_between(run.started, end)
        expected = 1 + fps * (end - run.started)
        self.assertGreater(count, expected * 0.8)
        self.assertLess(count, expected * 1.2)
        gaps = np.diff(run.handed_on[1:])
        self.assertAlmostEqual(float(np.median(gaps)), 1.0 / fps, delta=0.2 / fps)
        # frame_ready still fires once per frame, so the GUI thread gets fps
        # signals a second, not one per decoded frame.
        self.assertLessEqual(abs(run.signals - len(run.handed_on)), 1)

    def test_a_stalled_consumer_does_not_block_the_reader(self) -> None:
        # Nothing takes frames, as when the compositor has died. The reader
        # has to keep going at the same speed and drop old frames, so ffmpeg is
        # never left blocked on a full pipe with the picture frozen.
        fps = 50
        with ReaderRun(fps) as run:
            time.sleep(0.8)
            alive = run.thread.is_alive()
            count = len(run.handed_on)
            queued = run.decoder._queue.qsize()
        self.assertTrue(alive)
        # A reader blocked on the full queue would stop at 3 frames.
        self.assertGreater(count, 0.8 * fps * 0.7)
        self.assertLess(count, (1 + 0.8 * fps) * 1.2)
        self.assertLessEqual(queued, 2)
        latest = run.decoder._latest
        assert latest is not None
        self.assertEqual(int(latest[0, 0, 0]), run.proc.frames % 256)

    def test_resuming_after_a_pause_does_not_rush_to_catch_up(self) -> None:
        fps = 40
        with ReaderRun(fps) as run:
            time.sleep(0.3)
            run.decoder.set_paused(True)
            paused_at = time.perf_counter()
            time.sleep(0.6)
            resumed_at = time.perf_counter()
            run.decoder.set_paused(False)
            time.sleep(0.5)
            end = time.perf_counter()
        # One frame may already be on its way when the pause lands.
        self.assertEqual(run.frames_between(paused_at + 2.0 / fps, resumed_at), 0)
        after = run.frames_between(resumed_at, end)
        expected = 1 + fps * (end - resumed_at)
        # Catching up on the 0.6 s spent paused would add another 24 frames.
        self.assertGreater(after, expected * 0.7)
        self.assertLess(after, expected * 1.2)

    def test_watchdog_spares_a_paced_reader_and_still_kills_a_hung_one(self) -> None:
        # At a low output rate the reader waits a long time between frames.
        # That wait is one frame interval. The real _STALL_TIMEOUT is 8 s and
        # the slowest rate is 1 fps, so the watchdog must never take it for a
        # stall. A smaller timeout here keeps the test short.
        with mock.patch.object(hw_decoder, "_STALL_TIMEOUT", 0.6):
            with ReaderRun(fps=5) as run:
                time.sleep(1.6)
                killed = run.proc.killed
                count = len(run.handed_on)
        self.assertFalse(killed)
        self.assertGreaterEqual(count, 7)

        # A read that hangs after frames have been flowing (Drive dropping out)
        # still gets ffmpeg killed so the clip can be opened again.
        # Only the watchdog can release the hung read while the run is still on.
        with mock.patch.object(hw_decoder, "_STALL_TIMEOUT", 0.5):
            with ReaderRun(fps=25, proc=FakeFfmpeg(hang_after=5)) as run:
                run.thread.join(3.0)
                finished = not run.thread.is_alive()
                killed = run.proc.killed
        self.assertTrue(killed)
        self.assertTrue(finished)
        self.assertTrue(run.result)


if __name__ == "__main__":
    unittest.main()
