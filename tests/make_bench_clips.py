"""Make the test clips for the measuring kit (tests/bench_pipeline.py).

Every frame carries its own frame number as a large black-and-white pattern
near the middle of the picture. The kit reads the number back from each frame
the pane sends, so it can tell whether each output tick showed the next frame
of each clip, the same frame again (a repeat), or jumped ahead (a skip).

The pattern is made to survive what the pane does to a picture:

- HEVC or H.264 compression, 4:2:0 chroma, and scaling down to 540p or 360p.
  Each bit is a pair of 96 px blocks (at 1080p), one white and one black. The
  reader only asks which of the two is brighter, so it does not need to know
  how bright white has become.
- A 50% blend with another clip. Clip A has its pattern in the upper middle of
  the picture, clips B and C in the lower middle. Each clip is black where the
  other has its pattern, so both can be read at MIX 0.5.
- A moderate vignette dim. Both patterns sit within about 0.7 picture-heights
  of the centre, where the vignette is gentlest. --verify reads them at
  DIM 0.05, at MIX 0 and at MIX 0.5 (540p); in the pane's own output they
  read down to about DIM 0.02 at MIX 0.

Each pattern holds 16 bits: a 10-bit frame number and 6 check bits. The check
bits also say which clip it is (A, B or C).

The rest of the picture is a moving colour pattern with noise. It brings the
HEVC clips to about 40 Mb/s, like the real library, and it repeats exactly
every loop, so the clips loop cleanly.

The clips:

    bench_A_hevc_1080p24.mov   HEVC 1920x1080, 24 fps, 600 frames (25 s), pattern A
    bench_B_hevc_1080p24.mov   HEVC 1920x1080, 24 fps, 528 frames (22 s), pattern B
    bench_C_h264_1080p30.mov   H.264 1920x1080, 30 fps, 600 frames (20 s), pattern B
                               (like Light Tunnel: the 30 fps to 24 fps case)

A and B have different lengths, so their loop points fall at different times.

Usage (run with -B so no .pyc files are written):

    .venv\\Scripts\\python.exe -B tests\\make_bench_clips.py
    .venv\\Scripts\\python.exe -B tests\\make_bench_clips.py --verify-only
    .venv\\Scripts\\python.exe -B tests\\make_bench_clips.py --selftest

The clips go to %TEMP%\\mbp-bench\\media unless --out says otherwise, with a
manifest, bench_clips.json, that the kit reads. --verify decodes the clips
with ffmpeg the way the pane does, then blends and dims them, and checks that
the frame numbers still read.

This file is also imported by bench_pipeline.py and bench_ndi_receive.py for
the layout of the pattern and the reader.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FFMPEG = ROOT / "vendor" / "ffmpeg" / "ffmpeg.exe"


def bench_root() -> Path:
    temp = os.environ.get("TEMP") or os.environ.get("TMP") or str(Path.home() / "AppData" / "Local" / "Temp")
    return Path(temp) / "mbp-bench"


DEFAULT_MEDIA = bench_root() / "media"
MANIFEST_NAME = "bench_clips.json"

# ---------------------------------------------------------------------------
# The pattern. All positions are in 1920x1080 pixels; the reader scales them
# to the output size. Fill, Fit and Actual Size all leave a 16:9 1080p clip
# exactly filling a 16:9 output, so the scaling is the same for every mode.
# ---------------------------------------------------------------------------
REF_W, REF_H = 1920, 1080
BLOCK = 96
COLS, ROWS = 8, 4  # each region: 8 columns x 4 rows of blocks, 16 bit pairs
INDEX_BITS = 10
CHECK_BITS = 6
WORD_BITS = INDEX_BITS + CHECK_BITS
INDEX_MASK = (1 << INDEX_BITS) - 1
GAP = 24  # between the two regions
QUIET = 24  # black margin around each region, in both clips
REGION_W, REGION_H = COLS * BLOCK, ROWS * BLOCK
REGION_X = (REF_W - REGION_W) // 2
REGION_TOP = {"A": REF_H // 2 - GAP // 2 - REGION_H, "B": REF_H // 2 + GAP // 2}
REGION_NAMES = ("A", "B")
# Which clip a pattern belongs to is folded into its check bits.
SALT = {"A": 0x15, "B": 0x2A, "C": 0x0B}
CLIP_CODES = {"A": 1, "B": 2, "C": 3}
CODE_NAMES = {code: name for name, code in CLIP_CODES.items()}
# Sample points inside each block: a 4x4 grid in its middle half.
POINT_FRACTIONS = (0.3125, 0.4375, 0.5625, 0.6875)
POINTS_PER_BLOCK = len(POINT_FRACTIONS) ** 2
BLOCKS_PER_REGION = COLS * ROWS
REGION_SAMPLES = BLOCKS_PER_REGION * POINTS_PER_BLOCK
# A coarse grid over the whole picture, to tell black frames and frames that
# did not change from one output frame to the next.
GRID_COLS, GRID_ROWS = 32, 18
GRID_SAMPLES = GRID_COLS * GRID_ROWS
SAMPLES_PER_FRAME = 2 * REGION_SAMPLES + GRID_SAMPLES
# A bit pair whose two blocks differ by less than this (out of 255) is too
# faint to read; the pattern counts as unreadable in that frame.
MIN_CONTRAST = 6.0
WHITE_Y, BLACK_Y = 235, 16

CLIPS = {
    "A": {"file": "bench_A_hevc_1080p24.mov", "region": "A", "fps": 24, "frames": 600, "codec": "hevc"},
    "B": {"file": "bench_B_hevc_1080p24.mov", "region": "B", "fps": 24, "frames": 528, "codec": "hevc"},
    "C": {"file": "bench_C_h264_1080p30.mov", "region": "B", "fps": 30, "frames": 600, "codec": "h264"},
}


def crc6(value: int, nbits: int = INDEX_BITS) -> int:
    """CRC-6 (x^6 + x + 1) of the low nbits of value."""
    reg = 0
    for i in range(nbits - 1, -1, -1):
        bit = (value >> i) & 1
        top = (reg >> 5) & 1
        reg = (reg << 1) & 0x3F
        if top ^ bit:
            reg ^= 0x03
    return reg


CHECK_TABLE = np.array([crc6(i) for i in range(1 << INDEX_BITS)], dtype=np.int64)


def code_word(index: int, clip: str) -> int:
    index &= INDEX_MASK
    return index | ((int(CHECK_TABLE[index]) ^ SALT[clip]) << INDEX_BITS)


def bit_blocks(bit: int) -> tuple[tuple[int, int], tuple[int, int]]:
    """(row, col) of the upper and lower block of a bit. Bit set: upper block white."""
    col = bit % COLS
    pair = bit // COLS
    return (2 * pair, col), (2 * pair + 1, col)


def region_rect(region: str, quiet: int = 0) -> tuple[int, int, int, int]:
    """x0, y0, x1, y1 of a region in 1920x1080 pixels, with an optional margin."""
    x0 = REGION_X - quiet
    y0 = REGION_TOP[region] - quiet
    return x0, y0, REGION_X + REGION_W + quiet, REGION_TOP[region] + REGION_H + quiet


def paint_pattern_y(y_plane: np.ndarray, index: int, clip: str, region: str) -> None:
    """Draw clip's pattern for frame index into a 1080p Y plane (both regions' quiet zones are cleared too)."""
    for name in REGION_NAMES:
        x0, y0, x1, y1 = region_rect(name, QUIET)
        y_plane[y0:y1, x0:x1] = BLACK_Y
    word = code_word(index, clip)
    top = REGION_TOP[region]
    for bit in range(WORD_BITS):
        (r_hi, c), (r_lo, _c) = bit_blocks(bit)
        white_row = r_hi if (word >> bit) & 1 else r_lo
        bx = REGION_X + c * BLOCK
        by = top + white_row * BLOCK
        y_plane[by : by + BLOCK, bx : bx + BLOCK] = WHITE_Y


def clear_pattern_chroma(u_plane: np.ndarray, v_plane: np.ndarray) -> None:
    """Neutral chroma over both regions and their quiet zones (4:2:0 planes)."""
    for name in REGION_NAMES:
        x0, y0, x1, y1 = region_rect(name, QUIET)
        u_plane[y0 // 2 : y1 // 2, x0 // 2 : x1 // 2] = 128
        v_plane[y0 // 2 : y1 // 2, x0 // 2 : x1 // 2] = 128


# ---------------------------------------------------------------------------
# The reader
# ---------------------------------------------------------------------------
def sample_points(width: int, height: int) -> tuple[np.ndarray, np.ndarray]:
    """Pixel rows and columns to sample in a width x height picture.

    Order: region A (32 blocks x 16 points, block = row * 8 + col), region B
    (the same), then the coarse grid (18 rows x 32 columns).
    """
    sx = width / REF_W
    sy = height / REF_H
    ys: list[int] = []
    xs: list[int] = []
    for name in REGION_NAMES:
        top = REGION_TOP[name]
        for r in range(ROWS):
            for c in range(COLS):
                for fy in POINT_FRACTIONS:
                    for fx in POINT_FRACTIONS:
                        ys.append(int((top + (r + fy) * BLOCK) * sy))
                        xs.append(int((REGION_X + (c + fx) * BLOCK) * sx))
    for j in range(GRID_ROWS):
        for i in range(GRID_COLS):
            ys.append(int((j + 0.5) / GRID_ROWS * height))
            xs.append(int((i + 0.5) / GRID_COLS * width))
    y_arr = np.clip(np.array(ys, dtype=np.intp), 0, height - 1)
    x_arr = np.clip(np.array(xs, dtype=np.intp), 0, width - 1)
    return y_arr, x_arr


def frame_format(array: np.ndarray, width: int | None = None, height: int | None = None) -> tuple[str, int, int]:
    """('bgra' | 'nv12' | 'gray' | 'unknown', width, height) of a frame array.

    Today's frames are BGRA (h, w, 4). The rebuilt pipeline's are NV12
    (h * 3 // 2, w): the Y plane, then interleaved U and V at half size.
    """
    if array.ndim == 3 and array.shape[2] == 4:
        return "bgra", int(array.shape[1]), int(array.shape[0])
    if array.ndim == 2:
        rows, cols = int(array.shape[0]), int(array.shape[1])
        if height is not None and width is not None and cols == width and rows == height * 3 // 2:
            return "nv12", width, height
        if rows % 3 == 0:
            return "nv12", cols, rows * 2 // 3
        return "gray", cols, rows
    return "unknown", 0, 0


def luma_sampler(fmt: str, width: int, height: int):
    """A function frame -> uint8 samples (SAMPLES_PER_FRAME) for this format and size."""
    ys, xs = sample_points(width, height)
    if fmt == "bgra":
        # The pattern is grey, so the green channel is as good as luma.
        def sample(frame: np.ndarray) -> np.ndarray:
            return frame[ys, xs, 1]

        return sample
    if fmt in ("nv12", "gray", "y"):
        # Y is the first `height` rows of an NV12 frame.
        def sample(frame: np.ndarray) -> np.ndarray:
            return frame[ys, xs]

        return sample
    if fmt == "uyvy":
        # (h, 2w) bytes: U Y V Y. Luma at the odd bytes.
        xs2 = xs * 2 + 1

        def sample(frame: np.ndarray) -> np.ndarray:
            return frame[ys, xs2]

        return sample
    raise ValueError(f"no sampler for {fmt}")


def decode_samples(samples: np.ndarray) -> dict[str, np.ndarray]:
    """Read the frame numbers from samples (n, SAMPLES_PER_FRAME) uint8.

    Returns, per region ('A' upper, 'B' lower):
      index_<r>:    frame number, or -1 where the pattern could not be read
      clip_<r>:     clip code (1 A, 2 B, 3 C; 0 unreadable)
      contrast_<r>: the faintest bit pair's brightness difference (0-255)
    and grid (n, GRID_SAMPLES) uint8, mean_luma (n,) float.
    """
    samples = np.asarray(samples)
    if samples.ndim == 1:
        samples = samples[None, :]
    n = samples.shape[0]
    out: dict[str, np.ndarray] = {}
    blocks = samples[:, : 2 * REGION_SAMPLES].reshape(n, 2, BLOCKS_PER_REGION, POINTS_PER_BLOCK)
    means = blocks.astype(np.float32).mean(axis=3).reshape(n, 2, ROWS, COLS)
    upper = means[:, :, 0::2, :]
    lower = means[:, :, 1::2, :]
    diff = upper - lower  # (n, 2 regions, 2 pairs, 8 cols): bit = pair * 8 + col
    bits = diff > 0
    contrast = np.abs(diff).min(axis=(2, 3))
    weights = (np.int64(1) << (np.arange(2)[:, None] * COLS + np.arange(COLS)[None, :])).astype(np.int64)
    words = (bits.astype(np.int64) * weights[None, None]).sum(axis=(2, 3))
    index = words & INDEX_MASK
    salt = (words >> INDEX_BITS) ^ CHECK_TABLE[index]
    for k, region in enumerate(REGION_NAMES):
        readable = contrast[:, k] >= MIN_CONTRAST
        clip = np.zeros(n, dtype=np.int64)
        if region == "A":
            clip[(salt[:, k] == SALT["A"]) & readable] = CLIP_CODES["A"]
        else:
            clip[(salt[:, k] == SALT["B"]) & readable] = CLIP_CODES["B"]
            clip[(salt[:, k] == SALT["C"]) & readable] = CLIP_CODES["C"]
        out[f"index_{region}"] = np.where(clip > 0, index[:, k], -1)
        out[f"clip_{region}"] = clip
        out[f"contrast_{region}"] = contrast[:, k]
    grid = samples[:, 2 * REGION_SAMPLES :]
    out["grid"] = grid
    out["mean_luma"] = grid.astype(np.float32).mean(axis=1)
    return out


# ---------------------------------------------------------------------------
# Making the clips
# ---------------------------------------------------------------------------
class Background:
    """A moving colour pattern that repeats exactly every `frames` frames, plus noise."""

    def __init__(self, clip: str, frames: int) -> None:
        self.frames = frames
        self.seed = {"A": 11, "B": 22, "C": 33}[clip]
        x = (np.arange(REF_W, dtype=np.float32) + 0.5) / REF_W
        y = (np.arange(REF_H, dtype=np.float32) + 0.5) / REF_H
        k1, k2 = {"A": (2.0, 1.5), "B": (3.0, 1.0), "C": (1.5, 2.5)}[clip]
        self._a = 2 * np.pi * k1 * x
        self._b = 2 * np.pi * k2 * y
        self._c = 2 * np.pi * 3.0 * x
        self._d = 2 * np.pi * 2.0 * y
        xh = (np.arange(REF_W // 2, dtype=np.float32) + 0.5) / (REF_W // 2)
        yh = (np.arange(REF_H // 2, dtype=np.float32) + 0.5) / (REF_H // 2)
        self._xh = 2 * np.pi * xh
        self._yh = 2 * np.pi * yh
        self.u_bias, self.v_bias = {"A": (-18.0, 30.0), "B": (30.0, -14.0), "C": (8.0, 8.0)}[clip]

    def frame(self, i: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        phase = np.float32(2 * np.pi * (i % self.frames) / self.frames)
        # sin(a + p) * cos(b - p) and sin(c + d + 2p), as outer products.
        t1 = np.sin(self._a + phase)[None, :] * np.cos(self._b - phase)[:, None]
        ca = self._c + 2 * phase
        t2 = np.sin(ca)[None, :] * np.cos(self._d)[:, None] + np.cos(ca)[None, :] * np.sin(self._d)[:, None]
        luma = 0.5 + 0.22 * t1 + 0.16 * t2
        rng = np.random.default_rng(self.seed * 100003 + (i % self.frames))
        y = 16.0 + 219.0 * luma
        y += rng.integers(-14, 15, size=y.shape, dtype=np.int16)
        u = 128.0 + self.u_bias + 40.0 * np.sin(self._xh + phase)[None, :] * np.cos(self._yh)[:, None]
        v = 128.0 + self.v_bias + 40.0 * np.cos(self._yh - phase)[:, None] * np.ones_like(self._xh)[None, :]
        u += rng.integers(-6, 7, size=u.shape, dtype=np.int16)
        v += rng.integers(-6, 7, size=v.shape, dtype=np.int16)
        y8 = np.clip(y, 16, 235).astype(np.uint8)
        u8 = np.clip(u, 16, 240).astype(np.uint8)
        v8 = np.clip(v, 16, 240).astype(np.uint8)
        return y8, u8, v8


def render_frame(clip: str, i: int, background: Background) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    spec = CLIPS[clip]
    y, u, v = background.frame(i)
    paint_pattern_y(y, i % spec["frames"], clip, spec["region"])
    clear_pattern_chroma(u, v)
    return y, u, v


def _encoder_args(spec: dict) -> list[str]:
    tags = [
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
        "-colorspace", "bt709",
        "-color_range", "tv",
    ]
    if spec["codec"] == "hevc":
        params = (
            "keyint=24:min-keyint=24:scenecut=0:"
            "colorprim=bt709:transfer=bt709:colormatrix=bt709:range=limited:log-level=error"
        )
        return [
            "-c:v", "libx265", "-preset", "fast",
            "-b:v", "40M", "-maxrate", "48M", "-bufsize", "80M",
            "-x265-params", params,
            "-pix_fmt", "yuv420p", *tags, "-tag:v", "hvc1",
            "-video_track_timescale", "12288",
        ]
    return [
        "-c:v", "libx264", "-preset", "medium", "-profile:v", "main",
        "-b:v", "8M", "-maxrate", "10M", "-bufsize", "16M",
        "-g", "30", "-keyint_min", "30", "-sc_threshold", "0",
        "-x264-params", "colorprim=bt709:transfer=bt709:colormatrix=bt709",
        "-pix_fmt", "yuv420p", *tags, "-tag:v", "avc1",
        "-video_track_timescale", "15360",
    ]


def make_clip(clip: str, out_dir: Path, ffmpeg: Path) -> Path:
    spec = CLIPS[clip]
    path = out_dir / spec["file"]
    tmp = path.with_name(path.stem + ".partial" + path.suffix)
    cmd = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{REF_W}x{REF_H}", "-r", str(spec["fps"]),
        "-i", "pipe:0", "-an", *_encoder_args(spec), str(tmp),
    ]
    background = Background(clip, spec["frames"])
    started = time.perf_counter()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, creationflags=_no_window())
    assert proc.stdin is not None
    try:
        for i in range(spec["frames"]):
            y, u, v = render_frame(clip, i, background)
            proc.stdin.write(y.tobytes())
            proc.stdin.write(u.tobytes())
            proc.stdin.write(v.tobytes())
            if i % 120 == 119:
                print(f"  {spec['file']}: {i + 1}/{spec['frames']} frames", flush=True)
    finally:
        proc.stdin.close()
        code = proc.wait()
    if code != 0:
        raise RuntimeError(f"ffmpeg failed ({code}) making {path.name}")
    tmp.replace(path)
    seconds = spec["frames"] / spec["fps"]
    rate = path.stat().st_size * 8 / seconds / 1e6
    print(f"  {path.name}: {path.stat().st_size / 1e6:.0f} MB, {rate:.1f} Mb/s, {time.perf_counter() - started:.0f} s to make")
    return path


def _no_window() -> int:
    return 0x08000000 if sys.platform == "win32" else 0


def write_manifest(out_dir: Path) -> Path:
    clips = {}
    for name, spec in CLIPS.items():
        path = out_dir / spec["file"]
        clips[name] = {**spec, "path": str(path), "bytes": path.stat().st_size if path.is_file() else None}
    manifest = {
        "made": time.strftime("%Y-%m-%d %H:%M:%S"),
        "layout": {
            "ref_size": [REF_W, REF_H],
            "block": BLOCK,
            "cols": COLS,
            "rows": ROWS,
            "index_bits": INDEX_BITS,
            "check_bits": CHECK_BITS,
            "region_x": REGION_X,
            "region_top": REGION_TOP,
            "salt": SALT,
        },
        "clips": clips,
    }
    path = out_dir / MANIFEST_NAME
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path


def load_manifest(media_dir: Path) -> dict:
    return json.loads((media_dir / MANIFEST_NAME).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------
def _smoothstep(edge0: float, edge1: float, x: float) -> float:
    t = min(1.0, max(0.0, (x - edge0) / (edge1 - edge0)))
    return t * t * (3.0 - 2.0 * t)


def vignette_gain(height: int, width: int, dimmer: float) -> np.ndarray:
    """The pane's vignette dim (engine._apply_vignette_dim) as a gain on R'G'B'.

    out = sqrt(rgb^2 * shade * tail) = rgb * sqrt(shade * tail).
    """
    aspect = width / max(1, height)
    ys = ((np.arange(height, dtype=np.float32) + 0.5) / height - 0.5) * 2.0
    xs = ((np.arange(width, dtype=np.float32) + 0.5) / width - 0.5) * 2.0 * aspect
    r2 = xs[None, :] ** 2 + ys[:, None] ** 2
    progress = 1.0 - dimmer
    vig = min(1.0, progress / 0.8) ** 0.5
    falloff = vig * 2.6
    shade = 1.0 / np.square(np.power(r2, 1.85 / 2.0) * falloff * falloff + 1.0)
    tail = 1.0 - _smoothstep(0.76, 1.0, progress)
    return np.sqrt(np.clip(shade * tail, 0.0, 1.0)).astype(np.float32)


def _read_frames(ffmpeg: Path, path: Path, vf: str, pix_fmt: str, frame_bytes: int, count: int) -> list[bytes]:
    cmd = [
        str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostdin",
        "-hwaccel", "d3d11va", "-i", str(path), "-an", "-vf", vf,
        "-frames:v", str(count), "-f", "rawvideo", "-pix_fmt", pix_fmt, "pipe:1",
    ]
    out = subprocess.run(cmd, capture_output=True, check=True, creationflags=_no_window()).stdout
    return [out[i : i + frame_bytes] for i in range(0, len(out) - frame_bytes + 1, frame_bytes)]


def verify(media_dir: Path, ffmpeg: Path, count: int = 48) -> bool:
    """Decode the clips like the pane does and check the numbers read back."""
    ok = True
    a = media_dir / CLIPS["A"]["file"]
    b = media_dir / CLIPS["B"]["file"]
    c = media_dir / CLIPS["C"]["file"]
    print("Reading the clips back through ffmpeg (d3d11va decode):")

    def check(name: str, frames: np.ndarray, sampler, region: str, expect_clip: str, expect: list[int]) -> bool:
        samples = np.stack([sampler(f) for f in frames])
        dec = decode_samples(samples)
        got = dec[f"index_{region}"].tolist()
        clips = dec[f"clip_{region}"].tolist()
        good = got == expect and all(code == CLIP_CODES[expect_clip] for code in clips)
        low = float(dec[f"contrast_{region}"].min())
        print(f"  {name:<44} {'OK' if good else 'FAILED':<6} faintest pair {low:5.1f}")
        if not good:
            print(f"     expected {expect[:12]}..., read {got[:12]}...")
        return good

    # Today's pipeline: 540p BGRA via lanczos, and 1080p BGRA.
    for size in ((960, 540), (1920, 1080), (640, 360)):
        w, h = size
        vf = f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h},format=bgra,fps=24"
        fa = np.stack([np.frombuffer(x, np.uint8).reshape(h, w, 4) for x in _read_frames(ffmpeg, a, vf, "bgra", w * h * 4, count)])
        fb = np.stack([np.frombuffer(x, np.uint8).reshape(h, w, 4) for x in _read_frames(ffmpeg, b, vf, "bgra", w * h * 4, count)])
        sampler = luma_sampler("bgra", w, h)
        expect = list(range(len(fa)))
        ok &= check(f"A, BGRA {w}x{h}", fa, sampler, "A", "A", expect)
        ok &= check(f"B, BGRA {w}x{h}", fb, sampler, "B", "B", list(range(len(fb))))
        n = min(len(fa), len(fb))
        mixed = ((fa[:n].astype(np.uint16) * 128 + fb[:n].astype(np.uint16) * 128) >> 8).astype(np.uint8)
        ok &= check(f"A in a 50% blend, BGRA {w}x{h}", mixed, sampler, "A", "A", list(range(n)))
        ok &= check(f"B in a 50% blend, BGRA {w}x{h}", mixed, sampler, "B", "B", list(range(n)))
        if (w, h) == (960, 540):
            print("  Dimmed (vignette), 540p BGRA: share of frames read")
            for dimmer in (0.8, 0.6, 0.5, 0.4, 0.3, 0.2, 0.15, 0.1, 0.05):
                gain = vignette_gain(h, w, dimmer)[None, :, :, None]
                line = f"     DIM {dimmer:4.2f}:"
                for label, frames, mix_ok in (("MIX 0", fa[:n], None), ("MIX 0.5", mixed, None)):
                    dimmed = np.clip(frames.astype(np.float32) * gain, 0, 255).astype(np.uint8)
                    samples = np.stack([sampler(f) for f in dimmed])
                    dec = decode_samples(samples)
                    read_a = float(np.mean(dec["index_A"] == np.arange(n)))
                    part = f"  {label} A {read_a:4.0%}"
                    if label == "MIX 0.5":
                        read_b = float(np.mean(dec["index_B"] == np.arange(n)))
                        part += f" B {read_b:4.0%}"
                    wrong = int(np.sum((dec["index_A"] >= 0) & (dec["index_A"] != np.arange(n))))
                    if wrong:
                        part += f" ({wrong} WRONG)"
                        ok = False
                    line += part
                print(line)
    # The rebuilt pipeline: 1080p NV12.
    w, h = 1920, 1080
    vf = "fps=24,format=nv12"
    raw = _read_frames(ffmpeg, a, vf, "nv12", w * h * 3 // 2, count)
    fa = np.stack([np.frombuffer(x, np.uint8).reshape(h * 3 // 2, w) for x in raw])
    raw = _read_frames(ffmpeg, b, vf, "nv12", w * h * 3 // 2, count)
    fb = np.stack([np.frombuffer(x, np.uint8).reshape(h * 3 // 2, w) for x in raw])
    sampler = luma_sampler("nv12", w, h)
    ok &= check("A, NV12 1920x1080", fa, sampler, "A", "A", list(range(len(fa))))
    ok &= check("B, NV12 1920x1080", fb, sampler, "B", "B", list(range(len(fb))))
    n = min(len(fa), len(fb))
    mixed = ((fa[:n].astype(np.uint16) + fb[:n].astype(np.uint16) + 1) >> 1).astype(np.uint8)
    ok &= check("A in a 50% blend, NV12 1920x1080", mixed, sampler, "A", "A", list(range(n)))
    ok &= check("B in a 50% blend, NV12 1920x1080", mixed, sampler, "B", "B", list(range(n)))
    gain = vignette_gain(h, w, 0.3)
    dimmed = mixed.astype(np.float32)
    dimmed[:, :h] = 16 + (dimmed[:, :h] - 16) * gain[None]
    ok &= check("A, NV12 blend at DIM 0.3", np.clip(dimmed, 0, 255).astype(np.uint8), sampler, "A", "A", list(range(n)))
    # The 30 fps clip through fps=24, as the pane plays it.
    w, h = 960, 540
    vf = f"scale={w}:{h}:flags=lanczos,format=bgra,fps=24"
    fc = np.stack([np.frombuffer(x, np.uint8).reshape(h, w, 4) for x in _read_frames(ffmpeg, c, vf, "bgra", w * h * 4, count)])
    dec = decode_samples(np.stack([luma_sampler("bgra", w, h)(f) for f in fc]))
    idx = dec["index_B"]
    slots = np.round(idx * 24 / 30).astype(int)
    steps = np.diff(slots)
    good = bool(np.all(idx >= 0) and np.all(dec["clip_B"] == CLIP_CODES["C"]) and np.all(steps == 1))
    ok &= good
    print(f"  {'C at 24 fps (frame numbers mapped to 24 fps)':<44} {'OK' if good else 'FAILED'}")
    if not good:
        print(f"     read {idx[:16].tolist()} -> slots {slots[:16].tolist()}")
    return ok


def selftest() -> bool:
    """Draw, sample and read patterns in memory (no ffmpeg)."""
    ok = True
    for clip in ("A", "B", "C"):
        bg = Background(clip, CLIPS[clip]["frames"])
        for i in (0, 1, 2, 511, CLIPS[clip]["frames"] - 1):
            y, _u, _v = render_frame(clip, i, bg)
            small = y.reshape(540, 2, 960, 2).mean(axis=(1, 3)).astype(np.uint8)
            for frame, (w, h) in ((y, (1920, 1080)), (small, (960, 540))):
                dec = decode_samples(luma_sampler("gray", w, h)(frame))
                region = CLIPS[clip]["region"]
                got = int(dec[f"index_{region}"][0])
                code = int(dec[f"clip_{region}"][0])
                other = "B" if region == "A" else "A"
                if got != i or code != CLIP_CODES[clip] or int(dec[f"index_{other}"][0]) != -1:
                    print(f"  selftest FAILED: clip {clip} frame {i} at {w}x{h}: read {got} clip {code}")
                    ok = False
    print("  pattern selftest:", "OK" if ok else "FAILED")
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=DEFAULT_MEDIA, help=f"folder for the clips (default {DEFAULT_MEDIA})")
    parser.add_argument("--ffmpeg", type=Path, default=DEFAULT_FFMPEG, help="ffmpeg.exe (default vendor\\ffmpeg\\ffmpeg.exe)")
    parser.add_argument("--clips", default="A,B,C", help="which clips to make (default A,B,C)")
    parser.add_argument("--force", action="store_true", help="make clips again even if they exist")
    parser.add_argument("--verify-only", action="store_true", help="only check existing clips")
    parser.add_argument("--no-verify", action="store_true", help="skip the read-back check")
    parser.add_argument("--selftest", action="store_true", help="check the pattern code in memory, then stop")
    args = parser.parse_args()

    if args.selftest:
        return 0 if selftest() else 1
    if not args.ffmpeg.is_file():
        print(f"ffmpeg not found: {args.ffmpeg}")
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    if not args.verify_only:
        if not selftest():
            return 1
        for clip in [c.strip().upper() for c in args.clips.split(",") if c.strip()]:
            path = args.out / CLIPS[clip]["file"]
            if path.is_file() and not args.force:
                print(f"  {path.name}: exists (use --force to make it again)")
                continue
            print(f"Making {path.name} ...", flush=True)
            make_clip(clip, args.out, args.ffmpeg)
        print(f"Manifest: {write_manifest(args.out)}")
    if args.no_verify:
        return 0
    good = verify(args.out, args.ffmpeg)
    print("All clips read back correctly." if good else "SOME CHECKS FAILED.")
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
