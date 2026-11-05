from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

os.environ["QT_FFMPEG_DECODING_HW_ACCEL"] = "0"
os.environ.setdefault("NDI_DISABLE_HARDWARE_ACCELERATION", "1")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from .config import Config
from .log import enable_crash_log, install_error_logging, log_event
from .main_window import MainWindow, make_app_icon
from .priority import prefer_livestream_apps
from .timing import begin_precise_timer, end_precise_timer


def _prepare_windows_shell() -> None:
    if sys.platform != "win32":
        return
    import ctypes

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("MediaBackgroundPane.1")
    except OSError:
        pass
    try:
        ctypes.windll.kernel32.FreeConsole()
    except OSError:
        pass


def main() -> None:
    startup = "--startup" in sys.argv
    install_error_logging()
    enable_crash_log()
    # One line per start, so the log shows it works and when the pane started.
    launched = " --startup" if startup else ""
    log_event("start", f"pid {os.getpid()}, {Path(sys.executable).name}{launched}, Python {platform.python_version()}")
    _prepare_windows_shell()
    begin_precise_timer()
    prefer_livestream_apps()
    try:
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_CompressHighFrequencyEvents, True)
    except AttributeError:
        pass
    app = QApplication(sys.argv)
    app.setApplicationName("Media Background Pane")
    app.setWindowIcon(make_app_icon())
    app.setQuitOnLastWindowClosed(False)
    qss = Path(__file__).with_name("style.qss")
    if qss.is_file():
        check = Path(__file__).with_name("check.png").resolve().as_posix()
        app.setStyleSheet(
            qss.read_text(encoding="utf-8").replace("CHECKMARK_URL", check)
        )
    config = Config.load()
    window = MainWindow(config, startup_mode=startup)
    try:
        sys.exit(app.exec())
    finally:
        end_precise_timer()
