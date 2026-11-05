"""Check the iPad remote's knobs in a real browser, in real time.

The app's iPad page is loaded over HTTP into Qt's built-in browser (Chromium)
and drawn off screen. The PC side is the real slider widgets on their real
timers, MainWindow's real LAN code, and the real LAN request handler. The
check moves the PC sliders the way FADE, FADE TO BLACK and a drag on the iPad
do, and compares where each page draws its knobs, frame by frame.

It serves on a free port on 127.0.0.1 only, never the pane's 8745, so it does
not touch the running pane, its sliders or the network. It takes about 40 s.

    .venv\\Scripts\\python.exe tests\\check_ipad_slider_browser.py
    .venv\\Scripts\\python.exe tests\\check_ipad_slider_browser.py --web other\\web

tests/check_ipad_slider.py checks the same things on a simulated clock.
"""

from __future__ import annotations

import argparse
import bisect
import json
import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
# Draw in software and keep the browser engine's own log quiet; its graphics
# messages have nothing to do with this check.
os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = "--disable-gpu --log-level=3"

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

from PySide6.QtCore import QCoreApplication, Qt, QTimer, QUrl  # noqa: E402

QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)

from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile  # noqa: E402
from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import check_ipad_slider as sim  # noqa: E402
from media_background_pane import lan_server  # noqa: E402

MAX_GAP = sim.MAX_GAP
MAX_REST_GAP = sim.MAX_REST_GAP
BACK_STEP = sim.BACK_STEP
NEWS_MS = 250  # a real browser and real timers: allow one more frame or two

# Finds the knobs as the browser lays them out, and records them each frame.
# The page's own script draws them; this only measures.
SAMPLER = r"""
(function () {
  if (window.__samples) return "already";
  window.__samples = [];
  function along(slider, knob, vertical) {
    var s = slider.getBoundingClientRect();
    var k = knob.getBoundingClientRect();
    if (vertical) {
      return 1 - (k.top - s.top) / Math.max(1, s.height - k.height);
    }
    return (k.left - s.left) / Math.max(1, s.width - k.width);
  }
  var mix = document.getElementById("mix"), mixKnob = document.getElementById("mixKnob");
  var dim = document.getElementById("dim"), dimKnob = document.getElementById("dimKnob");
  // ts is the frame's time, the same one the page drew this frame's knobs for.
  function sample(ts) {
    window.__samples.push([performance.timeOrigin + ts,
                           along(mix, mixKnob, false), along(dim, dimKnob, true)]);
    requestAnimationFrame(sample);
  }
  requestAnimationFrame(sample);
  return "started";
})();
"""

# A finger on the iPad page: pointer events where a touch would land.
TOUCH = r"""
(function (id, type, value) {
  var el = document.getElementById(id);
  var r = el.getBoundingClientRect();
  var x = id === "mix" ? r.left + 11 + value * (r.width - 22) : r.left + r.width / 2;
  var y = id === "mix" ? r.top + r.height / 2 : r.top + 11 + (1 - value) * (r.height - 22);
  el.dispatchEvent(new PointerEvent(type, { clientX: x, clientY: y, pointerId: 7, bubbles: true }));
  return [x, y];
})(%r, %r, %r);
"""


class Page(QWebEnginePage):
    def __init__(self, profile, parent) -> None:
        super().__init__(profile, parent)
        self.errors: list[str] = []

    def javaScriptConsoleMessage(self, level, message, line, source) -> None:
        if level == QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
            self.errors.append(f"{source}:{line}: {message}")


