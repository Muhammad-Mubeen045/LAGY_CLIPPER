# Live_Clipping — Test Report

**Date:** 2026-11-17 (EST) · **Version:** 1.0.0 · **Tester:** Buzz (VM + laptop)
**Test source:** GTA 6 Trailer 2 — Rockstar Games,
`https://www.youtube.com/watch?v=VQRLujxTm3c` (167 s, public VOD)

## Environment

| | VM (build) | Laptop (target) |
|---|---|---|
| OS | Ubuntu 24.04 | Kali Linux |
| Python | 3.12.3 | 3.14.8 |
| ffmpeg | 8.1.2 | 9.0.2 |
| yt-dlp | 2026.10.26 (`pip install --user --break-system-packages`) | 2026.10.26 (`/usr/bin/yt-dlp`) |
| numpy | 1.26.4 | 2.3.5 |
| node | 24.22.1 | 24.21.0 |
| Disk free | — | 356 GB |
| RAM | — | 7.4 GB |

## 1. Compile check — PASS

```
cd ~/workspace/YT/app && python3 -m py_compile live_clipping.py engine/*.py
COMPILE_OK
```

## 2. End-to-end CLI test (real YouTube video) — PASS

Command:
```
python3 live_clipping.py --url "https://www.youtube.com/watch?v=VQRLujxTm3c" \
  --clips 3 --length 30 --sensitivity med --data-dir ~/workspace/YT/data
```
Exit code 0, total time ~3 min 45 s (download ~45 s, 3 × 30 s clips ~55 s each
with loudnorm on a 1.5-CPU VM; the laptop will be faster).

Log (abridged):
```
[11:40:31] Found: Grand Theft Auto VI Trailer 2 | Rockstar Games | status=not_live duration=167s
[11:41:16] Downloaded source: data/downloads/VQRLujxTm3c.mp4 (34.2 MB)
[11:41:17] Audio extracted: data/raw/VQRLujxTm3c_audio.wav
[11:41:23] No subtitles available for this video (keyword boost skipped).  ← BUG, see §5
[11:41:23] Energy: 166 windows, max=0.3521, threshold p80=0.2330
[11:41:23] Top 3 moments: #3 @72s score=70.2, #1 @103s score=100.0, #2 @123s score=97.3
[11:42:16] Clip 1: ..._moment03_01-12.mp4 (30.0s, 11.49 MB, score 70.2)
[11:43:14] Clip 2: ..._moment01_01-43.mp4 (30.0s, 13.04 MB, score 100.0)
[11:44:11] Clip 3: ..._moment02_02-03.mp4 (30.0s, 12.75 MB, score 97.3)
[11:44:11] === Job done: 3 clips ===
```

## 3. Clip verification (ffprobe) — PASS, all 3 clips

| Clip | Size | Resolution | Codec | FPS | Duration | Audio |
|---|---|---|---|---|---|---|
| `2026-11-17_Rockstar_Games_moment01_01-43.mp4` | 13.04 MB | 1080×1920 | h264 | 30 | 30.00 s | AAC ✓ |
| `2026-11-17_Rockstar_Games_moment02_02-03.mp4` | 12.75 MB | 1080×1920 | h264 | 30 | 30.00 s | AAC ✓ |
| `2026-11-17_Rockstar_Games_moment03_01-12.mp4` | 11.49 MB | 1080×1920 | h264 | 30 | 30.00 s | AAC ✓ |

All durations within 1 s of the requested 30 s; all vertical 1080×1920;
all have audio. Titles + source credit verified in `clips.json`, e.g.
`GTA 6 Top Moment 😱 | Rockstar Games Live Moment` with credit
`Source: Rockstar Games — https://www.youtube.com/watch?v=VQRLujxTm3c`.

## 4. Panel server test — PASS

Started `python3 live_clipping.py --serve --port 8899 --data-dir ~/workspace/YT/data`,
then:

| Check | Result |
|---|---|
| `GET /` | 200, real HTML UI (dark panel, no raw JSON in the page) |
| `GET /style.css`, `/app.js` | 200 |
| `GET /api/status` | 200 — job `done`, progress 100, tools ffmpeg ✓ / yt-dlp ✓, log tail present |
| `GET /api/clips` | 200 — 3 clips listed |
| `POST /api/fetch` (Trailer 2) | `ok: true`, title `Grand Theft Auto VI Trailer 2`, channel Rockstar Games, `live_status: not_live` |
| `POST /api/fetch` (non-YouTube URL) | `ok: false`, honest error: "That does not look like a YouTube link…" |
| `GET /api/clip-file?name=…` with `Range: bytes=0-999` | 206, 1000 bytes (video seeking works) |

## 5. Bugs found & fixed

1. **Subtitle detection (fixed, re-tested):** `fetch_subtitles` returned
   "no subtitles" when yt-dlp exited non-zero, even though it had written
   `VQRLujxTm3c.en.vtt`. Fixed to check for the `.vtt` file regardless of
   exit code. Re-test: `Subtitles found: VQRLujxTm3c.en.vtt (keyword boost
   enabled)` — 6 keyword windows found (`jason` @24s/28s/55s, `lucia` @50s,
   `vice city` @123s). Note: the 3 test clips were cut before this fix, so
   their scores do not include the +10 keyword boost; the pipeline is
   correct going forward.
