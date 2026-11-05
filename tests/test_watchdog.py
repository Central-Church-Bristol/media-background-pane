import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QCoreApplication

from media_background_pane import bus as bus_module
from media_background_pane import engine as engine_module
from media_background_pane import hw_decoder, log
from media_background_pane.config import Config
from media_background_pane.engine import Engine


class FakeSender:
    """Stands in for NdiSender. Nothing is sent on the network.

    Keeps the top-left pixel of every picture that would have gone out over
    NDI. FakeFfmpeg puts the frame number there, so it shows whether the
    picture is moving.
    """

    def __init__(self, name: str, fps: int, width: int = 0, height: int = 0) -> None:
        self.status = "test sender"
        self.available = True
        self._lock = threading.Lock()
        self._pictures: list[tuple[float, int]] = []

    def submit_array(self, array) -> None:
        with self._lock:
            self._pictures.append((time.monotonic(), int(array[0, 0, 0])))

    def since(self, start: float) -> list[int]:
        with self._lock:
            return [value for at, value in self._pictures if at >= start]

    def set_fps(self, fps: int) -> None:
        pass

    def set_size(self, width: int, height: int) -> None:
        pass

    def close(self) -> None:
        pass


class FakeFfmpeg:
    """An ffmpeg with the next frame always ready. Every byte of frame n is n % 256.

    hang_after: reads block after that many frames until kill(), like a stuck ffmpeg.
    crash_with: the next read raises this. It ends the decoder thread from inside.
    """

    def __init__(self, hang_after: int | None = None) -> None:
        self.stdout = self
        self.frames = 0
        self.hang_after = hang_after
        self.crash_with: BaseException | None = None
        self._dead = threading.Event()

    def read(self, size: int) -> bytes:
        if self.crash_with is not None:
            error, self.crash_with = self.crash_with, None
            raise error
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


def _raise(error: BaseException):
    def compose() -> None:
        raise error

    return compose


