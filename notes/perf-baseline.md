# Before numbers: today's pipeline (5 Nov 2026)

What today's code does with the PC's time, measured on 5 Nov 2026 between 17:38 and 18:36 with the new measuring kit. These are the numbers the rebuild in `perf-plan.md` has to beat.

- The code measured is a frozen copy of today's package at `%TEMP%\mbp-bench\baseline`. It is identical to commit 306194f, the code the live pane runs.
- The live pane was left alone.
- The kit: `tests\bench_pipeline.py`, `tests\make_bench_clips.py` and `tests\bench_ndi_receive.py` (see "How to run the kit").

## In short

- **540p, today's setting, plays smoothly while nothing moves.** With one clip on Program, or two blended at MIX 0.5, every output frame showed each clip's next frame. Frames went out 41.7 ms apart, p99 42.0 and 45.4 ms. In 10 minutes at MIX 0.5 there was one repeat, no skip, and all 51 loop points were clean.
- **It costs more CPU than the picture needs.** One clip at 540p uses 0.8 of a core in all (Task Manager: 4.9%); two blended use 2.1 cores (13.1%). Of the 2.1 cores, the compose thread, which does the blend, uses only 0.43. The two decoder reader threads take 0.79 and ffmpeg 0.85. The readers' cost is in how they read the pipe (see "Reading frames from ffmpeg").
- **A fade under DIM does not fit at 540p.** The blend and the vignette together take 55–60 ms a frame against 41.7. Output drops to 17–19 frames a second, and the clips skip frames.
- **A DIM fade alone only just fits at 540p.** The median call is 33 ms and p99 42 ms, with a few late frames (p99 interval 48.5 ms).
- **Every Take holds a frame and then jumps.** The frame pair across the Take is a repeat, often followed by a second, and at 540p Program then usually skips one. The first repeat comes from `take()` composing an extra frame on the GUI thread.
- **720p already repeats.** The reader gets 19.7 frames a second with one clip and 8 per clip with two.
- **1080p does not play.**
  - The reader threads deliver 4 new frames a second with one clip, and 1.9 per clip with two blended. So the clips play in slow motion, at 1/6 and 1/12 of real speed, each frame shown 6 to 12 times.
  - On top of that, compose at MIX 0.5 takes 95 ms a frame, so output falls to 10.6 frames a second. With DIM moving as well, it falls to 4.4.
  - CPU is still 1.0 to 2.6 cores, and ffmpeg only 0.1, because the slow readers hold ffmpeg back.
- **Leaving black blocks the GUI thread** for about 70 ms at 540p and 280 ms at 1080p.
- **The user's report fits what moves, not steady playback.** They saw choppy playback at times, worst while blending. Today, a fade with DIM below 1 drops to 17–19 frames a second, and every Take holds and then jumps. Under heavier load (the first pass), the 540p fade also picked up stray repeats. Steady playback, even blended, was clean.
- **The kit had to count CPU cycles.** Windows' sampled thread times put today's 540p compose thread at 0% and ffmpeg at about half its real CPU.

## Against the "done" list in perf-plan.md

| Done means | Target | Today (second pass) |
| --- | --- | --- |
| 1080p24, two clips at MIX 0.5: a new Program and a new Preview frame every tick | every tick, but one per loop point | 17.8% of frames on each bus are new and 82.2% are repeats. Each clip is read at 1.9 frames a second. |
| 1080p24 MIX 0.5: output intervals at the 99th percentile | 41.7 ± 4 ms | 109 ms. Mean 94.6 ms, so 10.6 frames a second. |
| CPU, the pane and its ffmpeg processes, 1080p MIX 0.5 | at most 1 core, about 0.6 hoped for | 2.56 cores (Task Manager 16%), with ffmpeg held back to 0.12. |
| Compose per frame during a DIM fade | at most 15 ms | About 150 ms at 1080p (p99 168 ms). At 540p, 33 ms median and 41.5 ms p99. |
| 540p, MIX 0.5 | the same as above | Smooth: no repeats or skips, p99 45.4 ms. But 2.09 cores. |
| 540p, fade under DIM (`fade_dimmed`) | the same as above | Not smooth: 17–19 frames a second, 27–40% of Program pairs and 55–87% of Preview pairs skip. 2.6 cores. |

## Conditions

- **PC:** Ryzen 7 3700X (8 cores, 16 threads), GTX 1660, Windows 10 19045.
- **Software:** Python 3.14.6, PySide6 6.11.2, numpy 2.5.2, ffmpeg 9.0.2 (the worktree's `vendor\ffmpeg\ffmpeg.exe`).
- **Settings, as in the pane:** 24 fps, GPU decode on, Scale to Fill, the 1 ms timer, below-normal priority. NDI is the recording stand-in, except in "Through real NDI".
- **Clips:** the bench clips A (on Program) and B (on Preview), HEVC 1080p24 at 41 Mb/s, read from `%TEMP%\mbp-bench\media`. The library clips are read in place from G: (Google Drive; already cached, 170–230 MB/s).
- **Other work on the PC:** other processes used 1.0–4.2 cores while the runs went on, about 2.5 on most runs. Most of it was other sessions' Python tests, about 1 core at a time (for example another project's watchdog test at 17:05). The live pane (pid 22024) used about 0.03 cores. DaVinci Resolve was open, at 0.76 cores at 17:10.
- **Timing:** each scenario ran 60 s, after 4 s of warm-up and 2 s to settle, each in a fresh process.
- **Two passes:** the main set was run twice. The second pass, in the tables, counts CPU by cycles; the first only by sampled thread times (see "What each number means"). The two passes agree on smoothness ("Run to run").

## The numbers

### 540p (today's setting)

Bench clips A (Program) and B (Preview), NDI stand-in, second pass.

