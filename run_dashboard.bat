@echo off
REM Starts the local dashboard web app and opens it in your browser.
REM Use this offline (not during a stream) to sync your songlist and pick artwork.
cd /d "%~dp0"
python -m app.dashboard
pause
