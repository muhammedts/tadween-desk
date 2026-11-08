@echo off
rem Installs the speech models Tadween Desk needs into app\models (about 1.04 GB to download),
rem checking every file's size and SHA-256 against app\models\manifest.json. Files that are
rem already in place are skipped. Options are passed on, e.g.:
rem   "Install Models.cmd" -DryRun                show what would be downloaded
rem   "Install Models.cmd" -From D:\my-downloads   use files you downloaded yourself
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0app\scripts\install_models.ps1" %*
set "code=%errorlevel%"
if not defined TADWEEN_NO_PAUSE pause
exit /b %code%