class Check:
    def __init__(self) -> None:
        self.app = QApplication.instance() or QApplication([])
        self.app.setStyleSheet(sim.STYLE.read_text(encoding="utf-8"))
        self.pane = sim.FakePane(3)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), lan_server._Handler)
        self.httpd.hub = self.pane._lan_hub
        self.port = self.httpd.server_address[1]
        if self.port == lan_server.DEFAULT_PORT:
            raise SystemExit("got the pane's own port; try again")
        threading.Thread(target=self.httpd.serve_forever, name="check-lan", daemon=True).start()
        self.commands: list[tuple[float, str]] = []
        real_apply = self.pane._apply_lan_command

        def apply(cmd):
            self.commands.append((time.time() * 1000.0, str(cmd.get("op"))))
            real_apply(cmd)

        self.pane._apply_lan_command = apply
        self.pump = QTimer()
        self.pump.setInterval(50)
        self.pump.timeout.connect(self.pane._pump_lan)
        self.pump.start()
        self.pc: list[list[float]] = []
        self.recorder = QTimer()
        self.recorder.setTimerType(Qt.TimerType.PreciseTimer)
        self.recorder.setInterval(4)
        self.recorder.timeout.connect(self._record)
        self.recorder.start()
        self.profile = QWebEngineProfile()  # off the record: nothing saved to disk
        self.view = QWebEngineView()
        self.page = Page(self.profile, self.view)
        self.view.setPage(self.page)
        self.view.resize(1080, 810)  # an iPad in landscape, in CSS pixels
        self.starts: list[tuple[float, str]] = []
        self.touches: list[tuple[float, float]] = []
        self.samples: list | None = None

    def _record(self) -> None:
        row = [time.time() * 1000.0]
        for key in ("mix", "dim"):
            slider = self.pane.slider(key)
            row.append(sim.drawn(slider, slider.visual_value()))
            row.append(slider.visual_speed())
        self.pc.append(row)

    def js(self, code: str, then=None) -> None:
        self.page.runJavaScript(code, 0, then or (lambda _result: None))

    def move(self, key: str, value: int) -> None:
        self.starts.append((time.time() * 1000.0, key))
        self.pane.slider(key).set_target(value)

    def fade_time(self, seconds: int) -> None:
        self.pane.config.fade_seconds = seconds
        for key in ("mix", "dim"):
            self.pane.slider(key).set_fade_seconds(seconds)

    def touch(self, key: str, points: list[tuple[int, float]]) -> None:
        start = time.time() * 1000.0
        self.starts.append((start, key))
        self.touches.append((start, start + points[-1][0]))
        last = len(points) - 1
        for i, (offset, value) in enumerate(points):
            kind = "pointerdown" if i == 0 else "pointerup" if i == last else "pointermove"
            if kind == "pointerup":
                QTimer.singleShot(offset, lambda v=value: self.js(TOUCH % (key, "pointermove", v)))
            QTimer.singleShot(offset, lambda k=kind, v=value: self.js(TOUCH % (key, k, v)))

    def run(self) -> int:
        plan = [
            (2.0, lambda: self.move("mix", 1000)),
            (6.0, lambda: self.move("mix", 0)),
            (10.0, lambda: self.move("dim", 0)),
            (14.0, lambda: self.move("dim", 1000)),
            (18.0, lambda: self.move("mix", 1000)),
            (19.5, lambda: self.move("mix", 0)),  # changed mind half way
            (23.5, lambda: self.fade_time(1)),
            (24.0, lambda: self.move("mix", 1000)),
            (26.0, lambda: self.move("mix", 0)),
            (28.0, lambda: self.fade_time(3)),
            (28.5, lambda: self.touch("mix", [(0, 0.0), (100, 0.35), (200, 0.7), (300, 1.0)])),
            (33.5, lambda: self.touch("mix", [(0, 1.0), (100, 0.65), (200, 0.3), (300, 0.0)])),
        ]
        self.page.loadFinished.connect(self._loaded)
        self.page.load(QUrl(f"http://127.0.0.1:{self.port}/"))
        self.view.show()
        self._plan = plan
        QTimer.singleShot(60000, self.app.quit)  # give up if the page never loads
        self.app.exec()
        self.httpd.shutdown()
        self.httpd.server_close()
        return self.report()

    def _loaded(self, ok: bool) -> None:
        if not ok:
            self.page.errors.append("the page did not load")
            self.app.quit()
            return
        # Prevent the page from capturing a pointer this check made up.
        self.js("Element.prototype.setPointerCapture = function () {};")
        self.js(SAMPLER)
        for at, step in self._plan:
            QTimer.singleShot(int(at * 1000), step)
        QTimer.singleShot(39000, self._collect)

    def _collect(self) -> None:
        def got(text):
            self.samples = json.loads(text) if text else []
            self.app.quit()

        self.js("JSON.stringify(window.__samples)", got)

    # The numbers.

    def _pc_at(self, at: float, column: int) -> float:
        times = [row[0] for row in self.pc]
        i = bisect.bisect_left(times, at)
        if i <= 0:
            return self.pc[0][column]
        if i >= len(self.pc):
            return self.pc[-1][column]
        before, after = self.pc[i - 1], self.pc[i]
        share = (at - before[0]) / max(1e-9, after[0] - before[0])
        return before[column] + share * (after[column] - before[column])

    def _news(self, at: float, key: str) -> bool:
        return any(start <= at < start + NEWS_MS for start, start_key in self.starts if start_key == key)

    def report(self) -> int:
        print(f"iPad slider check in a real browser (Qt WebEngine), port {self.port}")
        print("Real page over HTTP, real PC sliders and LAN code, real time. Nothing touches the running pane.")
        print()
        problems: list[str] = []
        if not self.samples:
            problems.append("the page drew no frames")
        else:
            first, last = self.samples[0][0], self.samples[-1][0]
            print(f"  {len(self.samples)} frames in {(last - first) / 1000:.1f} s; {len(self.pc)} PC samples")
            for key, column, pc_column, speed_column in (("mix", 1, 1, 2), ("dim", 2, 3, 4)):
                gaps, back, previous = [], 0, None
                for row in self.samples:
                    at, ipad = row[0], row[column]
                    pc = self._pc_at(at, pc_column)
                    if not self._news(at, key) and self.pc[0][0] <= at <= self.pc[-1][0]:
                        gaps.append((abs(ipad - pc), pc, at))
                    way = self._pc_at(at, speed_column)
                    if previous is not None and way and (ipad - previous) * (1 if way > 0 else -1) < -BACK_STEP:
                        back += 1
                    previous = ipad
                worst = max(gaps, default=(0.0, 0.0, 0.0))
                ends = max((g for g in gaps if g[1] < 0.2 or g[1] > 0.8), default=(0.0, 0.0, 0.0))
                rest = abs(self.samples[-1][column] - self._pc_at(self.samples[-1][0], pc_column))
                name = key.upper()
                print(
                    f"  {name}: largest gap {worst[0]:.3f} at {(worst[2] - first) / 1000:.1f} s "
                    f"({ends[0]:.3f} near the ends), {back} jumps back, {rest:.4f} apart at the end"
                )
                if worst[0] > MAX_GAP:
                    problems.append(f"{name} knobs were {worst[0]:.3f} apart at {(worst[2] - first) / 1000:.1f} s")
                if back:
                    problems.append(f"iPad {name} knob jumped back {back} times")
                if rest > MAX_REST_GAP:
                    problems.append(f"{name} knobs stopped {rest:.4f} apart")
        untouched = [op for at, op in self.commands if not any(s <= at <= e + 500 for s, e in self.touches)]
        print(f"  commands the page sent: {len(self.commands)}, all while touched: {'yes' if not untouched else 'no'}")
        if untouched:
            problems.append(f"the page sent {len(untouched)} commands while nobody touched it")
        if not self.commands:
            problems.append("the page sent no commands for the drags")
        if self.page.errors:
            problems.append("page script errors: " + "; ".join(self.page.errors[:3]))
        print()
        for problem in problems:
            print(f"  - {problem}")
        print("FAILED" if problems else "PASSED: the iPad knobs lined up with the PC knobs")
        return 1 if problems else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--web", type=Path, help="serve this web folder instead of the app's")
    args = parser.parse_args()
    if args.web:
        lan_server.WEB_ROOT = args.web.resolve()
    sys.exit(Check().run())
