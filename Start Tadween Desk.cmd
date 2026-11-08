@echo off
rem Starts Tadween Desk from this folder with the Python environment in app\.venv.
rem Nothing is installed or downloaded. Transcripts are saved on this PC in
rem %LOCALAPPDATA%\MeetingMinutes (the app's internal name). Options are passed on,
rem e.g. --data-dir D:\demo to use another History folder.
cd /d "%~dp0app"
if not exist ".venv\Scripts\pythonw.exe" (
  echo The Python environment app\.venv was not found. From the tadween-desk folder, run:
  echo   powershell -ExecutionPolicy Bypass -File app\scripts\bootstrap.ps1
  echo and then "Install Models.cmd". See README.md.
  pause
  exit /b 1
)
start "Tadween Desk" ".venv\Scripts\pythonw.exe" -m meeting_minutes app %*
