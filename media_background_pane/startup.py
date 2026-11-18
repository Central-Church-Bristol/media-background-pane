from __future__ import annotations

import os
import sys
import winreg
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "MediaBackgroundPane"
ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "Media Background Pane.exe"


def _command() -> str:
    """What Windows runs at sign-in: Media Background Pane.exe (Python runs inside it, 18 Nov, so
    Task Manager shows the pane, not Python); without it, the venv's pythonw.exe with main.py.
    The pane writes this at every start, so an older pythonw.exe entry is replaced."""
    if LAUNCHER.is_file():
        return f'"{LAUNCHER}" --startup'
    pythonw = ROOT / ".venv" / "Scripts" / "pythonw.exe"
    script = ROOT / "main.py"
    exe = pythonw if pythonw.is_file() else Path(sys.executable)
    return f'"{exe}" "{script}" --startup'


def process_file() -> str:
    """The program this process is (GetModuleFileName): Media Background Pane.exe when Python runs inside it."""
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    return buffer.value if ctypes.windll.kernel32.GetModuleFileNameW(None, buffer, len(buffer)) else ""


def inside_launcher() -> bool:
    """Python runs inside Media Background Pane.exe (18 Nov), not in a pythonw.exe of its own."""
    return Path(process_file()).name.lower() == LAUNCHER.name.lower()


def how_started() -> str:
    """For the session log."""
    if inside_launcher():
        return "Python runs inside Media Background Pane.exe"
    separate = os.environ.get("MEDIA_BACKGROUND_PANE_SEPARATE")
    if separate:  # (the exe couldn't, and started the venv's pythonw.exe instead, saying why)
        return f"Media Background Pane.exe couldn't run Python inside itself ({separate}), so it runs in {Path(sys.executable).name}"
    return f"Started with {Path(sys.executable).name}"


def is_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return bool(value)
    except OSError:
        return False


def set_enabled(enabled: bool) -> None:
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
    ) as key:
        if enabled:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, _command())
        else:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass
