"""When the pane shows and hides, what the session log says about it, Video
Input after a ProPresenter restart, and the tray "Show pane".

MainWindow's own methods run on a stand-in with fake windows, ProPresenter
process ids and a fake clock. No real window, tray icon or ProPresenter.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # test_workspace_detection's fakes

from media_background_pane import main_window  # noqa: E402
from media_background_pane.log import Throttle  # noqa: E402
from media_background_pane.main_window import MainWindow  # noqa: E402
from media_background_pane.propresenter import OpenWorkspace  # noqa: E402

PANE = 0x500
PP_WINDOW = 0x9A0
EVENING = OpenWorkspace("Evening Service", "log", 23256, "ProPresenter P23256 opened workspace Evening Service")
DEFAULT = OpenWorkspace("ProPresenter", "log", 23256, "ProPresenter P23256 opened the default workspace (ProPresenter)")
UNKNOWN = OpenWorkspace(None, "unknown", 23256, "ProPresenter P23256 has not logged its workspace yet")
NOT_RUNNING = OpenWorkspace(None, "not running", None, "ProPresenter is not running")


class World:
    """Fake Windows and ProPresenter, patched into main_window."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.now = 100.0
        self.lines: list[str] = []
        self.pids: list[int] = []
        self.reading = NOT_RUNNING
        self.visible: dict[int, bool] = {}
        self.owner: dict[int, int] = {}
        self.minimized: set[int] = set()
        self.cloaked: set[int] = set()
        self.rects = {PANE: (0, 830, 1200, 1080)}
        self.pp_window = (PP_WINDOW, (0, 0, 1920, 1080))
        self.stacked: list[tuple[int, int]] = []
        self.layer_active: bool | None = None
        self.triggers = 0
        self.clears = 0
        fakes = {
            "propresenter_pids": lambda: list(self.pids),
            "open_workspace": lambda pids, **_kw: self.reading,
            "prefer_livestream_apps": lambda: None,
            "native_window_visible": lambda hwnd: self.visible.get(hwnd, False),
            "window_owner": lambda hwnd: self.owner.get(hwnd, 0),
            "window_is_minimized": lambda hwnd: hwnd in self.minimized,
            "window_is_cloaked": lambda hwnd: hwnd in self.cloaked,
            "native_window_rect": lambda hwnd: self.rects.get(hwnd),
            "show_window_no_activate": lambda hwnd: self.visible.__setitem__(hwnd, True),
            "restore_window_no_activate": lambda hwnd: self.minimized.discard(hwnd),
            "propresenter_main_window": lambda: self.pp_window,
            "stack_window_above": lambda hwnd, pp: self.stacked.append((hwnd, pp)),
            "video_input_layer_active": lambda: self.layer_active,
            "trigger_first_video_input": self._trigger,
            "clear_video_input_layer": self._clear,
            "log_event": self.write,
        }
        for name, fake in fakes.items():
            patch = mock.patch.object(main_window, name, fake)
            patch.start()
            test.addCleanup(patch.stop)

    def _trigger(self, *_args) -> bool:
        self.triggers += 1
        return True

    def _clear(self, *_args) -> bool:
        self.clears += 1
        return True

    def write(self, context: str, message: str) -> None:
        self.lines.append(f"{context}: {message}")

    def clock(self) -> float:
        return self.now

    def logged(self, text: str) -> list[str]:
        return [line for line in self.lines if text in line]


class FakeTimer:
    def __init__(self) -> None:
        self.active = False

    def isActive(self) -> bool:
        return self.active

    def start(self) -> None:
        self.active = True

    def stop(self) -> None:
        self.active = False


