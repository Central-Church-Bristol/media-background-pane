"""Media Background Pane.exe runs Python inside itself (18 Nov), as CentraLight.exe has since 8 Nov. Before, it
started the venv's pythonw.exe, so Task Manager listed the pane as "Python". The launcher (launcher/Program.cs)
loads the venv's own Python (pyvenv.cfg's home) with Python 3.14's Configuration API and runs main.py in its own
process; if it can't, it starts .venv\\Scripts\\pythonw.exe, saying why. Start with Windows runs the exe too.
The launcher tests build a test copy into a temporary folder with a stand-in main.py and a venv (a junction to the
one running the tests, or a pretend one). Adapted from CentraLight's tests/test_launcher_inside_m18.py."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from media_background_pane import startup

ROOT = Path(__file__).resolve().parents[1]
CSC = Path(r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe")
IN_A_VENV = (Path(sys.prefix) / "pyvenv.cfg").is_file()

STAND_IN = '''"""What the Python that Media Background Pane.exe runs sees, in found.json."""
import ctypes, json, os, sys

buffer = ctypes.create_unicode_buffer(32768)
ctypes.windll.kernel32.GetModuleFileNameW(None, buffer, 32768)
found = {"process": buffer.value, "executable": sys.executable, "prefix": sys.prefix, "argv": sys.argv[1:],
         "path0": sys.path[0], "separate": os.environ.get("MEDIA_BACKGROUND_PANE_SEPARATE"),
         "no_stdout": sys.stdout is None}
try:
    import numpy, cv2, NDIlib, PySide6.QtCore, PySide6.QtMultimedia
    found["packages"] = True
except Exception as exc:
    found["packages"] = repr(exc)
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "found.json"), "w", encoding="utf-8") as f:
    json.dump(found, f)
sys.exit(7)
'''


@unittest.skipUnless(CSC.is_file() and IN_A_VENV, "needs the C# compiler that comes with Windows, and a venv")
class LauncherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = Path(tempfile.mkdtemp(prefix="media-background-pane-launcher-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        (self.folder / "main.py").write_text(STAND_IN, encoding="utf-8")
        sys.path.insert(0, str(ROOT))
        import build_launcher

        self.exe = self.folder / "Media Background Pane.exe"
        build_launcher.compile_exe(self.exe, ROOT / "launcher" / "icon.ico")

    def found(self, wait: float = 0.0) -> dict:
        path = self.folder / "found.json"
        deadline = time.monotonic() + wait
        while not path.is_file() and time.monotonic() < deadline:
            time.sleep(0.1)
        return json.loads(path.read_text(encoding="utf-8"))

    def test_python_runs_inside_it_with_the_venv(self) -> None:
        link = self.folder / ".venv"
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), sys.prefix], check=True, capture_output=True)
        self.addCleanup(os.rmdir, link)  # (the junction only, never what it points at; before the folder goes)
        result = subprocess.run([str(self.exe), "--startup", "two words"], cwd=self.folder, timeout=60)
        self.assertEqual(result.returncode, 7)  # (main.py's exit code)
        found = self.found()
        self.assertEqual(Path(found["process"]), self.exe)  # (Windows sees the pane's exe, not pythonw.exe)
        self.assertEqual(Path(found["executable"]), link / "Scripts" / "pythonw.exe")
        self.assertEqual(Path(found["prefix"]), link)  # (the venv...)
        self.assertIs(found["packages"], True)  # (...with its packages: numpy, OpenCV, NDI, Qt)
        self.assertEqual(found["argv"], ["--startup", "two words"])
        self.assertEqual(Path(found["path0"]), self.folder)  # (main.py's folder first, for media_background_pane)
        self.assertIsNone(found["separate"])
        self.assertTrue(found["no_stdout"])  # (no console, as with pythonw.exe)

    def test_with_a_python_it_cant_run_it_starts_pythonw_saying_why(self) -> None:
        """A Python without the Configuration API (older than 3.14): the venv's pythonw.exe, as before 18 Nov."""
        base = Path(sys.base_prefix)
        old = self.folder / "old python"
        old.mkdir()
        shutil.copy(base / "python3.dll", old / "python39.dll")  # (a Python 3.9 DLL, as far as it can tell: no API)
        scripts = self.folder / ".venv" / "Scripts"
        scripts.mkdir(parents=True)
        (self.folder / ".venv" / "pyvenv.cfg").write_text(f"home = {old}\n", encoding="utf-8")
        dll = f"python{sys.version_info.major}{sys.version_info.minor}.dll"
        for name in ("pythonw.exe", dll, "python3.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
            if (base / name).is_file():
                shutil.copy(base / name, scripts / name)  # (a pythonw.exe that runs, with this Python's own library)
        env = dict(os.environ, PYTHONHOME=str(base))
        result = subprocess.run([str(self.exe), "--startup"], cwd=self.folder, env=env, timeout=60)
        self.assertEqual(result.returncode, 0)  # (it started the other one)
        found = self.found(wait=30.0)
        self.assertEqual(Path(found["process"]), scripts / "pythonw.exe")
        self.assertEqual(found["argv"], ["--startup"])
        self.assertIn("python39.dll has no Python Configuration API", found["separate"])


class StartupCommandTests(unittest.TestCase):
    def test_start_with_windows_runs_the_exe(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            exe = Path(folder) / "Media Background Pane.exe"
            exe.write_bytes(b"")
            with mock.patch.object(startup, "LAUNCHER", exe):
                self.assertEqual(startup._command(), f'"{exe}" --startup')

    def test_without_the_exe_it_runs_pythonw(self) -> None:
        with mock.patch.object(startup, "LAUNCHER", Path(tempfile.gettempdir()) / "no such pane.exe"):
            command = startup._command()
        self.assertTrue(command.endswith(f'"{startup.ROOT / "main.py"}" --startup'), command)
        self.assertIn("pythonw.exe", command.lower())


class HowStartedTests(unittest.TestCase):
    def test_inside_the_exe(self) -> None:
        with mock.patch.object(startup, "process_file", return_value=str(startup.LAUNCHER)):
            self.assertTrue(startup.inside_launcher())
            self.assertEqual(startup.how_started(), "Python runs inside Media Background Pane.exe")

    def test_in_a_pythonw_of_its_own(self) -> None:
        with mock.patch.object(startup, "process_file", return_value=r"C:\x\.venv\Scripts\pythonw.exe"), \
                mock.patch.dict(os.environ, {"MEDIA_BACKGROUND_PANE_SEPARATE": "no python3NN.dll in C:\\old"}):
            self.assertFalse(startup.inside_launcher())
            self.assertIn("couldn't run Python inside itself (no python3NN.dll in C:\\old)", startup.how_started())

    def test_an_explicit_app_id_only_outside_the_exe(self) -> None:
        from media_background_pane import app

        with mock.patch.object(app, "inside_launcher", return_value=True), \
                mock.patch("ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID") as set_id, \
                mock.patch("ctypes.windll.kernel32.FreeConsole"):
            app._prepare_windows_shell()
        set_id.assert_not_called()
        with mock.patch.object(app, "inside_launcher", return_value=False), \
                mock.patch("ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID") as set_id, \
                mock.patch("ctypes.windll.kernel32.FreeConsole"):
            app._prepare_windows_shell()
        set_id.assert_called_once_with("MediaBackgroundPane.1")


if __name__ == "__main__":
    unittest.main()
