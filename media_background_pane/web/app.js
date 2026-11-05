const thumbsEl = document.getElementById("thumbs");
const statusEl = document.getElementById("status");
const previewImg = document.getElementById("previewImg");
const programImg = document.getElementById("programImg");
const fadeBtn = document.getElementById("fade");
const blackBtn = document.getElementById("black");
const durationBtn = document.getElementById("duration");
const autoFadeBtn = document.getElementById("autoFade");
const videoBtn = document.getElementById("videoInput");
const mixEl = document.getElementById("mix");
const dimEl = document.getElementById("dim");

// The MIX and DIM knobs are drawn where the PC says its knobs are. The PC
// reports about ten times a second. Between reports, a moving knob carries on
// the way the PC knob moves. When a report shows the knob is a little off, the
// difference fades out instead of making the knob jump.
const CARRY_LIMIT = 0.5; // seconds a report is carried forward, at most
const STALE_AGE = 80; // ms: a report this old when sent means the PC was busy and its knob had stopped
const PUMP_GAP = 0.05; // seconds the PC knob can move between two of its reports
const SETTLE_TIME = 0.08; // seconds for a small difference to fade out
const CATCH_UP_TIME = 0.03; // the same, when the knob is behind and only has to hurry on
const SNAP_DISTANCE = 0.15; // a jump this big while the PC knob is still is a snap (after a take)
const WAIT_LIMIT = 0.3; // seconds of travel a knob may be ahead and wait, instead of stepping back
const GHOST_HOLD = 600; // ms the ghost stays where the finger left it, waiting for the PC

let dragging = null;
let localGhost = { mix: null, dim: null };
let releasedAt = { mix: 0, dim: 0 };
let lastPost = { mix: 0, dim: 0 };
let lastFilesKey = "";
let connected = false;
let fadeSeconds = 10;
let reported = false;
let asked = 0;
let answered = 0;
// The PC's last report for each knob, and when it was true, on this page's clock.
let remote = {
  mix: { value: 0, speed: 0, target: 0, at: 0, limit: 0 },
  dim: { value: 1, speed: 0, target: 1, at: 0, limit: 0 },
};
let target = { mix: 0, dim: 1 };
let display = { mix: 0, dim: 1 };
let settle = { mix: 0, dim: 0 };
let lastFrame = 0;

function post(op, extra) {
  const body = Object.assign({ op }, extra || {});
  return fetch("/api/command", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).catch(() => {
    connected = false;
  });
}

function clamp01(value) {
  return Math.max(0, Math.min(1, value));
}

function valueFromEvent(el, event, vertical) {
  const rect = el.getBoundingClientRect();
  const cx = event.clientX;
  const cy = event.clientY;
  if (vertical) {
    const t = (cy - rect.top - 11) / Math.max(1, rect.height - 22);
    return clamp01(1 - t);
  }
  const t = (cx - rect.left - 11) / Math.max(1, rect.width - 22);
  return clamp01(t);
}

function placeHorizontal(el, fill, ghost, knob, value, dest, showGhost) {
  const rect = el.getBoundingClientRect();
  const span = Math.max(1, rect.width - 22);
  const x = value * span;
  const gx = dest * span;
  const y = rect.height / 2 - 11;
  knob.style.transform = `translate3d(${x}px, ${y}px, 0)`;
  ghost.style.transform = `translate3d(${gx}px, ${y}px, 0)`;
  fill.style.transform = `scaleX(${Math.max(0.001, value)})`;
  ghost.classList.toggle("on", showGhost && Math.abs(dest - value) > 0.01);
}

function placeVertical(el, fill, ghost, knob, value, dest, showGhost) {
  const rect = el.getBoundingClientRect();
  const span = Math.max(1, rect.height - 22);
  const y = (1 - value) * span;
  const gy = (1 - dest) * span;
  const x = rect.width / 2 - 11;
  knob.style.transform = `translate3d(${x}px, ${y}px, 0)`;
  ghost.style.transform = `translate3d(${x}px, ${gy}px, 0)`;
  fill.style.transform = `scaleY(${Math.max(0.001, value)})`;
  ghost.classList.toggle("on", showGhost && Math.abs(dest - value) > 0.01);
}

function postTarget(key, value, force) {
  const now = Date.now();
  if (!force && now - lastPost[key] < 50) return;
  lastPost[key] = now;
  post(key === "mix" ? "mix_target" : "dim_target", { value });
}

function paintSlidersNow() {
  placeHorizontal(
    mixEl,
    document.getElementById("mixFill"),
    document.getElementById("mixGhost"),
    document.getElementById("mixKnob"),
    display.mix,
    target.mix,
    dragging === "mix" || Math.abs(target.mix - display.mix) > 0.01
  );
  placeVertical(
    dimEl,
    document.getElementById("dimFill"),
    document.getElementById("dimGhost"),
    document.getElementById("dimKnob"),
    display.dim,
    target.dim,
    dragging === "dim" || Math.abs(target.dim - display.dim) > 0.01
  );
}

