@echo off
REM ============================================================
REM  Gold Brain AI PRO - AI + live-news + Market Intelligence server
REM  Double-click this file to run the service, and LEAVE IT OPEN.
REM    Dashboard : http://127.0.0.1:8008/
REM    EA bridge : http://127.0.0.1:8008/predict
REM  It auto-restarts if it ever crashes. Close the window to stop.
REM ============================================================
cd /d "%~dp0"
title Gold Brain AI - server (keep open)
:loop
echo.
echo [%date% %time%] Starting server on http://127.0.0.1:8008  (dashboard at /)
python ai_server.py --port 8008
echo.
echo [%date% %time%] Server exited. Restarting in 3s...  (press Ctrl+C to quit)
timeout /t 3 /nobreak >nul
goto loop
