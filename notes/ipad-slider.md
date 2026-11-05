# iPad remote: MIX and DIM knobs out of line

Fixed 3 Nov 2026 in code, uncommitted.

- The iPad half is in `media_background_pane/web/app.js`. The pane reads its web files from disk on every request, so this half is already live for any iPad that loads or reloads the page, even on the pane that has been running since 2 Nov.
- The PC half (`lan_server.py`, `main_window.py`, `widgets.py`) arrives when the pane is restarted on Wednesday 4 Nov.
- Until then, the new page still works with the old pane. Its knob trails the PC knob a little (see "Evidence") and never runs ahead or jumps.

## What it looked like

Reported by the user. The iPad fade slider looked as if it showed only the 0.2–0.8 part of the PC slider.

When the PC slider went below 0.2 or above 0.8, the iPad knob kept jumping back to about 0.1 or 0.9 and creeping on again, until the PC slider reached 0 or 1. DIM did the same; it uses the same code.

## Why

The old page did not draw the PC knob. It drew its own animation: each knob ran toward the target at a fixed speed of one track length per fade time. It looked at where the PC knob really was only once its own animation was within 0.02 of the target.

The PC knob does not move at that fixed speed:

- It eases in and out (`ChaseSlider._tick`). The page's guess was 0.045 out even on a plain 10 s fade.
- It slows down while the PC's main thread is held up, because each tick moves it at most 50 ms worth. This has been deliberate since 20 Sep (41d4455, "Keep MIX and DIM knobs smooth instead of hitching"); the old iPad page dates from the same change. Once a second, that thread waits on ProPresenter: `_sync_video_input_status` calls `find_api_base` and `/v1/status/layers`, synchronous requests with 0.8 s timeouts. A slow ProPresenter would slow the PC knob. That was not measured; ProPresenter's main app was not running on 3 Nov.
- It has to slow down and turn round when the target changes mid-fade.
- It follows the mouse when dragged on the PC.

So the iPad knob got ahead and reached the end early. Then two rules fought:

1. "Step toward the target" took the knob to within 0.02 of the end.
2. "Ease toward the PC knob" pulled it back toward where the PC knob really was.

Then rule 1 took over again. That loop is the jumping.

The two causes suspected in the report were checked and ruled out:

- **Mapping:** both sides put a value in the same place along the track. At rest, all 17 positions from 0 to 1 match exactly, including 0.001, 0.01, 0.99 and 0.999.
- **Sending:** the page sends targets only while a finger is on a slider (pointer events), at most every 50 ms, plus one on release. It sent nothing in any case without a touch.

## The fix

The PC now says how its knobs are moving, and the iPad draws its knobs from that.

- **PC:**
  - `_publish_lan_state` adds `mix_speed` and `dim_speed`, from the new `ChaseSlider.visual_speed()` (0 when the knob is still). Units: track lengths a second.
  - `LanHub.state()` adds `age_ms`: how long ago the pane published that state. The LAN pump publishes every 50 ms.
- **iPad (`app.js`):**
  - Each knob is drawn where the PC's last report puts it.
  - Between reports (about ten a second), the knob carries on the way the PC knob moves, for up to 0.5 s. The page repeats the PC's own ease-in and ease-out, using the fade time, and stops at the target.
  - It works out when a report was true from `age_ms` and half the round trip.
  - A report already more than 80 ms old when sent means the PC was busy and its knob had stopped. The page then carries it no further than one pump interval.
  - Small differences fade out instead of jumping: 0.08 s when the knob is ahead, 0.03 s when it is behind and only has to hurry on.
  - While the PC knob is on its way, a knob that is ahead by less than 0.3 s of travel waits instead of stepping back. Further ahead (for example after a Wi-Fi drop), it goes back.
  - A big jump while the PC knob is still (a take) is followed at once.
  - After a drag, the ghost stays where the finger let go until the PC's ghost gets there (at most 0.6 s), instead of flicking back to the PC's previous ghost.
  - An answer that arrives after a newer one is ignored.
- **Not changed:**
  - how the PC knob moves;
  - how touches map to values;
  - what the page sends;
  - the polling rate.

## Evidence (3 Nov 2026)