function setLocalTarget(key, value) {
  localGhost[key] = value;
  target[key] = value;
  paintSlidersNow();
}

function bindSlider(el, key, vertical) {
  const start = (event) => {
    event.preventDefault();
    dragging = key;
    const value = valueFromEvent(el, event, vertical);
    setLocalTarget(key, value);
    postTarget(key, value, true);
  };
  const move = (event) => {
    if (dragging !== key) return;
    event.preventDefault();
    const value = valueFromEvent(el, event, vertical);
    setLocalTarget(key, value);
    postTarget(key, value, false);
  };
  const end = () => {
    if (dragging !== key) return;
    if (localGhost[key] != null) postTarget(key, localGhost[key], true);
    releasedAt[key] = performance.now();
    dragging = null;
  };
  el.addEventListener("pointerdown", (event) => {
    el.setPointerCapture(event.pointerId);
    start(event);
  });
  el.addEventListener("pointermove", move);
  el.addEventListener("pointerup", end);
  el.addEventListener("pointercancel", end);
}

bindSlider(mixEl, "mix", false);
bindSlider(dimEl, "dim", true);

fadeBtn.addEventListener("click", () => post("fade"));
blackBtn.addEventListener("click", () => post("black"));
durationBtn.addEventListener("click", () => post("duration"));
autoFadeBtn.addEventListener("click", () => {
  post("auto_fade", { value: !autoFadeBtn.classList.contains("on") });
});
videoBtn.addEventListener("click", () => {
  post("video_input", { value: !videoBtn.classList.contains("on") });
});

function renderThumbs(files, preview, program) {
  const key = files.map((f) => f.path).join("|");
  if (key === lastFilesKey) {
    thumbsEl.querySelectorAll(".thumb").forEach((btn) => {
      const path = btn.dataset.path;
      btn.classList.toggle("preview", path === preview);
      btn.classList.toggle("program", path === program);
    });
    return;
  }
  lastFilesKey = key;
  thumbsEl.replaceChildren();
  files.forEach((file) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "thumb";
    btn.dataset.path = file.path;
    if (file.path === preview) btn.classList.add("preview");
    if (file.path === program) btn.classList.add("program");
    const img = document.createElement("img");
    img.alt = "";
    img.src = file.thumb || "";
    const name = document.createElement("span");
    name.textContent = file.name;
    btn.append(img, name);
    btn.addEventListener("click", () => post("preview", { path: file.path }));
    thumbsEl.append(btn);
  });
}

// How the PC knob moves (ChaseSlider._tick in widgets.py): it speeds up over
// the first tenth of the fade time, keeps a steady speed, then slows down over
// the last tenth and stops on its target. Carries a report forward by seconds.
function carry(r, seconds) {
  const fade = Math.max(0.05, fadeSeconds);
  const ease = Math.max(0.06, 0.1 * fade);
  const top = 1 / Math.max(fade - ease, 0.05);
  const accel = top / ease;
  let pos = r.value;
  let vel = r.speed;
  for (let left = seconds; left > 1e-6; left -= 0.008) {
    const dt = Math.min(0.008, left);
    const remaining = r.target - pos;
    const dist = Math.abs(remaining);
    if (dist < 0.0004 && Math.abs(vel) < top * 0.02) return r.target;
    const dir = remaining > 0 ? 1 : -1;
    if (vel * remaining < 0 || dist <= (vel * vel) / (2 * accel)) {
      const dv = accel * dt;
      vel = Math.abs(vel) <= dv ? 0 : vel - Math.sign(vel) * dv;
    } else {
      vel = Math.max(-top, Math.min(top, vel + dir * accel * dt));
    }
    pos += vel * dt;
    if ((dir > 0 && pos >= r.target) || (dir < 0 && pos <= r.target)) return r.target;
  }
  return clamp01(pos);
}

// Where the PC knob should be at time now (ms, this page's clock).
function predicted(key, now) {
  const r = remote[key];
  if (!r.speed && Math.abs(r.target - r.value) < 0.0005) return r.value;
  return carry(r, Math.min(r.limit, Math.max(0, (now - r.at) / 1000)));
}

function takeReport(key, state, at, now) {
  const value = Number(state[key]);
  if (!Number.isFinite(value)) return;
  const dest = Number(state[`${key}_target`]);
  const r = remote[key];
  // Where the knob would be drawn now from the last report.
  const before = clamp01(predicted(key, now) + settle[key]);
  r.value = clamp01(value);
  r.speed = Number(state[`${key}_speed`]) || 0;
  r.target = Number.isFinite(dest) ? clamp01(dest) : r.value;
  r.at = at;
  r.limit = Number(state.age_ms) > STALE_AGE ? PUMP_GAP : CARRY_LIMIT;
  const guess = predicted(key, now);
  if (!reported || (!r.speed && Math.abs(guess - before) > SNAP_DISTANCE)) {
    // The first report, or the PC knob jumped (a take): follow at once.
    settle[key] = 0;
    display[key] = guess;
  } else {
    // Carry on from where the knob would have been; the difference fades out.
    settle[key] = before - guess;
  }
}

