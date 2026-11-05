"""The iPad remote's MIX and DIM knobs must line up with the PC's.

tests/check_ipad_slider.py runs the iPad page's real script against the real PC
sliders and LAN code, on a simulated clock. It needs a QApplication, and the
other tests here use QCoreApplication, so it runs in its own process.
"""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from media_background_pane import lan_server  # noqa: E402

CHECK = Path(__file__).with_name("check_ipad_slider.py")

MAX_GAP = 0.02  # about 9 px on the iPad, under half the knob
MAX_REST_GAP = 0.002
MAX_RETREAT = 0.003
GHOST_DRIFT = 0.01
RECOVER_MS = 500
NEWS_MS = 200  # the page asks for news ten times a second


class LanHubTests(unittest.TestCase):
    def test_state_says_how_old_it_is(self) -> None:
        hub = lan_server.LanHub()
        self.assertEqual(hub.state(), {})
        clock = [100.0]
        with mock.patch.object(lan_server, "monotonic", lambda: clock[0]):
            published = {"mix": 0.5, "mix_speed": 0.1}
            hub.publish_state(published)
            clock[0] += 0.075
            state = hub.state()
        self.assertEqual(state["mix"], 0.5)
        self.assertEqual(state["mix_speed"], 0.1)
        self.assertAlmostEqual(state["age_ms"], 75.0, places=1)
        self.assertNotIn("age_ms", published)


class IpadSliderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        done = subprocess.run(
            [sys.executable, str(CHECK), "--json"],
            capture_output=True,
            text=True,
            timeout=600,
            cwd=str(ROOT),
        )
        try:
            report = json.loads(done.stdout)
        except json.JSONDecodeError:
            raise AssertionError(f"check_ipad_slider.py did not finish:\n{done.stderr[-3000:]}") from None
        cls.cases = {result["name"]: result for result in report["results"]}

    def moving_cases(self):
        return [(name, result) for name, result in self.cases.items() if name != "at_rest"]

    def test_at_rest_the_knobs_match_from_0_to_1(self) -> None:
        rest = self.cases["at_rest"]
        for key in ("mix", "dim"):
            checks = rest[key]["rest"]
            self.assertEqual(len(checks), 17)
            for check in checks:
                self.assertLess(
                    check["gap"],
                    MAX_REST_GAP,
                    f"{key} at {check['value']}: PC {check['pc']:.4f}, iPad {check['ipad']:.4f}",
                )

    def test_the_knobs_stay_together_while_they_move(self) -> None:
        for name, result in self.moving_cases():
            for key in ("mix", "dim"):
                with self.subTest(case=name, slider=key):
                    self.assertLessEqual(result[key]["max_gap"], MAX_GAP)

    def test_the_knobs_stay_together_near_the_ends(self) -> None:
        for name, result in self.moving_cases():
            for key in ("mix", "dim"):
                with self.subTest(case=name, slider=key):
                    self.assertLessEqual(result[key]["max_gap_ends"], MAX_GAP)

    def test_the_ipad_knob_never_jumps_back(self) -> None:
        for name, result in self.moving_cases():
            for key in ("mix", "dim"):
                with self.subTest(case=name, slider=key):
                    self.assertEqual(result[key]["back_steps"], 0, f"largest step back {result[key]['max_back']:.3f}")

    def test_the_ipad_knob_never_drifts_back(self) -> None:
        for name, result in self.moving_cases():
            for key in ("mix", "dim"):
                with self.subTest(case=name, slider=key):
                    self.assertLessEqual(result[key]["retreat"], MAX_RETREAT)

    def test_the_ipad_knob_starts_soon_after_the_pc_knob(self) -> None:
        for name, result in self.moving_cases():
            for key in ("mix", "dim"):
                for delay in result[key]["start_delays_ms"]:
                    with self.subTest(case=name, slider=key):
                        self.assertIsNotNone(delay)
                        self.assertLessEqual(delay, NEWS_MS)

    def test_a_take_shows_at_once_even_with_a_late_answer(self) -> None:
        take = self.cases["take"]["mix"]
        self.assertLessEqual(take["max_gap"], MAX_GAP)
        self.assertLess(take["rest_gap"], MAX_REST_GAP)

    def test_both_knobs_stop_in_the_same_place(self) -> None:
        for name, result in self.cases.items():
            for key in ("mix", "dim"):
                with self.subTest(case=name, slider=key):
                    self.assertLess(result[key]["rest_gap"], MAX_REST_GAP)

    def test_the_page_sends_commands_only_while_touched(self) -> None:
        for name, result in self.cases.items():
            with self.subTest(case=name):
                self.assertEqual(result["commands_untouched"], 0)
        self.assertGreater(self.cases["ipad_drag"]["commands"], 0)

    def test_the_ghost_stays_where_the_finger_let_go(self) -> None:
        drag = self.cases["ipad_drag"]
        for key in ("mix", "dim"):
            with self.subTest(slider=key):
                self.assertLessEqual(drag[key]["ghost_drift"], GHOST_DRIFT)

    def test_the_knobs_line_up_again_after_a_wifi_drop(self) -> None:
        drop = self.cases["wifi_drop"]["mix"]["recovered_ms"]
        self.assertEqual(len(drop), 1)
        self.assertIsNotNone(drop[0])
        self.assertLessEqual(drop[0], RECOVER_MS)

    def test_no_case_reports_a_problem(self) -> None:
        for name, result in self.cases.items():
            with self.subTest(case=name):
                self.assertEqual(result["problems"], [])


if __name__ == "__main__":
    unittest.main()
