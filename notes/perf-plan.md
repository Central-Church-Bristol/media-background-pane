# Faster, smoother playback at 1080p: plan (5 Nov 2026)

The user's report: about 20% CPU while playing, still not smooth at times, and worst while MIX is between 0 and 1 (two clips blended). It only manages 540p. vMix on the same PC blended two 1080p clips smoothly. The user is happy for the pipeline to be rebuilt, as long as the pane does the same things at the end.

This file is the shared plan for the sessions doing the work. Each session works in its own worktree, cut from branch `worktree-perf-1080p`.

## The PC and the media

- AMD Ryzen 7 3700X: 8 cores, 16 threads. So 20% CPU is about 3 threads busy.
- NVIDIA GeForce GTX 1660, 32 GB RAM, Windows 10. Two 1920x1080 60 Hz screens.
- Library: `G:\My Drive\Video Projection\Evening Service\Media Backgrounds` (Google Drive).
  - All clips but one are HEVC Main, 1920x1080, 24 fps, about 41 Mb/s, BT.709, limited range.
  - Light Tunnel is H.264 1080p30. Lamb.png is a still.
- Settings: 24 fps, quality 540p, GPU decode on, Scale to Fill.
- Python 3.14.6, PySide6 6.11.2, numpy 2.5.2, ndi-python 6.3.2.3, ffmpeg 9.0.2 (gyan essentials).
- `opencv-python-headless` 5.0.0.93 was added to the venv on 5 Nov, with `--no-deps`; numpy is unchanged. The running pane does not import it.

## What costs the time today

Measured on 5 Nov with Holy Fire (HEVC 1080p24), outside the running pane. The scripts are in the director session's scratchpad.

**Pixel work per frame.** One thread unless noted. 24 fps leaves 41.7 ms per frame.

| Work | 540p today | 1080p today | 1080p NV12, OpenCV |
| --- | --- | --- | --- |
| Blend at MIX 0.5 | 12 ms | 52 ms | 0.7 ms (`cv2.addWeighted`) |
| Vignette dim | 29 ms | 127 ms | about 5 ms, plus the gain map |
| Blend and dim (a fade under DIM) | **42 ms: over budget** | 182 ms | about 6 ms |
| Copy one frame | 0.1 ms | 0.5 ms | 0.17 ms |

- Plain numpy on NV12 is a usable fallback: 11 ms per 1080p blend.
- Making the 160x90 monitor image from NV12 takes about 1.2 ms.

**ffmpeg and the reader, per frame.** Decoding as fast as the reader takes frames.

| Set-up | ffmpeg CPU | Python CPU | Reads per frame |
| --- | --- | --- | --- |
| Today, GPU, 540p BGRA, Lanczos on the CPU | 12.5 ms | 1.0 ms | 64 |
| Today, GPU, 1080p BGRA | 21.7 ms | 5.9 ms | 254 |
| D3D11VA, 1080p NV12, no scaling | 7.0–7.8 ms | 1.2–1.9 ms | 95 |
| CUDA, 1080p NV12, no scaling | 5.9–6.1 ms | 1.5–2.0 ms | 95 |
| D3D11VA, then swscale bicubic to 540p NV12 | 8.9 ms | ~0 | 24 |
| Software decode, 1080p NV12 | 46 ms | 1.6 ms | 95 |

- At the same size, `scale=W:H:force_original_aspect_ratio=increase,crop=W:H` costs nothing extra: ffmpeg passes the frame through. So no probing is needed for 1080p clips at 1080p.
- `scale_cuda` took 6 s to start, the first time at least. Don't use it.
- `scale_d3d11` fails with EINVAL in both forms tried. So the "gpu-d3d11" mode never works on this PC, and every clip falls back to the plain "gpu" mode.

**NDI, 1080p24, one receiver connected.** The whole process's CPU:

| Format | CPU per frame | Share of one core |
| --- | --- | --- |
| NV12 | 6.6 ms | 16% |
| UYVY | 5.8 ms | 14% |
| BGRX | 4.3 ms | 10% |

- The NV12 send call itself takes 0.5 ms; UYVY and BGRX take 0.01 ms.
- **Crash found:** the ndi-python `VideoFrameV2.data` setter takes `yres`, `xres` and `line_stride_in_bytes` from the array's shape. An NV12 array has 1.5× as many rows as the picture is tall, so NDI read past the end of the buffer and the process crashed. Set `xres`, `yres` and `line_stride_in_bytes` again after every `data =`.

