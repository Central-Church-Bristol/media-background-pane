# Playback stall

Fixed 1 Nov 2026 in code. It happened again another way on 2 Nov, and more was fixed on 3 Nov. The running pane does not pick up either fix until it is opened again.

- `Engine._compose_run` logs an exception and keeps going. If that thread still exits, another one is started. Since 3 Nov that restart is in a `finally`, so it runs even when logging fails.
- `HwDecoder._enqueue` drops the oldest frame instead of blocking, so a missing compositor cannot fill the ffmpeg pipe. That also removed the only thing holding clips to real time; they played too fast until 2 Nov (see `playback-speed.md`).
- If ffmpeg produces no frame for 8 seconds while it should be playing, it is killed and the clip is opened again. A paused preview does not count.
- Since 3 Nov a watchdog on the GUI thread checks the compositor and both decoders every second, and starts again whatever has stopped. See "3 Nov 2026: watchdog" below.
- Errors go to `%LOCALAPPDATA%\Media Background Pane\session.log`. Since 3 Nov writing the log cannot fail without a trace. If that file cannot be written, entries go to `%TEMP%\Media Background Pane session.log`.

Noted 31 Oct 2026, during Evening Service. Quitting and opening the pane again was the only recovery at the time.

## What it looked like

The window stayed open and the buttons still drew. Preview and Program were both stuck on the last frame of **Light Tunnel**. The Program image did not change across two captures taken three seconds apart. DIM was up (the button still read Fade to Black), so this was not the intentional blackout pause.

## What had actually stopped

The compositor thread had exited. That thread is the only thing that pulls decoded frames and sends them to the monitors and to NDI. It is started once, in `Engine.__init__`, as a daemon named `compose`, running `Engine._compose_run` in `media_background_pane/engine.py`.

`_compose_run` has no handler around the loop. Any exception ends the thread. Nothing starts it again. `close()` is the only place that sets `_compose_running` to false, and the window had not closed, so the thread did not shut down on purpose.

With the compositor gone, the decoder fills its queue and then waits forever:

- `HwDecoder` in `media_background_pane/hw_decoder.py` uses `Queue(maxsize=2)`.
- `_enqueue` calls `put` with a 0.05s timeout and, on `Full`, tries again until `_running` is false. It does not drop frames.
- The live decoder thread was blocked in that `put`. It was not in the pause wait.
- ffmpeg then blocks writing the raw-video pipe, sits at 0 CPU, and the picture never moves.
- The first-frame watchdog (`_FIRST_FRAME_TIMEOUT`, 3 seconds) only kills ffmpeg if the first frame never arrives. After one frame has been seen, a stall is permanent.

The UI thread was still in the Qt event loop, which is why the window did not show as Not Responding. Picking another clip still only shows a single frame, because `take()` paints once on the UI thread and the dead compositor is what would have kept playing.

## Evidence from the live process (31 Oct 2026)