`tests/check_ipad_slider.py` runs the real page script (in Qt's JavaScript engine, with a small stand-in for the browser) against:

- the real PC slider widgets;
- MainWindow's real `_publish_lan_state`, `_pump_lan` and `_apply_lan_command`;
- a real `LanHub`.

It uses a simulated clock, Wi-Fi delays of 2–12 ms each way, and Qt timers that wander by a millisecond or two. Gaps are fractions of the track; 0.02 is about 9 px on an iPad in landscape, under half the knob.

| Case | Old page | New page |
| --- | --- | --- |
| Both knobs at rest, 17 values from 0 to 1 | exact | exact |
| FADE up and down, 10 s | 0.045 | 0.001 |
| Same, PC busy 0.25 s every second | 0.179, 18 jumps back | 0.014 |
| FADE sent back down at about 0.6 | 0.212, 10 jumps back | 0.001 |
| FADE TO BLACK and back | 0.045 | 0.001 |
| 3 s fades | 0.052 | 0.004 |
| 1 s fades | 0.049 | 0.011 |
| MIX and DIM dragged to the ends on the iPad | 0.052; ghost flicked back 0.1 and 0.5 | 0.001 |
| MIX dragged on the PC | 0.234, 9 jumps back | 0.001 |
| Wi-Fi slow for 0.3 s every 2 s | 0.045 | 0.001 |
| Wi-Fi drops 3 s while the PC takes and starts the next fade | 0.700; lined up again after 8.2 s | 0.002; lined up again after 0.3 s |
| Take while an older answer is still on its way | (did not exist then) | 0.001 |
| PC freezes for 0.6 s now and then | (did not exist then) | 0.012 |
| iPad drawing 30 frames a second | 0.045 | 0.001 |

The old page fails 13 of 14 cases (all but "at rest"); the new page passes 14 of 14. The gaps are judged from 0.2 s after a move starts; see "Not settled".

`tests/check_ipad_slider_browser.py` loads the page over HTTP into Qt's browser (Chromium, off screen), in real time. On the PC side it uses the real sliders on their real timers and the real LAN request handler, on a free port on 127.0.0.1. The run takes 39 s: 3 s fades of MIX and DIM, a change of mind, 1 s fades, and two drags on the iPad.

| Page | MIX | DIM |
| --- | --- | --- |
| New, 5 runs | 0.005–0.007 apart, no jumps back | 0.002–0.003 apart |
| Old | 0.179 apart, 24 jumps back | 0.073 apart |

In every run, both pages sent commands only during the drags (8 each).

The new page on the pane running now, without `mix_speed`, `dim_speed` or `age_ms`, was simulated:

- no jumps back and no commands without a touch in any case;
- trails by up to 0.017 on 10 s fades, 0.052 on 3 s fades, 0.113 on 1 s fades, and 0.041 with slow Wi-Fi.

Each piece of the fix was broken in turn to see whether the simulated check notices. 15 of 17 breaks fail it. Among them:

- no carrying between reports;
- a steady speed instead of the PC's ease-in and ease-out;
- ignoring `age_ms`;
- carrying on while the PC is busy;
- never waiting, or waiting however far ahead;
- the ghost not waiting;
- late answers not ignored;
- gliding instead of snapping after a take;
- the PC sending no speeds, no age, or speeds in the wrong units.

The two it misses only change how smoothly a tiny correction looks: starting a correction from the last drawn frame instead of the present moment, and making corrections jump instead of fade.

Tests: `tests/test_ipad_slider.py` (13), which runs the simulated check in its own process. The other tests use QCoreApplication and the check needs a QApplication. All 44 tests pass.

## Not settled

- **Start delay:** the iPad hears about a move up to about 0.15 s after it starts, because it asks ten times a second. On a 1 s fade, its knob starts up to 0.1 s late and catches up within about 0.1 s more. On the default 10 s fade this is under 0.001 of the track. Asking twice as often would double the remote's Wi-Fi traffic: each answer is about 5.6 KB with 28 clips in the library.
- **PC busy:** while the PC's main thread is held up, the iPad knob can be up to about 0.015 ahead on a 10 s fade, and waits.
- **Real iPad:** not tried on a real iPad or in Safari. The browser check uses Chromium. The page uses nothing newer than the old page did.
- **PC knob timing:** the PC knob itself, and so the picture's fade, can take longer than the set fade time while the PC's main thread waits on ProPresenter. That is how the PC slider has worked since 20 Sep. It is not changed here.

## Checking after the restart

Restart the pane first, as in `playback-stall.md` under "Checking after the restart". Then:

1. Check that the restarted pane sends the new fields. This only reads.

   ```powershell
   $s = Invoke-RestMethod "http://127.0.0.1:8745/api/state"
   "mix_speed: $($s.mix_speed)   dim_speed: $($s.dim_speed)   age_ms: $($s.age_ms)"
   ```

   You should see three numbers: 0 and 0 while nothing moves, and an age between 0 and about 60. Blanks mean the old code is still running.

2. Optional: press FADE on the PC and run this straight away. It prints MIX and its speed five times a second for 6 s. During a 10 s fade, the speed should read about 0.111 and MIX should rise steadily.

   ```powershell
   1..30 | ForEach-Object { $s = Invoke-RestMethod "http://127.0.0.1:8745/api/state"; "MIX {0:N3}  speed {1:N3}" -f $s.mix, $s.mix_speed; Start-Sleep -Milliseconds 200 }
   ```

3. Run both checks. They use their own sliders and their own port and do not touch the pane.

   ```powershell
   cd "C:\Users\avbri\Documents\Media Background Pane"
   .venv\Scripts\python.exe tests\check_ipad_slider.py
   .venv\Scripts\python.exe tests\check_ipad_slider_browser.py
   ```

   The first should end with `ALL PASSED: 14 of 14`. The second takes about 40 s and should end with `PASSED: the iPad knobs lined up with the PC knobs`.

4. On the iPad, close the remote tab and open it again: a page already open keeps its old script. Then:
   - Press FADE on the PC and watch both MIX knobs. They should move together and stop together, with no bouncing near the ends.
   - Do the same with FADE TO BLACK and back for DIM.
   - Drag the iPad's MIX knob to one end and let go. The grey ghost should stay where your finger left it, and the white knob should follow the PC knob there.
