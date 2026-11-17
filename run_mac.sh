#!/usr/bin/env bash
# Live_Clipping launcher — macOS (same as run.sh, kept separate for double-click use)
set -e
cd "$(dirname "$0")"
command -v ffmpeg >/dev/null 2>&1 || { echo "ffmpeg not found. Install with: brew install ffmpeg yt-dlp"; exit 1; }
command -v yt-dlp >/dev/null 2>&1 || python3 -m yt_dlp --version >/dev/null 2>&1 || { echo "Installing yt-dlp..."; python3 -m pip install --user yt-dlp; }
python3 live_clipping.py --serve --port 8899 --data-dir ~/YT &
PID=$!
sleep 2
echo "Live_Clipping panel: http://127.0.0.1:8899  (PID $PID)"
open "http://127.0.0.1:8899" 2>/dev/null || true
wait $PID
