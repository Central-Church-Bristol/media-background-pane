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

let dragging = null;
let localGhost = { mix: null, dim: null };
let lastPost = { mix: 0, dim: 0 };
let lastFilesKey = "";
let connected = false;
let fadeSeconds = 10;
let live = { mix: 0, dim: 1 };
let target = { mix: 0, dim: 1 };
let display = { mix: 0, dim: 1 };
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

function applyState(state) {
  connected = true;
  statusEl.classList.remove("err");
  statusEl.textContent = "";
  fadeSeconds = Number(state.duration) || 10;
  live.mix = Number(state.mix) || 0;
  live.dim = Number(state.dim) || 0;
  if (dragging !== "mix") {
    target.mix = Number(state.mix_target ?? live.mix);
  }
  if (dragging !== "dim") {
    target.dim = Number(state.dim_target ?? live.dim);
  }
  if (Math.abs(live.mix - target.mix) < 0.02 && Math.abs(live.mix - display.mix) > 0.2) {
    display.mix = live.mix;
  }
  if (Math.abs(live.dim - target.dim) < 0.02 && Math.abs(live.dim - display.dim) > 0.2) {
    display.dim = live.dim;
  }
  fadeBtn.disabled = !state.fade_enabled;
  durationBtn.textContent = `${state.duration}s`;
  autoFadeBtn.classList.toggle("on", !!state.auto_fade);
  videoBtn.classList.toggle("on", !!state.video_input);
  videoBtn.innerHTML = state.video_input ? "VIDEO<br>INPUT ON" : "VIDEO<br>INPUT OFF";
  if (live.dim <= 0.001) {
    blackBtn.classList.add("from");
    blackBtn.innerHTML = "FADE FROM<br>BLACK";
  } else {
    blackBtn.classList.remove("from");
    blackBtn.innerHTML = "FADE TO<br>BLACK";
  }
  renderThumbs(state.files || [], state.preview || "", state.program || "");
}

function stepToward(pos, dest, dt) {
  const fade = Math.max(0.05, fadeSeconds);
  const maxV = 1 / fade;
  const delta = dest - pos;
  const mag = Math.abs(delta);
  if (mag < 0.0008) return dest;
  return pos + Math.sign(delta) * Math.min(mag, maxV * dt);
}

function paintSliders(ts) {
  const dt = lastFrame ? Math.min(0.05, (ts - lastFrame) / 1000) : 0.016;
  lastFrame = ts;
  display.mix = stepToward(display.mix, target.mix, dt);
  display.dim = stepToward(display.dim, target.dim, dt);
  if (dragging !== "mix" && Math.abs(target.mix - display.mix) < 0.02) {
    display.mix += (live.mix - display.mix) * Math.min(1, dt * 8);
  }
  if (dragging !== "dim" && Math.abs(target.dim - display.dim) < 0.02) {
    display.dim += (live.dim - display.dim) * Math.min(1, dt * 8);
  }
  paintSlidersNow();
  requestAnimationFrame(paintSliders);
}

function pollState() {
  fetch("/api/state")
    .then((r) => {
      if (!r.ok) throw new Error("state");
      return r.json();
    })
    .then(applyState)
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
