#!/usr/bin/env bash
# Live_Clipping launcher — macOS / Linux
set -e
cd "$(dirname "$0")"
command -v ffmpeg >/dev/null 2>&1 || { echo "ffmpeg not found. Install it first:"; echo "  macOS:  brew install ffmpeg yt-dlp"; echo "  Linux:  sudo apt install ffmpeg  &&  pip install yt-dlp"; exit 1; }
command -v yt-dlp >/dev/null 2>&1 || python3 -m yt_dlp --version >/dev/null 2>&1 || { echo "Installing yt-dlp..."; python3 -m pip install --user --break-system-packages yt-dlp || python3 -m pip install --user yt-dlp; }
python3 live_clipping.py --serve --port 8899 --data-dir ~/YT &
PID=$!
sleep 2
URL="http://127.0.0.1:8899"
echo "Live_Clipping panel: $URL  (PID $PID)"
if command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL" 2>/dev/null || true; fi
if command -v open >/dev/null 2>&1; then open "$URL" 2>/dev/null || true; fi
wait $PID
