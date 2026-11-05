"""Which ProPresenter workspace is open, read from ProPresenter's own log.

Fake log folders, process ids and clocks; nothing here reads the real
ProPresenter files or talks to the network.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from media_background_pane import propresenter  # noqa: E402

EVENING = r"C:\Users\avbri\AppData\Roaming\RenewedVision\ProPresenter\LocalWorkspaces\Evening Service"
DEFAULT = r"C:\Users\avbri\Documents\ProPresenter"
OTHER = r"C:\Users\avbri\AppData\Roaming\RenewedVision\ProPresenter\LocalWorkspaces\Morning"
# As ProPresenter writes it: JSON escaped twice, and doubled backslashes on Base=.
PATH_SETTINGS = (
    r'Workspaces=["{\\u0022Name\\u0022:\\u0022Evening Service\\u0022,\\u0022Active\\u0022:true,'
    r'\\u0022Path\\u0022:\\u0022C:\\Users\\avbri\\AppData\\Roaming\\RenewedVision\\ProPresenter'
    r'\\LocalWorkspaces\\Evening Service\\u0022}"];' "\r\n"
    r"Base=C:\\Users\\avbri\\AppData\\Roaming\\RenewedVision\\ProPresenter\\LocalWorkspaces\\Evening Service;"
    "\r\n"
)


def run_log(pid: int, workspace: str | None, *, stage: str | None = None, filler: int = 1000) -> list[str]:
    """One ProPresenter run's log lines, shaped like the real ones."""
    stamp = "04/11/2026 18:54:23"
    lines = [f"{stamp}\t0:00:00.172\tP{pid} T2  [R]\tINFO - Main Started"]
    for i in range(filler):
        lines.append(f"{stamp}\t0:00:01.{i % 1000:03d}\tP{pid} T{5 + i % 9} [R]\tINFO - Loaded part {i}")
    if workspace is not None:
        lines.append(f"{stamp}\t0:00:03.475\tP{pid} T14 [R]\tINFO - Opened [{stage or workspace}\\Configuration\\Stage]")
        lines.append(f"{stamp}\t0:00:04.389\tP{pid} T14 [R]\tINFO - Opened [{workspace}\\Configuration\\Workspace]")
        lines.append(f"{stamp}\t0:00:04.591\tP{pid} T14 [R]\tINFO - Opened [{workspace}\\Themes\\Evening\\Theme]")
        for i in range(200):
            lines.append(f"{stamp}\t0:00:05.{i:03d}\tP{pid} T9 [R]\tINFO - Notifying client of video input device change")
    return lines


def write_log(path: Path, lines: list[str], *, nul_inside: str | None = None) -> None:
    """Write a log the way ProPresenter does: byte-order mark, CRLF, and NUL bytes."""
    data = ("\r\n".join(lines) + "\r\n").encode("utf-8")
    if nul_inside:
        word = nul_inside.encode("utf-8")
        data = data.replace(word, b"\x00".join(word[i : i + 1] for i in range(len(word))))
    path.write_bytes(b"\xef\xbb\xbf" + data + b"\x00" * 64)


class Folder:
    """A fake ProPresenter: its Logs folder, PathSettings.proPaths and process ids."""

    def __init__(self, test: unittest.TestCase) -> None:
        self._dir = tempfile.TemporaryDirectory()
        test.addCleanup(self._dir.cleanup)
        root = Path(self._dir.name)
        self.logs = root / "Logs"
        self.logs.mkdir()
        self.path_settings = root / "PathSettings.proPaths"
        self.path_settings.write_text(PATH_SETTINGS, encoding="utf-8")
        self.now = 1000.0
        for patch in (
            mock.patch.object(propresenter, "PP_LOGS", self.logs),
            mock.patch.object(propresenter, "PATH_SETTINGS", self.path_settings),
            mock.patch.object(propresenter, "NETWORK_PREFS", root / "no NetworkPreferences.proPref"),
            mock.patch.dict(propresenter._log_workspaces, clear=True),
            mock.patch.dict(propresenter._first_seen, clear=True),
            mock.patch.dict(propresenter._no_log_line, clear=True),
            mock.patch.dict(propresenter._api_bases, clear=True),
        ):
            patch.start()
            test.addCleanup(patch.stop)

    def clock(self) -> float:
        return self.now

    def read(self, *pids: int):
        return propresenter.open_workspace(list(pids), clock=self.clock)


class LogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pp = Folder(self)

    def test_evening_service_is_read_from_the_log(self) -> None:
        write_log(self.pp.logs / "Log.log", run_log(23256, EVENING))
        reading = self.pp.read(23256)
        self.assertEqual((reading.name, reading.source, reading.pid), ("Evening Service", "log", 23256))
        self.assertTrue(reading.is_named("Evening Service"))
        self.assertTrue(reading.is_named("evening service"))
        self.assertIn("P23256 opened workspace Evening Service", reading.detail)

    def test_the_default_workspace_is_not_evening_service(self) -> None:
        write_log(self.pp.logs / "Log.log", run_log(14124, DEFAULT))
        reading = self.pp.read(14124)
        self.assertEqual((reading.name, reading.source), ("ProPresenter", "log"))
        self.assertFalse(reading.is_named("Evening Service"))
        self.assertIn("the default workspace", reading.detail)

    def test_only_the_configuration_workspace_line_counts(self) -> None:
        # Stage, themes and playlists are "Opened" too, and can sit in other folders.
        write_log(self.pp.logs / "Log.log", run_log(23256, EVENING, stage=OTHER))
        self.assertEqual(self.pp.read(23256).name, "Evening Service")

    def test_nul_bytes_inside_the_line_are_ignored(self) -> None:
        write_log(self.pp.logs / "Log.log", run_log(23256, EVENING), nul_inside="Evening Service")
        self.assertEqual(self.pp.read(23256).name, "Evening Service")

    def test_unknown_until_the_line_is_logged(self) -> None:
        write_log(self.pp.logs / "Log.log", run_log(23256, None))
        reading = self.pp.read(23256)
        self.assertEqual((reading.name, reading.source), (None, "unknown"))
        self.pp.now += 4
        write_log(self.pp.logs / "Log.log", run_log(23256, EVENING))
        self.assertEqual(self.pp.read(23256).source, "log")

    def test_a_run_that_never_logs_its_workspace_falls_back_to_base(self) -> None:
        write_log(self.pp.logs / "Log.log", run_log(23256, None))
        self.assertEqual(self.pp.read(23256).source, "unknown")
        self.pp.now += propresenter._LOG_WAIT
        reading = self.pp.read(23256)
        self.assertEqual((reading.name, reading.source), ("Evening Service", "Base="))
        self.assertIn("no workspace line for P23256", reading.detail)

    def test_another_runs_line_is_not_used(self) -> None:
        write_log(self.pp.logs / "Log.log", run_log(111, DEFAULT))
        write_log(self.pp.logs / "Log.1.log", run_log(222, DEFAULT))
        self.assertEqual(self.pp.read(333).source, "unknown")
        self.pp.now += propresenter._LOG_WAIT
        self.assertEqual(self.pp.read(333).source, "Base=")

    def test_the_run_in_log_1_is_found(self) -> None:
        # Just after ProPresenter starts, its log can still be the previous one.
        write_log(self.pp.logs / "Log.log", run_log(9000, DEFAULT))
        write_log(self.pp.logs / "Log.1.log", run_log(23256, EVENING))
        self.assertEqual(self.pp.read(23256).name, "Evening Service")

    def test_an_unreadable_log_falls_back_to_base_at_once(self) -> None:
        (self.pp.logs / "Log.log").mkdir()  # cannot be opened as a file
        reading = self.pp.read(23256)
        self.assertEqual((reading.name, reading.source), ("Evening Service", "Base="))
        self.assertIn("could not read Log.log", reading.detail)

    def test_no_log_folder_falls_back_to_base_at_once(self) -> None:
        self.pp.logs.rmdir()
        reading = self.pp.read(23256)
        self.assertEqual(reading.source, "Base=")
        self.assertIn("no ProPresenter log in", reading.detail)

    def test_no_log_and_no_base_is_unknown(self) -> None:
        self.pp.logs.rmdir()
        self.pp.path_settings.unlink()
        self.assertEqual(self.pp.read(23256).source, "unknown")

    def test_the_answer_is_kept_for_the_run(self) -> None:
        write_log(self.pp.logs / "Log.log", run_log(23256, EVENING))
        self.assertEqual(self.pp.read(23256).name, "Evening Service")
        with mock.patch.object(propresenter, "_read_log_workspace", side_effect=AssertionError("read again")):
            for _ in range(5):
                self.assertEqual(self.pp.read(23256).name, "Evening Service")

    def test_a_new_run_with_the_same_process_id_is_read_again(self) -> None:
        write_log(self.pp.logs / "Log.log", run_log(23256, EVENING))
        self.assertEqual(self.pp.read(23256).name, "Evening Service")
        self.assertEqual(self.pp.read().source, "not running")
        write_log(self.pp.logs / "Log.log", run_log(23256, DEFAULT))
        self.assertEqual(self.pp.read(23256).name, "ProPresenter")

    def test_the_newest_run_is_the_one_that_counts(self) -> None:
        write_log(self.pp.logs / "Log.log", run_log(222, EVENING))
        write_log(self.pp.logs / "Log.1.log", run_log(111, DEFAULT))
        self.assertEqual(self.pp.read(111).name, "ProPresenter")
        self.pp.now += 5
        reading = self.pp.read(111, 222)
        self.assertEqual((reading.pid, reading.name), (222, "Evening Service"))

    def test_not_running(self) -> None:
        reading = self.pp.read()
        self.assertEqual((reading.source, reading.running), ("not running", False))
        self.assertFalse(reading.is_named("Evening Service"))

    def test_nothing_is_asked_over_the_network(self) -> None:
        write_log(self.pp.logs / "Log.log", run_log(23256, EVENING))
        with (
            mock.patch.object(propresenter, "_request", side_effect=AssertionError("network")),
            mock.patch.object(propresenter, "urlopen", side_effect=AssertionError("network")),
            mock.patch.object(propresenter, "propresenter_pids", return_value=[23256]),
        ):
            self.assertTrue(propresenter.workspace_is_open("Evening Service"))
            self.assertFalse(propresenter.workspace_is_open("Morning"))

    def test_the_real_logs_line_shape(self) -> None:
        # Copied from ProPresenter's log of 4 Nov 2026.
        line = (
            "04/11/2026 18:54:27\t0:00:04.389\tP23256 T14 [R]\tINFO - Opened "
            "[C:\\Users\\avbri\\AppData\\Roaming\\RenewedVision\\ProPresenter\\LocalWorkspaces"
            "\\Evening Service\\Configuration\\Workspace]"
        )
        match = propresenter._LOG_OPENED.search(line)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "23256")