| Quality | Scenario | Interval mean / sd | p1 / p50 / p99 / max | Late | Extra | Program next / rep / skip | Preview next / rep / skip | Across Take (pairs: rep, skip) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 540p | program | 41.7 / 0.1 | 41.4 / 41.7 / 42.0 / 42.2 | 0 | 0 | 100.0% / 0.0% / 0.0% | - | - |
| 540p | mix50 | 41.7 / 1.3 | 38.2 / 41.7 / 45.4 / 47.6 | 0 | 0 | 100.0% / 0.0% / 0.0% | 100.0% / 0.0% / 0.0% | - |
| 540p | fade | 41.6 / 2.6 | 34.2 / 41.7 / 46.7 / 61.2 | 0 | 4 | 99.6% / 0.2% / 0.2% | 100.0% / 0.0% / 0.0% | 4: 4, 0 |
| 540p | dim | 41.6 / 3.4 | 34.0 / 41.7 / 48.5 / 73.7 | 4 | 4 | 100.0% / 0.0% / 0.0% | - | - |
| 540p | take_black | 41.5 / 3.5 | 31.4 / 41.7 / 48.7 / 75.8 | 3 | 6 | 100.0% / 0.0% / 0.0% | - | - |
| 540p | fade_dimmed | 53.8 / 12.1 | 35.6 / 57.5 / 74.8 / 86.2 | 379 | 4 | 72.7% / 0.2% / 27.1% | 44.9% / 0.0% / 55.1% | 4: 4, 0 |

| Quality | Scenario | Process | Compose | Readers | GUI | Native | ffmpeg | Total | Task Manager | Compose ms per tick | Compose call p99 ms | Read fps (each playing clip) | Peak MB (process / ffmpeg) | Others busy |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 540p | program | 39% | 4% | 34% | 2% | 0% | 40% | 79% | 4.9% | 1.5 | 1.2 | 24.0 | 99 / 682 | 1.0 |
| 540p | mix50 | 124% | 43% | 79% | 3% | 0% | 85% | 209% | 13.1% | 17.8 | 22.0 | 24.0, 24.0 | 113 / 608 | 1.8 |
| 540p | fade | 114% | 31% | 80% | 4% | 0% | 75% | 189% | 11.8% | 12.7 | 26.0 | 19.8, 19.8 | 114 / 723 | 2.0 |
| 540p | dim | 93% | 54% | 36% | 3% | 0% | 36% | 129% | 8.1% | 22.6 | 41.5 | 19.8 | 126 / 572 | 1.6 |
| 540p | take_black | 91% | 54% | 34% | 3% | 0% | 36% | 127% | 7.9% | 22.5 | 41.9 | 9.9, 9.9 | 124 / 488 | 1.9 |
| 540p | fade_dimmed | 183% | 88% | 90% | 5% | 0% | 76% | 259% | 16.2% | 47.5 | 75.3 | 19.9, 19.8 | 136 / 490 | 2.4 |

### 720p

The same, Program only and MIX 0.5.

| Quality | Scenario | Interval mean / sd | p1 / p50 / p99 / max | Late | Extra | Program next / rep / skip | Preview next / rep / skip | Across Take (pairs: rep, skip) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 720p | program | 41.7 / 4.3 | 31.7 / 41.7 / 50.3 / 53.6 | 0 | 0 | 82.0% / 18.0% / 0.0% | - | - |
| 720p | mix50 | 41.7 / 6.6 | 27.9 / 43.2 / 55.2 / 58.5 | 0 | 0 | 33.8% / 66.2% / 0.0% | 33.5% / 66.5% / 0.0% | - |

| Quality | Scenario | Process | Compose | Readers | GUI | Native | ffmpeg | Total | Task Manager | Compose ms per tick | Compose call p99 ms | Read fps (each playing clip) | Peak MB (process / ffmpeg) | Others busy |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 720p | program | 98% | 5% | 91% | 3% | 0% | 44% | 142% | 8.9% | 1.9 | 11.8 | 19.7 | 122 / 739 | 1.8 |
| 720p | mix50 | 200% | 77% | 120% | 3% | 0% | 38% | 238% | 14.9% | 32.0 | 50.2 | 8.1, 8.0 | 146 / 499 | 2.0 |

What it shows:

- **720p is already past the readers' limit.** With one clip, the reader gets 19.7 frames a second, not 24, so 18% of output frames repeat. With two blended, each clip gets 8 a second, so two thirds repeat.
- **The output itself still keeps 24 frames a second at 720p.** Compose at MIX 0.5 is 32 ms a tick, and the intervals' p99 is 55 ms.

### 1080p

The same, at 1080p.

| Quality | Scenario | Interval mean / sd | p1 / p50 / p99 / max | Late | Extra | Program next / rep / skip | Preview next / rep / skip | Across Take (pairs: rep, skip) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1080p | program | 41.7 / 4.5 | 28.2 / 41.7 / 50.4 / 52.6 | 0 | 0 | 16.5% / 83.5% / 0.0% | - | - |
| 1080p | mix50 | 94.6 / 6.0 | 79.3 / 94.6 / 109.3 / 116.4 | 634 | 0 | 17.8% / 82.2% / 0.0% | 17.8% / 82.2% / 0.0% | - |
| 1080p | fade | 62.9 / 24.7 | 17.4 / 48.2 / 102.5 / 115.3 | 436 | 4 | 16.9% / 83.1% / 0.0% | 17.6% / 82.4% / 0.0% | 4: 4, 0 |
| 1080p | dim | 78.6 / 52.5 | 30.7 / 41.7 / 170.4 / 183.7 | 259 | 4 | 31.7% / 68.3% / 0.0% | - | - |
| 1080p | take_black | 79.6 / 54.4 | 21.0 / 41.8 / 176.2 / 223.5 | 255 | 6 | 32.3% / 67.7% / 0.0% | - | - |
| 1080p | fade_dimmed | 225.4 / 57.8 | 147.4 / 258.1 / 312.6 / 343.0 | 266 | 4 | 48.1% / 51.0% / 0.8% | 54.2% / 45.8% / 0.0% | 4: 4, 0 |