2. **Broken-pipe noise (fixed):** a client closing a video stream early
   (seek/abort) logged a `ConnectionResetError` traceback. Now silently
   ignored in the request handler.
3. **Keyword boost window too narrow (fixed, re-tested):** the boost only
   looked at subtitle words within ±2 s of the energy peak, so a keyword
   spoken inside the clip (e.g. `vice city` at 123 s, inside the
   123–153 s clip) never counted. Fixed to check the whole clip window.
   Verified on the VM with the real Trailer 2 audio + subtitles: moments
   #1 and #2 now correctly get `keywords: ['vice city']` and score 100.0.
   The laptop clips (§6) were regenerated with this fix.

## 6. Laptop sync + native laptop run — PASS

**Sync notes (worth knowing):**
- SSH only works with the explicit key: `-i ~/.ssh/id_ed25519
  -o IdentitiesOnly=yes` plus the proxy. A `laptop-yt` host entry was
  added to `/root/.ssh/config` (note: OpenSSH here reads `/root/.ssh/`,
  not `$HOME/.ssh/`, because the shell user is root) so plain
  `ssh laptop-yt` / `scp … laptop-yt:…` now works.
- `scp` of files larger than ~10 MB through the proxy consistently fails
  with `Connection closed`, leaving 0-byte files (3 attempts, same
  result). Small text files sync fine. Workaround used: the app code was
  synced by scp, and the clips were **generated natively on the laptop**
  instead of copying the VM clips over — stronger proof the app works on
  the target machine.
- First `scp -r app/` created a nested `~/YT/app/app/`; fixed by moving
  files up. App now lives directly in `~/YT/app/`.

**Native laptop run** (same command as §2, `--data-dir ~/YT`):
```
[06:48:46] Found: Grand Theft Auto VI Trailer 2 | Rockstar Games | status=not_live duration=167s
[06:48:55] Downloaded source: /home/b1-noxx/YT/downloads/VQRLujxTm3c.mp4 (34.2 MB)   ← 9 s
[06:49:03] Subtitles found: VQRLujxTm3c.en.vtt (keyword boost enabled).
[06:49:03] Top 3 moments: #3 @72s score=70.2, #1 @103s score=100.0, #2 @123s score=100.0
[06:51:27] === Job done: 3 clips ===   (total ~2 min 45 s — faster than the VM)
```
Re-run after the §5.3 fix regenerated the clips with the keyword boost.
Final laptop clips (ffprobe-verified, all 1080×1920 h264, 30.00 s, AAC):

| Clip | Size | Score | Keywords |
|---|---|---|---|
| `2026-11-17_Rockstar_Games_moment01_01-43.mp4` | 13.04 MB | 100.0 | vice city |
| `2026-11-17_Rockstar_Games_moment02_02-03.mp4` | 12.74 MB | 100.0 | vice city |
| `2026-11-17_Rockstar_Games_moment03_01-12.mp4` | 11.49 MB | 70.2 | — |

`~/YT/clips.json` on the laptop lists all 3 with titles, descriptions and
source credits. The VM clips in `~/workspace/YT/data/clips/` are the
pre-fix set (scores 100.0 / 97.3 / 70.2); the laptop set is the final one.

**Panel smoke test on the laptop — PASS:** started the panel on the
laptop (`python3 live_clipping.py --serve --port 8899 --data-dir ~/YT`),
then from the laptop itself: `GET /` 200, `/api/status` → `done`, progress
100, tools ffmpeg ✓ / ffprobe ✓ / yt-dlp ✓ / Python 3.14.8, `/api/clips`
→ 3 clips, clip-file `Range` request → 206. Panel stopped afterwards.

## 7. What was NOT tested (needs the user)

- **Real live-stream mode** (`record_live` via ffmpeg + stream URL):
  no live GTA 6 stream was running at test time. The code path is
  implemented (bounded `ffmpeg -t` recording) but unproven end-to-end.
  Needs a real live link — e.g. a Ducky Bhai GTA 6 live — to test.
- **Upcoming-stream refusal** — implemented (`is_upcoming` → clear error),
  not exercised against a real scheduled stream.
- **Windows / macOS launchers** (`run.bat`, `run_mac.sh`) — written but not
  run; no Windows/Mac machine was available. Linux `run.sh` logic matches
  the tested manual start.
- **PyInstaller build** — spec file written, binary not built (needs
  `pip install pyinstaller`; not required for the laptop install).
- **Drive upload via rclone** — rclone is NOT installed on the laptop
  (`rclone: command not found`), so no rclone command was run. Drive sync
  is documented as a manual step in the README and panel.

## 8. Drive folder — verified

`Live_Clipping` — https://drive.google.com/drive/folders/1_ski57eGLoqe5y0Nv5UtB7Eu51PycoDS
Subfolders present: `01-clips-ready`, `02-raw-moments`, `03-sources`,
`04-titles-thumbnails`, `05-posted`, `06-app-builds`.
