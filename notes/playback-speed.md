# Playback speed

Fixed 2 Nov 2026 in code. The running pane does not pick this up until it is opened again.

- `HwDecoder._play_mode` reads one frame from ffmpeg every 1/fps second (1/24 s at the default 24 fps). This now sets the playback speed.
- After a pause, the reader starts its clock again from that moment. It does the same when ffmpeg falls more than two frames behind (a slow loop restart, a slow disk). It does not rush to catch up.
- `_enqueue` still drops the oldest frame instead of waiting. A dead compositor still cannot freeze ffmpeg (see `playback-stall.md`).
- The stall watchdog is unchanged. The reader never waits more than one frame between reads, far inside `_STALL_TIMEOUT`.
- Tests: `tests/test_decoder_pacing.py`. Measuring tool: `tests/check_playback_speed.py`.

Noted 2 Nov 2026. Spotted by reading the code during the CentraLight work, then measured on the pane that had been running since 18:30 that day. Caused by the 1 Nov stall fix.

## What it looked like

Looping clips on Program played too fast. Holy Fire ran at about 2.7 times normal speed, so a 10-second loop would come round in under 4 seconds. Preview clips played fast too while MIX was moving. Stills and paused clips were not affected. ffmpeg and the pane each used about a full CPU core for one clip.

Nobody had reported it from a service. If the pane was restarted on 1 Nov after the stall fix and used that evening, that service had fast clips too. Not known.

## Why

ffmpeg decodes as fast as its output is read. The command has no `-re` or `-readrate`, so ffmpeg does nothing to hold itself to real time.

Before 1 Nov, `_enqueue` waited whenever the 2-frame queue was full. The compositor takes one frame per tick, 24 a second. So that wait held the reader to 24 frames a second, and the pipe held ffmpeg to the same. It was the only thing setting the playback speed. Nothing in the code said so.

The 1 Nov stall fix made `_enqueue` drop the oldest frame instead of waiting. That was right for the stall, but it removed the pacing. The reader then pulled frames as fast as ffmpeg and Python could move them, and the compositor showed whichever frame was newest at each tick.

The command ends in `fps=24`, so every frame read is 1/24 of a second of the clip. Frames read per second divided by 24 is the playback speed.

`frame_ready` fires once per frame read. The GUI thread was getting about 65 queued signals a second instead of 24.

## Evidence (2 Nov 2026)

From the running pane, read only. It was not restarted.

- Pane: `pythonw.exe main.py --startup`, started 18:30, pid 18184. Settings: 540p, 24 fps, GPU decode on.
- Program: ffmpeg pid 22424 on `Holy Fire.mov` (HEVC 1920×1080, 24 fps, 40.7 Mb/s), started 19:14. It ran `-hwaccel d3d11va` with a CPU lanczos scale to 960×540.
- Over 10 seconds, ffmpeg wrote 64.4 frames a second into the pipe and the pane read 64.4. One 960×540 frame is 2,073,600 bytes. That is 2.7 times real time. Two more samples gave 65.5 and 67.3.
- ffmpeg read Holy Fire at 12.7 MB/s. The file's bitrate is 5.1 MB/s, which gives about 2.5 times.
- ffmpeg had used 9,382 CPU seconds in about 75 minutes. During the 10 seconds, ffmpeg and the pane each used about one core.
- Preview: ffmpeg pid 4720 on `Light Tunnel.mov`, paused, using no CPU.

From `tests/check_playback_speed.py`. This runs the app's own `HwDecoder` and ffmpeg command at 960×540, 24 fps. A stand-in compositor takes one frame per tick.

| Clip | Mode | Before | After |
| --- | --- | --- | --- |
| `vendor/ffmpeg/testsrc.mp4` (H.264, 24 fps) | software | 49.4 frames/s, 2.06× | 24.2 frames/s, 1.01× |
| `vendor/ffmpeg/testsrc.mp4` | gpu | 50.6 frames/s, 2.11× | 24.0 frames/s, 1.00× |
| `Light Tunnel.mov` (H.264, 30 fps, local copy) | software | not run | 24.0 frames/s, 1.00× |
| `Light Tunnel.mov` | gpu | not run | 24.2 frames/s, 1.01× |