| Quality | Scenario | Process | Compose | Readers | GUI | Native | ffmpeg | Total | Task Manager | Compose ms per tick | Compose call p99 ms | Read fps (each playing clip) | Peak MB (process / ffmpeg) | Others busy |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1080p | program | 93% | 6% | 85% | 2% | 0% | 11% | 104% | 6.5% | 2.4 | 17.1 | 4.0 | 182 / 881 | 2.6 |
| 1080p | mix50 | 244% | 86% | 154% | 4% | 0% | 12% | 256% | 16.0% | 81.7 | 109.4 | 1.9, 1.9 | 238 / 893 | 2.8 |
| 1080p | fade | 192% | 57% | 130% | 4% | 0% | 12% | 203% | 12.7% | 35.9 | 100.1 | 2.0, 2.0 | 238 / 768 | 2.5 |
| 1080p | dim | 133% | 62% | 67% | 4% | 0% | 8% | 141% | 8.8% | 48.9 | 168.3 | 2.7 | 293 / 873 | 2.2 |
| 1080p | take_black | 133% | 62% | 67% | 4% | 0% | 8% | 141% | 8.8% | 50.0 | 174.9 | 1.4, 1.4 | 292 / 875 | 2.3 |
| 1080p | fade_dimmed | 228% | 92% | 129% | 6% | 0% | 11% | 239% | 14.9% | 210.6 | 313.6 | 1.8, 1.7 | 308 / 889 | 3.2 |

### Library clips: Holy Fire on Program, Leaves on Preview

Read in place from G:. They carry no frame numbers, so instead of repeats the table shows the share of output frames identical to the one before (`unchanged`). With one clip on Program that is the repeat rate.

| Quality | Scenario | Interval mean / sd | p1 / p50 / p99 / max | Late | Extra | Program next / rep / skip | Preview next / rep / skip | Across Take (pairs: rep, skip) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 540p | program | 41.7 / 0.7 | 39.9 / 41.7 / 43.5 / 48.2 | 0 | 0 | unchanged 0.1% | - | - |
| 540p | mix50 | 41.7 / 3.0 | 32.1 / 41.6 / 50.4 / 66.1 | 1 | 0 | unchanged 0.1% | - | - |
| 540p | dim | 41.7 / 3.7 | 34.7 / 41.7 / 49.5 / 86.2 | 4 | 4 | unchanged 0.0% | - | - |
| 540p | fade_dimmed | 53.6 / 11.8 | 35.6 / 57.8 / 75.5 / 87.5 | 344 | 4 | unchanged 0.8% | - | - |
| 1080p | program | 41.7 / 4.1 | 30.5 / 41.8 / 49.3 / 50.4 | 0 | 0 | unchanged 83.1% | - | - |
| 1080p | mix50 | 88.6 / 5.3 | 76.1 / 88.2 / 101.9 / 110.4 | 676 | 0 | unchanged 78.8% | - | - |
| 1080p | dim | 79.3 / 54.1 | 27.6 / 41.7 / 175.5 / 184.1 | 253 | 4 | unchanged 41.0% | - | - |
| 1080p | fade_dimmed | 210.1 / 54.3 | 138.2 / 244.5 / 280.3 / 293.4 | 285 | 4 | unchanged 25.3% | - | - |

| Quality | Scenario | Process | Compose | Readers | GUI | Native | ffmpeg | Total | Task Manager | Compose ms per tick | Compose call p99 ms | Read fps (each playing clip) | Peak MB (process / ffmpeg) | Others busy |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 540p | program | 40% | 4% | 34% | 2% | 0% | 40% | 81% | 5.0% | 1.6 | 3.4 | 24.0 | 100 / 425 | 2.4 |
| 540p | mix50 | 158% | 47% | 107% | 4% | 0% | 89% | 247% | 15.5% | 19.6 | 34.2 | 23.9, 24.0 | 114 / 427 | 4.2 |
| 540p | dim | 98% | 59% | 35% | 4% | 0% | 38% | 136% | 8.5% | 24.7 | 47.2 | 19.9 | 127 / 614 | 3.2 |
| 540p | fade_dimmed | 178% | 89% | 85% | 4% | 0% | 74% | 252% | 15.8% | 47.6 | 76.0 | 19.9, 19.8 | 135 / 428 | 3.0 |
| 1080p | program | 92% | 5% | 84% | 2% | 0% | 11% | 103% | 6.4% | 2.1 | 14.5 | 4.1 | 183 / 620 | 2.8 |
| 1080p | mix50 | 243% | 86% | 153% | 3% | 0% | 12% | 255% | 15.9% | 76.2 | 101.5 | 2.0, 2.0 | 239 / 628 | 2.9 |
| 1080p | dim | 133% | 62% | 67% | 4% | 0% | 8% | 141% | 8.8% | 49.5 | 173.5 | 2.7 | 292 / 615 | 2.7 |
| 1080p | fade_dimmed | 227% | 92% | 129% | 5% | 0% | 11% | 238% | 14.9% | 196.6 | 280.6 | 1.9, 1.9 | 306 / 621 | 2.4 |

What it shows:

- **The library clips cost what the bench clips cost.** With one clip at 540p, both use 0.79–0.81 cores. At 1080p the readers manage the same 4 frames a second, so 83% of output frames repeat.
- **So the bench clips stand in for the library.** Their patterns show the repeats and skips the library clips cannot.
- **MIX 0.5 at 540p cost more here** (2.47 cores, against 2.09 with the bench clips). That run also had the most other load, 4.2 cores, and the readers are the part that grows under load.

### The 30 fps H.264 clip (like Light Tunnel) on Preview

Clip A on Program, clip C (H.264 1080p30) on Preview. ffmpeg's `fps=24` drops every fifth frame of C; the kit maps C's frame numbers to 24 fps slots, so a dropped fifth frame is not counted as a skip.