class Pane:
    """MainWindow's show/hide, ProPresenter and Video Input code, on fake windows."""

    _check_workspace = MainWindow._check_workspace
    _read_propresenter = MainWindow._read_propresenter
    _workspace_decision = MainWindow._workspace_decision
    _note_decision = MainWindow._note_decision
    _show_if_windows_hid_it = MainWindow._show_if_windows_hid_it
    _note_window_state = MainWindow._note_window_state
    _stack_above_propresenter = MainWindow._stack_above_propresenter
    _note_attached = MainWindow._note_attached
    _pane_is_on_screen = MainWindow._pane_is_on_screen
    _try_trigger_video_input = MainWindow._try_trigger_video_input
    _rearm_video_input = MainWindow._rearm_video_input
    _toggle_video_input = MainWindow._toggle_video_input
    show_pane_on_top = MainWindow.show_pane_on_top
    _tray_actions = MainWindow._tray_actions
    _menu_actions = MainWindow._menu_actions

    def __init__(self, world: World) -> None:
        self.world = world
        self.config = SimpleNamespace(
            always_on_top=True,
            auto_show_on_workspace=True,
            workspace_name="Evening Service",
            fade_seconds=10,
        )
        self.qt_visible = False
        self._collapsed = False
        self._shown_for_workspace = False
        self._shown_by_hand = False
        self._pp_pids = None
        self._last_decision = None
        self._attached = None
        self._window_state = None
        self._events = Throttle(10.0, write=world.write, clock=world.clock)
        self._taskbar_owner = None
        self._video_input_triggered = False
        self._video_input_ignore_until = 0.0
        self._video_input_timer = FakeTimer()
        self._video_input_timer.start()
        self.video_input_button = SimpleNamespace(isChecked=lambda: self.button_wants_on)
        self.button_wants_on = False
        self.button_shows = False

    def isVisible(self) -> bool:
        return self.qt_visible

    def show(self) -> None:
        self.qt_visible = True
        self.world.visible[PANE] = True

    def hide(self) -> None:
        self.qt_visible = False
        self.world.visible[PANE] = False

    def _native_hwnd(self) -> int:
        return PANE

    def set_collapsed(self, collapsed: bool) -> None:
        self._collapsed = collapsed

    def _set_video_input_button(self, active: bool) -> None:
        self.button_shows = active

    def _detach_hidden_owner(self) -> None:
        pass

    def _hide_from_taskbar(self) -> None:
        pass

    def _snap_to_propresenter(self) -> None:
        pass

    def _sync_gui_enabled(self) -> None:
        pass

    def _bring_on_screen(self) -> None:
        pass

    def _open_settings(self) -> None:
        pass

    @property
    def on_screen(self) -> bool:
        return self.qt_visible and self.world.visible.get(PANE, False)


class Setup(unittest.TestCase):
    def setUp(self) -> None:
        self.world = World(self)
        self.pane = Pane(self.world)

    def tick(self, seconds: float = 1.0) -> None:
        self.world.now += seconds
        self.pane._check_workspace()

    def propresenter(self, reading: OpenWorkspace, *pids: int) -> None:
        self.world.reading = reading
        self.world.pids = list(pids)


class ShowHideTests(Setup):
    def test_shown_when_the_log_says_evening_service(self) -> None:
        self.propresenter(EVENING, 23256)
        self.tick()
        self.assertTrue(self.pane.on_screen)
        self.assertTrue(self.world.logged("propresenter: running: P23256"))
        self.assertTrue(
            self.world.logged(
                "workspace check: show: ProPresenter P23256 opened workspace Evening Service (from its log)"
            )
        )
        self.assertTrue(self.world.logged("pane: shown"))

    def test_hidden_with_the_reason_when_another_workspace_opens(self) -> None:
        self.propresenter(EVENING, 23256)
        self.tick()
        self.propresenter(DEFAULT, 23256)
        self.tick()
        self.assertFalse(self.pane.on_screen)
        self.assertTrue(self.world.logged("hide: ProPresenter P23256 opened the default workspace"))
        self.assertTrue(self.world.logged("not Evening Service (from its log)"))
        self.assertTrue(self.world.logged("pane: hidden"))

    def test_the_base_fallback_is_named_in_the_log(self) -> None:
        self.propresenter(
            OpenWorkspace("Evening Service", "Base=", 23256, "ProPresenter P23256: no line; Base= says workspace Evening Service"),
            23256,
        )
        self.tick()
        self.assertTrue(self.pane.on_screen)
        self.assertTrue(self.world.logged("from PathSettings Base=, as the log had no answer"))

    def test_unknown_leaves_the_pane_as_it_is(self) -> None:
        self.propresenter(UNKNOWN, 23256)
        self.tick()
        self.assertFalse(self.pane.on_screen)
        self.propresenter(EVENING, 23256)
        self.tick()
        self.propresenter(UNKNOWN, 23256)
        self.tick()
        self.assertTrue(self.pane.on_screen)
        self.assertTrue(self.world.logged("no change: ProPresenter P23256 has not logged its workspace yet"))

    def test_an_unchanged_decision_is_logged_once(self) -> None:
        self.propresenter(EVENING, 23256)
        for _ in range(100):
            self.tick()
        self.assertEqual(len(self.world.logged("workspace check:")), 1)

    def test_propresenter_starting_and_stopping_is_logged(self) -> None:
        self.tick()
        self.propresenter(EVENING, 23256)
        self.tick(20)
        self.propresenter(NOT_RUNNING)
        self.tick(20)
        self.propresenter(EVENING, 24044)
        self.tick(20)
        self.assertTrue(self.world.logged("propresenter: not running"))
        self.assertTrue(self.world.logged("propresenter: started: P23256"))
        self.assertTrue(self.world.logged("propresenter: stopped: P23256"))
        self.assertTrue(self.world.logged("propresenter: started: P24044"))

    def test_without_the_workspace_option_it_follows_propresenter_running(self) -> None:
        self.pane.config.auto_show_on_workspace = False
        self.propresenter(DEFAULT, 23256)
        self.tick()
        self.assertTrue(self.pane.on_screen)
        self.propresenter(NOT_RUNNING)
        self.tick()
        self.assertFalse(self.pane.on_screen)


