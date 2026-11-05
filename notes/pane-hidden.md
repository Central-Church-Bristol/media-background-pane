# Pane vanished during setup (4 Nov 2026)

Fixed in code on 4 Nov 2026; nothing is committed. The pane was restarted with these changes at 23:20 that evening (pid 22024). ProPresenter has not been opened since, so the new detection has not yet seen it start.

## What happened

Reported by the user. Times are from ProPresenter's log and the pane's `session.log`, read only.

- **15:26:** MBP started (pid 16560, `--startup`).
- **18:54:23:** ProPresenter started with Evening Service (P23256).
- **18:58:34:** ProPresenter logged "Input 1: Media Background" triggered. The user says the background was working.
- **18:59–19:10:** ProPresenter logged a video input device change for the vicar's MacBook's NDI source 63 times, about every 4 s.
- **A few minutes after setup:** the background was frozen in ProPresenter and the pane had gone.
  - It stayed gone after minimising and maximising ProPresenter.
  - The tray icon was still there.
- **About 19:08:47:** the user quit MBP from the tray.
- **19:09:58:** the user started MBP again (pid 24044). It was fine for the rest of the evening.

What else the logs show, and what they don't:

- `session.log` has nothing between 15:26:52 and 19:09:58, so nothing recorded why the pane went, and the watchdog did not see a stopped compositor or decoder.
- Google Drive kept reading the clip until the quit, so the decoder was still reading.
- Windows logged no crash and no display-driver event.
- It is not proven whether ProPresenter's input froze under the device changes, or whether MBP's NDI output froze.

## What was weak

1. **Workspace detection guessed.**
   - `active_workspace_name()` asked ProPresenter's API every second from the GUI thread, probing several ports with 0.8 s timeouts.
   - Its reading of `PathSettings.proPaths` never worked. `_path_settings_workspaces()` always returned nothing, because the `unicode_escape` decode mangles the backslashes in the paths. So it always ended up using the `Base=` line.
   - "ProPresenter is not running" came from a process snapshot. If that snapshot failed even for a second, the pane was hidden.
2. **Nothing logged the pane being shown or hidden,** so there was no way to tell why it went.
3. **Video Input was switched on only once per MBP run,** not per ProPresenter run. `_video_input_triggered` was reset only at MBP start-up.
4. **There was no way to get the pane back short of restarting MBP.** Left-clicking the tray icon showed it, but the next second's check could hide it again.

## The fixes

1. **The workspace comes from ProPresenter's own log** (`propresenter.open_workspace`).
   - **Where it looks:** `%LOCALAPPDATA%\RenewedVision\ProPresenter\Logs\Log.log` (then `Log.1.log`). Each run logs exactly one `INFO - Opened [...\Configuration\Workspace]` line, tagged with its process id. That id is matched to the running ProPresenter.exe.
   - **Reading it:** the file is read as bytes; the byte-order mark and NUL bytes are dropped. Only the `Configuration\Workspace` line counts; Stage, Themes and the other `Opened [...]` lines don't. The default workspace (`Documents\ProPresenter`) is reported by that name and is not Evening Service.
   - **Caching:** the answer is kept for the run. A new run, even with the same process id, is read again.
   - **Before the line is logged:** the workspace is "unknown", and the pane is left as it is.
   - **Falling back to `Base=`:** after 30 s with no line, or at once if the log cannot be read, the `Base=` line is used, and the log says so.
   - **No network:** none of this asks ProPresenter over the network. The old scoring code and the broken `PathSettings` decoder are gone.
   - **Video Input status:** the once-a-second status check now remembers ProPresenter's API address for the run. That is one request a second instead of a probe of every port. It probes again if the address stops answering, or ProPresenter restarts.
2. **Every show and hide is logged, with its reason.** Lines look like this:
   - `propresenter: running: P23256`, `propresenter: started: P24044`, `propresenter: stopped: P23256`
   - `workspace check: show: ProPresenter P23256 opened workspace Evening Service (from its log)`. This is written when the decision changes. The source is "from its log", "from PathSettings Base=, as the log had no answer", or "no change" while unknown.
   - `pane: shown`, `pane: hidden`, `pane: put back on screen`
   - `pane: Windows had hidden the pane; showed it again (its owner window 0x9A0 is hidden)`
   - `pane: stacked above ProPresenter window 0x9A0, 1920x1080 at 0,0`, written when the window or its size changes
   - `pane window: on screen; 1200x250 at 0,830; owner 0x9A0`, written when the pane looks different to Windows: hidden by Windows, minimised, cloaked (kept off screen by Windows), moved, or a new owner. If the pane vanishes without the check hiding it, this line says how.
   - `pane: shown from the tray menu ...`, `video input: ...`

   Each kind of line gets up to 5 lines per 10 s. More than that is counted, and goes in as one line, for example `[95 more times in the last 15 s; this was the last]`. A flickering window cannot flood the log.
3. **Video Input:**
   - **New run:** when a new ProPresenter run appears, the pane switches its first video input on again, retrying every 2 s as at start-up.
   - **Operator's off:** if the operator switches it off, it stays off for the rest of that ProPresenter run, even if the start-up attempt was still retrying.
4. **The tray menu starts with "Show pane".**
   - It un-minimises the pane, shows it, moves it back onto a screen if it is off all of them, and stacks it above ProPresenter, whatever the workspace check says.
   - It stays up until ProPresenter closes or restarts, or the check agrees the workspace is open. Both are logged.
5. **The session log can no longer lose entries the way it did tonight** (see "Found while doing this"):
   - A failed folder creation no longer stops an entry; opening the file decides.
   - The fallback log's path comes from `TEMP` directly, without `tempfile`.
   - Entries that reach no file are kept in memory, up to 200, and written with their own times when the log works again.