| Quality | Scenario | Interval mean / sd | p1 / p50 / p99 / max | Late | Extra | Program next / rep / skip | Preview next / rep / skip | Across Take (pairs: rep, skip) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 540p | program | 41.7 / 0.2 | 41.3 / 41.7 / 42.0 / 44.3 | 0 | 0 | 100.0% / 0.0% / 0.0% | - | - |
| 540p | mix50 | 41.7 / 1.7 | 37.5 / 41.6 / 46.6 / 51.5 | 0 | 0 | 100.0% / 0.0% / 0.0% | 100.0% / 0.0% / 0.0% | - |
| 1080p | program | 41.7 / 4.1 | 31.1 / 41.8 / 49.4 / 53.5 | 0 | 0 | 17.2% / 82.8% / 0.0% | - | - |
| 1080p | mix50 | 87.4 / 5.1 | 73.9 / 87.4 / 100.1 / 106.6 | 686 | 0 | 17.9% / 82.1% / 0.0% | 17.9% / 82.1% / 0.0% | - |

| Quality | Scenario | Process | Compose | Readers | GUI | Native | ffmpeg | Total | Task Manager | Compose ms per tick | Compose call p99 ms | Read fps (each playing clip) | Peak MB (process / ffmpeg) | Others busy |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 540p | program | 40% | 3% | 35% | 2% | 0% | 42% | 82% | 5.1% | 1.4 | 1.3 | 24.0 | 99 / 441 | 1.9 |
| 540p | mix50 | 139% | 41% | 94% | 3% | 0% | 94% | 232% | 14.5% | 17.3 | 26.8 | 24.0, 24.0 | 113 / 459 | 2.0 |
| 1080p | program | 92% | 5% | 84% | 2% | 0% | 11% | 103% | 6.4% | 2.0 | 14.2 | 4.1 | 182 / 607 | 2.4 |
| 1080p | mix50 | 243% | 86% | 154% | 3% | 0% | 13% | 256% | 16.0% | 75.1 | 99.8 | 2.1, 2.1 | 238 / 630 | 2.7 |

What it shows:

- **At 540p the 30 fps clip was clean.** ffmpeg's `fps=24` dropped exactly every fifth frame, and the kit saw no repeats, no skips and three clean loop points in a minute.
- **At 1080p it repeats like the others.** The limit is the reader, not the clip.
- **ffmpeg cost a little more** for H.264 30 fps plus HEVC (0.94 cores) than for two HEVC clips (0.85).

### Through real NDI

Today's own `NdiSender` (BGRX), named "MBP bench ..." in the group mbp-bench, received by `bench_ndi_receive.py` in another process as UYVY, as ProPresenter would. CPU now includes NDI's own threads ("native").

| Quality | Scenario | Interval mean / sd | p1 / p50 / p99 / max | Late | Extra | Program next / rep / skip | Preview next / rep / skip | Across Take (pairs: rep, skip) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 540p | program | 41.7 / 0.6 | 39.7 / 41.7 / 43.7 / 45.0 | 0 | 0 | 100.0% / 0.0% / 0.0% | - | - |
| 540p | mix50 | 41.7 / 2.0 | 36.3 / 41.6 / 47.3 / 56.2 | 0 | 0 | 100.0% / 0.0% / 0.0% | 100.0% / 0.0% / 0.0% | - |
| 1080p | program | 41.7 / 4.1 | 30.1 / 41.7 / 49.7 / 51.4 | 0 | 0 | 16.5% / 83.5% / 0.0% | - | - |
| 1080p | mix50 | 91.3 / 5.3 | 78.4 / 91.2 / 103.9 / 110.3 | 657 | 0 | 18.1% / 81.9% / 0.0% | 18.1% / 81.9% / 0.0% | - |

| Quality | Scenario | Process | Compose | Readers | GUI | Native | ffmpeg | Total | Task Manager | Compose ms per tick | Compose call p99 ms | Read fps (each playing clip) | Peak MB (process / ffmpeg) | Others busy |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 540p | program | 48% | 4% | 34% | 2% | 7% | 40% | 89% | 5.6% | 1.8 | 4.1 | 24.0 | 115 / 558 | 2.1 |
| 540p | mix50 | 172% | 51% | 110% | 3% | 8% | 91% | 263% | 16.5% | 21.2 | 30.0 | 24.0, 24.0 | 129 / 492 | 2.5 |
| 1080p | program | 118% | 8% | 85% | 2% | 22% | 11% | 129% | 8.1% | 3.5 | 18.4 | 4.0 | 213 / 880 | 2.4 |
| 1080p | mix50 | 253% | 86% | 153% | 3% | 11% | 12% | 265% | 16.6% | 78.3 | 106.5 | 2.0, 2.0 | 269 / 868 | 3.2 |

| Quality | Scenario | Sent / received | Arrival mean / sd | p1 / p50 / p99 / max | Late | A next / rep / skip | B next / rep / skip | Format |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 540p | program | 1441 / 1441 | 41.7 / 1.3 | 38.7 / 41.6 / 44.7 / 46.8 | 0 | 100.0% / 0.0% / 0.0% | - | {'UYVY': 1583} |
| 540p | mix50 | 1442 / 1442 | 41.7 / 2.2 | 35.8 / 41.6 / 47.4 / 55.8 | 0 | 100.0% / 0.0% / 0.0% | 100.0% / 0.0% / 0.0% | {'UYVY': 1585} |
| 1080p | program | 1441 / 1441 | 41.7 / 4.6 | 30.2 / 41.5 / 52.8 / 57.6 | 0 | 16.5% / 83.5% / 0.0% | - | {'UYVY': 1584} |
| 1080p | mix50 | 658 / 658 | 91.2 / 5.8 | 78.5 / 90.7 / 106.1 / 114.0 | 657 | 18.1% / 81.9% / 0.0% | 18.1% / 81.9% / 0.0% | {'UYVY': 774} |

