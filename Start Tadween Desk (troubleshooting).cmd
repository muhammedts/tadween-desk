@echo off
rem Same as "Start Tadween Desk.cmd", but keeps this console window open so any
rem start-up error stays visible. Error messages never contain transcript text.
cd /d "%~dp0app"
if not exist ".venv\Scripts\python.exe" (
  echo The Python environment app\.venv was not found. From the tadween-desk folder, run:
  echo   powershell -ExecutionPolicy Bypass -File app\scripts\bootstrap.ps1
  echo and then "Install Models.cmd". See README.md.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m meeting_minutes app %*
echo.
echo The app has closed (exit code %errorlevel%).
pause
