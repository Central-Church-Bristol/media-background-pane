"""Check that the iPad remote's MIX and DIM knobs line up with the PC's.

The iPad page's own script (media_background_pane/web/app.js) runs in Qt's
JavaScript engine, with a small stand-in for the browser. The PC side is the real
slider widgets plus the real LAN code from main_window.py and lan_server.py. Time
is simulated, so a 10-second fade takes a fraction of a second and every run gives
the same numbers.

Each case moves the sliders the way an operator would (FADE, FADE TO BLACK, a
drag on the iPad), and compares where the two knobs are drawn, 60 times a second.

Nothing here talks to the running pane or to the network.

    .venv\\Scripts\\python.exe tests\\check_ipad_slider.py
    .venv\\Scripts\\python.exe tests\\check_ipad_slider.py --page other\\app.js
    .venv\\Scripts\\python.exe tests\\check_ipad_slider.py --trace busy_pc

tests/test_ipad_slider.py runs this with --json and checks the results.
"""

from __future__ import annotations

import argparse
import heapq
import itertools
import json
import os
import random
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

# The PC sliders are widgets, so this needs a QApplication. "offscreen" keeps
# every window off the screen.
os.environ["QT_QPA_PLATFORM"] = "offscreen"

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtQml import QJSEngine, QJSValue  # noqa: E402
from PySide6.QtWidgets import QApplication, QHBoxLayout, QWidget  # noqa: E402

from media_background_pane import lan_server, widgets  # noqa: E402
from media_background_pane.lan_server import LanHub  # noqa: E402
from media_background_pane.main_window import MainWindow  # noqa: E402
from media_background_pane.widgets import LabeledSlider  # noqa: E402

PAGE = ROOT / "media_background_pane" / "web" / "app.js"
STYLE = ROOT / "media_background_pane" / "style.qss"

# What "lined up" means. An iPad slider track is about 450 px long, so 0.02 of
# the track is about 9 px, less than half the 22 px knob.
MAX_GAP = 0.02
# Once both knobs have stopped, they must be in the same place.
MAX_REST_GAP = 0.002
# A step of the iPad knob, within one frame, against the way the PC knob is
# going. 0.004 of the track is under 2 px.
BACK_STEP = 0.004
# How far the iPad knob may drift back from its furthest point while the PC
# knob keeps going the same way.
MAX_RETREAT = 0.003
# "Out of line": further apart than the width of the knob (22 px of about 450).
OUT_OF_LINE = 0.05
# After the finger lets go, the iPad ghost must stay where it was let go.
GHOST_DRIFT = 0.01
# After a Wi-Fi drop, the knobs must line up again within this many ms.
RECOVER_MS = 500
# The page asks the PC for news ten times a second, so it can learn that a
# knob has started moving up to this many ms late. The gap is judged after that.
NEWS_MS = 200

# Where the page's sliders sit on an iPad in landscape, in CSS pixels. Only the
# sizes matter: the page maps a value to a position along the slider.
MIX_RECT = (330.0, 610.0, 420.0, 44.0)
DIM_RECT = (980.0, 30.0, 44.0, 640.0)
KNOB = 22.0  # .knob width and height in app.css