What it shows:

- **The receiver got every frame that was sent**, and the frame numbers it read advanced exactly as on the sending side. NDI added about 1 ms of jitter: standard deviation 1.3 ms against 0.6 ms at 540p.
- **NDI's own threads cost 7–8% of a core at 540p and 22% at 1080p**, sending BGRX with one receiver connected.
- **Total CPU rose by more than NDI's own share.** At 540p MIX 0.5 the readers went from 0.79 to 1.10 cores with the real sender and a receiver running. The stand-in results understate the pane's real CPU by roughly that much.
- **So the stand-in tables say the same about smoothness** as a receiver sees it, which is why the main set uses the stand-in.

### Ten minutes at 540p, MIX 0.5

Bench clips, NDI stand-in, 600 s.

| Quality | Scenario | Interval mean / sd | p1 / p50 / p99 / max | Late | Extra | Program next / rep / skip | Preview next / rep / skip | Across Take (pairs: rep, skip) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 540p | mix50 | 41.7 / 1.9 | 37.1 / 41.6 / 46.9 / 60.8 | 0 | 0 | 100.0% / 0.0% / 0.0% | 100.0% / 0.0% / 0.0% | - |

| Quality | Scenario | Process | Compose | Readers | GUI | Native | ffmpeg | Total | Task Manager | Compose ms per tick | Compose call p99 ms | Read fps (each playing clip) | Peak MB (process / ffmpeg) | Others busy |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 540p | mix50 | 138% | 43% | 92% | 3% | 0% | 84% | 222% | 13.9% | 18.1 | 26.6 | 24.0, 24.0 | 133 / 498 | 2.8 |

What it shows:

- **In 10 minutes, Program repeated one frame (at 5.5 s) and Preview none.** There were no skips. All 51 loop points were clean (24 for A, 27 for B).
- **No interval was over 62.5 ms**; the longest was 60.8 ms.
- **So today's 540p blend is steady while nothing moves.** The choppiness the user sees has to come from what moves: fades under DIM, Takes, coming back from black, and whatever else holds the GUI thread (part C).

### Run to run

The main set was run twice, about 15 minutes apart. Smoothness agrees except where compose is over budget (`fade_dimmed`), which got worse with more load on the PC. The first pass's CPU numbers were sampled, and are not shown.

| Quality | Scenario | p99 interval ms (pass 1, pass 2) | Program rep / skip (pass 1; pass 2) | Preview rep / skip (pass 1; pass 2) | Others busy, cores |
| --- | --- | --- | --- | --- | --- |
| 540p | program | 42.2, 42.0 | 0.0% / 0.0%; 0.0% / 0.0% | -; - | 3.5, 1.0 |
| 540p | mix50 | 46.8, 45.4 | 0.0% / 0.0%; 0.0% / 0.0% | 0.1% / 0.0%; 0.0% / 0.0% | 2.3, 1.8 |
| 540p | fade | 52.2, 46.7 | 0.2% / 0.3%; 0.2% / 0.2% | 0.7% / 0.0%; 0.0% / 0.0% | 3.8, 2.0 |
| 540p | dim | 49.4, 48.5 | 0.2% / 0.0%; 0.0% / 0.0% | -; - | 3.4, 1.6 |
| 540p | take_black | 49.1, 48.7 | 0.0% / 0.0%; 0.0% / 0.0% | -; - | 2.8, 1.9 |
| 540p | fade_dimmed | 90.5, 74.8 | 0.1% / 39.7%; 0.2% / 27.1% | 0.0% / 87.3%; 0.0% / 55.1% | 3.6, 2.4 |
| 1080p | program | 49.5, 50.4 | 83.1% / 0.0%; 83.5% / 0.0% | -; - | 2.8, 2.6 |
| 1080p | mix50 | 101.2, 109.3 | 82.1% / 0.0%; 82.2% / 0.0% | 82.1% / 0.0%; 82.2% / 0.0% | 2.9, 2.8 |
| 1080p | fade | 99.8, 102.5 | 83.0% / 0.0%; 83.1% / 0.0% | 82.4% / 0.0%; 82.4% / 0.0% | 2.9, 2.5 |
| 1080p | dim | 168.4, 170.4 | 68.3% / 0.0%; 68.3% / 0.0% | -; - | 2.7, 2.2 |
| 1080p | take_black | 169.2, 176.2 | 67.4% / 0.0%; 67.7% / 0.0% | -; - | 2.2, 2.3 |
| 1080p | fade_dimmed | 272.4, 312.6 | 49.4% / 0.0%; 51.0% / 0.8% | 46.9% / 0.0%; 45.8% / 0.0% | 2.5, 3.2 |

## Where the repeats and skips happen

**Steady playback at 540p is clean.** With one clip on Program, every output frame showed the next frame, for 60 s, across two loop points. Blended at MIX 0.5, Preview repeated one frame in the first pass's minute and none in the second's. The loop points were clean in every run that kept up.

**At every Take.** The frame pair across the Take is a repeat, often followed by a second. At 540p, Program then usually skips a frame: at 3 of the 4 Takes in the second pass's `fade` run, and at all 4 in the first. (At 1080p nothing is ever queued, so there is no skip.) In the second pass, every repeat and skip in the 540p `fade` run came at a Take. From the frame records of one Take (540p, `fade`, a trial run):

| Time | Sent by | MIX | What NDI got |
| --- | --- | --- | --- |
| 13.906 | compose | 1.000 | B 233 |
| 13.949 | GUI thread (`take()` composes at once) | 0 | B 233 again |
| 13.958 | compose, 9.5 ms later | 0 | B 233 a third time |
| 13.989 | compose, 30.5 ms later | 0 | B 235: 234 is never shown |

