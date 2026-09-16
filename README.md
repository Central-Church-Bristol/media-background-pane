# Media Background Pane

Windows app for picking stills and videos, fading them, and sending the result over NDI so ProPresenter can use it as a background — typically on the same PC as the livestream.

Built for [Central Church Bristol](https://github.com/Central-Church-Bristol).

## What it does

- Browses a media folder (images and video)
- Preview / program buses with MIX, DIM, a fade take, and Fade to Black
- MIX and DIM don’t jump: a darker grey ghost handle follows the pointer, and the live handle eases toward it at the fade duration (same motion as FADE, Auto Fade, and Fade to Black)
- Sends output as an NDI source named **Media Background Pane**
- Can sit on top of ProPresenter, start with Windows, and show itself when a named workspace is open
- Can trigger ProPresenter’s first video input on startup

Supported files: `.jpg` `.jpeg` `.png` `.webp` `.bmp` `.tif` `.tiff` `.gif` `.mp4` `.mov` `.m4v` `.avi` `.mkv` `.webm` `.wmv`

## Requirements

- Windows
- [Python 3](https://www.python.org/)
- [NDI Runtime](https://ndi.video/tools/)
- ProPresenter on the same PC (for the video-input workflow)

## Run

Double-click `run.bat`. The first launch creates `.venv` and installs dependencies from `requirements.txt`.

Or, after the venv exists:

```bat
.venv\Scripts\pythonw.exe main.py
```

Only one instance runs at a time.

## ProPresenter (same PC)

1. **Settings → Inputs → Video Inputs → +**
2. Device: **Media Background Pane (NDI)**
3. Choose **24 fps** and a mode that matches Quality in this app (960×540 for 540p, 640×360 for 360p)
4. Put that input on your background look
5. **Settings → Network → Enable Network** so the pane can auto-trigger the first video input on startup

Default quality is **540p / 24 fps**. Recoding source files to 540p 24fps H.264 (about 2–4 Mbps) keeps the livestream PC from decoding heavy 1080p media.

## Settings worth knowing

| Setting | Typical use |
| --- | --- |
| Media folder | Folder of backgrounds to browse |
| Quality / fps | Match the ProPresenter NDI input; 540p 24fps is the lightest practical default |
| Workspace name | Used to show the pane when that ProPresenter workspace is open |
| Always on top / Start with Windows | Keep the pane available during service |
| NDI name | Source name ProPresenter will see |

Settings are stored in `%LOCALAPPDATA%\Media Background Pane\settings.json`.
