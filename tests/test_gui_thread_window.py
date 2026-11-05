"""The real MainWindow, built with its background threads and stall detector.

test_gui_thread.py tests these parts on stand-ins. This builds the window
itself, to check how __init__ wires them: the library arrives from its
thread, ProPresenter is asked from its own thread, a blocked GUI thread is
written to the session log, and quitting stops the threads.

It needs a QApplication, and the other tests use QCoreApplication, so it runs
in its own process, on Qt's offscreen platform (no window or tray icon
appears). The engine, LAN remote and tray are fakes; ProPresenter, the
registry, the process priority and the live pane's files are never touched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class MainWindowThreadsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._folder = tempfile.TemporaryDirectory()
        root = Path(cls._folder.name)
        env = dict(os.environ)
        env.update(
            QT_QPA_PLATFORM="offscreen",
            MBP_TEST_ROOT=str(root),
            LOCALAPPDATA=str(root / "LocalAppData"),
            TEMP=str(root / "Temp"),
            TMP=str(root / "Temp"),
        )
        done = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--child"],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(ROOT),
            env=env,
        )
        lines = [line for line in done.stdout.splitlines() if line.startswith("{")]
        if not lines:
            raise AssertionError(f"no report from the child (exit {done.returncode}):\n{done.stdout}\n{done.stderr[-3000:]}")
        cls.report = json.loads(lines[-1])

    @classmethod
    def tearDownClass(cls) -> None:
        cls._folder.cleanup()

    def test_no_errors(self) -> None:
        self.assertEqual(self.report["errors"], [])

    def test_the_library_arrives_from_its_thread(self) -> None:
        self.assertEqual(self.report["files"], ["Holy Fire.mov", "Lamb.png"])
        self.assertEqual(self.report["thumbs"], ["Holy Fire.mov", "Lamb.png"])
        self.assertEqual(self.report["scanned_on"], ["library calls"])
        self.assertLess(self.report["init_seconds"], 2.0)

    def test_propresenter_is_asked_from_its_own_thread(self) -> None:
        self.assertTrue(self.report["api_threads"])
        self.assertEqual(set(self.report["api_threads"]), {"propresenter calls"})
        self.assertTrue(self.report["video_input_on"])
        self.assertIn("video input: switched ProPresenter's first video input on", self.report["log"])

    def test_a_blocked_gui_thread_goes_in_the_session_log(self) -> None:
        stalls = [line for line in self.report["log"] if "gui: the window thread was blocked for" in line]
        self.assertEqual(len(stalls), 1, self.report["log"])
        self.assertIn("it was in block_the_gui_thread", stalls[0])

    def test_quitting_stops_the_threads(self) -> None:
        self.assertEqual(self.report["threads_after_quit"], [])


def child() -> None:
    """Runs in the child process: build the window, use it, quit, and print a report."""
    root = Path(os.environ["MBP_TEST_ROOT"])
    (root / "LocalAppData").mkdir(exist_ok=True)
    (root / "Temp").mkdir(exist_ok=True)

    from PySide6.QtCore import QObject, QTimer, Signal
    from PySide6.QtGui import QColor, QImage
    from PySide6.QtWidgets import QApplication

    app = QApplication([])
    errors: list[str] = []
    sys.excepthook = lambda kind, exc, tb: errors.append(f"{kind.__name__}: {exc}")
    threading.excepthook = lambda args: errors.append(f"thread {args.thread.name}: {args.exc_type.__name__}: {args.exc_value}")

    from media_background_pane import config as config_module
    from media_background_pane import library, log, main_window, propresenter

    session_log = root / "session.log"
    log.session_log_path = lambda: session_log
    log.fallback_log_path = lambda: root / "fallback.log"
    thumbs = root / "thumbs"
    thumbs.mkdir()
    library.thumb_cache_dir = lambda: thumbs
    config_module.thumb_cache_dir = lambda: thumbs

    api_threads: list[str] = []
    layer = {"on": False}

    def fake_status():
        api_threads.append(threading.current_thread().name)
        return layer["on"]

    def fake_trigger(_duration=None):
        api_threads.append(threading.current_thread().name)
        layer["on"] = True
        return True

    def no_api(*_args, **_kwargs):
        raise AssertionError("the real ProPresenter API was called")

    propresenter._request = no_api
    main_window.video_input_layer_active = fake_status
    main_window.trigger_first_video_input = fake_trigger
    main_window.clear_video_input_layer = lambda _duration=None: True
    main_window.propresenter_pids = lambda: []
    propresenter.propresenter_pids = lambda: []
    main_window.prefer_livestream_apps = lambda: None
    main_window.set_startup_enabled = lambda _enabled: None
    main_window.PATH_SETTINGS = root / "no ProPresenter" / "PathSettings.proPaths"

    scanned_on: set[str] = set()
    real_scan = main_window.scan_library

    def watched_scan(*args, **kwargs):
        scanned_on.add(threading.current_thread().name)
        return real_scan(*args, **kwargs)

    main_window.scan_library = watched_scan

    class FakeLanServer:
        def __init__(self, *_args) -> None:
            pass

        def start(self):
            return None  # as when the port is taken: no LAN remote

    class FakeEngine(QObject):
        preview_changed = Signal(QImage)
        program_changed = Signal(QImage)
        taken = Signal()

        def __init__(self, _config, parent=None) -> None:
            super().__init__(parent)
            self.preview = SimpleNamespace(path=None, has_source=False, is_video=False)
            self.program = SimpleNamespace(path=None, has_source=False)
            self.dimmer = 1.0
            self.gui_enabled = False
            self.ndi_status = ("off", False)
            self.decoder_status = "idle"

        def set_mix(self, _value) -> None:
            pass

        def set_dimmer(self, _value, allow_pause=True) -> None:
            pass

        def close(self) -> None:
            pass

    def fake_build_tray(window) -> None:
        window.tray = SimpleNamespace(setToolTip=lambda *_args: None)

    class TestConfig(config_module.Config):
        def save(self) -> None:
            pass  # never the live settings.json

    main_window.LanServer = FakeLanServer
    main_window.Engine = FakeEngine
    main_window.MainWindow._build_tray = fake_build_tray

    media = root / "Media Backgrounds"
    media.mkdir()
    still = QImage(320, 180, QImage.Format.Format_RGB32)
    still.fill(QColor(200, 160, 40))
    still.save(str(media / "Lamb.png"))
    video = media / "Holy Fire.mov"
    video.write_bytes(b"\x00" * 4096)
    cached = QImage(160, 90, QImage.Format.Format_RGB32)
    cached.fill(QColor(90, 20, 10))
    cached.save(str(library.thumb_cache_path(video)), "JPG", 80)  # so no QMediaPlayer is needed

    config = TestConfig()
    config.media_folder = str(media)
    config.start_with_windows = False
    started = time.perf_counter()
    window = main_window.MainWindow(config)
    init_seconds = time.perf_counter() - started
    report: dict = {"init_seconds": init_seconds}

    def block_the_gui_thread() -> None:
        time.sleep(0.8)

    def look() -> None:
        report["files"] = [path.name for path in window._files]
        report["thumbs"] = sorted(Path(path).name for path in window.thumbs._buttons if window.thumbs.thumb_image(path) is not None)
        report["video_input_on"] = window.video_input_button.isChecked()

    QTimer.singleShot(1500, look)
    QTimer.singleShot(1600, block_the_gui_thread)
    QTimer.singleShot(3200, app.quit)
    app.exec()

    workers = (window._pp_calls, window._library_calls)
    for worker in workers:
        worker._thread.join(2.0)
    watcher = window._stall_detector._watcher
    still_running = [worker.name for worker in workers if worker.thread_alive()]
    if watcher is not None and watcher.is_alive():
        still_running.append("gui stall watch")
    report.update(
        errors=errors,
        scanned_on=sorted(scanned_on),
        api_threads=api_threads,
        threads_after_quit=still_running,
        log=[line.split(" ", 2)[-1] for line in session_log.read_text(encoding="utf-8").splitlines()] if session_log.exists() else [],
    )
    print(json.dumps(report))
    sys.stdout.flush()


if __name__ == "__main__":
    if "--child" in sys.argv:
        child()
    else:
        unittest.main()