- Pane: `pythonw.exe main.py --startup`, started 12:59, pid 10312. Window title "Media Background Pane".
- Clip decoder: ffmpeg started 16:48 on `G:\My Drive\Video Projection\Evening Service\Media Backgrounds\Light Tunnel.mov`, with D3D11VA and a 960×540 scale. About 0.5s of CPU total, then idle. No disk reads after that.
- `py-spy dump` (0.4.2) showed three Python threads only: the UI thread in `app.py` `app.exec()`, the LAN server in `select`, and the decoder thread in `queue.put` → `_enqueue` → `_play_mode`. There was no `compose` thread.
- The process had about an hour of CPU belonging to threads that had already exited, so playback had been working earlier in the same session. The compositor died afterward.
- No Application crash was logged. `pythonw` calls `FreeConsole()` in `media_background_pane/app.py`, so the traceback from the dead thread was discarded. The exception itself was never captured. Do not invent a specific NDI or QImage cause until a log shows one.
- The movie file was fine. A separate ffmpeg read decoded H.264 1920×1080 frames from it immediately. ProPresenter was running. The stall was inside this process, before a frame was handed to NDI.
- `instance.lock` in `%LOCALAPPDATA%\Media Background Pane\` pointed at a dead pid from August. That was not this failure.

## Fix later

1. Log exceptions from background threads to a file under `%LOCALAPPDATA%\Media Background Pane\`. Stderr from `pythonw` is not visible.
2. Catch exceptions inside `_compose_run`, write that log, and keep the loop going. If the thread still exits, start another one. One error must not end playback for the rest of the session.
3. When the frame queue is full, drop the oldest frame instead of blocking in `_enqueue`. A missing consumer must not freeze ffmpeg.
4. The first-frame watchdog does not cover a stall after playback has started. After the logging and the non-blocking queue are in, see if a mid-playback stall still needs its own restart.

`py-spy` was installed into `.venv` while diagnosing this. That was not an app change.

## Google Drive, 31 Oct 2026

The media folder is `G:\My Drive\Video Projection\Evening Service\Media Backgrounds`. Drive died during the same event and had to be restarted. The pane started working again only after the pane itself was restarted. Test the Drive link later. Do not treat it as settled.

A Drive hang can freeze the picture, because ffmpeg reads the mov from `G:`. That would leave the compositor alive and block the decoder in `stdout.read`.

The dump we actually got was different. The compositor thread was already gone, and the decoder was blocked in `queue.put` on a full queue. The file could be read at that moment, and a separate ffmpeg decoded frames from Light Tunnel straight away. Drive coming back does not restart a dead compositor. A new pane process does.

## 2 Nov 2026: compositor gone again, with the 1 Nov fix in place

The CentraLight session reported that the pane's NDI source sent one frame per connection and then nothing. Checked at 22:44, without restarting or pausing the pane. The pane was running the 1 Nov code: it started after that change, and the decoder line numbers in the dump match it. So the compose restart and the session log were both in place. Neither did anything.

- Pane: `pythonw.exe main.py --startup`, pid 18184, started 18:30. The PC had booted at 18:29.
- `py-spy dump --nonblocking` showed four Python threads, the same in four dumps from 22:45 to 22:47:
  - the UI thread in `app.exec()`
  - the LAN server
  - a paused decoder (Light Tunnel, on Preview) and its watchdog.
- There was no `compose` thread and no Program decoder.
- The OS thread list agrees. Python 3.14 gives OS threads their Python names, and there was no thread named `compose` or holding the Program decoder. About 12,600 of the process's 13,800 CPU seconds belonged to threads that had exited.
- The Holy Fire ffmpeg had exited and no new one had started. At about 20:30 it was still decoding (too fast; see `playback-speed.md`).
- `/api/state` still listed Holy Fire on Program and Light Tunnel on Preview, with MIX at 0 and DIM fully up. `/api/program.jpg` stayed the same picture for 10 seconds.
- There was no `session.log` anywhere.
- `closeEvent` / `Engine.close()` had not run: the NDI sender, the LAN server and the window were still alive, and settings were last saved at 19:11.
- Memory was not the cause. The pane peaked at about 700 MB, the PC had 24 GB of 32 GB free, and Windows logged no low-memory events.

Not known: what ended the compose and Program decoder threads, or exactly when. It was some time between 20:30 and 22:44. The compose thread ended without starting a new one, and nothing reached the log.

One way this could stay silent: `log_exception` itself raises something other than `OSError`. The excepthook's own error would then go to the missing `pythonw` stderr. On 3 Nov this was shown to be possible (see "3 Nov 2026: logging"). Whether it is what happened on 2 Nov is not known.

Done on 3 Nov, in the sections below: a watchdog outside the compose thread, a `log_exception` that cannot raise, and a check after the restart that the Program picture moves.

## 3 Nov 2026: watchdog

`Engine.check_playback` runs every second from a `QTimer` on the GUI thread. That thread was still working on 31 Oct and on 2 Nov. It checks:

| It finds | It does | Line in session.log |
| --- | --- | --- |
| no compose thread | starts one | `watchdog: the compositor had stopped; started it again` |
| a compose thread on the same frame for 5 s | logs where it is waiting, once. It does not replace it: the stuck thread holds the lock the buttons need, so a new one would wait too. | `watchdog: the compositor has been on the same frame for 5 s ...`, then a stack |
| no thread for the Program or Preview decoder | starts the clip again, paused if it was paused | `watchdog: the Program decoder had stopped (Holy Fire.mov); started it again` |
| a clip that was playing but has read no frame for 30 s | starts the clip again | `watchdog: the Program decoder had read no frame for 30 s (Holy Fire.mov); started it again` |

- The 30 s rule is only a backstop. The decoder's own 8 s rule normally deals with a frozen ffmpeg, and opening a clip from Google Drive can take up to another 15 s. It does not act before a clip's first frame, or on a paused clip.
- After a gap of more than 5 s between two checks (PC asleep, GUI thread busy), it measures again instead of acting.
- The compose thread's own restart still comes first, and now logs `compose: the compose thread ended while the pane was running; starting a new one`.
- Whatever ends a decoder thread is logged as `decoder program thread` (or `preview`) with its traceback.
- Code: `Engine.check_playback` and `_check_compose` in `engine.py`, `Bus.check_decoder` in `bus.py`. `HwDecoder` counts the frames it reads (`frames_read`). Its pacing is unchanged.

## 3 Nov 2026: logging

The 1 Nov `log_exception` caught only `OSError`. Anything else it raised escaped into the handler that was meant to keep the thread alive. Shown on 3 Nov with the saved 1 Nov code: an error message containing a stray surrogate character (possible in an odd file name) made it raise `UnicodeEncodeError` and write nothing. In the compose thread that would have skipped the restart. Under pythonw nothing would show.

Now, in `log.py`:

- `log_exception` and `log_event` never raise.
- Text that cannot be encoded is written escaped. If a traceback cannot be formatted, the error line is still written.
- If session.log cannot be written, the entry goes to `%TEMP%\Media Background Pane session.log` with a note saying why.
- If neither can be written, the next entry that is written says how many were lost.
- If a full log cannot be renamed to `session.log.1`, it keeps growing instead of losing entries.
- `install_error_logging` also logs GUI-thread errors (Qt passes errors in slots to `sys.excepthook`) and errors in `__del__`.
- Each start writes `start: pid ...`, the engine adds `watchdog: on ...`, and closing writes `stop: the pane is closing`. A pane that disappeared without a `stop:` line did not close normally.

## Evidence (3 Nov 2026)

- `tests/test_watchdog.py`, 10 tests: the real `Engine`, decoders and watchdog, with stand-ins for ffmpeg and NDI.
- `tests/test_error_log.py`, 12 tests. Two of them run under pythonw.
- All 31 tests passed in 8 runs.
- The new tests fail on the code from before this work, and on 12 copies that each had one safeguard taken out (the `finally`, the timer, the fallback file, the first-frame rule, the paused rule, the gap rule and others).
- `tests/check_watchdog.py` runs the real engine with real ffmpeg and a real clip. It breaks playback on purpose and times how long the NDI picture stood still. Results over five runs, on `testsrc.mp4` and a local copy of Light Tunnel:

| Break | Picture still for | Who fixed it |
| --- | --- | --- |
| compose thread ended, its own restart blocked (as on 2 Nov) | 0.04 to 1.00 s, 11 times | watchdog |
| compose error while logging is broken | 0.04 to 0.05 s | compose thread's own restart |
| Program decoder thread ended | 0.67 to 1.50 s | watchdog |
| Program ffmpeg frozen | 9.7 s | decoder's 8 s rule; no watchdog line |
| Program ffmpeg frozen, decoder's 8 s rule switched off | 30.9 s (testsrc), 32.0 s (Light Tunnel) | watchdog's 30 s rule |
| preview, mix, take, blackout and back | (no break) | no watchdog line |

## Not settled

- Not tried in the real app yet. The pane open since 2 Nov runs the old code and was left alone.
- The cause of the 2 Nov stop is still not known. The next one should leave a traceback in the log.
- A stuck (not ended) compositor is only logged, not fixed. None has been seen.
- If the GUI thread itself hangs, the watchdog cannot run, and the window would show Not Responding. The compose thread's own restart does not need it.
- The iPad (LAN) server is not watched.

## Checking after the restart

After quitting the pane from the tray and opening it again:

1. Check the new code started. The last lines should include `start:` and `watchdog: on` with today's time:

   ```powershell
   Get-Content "$env:LOCALAPPDATA\Media Background Pane\session.log" -Tail 5
   ```

   If the file is missing, look in `$env:TEMP\Media Background Pane session.log`.

2. With a looping clip on Program, run the speed and picture check in `playback-speed.md`.

3. To see the watchdog repair things, run its check. It uses its own copy of the engine and a test clip, so it does not touch the open pane or NDI. It takes about 40 seconds and should end with `ALL PASSED: 6 of 6`:

   ```powershell
   cd "C:\Users\avbri\Documents\Media Background Pane"
   .venv\Scripts\python.exe tests\check_watchdog.py
   ```

4. After a service, list any repairs. Nothing printed means nothing needed repairing:

   ```powershell
   Select-String -Path "$env:LOCALAPPDATA\Media Background Pane\session.log" -Pattern "watchdog: the|compose:"
   ```

