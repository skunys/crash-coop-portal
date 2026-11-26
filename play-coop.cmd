@echo off
rem ===========================================================================
rem  Crash: Mind over Mutant -- co-op with a friend in a browser
rem  Starts the game, then the portal (MediaMTX + ffmpeg + the web server),
rem  and prints the link to send to your friend. Close this window to stop the
rem  portal; the game keeps running.
rem
rem  The portal is restarted automatically if it ever exits on its own: a dead
rem  portal means a dead stream for your friend. Each restart prints a NEW
rem  tunnel link (quick tunnels have no fixed name), so after a restart send
rem  her the new one -- it is also saved in current-link.txt.
rem ===========================================================================
setlocal

title Crash co-op portal
cd /d "%~dp0"

rem --- game location (edit these two lines if needed) ------------------------
set "GAME=C:\CrashMoM\program\Crash Mind over Mutant.exe"
set "GAMEDIR=C:\CrashMoM"

rem --- try common install locations if the above doesn't exist ---------------
if not exist "%GAME%" (
  for %%P in ("D:\CrashMoM" "E:\CrashMoM" "C:\Program Files\CrashMoM" "C:\Games\CrashMoM") do (
    if exist "%%~P\program\Crash Mind over Mutant.exe" (
      set "GAME=%%~P\program\Crash Mind over Mutant.exe"
      set "GAMEDIR=%%~P"
      goto :game_found
    )
  )
)
:game_found

if not exist "%GAME%" (
  echo.
  echo   The game was not found at:
  echo     %GAME%
  echo   Install it to C:\CrashMoM or edit GAME= and GAMEDIR= in this file.
  echo.
  pause
  exit /b 1
)

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo   Python 3 was not found on PATH -- the portal cannot start.
  echo.
  pause
  exit /b 1
)

rem --- 1. the P2 host (listens on 127.0.0.1:9099 for her client) -----------
echo   Starting the P2 host (listening on 127.0.0.1:9099)...
start "" /D "%~dp0" "python" "p2_host.py"
timeout /t 2 /nobreak >nul

rem --- 2. the game (only if it is not already open) ------------------------
powershell -NoProfile -Command "if (Get-Process -Name 'Crash Mind over Mutant' -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
if errorlevel 1 (
  echo   Starting the game...
  start "" /D "%GAMEDIR%" "%GAME%"
  rem give the game a moment to create its window before capturing
  timeout /t 8 /nobreak >nul
) else (
  echo   The game is already running.
)

rem --- 3. the portal, restarted if it exits unexpectedly ---------------------
:portal
echo   Starting the portal...
python portal.py
set "CODE=%ERRORLEVEL%"
echo.
echo   ======================================================================
echo    The portal stopped on its own (exit code %CODE%).
echo.
echo    Cleaning up what it left behind, then starting it again in 5 seconds.
echo.
echo    !! THE TUNNEL LINK HAS CHANGED. Once it is back, the new link is
echo       printed in this window and saved to:
echo         %~dp0current-link.txt
echo       Send your friend that new link -- the old one stops working.
echo.
echo    To stop for good: press Ctrl+C, then Y, or close this window.
echo   ======================================================================
echo.
rem Its children outlive a crashed parent: the old MediaMTX keeps holding the
rem RTSP/HLS ports and the old tunnel keeps the dead address.
taskkill /F /IM mediamtx.exe >nul 2>nul
rem (the capture ffmpeg exits by itself once its publish target is gone)
taskkill /F /IM cloudflared.exe >nul 2>nul
timeout /t 5 /nobreak >nul
goto portal