BROWSER = r"""
// Just enough of a browser for app.js: elements, fetch, timers and frames.
// Python sets __clock (milliseconds) and calls the __ functions at the bottom.
var __clock = 0;
var __frames = [];
var __timers = [];
var __pending = {};
var __nextRequest = 1;
var __requests = [];
var __elements = {};
var __rejected = [];

function __Promise() { this._state = 0; this._value = undefined; this._waiting = []; }
__Promise.prototype._settle = function (state, value) {
  if (this._state !== 0) return;
  var self = this;
  if (state === 1 && value && typeof value.then === "function") {
    value.then(function (v) { self._settle(1, v); }, function (e) { self._settle(2, e); });
    return;
  }
  this._state = state;
  this._value = value;
  if (state === 2 && this._waiting.length === 0) __rejected.push(this);
  var waiting = this._waiting;
  this._waiting = [];
  for (var i = 0; i < waiting.length; i++) waiting[i]();
};
__Promise.prototype.then = function (onDone, onFail) {
  var self = this;
  this._handled = true;
  var next = new __Promise();
  var run = function () {
    var handler = self._state === 1 ? onDone : onFail;
    if (typeof handler !== "function") { next._settle(self._state, self._value); return; }
    try {
      next._settle(1, handler(self._value));
    } catch (e) {
      next._settle(2, e);
    }
  };
  if (this._state !== 0) run(); else this._waiting.push(run);
  return next;
};
__Promise.prototype["catch"] = function (onFail) { return this.then(null, onFail); };
function __resolved(value) { var p = new __Promise(); p._settle(1, value); return p; }

function __Element(id) {
  var self = this;
  this.id = id;
  this.style = {};
  this.dataset = {};
  this.textContent = "";
  this.innerHTML = "";
  this.disabled = false;
  this._classes = {};
  this._listeners = {};
  this._rect = { left: 0, top: 0, width: 0, height: 0 };
  this.classList = {
    add: function (name) { self._classes[name] = true; },
    remove: function (name) { delete self._classes[name]; },
    contains: function (name) { return self._classes[name] === true; },
    toggle: function (name, force) {
      var on = force === undefined ? !self._classes[name] : !!force;
      if (on) self._classes[name] = true; else delete self._classes[name];
      return on;
    },
  };
}
__Element.prototype.getBoundingClientRect = function () {
  var r = this._rect;
  return { left: r.left, top: r.top, width: r.width, height: r.height,
           right: r.left + r.width, bottom: r.top + r.height, x: r.left, y: r.top };
};
__Element.prototype.addEventListener = function (type, fn) {
  (this._listeners[type] = this._listeners[type] || []).push(fn);
};
__Element.prototype.removeEventListener = function () {};
__Element.prototype.setPointerCapture = function () {};
__Element.prototype.releasePointerCapture = function () {};
__Element.prototype.querySelectorAll = function () { return []; };
__Element.prototype.replaceChildren = function () {};
__Element.prototype.append = function () {};

var document = {
  getElementById: function (id) {
    if (!__elements[id]) __elements[id] = new __Element(id);
    return __elements[id];
  },
  createElement: function () { return new __Element(""); },
};
var window = { addEventListener: function () {} };
var performance = { now: function () { return __clock; } };
Date.now = function () { return __clock; };
function Image() { this.onload = null; this.onerror = null; this.src = ""; }

function requestAnimationFrame(callback) { __frames.push(callback); return __frames.length; }
function cancelAnimationFrame() {}
function __addTimer(fn, ms, repeat) {
  var wait = Math.max(1, Number(ms) || 0);
  __timers.push({ fn: fn, every: wait, due: __clock + wait, repeat: repeat, live: true });
  return __timers.length;
}
function setInterval(fn, ms) { return __addTimer(fn, ms, true); }
function setTimeout(fn, ms) { return __addTimer(fn, ms, false); }
function clearInterval(id) { if (__timers[id - 1]) __timers[id - 1].live = false; }
var clearTimeout = clearInterval;

function fetch(url, options) {
  var id = __nextRequest++;
  var promise = new __Promise();
  __pending[id] = promise;
  __requests.push({ id: id, url: String(url), method: (options && options.method) || "GET",
                    body: (options && options.body) || "" });
  return promise;
}

function __nextDue() {
  var due = Infinity;
  for (var i = 0; i < __timers.length; i++) {
    if (__timers[i].live && __timers[i].due < due) due = __timers[i].due;
  }
  return due;
}
function __runTimers() {
  for (var guard = 0; guard < 10000; guard++) {
    var next = null;
    for (var i = 0; i < __timers.length; i++) {
      var t = __timers[i];
      if (t.live && t.due <= __clock && (next === null || t.due < next.due)) next = t;
    }
    if (next === null) return;
    if (next.repeat) next.due += next.every; else next.live = false;
    next.fn();
  }
}
function __runFrame(ts) {
  var callbacks = __frames;
  __frames = [];
  for (var i = 0; i < callbacks.length; i++) callbacks[i](ts);
}
function __takeRequests() { var out = JSON.stringify(__requests); __requests = []; return out; }
// Errors the page script threw that nothing in the page handled.
function __takeErrors() {
  var out = [];
  for (var i = 0; i < __rejected.length; i++) {
    var p = __rejected[i];
    if (!p._handled) out.push(String((p._value && p._value.stack) || p._value));
  }
  __rejected = [];
  return JSON.stringify(out);
}
function __answer(id, status, text) {
  var promise = __pending[id];
  delete __pending[id];
  if (!promise) return;
  promise._settle(1, { ok: status >= 200 && status < 300, status: status,
                       json: function () { return __resolved(JSON.parse(text)); } });
}
function __touch(id, type, x, y) {
  var el = document.getElementById(id);
  var listeners = el._listeners[type] || [];
  var event = { type: type, clientX: x, clientY: y, pointerId: 1, preventDefault: function () {} };
  for (var i = 0; i < listeners.length; i++) listeners[i](event);
}
function __setRect(id, left, top, width, height) {
  document.getElementById(id)._rect = { left: left, top: top, width: width, height: height };
}
function __read() {
  function at(id) { return document.getElementById(id).style.transform || ""; }
  function on(id) { return document.getElementById(id).classList.contains("on"); }
  return JSON.stringify({
    mix: at("mixKnob"), mixGhost: at("mixGhost"), mixGhostOn: on("mixGhost"),
    dim: at("dimKnob"), dimGhost: at("dimGhost"), dimGhostOn: on("dimGhost"),
    error: document.getElementById("status").classList.contains("err"),
  });
}
"""

TRANSLATE = re.compile(r"translate3d\(\s*([-0-9.e+]+)px,\s*([-0-9.e+]+)px")


