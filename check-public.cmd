@echo off
title Check the co-op portal from the internet
cd /d "%~dp0"
python check-public.py
echo.
pause
