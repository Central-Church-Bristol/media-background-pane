# Smooth 1080p playback: the rebuilt pipeline (5 Nov 2026)

Branch `worktree-perf-1080p`. Nothing is in the main folder yet, so the running pane still has the old code. `perf-plan.md` has the plan and the measurements that led to it. `gui-thread.md` covers the GUI-thread half of the work.

## What was wrong

The user saw about 20% CPU (three of the Ryzen's 16 threads), choppy playback at times, worst while MIX was between 0 and 1, and only 540p.

1. **Mixing took longer than a frame.** The blend and the vignette dim were float maths on 4-byte BGRA pixels.
   - At 540p a fade with both MIX and DIM moving took 42 ms per frame. 24 fps leaves 41.7 ms.
   - At 1080p, MIX alone took 52 ms and the dim 127 ms.
2. **Two clocks fought.** Each decoder timed its own reads, the compositor ticked on its own clock, and a 2-frame queue dropped the oldest frame. The clocks drifted against each other, so a frame was shown twice here and skipped there. Two clips meant twice the chance.
3. **ffmpeg and Python did needless work.**
   - Every frame was scaled with Lanczos and turned into BGRA on the CPU.
   - Python copied each frame about six times and allocated a fresh 8 MB for each.
4. **Busy-waits:** three threads spun for the last 1–2 ms of every wait, holding the GIL.

## What changed

- **One format, NV12, from ffmpeg to NDI.** NV12 is what the GPU decodes to (1.5 bytes a pixel). Nothing converts colour on the way, and a 1080p clip at 1080p is not scaled at all. Colours are BT.709, which NDI assumes at every size; this was checked by sending colours through NDI and reading them back.
- **One clock.**
  - The compositor takes exactly one frame from each playing clip per tick.
  - The decoder keeps half a second decoded ahead, in order, and simply waits when that is full.
  - So nothing repeats or skips while the decoder keeps up, and a clip plays at exactly its speed.
- **NVDEC first.** On this NVIDIA card it decodes through CUDA. Two 1080p clips cost 13% of a core in ffmpeg, against 87% through D3D11VA. Other cards still get D3D11VA, then software.
- **Mixing with OpenCV.**
  - A 1080p blend takes 0.7 ms, where it used to take 52 ms.
  - The dim looks the same: the old maths turned out to be a per-pixel gain. During a fade it takes about 4 ms (old: 127 ms), and about 1.3 ms when held.
  - Against the old maths it scores 45–48 dB PSNR, which is not visible.
- **Frames go out on the beat.** The frame mixed on one tick is sent at the start of the next. So the moment it leaves no longer depends on how long mixing took, at the cost of one frame (42 ms) of delay.
- **The window thread no longer blocks fades.** ProPresenter's API and the Drive folder scans moved to their own threads (see `gui-thread.md`). While ProPresenter was starting, its API had blocked the window thread for about 2.4 s of every second.
- **Safety:**
  - A decoder is deleted only after its thread has ended. Before, it could be deleted while that thread was still sending it a signal.
  - The NDI sender finishes with a buffer before replacing it.
  - NDI frames carry real timecodes instead of 0.
  - The watchdog keeps checking when one of its checks fails.
  - A repeating error backs off in the log instead of filling it.
- **1080p is the default.** A saved 540p moves to 1080p once; Settings can still set it back.

## Measured (5 Nov 2026)

These ran while the PC was busy with other tests, so the CPU numbers are on the high side.

**Real ffmpeg, Holy Fire (HEVC 1080p24), `tests/check_playback_speed.py`:**

| Output | Speed | Ticks with no new frame | Frames skipped | ffmpeg CPU |
| --- | --- | --- | --- | --- |
| 1080p, GPU | 1.00× | 0 of 193 | 0 | 0.15 cores |
| 720p, GPU | 1.00× | 0 of 193 | 0 | 0.12 cores |
| 540p, GPU | 1.00× | 0 of 192 | 0 | 0.09 cores |
| 1080p, software decode | 1.00× | 0 of 193 | 0 | 0.72 cores |

**The real Engine, headless:** Holy Fire on Program and Leaves on Preview, 1080p 24 fps, NDI replaced by a recorder.

| Scenario | Frames | Interval p1 / p50 / p99 / max | Ticks with no new frame |
| --- | --- | --- | --- |
| MIX 0.5, 60 s | 1440 (24.00/s) | 40.9 / 41.6 / 42.6 / 42.8 ms | 0 |
| MIX 0.5, DIM moving all the time, 30 s | 720 | 40.9 / 41.6 / 42.6 / 42.7 ms | 0 |
| Program only, 30 s | 720 | 40.9 / 41.6 / 42.5 / 42.6 ms | 0 |

- CPU on a quiet PC, MIX 0.5: the pane process 7.4% of one core (the compositor 3.3%), plus ffmpeg ×2 35%. That is about 0.4 of a core in all, under 3% of the PC.
- **Looping:** Light Tunnel (10 s, 30 fps) and Liquid Christ (1 min) looped many times during the same scenarios, with 0 ticks short of a frame.

**Through real NDI** (private group `mbp-bench`, a still of RGB 200, 50, 100):
- 1080p arrived as 1920×1080 with Y 89, U 135, V 192: exactly BT.709. A receiver shows RGB 200, 49, 101.
- 540p arrived the same.
- Frames arrived 41.8 ms apart (p50), 43.5 ms at p99.

**`tests/check_watchdog.py` at 1080p** passes all six cases:
- a stopped compositor costs at most 0.7 s of still picture;
- a dead decoder costs 0.33 s;
- a hung ffmpeg is restarted after 9.2 s, by its 8-second rule;
- ordinary use causes no false repairs.

**Tests:** 127 pass.

## After switching to the new version

1. **ProPresenter:** Settings → Inputs → Video Inputs. Set the Media Background Pane input to 1920×1080, 24 fps, to match the new Quality.
2. **The Settings dialog** should show Quality "High — 1080p" and the decoder as "GPU (NVDEC)".
3. **During or after a service, see what the pane logged about playback:**

   ```powershell
   Select-String -Path "$env:LOCALAPPDATA\Media Background Pane\session.log" -Pattern "compose:|gui:|watchdog:|decoder" | Select-Object -Last 30
   ```

   - **No `compose: no new frame ready in time` lines** means every frame was on time.
   - **`gui: the window thread was blocked for ...`** means a fade was held up, and the line says by what.
4. **Task Manager:** with two clips blending, the pane and its two ffmpeg processes together should sit around 3% CPU.

## Not tested

- **With ProPresenter:** it was not opened, by agreement. How ProPresenter shows a 1080p NDI input is to be seen.
- **A whole service:** the soak test (`tests/soak_pipeline.py`) runs the pipeline the way an operator does, for as long as asked.
- **The iPad remote:** it uses the same monitor images as before, and it was not tried.