## Why it stutters

1. **The blend and the dim are float and uint16 numpy on BGRA, with big temporaries.** At 540p, a fade with both MIX and DIM moving takes longer than one frame. At 1080p even MIX alone does.
2. **Two clocks.** Each decoder paces itself with its own `sleep_until` clock (added 2 Nov). The compositor ticks on another clock and takes whatever is in a 2-deep drop-oldest queue. The two drift in phase, so some ticks get no new frame (a frame shown twice) and some get two (one dropped). Blending two clips doubles the chance.
3. **Wasted ffmpeg work.**
   - Each frame is downloaded, scaled with Lanczos and converted to BGRA on the CPU.
   - Each frame is copied about six times in Python: bytearray, `bytes()`, `.copy()`, the queue, the bus, the NDI buffer. A fresh 8 MB buffer is allocated per frame.
   - At 1080p it takes 254 pipe reads per frame.
   - `fps=` comes last in the filter chain, so frames that `fps` drops are still scaled.
4. **Busy-waits.** `timing.sleep_until` spins for the last 1–2 ms of every wait, holding the GIL. It runs in three threads.
5. **Smaller issues:**
   - The NDI timecode is 0 on every frame; it should be synthesised.
   - `NdiSender.set_size` swaps buffers while NDI may still be reading the old one.
   - Stills are scaled with `FastTransformation`.
   - The Video Input status poll is HTTP on the GUI thread, once a second, with a 0.8 s timeout. A slow ProPresenter can stall the MIX/DIM knob animation, which runs on the GUI thread.

## The new pipeline

Keep the structure: Bus, Engine, HwDecoder, NdiSender and the watchdog. Change what moves through it, and who owns the clock.

### Frames are NV12 everywhere

- An `np.uint8` C-contiguous array of shape `(height * 3 // 2, width)`.
  - Rows `[0:height]` are Y.
  - Rows `[height:]` are interleaved U, V at half resolution.
- Values are limited range ("tv": Y 16–235, U/V 16–240).
- Colour matrix: BT.709 when the output height is 720 or more. Below that, use whatever NDI assumes; check what it assumes and write it down. ffmpeg converts a clip whose tags differ, for example `yuvj` or full range.
- Black is Y 16, U/V 128.

### Decoder (`hw_decoder.py`)

**The ffmpeg command:**
- `-hwaccel d3d11va`, leaving frames to download as NV12. CUDA is optional: it is a little cheaper and starts faster, but only works on NVIDIA.
- `-vf fps={fps},{scale chain},format=nv12`, with `fps` first.
  - Fill: `scale=W:H:force_original_aspect_ratio=increase:force_divisible_by=2:flags=bicubic,crop=W:H`
  - Fit: `...decrease...,pad=W:H:(ow-iw)/2:(oh-ih)/2:black`
  - Actual: the existing `_crop_pad`.
- Output: `-f rawvideo -pix_fmt nv12 pipe:1`.
- Drop the gpu-d3d11 mode, unless `scale_d3d11` can be made to work quickly.
- Fallback order: gpu, then software.

**Reading:**
- Use a pool of preallocated frame buffers and `readinto` a free one.
- No `bytes`, `bytearray` or `.copy()` per frame.

**Queue and clock:**
- An in-order queue about half a second deep (`max(4, fps // 2)` frames).
- When the queue is full, the reader waits for a free buffer. The pipe then fills and ffmpeg waits too. That wait is the pacing.
- No clock in the decoder. The compositor takes one frame per output tick, so it alone sets playback speed.

**Methods and counters:**
- `pop_frame(dest, timeout) -> bool`: copies the next frame in order into `dest`, and the buffer goes back to the pool.
- `copy_frame(dest)` and `discard_queued()` stay, but nothing that needs continuity may use them.
- `frames_read`, `thread_alive()`, `paused`, `status`, and the `status_changed` and `frame_ready` signals stay. The watchdog and Bus use them.

**Pausing:**
- Paused means nobody is taking frames.
- Keep the queue, so playing again continues with the very next frame.
- Draining on pause would skip up to half a second.

