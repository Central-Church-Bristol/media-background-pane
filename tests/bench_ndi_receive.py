"""End-to-end check through real NDI: what a receiver such as ProPresenter sees.

Finds an NDI source in the private group "mbp-bench" whose name contains
"MBP bench" (by default), receives its video, and records when each frame
arrives, the sender's timestamp, and the frame numbers drawn into the test
clips (see make_bench_clips.py). It writes the raw records (.npz) and a short
summary (.json): arrival intervals, and how each clip advanced from one
received frame to the next.

bench_pipeline.py starts this itself with --ndi real --receiver, one receiver
per scenario, and cuts the records to the measured window. It can also be run
by hand next to a bench run:

    python -B tests\\bench_ndi_receive.py --seconds 70 --out "%TEMP%\\mbp-bench\\recv.json"

It never looks outside the group "mbp-bench", so it cannot connect to the live
pane or to ProPresenter's sources.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import make_bench_clips as pattern  # noqa: E402

NDI_GROUP = "mbp-bench"
NDI_PREFIX = "MBP bench"


def _fourcc_name(ndi, value) -> str:
    for name in dir(ndi):
        if name.startswith("FOURCC_VIDEO_TYPE_") and getattr(ndi, name) == value:
            return name.replace("FOURCC_VIDEO_TYPE_", "")
    return str(value)


class FrameSampler:
    """Samples luma at the pattern points from a received NDI video frame."""

    def __init__(self) -> None:
        self._cache: dict = {}

    def sample(self, fourcc: str, xres: int, yres: int, stride: int, data: np.ndarray) -> np.ndarray | None:
        flat = np.ascontiguousarray(data).reshape(-1)
        if fourcc in ("UYVY", "UYVA"):
            step, offset = 2, 1
        elif fourcc in ("BGRA", "BGRX", "RGBA", "RGBX"):
            step, offset = 4, 1
        elif fourcc in ("NV12", "I420", "YV12"):
            step, offset = 1, 0
        else:
            return None
        if stride <= 0:
            stride = xres * step
        if flat.size < stride * yres:
            return None
        rows = flat[: stride * yres].reshape(yres, stride)
        key = (fourcc, xres, yres, stride)
        if key not in self._cache:
            ys, xs = pattern.sample_points(xres, yres)
            self._cache[key] = (ys, xs * step + offset)
        ys, cols = self._cache[key]
        return rows[ys, cols]


def receive(source_part: str, seconds: float, wait: float, group: str, ready: Path | None = None, quiet_stop: float = 5.0) -> dict:
    """Receive for up to `seconds` after connecting; stop early once frames have
    stopped coming for `quiet_stop` seconds (the sender has closed)."""
    import NDIlib as ndi

    if not ndi.initialize():
        raise SystemExit("NDI initialize failed")
    finder_settings = ndi.FindCreate()
    finder_settings.groups = group
    finder_settings.show_local_sources = True
    finder = ndi.find_create_v2(finder_settings)
    if finder is None:
        raise SystemExit("NDI find_create failed")
    source = None
    deadline = time.perf_counter() + wait
    while time.perf_counter() < deadline and source is None:
        ndi.find_wait_for_sources(finder, 500)
        for s in ndi.find_get_current_sources(finder):
            name = s.ndi_name or ""
            if source_part in name and NDI_PREFIX in name:
                source = s
                break
    if source is None:
        ndi.find_destroy(finder)
        raise SystemExit(f"no NDI source containing {source_part!r} in group {group!r} within {wait:g} s")
    source_name = source.ndi_name
    recv_settings = ndi.RecvCreateV3()
    recv_settings.source_to_connect_to = source
    recv_settings.color_format = ndi.RECV_COLOR_FORMAT_UYVY_BGRA
    recv_settings.bandwidth = ndi.RECV_BANDWIDTH_HIGHEST
    recv_settings.allow_video_fields = False
    recv_settings.ndi_recv_name = f"{NDI_PREFIX} receiver"
    recv = ndi.recv_create_v3(recv_settings)
    if recv is None:
        ndi.find_destroy(finder)
        raise SystemExit("NDI recv_create failed")
    ndi.recv_connect(recv, source)
    ndi.find_destroy(finder)
    if ready is not None:
        ready.write_text(source_name, encoding="utf-8")
    sampler = FrameSampler()
    cap = int(seconds * 120) + 1000
    arrival = np.zeros(cap, np.float64)
    stamp = np.zeros(cap, np.int64)
    samples = np.zeros((cap, pattern.SAMPLES_PER_FRAME), np.uint8)
    fourccs: dict[str, int] = {}
    n = 0
    started = time.perf_counter()
    end = started + seconds
    last = started
    while time.perf_counter() < end and n < cap:
        if n and time.perf_counter() - last > quiet_stop:
            break
        kind, video, _audio, _meta = ndi.recv_capture_v2(recv, 100)
        if kind == ndi.FRAME_TYPE_VIDEO:
            now = time.perf_counter()
            last = now
            try:
                fourcc = _fourcc_name(ndi, video.FourCC)
                fourccs[fourcc] = fourccs.get(fourcc, 0) + 1
                got = sampler.sample(fourcc, int(video.xres), int(video.yres), int(video.line_stride_in_bytes), video.data)
                if got is not None:
                    samples[n] = got
                arrival[n] = now
                stamp[n] = int(video.timestamp)
                n += 1
            finally:
                ndi.recv_free_video_v2(recv, video)
        elif kind == ndi.FRAME_TYPE_ERROR:
            break
    ndi.recv_destroy(recv)
    ndi.destroy()
    return {
        "source": source_name,
        "arrival": arrival[:n],
        "stamp": stamp[:n],
        "samples": samples[:n],
        "fourccs": fourccs,
        "started": started,
    }


def summarise(rec: dict, t0: float, t1: float, fps: int, clips: dict) -> dict:
    """Arrival intervals and per-clip advance for frames received between t0 and t1 (perf_counter)."""
    import bench_pipeline as bench

    t = rec["arrival"]
    sel = (t >= t0) & (t <= t1)
    out: dict = {"frames": int(sel.sum()), "fourccs": rec.get("fourccs"), "source": rec.get("source")}
    if out["frames"] < 2:
        return out
    t = t[sel]
    out["intervals"] = bench.interval_stats(t, 1000.0 / fps)
    stamps = rec["stamp"][sel]
    if np.all(stamps > 0):
        out["sender_timestamp_intervals"] = bench.interval_stats(stamps / 1e7, 1000.0 / fps)
    dec = pattern.decode_samples(rec["samples"][sel])
    n = len(t)
    nan = np.full(n, np.nan, np.float32)
    zeros = np.zeros(n, np.int8)
    black = dec["grid"].max(axis=1) <= 18
    clip_results = {}
    for name, info in clips.items():
        weight = np.ones(n, np.float32)
        clip_results[name] = bench.analyse_clip(name, info, dec, t, t0, fps, zeros, zeros, weight, nan, black, [])
        clip_results[name].pop("by_bus", None)
    out["clips"] = clip_results
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", default=NDI_PREFIX, help="part of the source name to connect to")
    parser.add_argument("--group", default=NDI_GROUP)
    parser.add_argument("--seconds", type=float, default=70.0)
    parser.add_argument("--wait", type=float, default=20.0, help="seconds to wait for the source to appear")
    parser.add_argument("--fps", type=int, default=24)
    parser.add_argument("--media", type=Path, default=pattern.DEFAULT_MEDIA)
    parser.add_argument("--out", type=Path, required=True, help="summary .json; the raw records go next to it as .npz")
    parser.add_argument("--ready", type=Path, help="file to create once connected (used by bench_pipeline.py)")
    args = parser.parse_args()
    if args.group != NDI_GROUP:
        raise SystemExit(f"only the private group {NDI_GROUP!r} is allowed")
    if args.ready:
        args.ready.unlink(missing_ok=True)
    rec = receive(args.source, args.seconds, args.wait, args.group, ready=args.ready)
    np.savez_compressed(
        args.out.with_suffix(".npz"),
        arrival=rec["arrival"],
        stamp=rec["stamp"],
        samples=rec["samples"],
    )
    try:
        manifest = pattern.load_manifest(args.media)
        clips = {k: v for k, v in manifest["clips"].items() if k in ("A", "B")}
    except OSError:
        clips = {}
    t = rec["arrival"]
    summary = summarise(rec, t[0] if len(t) else 0, t[-1] if len(t) else 0, args.fps, clips) if len(t) else {"frames": 0}
    summary["raw"] = str(args.out.with_suffix(".npz"))
    args.out.write_text(json.dumps(summary, indent=1, default=float), encoding="utf-8")
    iv = summary.get("intervals") or {}
    print(f"{summary.get('frames', 0)} frames from {rec['source']}; formats {rec['fourccs']}")
    if iv:
        print(f"arrival intervals ms: mean {iv['mean']:.1f} sd {iv['sd']:.1f} p1 {iv['p1']:.1f} p50 {iv['p50']:.1f} p99 {iv['p99']:.1f} max {iv['max']:.1f}")
    for name, c in (summary.get("clips") or {}).items():
        s = c["share"]
        if c["pairs"]:
            print(f"clip {name}: next {s['next']:.1%} repeat {s['repeat']:.1%} skip {s['skip']:.1%} ({c['pairs']} pairs)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
