@echo off
REM Single fetch — reads the current song once (SSL queue by default, or the
REM song file if song_source is "file"), writes the artwork, then exits.
cd /d "%~dp0"
python -m app.watcher