class PageError(RuntimeError):
    pass


class Page:
    """The iPad page's script, running in QJSEngine."""

    def __init__(self, script: Path) -> None:
        self.engine = QJSEngine()
        self._evaluate(BROWSER, "browser stand-in")
        self.root = self.engine.globalObject()
        self.call("__setRect", "mix", *MIX_RECT)
        self.call("__setRect", "dim", *DIM_RECT)
        self._evaluate(script.read_text(encoding="utf-8"), script.name)

    def _evaluate(self, code: str, name: str) -> None:
        result = self.engine.evaluate(code, name)
        if result.isError():
            raise PageError(f"{name}: {result.toString()} (line {result.property('lineNumber').toInt()})")

    def set_clock(self, ms: float) -> None:
        self.root.setProperty("__clock", QJSValue(float(ms)))

    def call(self, name: str, *args) -> QJSValue:
        values = [QJSValue(a) if isinstance(a, str) else QJSValue(float(a)) for a in args]
        result = self.root.property(name).call(values)
        if result.isError():
            raise PageError(f"{name}: {result.toString()} (line {result.property('lineNumber').toInt()})")
        return result

    def take(self, name: str) -> list:
        return json.loads(self.call(name).toString())

    def read(self) -> dict:
        return json.loads(self.call("__read").toString())


def knob_value(transform: str, key: str) -> float | None:
    """Where the page drew a knob, as 0..1 along its slider."""
    match = TRANSLATE.search(transform)
    if not match:
        return None
    x, y = float(match.group(1)), float(match.group(2))
    if key == "mix":
        return x / (MIX_RECT[2] - KNOB)
    return 1.0 - y / (DIM_RECT[3] - KNOB)


def touch_point(key: str, value: float) -> tuple[float, float]:
    """Where a finger touches the page's slider to point at value."""
    left, top, width, height = MIX_RECT if key == "mix" else DIM_RECT
    if key == "mix":
        return left + KNOB / 2 + value * (width - KNOB), top + height / 2
    return left + width / 2, top + KNOB / 2 + (1.0 - value) * (height - KNOB)


def drawn(slider, value: float) -> float:
    """Where the PC slider draws its knob for value, as 0..1 along its groove."""
    center = slider._handle_center(float(value))
    groove = slider._groove_rect()
    if slider.orientation() == Qt.Orientation.Horizontal:
        return (center.x() - groove.x() - 7.0) / max(1.0, groove.width() - 14.0)
    return 1.0 - (center.y() - groove.y() - 7.0) / max(1.0, groove.height() - 14.0)


class FakePane:
    """The parts of MainWindow that its LAN methods use, with the real sliders."""

    _publish_lan_state = MainWindow._publish_lan_state
    _pump_lan = MainWindow._pump_lan
    _apply_lan_command = MainWindow._apply_lan_command

    def __init__(self, fade: float) -> None:
        self.config = SimpleNamespace(fade_seconds=fade)
        self.window = QWidget()
        layout = QHBoxLayout(self.window)
        self.mix_slider = LabeledSlider("MIX", Qt.Orientation.Horizontal)
        self.dim_slider = LabeledSlider("DIM")
        layout.addWidget(self.mix_slider, 1)
        layout.addWidget(self.dim_slider)
        self.window.resize(520, 300)
        self.window.show()
        QApplication.processEvents()  # lay the sliders out; nothing is moving yet
        for slider in (self.mix_slider, self.dim_slider):
            slider.set_fade_seconds(fade)
        self.dim_slider.snap_to(1000)
        self.engine = SimpleNamespace(
            preview=SimpleNamespace(path=None, has_source=True),
            program=SimpleNamespace(path=None),
        )
        self.auto_fade = SimpleNamespace(isChecked=lambda: True)
        self.video_input_button = SimpleNamespace(isChecked=lambda: False)
        self._lan_files: list[dict] = []
        self._lan_hub = LanHub()
        self._lan_server = object()
        self._pending_auto_fade = False
        self._publish_lan_state()  # as _start_lan_remote does, before the page can ask

    def slider(self, key: str):
        return (self.mix_slider if key == "mix" else self.dim_slider).slider

    def close(self) -> None:
        for key in ("mix", "dim"):
            self.slider(key)._timer.stop()
        self.window.close()
        self.window.deleteLater()


@dataclass
class Case:
    name: str
    title: str
    fade: float
    end_ms: int
    events: list = field(default_factory=list)
    # Times the PC's main thread is held up, as (start ms, length ms). The PC
    # knob and the LAN pump both run on that thread.
    stalls: tuple = ()
    # Times the Wi-Fi is slow, as (start ms, length ms).
    slow_wifi: tuple = ()
    # Times the Wi-Fi drops out completely, as (start ms, length ms).
    outages: tuple = ()
    # Times when answers leaving the PC are held up on the way back, so a
    # later answer can overtake them, as (start ms, length ms).
    slow_answers: tuple = ()
    fps: float = 60.0
    seed: int = 7
    # Only check where the knobs settle, not how they move between settings.
    rest_only: bool = False


