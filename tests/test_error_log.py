import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from media_background_pane import log
from media_background_pane.log import log_event, log_exception

PYTHONW = Path(sys.executable).with_name("pythonw.exe")


class LogFolder:
    """session.log and the fallback log in a temporary folder, never the pane's own.

    fail_main / fail_fallback make that location fail the way a full disk or a
    locked file would.
    """

    def __init__(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        root = Path(self._dir.name)
        self.main = root / "LocalAppData" / "session.log"
        self.fallback = root / "Temp" / "fallback session.log"
        self._main_error: BaseException | None = None
        self._fallback_error: BaseException | None = None
        self._patches = [
            mock.patch.object(log, "session_log_path", side_effect=self._main_path),
            mock.patch.object(log, "fallback_log_path", side_effect=self._fallback_path),
            mock.patch.dict(log._last_written, clear=True),
            mock.patch.object(log, "_lost", 0),
            mock.patch.object(log, "_lost_reason", ""),
            mock.patch.object(log, "_unwritten", []),
            mock.patch.object(log, "_dropped", 0),
        ]

    def _main_path(self) -> Path:
        if self._main_error is not None:
            raise self._main_error
        return self.main

    def _fallback_path(self) -> Path:
        if self._fallback_error is not None:
            raise self._fallback_error
        return self.fallback

    def fail_main(self, error: BaseException | None) -> None:
        self._main_error = error

    def fail_fallback(self, error: BaseException | None) -> None:
        self._fallback_error = error

    def text(self, which: Path) -> str:
        return which.read_text(encoding="utf-8") if which.is_file() else ""

    def start(self) -> None:
        for patch in self._patches:
            patch.start()

    def stop(self) -> None:
        for patch in reversed(self._patches):
            patch.stop()
        self._dir.cleanup()


class LogWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.logs = LogFolder()
        self.logs.start()
        self.addCleanup(self.logs.stop)

    def test_text_that_cannot_be_encoded_is_still_written(self) -> None:
        # The 1 Nov logger raised UnicodeEncodeError here, from inside the
        # handler that was meant to keep the thread alive.
        log_exception("compose", RuntimeError("bad name \udc80.mov"))
        self.assertIn("RuntimeError: bad name \\udc80.mov", self.logs.text(self.logs.main))

    def test_an_error_whose_traceback_cannot_be_formatted_is_still_written(self) -> None:
        with mock.patch.object(log.traceback, "format_exception", side_effect=MemoryError()):
            log_exception("compose", ValueError("bad frame"))
        text = self.logs.text(self.logs.main)
        self.assertIn("ValueError: bad frame", text)
        self.assertIn("no traceback: MemoryError", text)

    def test_the_fallback_log_gets_the_entry_when_session_log_cannot_be_written(self) -> None:
        self.logs.fail_main(PermissionError("access denied"))
        log_exception("decoder program thread", RuntimeError("gone"))
        fallback = self.logs.text(self.logs.fallback)
        self.assertIn("could not write the session log (PermissionError: access denied)", fallback)
        self.assertIn("RuntimeError: gone", fallback)

    def test_nothing_raises_when_no_log_can_be_written_and_the_gap_is_reported_later(self) -> None:
        self.logs.fail_main(PermissionError("access denied"))
        self.logs.fail_fallback(OSError("disk full"))
        log_exception("compose", RuntimeError("first"))
        log_event("watchdog", "the compositor had stopped; started it again")
        self.assertFalse(self.logs.main.exists())
        self.assertFalse(self.logs.fallback.exists())

        self.logs.fail_main(None)
        log_event("watchdog", "the Program decoder had stopped (Holy Fire.mov); started it again")
        text = self.logs.text(self.logs.main)
        self.assertIn("log: 2 earlier entries could not be written", text)
        self.assertIn("PermissionError: access denied", text)
        self.assertIn("OSError: disk full", text)
        self.assertIn("watchdog: the Program decoder had stopped", text)
        # The two entries themselves are not lost: they come first, in order.
        first = text.index("RuntimeError: first")
        second = text.index("watchdog: the compositor had stopped; started it again")
        third = text.index("watchdog: the Program decoder had stopped")
        self.assertLess(first, second)
        self.assertLess(second, third)

    def test_entries_kept_while_nothing_can_be_written_are_capped(self) -> None:
        self.logs.fail_main(PermissionError("access denied"))
        self.logs.fail_fallback(OSError("disk full"))
        for number in range(log._KEEP_UNWRITTEN + 30):
            log_event("test", f"entry {number}")
        self.logs.fail_main(None)
        log_event("test", "back")
        text = self.logs.text(self.logs.main)
        self.assertIn(f"log: {log._KEEP_UNWRITTEN + 30} earlier entries could not be written", text)
        self.assertIn("log: the oldest 30 of them were not kept", text)
        self.assertNotIn("test: entry 29\n", text)
        self.assertIn("test: entry 30\n", text)
        self.assertIn(f"test: entry {log._KEEP_UNWRITTEN + 29}\n", text)

    def test_a_log_folder_that_says_it_exists_does_not_stop_the_entry(self) -> None:
        # 4 Nov 2026: mkdir(exist_ok=True) raised FileExistsError for the pane's
        # own log folder, which was there, and 54 entries were lost.
        self.logs.main.parent.mkdir(parents=True)
        error = FileExistsError(183, "Cannot create a file when that file already exists")
        with mock.patch.object(Path, "mkdir", side_effect=error):
            log_event("watchdog", "written although mkdir failed")
        self.assertIn("watchdog: written although mkdir failed", self.logs.text(self.logs.main))
        self.assertFalse(self.logs.fallback.exists())

    def test_a_log_that_cannot_be_rotated_keeps_the_new_entry(self) -> None:
        self.logs.main.parent.mkdir(parents=True)
        self.logs.main.write_text("x" * (log._MAX_BYTES + 10), encoding="utf-8")
        with mock.patch.object(Path, "replace", side_effect=PermissionError("file in use")):
            log_event("watchdog", "after a failed rotation")
        self.assertTrue(self.logs.text(self.logs.main).endswith("watchdog: after a failed rotation\n"))

    def test_a_full_log_is_rotated_to_one_previous_file(self) -> None:
        self.logs.main.parent.mkdir(parents=True)
        self.logs.main.write_text("old\n" * (log._MAX_BYTES // 4 + 10), encoding="utf-8")
        log_event("start", "pid 1")
        previous = self.logs.main.with_name("session.log.1")
        self.assertTrue(previous.read_text(encoding="utf-8").startswith("old\n"))
        self.assertTrue(self.logs.text(self.logs.main).endswith("start: pid 1\n"))

    def test_repeats_are_throttled_but_other_errors_are_not(self) -> None:
        for _ in range(3):
            log_exception("compose", RuntimeError("same"))
        log_exception("compose", ValueError("different type"))
        log_exception("decoder program", RuntimeError("other place"))
        text = self.logs.text(self.logs.main)
        self.assertEqual(text.count("RuntimeError: same"), 1)
        self.assertIn("ValueError: different type", text)
        self.assertIn("RuntimeError: other place", text)


class LogPathTests(unittest.TestCase):
    def test_the_fallback_log_does_not_need_tempfile(self) -> None:
        # 4 Nov 2026: in the running pane, the first tempfile.gettempdir() raised
        # "TypeError: first argument must be string or compiled pattern" from
        # os.path.expandvars, so the fallback log could not be used either.
        boom = TypeError("first argument must be string or compiled pattern")
        with (
            mock.patch.dict(os.environ, {"TEMP": r"C:\Temp for test", "TMP": ""}),
            mock.patch("tempfile.gettempdir", side_effect=boom),
            mock.patch("os.path.expandvars", side_effect=boom),
        ):
            self.assertEqual(log.fallback_log_path(), Path(r"C:\Temp for test") / "Media Background Pane session.log")

    def test_working_out_the_session_log_path_creates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": folder}):
                path = log.session_log_path()
            self.assertEqual(path, Path(folder) / "Media Background Pane" / "session.log")
            self.assertFalse(path.parent.exists())


class ThrottleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 0.0
        self.lines: list[str] = []
        self.throttle = log.Throttle(10.0, 3, write=lambda context, message: self.lines.append(f"{context}: {message}"), clock=lambda: self.now)

    def test_a_few_events_go_straight_in(self) -> None:
        for number in range(3):
            self.throttle.event("reshow", "pane", f"shown again {number}")
        self.assertEqual(self.lines, ["pane: shown again 0", "pane: shown again 1", "pane: shown again 2"])

    def test_a_flood_is_held_back_and_counted(self) -> None:
        for number in range(50):
            self.now += 0.1
            self.throttle.event("reshow", "pane", f"shown again {number}")
        self.assertEqual(len(self.lines), 3)
        self.throttle.flush()
        self.assertEqual(len(self.lines), 3)  # still no room
        self.now += 10
        self.throttle.flush()
        self.assertEqual(len(self.lines), 4)
        self.assertEqual(self.lines[-1], "pane: shown again 49 [47 more times in the last 15 s; this was the last]")

    def test_kinds_are_held_back_separately(self) -> None:
        for _ in range(10):
            self.throttle.event("reshow", "pane", "shown again")
        self.throttle.event("decision", "workspace check", "hide: another workspace")
        self.assertIn("workspace check: hide: another workspace", self.lines)

    def test_a_held_count_goes_out_before_the_next_line_of_its_kind(self) -> None:
        for _ in range(5):
            self.throttle.event("reshow", "pane", "shown again")
        self.now += 11
        self.throttle.event("reshow", "pane", "shown again, later")
        self.assertEqual(self.lines[-2:], ["pane: shown again [2 more times in the last 11 s; this was the last]", "pane: shown again, later"])


class HookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.logs = LogFolder()
        self.logs.start()
        self.addCleanup(self.logs.stop)
        # The hooks this installs are removed again when these patches end.
        for patch in (
            mock.patch.object(threading, "excepthook", lambda args: None),
            mock.patch.object(sys, "excepthook", lambda *args: None),
            mock.patch.object(sys, "unraisablehook", lambda unraisable: None),
            mock.patch.object(log.install_error_logging, "_installed", False, create=True),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        log.install_error_logging()

    def test_thread_errors_are_logged(self) -> None:
        def crash() -> None:
            raise ZeroDivisionError("in a thread")

        thread = threading.Thread(target=crash, name="compose")
        thread.start()
        thread.join()
        text = self.logs.text(self.logs.main)
        self.assertIn("thread compose", text)
        self.assertIn("ZeroDivisionError: in a thread", text)

    def test_gui_thread_errors_are_logged(self) -> None:
        # Qt hands errors raised in slots to sys.excepthook and carries on.
        try:
            raise KeyError("in a slot")
        except KeyError as exc:
            sys.excepthook(type(exc), exc, exc.__traceback__)
        text = self.logs.text(self.logs.main)
        self.assertIn("main thread", text)
        self.assertIn("KeyError: 'in a slot'", text)

    def test_errors_in_del_are_logged(self) -> None:
        class Broken:
            def __del__(self) -> None:
                raise ValueError("in __del__")

        Broken()
        text = self.logs.text(self.logs.main)
        self.assertIn("unraisable", text)
        self.assertIn("ValueError: in __del__", text)


@unittest.skipUnless(sys.platform == "win32" and PYTHONW.is_file(), "needs pythonw.exe")
class PythonwTests(unittest.TestCase):
    """The pane runs under pythonw, which has no stderr. Check the log there."""

    CHILD = (
        "import sys, threading\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "from media_background_pane.log import install_error_logging\n"
        "install_error_logging()\n"
        "def crash():\n"
        "    raise RuntimeError('compose thread died under pythonw \\udc80')\n"
        "thread = threading.Thread(target=crash, name='compose')\n"
        "thread.start()\n"
        "thread.join()\n"
    )

    def _run_child(self, local: Path, temp: Path) -> None:
        env = dict(os.environ, LOCALAPPDATA=str(local), TEMP=str(temp), TMP=str(temp))
        subprocess.run([str(PYTHONW), "-c", self.CHILD], env=env, timeout=60, check=True)

    def test_a_thread_error_under_pythonw_reaches_session_log(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            local, temp = Path(folder) / "Local", Path(folder) / "Temp"
            local.mkdir()
            temp.mkdir()
            self._run_child(local, temp)
            text = (local / "Media Background Pane" / "session.log").read_text(encoding="utf-8")
        self.assertIn("thread compose", text)
        self.assertIn("RuntimeError: compose thread died under pythonw \\udc80", text)

    def test_under_pythonw_the_fallback_is_used_when_session_log_cannot_be(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            local, temp = Path(folder) / "Local", Path(folder) / "Temp"
            local.mkdir()
            temp.mkdir()
            # A file where the log folder should be, so session.log cannot be made.
            (local / "Media Background Pane").write_text("not a folder", encoding="utf-8")
            self._run_child(local, temp)
            text = (temp / "Media Background Pane session.log").read_text(encoding="utf-8")
        self.assertIn("could not write the session log (", text)
        self.assertIn("RuntimeError: compose thread died under pythonw \\udc80", text)

    def test_a_crash_in_native_code_leaves_every_threads_stack_in_crash_log(self) -> None:
        # Since 5 Nov 2026. A process killed by a fault in a native library
        # runs none of its Python handlers; faulthandler still writes.
        child = (
            "import sys, threading, time, faulthandler\n"
            f"sys.path.insert(0, {str(REPO)!r})\n"
            "from media_background_pane.log import enable_crash_log\n"
            "enable_crash_log()\n"
            "def compose_like():\n"
            "    time.sleep(30)\n"
            "threading.Thread(target=compose_like, name='compose', daemon=True).start()\n"
            "time.sleep(0.2)\n"
            "def native_fault():\n"
            "    faulthandler._sigsegv()\n"
            "native_fault()\n"
        )
        with tempfile.TemporaryDirectory() as folder:
            local, temp = Path(folder) / "Local", Path(folder) / "Temp"
            local.mkdir()
            temp.mkdir()
            env = dict(os.environ, LOCALAPPDATA=str(local), TEMP=str(temp), TMP=str(temp))
            result = subprocess.run([str(PYTHONW), "-c", child], env=env, timeout=60)
            text = (local / "Media Background Pane" / "crash.log").read_text(encoding="utf-8", errors="replace")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("crash log armed: pid", text)
        self.assertIn("native_fault", text)
        self.assertIn("compose_like", text)


if __name__ == "__main__":
    unittest.main()
