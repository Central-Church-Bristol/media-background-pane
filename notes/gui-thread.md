# GUI-thread stalls (5 Nov 2026)

Fixed in code on 5 Nov 2026 in the `worktree-perf-1080p` worktree. The running pane does not have it until it is restarted with this code.

## Why it matters

The MIX and DIM fades run on the GUI thread:

- `ChaseSlider` in `widgets.py` ticks every 8 ms and calls `engine.set_mix` / `engine.set_dimmer`.
- Each tick moves the knob by the time since the last tick, but by no more than 50 ms of it.
- So when the GUI thread is blocked for 0.8 s, the knob does not jump. The fade stands still for 0.8 s, then carries on, and finishes 0.8 s late.
- The iPad's knobs are moved by the same timers, so they stop too, even while the pane is hidden.

Anything slow on the GUI thread therefore shows on the projector as a fade that stops and starts.

## What blocked it, and for how long

Measured on 5 Nov on the church PC, outside the running pane. The scripts are in the helper session's scratchpad. They were read-only: they listed the Drive folder, read ProPresenter's logs and used a scratch thumbnail cache. ProPresenter was not running.

| Work | How often | GUI thread before | Now |
| --- | --- | --- | --- |
| Video Input status (`_sync_video_input_status`) | every second | 7 ms process snapshot, plus one request with a 0.8 s timeout. **2.4 s** while ProPresenter's API is not answering (see below) | ProPresenter thread |
| Start-up switch-on (`_try_trigger_video_input`) | every 2 s until it is on | status, input list and trigger. **4.8 s** while the API is not answering | ProPresenter thread |
| The operator's Video Input click (`_toggle_video_input`) | per click | up to 0.8 s a request; 2.4 s while the API is not answering | ProPresenter thread |
| Library refresh (`refresh_library`) | at start, after Settings and drops, and 0.4 s after any change in the media folder or ProPresenter's settings folder | **61 ms, up to 205 ms**, for 28 files with every thumbnail cached; 99 ms with none cached | 0.04 ms to ask, 3.7 ms (up to 5.3 ms) to show it; 11 ms when the buttons are made. The scan (35–47 ms) is on the library thread |
| Copying dropped files onto Drive (`_import_dropped_files`) | per drop | as long as the copy takes; clips here are 0.3–6.8 GB. Not timed | library thread |
| Finding ProPresenter's window (`_stack_above_propresenter` 10 a second, `_snap_to_propresenter` 2.5 a second) | 15 lookups a second | 7 ms each, for a process snapshot: about **100 ms of every second** while the pane is up. 17 ms a second even with ProPresenter closed | 0.4 ms each; nothing with ProPresenter closed |
| Process priority (`prefer_livestream_apps`, in `_check_workspace`) | every second | 7 ms, another process snapshot | ProPresenter thread |

**Why 2.4 s.** On Windows, a connection to a port with nothing listening on 127.0.0.1 is not refused at once. Windows tries again, and refuses only after 2.0 s, so with a 0.8 s timeout every attempt waits the full 0.8 s. `find_api_base()` tries three ports (1025, 50000, 50001) until one answers, and remembers the answer only once there is one. So while ProPresenter runs and its API is not up yet, each status check blocked the GUI thread for 2.4 s. That is every second, and 4.8 s for each start-up attempt, so the fades barely moved. It happens at every ProPresenter start, until its API answers. It would never end if the API were switched off or on another port.

**Why the library refreshed so often.** The folder watcher also watches `%APPDATA%\RenewedVision\ProPresenter`, for `PathSettings.proPaths`. Any file ProPresenter adds or renames there schedules a library scan as well. That scan reads Google Drive: a `stat` and a `resolve()` for each file's thumbnail name, twice per file, plus the cached thumbnails.

## What changed

All in `media_background_pane/main_window.py`, unless named.

1. **`BackgroundCalls`**
   - It is a thread with a queue. `submit(kind, call, on_done)` is called on the GUI thread. The call runs on the thread, in order, and `on_done(result)` runs back on the GUI thread, through a queued Qt signal.
   - A call of a kind already waiting or running is skipped, so requests cannot pile up. Clicks pass `single=False`, so every click is sent, in order.
   - If a call raises, the error is logged with `log_exception` and `on_done` gets a failure value. Before, such an error, for example an `http.client` error that is not an `OSError`, ended the GUI-thread slot.
   - If the thread itself ends, the next `submit` starts a new one and logs `<name> thread: it had stopped; started it again`.
   - It has no Qt parent, so Qt never deletes it while its thread could still emit. Quitting stops it.
   - There are two of them:
     - `self._pp_calls`, the "propresenter calls" thread, for ProPresenter's API and the priority snapshot;
     - `self._library_calls`, the "library calls" thread, for Drive.

     They are separate so that a 2.4 s ProPresenter probe never holds up the library, and a slow Drive never holds up a Video Input click.
