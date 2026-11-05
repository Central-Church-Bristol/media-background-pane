"""The GUI thread stays free: ProPresenter's API, the library scan, and the stall detector.

The MIX and DIM fades tick every 8 ms on the GUI thread, so anything that
blocks it makes a fade stop and start. These tests run MainWindow's own
methods on stand-ins, with the real BackgroundCalls thread and a fake
ProPresenter whose every request takes 0.8 s. Nothing here talks to a real
ProPresenter, and the session log goes to a temporary folder.
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402

from media_background_pane import library, log, main_window, propresenter  # noqa: E402
from media_background_pane.log import Throttle  # noqa: E402
from media_background_pane.main_window import BackgroundCalls, GuiStallDetector, MainWindow  # noqa: E402

_module_patches: list = []
_log_folder: tempfile.TemporaryDirectory | None = None
_real_requests: list[str] = []


def _no_real_propresenter(url: str, timeout: float = 0.8):
    _real_requests.append(url)
    raise OSError("the tests never ask a real ProPresenter")


def setUpModule() -> None:
    global _log_folder
    _log_folder = tempfile.TemporaryDirectory()
    root = Path(_log_folder.name)
    for patch in (
        mock.patch.object(log, "session_log_path", return_value=root / "session.log"),
        mock.patch.object(log, "fallback_log_path", return_value=root / "fallback.log"),
        mock.patch.object(propresenter, "_request", _no_real_propresenter),
    ):
        patch.start()
        _module_patches.append(patch)


def tearDownModule() -> None:
    while _module_patches:
        _module_patches.pop().stop()
    if _log_folder is not None:
        _log_folder.cleanup()


def qt_app() -> QCoreApplication:
    return QCoreApplication.instance() or QCoreApplication([])


def run_qt(seconds: float) -> None:
    """Let Qt run timers and deliver queued signals for this long."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 10)
        time.sleep(0.002)


def wait_until(condition, timeout: float = 5.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 10)
        if condition():
            return True
        time.sleep(0.002)
    return bool(condition())


GUI = threading.main_thread().name


class Lines:
    """Session-log lines from main_window, kept in a list."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.lines: list[str] = []
        patch = mock.patch.object(main_window, "log_event", self.write)
        patch.start()
        test.addCleanup(patch.stop)

    def write(self, context: str, message: str) -> None:
        self.lines.append(f"{context}: {message}")

    def logged(self, text: str) -> list[str]:
        return [line for line in self.lines if text in line]


class SlowProPresenter:
    """A fake ProPresenter API in which every request takes `delay` seconds, as a busy or starting one's do."""

    def __init__(self, test: unittest.TestCase, delay: float = 0.8) -> None:
        self.delay = delay
        self.layer_on: bool | None = False
        self.works = True
        self.requests: list[str] = []
        self.threads: set[str] = set()
        self.at_once = 0
        self.most_at_once = 0
        self._lock = threading.Lock()
        for name, fake in {
            "video_input_layer_active": self.video_input_layer_active,
            "trigger_first_video_input": self.trigger_first_video_input,
            "clear_video_input_layer": self.clear_video_input_layer,
        }.items():
            patch = mock.patch.object(main_window, name, fake)
            patch.start()
            test.addCleanup(patch.stop)

    def _request(self, what: str) -> None:
        with self._lock:
            self.at_once += 1
            self.most_at_once = max(self.most_at_once, self.at_once)
            self.requests.append(what)
            self.threads.add(threading.current_thread().name)
        try:
            time.sleep(self.delay)
        finally:
            with self._lock:
                self.at_once -= 1

    def video_input_layer_active(self) -> bool | None:
        self._request("status")
        return self.layer_on if self.works else None

    def trigger_first_video_input(self, _duration=None) -> bool:
        self._request("trigger")
        if self.works:
            self.layer_on = True
        return self.works

    def clear_video_input_layer(self, _duration=None) -> bool:
        self._request("clear")
        if self.works:
            self.layer_on = False
        return self.works


class FakeTimer:
    def __init__(self) -> None:
        self.active = False

    def isActive(self) -> bool:
        return self.active

    def start(self) -> None:
        self.active = True

    def stop(self) -> None:
        self.active = False


