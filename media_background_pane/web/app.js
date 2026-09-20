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
  const point = event.touches ? event.touches[0] : event;
  if (vertical) {
    const t = (point.clientY - rect.top - 11) / Math.max(1, rect.height - 22);
    return clamp01(1 - t);
  }
  const t = (point.clientX - rect.left - 11) / Math.max(1, rect.width - 22);
  return clamp01(t);
}

function placeHorizontal(el, fill, ghost, knob, value, target, showGhost) {
  const rect = el.getBoundingClientRect();
  const span = Math.max(1, rect.width - 22);
  const x = 11 + value * span;
  knob.style.left = `${x - 11}px`;
  knob.style.top = `${rect.height / 2 - 11}px`;
  fill.style.right = `${rect.width - x}px`;
  const gx = 11 + target * span;
  ghost.style.left = `${gx - 11}px`;
  ghost.style.top = `${rect.height / 2 - 11}px`;
  ghost.classList.toggle("on", showGhost && Math.abs(target - value) > 0.01);
}

function placeVertical(el, fill, ghost, knob, value, target, showGhost) {
  const rect = el.getBoundingClientRect();
  const span = Math.max(1, rect.height - 22);
  const y = 11 + (1 - value) * span;
  knob.style.left = `${rect.width / 2 - 11}px`;
  knob.style.top = `${y - 11}px`;
  fill.style.top = `${y}px`;
  const gy = 11 + (1 - target) * span;
  ghost.style.left = `${rect.width / 2 - 11}px`;
  ghost.style.top = `${gy - 11}px`;
  ghost.classList.toggle("on", showGhost && Math.abs(target - value) > 0.01);
}

function postTarget(key, value, force) {
  const now = Date.now();
  if (!force && now - lastPost[key] < 50) return;
  lastPost[key] = now;
  post(key === "mix" ? "mix_target" : "dim_target", { value });
}

function bindSlider(el, key, vertical) {
  const start = (event) => {
    event.preventDefault();
    dragging = key;
    const value = valueFromEvent(el, event, vertical);
    localGhost[key] = value;
    postTarget(key, value, true);
  };
  const move = (event) => {
    if (dragging !== key) return;
    event.preventDefault();
    const value = valueFromEvent(el, event, vertical);
    localGhost[key] = value;
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
  const mix = Number(state.mix) || 0;
  const dim = Number(state.dim) || 0;
  const mixTarget = dragging === "mix" && localGhost.mix != null ? localGhost.mix : Number(state.mix_target ?? mix);
  const dimTarget = dragging === "dim" && localGhost.dim != null ? localGhost.dim : Number(state.dim_target ?? dim);
  placeHorizontal(
    mixEl,
    document.getElementById("mixFill"),
    document.getElementById("mixGhost"),
    document.getElementById("mixKnob"),
    mix,
    mixTarget,
    dragging === "mix" || Math.abs(mixTarget - mix) > 0.01
  );
  placeVertical(
    dimEl,
    document.getElementById("dimFill"),
    document.getElementById("dimGhost"),
    document.getElementById("dimKnob"),
    dim,
    dimTarget,
    dragging === "dim" || Math.abs(dimTarget - dim) > 0.01
  );
  fadeBtn.disabled = !state.fade_enabled;
  durationBtn.textContent = `${state.duration}s`;
  autoFadeBtn.classList.toggle("on", !!state.auto_fade);
  videoBtn.classList.toggle("on", !!state.video_input);
  videoBtn.innerHTML = state.video_input ? "VIDEO<br>INPUT ON" : "VIDEO<br>INPUT OFF";
  if (dim <= 0.001) {
    blackBtn.classList.add("from");
    blackBtn.innerHTML = "FADE FROM<br>BLACK";
  } else {
    blackBtn.classList.remove("from");
    blackBtn.innerHTML = "FADE TO<br>BLACK";
  }
  renderThumbs(state.files || [], state.preview || "", state.program || "");
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

function pollImages() {
  if (!connected) return;
  const t = Date.now();
  previewImg.src = `/api/preview.jpg?t=${t}`;
  programImg.src = `/api/program.jpg?t=${t}`;
}

pollState();
pollImages();
setInterval(pollState, 100);
setInterval(pollImages, 200);
window.addEventListener("resize", pollState);