**Stall rule:**
- Kill and reopen ffmpeg only when the reader has waited more than 8 s for ffmpeg while the queue had room.
- Time spent waiting for the compositor, or paused, never counts.
- Keep the 3 s first-frame rule and the retry loop.

**Why waiting is safe again:** a full queue that blocks the reader was the 1 Nov freeze. It is safe now because the watchdog restarts a dead compositor within about a second (3 Nov), so a missing consumer is temporary.

### Bus (`bus.py`)

- `frame_np` is the displayed frame, in NV12.
- `take_frame` pops the next frame into `frame_np` and `dest`. The alpha writes go.
- **First frame:** when a clip loads paused after its first frame, pop that first frame from the queue rather than copying the latest. The queue then continues at frame 2. With a deep queue, "latest" may be frame 12.
- **Swap and adopt:** don't discard queued frames. The queue is the continuation of what is on screen. Re-check whether `_skip_frames` is still needed.
- **Stills:** load the still, then `place_rgb` with smooth scaling, then convert to NV12 once.
- **Qt fallback:** convert each frame to NV12 with the same helper.

### Engine (`engine.py`)

**Blend and dim:**
- Blend with `cv2.addWeighted` on the whole NV12 buffer. In limited-range YUV this equals blending in R'G'B'. Keep a numpy fallback for when cv2 is missing.
- The vignette dim keeps exactly the same look. Today's maths is `out = sqrt(rgb² · shade · tail)`, which is `rgb · g` with `g = sqrt(tail) / (1 + f² · P)`. Here `f` is `vig · strength` and `P = r2^(radius/2)`, which can be precomputed per size.
  - In NV12: `Y' = 16 + (Y−16)·g` at full resolution, and `UV' = 128 + (UV−128)·g` at half resolution.
- The thresholds don't change: DIM ≤ 0.001 is black, MIX ≤ 0.001 is Program only, ≥ 0.999 is Preview only.

**Compose loop: the only clock.**
- One tick per 1/fps. Pop one Program frame and, while mixing, one Preview frame, as today.
- **Waiting:** wait with `time.sleep`. On Windows, Python 3.11+ uses a high-resolution timer for it. Remove the spin from `sleep_until`, or stop using it here. Measure how late the waits wake.
- **Thread priority:** run the compose thread above normal, for example `THREAD_PRIORITY_TIME_CRITICAL`. It uses very little CPU, and its timing is the output.
- **Optional:** `sys.setswitchinterval(0.001)`, so the compose thread gets the GIL soon after it wakes.
- **Counters:** count ticks where a playing bus had no new frame. Log them at most once a minute, for example `compose: Program had no new frame 3 times in the last 60 s`, so a real service leaves evidence.

**Monitors and quality changes:**
- The monitor images (160x90 at 5 fps) come from NV12: shrink Y and UV with cv2, then convert the tiny image with BT.709.
- `set_quality` reallocates the NV12 buffers and vignette maps, and tells NDI the new size.

### NDI (`ndi_output.py`)

- FourCC NV12. After each `frame.data = buf`, set `xres`, `yres` and `line_stride_in_bytes = width` again.
- Set `timecode = ndi.SEND_TIMECODE_SYNTHESIZE`.
- Async send, double-buffered as today. `set_size` first flushes with `send_send_video_async_v2(send, None)`, then replaces the buffers.
- Keep the interface the benchmark relies on:
  - the constructor `NdiSender(name, fps, width, height)`
  - `submit_array(array)`, `set_fps(fps)`, `set_size(w, h)`, `close()`, `connection_count()`
  - the `status` and `available` attributes.

### Settings

- The default quality becomes 1080p. `DEFAULTS_REVISION` 3 moves a saved 540p to 1080p once. The user wants 1080p, and Settings can still set it back.
- `requirements.txt` gains `opencv-python-headless`.

## Who does what

The user is on Remote Control, and side sessions can't be started from there. So everything happens in the director's worktree (`.claude\worktrees\perf-1080p`, branch `worktree-perf-1080p`). Helper agents work on files that don't overlap, and only the director commits.