## Found while doing this (not fixed)

- **Lost log entries:** at 22:27:48 the pane running now wrote:

  ```
  log: 54 earlier entries could not be written (session log (FileExistsError: [WinError 183] ...
  'C:\Users\avbri\AppData\Local\Media Background Pane'); fallback log (TypeError: first argument
  must be string or compiled pattern))
  watchdog: the Program decoder had stopped (Leaves.mov); started it again
  ```

  Neither error should happen in a healthy process:
  - `Path.mkdir(exist_ok=True)` raises FileExistsError only when `os.path.isdir` then says the folder isn't there, and the folder exists.
  - The TypeError comes from `re.compile` rejecting the fixed pattern string in `os.path.expandvars`, which `tempfile.gettempdir()` calls on first use.

  Something inside the pane process went wrong, and the 54 entries are gone. The last good write before 22:27:48 was the 19:09:59 `watchdog: on` line, so the failures may span almost the whole run since the 19:09 restart, not a moment. Some of the 54 may have been earlier decoder restarts.

  It wasn't the PC running short: Windows logged no low-memory or application-error events that evening (32 GB of RAM, about 23 GB free). It might have been a resource running out inside the pane process itself, or memory corruption by a native library. The Program decoder thread stopped at 22:27:47 and the watchdog restarted it.

  At 23:02 the process was using 676 MB of private memory, steady over 20 minutes, with 679 handles and 51 threads, and its Program picture was moving. With the logging fix, entries like these are kept and written later, so next time the reason should survive.
- **Video Input status still polled:** the check still asks ProPresenter once a second from the GUI thread. It is now one request, with a 0.8 s timeout.
- **Tray left-click:** left-clicking the tray icon still only expands or collapses the pane. "Show pane" is the reliable way back.

## Evidence (4 Nov 2026)

- **ProPresenter's real logs**, read only:
  - P23256 reads as Evening Service in 3 ms; P14124 reads as the default workspace in 8 ms, from `Log.1.log`.
  - Every log from 31 Oct has exactly one workspace line per run, about 142 KB in.
  - These files have no NUL bytes; the tests have them anyway.
  - `Base=` reads as Evening Service.
- **Tests:**

  | File | Tests | What they use |
  | --- | --- | --- |
  | `tests/test_workspace_detection.py` | 21 | fake log folders, process ids and clock; no network |
  | `tests/test_pane_visibility.py` | 21 | MainWindow's own methods on fake windows; one test end to end through the real log reader |
  | `tests/test_error_log.py` | 20 (8 new) | — |

  All 94 tests pass.
- **Deliberate breaks:** 18 safeguards were broken one at a time in a copy of the code, and the tests failed every time:
  - any `Opened` line taken as the workspace;
  - NUL bytes kept;
  - another run's line used;
  - no `Base=` fallback;
  - no wait for a new run;
  - no re-arming of Video Input;
  - the operator's off not holding;
  - the check hiding a pane shown from the tray;
  - Windows blamed for the pane's own hide;
  - the throttle never holding back;
  - a mkdir error stopping an entry;
  - unwritten entries dropped;
  - and others.

## Restarted (4 Nov 2026, 23:19–23:20)

The supervising session restarted the pane, with the user's OK, without forcing anything:

1. WM_CLOSE went to the pane's own window. Its closeEvent ran and logged `23:19:27 stop: the pane is closing`, and the process stayed up, as expected.
2. WM_ENDSESSION (wParam 1), sent with SendMessageTimeout to a Qt window that still existed, made Qt quit as the tray Quit does. Both processes exited within seconds.
   - PostMessage does not work for WM_ENDSESSION.
   - Qt had recreated the pane's own window after WM_CLOSE, so the old handle was gone.
3. Started again with explorer.exe on `Media Background Pane.exe`: pid 22024.

The new code's lines are in `session.log`:

```
23:20:39 start: pid 22024, pythonw.exe, Python 3.14.6
23:20:40 propresenter: not running
23:20:40 pane: not shown at start: ProPresenter is not running
23:20:41 pane window: hidden by the pane; 1500x286 at 1920,794; no owner
```

The NDI source `DESKTOP-AR8BC8U (Media Background Pane)` was back, along with the two cameras.

## Not tested

- **When ProPresenter opens:** not seen yet; it has not been opened since the restart. Steps 2–4 below check it.
- **Reading the log while ProPresenter runs:** not checked. If ProPresenter keeps `Log.log` unreadable while it runs, detection falls back to `Base=` at once and the log says so.

## Checking after the restart

Step 1 was seen at 23:20 on 4 Nov; steps 2–4 wait for the next time ProPresenter opens.

1. With ProPresenter closed, the log should end with `propresenter: not running` and `pane: not shown at start: ProPresenter is not running`:

   ```powershell
   Get-Content "$env:LOCALAPPDATA\Media Background Pane\session.log" -Tail 10
   ```

2. Open ProPresenter with Evening Service. Within a few seconds the same command should show:
   - `propresenter: started: P...`
   - `workspace check: show: ProPresenter P... opened workspace Evening Service (from its log)`
   - `pane: shown`
   - `pane: stacked above ProPresenter window ...`
   - a little later, `video input: switched ProPresenter's first video input on`

3. Right-click the tray icon: "Show pane" is the first item.

4. After a service, list everything the pane logged about itself and ProPresenter:

   ```powershell
   Select-String -Path "$env:LOCALAPPDATA\Media Background Pane\session.log" -Pattern "pane|propresenter|workspace check|video input" | Select-Object -Last 40
   ```
