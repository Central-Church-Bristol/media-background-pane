"""NV12 frames: the one picture format between the decoders, the mixer and NDI.

A frame is a C-contiguous uint8 array of shape (height * 3 // 2, width):
- rows [0:height] are luma (Y), one byte per pixel
- rows [height:] are chroma at half resolution, U and V interleaved

Values are limited range (Y 16-235, U/V 16-240) in BT.709. That is what the
decoder's ffmpeg command produces, and what NDI assumes at every size (checked
on 5 Nov 2026: BGRX sent at 1080p, 720p, 540p and 360p came back as BT.709).

NV12 is 1.5 bytes a pixel against 4 for BGRA, and it is what the GPU decoder
hands over, so nothing converts colour on the way through. Blending and dimming
work directly on it: in limited-range YUV a weighted mix of two pictures is the
same as mixing them in R'G'B', and a per-pixel gain on R'G'B' is a gain on Y-16
and on U/V-128.
"""

from __future__ import annotations

import numpy as np

try:  # OpenCV does the per-pixel work about 15x faster than numpy.
    import cv2
except ImportError:  # pragma: no cover - the app still works, just slower
    cv2 = None
else:
    # One thread: each operation takes about a millisecond at 1080p, and a
    # pool of 16 worker threads woken 24 times a second costs more CPU than
    # it saves, and competes with ProPresenter for cores.
    cv2.setNumThreads(1)

Y_BLACK = 16
UV_BLACK = 128

# BT.709.
_KR = 0.2126
_KB = 0.0722
_KG = 1.0 - _KR - _KB

# The vignette dim. Same constants and same look as the BGRA version it replaced:
# natural vignetting ≈ cos⁴(θ) ≈ 1/(1+r²)². The cosine develops until fade
# progress 0.8, then that shape holds and the whole frame eases to black, so
# the hotspot never pinches to a dot. Exposure only: colour is left alone.
VIGNETTE_LIMIT = 0.8
VIGNETTE_STRENGTH = 2.6
VIGNETTE_RADIUS = 1.85
VIGNETTE_GAMMA = 0.5