CASES = [
    Case(
        "fade_10s",
        "FADE up, then back down (10 s fade)",
        10,
        25000,
        [(500, "pc", "mix", 1.0), (12500, "pc", "mix", 0.0)],
    ),
    Case(
        "busy_pc",
        "FADE up and down while the PC is busy for 0.25 s every second (10 s fade)",
        10,
        28000,
        [(500, "pc", "mix", 1.0), (14000, "pc", "mix", 0.0)],
        stalls=tuple((1000 * k + 300, 250) for k in range(28)),
    ),
    Case(
        "change_of_mind",
        "FADE up, sent back down at about 0.6 (10 s fade)",
        10,
        20000,
        [(500, "pc", "mix", 1.0), (6500, "pc", "mix", 0.0)],
    ),
    Case(
        "dim_10s",
        "FADE TO BLACK, then back up (10 s fade)",
        10,
        25000,
        [(500, "pc", "dim", 0.0), (12500, "pc", "dim", 1.0)],
    ),
    Case(
        "fade_3s",
        "MIX and DIM, 3 s fades",
        3,
        16000,
        [
            (300, "pc", "mix", 1.0),
            (4300, "pc", "mix", 0.0),
            (8300, "pc", "dim", 0.0),
            (12300, "pc", "dim", 1.0),
        ],
    ),
    Case(
        "fade_1s",
        "MIX and DIM, 1 s fades",
        1,
        7000,
        [
            (300, "pc", "mix", 1.0),
            (2000, "pc", "mix", 0.0),
            (3700, "pc", "dim", 0.0),
            (5400, "pc", "dim", 1.0),
        ],
    ),
    Case(
        "ipad_drag",
        "MIX dragged to each end on the iPad, DIM down and up (10 s fade)",
        10,
        50000,
        [
            (500, "ipad", "mix", [(0, 0.0), (100, 0.3), (200, 0.6), (300, 0.9), (400, 1.0)]),
            (12500, "ipad", "mix", [(0, 1.0), (100, 0.7), (200, 0.4), (300, 0.1), (400, 0.0)]),
            (25000, "ipad", "dim", [(0, 1.0), (150, 0.5), (300, 0.0)]),
            (37500, "ipad", "dim", [(0, 0.0), (150, 0.5), (300, 1.0)]),
        ],
    ),
    Case(
        "pc_drag",
        "MIX dragged on the PC: slowly up, then quickly down (10 s fade)",
        10,
        20000,
        [
            (500, "drag", "mix", [(i * 50, i / 60.0) for i in range(61)]),
            (8000, "drag", "mix", [(0, 1.0), (100, 0.5), (200, 0.0)]),
        ],
    ),
    Case(
        "slow_wifi",
        "FADE up and down while the Wi-Fi stalls for a moment every 2 s (10 s fade)",
        10,
        25000,
        [(500, "pc", "mix", 1.0), (12500, "pc", "mix", 0.0)],
        slow_wifi=tuple((2000 * k + 700, 300) for k in range(13)),
    ),
    Case(
        "wifi_drop",
        "Wi-Fi drops for 3 s during a FADE, while the PC takes and starts the next fade (10 s fade)",
        10,
        24000,
        [(500, "pc", "mix", 1.0), (7000, "snap", "mix", 0.0), (7100, "pc", "mix", 1.0)],
        outages=((5000, 3000),),
    ),
    Case(
        "take",
        "FADE up, then a take snaps MIX back to 0 while an older answer is still on its way (10 s fade)",
        10,
        13000,
        [(500, "pc", "mix", 1.0), (11000, "snap", "mix", 0.0)],
        slow_answers=((10850, 150),),
    ),
    Case(
        "long_hold",
        "FADE up and down while the PC freezes for 0.6 s now and then (10 s fade)",
        10,
        25000,
        [(500, "pc", "mix", 1.0), (12500, "pc", "mix", 0.0)],
        stalls=((3000, 600), (7000, 600), (15500, 600), (19700, 600)),
    ),
    Case(
        "low_frame_rate",
        "FADE up and down with the iPad drawing 30 frames a second (10 s fade)",
        10,
        25000,
        [(500, "pc", "mix", 1.0), (12500, "pc", "mix", 0.0)],
        fps=30.0,
    ),
]

REST_VALUES = (0.0, 0.001, 0.01, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.99, 0.999, 1.0)
REST_HOLD = 2000  # ms each value is held before the knobs are compared
REST_CASE = Case(
    "at_rest",
    "Both knobs at rest, set to 17 values from 0 to 1",
    10,
    500 + REST_HOLD * len(REST_VALUES) * 2,
    [
        (500 + REST_HOLD * i, "snap", key, value)
        for i, (key, value) in enumerate([("mix", v) for v in REST_VALUES] + [("dim", v) for v in REST_VALUES])
    ],
    rest_only=True,
)