- `take()` composes a frame itself, from the GUI thread, right after the swap. That is an extra frame, so the receiver gets one more frame than ticks.
- `Bus.swap` calls `discard_queued()` on both decoders. The next tick finds the queue empty, waits its 8 ms, and repeats the frame.
- The frame that was dropped from the queue is the one that never appears.
- The picture holds for about 80 ms, then jumps. The repeat came at every one of the 16 Takes in the `fade` and `fade_dimmed` runs (4 per run, both qualities). A Take under black cannot be seen, so it is not counted.

**During fades, under load.** The first pass's 540p `fade` run had more load on the PC (3.8 cores in other processes). It also had 9 repeats away from the Takes. At 5.285 s, two ticks came 69 and 60 ms apart, both buses repeated in the same tick, and the compose loop then caught up with ticks 22–29 ms apart. The second pass (2.0 cores of other load) had none. It was only seen with two clips playing, never with Program alone. Likely cause: three busy Python threads (compose and two readers) contending for the GIL. Not proven.

**Going to black and back.** About 3 frames go by unseen each time. For the last few ticks of the DIM chase the value is already 0, so the output is black, but the clip is only paused when the chase ends. Then `set_paused(True)` drops the queued frames. Nobody can see it, because the picture is black.

**Coming back from black holds the GUI thread.** The `set_dimmer` call that leaves black takes about 70 ms at 540p and about 280 ms at 1080p. The knob animation and every other GUI timer wait that long.
- First it composes a whole dimmed frame itself, on the GUI thread (`_push_dimmed_now`). That is about 35 ms at 540p and 130 ms at 1080p, timed from when the knob's value first leaves 0.
- Then `_emit_preview_frame` waits for the compose lock while the compose thread finishes its own dimmed frame: 33–37 ms at 540p and 139–147 ms at 1080p, measured from the pushed frame to the call's return.

**Take under black.** The clip taken under black starts from its first frame when DIM comes up. The pattern first reads at about frame 13, half a second after DIM leaves 0, while the picture is still very dark.

**When compose is too slow, it skips.** At 540p with the blend and the vignette together (`fade_dimmed`), a compose call takes 55–60 ms against a 41.7 ms budget. Output drops to 17–19 frames a second. The decoders keep reading at 24 a second, so the clips skip frames. Figures are from the two passes; the busier pass was the worse.
- Preview is only on air during the fades, the slowest part, so it skipped on 55–87% of frame pairs.
- Program also counts the 5 s holds between fades. There DIM alone takes about 37 ms, which fits, so Program skipped on 27–40% overall.

**At 1080p everything repeats.** The reader threads cannot keep up (see "Reading frames from ffmpeg" below). Program gets 4 new frames a second with one clip and 1.9 a second per clip when two are blended. Each frame is shown about 6 or 12 times.
- Nothing is skipped. The readers restart their clock instead of catching up, so the clips play in slow motion, at about 1/6 and 1/12 of real speed.
- On top of that, a compose call at 1080p MIX 0.5 takes about 95 ms, so output falls to 10.6 frames a second.
- With DIM moving too, output falls to 4.4 frames a second.

## Other things found

- **The gpu-d3d11 mode is tried, and fails, for every clip.** This ffmpeg has the `scale_d3d11` filter, so each load runs:
  1. a probe (`ffmpeg -i clip`)
  2. the gpu-d3d11 command, which ends at once
  3. the plain gpu command

  `ffmpeg -filters` also runs at start-up, more than once when both buses load together. That is about 0.4 s before the first frame, and two extra processes per clip load.
- **ffmpeg's own CPU at 1080p is low only because the readers hold it back.** It waits on a full pipe most of the time.
- **Memory.** The process peaks at about 100–135 MB at 540p and 180–310 MB at 1080p, including the kit's own 11 MB of frame records. The ffmpeg processes together peak at 430–890 MB of working set.
- **The compose thread runs at normal priority**, the process at below normal (from `prefer_livestream_apps`).
- **GetThreadTimes undercounts short bursts.** In the first pass the compose thread at 540p MIX 0 read 0% CPU, and ffmpeg about half its real CPU. The kit now counts cycles instead (see "What each number means").

### Reading frames from ffmpeg costs more than the plan's table says

The plan's table puts Python's share at 1.0 ms per 540p frame. Today's reader loop takes about 16 ms, and the reader threads use more CPU than the compose thread whenever MIX is 0.

