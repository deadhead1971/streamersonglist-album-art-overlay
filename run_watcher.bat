@echo off
REM Runtime watcher — launch this at stream start.
REM Default: polls your StreamerSonglist queue and writes the artwork image for
REM OBS (top of the queue = current song). If song_source is "file" it watches
REM your "current song" file instead.
cd /d "%~dp0"
python -m app.watcher --watch
pause