2. **Video Input** runs on the ProPresenter thread. What the operator sees, and the session-log lines, are as before:
   - It is switched on again for each ProPresenter run.
   - The operator's off holds for the run.
   - The button follows ProPresenter, once a second.
   - The `video input:` lines are the same.

   What is new, because requests now overlap with clicks:
   - **No pile-up.** A status check or start-up attempt still waiting or running is not sent again. None is sent while a click's request is on its way.
   - **Out-of-date answers are dropped.** `_video_input_serial` moves on at each click, each new ProPresenter run, and when the start-up attempt succeeds. An answer to a request sent before that is not used. So a status answer from before a click cannot flip the button back, and an attempt for a run that ended cannot count for the new one.
   - **A waiting start-up attempt gives way.** It checks the serial before each of its requests. So if the operator switches the input while the attempt is still waiting, it sends nothing.
   - **The button shows a click at once.** If ProPresenter does not take the change, the button goes back to what ProPresenter last said (`_video_input_known`). Before, the window froze until the answer.
   - **A click made during a freeze could do the opposite.** Before, if the start-up attempt switched the input on during a freeze, a click made then (meant as "on") was handled afterwards and switched it off.
3. **Library**
   - `refresh_library` hands the scan to the library thread (`scan_library`). It returns at once.
   - `_show_library` applies the result on the GUI thread.
   - A refresh asked for during a scan runs once that scan is back. Files just copied in (`extra`) wait for it.
   - Thumbnails for stills are made on the library thread. Videos with no thumbnail yet still go to the `Thumbnailer`.
   - Dropped files are copied on the library thread. The wait cursor stays up until the copy is done, and the pane keeps working meanwhile.
4. **ProPresenter's window**
   - `propresenter_main_window`, `propresenter_window_rect` and `propresenter_main_hwnd` in `propresenter.py` take the process ids when they are known.
   - The window code passes `self._pp_pids`, from the workspace check, once a second. A new ProPresenter run is still found within a second, and the pane is only shown once that check has seen it anyway.
5. **The stall detector** (`GuiStallDetector`): see below.

## The new session-log line

```
2026-11-09 18:42:10 gui: the window thread was blocked for 0.3 s; it was in _read_log_workspace (propresenter.py:490) < open_workspace (propresenter.py:457) < _workspace_decision (main_window.py:1681) < _check_workspace (main_window.py:1762)
```

That example is made up, to show the shape. It is one of the reads left alone below, on a cold disk. A real one, from the window test, which blocks the GUI thread with a 0.8 s sleep: `gui: the window thread was blocked for 0.8 s; it was in block_the_gui_thread (test_gui_thread_window.py:207)`.

**How it is measured.** A timer fires every 100 ms on the GUI thread. When a beat comes late, the GUI thread was busy with something else for that long, and the fades stood still.

**What the line says:**
- **"blocked for 0.8 s"**: the gap between two beats less half a beat. That is right to about 0.05 s. Only blocks over 0.25 s are written.
- **"it was in ..."**: where the GUI thread was. A watcher thread looks at it once a block has lasted 0.25 s. The innermost call comes first, up to five calls, each `function (file:line)`, stopping at Qt's event loop.
- **"it was running no Python code (Qt's own work, or waiting for another thread)"**: the GUI thread was inside Qt itself, for example painting, a media player or a native dialog, or it was waiting for the Python lock while another thread held it.
- **No "it was in" part**: the watcher could not look in time. That happens when a block holds the Python lock throughout, for example one long C call.

**Throttling and sleep:**
- Up to five lines a minute. Beyond that they are counted, and the count goes in with the next line that has room: `[12 more times in the last 61 s; this was the last]`.
- Time with the PC asleep does not count. The detector's clock (`QueryUnbiasedInterruptTime`) stops during sleep.

**What it costs:**
- 3.5 µs per beat, ten beats a second.
- The watcher wakes twenty times a second to compare two numbers.
- Looking at the stack takes 0.05 ms, and happens only during a block.
- The clock moves in 1 ms steps.