| Part | Who | Files it owns |
| --- | --- | --- |
| A. Pipeline rebuild | director | `hw_decoder.py`, `bus.py`, `engine.py`, `ndi_output.py`, `timing.py`, `config.py`, `log.py`, `requirements.txt`, README's quality wording, tests for these, `notes/perf-pipeline.md` |
| B. Measuring kit and before numbers | helper agent | new files only: `tests/bench_*.py`, `tests/make_bench_clips.py`, `notes/perf-baseline.md` |
| C. GUI-thread stalls | helper agent | `main_window.py`, `propresenter.py` (and `library.py` if needed), new tests, `notes/gui-thread.md` |
| Integration, after numbers, report | director | commits on `worktree-perf-1080p` |

Anyone who needs a file they don't own keeps the change minimal and says so in their report.

## Found on 5 Nov: the live pane's memory looks corrupted again

**What the live pane (pid 22024, started 23:20 on 4 Nov) logs:**
- Since at least 15:43, its watchdog has failed every few seconds with `AssertionError` "Thread.__init__() not called", from `thread.is_alive()` on the compose thread.
- The final line of each traceback reads `None: ...`. Python's `traceback` module prints that only when the exception's type is None, which a working interpreter can't produce.
- `py-spy dump` shows the compose thread alive in `sleep_until`. So playback runs, but the watchdog can't check it.

**What it costs:**
- The repeated traceback (about 1.2 KB every 5 s) fills `session.log` and its one rotation in under an hour. That pushed out the restart lines from the night before.

**The pattern:**
- This follows the impossible errors of 4 Nov: `isdir` false on an existing folder, and `re.compile` rejecting a fixed string.
- It may also explain the threads that ended silently on 31 Oct and 2 Nov.
- Suspected: a native component corrupting Python's memory. Not found yet.
- Ruled out so far:
  - the ndi-python `data` setter, which keeps exactly one reference to the current array
  - `_publish_decoder_frame`, the long-lived QImage over numpy memory, which is never called

**What to do in the rebuild:**
- Keep the buffers that native code reads strictly alive.
- Don't delete QObjects while another thread can still emit their signals. Today `Bus._stop_decoder` calls `deleteLater()` from a reaper thread, even when the decoder thread has not finished in 2 s.
- Make repeated identical errors collapse in the log.

## Done means

**Smoothness**, at 1080p24, blending two real library clips at MIX 0.5 for 10 minutes:
- Every output tick has a new Program frame and a new Preview frame. The only exception is at most one at a clip's loop point.
- Output frame intervals stay within ±4 ms of 41.7 ms at the 99th percentile.

**CPU**, the pane plus its ffmpeg processes:
- At most 1 core in total; the target is about 0.6.
- Compose at most 15 ms per tick during a DIM fade.

**Also:**
- The same results at 720p and 540p.
- Blend and dim match the old maths within 40 dB PSNR, compared in RGB.
- All tests pass, or are updated with the same intent. New tests cover backpressure, the stall rule, the first frame, and continuity across Take.
- `tests/check_playback_speed.py`, updated, shows 1.00× in GPU and software modes.
- Behaviour stays the same:
  - Take, Fade, Fade to Black and Take under black
  - the Preview first frame
  - stills, Fill, Fit and Actual Size
  - the fps and quality settings
  - the watchdog's repairs and the session log
  - the LAN remote

## Rules for every session

**Leave the live setup alone:**
- Don't touch the running pane (pythonw.exe running `main.py` from `C:\Users\avbri\Documents\Media Background Pane`).
- Don't touch the main folder's files, and don't open ProPresenter.
- Don't run `main.py`; it would take the single-instance lock.
- Don't use port 8745.

**Tools:**
- Python: `C:\Users\avbri\Documents\Media Background Pane\.venv\Scripts\python.exe`. Don't install or upgrade packages in it; ask the director.
- ffmpeg is git-ignored, so copy `vendor\ffmpeg\ffmpeg.exe` and `testsrc.mp4` from the main folder into your worktree's `vendor\ffmpeg\`.

**NDI tests:**
- Only in the private group `mbp-bench`, with source names starting `MBP bench`.
- Never use the name "Media Background Pane".

**Git:**
- Commit on your own branch with `git -c user.name="Jonah Ling" -c user.email="147850413+Jonah-Ling@users.noreply.github.com" commit ...`, ending the message with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Don't push, merge or rebase onto other branches.