class VideoInputPane:
    """MainWindow's Video Input code with the real ProPresenter thread, on a fake button and timer."""

    _try_trigger_video_input = MainWindow._try_trigger_video_input
    _video_input_attempted = MainWindow._video_input_attempted
    _rearm_video_input = MainWindow._rearm_video_input
    _sync_video_input_status = MainWindow._sync_video_input_status
    _video_input_status = MainWindow._video_input_status
    _toggle_video_input = MainWindow._toggle_video_input
    _video_input_toggled = MainWindow._video_input_toggled

    def __init__(self, test: unittest.TestCase) -> None:
        self.config = SimpleNamespace(fade_seconds=1)
        self._video_input_triggered = False
        self._video_input_ignore_until = 0.0
        self._video_input_serial = 0
        self._video_input_known = False
        self._video_input_timer = FakeTimer()
        self._video_input_timer.start()
        self._pp_calls = BackgroundCalls("test propresenter")
        # Stopped (and its thread ended) before the fake API is unpatched.
        test.addCleanup(self._pp_calls.stop, 3.0)
        self.checked = False
        self.video_input_button = SimpleNamespace(isChecked=lambda: self.checked)
        self.shown: list[bool] = []  # every state the button was set to, in order
        self.shown_on: set[str] = set()  # the threads that set it

    def _set_video_input_button(self, active: bool) -> None:
        self.checked = active
        self.shown.append(active)
        self.shown_on.add(threading.current_thread().name)

    def click(self) -> None:
        """The operator's click: Qt flips the button, then calls the slot."""
        self.checked = not self.checked
        self._toggle_video_input()

    def idle(self) -> bool:
        return not self._pp_calls.busy("status", "attempt", "toggle")