def nv12_shape(width: int, height: int) -> tuple[int, int]:
    return (height * 3 // 2, width)


def black(width: int, height: int) -> np.ndarray:
    frame = np.empty(nv12_shape(width, height), dtype=np.uint8)
    fill_black(frame)
    return frame


def fill_black(frame: np.ndarray) -> np.ndarray:
    height = frame.shape[0] * 2 // 3
    frame[:height] = Y_BLACK
    frame[height:] = UV_BLACK
    return frame


def frame_size(frame: np.ndarray) -> tuple[int, int]:
    """(width, height) of the picture in an NV12 array."""
    return int(frame.shape[1]), int(frame.shape[0]) * 2 // 3


def copy_into(dest: np.ndarray, src: np.ndarray) -> None:
    """Copy src into dest. A frame of another size is centred on black."""
    if src.shape == dest.shape:
        np.copyto(dest, src)
        return
    fill_black(dest)
    dw, dh = frame_size(dest)
    sw, sh = frame_size(src)
    w = min(dw, sw) & ~1
    h = min(dh, sh) & ~1
    dx, dy = ((dw - w) // 2) & ~1, ((dh - h) // 2) & ~1
    sx, sy = ((sw - w) // 2) & ~1, ((sh - h) // 2) & ~1
    dest[dy : dy + h, dx : dx + w] = src[sy : sy + h, sx : sx + w]
    dest[dh + dy // 2 : dh + (dy + h) // 2, dx : dx + w] = src[sh + sy // 2 : sh + (sy + h) // 2, sx : sx + w]


def _smoothstep(edge0: float, edge1: float, x: float) -> float:
    t = min(1.0, max(0.0, (x - edge0) / (edge1 - edge0)))
    return t * t * (3.0 - 2.0 * t)


def vignette_r2(height: int, width: int) -> np.ndarray:
    """Squared distance from the centre, in units where the frame is 2 tall."""
    aspect = width / max(1, height)
    ys = ((np.arange(height, dtype=np.float32) + 0.5) / height - 0.5) * 2.0
    xs = ((np.arange(width, dtype=np.float32) + 0.5) / width - 0.5) * 2.0 * aspect
    return xs[np.newaxis, :] ** 2 + ys[:, np.newaxis] ** 2


def vignette_gain_terms(dimmer: float) -> tuple[float, float]:
    """(f², s) for gain = s / (1 + f² · r^radius) at this DIM position.

    The BGRA version computed sqrt(rgb² · shade · tail) with
    shade = 1/(1 + f²·r^radius)². That is rgb · sqrt(tail) / (1 + f²·r^radius),
    a plain gain, which is what lets the dim work on NV12.
    """
    progress = 1.0 - dimmer
    vig = min(1.0, progress / VIGNETTE_LIMIT) ** VIGNETTE_GAMMA
    falloff = vig * VIGNETTE_STRENGTH
    tail = 1.0 - _smoothstep(VIGNETTE_LIMIT - 0.04, 1.0, progress)
    return falloff * falloff, float(np.sqrt(max(0.0, tail)))


class Mixer:
    """Blend and dim NV12 frames of one size, into buffers it owns.

    Each method returns one of the mixer's own buffers. The caller copies it
    (NdiSender does) before the next call of the same method.
    """

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height
        shape = nv12_shape(width, height)
        self.black = black(width, height)
        self._mixed = np.empty(shape, dtype=np.uint8)
        self._dimmed = np.empty(shape, dtype=np.uint8)
        # r^radius in NV12 layout: luma at full resolution, then each chroma
        # sample's value twice (U and V share it).
        power = VIGNETTE_RADIUS / 2.0
        luma = np.power(vignette_r2(height, width), power).astype(np.float32)
        chroma = np.power(vignette_r2(height // 2, width // 2), power).astype(np.float32)
        self._radius = np.empty(shape, dtype=np.float32)
        self._radius[:height] = luma
        self._radius[height:] = np.repeat(chroma, 2, axis=1)
        self._offset = np.empty(shape, dtype=np.float32)
        self._offset[:height] = Y_BLACK
        self._offset[height:] = UV_BLACK
        self._gain = np.empty(shape, dtype=np.float32)
        self._gain_for: float | None = None
        # The dim as two 8-bit planes, out = src * gain8/255 + base8, where
        # base8 = offset * (1 - gain). Rebuilt when DIM moves, from a quarter
        # resolution map: the vignette is a smooth shape, so scaling it up
        # loses nothing, and a fade rebuilds it every frame.
        self._gain8 = np.empty(shape, dtype=np.uint8)
        self._base8 = np.empty(shape, dtype=np.uint8)
        self._scaled8 = np.empty(shape, dtype=np.uint8)
        small_h, small_w = max(2, height // 4), max(2, width // 4)
        self._radius_small = np.power(vignette_r2(small_h, small_w), power).astype(np.float32)
        self._work = np.empty(shape, dtype=np.float32)
        self._wide_a: np.ndarray | None = None
        self._wide_b: np.ndarray | None = None

    def blend(self, a: np.ndarray, b: np.ndarray, mix: float) -> np.ndarray:
        """(1 - mix) · a + mix · b."""
        out = self._mixed
        if cv2 is not None:
            cv2.addWeighted(a, 1.0 - mix, b, mix, 0.0, dst=out)
            return out
        if self._wide_a is None:
            self._wide_a = np.empty(a.shape, dtype=np.uint16)
            self._wide_b = np.empty(a.shape, dtype=np.uint16)
        wa, wb = self._wide_a, self._wide_b
        weight = int(round(mix * 256))
        np.multiply(a, 256 - weight, out=wa, dtype=np.uint16)
        np.multiply(b, weight, out=wb, dtype=np.uint16)
        np.add(wa, wb, out=wa)
        np.add(wa, 128, out=wa)
        np.right_shift(wa, 8, out=wa)
        np.copyto(out, wa, casting="unsafe")
        return out

    def dim(self, src: np.ndarray, dimmer: float) -> np.ndarray:
        """The camera-vignette dim. dimmer 1 is untouched, 0 is black."""
        if dimmer <= 0.001:
            return self.black
        if dimmer >= 0.999:
            return src
        gain = self._gain_map(dimmer)
        work = self._work
        out = self._dimmed
        if cv2 is not None:
            # offset + (src - offset) * g = src * g + offset * (1 - g), in two
            # 8-bit passes: about 1 ms a 1080p frame instead of 6 in float.
            cv2.multiply(src, self._gain8, dst=self._scaled8, scale=1.0 / 255.0)
            cv2.add(self._scaled8, self._base8, dst=out)
            return out
        np.subtract(src, self._offset, out=work, dtype=np.float32)
        np.multiply(work, gain, out=work)
        np.add(work, self._offset, out=work)
        np.clip(work, 0.0, 255.0, out=work)
        np.rint(work, out=work)
        np.copyto(out, work, casting="unsafe")
        return out

    def _gain_map(self, dimmer: float) -> np.ndarray:
        key = round(dimmer, 5)
        if self._gain_for == key:
            return self._gain
        f2, s = vignette_gain_terms(dimmer)
        gain = self._gain
        if cv2 is not None:
            small = s / (1.0 + np.float32(f2) * self._radius_small)
            gain8 = np.clip(np.rint(small * 255.0), 0, 255).astype(np.uint8)
            base_y = np.clip(np.rint(Y_BLACK * (1.0 - small)), 0, 255).astype(np.uint8)
            base_uv = np.clip(np.rint(UV_BLACK * (1.0 - small)), 0, 255).astype(np.uint8)
            h, w = self.height, self.width
            # The chroma rows cover the same picture as the luma rows, at half
            # the height, so the same map scaled to their size fits them.
            cv2.resize(gain8, (w, h), dst=self._gain8[:h], interpolation=cv2.INTER_LINEAR)
            cv2.resize(gain8, (w, h // 2), dst=self._gain8[h:], interpolation=cv2.INTER_LINEAR)
            cv2.resize(base_y, (w, h), dst=self._base8[:h], interpolation=cv2.INTER_LINEAR)
            cv2.resize(base_uv, (w, h // 2), dst=self._base8[h:], interpolation=cv2.INTER_LINEAR)
        else:
            np.multiply(self._radius, np.float32(f2), out=gain)
            np.add(gain, np.float32(1.0), out=gain)
            np.divide(np.float32(s), gain, out=gain)
        self._gain_for = key
        return gain


def bgra_to_nv12(bgra: np.ndarray) -> np.ndarray:
    """A BGRA (or BGRX) uint8 picture of even size, as a new NV12 frame."""
    height, width = int(bgra.shape[0]), int(bgra.shape[1])
    out = np.empty(nv12_shape(width, height), dtype=np.uint8)
    cy = 219.0 / 255.0
    cu = (224.0 / 255.0) / (2.0 * (1.0 - _KB))
    cv = (224.0 / 255.0) / (2.0 * (1.0 - _KR))
    # Columns: B, G, R, A, constant.
    matrix = np.array(
        [
            [_KB * cy, _KG * cy, _KR * cy, 0.0, Y_BLACK],
            [(1.0 - _KB) * cu, -_KG * cu, -_KR * cu, 0.0, UV_BLACK],
            [-_KB * cv, -_KG * cv, (1.0 - _KR) * cv, 0.0, UV_BLACK],
        ],
        dtype=np.float32,
    )
    src = np.ascontiguousarray(bgra)
    if cv2 is not None:
        yuv = cv2.transform(src, matrix)
        out[:height] = yuv[..., 0]
        uv = cv2.resize(np.ascontiguousarray(yuv[..., 1:]), (width // 2, height // 2), interpolation=cv2.INTER_AREA)
        out[height:] = uv.reshape(height // 2, width)
        return out
    pixels = src.astype(np.float32)
    yuv = pixels @ matrix[:, :4].T + matrix[:, 4]
    out[:height] = np.clip(np.rint(yuv[..., 0]), 0, 255)
    uv = yuv[..., 1:].reshape(height // 2, 2, width // 2, 2, 2).mean(axis=(1, 3))
    out[height:] = np.clip(np.rint(uv), 0, 255).astype(np.uint8).reshape(height // 2, width)
    return out


def nv12_to_bgra(frame: np.ndarray, out_width: int, out_height: int) -> np.ndarray:
    """A small BGRA copy of an NV12 frame, for the monitors and the iPad remote.

    The picture is shrunk first, so the colour maths only touches the small one.
    """
    width, height = frame_size(frame)
    out_width = max(2, out_width)
    out_height = max(2, out_height)
    luma = frame[:height]
    chroma = frame[height:].reshape(height // 2, width // 2, 2)
    if cv2 is not None:
        y = cv2.resize(luma, (out_width, out_height), interpolation=cv2.INTER_AREA)
        uv = cv2.resize(chroma, (out_width, out_height), interpolation=cv2.INTER_AREA)
    else:
        rows = (np.arange(out_height) * height) // out_height
        cols = (np.arange(out_width) * width) // out_width
        y = luma[rows][:, cols]
        uv = chroma[rows // 2][:, cols // 2]
    yf = (y.astype(np.float32) - Y_BLACK) * (255.0 / 219.0)
    u = (uv[..., 0].astype(np.float32) - UV_BLACK) * (255.0 / 224.0)
    v = (uv[..., 1].astype(np.float32) - UV_BLACK) * (255.0 / 224.0)
    red = yf + 2.0 * (1.0 - _KR) * v
    blue = yf + 2.0 * (1.0 - _KB) * u
    green = (yf - _KR * red - _KB * blue) / _KG
    out = np.empty((out_height, out_width, 4), dtype=np.uint8)
    out[..., 0] = np.clip(blue + 0.5, 0, 255)
    out[..., 1] = np.clip(green + 0.5, 0, 255)
    out[..., 2] = np.clip(red + 0.5, 0, 255)
    out[..., 3] = 255
    return out
