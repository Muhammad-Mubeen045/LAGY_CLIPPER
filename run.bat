@echo off
REM Live_Clipping launcher — Windows
REM Requires: Python 3.11+ (python.org), ffmpeg in PATH (winget install Gyan.FFmpeg),
REM and yt-dlp (pip install yt-dlp).
cd /d %~dp0
where ffmpeg >nul 2>&1 || (echo ffmpeg not found. Install with: winget install Gyan.FFmpeg & pause & exit /b 1)
python -m yt_dlp --version >nul 2>&1 || python -m pip install yt-dlp
echo Live_Clipping panel: http://127.0.0.1:8899
start http://127.0.0.1:8899
python live_clipping.py --serve --port 8899 --data-dir %USERPROFILE%\YT
pause