class ProPresenterThreadTests(unittest.TestCase):
    def setUp(self) -> None:
        qt_app()
        self.lines = Lines(self)
        self.pp = SlowProPresenter(self)
        self.pane = VideoInputPane(self)

    def tearDown(self) -> None:
        self.assertEqual(_real_requests, [])

    def test_a_slow_propresenter_does_not_hold_up_the_gui_thread(self) -> None:
        started = time.perf_counter()
        self.pane._sync_video_input_status()
        self.pane._try_trigger_video_input()
        self.pane.click()
        # On the GUI thread these were up to four requests of 0.8 s each.
        self.assertLess(time.perf_counter() - started, 0.1)
        gaps: list[float] = []
        last = [time.perf_counter()]

        def heartbeat() -> None:
            now = time.perf_counter()
            gaps.append(now - last[0])
            last[0] = now

        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(heartbeat)
        timer.start()
        self.assertTrue(wait_until(self.pane.idle, timeout=10))
        timer.stop()
        self.assertEqual(self.pp.requests, ["status", "trigger"])
        self.assertGreater(len(gaps), 50)
        self.assertLess(max(gaps), 0.2)
        self.assertTrue(self.pane.checked)

    def test_answers_are_applied_on_the_gui_thread(self) -> None:
        self.pane._sync_video_input_status()
        self.pane.click()
        self.assertTrue(wait_until(self.pane.idle, timeout=10))
        self.assertEqual(self.pp.threads, {"test propresenter calls"})
        self.assertEqual(self.pane.shown_on, {GUI})
        self.assertTrue(self.lines.logged("video input: switched on by the operator"))

    def test_status_polls_do_not_overlap(self) -> None:
        for _ in range(10):  # once every 50 ms while the first is still out
            self.pane._sync_video_input_status()
            run_qt(0.05)
        self.assertEqual(self.pp.requests, ["status"])
        self.assertTrue(wait_until(self.pane.idle))
        self.pane._sync_video_input_status()
        self.assertTrue(wait_until(self.pane.idle))
        self.assertEqual(self.pp.requests, ["status", "status"])
        self.assertEqual(self.pp.most_at_once, 1)

    def test_the_button_follows_propresenter(self) -> None:
        self.pp.layer_on = True
        self.pane._sync_video_input_status()
        self.assertTrue(wait_until(self.pane.idle))
        self.assertEqual(self.pane.shown, [True])
        self.pp.layer_on = None  # unknown: no change
        self.pane._sync_video_input_status()
        self.assertTrue(wait_until(self.pane.idle))
        self.assertEqual(self.pane.shown, [True])

    def test_switches_go_to_propresenter_one_at_a_time_in_order(self) -> None:
        self.pane._try_trigger_video_input()  # the start-up attempt: status, then trigger
        run_qt(0.1)
        self.pane.click()  # on
        self.pane.click()  # off
        self.pane.click()  # on
        self.assertEqual(self.pane.shown, [True, False, True])  # the operator's choices show at once
        self.assertTrue(wait_until(self.pane.idle, timeout=10))
        # The attempt's status check was out already; it did not trigger after the operator chose.
        self.assertEqual(self.pp.requests, ["status", "trigger", "clear", "trigger"])
        self.assertEqual(self.pp.most_at_once, 1)
        self.assertEqual(self.pane.shown, [True, False, True, True])  # no flicker while the answers came in
        self.assertEqual(len(self.lines.logged("by the operator")), 3)
        self.assertEqual(self.lines.logged("switched ProPresenter's first video input on"), [])

    def test_the_operators_off_holds_when_the_start_up_attempt_was_waiting(self) -> None:
        self.pane._sync_video_input_status()  # out for 0.8 s
        run_qt(0.05)
        self.pane._try_trigger_video_input()  # waits behind it
        self.pane.checked = False
        self.pane._toggle_video_input()  # the operator's off
        self.assertFalse(self.pane._video_input_timer.isActive())
        self.assertTrue(wait_until(self.pane.idle, timeout=10))
        self.assertEqual(self.pp.requests, ["status", "clear"])  # the attempt neither asked nor triggered
        self.assertTrue(self.lines.logged("video input: switched off by the operator"))
        self.pane._try_trigger_video_input()
        run_qt(0.1)
        self.assertEqual(self.pp.requests, ["status", "clear"])  # and it holds for the run

    def test_a_status_answer_from_before_a_click_does_not_flip_the_button_back(self) -> None:
        self.pp.layer_on = False
        self.pane._sync_video_input_status()  # will say "off"
        run_qt(0.1)
        self.pane.click()  # the operator switches on meanwhile
        self.assertTrue(wait_until(self.pane.idle, timeout=10))
        self.assertEqual(self.pane.shown, [True, True])

    def test_an_attempt_for_a_run_that_ended_does_not_count(self) -> None:
        self.pane._try_trigger_video_input()
        run_qt(0.1)  # its status check is out
        self.pane._rearm_video_input()  # ProPresenter restarted meanwhile
        self.assertTrue(wait_until(self.pane.idle, timeout=10))
        self.assertEqual(self.pp.requests, ["status"])  # no trigger for the run that ended
        self.assertFalse(self.pane._video_input_triggered)
        self.assertTrue(self.pane._video_input_timer.isActive())
        self.pane._try_trigger_video_input()  # the next retry, for the new run
        self.assertTrue(wait_until(self.pane.idle, timeout=10))
        self.assertEqual(self.pp.requests, ["status", "status", "trigger"])
        self.assertTrue(self.pane._video_input_triggered)
        self.assertEqual(len(self.lines.logged("video input: switched ProPresenter's first video input on")), 1)

    def test_a_switch_propresenter_does_not_take_goes_back(self) -> None:
        self.pp.works = False
        self.pane.click()
        self.assertEqual(self.pane.shown, [True])
        self.assertTrue(wait_until(self.pane.idle, timeout=5))
        self.assertEqual(self.pane.shown, [True, False])
        self.assertFalse(self.pane._video_input_triggered)  # the start-up attempt keeps trying
        self.assertEqual(self.lines.logged("by the operator"), [])


