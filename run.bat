@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  py -3 -m venv .venv
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)
".venv\Scripts\python.exe" -c "from media_background_pane.hw_decoder import ensure_ffmpeg; ensure_ffmpeg()"
start "" ".venv\Scripts\pythonw.exe" main.py %*
