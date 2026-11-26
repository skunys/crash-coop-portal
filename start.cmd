@echo off
rem Crash: Mind over Mutant co-op portal -- starts MediaMTX, the ffmpeg capture
rem and the web server in one window. Close this window to stop everything.
title Crash co-op portal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python 3 was not found on PATH.
  pause
  exit /b 1
)

python portal.py
echo.
echo Portal stopped. Logs are in the logs folder.
pause