class WindowsTests(Setup):
    def setUp(self) -> None:
        super().setUp()
        self.propresenter(EVENING, 23256)
        self.tick()
        self.world.owner[PANE] = PP_WINDOW

    def test_windows_hiding_the_pane_is_undone_and_logged(self) -> None:
        self.world.visible[PANE] = False  # Windows hid it; Qt still thinks it is up
        self.pane._stack_above_propresenter()
        self.assertTrue(self.pane.on_screen)
        self.assertTrue(
            self.world.logged(f"Windows had hidden the pane; showed it again (its owner window 0x{PP_WINDOW:X} is hidden)")
        )

    def test_flicker_cannot_flood_the_log(self) -> None:
        for _ in range(100):  # hidden and shown again 20 times a second for 5 s
            self.world.now += 0.05
            self.world.visible[PANE] = False
            self.pane._stack_above_propresenter()
        self.assertTrue(self.pane.on_screen)
        self.assertEqual(len(self.world.logged("Windows had hidden the pane")), 5)
        self.tick(10)
        lines = self.world.logged("Windows had hidden the pane")
        self.assertEqual(len(lines), 6)
        self.assertIn("[95 more times in the last", lines[-1])

    def test_a_pane_hidden_by_the_check_is_not_blamed_on_windows(self) -> None:
        self.propresenter(DEFAULT, 23256)
        self.tick()
        self.propresenter(EVENING, 23256)
        self.tick()
        self.assertTrue(self.pane.on_screen)
        self.assertEqual(self.world.logged("Windows had hidden the pane"), [])

    def test_the_propresenter_window_is_logged_when_it_changes(self) -> None:
        for _ in range(50):
            self.pane._stack_above_propresenter()
        self.assertEqual(len(self.world.logged("stacked above ProPresenter window")), 1)
        self.assertTrue(self.world.logged(f"stacked above ProPresenter window 0x{PP_WINDOW:X}, 1920x1080 at 0,0"))
        self.world.now += 11
        self.world.pp_window = (PP_WINDOW, (0, 0, 1280, 720))
        self.pane._stack_above_propresenter()
        self.assertTrue(self.world.logged("1280x720 at 0,0"))
        self.world.now += 11
        self.world.pp_window = None
        self.pane._stack_above_propresenter()
        self.assertTrue(self.world.logged("no ProPresenter window to stack above"))

    def test_how_the_pane_looks_to_windows_is_logged_when_it_changes(self) -> None:
        self.assertTrue(self.world.logged("pane window: on screen; 1200x250 at 0,830"))
        self.world.cloaked.add(PANE)
        self.tick(11)
        self.assertTrue(self.world.logged("pane window: cloaked: Windows keeps it off screen"))
        self.world.cloaked.clear()
        self.world.minimized.add(PANE)
        self.tick(11)
        self.assertTrue(self.world.logged("pane window: minimised"))


class VideoInputTests(Setup):
    def test_switched_on_again_after_a_propresenter_restart(self) -> None:
        self.propresenter(EVENING, 23256)
        self.tick()
        self.pane._try_trigger_video_input()
        self.assertEqual(self.world.triggers, 1)
        for _ in range(5):
            self.tick()
            self.pane._try_trigger_video_input()
        self.assertEqual(self.world.triggers, 1)
        self.propresenter(NOT_RUNNING)
        self.tick()
        self.propresenter(EVENING, 24044)
        self.tick()
        self.assertTrue(self.pane._video_input_timer.isActive())
        self.pane._try_trigger_video_input()
        self.assertEqual(self.world.triggers, 2)
        self.assertTrue(self.world.logged("new ProPresenter run: will switch its first video input on"))

    def test_the_operators_off_holds_for_the_run(self) -> None:
        self.propresenter(EVENING, 23256)
        self.tick()
        self.pane._try_trigger_video_input()
        self.pane.button_wants_on = False
        self.pane._toggle_video_input()
        self.assertEqual(self.world.clears, 1)
        for _ in range(5):
            self.tick()
            self.pane._try_trigger_video_input()
        self.assertEqual(self.world.triggers, 1)
        self.propresenter(EVENING, 24044)
        self.tick()
        self.pane._try_trigger_video_input()
        self.assertEqual(self.world.triggers, 2)

    def test_off_before_the_first_attempt_stops_the_retries(self) -> None:
        self.propresenter(EVENING, 23256)
        self.tick()
        self.pane.button_wants_on = False
        self.pane._toggle_video_input()
        self.assertFalse(self.pane._video_input_timer.isActive())
        self.pane._try_trigger_video_input()
        self.assertEqual(self.world.triggers, 0)

    def test_an_input_already_on_is_not_triggered_again(self) -> None:
        self.propresenter(EVENING, 23256)
        self.world.layer_active = True
        self.tick()
        self.pane._try_trigger_video_input()
        self.assertEqual(self.world.triggers, 0)
        self.assertTrue(self.pane.button_shows)