class EngineRun:
    """The app's own Engine, decoders and watchdog, with stand-ins for NDI and ffmpeg.

    The session log goes to a temporary folder. The watchdog timer is stopped
    unless timer=True, so a test can run the checks itself.
    """

    def __init__(self, hang_after: int | None = None, timer: bool = False) -> None:
        self._dir = tempfile.TemporaryDirectory()
        root = Path(self._dir.name)
        self.log_path = root / "session.log"
        self.clip = root / "Holy Fire.mov"
        self.second_clip = root / "Light Tunnel.mov"
        for clip in (self.clip, self.second_clip):
            clip.write_bytes(b"stand-in clip")
        self.procs: list[FakeFfmpeg] = []
        self._hang_after = hang_after
        self._timer = timer
        self._patches = [
            mock.patch.object(engine_module, "NdiSender", FakeSender),
            mock.patch.object(engine_module, "warmup_ffmpeg", lambda: None),
            mock.patch.object(bus_module, "find_ffmpeg", return_value=Path("ffmpeg.exe")),
            mock.patch.object(hw_decoder, "find_ffmpeg", return_value=Path("ffmpeg.exe")),
            mock.patch.object(hw_decoder, "_has_filter", return_value=False),
            mock.patch.object(hw_decoder, "_assign_job"),
            mock.patch.object(hw_decoder.subprocess, "Popen", side_effect=self._popen),
            mock.patch.object(log, "session_log_path", return_value=self.log_path),
            mock.patch.object(log, "fallback_log_path", return_value=root / "fallback.log"),
            mock.patch.dict(log._last_written, clear=True),
        ]

    def _popen(self, *args, **kwargs) -> FakeFfmpeg:
        proc = FakeFfmpeg(self._hang_after)
        self.procs.append(proc)
        return proc

    def __enter__(self) -> "EngineRun":
        for patch in self._patches:
            patch.start()
        config = Config()
        config.quality = "360p"
        self.engine = Engine(config)
        if not self._timer:
            self.engine._watchdog.stop()
        self.sender: FakeSender = self.engine._sender
        return self

    def __exit__(self, *exc_info) -> None:
        try:
            self.engine.close()
            for thread in threading.enumerate():
                if thread.name in ("ffmpeg-program", "ffmpeg-preview", "decoder-reap", "compose"):
                    thread.join(2.0)
        finally:
            for patch in reversed(self._patches):
                patch.stop()
            self._dir.cleanup()

    def play_program(self) -> None:
        self.engine.program.load(self.clip)

    def log_text(self) -> str:
        return self.log_path.read_text(encoding="utf-8") if self.log_path.is_file() else ""

    def pump(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while True:
            QCoreApplication.processEvents()
            if time.monotonic() >= end:
                return
            time.sleep(0.01)

    def wait_for(self, condition, timeout: float) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if condition():
                return True
            self.pump(0.02)
        return bool(condition())

    def time_until_moving(self, timeout: float = 3.0) -> float | None:
        """Seconds until NDI has had three different pictures, or None."""
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if len(set(self.sender.since(start))) >= 3:
                return time.monotonic() - start
            self.pump(0.02)
        return None

    def frozen(self) -> bool:
        """True if NDI gets no new picture: none at all, or the same one again and again."""
        self.pump(0.25)
        start = time.monotonic()
        self.pump(0.5)
        return len(set(self.sender.since(start))) <= 1

    def end_compose_thread_like_2_nov(self) -> None:
        """End the compose thread so that its own restart does not fire either."""
        engine = self.engine
        thread = engine._compose_thread
        assert thread is not None
        engine._on_compose_thread_finished = lambda: None
        engine._compose_unlocked = _raise(SystemExit())
        thread.join(2.0)
        del engine._compose_unlocked
        del engine._on_compose_thread_finished
        assert not thread.is_alive()

    def check_every_second(self, steps: int, pause: float = 0.1) -> list[str]:
        """Run the watchdog `steps` times, one second apart on its own clock.

        The short real pause between checks lets the compose thread tick.
        """
        start = time.monotonic()
        found: list[str] = []
        for step in range(steps):
            found += self.engine.check_playback(start + step)
            time.sleep(pause)
        return found


class WatchdogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._app = QCoreApplication.instance() or QCoreApplication([])

    def test_brings_back_a_compositor_that_ended_without_restarting_itself(self) -> None:
        # The state found on 2 Nov: no compose thread and nothing to start
        # another, so NDI and the monitors kept the last picture.
        with EngineRun() as run:
            run.play_program()
            self.assertIsNotNone(run.time_until_moving())
            run.end_compose_thread_like_2_nov()
            self.assertTrue(run.frozen())
            found = run.engine.check_playback()
            self.assertEqual(found, ["the compositor had stopped; started it again"])
            back_after = run.time_until_moving(1.0)
            self.assertIsNotNone(back_after)
            self.assertIn("watchdog: the compositor had stopped; started it again", run.log_text())

    def test_the_watchdog_runs_by_itself_every_second(self) -> None:
        with EngineRun(timer=True) as run:
            run.play_program()
            self.assertIsNotNone(run.time_until_moving())
            run.end_compose_thread_like_2_nov()
            ended = time.monotonic()
            engine = run.engine
            back = run.wait_for(lambda: engine._compose_thread is not None and engine._compose_thread.is_alive(), 3.0)
            self.assertTrue(back)
            # One timer interval, plus slack for a busy test machine.
            self.assertLess(time.monotonic() - ended, 1.6)
            self.assertIsNotNone(run.time_until_moving(1.0))
            self.assertIn("watchdog: the compositor had stopped; started it again", run.log_text())

    def test_a_compositor_error_while_logging_is_broken_still_gets_a_new_thread(self) -> None:
        # If logging itself fails, the 1 Nov restart was skipped. Now it runs in finally.
        with EngineRun() as run:
            engine = run.engine
            run.play_program()
            self.assertIsNotNone(run.time_until_moving())
            old = engine._compose_thread
            raised = {"done": False}

            def bad_frame_once() -> None:
                if not raised["done"]:
                    raised["done"] = True
                    raise ValueError("bad frame")
                Engine._compose_unlocked(engine)

            ended_with: list[BaseException] = []
            with mock.patch.object(engine_module, "log_exception", side_effect=RuntimeError("logging is broken")), \
                    mock.patch.object(threading, "excepthook", lambda args: ended_with.append(args.exc_value)):
                engine._compose_unlocked = bad_frame_once
                assert old is not None
                old.join(2.0)
                replaced = run.wait_for(
                    lambda: engine._compose_thread not in (None, old) and engine._compose_thread.is_alive(), 2.0
                )
            del engine._compose_unlocked
            self.assertFalse(old.is_alive())
            self.assertTrue(replaced)
            self.assertEqual([type(error) for error in ended_with], [RuntimeError])
            self.assertIsNotNone(run.time_until_moving(1.0))
            self.assertIn("the compose thread ended while the pane was running; starting a new one", run.log_text())

    def test_brings_back_a_program_decoder_whose_thread_ended(self) -> None:
        with EngineRun() as run:
            engine = run.engine
            run.play_program()
            self.assertIsNotNone(run.time_until_moving())
            decoder = engine.program._decoder
            assert decoder is not None
            old = decoder._thread
            assert old is not None
            decoder._proc.crash_with = SystemExit()
            old.join(2.0)
            self.assertFalse(old.is_alive())
            self.assertTrue(run.frozen())
            found = engine.check_playback()
            self.assertEqual(found, ["the Program decoder had stopped (Holy Fire.mov); started it again"])
            self.assertIs(engine.program._decoder, decoder)
            self.assertTrue(decoder.thread_alive())
            self.assertIsNotNone(run.time_until_moving(1.5))
            text = run.log_text()
            # Why the thread ended, then what the watchdog did.
            self.assertIn("decoder program thread", text)
            self.assertIn("SystemExit", text)
            self.assertIn("watchdog: the Program decoder had stopped (Holy Fire.mov); started it again", text)

    def test_brings_back_a_preview_decoder_during_a_mix(self) -> None:
        with EngineRun() as run:
            engine = run.engine
            run.play_program()
            engine.load_preview(run.second_clip)
            self.assertTrue(run.wait_for(lambda: not engine.preview.capturing_first_frame, 3.0))
            engine.set_mix(0.5)
            decoder = engine.preview._decoder
            assert decoder is not None
            self.assertTrue(run.wait_for(lambda: not decoder.paused and decoder.frames_read > 3, 2.0))
            old = decoder._thread
            assert old is not None
            decoder._proc.crash_with = SystemExit()
            old.join(2.0)
            found = engine.check_playback()
            self.assertEqual(found, ["the Preview decoder had stopped (Light Tunnel.mov); started it again"])
            self.assertTrue(run.wait_for(lambda: decoder.frames_read > 3, 2.0))
            self.assertFalse(decoder.paused)

    def test_restarts_a_clip_that_stopped_mid_playback(self) -> None:
        # ffmpeg stops sending frames and the decoder's own stall kill does not
        # act (here: switched off). Only the watchdog is left.
        with mock.patch.object(hw_decoder, "_STALL_TIMEOUT", 1000.0):
            with EngineRun(hang_after=12) as run:
                engine = run.engine
                run.play_program()
                decoder = engine.program._decoder
                assert decoder is not None
                self.assertTrue(run.wait_for(lambda: decoder.frames_read >= 12, 3.0))
                found = run.check_every_second(40)
                self.assertEqual(
                    found, ["the Program decoder had read no frame for 30 s (Holy Fire.mov); started it again"]
                )
                self.assertTrue(run.wait_for(lambda: len(run.procs) == 2 and decoder.frames_read > 3, 2.0))

    def test_a_clip_that_is_still_opening_is_left_to_the_decoder(self) -> None:
        # Opening a clip from Google Drive can take a while. Restarting it
        # before its first frame could keep it from ever starting.
        with mock.patch.object(hw_decoder, "_FIRST_FRAME_TIMEOUT", 1000.0), \
                mock.patch.object(engine_module, "_DECODER_STUCK", 5.0):
            with EngineRun(hang_after=0) as run:
                run.play_program()
                run.pump(0.2)
                found = run.check_every_second(12)
                self.assertEqual(found, [])
                self.assertEqual(len(run.procs), 1)

    def test_a_long_gap_between_checks_is_not_taken_for_a_stall(self) -> None:
        # The PC was asleep, or the GUI thread was busy, between two checks.
        with mock.patch.object(hw_decoder, "_STALL_TIMEOUT", 1000.0):
            with EngineRun(hang_after=12) as run:
                engine = run.engine
                run.play_program()
                decoder = engine.program._decoder
                assert decoder is not None
                self.assertTrue(run.wait_for(lambda: decoder.frames_read >= 12, 3.0))
                start = time.monotonic()
                found = engine.check_playback(start)
                found += engine.check_playback(start + 1)
                found += engine.check_playback(start + 3600)
                found += engine.check_playback(start + 3601)
                self.assertEqual(found, [])
                self.assertEqual(len(run.procs), 1)

    def test_leaves_a_healthy_pane_alone(self) -> None:
        # A short stall limit makes a false restart easier to catch.
        with mock.patch.object(engine_module, "_DECODER_STUCK", 5.0):
            with EngineRun() as run:
                engine = run.engine
                run.play_program()
                engine.load_preview(run.second_clip)
                self.assertTrue(run.wait_for(lambda: not engine.preview.capturing_first_frame, 3.0))
                found = run.check_every_second(10)  # Program playing, Preview paused
                engine.set_mix(0.5)
                found += run.check_every_second(6)  # both playing
                engine.take()
                found += run.check_every_second(6)
                engine.set_dimmer(0.0)
                found += run.check_every_second(10)  # blacked out: both paused
                engine.set_dimmer(1.0)
                found += run.check_every_second(6)
                engine.set_fps(25)
                found += run.check_every_second(6)  # both decoders restart themselves
                self.assertEqual(found, [])
                self.assertEqual(len(run.procs), 4)
                self.assertNotIn("watchdog: the", run.log_text())

    def test_reports_once_where_a_stuck_compositor_is_waiting(self) -> None:
        with EngineRun() as run:
            engine = run.engine
            thread = engine._compose_thread
            gate = threading.Event()

            def stuck_in_compose() -> None:
                gate.wait(10.0)

            engine._compose_unlocked = stuck_in_compose
            try:
                time.sleep(0.3)
                start = time.monotonic()
                found: list[str] = []
                for step in range(10):
                    found += engine.check_playback(start + step)
            finally:
                gate.set()
                del engine._compose_unlocked
            self.assertEqual(len(found), 1)
            self.assertIn("the compositor has been on the same frame for 5 s", found[0])
            self.assertIn("in stuck_in_compose", found[0])
            # It is not replaced: a second compose thread would wait on the same lock.
            self.assertIs(engine._compose_thread, thread)
            self.assertIn("Where it is waiting", run.log_text())


if __name__ == "__main__":
    unittest.main()