class BackgroundCallsTests(unittest.TestCase):
    def setUp(self) -> None:
        qt_app()
        self.lines = Lines(self)
        self.calls = BackgroundCalls("test")
        self.addCleanup(self.calls.stop, 2.0)

    def test_an_error_is_logged_and_answered_with_failed(self) -> None:
        errors: list[tuple[str, str]] = []
        answers: list = []

        def broken() -> None:
            raise ValueError("bad answer")

        with mock.patch.object(main_window, "log_exception", lambda context, exc=None: errors.append((context, type(exc).__name__))):
            self.calls.submit("status", broken, answers.append, failed="failed")
            self.assertTrue(wait_until(lambda: answers))
        self.assertEqual(answers, ["failed"])
        self.assertEqual(errors, [("test thread: status", "ValueError")])
        self.assertTrue(self.calls.thread_alive())

    def test_a_thread_that_ended_is_started_again(self) -> None:
        answers: list = []

        def end_the_thread() -> None:
            raise SystemExit  # not an Exception, so it ends the thread, as threads ended on 31 Oct

        self.calls.submit("status", end_the_thread, answers.append)
        self.assertTrue(wait_until(lambda: not self.calls.thread_alive(), timeout=2))
        self.assertTrue(self.calls.submit("status", lambda: "fine", answers.append))
        self.assertTrue(wait_until(lambda: answers, timeout=2))
        self.assertEqual(answers, ["fine"])
        self.assertEqual(self.lines.logged("test thread: it had stopped; started it again"), ["test thread: it had stopped; started it again"])

    def test_after_stop_nothing_more_is_taken(self) -> None:
        self.calls.stop(1.0)
        self.assertFalse(self.calls.submit("status", lambda: 1))
        self.assertFalse(self.calls.thread_alive())


class FakeWatcher:
    def __init__(self) -> None:
        self.dirs: list[str] = []

    def directories(self) -> list[str]:
        return list(self.dirs)

    def removePath(self, path: str) -> None:
        self.dirs.remove(path)

    def addPath(self, path: str) -> None:
        self.dirs.append(path)


class FakeStrip:
    def __init__(self) -> None:
        self.files: list[Path] = []
        self.thumbs: dict[str, QImage] = {}
        self.hint = ""
        self.threads: set[str] = set()

    def set_files(self, paths: list[Path]) -> None:
        self.files = list(paths)
        self.threads.add(threading.current_thread().name)

    def set_thumb(self, path: str, image: QImage) -> None:
        self.thumbs[path] = image

    def set_empty_hint(self, text: str) -> None:
        self.hint = text


class LibraryPane:
    """MainWindow's library code with the real library thread, on a fake strip and watcher."""

    refresh_library = MainWindow.refresh_library
    _show_library = MainWindow._show_library

    def __init__(self, test: unittest.TestCase, folder: Path) -> None:
        self.config = SimpleNamespace(media_folder=str(folder), include_subfolders=False)
        self._library_calls = BackgroundCalls("test library")
        test.addCleanup(self._library_calls.stop, 3.0)
        self._library_rescan = False
        self._library_extra: list[Path] = []
        self._folder_watcher = FakeWatcher()
        self.thumbs = FakeStrip()
        self.requested: list[Path] = []
        self.thumbnailer = SimpleNamespace(request=self.requested.append)
        self._files: list[Path] = []
        self._lan_files: list[dict] = []

    def _sync_thumb_roles(self) -> None:
        pass