**To list the stalls after a service:**

```powershell
Select-String -Path "$env:LOCALAPPDATA\Media Background Pane\session.log" -Pattern " gui: | thread: " | Select-Object -Last 40
```

Python 3.14 gives OS threads their Python names. So a `py-spy dump` shows the new threads as `propresenter calls`, `library calls` and `gui stall watch`.

## Left alone, with what it costs

None of these blocks for 50 ms in normal use, or they are rare, or the files are not mine:

- **The workspace check's own process snapshot** (`_read_propresenter`): 7 ms a second. The show/hide decision needs it there and then.
- **ProPresenter's log, while a new run has not logged its workspace yet** (`open_workspace`):
  - It is read every second until the line appears, about 4 s after ProPresenter starts, or for 30 s if the line never comes. After that the answer is kept for the run.
  - Today's two logs (650 KB and 350 KB): 10 ms, or 46 ms the first time after a reboot.
  - Two 4 MB logs, the read limit (the biggest seen so far is 2.5 MB): 51 ms, up to 73 ms.
  - If it ever shows up in the new log line, keep each file's matches by size and date, and read only new bytes.
- **The Thumbnailer, for a video with no thumbnail yet** (a new clip, or one whose size or date changed):
  - It needs QMediaPlayer, on the GUI thread.
  - Making each thumbnail took 19–49 ms in Python.
  - In 12 new clips, Qt's media player itself blocked the GUI thread twice, for 335 ms and 193 ms, with no Python code running.
  - Only for new clips. Moving it would mean ffmpeg in a subprocess instead.
- **Stills from Drive** (`bus.py`, the pipeline's file):
  - `Bus.load` reads a still on the GUI thread: Lamb.png took 12 ms, and 240 ms the first time.
  - Changing quality or scale mode reads it again.
  - Noted for the pipeline work.
- **Small items:**
  - `config.save()`: 0.25 ms (the same JSON to a local file).
  - The LAN pump every 50 ms: a dictionary, microseconds.
  - The 5 fps JPEG snapshots: 0.18 ms each.
  - `lan_ips()`: 0.2 ms, 7 ms the first time.
  - Watching or unwatching the Drive folder: 1.4 ms, 15 ms the first time.
- **Not measured here:** the engine's own GUI-thread work, such as the 1 s watchdog and `load_preview`. Those files are being rebuilt. The new line will show them if they block.

## Tests

| File | Tests | What they use |
| --- | --- | --- |
| `tests/test_gui_thread.py` | 26 | the real `BackgroundCalls` thread; a fake ProPresenter whose every request takes 0.8 s; the real library scan on a temporary folder; the stall detector on a fake clock and on a real 0.8 s block |
| `tests/test_gui_thread_window.py` | 5 | the real `MainWindow`, in its own process on Qt's offscreen platform, with a fake engine, LAN remote and tray |
| `tests/test_pane_visibility.py` | 21, adapted | its stand-in now has an inline `BackgroundCalls`, and its fake `propresenter_main_window` takes the process ids |

`tests/test_gui_thread.py` shows:
- With a 0.8 s ProPresenter, the GUI thread's calls return in under 0.1 s, and a 10 ms heartbeat never waits 0.2 s.
- Answers are applied on the GUI thread, and requests are made on the ProPresenter thread.
- Status checks do not overlap.
- Clicks go to ProPresenter one at a time, in order, without flicker.
- The operator's off stops a waiting start-up attempt.
- Out-of-date answers are dropped, and a refused click goes back.
- Library refreshes during a scan merge into one more scan.
- The detector writes one line, with where the thread was, for a real 0.8 s block, keeps quiet otherwise, and is throttled.

The window test runs the real `__init__` and checks that:
- the library arrives from its thread;
- ProPresenter is asked only from its thread;
- a 0.8 s block on the GUI thread puts `gui: the window thread was blocked for 0.8 s; it was in block_the_gui_thread (...)` into the session log;
- quitting stops all three threads.

All tests use temporary session logs. A guard makes any real ProPresenter request fail.

127 tests pass. That is the 94 from before, these 31, and 2 that the pipeline work added to `test_decoder_pacing.py` meanwhile.

## Not tested

- With a real ProPresenter: it was not running, and the tests never ask it. Next service, check that Video Input still switches on at start and follows ProPresenter. Then list the `gui:` lines with the command above.
- A real copy of a large clip onto Drive in the background.