- Before the fix, the stand-in skipped frames on 116 of 120 ticks. After, there were no skips, and at most 1 tick in 120 had no new frame.
- With nothing taking frames, the reader went 50 frames a second before and 24 after.
- In the first second after a 2-second pause, it read 50 frames before and 24 after.
- After the fix, ffmpeg used 0.2 to 0.6 cores and Python about 0.45 cores per playing clip.

## Why not `-readrate 1`

Tried on the unpaced decoder. It averaged 24 frames a second across loop restarts, in software and GPU mode. But the frames came unevenly. The stand-in compositor had no new frame on about 18% of ticks and skipped frames on about 12%. After a 3-second pause it read 50 frames in the first second to catch up, and `-readrate_catchup 1` did not change that. Pacing in the reader keeps pause and stalls under the app's control.

## Not settled

- Not checked in the real app yet. The pane running on 2 Nov still has the old code and was left alone.
- By 22:44 on 2 Nov that pane's compositor and Program decoder had gone, so NDI was frozen. The cause is not known, and it is a separate problem from the speed (see `playback-stall.md`). Since 3 Nov a watchdog starts them again. Because of the drop-oldest queue, a dead compositor no longer stops the decoder. So the read rate alone cannot show a frozen picture.
- The reader and the compositor now each keep their own 1/fps clock. Both use `time.perf_counter`, so they do not drift apart, and the 2-frame queue absorbs small jitter. Not tried yet: a busy machine during a long MIX or DIM fade, or 1080p quality.
- `gpu-d3d11` mode never works with this ffmpeg (9.0.2). `scale_d3d11` fails with "Unsupported pixel format: (null)", so every clip falls back to `gpu` (GPU decode, CPU scale). This is older than the stall fix and is not fixed here.
- Reading one 960×540 frame from the pipe costs the pane about 20 ms of CPU. That is about half a core per playing clip, the same as before 1 Nov. Worth a look before using 1080p quality.

## Checking the live pane

The full check after a restart, including the log and the watchdog, is in `playback-stall.md` under "Checking after the restart". This is its speed and picture step.

Put a looping video on Program, wait for the fade to finish, and leave MIX at 0. Then run this in PowerShell. It assumes 540p quality and the iPad remote on its usual port, 8745. It checks two things over 10 seconds:

- **Speed:** how fast the pane reads frames from ffmpeg.
- **Picture:** whether the Program picture changes. It reads the snapshot that the iPad remote serves; the compositor refreshes that snapshot about five times a second.

```powershell
$frame = 960 * 540 * 4   # one 540p picture, as the pane reads it from ffmpeg
function PaneReadBytes {
  (Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object CommandLine -like '*Media Background Pane*main.py*' |
    Measure-Object -Property ReadTransferCount -Sum).Sum
}
function ProgramPicture {
  $jpg = (Invoke-WebRequest -UseBasicParsing "http://127.0.0.1:8745/api/program.jpg").Content
  (Get-FileHash -InputStream ([IO.MemoryStream]::new($jpg))).Hash
}
$clock = [Diagnostics.Stopwatch]::StartNew(); $a = PaneReadBytes
$pictures = foreach ($i in 1..10) { ProgramPicture; Start-Sleep -Seconds 1 }
$b = PaneReadBytes; $seconds = $clock.Elapsed.TotalSeconds
$speed = ($b - $a) / $seconds / $frame / 24
$moving = @($pictures | Select-Object -Unique).Count
"Speed:   {0:N2}x normal (1.00x is right)" -f $speed
"Picture: {0} different Program pictures in 10 seconds (10 is right, 1 means frozen)" -f $moving
```

| When | Speed | Pictures | Meaning |
| --- | --- | --- | --- |
| 2 Nov ~20:30, old code | 2.80x | (not measured) | clips too fast |
| 2 Nov 22:50, old code | 0.00x | 1 | frozen picture, see `playback-stall.md` |
| Healthy | about 1.00x | 10 | right |

About 2.00x during a fade is normal, because two clips are playing. A still image on Program also gives 1 picture. The "healthy" row has not been seen yet; it is what the code should give.