class TrayTests(Setup):
    def test_the_tray_menu_starts_with_show_pane(self) -> None:
        labels = [item[0] if item else None for item in self.pane._tray_actions()]
        self.assertEqual(labels[0], "Show pane")
        self.assertIn("Settings…", labels)
        self.assertEqual(labels[-1], "Quit")

    def test_show_pane_stays_up_until_propresenter_restarts(self) -> None:
        self.propresenter(DEFAULT, 23256)
        self.tick()
        self.assertFalse(self.pane.on_screen)
        self.pane.show_pane_on_top()
        self.assertTrue(self.pane.on_screen)
        self.assertIn((PANE, PP_WINDOW), self.world.stacked)
        self.assertTrue(self.world.logged("shown from the tray menu, whatever the workspace check says"))
        for _ in range(30):
            self.tick()
        self.assertTrue(self.pane.on_screen)
        self.assertTrue(self.world.logged('hide, but the tray "Show pane" keeps it up'))
        self.propresenter(DEFAULT, 24044)
        self.tick()
        self.assertFalse(self.pane.on_screen)
        self.assertTrue(self.world.logged('tray "Show pane" ends: ProPresenter closed or restarted'))

    def test_show_pane_hands_back_once_the_check_agrees(self) -> None:
        self.propresenter(DEFAULT, 23256)
        self.tick()
        self.pane.show_pane_on_top()
        self.propresenter(EVENING, 23256)
        self.tick()
        self.assertFalse(self.pane._shown_by_hand)
        self.assertTrue(self.world.logged('tray "Show pane" ends: the workspace check agrees'))
        self.propresenter(DEFAULT, 23256)
        self.tick()
        self.assertFalse(self.pane.on_screen)

    def test_show_pane_brings_back_a_minimised_or_hidden_pane(self) -> None:
        self.propresenter(EVENING, 23256)
        self.tick()
        self.world.minimized.add(PANE)
        self.world.visible[PANE] = False
        self.pane.show_pane_on_top()
        self.assertNotIn(PANE, self.world.minimized)
        self.assertTrue(self.pane.on_screen)


class EndToEndTests(unittest.TestCase):
    """The real log reader drives the pane, from fake ProPresenter log files."""

    def setUp(self) -> None:
        from test_workspace_detection import DEFAULT as DEFAULT_FOLDER
        from test_workspace_detection import EVENING as EVENING_FOLDER
        from test_workspace_detection import Folder, run_log, write_log

        self.folders = (EVENING_FOLDER, DEFAULT_FOLDER)
        self.run_log, self.write_log = run_log, write_log
        self.pp = Folder(self)
        self.world = World(self)
        patch = mock.patch.object(
            main_window,
            "open_workspace",
            lambda pids: main_window_module_open_workspace(pids, clock=self.pp.clock),
        )
        patch.start()
        self.addCleanup(patch.stop)
        self.pane = Pane(self.world)

    def tick(self, *pids: int) -> None:
        self.world.pids = list(pids)
        self.pp.now += 1
        self.world.now += 1
        self.pane._check_workspace()

    def test_the_log_line_shows_and_hides_the_pane(self) -> None:
        evening, default = self.folders
        self.write_log(self.pp.logs / "Log.log", self.run_log(23256, None))
        self.tick(23256)
        self.assertFalse(self.pane.on_screen)  # started, workspace not logged yet
        self.write_log(self.pp.logs / "Log.log", self.run_log(23256, evening))
        self.tick(23256)
        self.assertTrue(self.pane.on_screen)
        self.write_log(self.pp.logs / "Log.1.log", self.run_log(23256, evening))
        self.write_log(self.pp.logs / "Log.log", self.run_log(24044, default))
        self.tick()
        self.tick(24044)
        self.assertFalse(self.pane.on_screen)
        self.assertTrue(self.world.logged("show: ProPresenter P23256 opened workspace Evening Service (from its log)"))
        self.assertTrue(self.world.logged("hide: ProPresenter is not running"))
        self.assertTrue(self.world.logged("no change: ProPresenter P23256 has not logged its workspace yet"))


from media_background_pane.propresenter import open_workspace as main_window_module_open_workspace  # noqa: E402


if __name__ == "__main__":
    unittest.main()