class LibraryTests(unittest.TestCase):
    def setUp(self) -> None:
        qt_app()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.media = root / "Media Backgrounds"
        self.media.mkdir()
        self.cache = root / "thumbs"
        self.cache.mkdir()
        patch = mock.patch.object(library, "thumb_cache_dir", lambda: self.cache)
        patch.start()
        self.addCleanup(patch.stop)
        still = QImage(320, 180, QImage.Format.Format_RGB32)
        still.fill(QColor(200, 160, 40))
        self.assertTrue(still.save(str(self.media / "Lamb.png")))
        (self.media / "Holy Fire.mov").write_bytes(b"\x00" * 4096)
        (self.media / "notes.txt").write_text("not media", encoding="utf-8")
        self.elsewhere = root / "Dropped"
        self.elsewhere.mkdir()
        (self.elsewhere / "Beach.mov").write_bytes(b"\x00" * 4096)

    def test_the_scan_finds_the_media_and_makes_thumbnails_for_stills(self) -> None:
        scan = main_window.scan_library(self.media, False)
        self.assertTrue(scan.folder_ok)
        self.assertEqual([path.name for path in scan.files], ["Holy Fire.mov", "Lamb.png"])
        self.assertEqual(scan.thumb_names, [library.thumb_cache_path(path).name for path in scan.files])
        self.assertIn(str(self.media / "Lamb.png"), scan.thumbs)
        self.assertTrue((self.cache / scan.thumb_names[1]).is_file())
        # A video's first frame needs QMediaPlayer, on the GUI thread.
        self.assertNotIn(str(self.media / "Holy Fire.mov"), scan.thumbs)

    def test_a_cached_thumbnail_is_read_on_the_library_thread(self) -> None:
        video = self.media / "Holy Fire.mov"
        cached = QImage(160, 90, QImage.Format.Format_RGB32)
        cached.fill(QColor(10, 20, 30))
        cached.save(str(library.thumb_cache_path(video)), "JPG", 80)
        scan = main_window.scan_library(self.media, False)
        self.assertEqual(scan.thumbs[str(video)].size(), cached.size())

    def test_a_missing_folder_gives_an_empty_library(self) -> None:
        scan = main_window.scan_library(self.media / "gone", False)
        self.assertFalse(scan.folder_ok)
        self.assertEqual(scan.files, [])

    def test_a_slow_drive_does_not_hold_up_the_gui_thread(self) -> None:
        real_scan = main_window.scan_library
        scans: list[list[Path]] = []
        scanned_on: set[str] = set()

        def slow_scan(folder, recursive, extra=None):
            scans.append(list(extra or []))
            scanned_on.add(threading.current_thread().name)
            time.sleep(0.5)
            return real_scan(folder, recursive, extra)

        pane = LibraryPane(self, self.media)
        dropped = self.elsewhere / "Beach.mov"
        with mock.patch.object(main_window, "scan_library", slow_scan):
            started = time.perf_counter()
            pane.refresh_library()
            pane.refresh_library()  # asked for again during the scan
            pane.refresh_library(extra=[dropped])
            self.assertLess(time.perf_counter() - started, 0.05)
            self.assertTrue(wait_until(lambda: len(scans) == 2 and not pane._library_calls.busy("scan"), timeout=5))
            run_qt(0.1)
        self.assertEqual(len(scans), 2)  # three refreshes made two scans
        self.assertEqual(scans[1], [dropped])  # the dropped file went with the second
        self.assertEqual(scanned_on, {"test library calls"})
        self.assertEqual(pane.thumbs.threads, {GUI})
        self.assertEqual([path.name for path in pane._files], ["Beach.mov", "Holy Fire.mov", "Lamb.png"])
        self.assertEqual(pane._folder_watcher.dirs, [str(self.media)])
        self.assertEqual(pane.thumbs.hint, "Drop images or videos here")
        self.assertIn(str(self.media / "Lamb.png"), pane.thumbs.thumbs)
        # Videos without a thumbnail go to the Thumbnailer (which ignores a path it has queued already).
        self.assertEqual(pane.requested[-2:], [dropped, self.media / "Holy Fire.mov"])
        self.assertEqual(set(pane.requested), {dropped, self.media / "Holy Fire.mov"})
        lamb = next(item for item in pane._lan_files if item["name"] == "Lamb.png")
        self.assertEqual(lamb["thumb"], f"/thumbs/{library.thumb_cache_path(self.media / 'Lamb.png').name}")


class StallDetectorTests(unittest.TestCase):
    """The detector's beats, on a fake clock."""

    def setUp(self) -> None:
        qt_app()
        self.now = 1000.0
        self.lines: list[str] = []
        self.events = Throttle(60.0, burst=5, write=lambda context, message: self.lines.append(f"{context}: {message}"), clock=lambda: self.now)
        self.detector = GuiStallDetector(events=self.events, clock=lambda: self.now)

    def beat(self, after: float) -> None:
        self.now += after
        self.detector._beat()

    def test_quiet_while_the_gui_thread_keeps_up(self) -> None:
        for _ in range(600):  # a minute of beats
            self.beat(0.1)
        self.beat(0.28)  # blocked for about 0.23 s: under the threshold
        self.beat(0.12)
        self.assertEqual(self.lines, [])

    def test_a_blocked_gui_thread_is_logged_with_how_long(self) -> None:
        self.beat(0.1)
        self.beat(0.85)  # the block began up to 0.1 s before the late beat: 0.8 s, give or take 0.05
        self.assertEqual(self.lines, ["gui: the window thread was blocked for 0.8 s"])
        self.beat(12.3)
        self.assertEqual(self.lines[-1], "gui: the window thread was blocked for 12 s")

    def test_where_it_was_goes_in_the_line(self) -> None:
        self.beat(0.1)
        self.detector._sample = (self.detector._last, "_check_workspace (main_window.py:1760)")
        self.beat(0.65)
        self.detector._sample = (self.detector._last, "")
        self.beat(0.65)
        self.assertEqual(
            self.lines,
            [
                "gui: the window thread was blocked for 0.6 s; it was in _check_workspace (main_window.py:1760)",
                "gui: the window thread was blocked for 0.6 s; it was running no Python code "
                "(Qt's own work, or waiting for another thread)",
            ],
        )

    def test_a_look_from_an_earlier_stall_is_not_used(self) -> None:
        self.beat(0.1)
        self.detector._sample = (self.detector._last - 5.0, "refresh_library (main_window.py:1120)")
        self.beat(0.85)
        self.assertEqual(self.lines, ["gui: the window thread was blocked for 0.8 s"])

    def test_the_lines_are_throttled(self) -> None:
        for _ in range(20):  # blocked for 0.6 s every second
            self.beat(0.65)
            for _ in range(4):
                self.beat(0.1)
        self.assertEqual(len(self.lines), 5)
        for _ in range(600):  # a quiet minute: the count goes in once there is room
            self.beat(0.1)
        self.assertEqual(len(self.lines), 6)
        self.assertRegex(self.lines[-1], r"blocked for 0\.6 s \[15 more times in the last \d+ s; this was the last\]$")


