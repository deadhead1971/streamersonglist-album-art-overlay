@echo off
REM Single fetch — reads the song file once, writes the artwork, then exits.
REM Handy for triggering per-song from TouchPortal instead of the watch loop.
cd /d "%~dp0"
python -m app.watcher
