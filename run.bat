@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  py -3 -m venv .venv
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)
if not exist ".venv\Scripts\pythonw.exe" (
  echo Media Background Pane couldn't set up its Python ^(.venv^). Is Python 3 installed?
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -c "from media_background_pane.hw_decoder import ensure_ffmpeg; ensure_ffmpeg()"
rem Media Background Pane.exe runs Python inside itself, so Task Manager shows the pane (18 Nov)
if exist "Media Background Pane.exe" (
  start "" "Media Background Pane.exe" %*
) else (
  start "" ".venv\Scripts\pythonw.exe" main.py %*
)
