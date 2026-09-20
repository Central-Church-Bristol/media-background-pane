# Media Background Pane

Windows app for picking stills and videos, fading them, and sending the result over NDI so ProPresenter can use it as a background — typically on the same PC as the livestream.

Built for [Central Church Bristol](https://github.com/Central-Church-Bristol).

## What it does

- Browses a media folder (images and video). Drop files or folders onto the library to copy them in; names that already exist get a ` 2`, ` 3`, suffix
- SIZE under the library scales the thumbnails. SCALE next to it is how media fills Preview, Program, and NDI:
  - **Scale to Fill** (default) — covers the frame and crops overflow
  - **Scale to Fit** — letterboxes so the whole image stays visible
  - **Actual Size** — 1:1 pixels, centred; pads if smaller, crops if larger
- Preview / program buses with MIX, DIM, a fade take, and Fade to Black
- MIX and DIM don’t jump: a darker grey ghost handle follows the pointer, and the live handle eases toward it at the fade duration (same motion as FADE, Auto Fade, and Fade to Black)
- Auto Fade is on each launch (not saved): clicking a clip loads Preview, then fades to Program
- Fade duration cycles 1, 3, 5, 10, 20, and 30 seconds (starts at 10s each launch)
- DIM and Fade to Black close a camera-style vignette: edges darken first (colour stays the same), then from about 80% faded the whole frame eases to black
- With DIM at black, FADE still takes preview to program without decoding (output stays black); PREVIEW and the thumbnail selection update so you can see the swap
- Sends output as an NDI source named **Media Background Pane**
- Stays in front of ProPresenter without covering other apps you bring forward; can start with Windows and show itself when a named workspace is open
- Can trigger ProPresenter’s first video input on startup
- iPad remote on the local network: same library, MIX/DIM, FADE, and video input as the desktop pane

Supported files: `.jpg` `.jpeg` `.png` `.webp` `.bmp` `.tif` `.tiff` `.gif` `.mp4` `.mov` `.m4v` `.avi` `.mkv` `.webm` `.wmv`

## Requirements

- Windows
- [Python 3](https://www.python.org/)
- [NDI Runtime](https://ndi.video/tools/)
- ProPresenter on the same PC (for the video-input workflow)

`run.bat` downloads **ffmpeg** into `vendor/ffmpeg/` on first launch. That is what does GPU video decode (D3D11VA). If ffmpeg is missing, the pane falls back to Qt software decode.

## Run

Double-click `run.bat`. The first launch creates `.venv`, installs dependencies from `requirements.txt`, and downloads ffmpeg if it is not already present.

Or, after the venv exists:

```bat
.venv\Scripts\pythonw.exe main.py
```

Only one instance runs at a time.

## ProPresenter (same PC)

1. **Settings → Inputs → Video Inputs → +**
2. Device: **Media Background Pane (NDI)**
3. Choose **24 fps** and a mode that matches Quality in this app (960×540 for 540p, 1920×1080 for 1080p)
4. Put that input on your background look
5. **Settings → Network → Enable Network** so the pane can auto-trigger the first video input on startup

Default quality is **540p / 24 fps**. GPU decode (D3D11VA via ffmpeg) is on by default so existing 1080p files can play without a heavy CPU decode. 720p and 1080p output are usable once that is working; match the ProPresenter NDI input to Quality. If GPU decode is off or ffmpeg is missing, recoding sources to 540p 24fps H.264 (about 2–4 Mbps) still helps a lot.

## iPad (same Wi‑Fi)

The Windows pane has to stay running — it still does NDI and decoding. On the iPad, open **Safari** to the **iPad URL** in Settings (also in the tray tooltip), typically `http://192.168.x.x:8745`. It must be **http://**, not https:// — there is no certificate.

Chrome on iPad often upgrades the address to https and then shows `ERR_SSL_PROTOCOL_ERROR`. Use Safari, or in Chrome type the full `http://…` URL and turn off **Always use secure connections**.

Use the same Wi‑Fi as the livestream PC. The first time the pane listens, Windows Firewall may ask to allow it — choose the **private** network.

The iPad page is a remote control for the same session: thumbnails, preview/program, MIX and DIM, FADE, Fade to Black, Auto Fade, duration, and Video Input. Media folder, quality, SCALE, and NDI name stay on the desktop pane.

## Settings worth knowing

| Setting | Typical use |
| --- | --- |
| Media folder | Folder of backgrounds to browse; drop files onto the library to copy them here |
| Quality / fps | Match the ProPresenter NDI input; 540p 24fps is still the default |
| Use GPU decode | D3D11VA via ffmpeg; falls back to software ffmpeg, then Qt |
| Workspace name | Used to show the pane when that ProPresenter workspace is open |
| Keep above ProPresenter / Start with Windows | Sit in front of ProPresenter, without covering other apps you bring forward |
| NDI name | Source name ProPresenter will see |
| SCALE (under the library) | Scale to Fill, Scale to Fit, or Actual Size; remembered between launches |
| iPad URL | Open `http://…:8745` in Safari on the same Wi‑Fi (not https); Windows Firewall may prompt once |

Settings are stored in `%LOCALAPPDATA%\Media Background Pane\settings.json`.