class ApiAddressTests(unittest.TestCase):
    """The Video Input button asks ProPresenter for its status every second."""

    def setUp(self) -> None:
        self.pp = Folder(self)
        self.pids = [5000]
        self.calls: list[str] = []
        self.status_fails = False
        for patch in (
            mock.patch.object(propresenter, "propresenter_pids", side_effect=lambda: list(self.pids)),
            mock.patch.object(propresenter, "_request", side_effect=self._request),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def _request(self, url: str, timeout: float = 0.8):
        self.calls.append(url)
        if url.endswith(":50001/version"):
            return 200, b'{"name": "ProPresenter", "host_description": "ProPresenter 7"}'
        if url.endswith("/version"):
            raise URLError("refused")
        if url.endswith("/v1/status/layers"):
            if self.status_fails:
                raise URLError("gone")
            return 200, b'{"video_input": true}'
        raise HTTPError(url, 404, "no", None, None)

    def probes(self) -> int:
        return sum(1 for url in self.calls if url.endswith("/version"))

    def test_the_ports_are_probed_once_per_run(self) -> None:
        for _ in range(5):
            self.assertTrue(propresenter.video_input_layer_active())
        self.assertEqual(self.probes(), 3)  # 1025, 50000, then 50001 answers
        self.assertEqual(sum(1 for url in self.calls if url.endswith("/v1/status/layers")), 5)

    def test_a_connection_error_means_probing_again(self) -> None:
        self.assertTrue(propresenter.video_input_layer_active())
        self.status_fails = True
        self.assertIsNone(propresenter.video_input_layer_active())
        self.status_fails = False
        self.calls.clear()
        self.assertTrue(propresenter.video_input_layer_active())
        self.assertEqual(self.probes(), 3)

    def test_a_new_run_is_probed_again(self) -> None:
        self.assertTrue(propresenter.video_input_layer_active())
        self.pids = [6000]
        self.calls.clear()
        self.assertTrue(propresenter.video_input_layer_active())
        self.assertEqual(self.probes(), 3)

    def test_not_running_asks_nothing(self) -> None:
        self.pids = []
        self.assertIsNone(propresenter.video_input_layer_active())
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