On its own, outside the Engine (today's `HwDecoder` on clip A at 540p, with a stand-in compositor taking one frame per tick for 10 s):

- The reader thread used 38% of a core, 16 ms per frame.
- 14.6 ms of that is `_read_exact`: 64 pipe reads per 2 MB frame.
- `sleep_until` used no measurable CPU. Python 3.14's `time.sleep` wakes on time here, so the spin at the end hardly runs.
- The rest of the loop (`frombuffer`, `.copy()`, the alpha write, the queue, the signal) is about 1.4 ms.

Why `_read_exact` is so slow: each `stdout.read(size - len(buf))` asks for the whole rest of the frame. Python allocates a buffer that size (up to 2 MB, or 8 MB at 1080p), the pipe fills 32 KB of it, and Python shrinks it again. That is 64 large allocations per frame at 540p and 254 at 1080p.

Reading the same ffmpeg output as fast as it comes, with no app code, Python CPU per frame:

| Read method | 540p BGRA (2.0 MB) | 1080p NV12 (3.0 MB) |
| --- | --- | --- |
| Today: `read(rest)`, append to a bytearray, `bytes()` | 20.0 ms | 39.6 ms |
| `readinto` the rest of a preallocated buffer | 1.2 ms | 1.9 ms |
| `readinto`, at most 64 KB per call | 0.8 ms | 0.9 ms |
| `readinto`, at most 256 KB per call | 1.0 ms | 0.7 ms |
| `readinto`, at most 1 MB per call | 1.2 ms | 0.9 ms |

- 150 frames each. `time.thread_time()` is coarse on Windows, so the `readinto` rows are good to about ±0.3 ms.
- The number of reads per frame is the same in every row (the pipe hands over 32 KB at a time). Only the allocation goes.
- So the plan's `readinto` into a pool of buffers removes almost all of it. Capping each call at 64–256 KB saves a little more.

## What the kit does

`tests\bench_pipeline.py` runs the real `Engine` with no window (`QT_QPA_PLATFORM=offscreen`), the way the pane uses it:

- It sets up the process like `app.main()`: the 1 ms timer and below-normal priority (ProPresenter and vMix were not running, so below normal it is).
- `cfg = Config()`, quality and 24 fps set, GPU decode on, Scale to Fill. Then `engine.program.load(A)` and `engine.load_preview(B)`, so B sits paused on its first frame as it does in a service.
- `engine.gui_enabled` stays on, as it is in the pane (the LAN remote keeps it on), so the 160x90 monitor pictures are made too.
- The MIX and DIM knobs are copied from `widgets.ChaseSlider`: a precise QTimer every 8 ms on the GUI thread, easing over the first and last tenth of the fade, the value an integer 0-1000. The Fade and Fade to Black buttons and the knob handlers do what `MainWindow` does: `set_mix` on each change, `take()` when MIX reaches 1, `set_dimmer(value, allow_pause=False)` while DIM is moving and `allow_pause=True` when it stops.
- `engine.NdiSender` is replaced by a stand-in with the same interface. For each frame it keeps the time, the thread that sent it, MIX, DIM, which clip is on each bus, and about 1,600 pixels at the pattern positions. That costs tens of microseconds in the compose thread. The patterns are read after the run.
- The stand-in tells the frame format from the array's shape: `(h, w, 4)` is today's BGRA, `(h * 3 // 2, w)` is NV12, and luma comes from the green channel or the Y plane. `--selftest` checks both on synthetic frames.

The test clips (`tests\make_bench_clips.py`) carry their own frame numbers as a pattern of 96 px black and white blocks near the middle of the picture: 10 bits of frame number and 6 check bits that also name the clip. Clip A has its pattern in the upper middle, B and C in the lower middle, and each is black where the other's pattern is, so both read at MIX 0.5. The clip maker checks them through ffmpeg the way the pane decodes them: every frame read back at 360p, 540p and 1080p, in BGRA and NV12, blended 50/50, and under the vignette down to DIM 0.05.

| Clip | Format | Frames | Pattern |
| --- | --- | --- | --- |
| `bench_A_hevc_1080p24.mov` | HEVC Main 1920x1080, 24 fps, 41 Mb/s, BT.709 limited, 1 s GOP | 600 (25 s) | A, upper |
| `bench_B_hevc_1080p24.mov` | the same | 528 (22 s) | B, lower |
| `bench_C_h264_1080p30.mov` | H.264 Main 1920x1080, 30 fps, 8 Mb/s, like Light Tunnel | 600 (20 s) | C, lower |

The rest of each picture is a moving colour pattern with noise, which brings the HEVC clips to the library's bitrate. A and B have different lengths, so their loop points do not line up.

### The scenarios

Each runs in its own Python process: 4 s after loading, then the setup (MIX or DIM held), 2 s, then 60 s measured.

| Name | What happens in the 60 s |
| --- | --- |
| `program` | A on Program at MIX 0, DIM 1. B waits on Preview, paused. |
| `mix50` | MIX held at 0.5: A and B blended. |
| `fade` | The Fade button every 15 s: MIX chases 0 to 1 over 10 s, then Take. A and B swap each time. |
| `dim` | Fade to Black (DIM 1 to 0 over 10 s), 5 s of black, Fade from Black (10 s), every 30 s. |
| `take_black` | Fade to Black, Take while black (Fade pressed under black), Fade from Black, every 30 s. |
| `fade_dimmed` | DIM held at 0.5, and the Fade button every 15 s: the blend and the vignette together. |

### What each number means

- **Output frame intervals:** time between consecutive frames handed to NDI, in ms. At 24 fps each should be 41.7. "late" counts intervals over 62.5 ms, a missed tick. "extra" counts frames sent by another thread than the compose thread (today: `take()`, Fade from Black and black on the GUI thread).
- **Advance per bus:** between two consecutive output frames in which a clip's pattern can be read, how far its frame number moved. 1 is right. 0 is a repeat (a frame shown twice). 2 or more is a skip. They are counted per bus (the clip on Program, the clip on Preview), and separately for the pair of frames across a Take. A clip that is too faint to read (low MIX share, deep DIM, black) is not counted, so a fade only counts each clip while it is visible.
- **CPU:** CPU time over wall time, so 100% is one of the 3700X's 16 threads. It comes from cycle counts (`QueryThreadCycleTime`, `QueryProcessCycleTime`), divided by the counter's rate, which the kit measures at the start of each run (about 3.6 GHz here). `GetThreadTimes` and `GetProcessTimes` are only sampled at the clock tick. A thread that wakes, works for a millisecond and sleeps again between two ticks can be charged nothing: today's compose thread at 540p MIX 0 read 0% that way, against about 1.1 ms per tick of real work. The sampled numbers are still in the JSON, as `cpu_sampled`. The columns:
- **process:** the pane's Python process.
- **compose:** the compose thread, found by its name and opened by its `native_id`.
- **readers:** the two decoder reader threads, `ffmpeg-program` and `ffmpeg-preview`.
- **gui:** the main thread: the knob timer, signals, the watchdog.
- **native:** threads Python did not start (Qt, NDI).
- **ffmpeg:** the ffmpeg processes, found by parent pid.
- **total:** process plus ffmpeg. Task Manager would show the total divided by 16.
- **compose ms/tick:** compose thread CPU per output tick. **compose p99:** 99th percentile of the wall time of one `_compose_unlocked` call (it includes up to 8 ms waiting for a Program frame). The budget at 24 fps is 41.7 ms.
- **Memory:** the process's peak working set and the ffmpeg processes' peak working sets, in MB.
- **others:** cores busy in other processes during the run (`GetSystemTimes`), to show how loaded the PC was.

## How to run the kit

Always with the venv's Python and `-B`, from the worktree:

```powershell
$py = "C:\Users\avbri\Documents\Media Background Pane\.venv\Scripts\python.exe"

# Once: make the test clips (about 2 minutes) and check they read back
& $py -B tests\make_bench_clips.py

# Check the kit itself on synthetic BGRA and NV12 frames (no Engine, a few seconds)
& $py -B tests\bench_pipeline.py --selftest

# Today's code: freeze a copy first, then measure the copy
robocopy "C:\Users\avbri\Documents\Media Background Pane\media_background_pane" "$env:TEMP\mbp-bench\baseline\media_background_pane" /E /XD __pycache__
& $py -B tests\bench_pipeline.py --code "$env:TEMP\mbp-bench\baseline" --label before --out "$env:TEMP\mbp-bench\results\before.json"

# The new code: the worktree itself
& $py -B tests\bench_pipeline.py --code . --label after --out "$env:TEMP\mbp-bench\results\after.json"

# Side by side
& $py -B tests\bench_pipeline.py --compare "$env:TEMP\mbp-bench\results\before.json" "$env:TEMP\mbp-bench\results\after.json"
```

More:

- `--quality 540p,1080p` (default), or any of 1080p, 720p, 540p, 360p.
- `--scenarios program,mix50,fade,dim,take_black,fade_dimmed` (default: all six). `--seconds 60` per scenario.
- `--clips bench` (A on Program, B on Preview; default), `bench30` (A and the 30 fps H.264 clip C), `real` (Holy Fire and Leaves from the library, read in place on G:), or `--program PATH --preview PATH`.
- `--decode software` turns GPU decode off, like the Settings switch.
- The long run for the "done" check: `--scenarios mix50 --quality 1080p --seconds 600 --label long`.
- `--ndi real --receiver` uses the package's own NdiSender and receives it in a second process (`tests\bench_ndi_receive.py`), so CPU includes NDI's work and a third table shows what a receiver gets.
- `--table FILE` prints the tables of a results file again; `--reanalyse FILE` reruns the analysis on the saved frame records.

Each scenario runs in its own Python process. Results: a JSON file (default `%TEMP%\mbp-bench\results\<label>_<time>.json`) and, in the folder of the same name, one `.npz` per scenario with every output frame's time, thread, MIX, DIM and pattern samples. Each run's private folder (`%TEMP%\mbp-bench\runs\...`) holds its session log.

### The runs behind this note

All against `--code "$env:TEMP\mbp-bench\baseline"`. The results are in `%TEMP%\mbp-bench\results`:

| File | Extra options |
| --- | --- |
| `before.json` | none (bench clips, all six scenarios, 540p and 1080p) |
| `before-sampled.json` | the same, first pass, before the kit counted cycles |
| `before-720p.json` | `--quality 720p --scenarios program,mix50` |
| `before-real.json` | `--clips real --scenarios program,mix50,dim,fade_dimmed` |
| `before-30fps.json` | `--clips bench30 --scenarios program,mix50` |
| `before-ndi.json` | `--ndi real --receiver --scenarios program,mix50` |
| `before-long.json` | `--quality 540p --scenarios mix50 --seconds 600` |

To compare, run the same options with `--code .` and a new `--label`, then `--compare` the two files.

### Measuring the rebuilt code

- In the JSON, `formats` should say `nv12`, and `frame_shapes` should show `(1620, 1920)` at 1080p. The stand-in reads luma from the Y plane.
- The compose call times wrap `engine._compose_unlocked`. If the rebuild renames it, pass `--compose-method <name>`, or those times are left out and a warning says so. The compose thread is found by its name, `compose`.
- If the new `NdiSender` gains methods the stand-in does not know, the stand-in counts the calls (`sender_calls` in the JSON) and records frames passed to any method named `submit...`.
- Use the same clips for before and after. They live in `%TEMP%\mbp-bench\media`, and `make_bench_clips.py` makes them again from the same picture and seeds if they are gone.
- Run before and after under similar load. Each run records the cores other processes were using (`others`).

## What keeps it away from the live pane

- The measured package's `log.session_log_path` and `log.fallback_log_path` are replaced before anything else, and each child process gets its own `LOCALAPPDATA`, `TEMP` and `TMP`. It checks both paths point into the private folder before it builds the Engine.
- `Config()` is built and filled in; `Config.load()` and `Config.save()` are never called.
- NDI is a stand-in. Even with `--ndi real`, `NDIlib.send_create` is wrapped: it refuses any sender whose name does not start with "MBP bench", and puts the rest in the group `mbp-bench`. The receiver only looks in that group.
- No window, no LAN server, no single-instance lock; `main.py` is never run. `--code` pointing at the main folder is refused.

## What was not measured

- **ProPresenter itself.** The NDI receiver in `bench_ndi_receive.py` stands in for it. It asks NDI for UYVY, the way most receivers do.
- **The GPU.** The d3d11va decode runs on the GTX 1660; its load was not read.
- **The rest of the GUI thread.** Only the knob animation runs here. The pane's GUI thread also has the workspace and z-order timers, the thumbnails, painting the monitors, and the Video Input status poll (HTTP to ProPresenter, up to 0.8 s). Those are part C of the plan.
- **The LAN remote.** Its snapshots come from the monitor pictures, which are made here because `gui_enabled` is on, but no iPad asks for them.
- **Stills, Fit and Actual Size, the Qt fallback, software decode.** The kit can do software decode (`--decode software`) and any two clips (`--program`, `--preview`); those runs were not made.
- **A busy PC.** Other work ran during these runs: another Python process at about 1 core most of the time, and up to 4 cores in all. Each run records it. The pane's process ran at below-normal priority, as it does in a service.
