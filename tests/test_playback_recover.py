import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
from PySide6.QtCore import QCoreApplication

from unittest import mock

from media_background_pane import hw_decoder, log
from media_background_pane.engine import Engine
from media_background_pane.hw_decoder import HwDecoder
from media_background_pane.log import log_exception

_log_dir: tempfile.TemporaryDirectory | None = None
_log_patches: list = []


def setUpModule() -> None:
    # Anything these tests log goes to a temporary folder, never the pane's own session.log.
    global _log_dir
    _log_dir = tempfile.TemporaryDirectory()
    root = Path(_log_dir.name)
    _log_patches[:] = [
        mock.patch.object(log, "session_log_path", return_value=root / "session.log"),
        mock.patch.object(log, "fallback_log_path", return_value=root / "fallback.log"),
    ]
    for patch in _log_patches:
        patch.start()


def tearDownModule() -> None:
    for patch in reversed(_log_patches):
        patch.stop()
    if _log_dir is not None:
        _log_dir.cleanup()


class LogTests(unittest.TestCase):
    def test_writes_traceback_and_throttles_repeats(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "session.log"
            try:
                raise RuntimeError("compose died")
            except RuntimeError as exc:
                log_exception("compose", exc, path=path)
                log_exception("compose", exc, path=path)
            text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count("RuntimeError: compose died"), 1)
        self.assertIn("compose", text)


class QueueTests(unittest.TestCase):
    """The decoder's frame queue since 5 Nov 2026.

    It replaced the 1 Nov drop-oldest queue. A full queue makes the reader
    wait again, which is what paces playback. That is safe now: the watchdog
    restarts a dead compositor within a second, and stop() always releases
    the reader. The 31 Oct freeze was a reader waiting forever on a full queue
    with no compositor and nothing to start one.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QCoreApplication.instance() or QCoreApplication([])

    def _decoder(self) -> HwDecoder:
        decoder = HwDecoder("test")
        decoder._running = True
        decoder._paused = False
        decoder._generation = 1
        return decoder

    def test_a_full_queue_holds_its_frames_in_order_and_stop_releases_the_reader(self) -> None:
        frames = hw_decoder._Frames((2, 2), depth=2)
        running = [True]
        for value in (1, 2):
            buffer = frames.take_free(lambda: running[0])
            assert buffer is not None
            buffer.fill(value)
            self.assertTrue(frames.put(buffer))
        waiting: list = []
        reader = threading.Thread(target=lambda: waiting.append(frames.take_free(lambda: running[0])))
        reader.start()
        reader.join(0.3)
        self.assertTrue(reader.is_alive())  # waiting, not dropping the oldest
        first = frames.pop(0.0)
        assert first is not None
        self.assertEqual(int(first[0, 0]), 1)
        frames.give_back(first)
        reader.join(1.0)
        self.assertFalse(reader.is_alive())
        self.assertIsNotNone(waiting[0])
        second = frames.pop(0.0)
        assert second is not None
        self.assertEqual(int(second[0, 0]), 2)
        # A reader waiting on a full queue is let go at once when the clip stops.
        frames.put(second)
        frames.put(np.zeros((2, 2), np.uint8))
        late: list = []
        reader = threading.Thread(target=lambda: late.append(frames.take_free(lambda: running[0])))
        reader.start()
        time.sleep(0.1)
        started = time.perf_counter()
        frames.close()
        reader.join(1.0)
        self.assertFalse(reader.is_alive())
        self.assertLess(time.perf_counter() - started, 0.5)
        self.assertEqual(late, [None])

    def test_stall_watch_kills_a_reader_stuck_on_ffmpeg_and_spares_one_waiting_for_frames_to_be_taken(self) -> None:
        decoder = self._decoder()

        class Proc:
            def __init__(self) -> None:
                self.killed = False

            def poll(self) -> int | None:
                return 0 if self.killed else None

            def kill(self) -> None:
                self.killed = True

        got = threading.Event()
        got.set()
        stuck = hw_decoder._Frames((2, 2), depth=2)
        stuck.reading_since = time.monotonic() - 30
        stalled = Proc()
        decoder._watch_playback(stalled, 1, got, stuck)
        self.assertTrue(stalled.killed)

        # Waiting for the compositor (or paused): reading_since is None.
        waiting = hw_decoder._Frames((2, 2), depth=2)
        spared = Proc()
        thread = threading.Thread(target=decoder._watch_playback, args=(spared, 1, got, waiting))
        thread.start()
        time.sleep(0.6)
        self.assertFalse(spared.killed)
        decoder._running = False
        thread.join(1)
        self.assertFalse(thread.is_alive())


class ComposeTests(unittest.TestCase):
    def test_one_exception_does_not_stop_the_loop(self) -> None:
        logged: list[str] = []

        class Fake:
            def __init__(self) -> None:
                self._compose_running = True
                self._compose_lock = threading.Lock()
                self._compose_ticks = 0
                self.config = type("Config", (), {"fps": 1000})()
                self.ticks = 0

            def _compose_unlocked(self) -> None:
                self.ticks += 1
                if self.ticks == 1:
                    raise RuntimeError("bad frame")
                if self.ticks >= 4:
                    self._compose_running = False

        fake = Fake()
        original = Engine._compose_run.__globals__["log_exception"]
        Engine._compose_run.__globals__["log_exception"] = lambda *args, **kwargs: logged.append("compose")
        try:
            Engine._compose_run(fake)
        finally:
            Engine._compose_run.__globals__["log_exception"] = original
        self.assertGreaterEqual(fake.ticks, 4)
        self.assertEqual(logged, ["compose"])

    def test_a_dead_thread_is_replaced_while_playback_should_continue(self) -> None:
        class Fake:
            def __init__(self) -> None:
                self._compose_running = True
                self._compose_thread_lock = threading.Lock()
                self._compose_thread: threading.Thread | None = None
                self.started = threading.Event()

            def _compose_run(self) -> None:
                self.started.set()
                while self._compose_running:
                    time.sleep(0.01)

        fake = Fake()
        fake._compose_thread_main = lambda: Engine._compose_thread_main(fake)
        fake._on_compose_thread_finished = lambda: Engine._on_compose_thread_finished(fake)
        fake._start_compose_thread = lambda: Engine._start_compose_thread(fake)
        Engine._on_compose_thread_finished(fake)
        self.assertTrue(fake.started.wait(1))
        thread = fake._compose_thread
        self.assertIsNotNone(thread)
        fake._compose_running = False
        assert thread is not None
        thread.join(1)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