// The ghost shows where the knob is heading: the finger while dragging, then
// the PC's own ghost once the PC has caught up with where the finger let go.
function ghostTarget(key, now) {
  const mine = localGhost[key];
  if (mine != null) {
    const waiting = Math.abs(remote[key].target - mine) > 0.002;
    if (waiting && now - releasedAt[key] < GHOST_HOLD) return mine;
    localGhost[key] = null;
  }
  return remote[key].target;
}

function applyState(state, sentAt, receivedAt) {
  connected = true;
  statusEl.classList.remove("err");
  statusEl.textContent = "";
  fadeSeconds = Number(state.duration) || 10;
  // The PC says how old its report was when it answered; half the round trip
  // covers the answer's way back.
  const age = Math.max(0, Number(state.age_ms) || 0);
  const at = receivedAt - Math.max(0, receivedAt - sentAt) / 2 - age;
  takeReport("mix", state, at, receivedAt);
  takeReport("dim", state, at, receivedAt);
  reported = true;
  if (dragging !== "mix") target.mix = ghostTarget("mix", receivedAt);
  if (dragging !== "dim") target.dim = ghostTarget("dim", receivedAt);
  fadeBtn.disabled = !state.fade_enabled;
  durationBtn.textContent = `${state.duration}s`;
  autoFadeBtn.classList.toggle("on", !!state.auto_fade);
  videoBtn.classList.toggle("on", !!state.video_input);
  videoBtn.innerHTML = state.video_input ? "VIDEO<br>INPUT ON" : "VIDEO<br>INPUT OFF";
  if (remote.dim.value <= 0.001) {
    blackBtn.classList.add("from");
    blackBtn.innerHTML = "FADE FROM<br>BLACK";
  } else {
    blackBtn.classList.remove("from");
    blackBtn.innerHTML = "FADE TO<br>BLACK";
  }
  renderThumbs(state.files || [], state.preview || "", state.program || "");
}

// The next drawn position for one knob, dt seconds after the last frame.
function follow(key, now, dt) {
  const r = remote[key];
  const way = Math.sign(r.speed || r.target - r.value);
  const behind = settle[key] * way < 0;
  const fade = Math.exp(-dt / (behind ? CATCH_UP_TIME : SETTLE_TIME));
  settle[key] = Math.abs(settle[key]) < 0.0005 ? 0 : settle[key] * fade;
  const pc = predicted(key, now);
  const next = clamp01(pc + settle[key]);
  const shown = display[key];
  if (r.speed) {
    // While the PC knob is on its way, a knob that is only slightly ahead of
    // it waits instead of stepping back. Further ahead, it goes back.
    const dir = Math.sign(r.speed);
    const lead = (shown - pc) * dir;
    const onTheWay = dir > 0 ? shown <= r.target : shown >= r.target;
    const stepBack = (next - shown) * dir < 0;
    if (stepBack && onTheWay && lead > 0 && lead < Math.abs(r.speed) * WAIT_LIMIT) {
      settle[key] = shown - pc; // so the next report starts from where the knob is drawn
      return shown;
    }
  }
  return next;
}

function paintSliders(ts) {
  const dt = lastFrame ? Math.min(0.05, (ts - lastFrame) / 1000) : 0.016;
  lastFrame = ts;
  display.mix = follow("mix", ts, dt);
  display.dim = follow("dim", ts, dt);
  paintSlidersNow();
  requestAnimationFrame(paintSliders);
}

function pollState() {
  const sentAt = performance.now();
  const ask = ++asked;
  fetch("/api/state")
    .then((r) => {
      if (!r.ok) throw new Error("state");
      return r.json();
    })
    .then((state) => {
      if (ask < answered) return; // an older answer that arrived late
      answered = ask;
      applyState(state, sentAt, performance.now());
    })
    .catch(() => {
      connected = false;
      statusEl.classList.add("err");
      statusEl.textContent = "Can’t reach the pane. Same Wi‑Fi as the livestream PC?";
    });
}

function loadJpeg(img, url) {
  if (img._inflight) return;
  img._inflight = true;
  const probe = new Image();
  probe.onload = () => {
    img.src = probe.src;
    img._inflight = false;
  };
  probe.onerror = () => {
    img._inflight = false;
  };
  probe.src = url;
}

function pollImages() {
  if (!connected || dragging) return;
  const t = Date.now();
  loadJpeg(previewImg, `/api/preview.jpg?t=${t}`);
  loadJpeg(programImg, `/api/program.jpg?t=${t}`);
}

pollState();
pollImages();
setInterval(pollState, 100);
setInterval(pollImages, 200);
requestAnimationFrame(paintSliders);
window.addEventListener("resize", () => {
  lastFrame = 0;
});