class StallDetectorLiveTests(unittest.TestCase):
    """The real detector, its watcher thread and its clock, on a GUI thread really blocked."""

    def test_a_real_block_is_caught_with_where_it_was_and_idle_time_is_quiet(self) -> None:
        qt_app()
        lines: list[str] = []
        events = Throttle(60.0, burst=5, write=lambda context, message: lines.append(f"{context}: {message}"))
        detector = GuiStallDetector(events=events)
        detector.start()
        self.addCleanup(detector.stop)
        run_qt(0.6)
        self.assertEqual(lines, [])

        def block_the_gui_thread() -> None:
            time.sleep(0.8)

        QTimer.singleShot(0, block_the_gui_thread)
        self.assertTrue(wait_until(lambda: lines, timeout=3))
        run_qt(0.3)
        self.assertEqual(len(lines), 1, lines)
        self.assertRegex(lines[0], r"^gui: the window thread was blocked for (0\.[6-9]|1\.[0-2]) s")
        self.assertRegex(lines[0], r"; it was in block_the_gui_thread \(test_gui_thread\.py:\d+\)")

    def test_the_clock_stops_only_while_the_pc_sleeps(self) -> None:
        start_awake, start = main_window.awake_seconds(), time.monotonic()
        time.sleep(0.2)
        self.assertAlmostEqual(main_window.awake_seconds() - start_awake, time.monotonic() - start, delta=0.04)


class ProPresenterWindowTests(unittest.TestCase):
    """The window timers look among the process ids the workspace check found, with no snapshot."""

    def test_known_process_ids_need_no_snapshot(self) -> None:
        with mock.patch.object(propresenter, "propresenter_pids", side_effect=AssertionError("a snapshot was taken")):
            self.assertIsNone(propresenter.propresenter_main_window(()))
            self.assertIsNone(propresenter.propresenter_window_rect(()))
            self.assertIsNone(propresenter.propresenter_main_window([4]))  # the System process: no windows

    def test_with_no_ids_known_it_takes_a_snapshot(self) -> None:
        with mock.patch.object(propresenter, "propresenter_pids", return_value=[]) as pids:
            self.assertIsNone(propresenter.propresenter_main_window())
        pids.assert_called_once_with()

    def test_the_window_timers_pass_the_known_ids(self) -> None:
        seen: list = []
        pane = SimpleNamespace(
            _resizing=False,
            _pp_pids=(23256,),
            _native_hwnd=lambda: 0x500,
            _taskbar_owner=None,
            config=SimpleNamespace(always_on_top=True),
            _shown_for_workspace=True,
            _shown_by_hand=False,
            _detach_hidden_owner=lambda: None,
            _note_attached=lambda found: None,
            _show_if_windows_hid_it=lambda: None,
        )
        with (
            mock.patch.object(main_window, "propresenter_window_rect", lambda known=None: seen.append(known)),
            mock.patch.object(main_window, "propresenter_main_window", lambda known=None: seen.append(known)),
        ):
            MainWindow._snap_to_propresenter(pane)
            MainWindow._stack_above_propresenter(pane)
        self.assertEqual(seen, [(23256,), (23256,)])


if __name__ == "__main__":
    unittest.main()
