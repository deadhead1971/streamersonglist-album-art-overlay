@echo off
REM Runtime watcher — launch this at stream start (e.g. from TouchPortal).
REM Watches your "current song" file and writes the artwork image for OBS.
cd /d "%~dp0"
python -m app.watcher --watch
pause