class Run:
    """One case: the page and the PC sliders, on one simulated clock."""

    def __init__(self, script: Path, case: Case) -> None:
        self.case = case
        self.now = 0.0
        self.rng = random.Random(case.seed)
        self.order = itertools.count()
        self.flights: list = []  # (due ms, order, deliver) network traffic in transit
        self.rows: list[dict] = []
        self.commands: list[tuple[float, str]] = []
        self.page_errors: list[str] = []
        self.error_shown = False
        self.error_at_end = False
        self.touches: list[tuple[float, float, str, float]] = []  # (start ms, end ms, key, final value)
        self.starts: list[tuple[float, str]] = []  # (ms, key) when the operator started a move
        self.rest_checks: list[tuple[float, str, float]] = []
        self._patches = [
            mock.patch.object(widgets, "monotonic", self.seconds),
            mock.patch.object(lan_server, "monotonic", self.seconds, create=True),
        ]
        for patch in self._patches:
            patch.start()
        try:
            self.pane = FakePane(case.fade)
            self.page = Page(script)
        except BaseException:
            self._stop_patches()
            raise

    def seconds(self) -> float:
        return self.now / 1000.0

    def close(self) -> None:
        self.pane.close()
        self._stop_patches()

    def _stop_patches(self) -> None:
        for patch in reversed(self._patches):
            patch.stop()

    @staticmethod
    def _inside(at: float, windows) -> bool:
        return any(start <= at < start + length for start, length in windows)

    def busy(self, at: float) -> bool:
        return self._inside(at, self.case.stalls)

    def _busy_until(self, at: float) -> float:
        for start, length in self.case.stalls:
            if start <= at < start + length:
                return start + length
        return at

    def _delay(self) -> float:
        delay = self.rng.uniform(2.0, 12.0)
        if self._inside(self.now, self.case.slow_wifi):
            delay += self.rng.uniform(100.0, 300.0)
        return delay

    # The operator.

    def _events(self) -> list:
        queue = []
        for event in self.case.events:
            ms, kind, key = event[0], event[1], event[2]
            if kind in ("pc", "snap"):
                value = event[3]
                queue.append((float(ms), next(self.order), kind, key, value))
                if kind == "snap" and self.case.rest_only:
                    self.rest_checks.append((float(ms) + REST_HOLD - 10.0, key, value))
            elif kind in ("ipad", "drag"):
                points = event[3]
                last = len(points) - 1
                for i, (offset, value) in enumerate(points):
                    step = "down" if i == 0 else "up" if i == last else "move"
                    queue.append((float(ms + offset), next(self.order), kind + "_" + step, key, value))
                if kind == "ipad":
                    self.touches.append((float(ms), float(ms + points[-1][0]), key, points[-1][1]))
        heapq.heapify(queue)
        return queue

    def _act(self, kind: str, key: str, value: float) -> None:
        slider = self.pane.slider(key)
        if kind in ("pc", "snap", "ipad_down", "drag_down"):
            self.starts.append((self.now, key))
        if kind == "pc":
            slider.set_target(int(round(value * 1000)))
        elif kind == "snap":
            slider.snap_to(int(round(value * 1000)))
        elif kind.startswith("ipad_"):
            x, y = touch_point(key, value)
            step = {"ipad_down": "pointerdown", "ipad_move": "pointermove", "ipad_up": "pointerup"}[kind]
            if step == "pointerup":
                self.page.call("__touch", key, "pointermove", x, y)
            self.page.call("__touch", key, step, x, y)
        elif kind.startswith("drag_"):
            center = slider._handle_center(value * 1000.0)
            kinds = {
                "drag_down": QEvent.Type.MouseButtonPress,
                "drag_move": QEvent.Type.MouseMove,
                "drag_up": QEvent.Type.MouseButtonRelease,
            }
            if kind == "drag_up":
                self._mouse(slider, QEvent.Type.MouseMove, center, Qt.MouseButton.LeftButton)
            buttons = Qt.MouseButton.NoButton if kind == "drag_up" else Qt.MouseButton.LeftButton
            self._mouse(slider, kinds[kind], center, buttons)

    @staticmethod
    def _mouse(slider, kind, center: QPointF, buttons) -> None:
        button = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else Qt.MouseButton.LeftButton
        event = QMouseEvent(kind, center, slider.mapToGlobal(center), button, buttons, Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(slider, event)

    # The network between the iPad and the PC's LAN server.

    def _send(self) -> None:
        for request in self.page.take("__takeRequests"):
            if self._inside(self.now, self.case.outages):
                # No answer comes back; the browser gives up on the request.
                heapq.heappush(
                    self.flights,
                    (self.now + 50.0, next(self.order), lambda r=request: self.page.call("__answer", r["id"], 0, "")),
                )
                continue
            down = self._delay()
            heapq.heappush(
                self.flights,
                (self.now + self._delay(), next(self.order), lambda r=request, d=down: self._serve(r, d)),
            )

    def _serve(self, request: dict, down: float) -> None:
        hub = self.pane._lan_hub
        if self._inside(self.now, self.case.slow_answers):
            down += self.rng.uniform(300.0, 450.0)
        if request["method"] == "POST" and request["url"] == "/api/command":
            payload = json.loads(request["body"] or "{}")
            if isinstance(payload, dict) and payload.get("op"):
                hub.submit(payload)
                self.commands.append((self.now, str(payload.get("op"))))
            status, text = 204, ""
        elif request["url"] == "/api/state":
            status, text = 200, json.dumps(hub.state())
        else:
            status, text = 404, ""
        heapq.heappush(
            self.flights,
            (self.now + down, next(self.order), lambda: self.page.call("__answer", request["id"], status, text)),
        )

    # The PC's main thread.

    def _tick(self) -> None:
        for key in ("mix", "dim"):
            slider = self.pane.slider(key)
            if slider.is_chasing():
                slider._tick()

    def _sample(self) -> None:
        view = self.page.read()
        self.error_shown = self.error_shown or view["error"]
        self.error_at_end = view["error"]
        row = {"t": self.now}
        for key in ("mix", "dim"):
            slider = self.pane.slider(key)
            ghost = knob_value(view[key + "Ghost"], key) if view[key + "GhostOn"] else None
            # Which way the PC knob is going: still the same while the PC is
            # frozen in the middle of a move.
            way = (slider.visual_speed() > 0) - (slider.visual_speed() < 0)
            row[key] = (drawn(slider, slider.visual_value()), knob_value(view[key], key), ghost, way)
        self.rows.append(row)

    def go(self) -> dict:
        case = self.case
        frame = 1000.0 / case.fps
        next_tick, next_pump, next_frame = 8.0, self.rng.uniform(1.0, 50.0), frame
        late_tick = late_pump = False
        events = self._events()
        stall_ends = sorted(start + length for start, length in case.stalls)
        self._send()
        while True:
            due = [case.end_ms, next_tick, next_pump, next_frame, self.page.call("__nextDue").toNumber()]
            if events:
                due.append(events[0][0])
            if self.flights:
                due.append(self.flights[0][0])
            due.extend(end for end in stall_ends if end > self.now)
            self.now = max(self.now, min(due))
            self.page.set_clock(self.now)
            if self.now >= case.end_ms:
                break
            busy = self.busy(self.now)
            while events and events[0][0] <= self.now:
                ms, order, kind, key, value = heapq.heappop(events)
                if busy and (kind in ("pc", "snap") or kind.startswith("drag_")):
                    heapq.heappush(events, (self._busy_until(self.now), order, kind, key, value))
                    continue
                self._act(kind, key, value)
                self._send()
            while self.flights and self.flights[0][0] <= self.now:
                _, _, deliver = heapq.heappop(self.flights)
                deliver()
                self._send()
            if self.now >= next_tick:
                while next_tick <= self.now:
                    next_tick += 8.0 + self.rng.uniform(-1.0, 1.0)
                late_tick = True
            if self.now >= next_pump:
                while next_pump <= self.now:
                    next_pump += 50.0 + self.rng.uniform(-2.5, 2.5)
                late_pump = True
            if not busy:
                if late_tick:
                    late_tick = False
                    self._tick()
                if late_pump:
                    late_pump = False
                    self.pane._pump_lan()
            if self.page.call("__nextDue").toNumber() <= self.now:
                self.page.call("__runTimers")
                self._send()
            if self.now >= next_frame:
                while next_frame <= self.now:
                    next_frame += frame
                self.page.call("__runFrame", self.now)
                self._send()
                self._sample()
        self.page_errors = self.page.take("__takeErrors")
        return self.measure()

    # The numbers.

    def _ignored(self, at: float) -> bool:
        # While the Wi-Fi is down the iPad cannot know where the PC knob is.
        return any(start <= at < start + length + RECOVER_MS for start, length in self.case.outages)

    def _news(self, at: float, key: str) -> bool:
        # Just after a move starts, the page may not have heard about it yet.
        return any(start <= at < start + NEWS_MS for start, start_key in self.starts if start_key == key)

    def _start_delays(self, rows, key: str) -> list[float]:
        """For each move started, how much later the iPad knob started moving than the PC knob."""
        delays = []
        for start, start_key in self.starts:
            if start_key != key or self._ignored(start):
                continue
            after = [row for row in rows if row[0] >= start]
            if not after:
                continue
            pc_from, ipad_from = after[0][1], after[0][2]
            pc_at = next((t for t, pc, _i, _g in after if abs(pc - pc_from) > 0.002), None)
            ipad_at = next((t for t, _p, ipad, _g in after if abs(ipad - ipad_from) > 0.002), None)
            if pc_at is not None:
                delays.append(None if ipad_at is None else max(0.0, ipad_at - pc_at))
        return delays

    def measure(self) -> dict:
        result = {
            "name": self.case.name,
            "title": self.case.title,
            "commands": len(self.commands),
            "commands_untouched": sum(1 for t, _op in self.commands if not self._touching(t)),
            "page_errors": self.page_errors,
            "error_shown": self.error_shown,
            "error_at_end": self.error_at_end,
        }
        for key in ("mix", "dim"):
            every = [(r["t"], r[key][0], r[key][1], r[key][2]) for r in self.rows if r[key][1] is not None]
            ways = {r["t"]: r[key][3] for r in self.rows}
            rows = [row for row in every if not self._ignored(row[0])]
            judged = [row for row in rows if not self._news(row[0], key)]
            gaps = [abs(ipad - pc) for _t, pc, ipad, _g in judged]
            end_gaps = [abs(ipad - pc) for _t, pc, ipad, _g in judged if pc < 0.2 or pc > 0.8]
            back = []
            out_of_line = 0.0
            retreat, way, furthest = 0.0, 0, 0.0
            stutter = 0.0
            frame = 1000.0 / self.case.fps
            for (t0, pc0, ipad0, _g0), (t1, pc1, ipad1, _g1) in zip(rows, rows[1:]):
                if t1 - t0 > 2.5 * frame:
                    way = 0  # not neighbouring frames: a stretch in between is not judged
                    continue
                going = pc1 - pc0
                step_way = ways[t1]
                if step_way and (ipad1 - ipad0) * step_way < -BACK_STEP:
                    back.append(abs(ipad1 - ipad0))
                if abs(ipad0 - pc0) > OUT_OF_LINE and not self._news(t0, key):
                    out_of_line += t1 - t0
                if abs(going) > 1e-6:
                    stutter = max(stutter, abs((ipad1 - ipad0) - going))
                if not step_way:
                    way = 0
                elif step_way != way:
                    way, furthest = step_way, ipad1
                else:
                    furthest = max(furthest * way, ipad1 * way) * way
                    retreat = max(retreat, (furthest - ipad1) * way)
            drift = 0.0
            for _start, end, touch_key, value in self.touches:
                if touch_key == key:
                    for t, _pc, _ipad, ghost in every:
                        if end < t <= end + 3000.0 and ghost is not None:
                            drift = max(drift, abs(ghost - value))
            recovered = []
            for start, length in self.case.outages:
                back_at = start + length
                after = [(t, abs(ipad - pc)) for t, pc, ipad, _g in every if t >= back_at]
                settled = next(
                    (t for t, _gap in after if all(g <= MAX_GAP for u, g in after if t <= u <= t + 1000.0)),
                    None,
                )
                recovered.append(None if settled is None else settled - back_at)
            rest = []
            for ms, check_key, value in self.rest_checks:
                if check_key == key:
                    row = min(every, key=lambda r: abs(r[0] - ms))
                    rest.append({"value": value, "pc": row[1], "ipad": row[2], "gap": abs(row[2] - row[1])})
            result[key] = {
                "max_gap": max(gaps, default=0.0),
                "max_gap_ends": max(end_gaps, default=0.0),
                "out_of_line_ms": out_of_line,
                "back_steps": len(back),
                "max_back": max(back, default=0.0),
                "retreat": retreat,
                "stutter": stutter,
                "ghost_drift": drift,
                "recovered_ms": recovered,
                "start_delays_ms": self._start_delays(rows, key),
                "rest_gap": abs(every[-1][2] - every[-1][1]) if every else 1.0,
                "rest": rest,
            }
        result["problems"] = problems(result, self.case)
        return result

    def _touching(self, at: float) -> bool:
        # A command can reach the PC up to a few hundred ms after it was sent.
        return any(start <= at <= end + 400.0 for start, end, _key, _value in self.touches)

    def trace(self, key: str, every_ms: float = 100.0) -> list[tuple[float, float, float]]:
        out, next_at = [], 0.0
        for row in self.rows:
            if row["t"] >= next_at and row[key][1] is not None:
                out.append((row["t"], row[key][0], row[key][1]))
                next_at = row["t"] + every_ms
        return out


def problems(result: dict, case: Case) -> list[str]:
    found = []
    for key in ("mix", "dim"):
        m = result[key]
        name = key.upper()
        if not case.rest_only:
            if m["max_gap"] > MAX_GAP:
                found.append(f"{name} knobs were up to {m['max_gap']:.3f} apart ({m['max_gap_ends']:.3f} near the ends)")
            if m["back_steps"]:
                found.append(f"iPad {name} knob jumped back {m['back_steps']} times (up to {m['max_back']:.3f})")
            elif m["retreat"] > MAX_RETREAT:
                found.append(f"iPad {name} knob drifted back {m['retreat']:.3f} while the PC knob kept going")
            if m["ghost_drift"] > GHOST_DRIFT:
                found.append(f"iPad {name} ghost moved {m['ghost_drift']:.3f} away from where the finger let go")
            for took in m["recovered_ms"]:
                if took is None or took > RECOVER_MS:
                    found.append(f"{name} knobs had not lined up {RECOVER_MS / 1000:.1f} s after the Wi-Fi came back")
            late = [d for d in m["start_delays_ms"] if d is None or d > NEWS_MS]
            if late:
                found.append(f"iPad {name} knob started moving more than {NEWS_MS / 1000:.1f} s after the PC knob")
        if m["rest_gap"] > MAX_REST_GAP:
            found.append(f"{name} knobs stopped {m['rest_gap']:.3f} apart")
        for check in m["rest"]:
            if check["gap"] > MAX_REST_GAP:
                found.append(f"{name} at {check['value']}: PC {check['pc']:.4f}, iPad {check['ipad']:.4f}")
    if result["commands_untouched"]:
        found.append(f"the page sent {result['commands_untouched']} commands while nobody touched it")
    if result["page_errors"]:
        found.append(f"the page script failed: {result['page_errors'][0]}")
    if result["error_shown"] and not case.outages:
        found.append("the page showed its 'Can't reach the pane' message")
    if result["error_at_end"]:
        found.append("the page still showed its 'Can't reach the pane' message at the end")
    return found


def run_case(script: Path, case: Case) -> tuple[dict, Run]:
    run = Run(script, case)
    try:
        return run.go(), run
    finally:
        run.close()


def summary(result: dict, case: Case) -> str:
    mix, dim = result["mix"], result["dim"]
    if case.rest_only:
        checks = mix["rest"] + dim["rest"]
        worst = max((c["gap"] for c in checks), default=0.0)
        return f"{len(checks)} settings compared; largest difference {worst:.4f}; no commands sent"
    worst = max(mix["max_gap"], dim["max_gap"])
    ends = max(mix["max_gap_ends"], dim["max_gap_ends"])
    apart = max(mix["out_of_line_ms"], dim["out_of_line_ms"])
    back = mix["back_steps"] + dim["back_steps"]
    parts = [f"largest gap {worst:.3f} ({ends:.3f} near the ends)"]
    delays = [d for m in (mix, dim) for d in m["start_delays_ms"] if d is not None]
    if delays:
        parts.append(f"iPad knob starts up to {max(delays) / 1000:.2f} s after the PC knob")
    if apart >= 20:
        parts.append(f"over a knob's width apart for {apart / 1000:.1f} s")
    parts.append(f"{back} jumps back" if back else "no jumps back")
    took = [t for m in (mix, dim) for t in m["recovered_ms"]]
    if took:
        if None in took:
            parts.append("never lined up again after the Wi-Fi came back")
        else:
            parts.append(f"lined up again {max(took) / 1000:.2f} s after the Wi-Fi came back")
    if result["commands_untouched"]:
        parts.append(f"{result['commands_untouched']} commands sent untouched")
    elif result["commands"]:
        parts.append(f"{result['commands']} commands sent, all while touched")
    else:
        parts.append("no commands sent")
    return "; ".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--page", type=Path, default=PAGE, help="the page script to check (default: the app's)")
    parser.add_argument("--json", action="store_true", help="print the measurements as JSON")
    parser.add_argument("--trace", metavar="CASE", help="also print both knobs every 0.1 s for one case")
    parser.add_argument("--only", metavar="CASE", action="append", help="run only these cases")
    args = parser.parse_args(argv)

    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(STYLE.read_text(encoding="utf-8"))
    cases = [REST_CASE] + CASES
    if args.only:
        cases = [c for c in cases if c.name in args.only]
    results = []
    traces = {}
    for case in cases:
        result, run = run_case(args.page, case)
        results.append((case, result))
        if args.trace == case.name:
            traces[case.name] = {key: run.trace(key) for key in ("mix", "dim")}
    failed = [result for _case, result in results if result["problems"]]

    if args.json:
        print(json.dumps({"page": str(args.page), "results": [r for _c, r in results], "traces": traces}))
        return 1 if failed else 0

    print(f"iPad slider check, page script: {args.page}")
    print("Simulated time, real PC slider and LAN code, real page script. Nothing touches the running pane.")
    print()
    for case, result in results:
        mark = "FAIL" if result["problems"] else "PASS"
        print(f"  {mark}  {result['title']}")
        print(f"        {summary(result, case)}")
        for problem in result["problems"][:4]:
            print(f"        - {problem}")
    for name, by_key in traces.items():
        for key, rows in by_key.items():
            print()
            print(f"{name}, {key.upper()}: time, PC knob, iPad knob")
            for t, pc, ipad in rows:
                mark = "<" if ipad < pc - 0.005 else ">" if ipad > pc + 0.005 else " "
                print(f"  {t / 1000:6.2f} s  {pc:6.3f}  {ipad:6.3f}  {mark}")
    print()
    if failed:
        print(f"FAILED: {len(failed)} of {len(results)}")
        return 1
    print(f"ALL PASSED: {len(results)} of {len(results)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
