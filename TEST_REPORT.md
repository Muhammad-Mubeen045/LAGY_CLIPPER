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

---

# v1.2 — Max quality, live capture from the running stream, sequenced Drive upload, panel live progress, review/approve, SHORT vs VIDEO + framing

Tested 2026-11-17 (VM, 13:00–13:35 EST) unless noted. Code: `~/workspace/YT/app/` (v1.2.0).

## What changed in v1.2 (vs v1.0)
- **Max quality:** 1080p cap removed. Quality setting Max / 4K / 2K / 1080p, default Max. Source 4K → vertical 2160x3840; 2K → 1440x2560; else 1080x1920. Lower source → lanczos upscale + unsharp + light denoise, labelled honestly "upscaled from 720p" in `clips.json` (never fake native 4K). CRF 18, preset medium, AAC 192k.
- **Live capture from the running stream (core mode):** yt-dlp records the live stream itself (bounded by record time, ffprobe-verified playable); fallback ffmpeg HLS in 60 s segments with per-segment peak-energy detection. Never downloads a finished VOD for a live stream; never reports live clips as done if only a VOD download was attempted. Retries: yt-dlp direct → ffmpeg HLS → exact honest error.
- **WEEI root cause (fixed):** old code used `-f "b[height<=1080]/b"` — live streams have NO combined format `b`, only separate HLS video/audio (269–270 video, 233–234 audio), so yt-dlp answered "Requested format is not available". Selector is now `bv*+ba/b` with a retry and the yt-dlp error shown.
- **Drive sequenced upload (07-cuts-sequence):** `engine/drive_upload.py` — `find_rclone()` searches PATH, `~/go/bin`, `~/.local/bin`, `/usr/local/bin`, `/usr/bin`; `upload_clip()` = rclone copy + `rclone ls` verify by name+size, one retry, clear error when rclone missing (never logs the token). Sequence names `001_…`, `002_…` in `Live_Clipping/07-cuts-sequence/<YYYY-MM-DD_Channel_video-id>/`. Each clip uploaded immediately after cutting; `drive_path`/`uploaded_at` in `clips.json`; per-stream manifest uploaded at job end (carries approvals, for Buzz to read from Drive).
- **Free laptop space:** "Free laptop space after Drive upload" (default ON) — local clip deleted only after verified upload. "Delete source after job" (default OFF).
- **VOD time range:** Full / First 30 min / First 1 hour / First 2 hours via yt-dlp `--download-sections`. Default Auto = first 1 hour for videos over 2 hours, else full.
- **Panel live progress + refresh survival:** every job writes `live_status.json` to disk on every stage change; `/api/status` reads it from disk on every request and prefers it when fresher than in-memory state; log tail reads log files from disk (`logs/live_clipping.log` + `logs/rerun_1h.log`, merged by timestamp). Download progress: bytes / total / % / speed (MB/s) parsed from yt-dlp `--progress --newline` + part-file size. Live progress: elapsed, segments, moment count, pulsing LIVE indicator — no fake % for a live stream. UI auto-refreshes every 2 s.
- **Review → edit → approve:** per-clip preview, score, timestamp, title/description/hashtags; Edit (trim start/end, title, description, hashtags), Approve sets `approved=true` + `approved_at` in `clips.json` and in the Drive manifest. Approval does NOT publish. No YouTube OAuth/upload code in the app — Buzz uploads approved clips later from Drive via Postiz.
- **SHORT vs VIDEO classification + framing:** classifier decides type; panel card shows type badge + score + reasons; user can change type / approve / reject before approving.
  - SHORT: single peak moment, 45 s (30–60 s), vertical 9:16, for Shorts. Rule: one strong audio peak + keyword boost, score ≥ threshold.
  - VIDEO: sustained segment, 2–10 min, horizontal 16:9 (no crop), for long-form. Rule: sustained energy above p65 (floored at 0.30×max) for ≥120 s with dips ≤5 s bridged, plus ≥2 peaks above p80 (floored at 0.60×max).
  - Framing: vertical SHORT crops keep gameplay centred and keep a facecam in frame; panel shows a preview frame per cut + nudge controls (shift crop left/right/up/down) + Gameplay/Facecam focus toggle with corner select (top-right default).

## 1. Max quality end-to-end — PASS
Command (VM): `python3 live_clipping.py --url https://www.youtube.com/watch?v=VQRLujxTm3c --clips 2 --length 30 --quality max --data-dir ~/workspace/YT/data-v12`
- Source downloaded at Max: **3840x2160 AV1, 166.75 s, 185.8 MB** (vs 34.2 MB at 1080p).
- Clip 1: 30.0 s, 73.42 MB, **2160x3840**, "native 2160p". Clip 2: 30.0 s, 73.63 MB, same.
- Both uploaded + verified to fake Drive in sequence (`001_…`, `002_…` in `2026-11-17_Rockstar_Games_VQRLujxTm3c/`), local copies freed (147 MB total), `clips/` empty afterwards. Total ~17 min (4K medium-preset encodes on 1.5-CPU VM).
- Bug caught + fixed mid-test: upscale labelling compared source height to target height, so native 4K was mislabelled "upscaled from 2160p" — now compares source **width** to target vertical height.

## 2. Live capture on a real live stream (WEEI) — PASS, with two findings
Stream: WEEI Boston Sports live `https://www.youtube.com/live/Y6JZ4yzZNAw` (live, ~1,303 concurrent viewers / 1,902 views at 07:16, falling from 1,783).

**Attempt 1 (killed by a VM runtime restart, 13:12):** `live_clipping.py --url …/live/Y6JZ4yzZNAw --clips 2 --length 30 --record-minutes 1 --quality 1080p --data-dir ~/workspace/YT/data-live`
- yt-dlp live recording started and the file grew (94 MB at 13:17) — capture itself worked — but the 120 s stop did NOT stop it: the file kept growing to **117 MB / 409 s** before the restart killed everything. Root cause: `proc.terminate()` signals only the yt-dlp parent; its children (ffmpeg merge) inherit the stdout pipe and keep the recording going.
- The orphaned 409 s file was still **ffprobe-playable** (1920x1080, video+audio) — the live capture produces a valid file even when killed hard.
- Fix applied after this run (`engine/fetch.py`, post-sync edit — see §8): `start_new_session=True` + kill the whole process group (`SIGTERM`, then `SIGKILL` after 15 s); stop timeout tightened from record-minutes + 60 s grace to **+30 s**.

**Attempt 2 (after the fix, 13:20–13:23):** same command, `--data-dir ~/workspace/YT/data-live2`
- Recording **stopped on time**: limit reached 13:21:55, yt-dlp stopped 13:22:01.
- Recorded file: **30.1 s, 6.8 MB, 1920x1080** — less than the ~90 s of wall time. Honest note: yt-dlp live HLS takes ~30–60 s to start writing (format extraction + playlist), so a short record yields less than the record time. For a 10-minute record this matters proportionally less; the ffmpeg segment fallback starts faster.
- Moment detection on the live recording: **Top 1 moment @0 s, score 100.0** (30 s of audio, peaks merge into one — only 1 clip instead of the 2 requested, correct for the audio).
- Clip cut: `001_…_moment01_00-00.mp4`, **30.0 s, 13.52 MB, 1080x1920, native 1080p, type=SHORT**.
- Drive upload in this run **FAILED honestly**: the VM runtime restart had wiped `/tmp/fakebin/rclone`, so `find_rclone()` found nothing — the clip was kept locally as Pending with the clear error, nothing deleted. Re-created the fake-rclone stub and retried via `drive_upload.upload_clip()` → **verified in fake Drive** (`gdrive:Live_Clipping/07-cuts-sequence/2026-11-17_WEEI_Boston_s_Sports_Original_Y6JZ4yzZNAw/001_…mp4`, 13.5 MB). Full live chain (capture → moment → SHORT cut → Drive upload verified) is complete.
- Live progress reporting worked: `live_status.json` showed `live_capture: {method: yt-dlp live download, elapsed_seconds: 400, downloaded_bytes: 120586240, status: "capturing live now"}` in attempt 1.

## 3. Panel refresh + download progress — PASS (VM)
Job on Trailer 2 (1080p, 1 clip, panel port 8898): `/api/status` polls during download showed **32.2% → 45.5% → 64.5% → 85.5% → 97.4%**, 10.2 → 30.8 of 31.6 MB, speeds ~2.2–6.5 MB/s. Kill + restart panel → `/api/status` still showed "done" from disk (`live_status.json`), log tail from disk files, clips still listed.
- Bug caught + fixed: download showed `None` for the first 16 s — Python pipe buffering in `download_vod`'s Popen; fixed with `bufsize=1`.
- A second 3-second "download" in that test was a re-download of an already-present file (mtime unchanged) — not a real download; polls simply missed the 3 s window.
- Laptop panel (main agent, ~07:51 EST): `/api/status` returned status running, stage download, "Downloading first hour at max quality… (3348 MB so far)", download bytes 3347841072, speed 2.3 MB/s, tools version 1.2.0 with rclone found — live progress confirmed on the laptop panel too.

## 4. Review flow — PASS (VM panel, fake rclone)
Edit title/description/hashtags saved; Approve sets `approved=true` + `approved_at`; Drive manifest in fake Drive shows `approved: True`; Trim 100–130 s re-cut OK (30.0 s, approval reset to False, Drive re-uploaded, local file present again at 24 MB). Job done message: "1 clips cut, 1 uploaded … 23.9 MB freed".

## 5. Reframe / change-type / preview / reject — PASS (VM panel 8899, data-panel clip)
- Reframe facecam top-right: ok, focus=facecam, corner=top-right, res 1080x1920, drive=uploaded.
- `GET /api/preview`: HTTP 200, 84,709-byte JPEG.
- Nudge right +100: ok, nudge=(100, 0).
- Change-type SHORT→VIDEO: ok, type=VIDEO, **1920x1080 horizontal**, drive=uploaded.
- Reject: ok, rejected=True, approved=False.
- Note: on this legacy clip record (created before `reasons` existed) the reasons list shows only the change note; new records carry the classifier reasons.

## 6. Classifier unit tests — PASS after fix
- Synthetic sustained run (300 s, 6 peaks, keywords clutch/gta) → VIDEO segment @52 s, 298 s, score 94.5, reasons listed.
- Trailer 2 (167 s) → no sustained segments (too short).
- Bug caught + fixed (`engine/moments.py`): flat audio produced a fake 200 s segment with 188 "peaks" — fixed with floors (low ≥ 0.30×max, high ≥ 0.60×max) and a strict left-neighbour peak rule.
- Real-audio check on the salvaged 409 s WEEI recording: **0 VIDEO candidates — correct negative.** Talk radio has 120 windows <0.05 (pauses) and its longest sustained run above p65 is **34 s** (needs ≥120 s). WEEI talk is not a VIDEO source; the positive VIDEO path is proven only on synthetic audio so far. A real VIDEO positive needs action-heavy stream audio (e.g. GTA gameplay) — watch the Ducky 1-hour rerun's output for one.

## 7. Drive upload unit tests — PASS (fake rclone stub + /tmp/fakedrive)
`upload_clip` verified, size-mismatch detection works, missing-file and missing-rclone paths return clear errors, `upload_manifest` verified. Sequence naming, quality formats, VOD-range resolution, target sizes all asserted PASS. Progress-line parser unit-tested (VOD line 45.2% of 185.80 MiB at 5.20 MiB/s; live line with no total).

## 8. What changed AFTER the main agent's laptop sync (needs re-sync)
The main agent synced the VM snapshot to the laptop and restarted the panel itself (~07:51 EST, and again for the classification snapshot). The only code edit made after that sync is:
- **`engine/fetch.py` — live-record stop fix:** `start_new_session=True` on the yt-dlp Popen + process-group kill (`SIGTERM`, then `SIGKILL` after 15 s) + stop timeout from record-minutes + 60 s to +30 s grace. This is the fix proven in §2 attempt 2. It must be synced to the laptop and the panel restarted after the Ducky rerun finishes for live-record stops to work correctly there. No other VM file changed after the sync.

## 9. Laptop state at end of this session (2026-11-17 ~13:35 EST)
- **Laptop SSH unreachable** (two attempts, exit 255, "Connection closed by UNKNOWN port 65535") — could not verify `/api/status`, the rerun (PID 193551), or the panel from the VM. Laptop may be asleep/offline or the Tailscale proxy path is down. Last known state is the main agent's ~07:51 EST check (rerun downloading at 3348 MB, panel live). Do NOT assume the rerun finished; re-check when the laptop is reachable.
- The Ducky rerun (first hour, max quality) uploads its own clips to Drive `07-cuts-sequence/2026-11-17_Ducky_Reloaded_GpIWRLQBTvU/` itself — do not re-upload those clips.

## 10. What is NOT verified
- **VIDEO cut from real stream audio** — classifier positive path proven on synthetic audio only; real WEEI talk correctly yields none (§6). Needs action-heavy gameplay audio.
- **Live capture on a Ducky Bhai GTA 6 live** — proven on WEEI (real live stream) but not on a GTA 6 stream; the Ducky live is the real acceptance test when it runs.
- **Windows/macOS launchers** (`run.bat`, `run_mac.sh`) — written, not run; no Windows/Mac machine available. Linux `run.sh` logic matches the tested manual start.
- **PyInstaller build** — spec written, binary not built.
- **Full clip-upload path on the laptop against real Drive** — code ready and proven against fake rclone; real-Drive upload from the laptop is what the Ducky rerun is doing now (verify its `07-cuts-sequence` folder when the laptop is back).

## 11. v1.4 — visual moments + story-complete Shorts — PASS (2026-11-17)

Test: `test_v14_moments.py` on GTA 6 Trailer 2 (`VQRLujxTm3c`, 167 s, VM copy, cvtest venv cv2 4.10.0). **ALL PASS.**

- Shot changes: 34 detected (fast ffmpeg scene pass).
- Face sampling: 18/33 sampled frames have a face (YuNet, every 5 s).
- Combined score (`0.45*audio + 0.35*face + 0.20*shot`): 166 windows, max 0.848, p65 threshold 0.507.
- 5 moments, peaks 19/62/87/112/138 s, all ≥ 20 s apart. Scores 88.7 / 71.0 / 85.6 / 93.0 / 100.0. **All 5 carry a visual signal** (faces on screen + new shot); the lowest (71.0 @62 s) is a face/keyword moment, not a loud peak.
- Story windows: lengths 61/66/68/63/68 s (within [55, 70] for a 60 s clip); **all 5 have a shot/dialogue-aligned edge** (e.g. 57–117 s → 50–118 s).
- Two bugs found and fixed by the test: (1) a fixed 15 s merge window made 60 s Shorts near-duplicates (two windows starting 7 s apart); (2) a half-clip merge window chained distinct beats into 3 moments. Fix: greedy top-N selection — highest score first, each ≥ `max(15, clip_length//3)` s from every peak already taken — which cannot chain.
- Smoke checks: `find_sustained_segments` (VIDEO path) still works; `classify_short` shows signals; audio-only input (no visual signals) ranks the loud peak first, as before — backward compatible.
- Default clip length is now 60 s (was 45 s); 90 s is an option. The cut loop uses each moment's actual story length, not the fixed clip length.

## 12. v1.5 — Review & Filter: keep or drop, before you post — PASS (2026-11-18)

Built because the panel had approve/reject/delete buttons buried in
each clip card but no review section and no filters — clips from
every job mixed together, no way to see what still needed a decision.

- Filter unit test `test_v15_filters.js` (node, extracts the real
  clipStatus / clipMatchesFilter from panel/app.js): **ALL PASS** —
  status (needs/approved/rejected/posted), type, source folder, min
  score, and combined filters.
- Live test on the laptop `test_v15_live.sh` (synthetic record only,
  TEST_v15_bulk.mp4 + Drive folder TEST_v15_review, clips.json backed
  up and restored): **ALL PASS** — panel v1.5 started on 8899;
  bulk approve set approved=true; bulk reject set rejected=true and
  approved=false; single delete returned drive_deleted=true
  ("Drive copy deleted"), the record left clips.json (9 records) and
  the Drive listing came back empty. clips.json restored to 9
  records, no test record left, test Drive folder removed.
- The served panel HTML was verified to contain the new review UI
  ("Review — keep or drop", fStatus, v1.5) and the panel was left
  running on 8899 so the user can open it.
- Delete now removes the Drive copy too (new
  engine/drive_upload.py delete_from_drive, rclone delete of the
  exact file). Reject stays soft — marked not-worthy, stays in Drive.
  Bulk actions go through one new /api/clips/bulk endpoint with
  per-file results. Nothing is posted from the panel.

## 13. v1.5.1 — Bugfix: the int(None) crash — PASS (2026-11-18)

The panel showed `int() argument must be a string, a bytes-like
object or a real number, not 'NoneType'` when the user clicked
Process (screenshot 04:41 EST). Reproduced on the laptop: POST
/api/process with `{"length": null, ...}` returned exactly that
error — a null in the body (JSON turns a JS NaN into null) hit
`int(body.get("length", 60))`, whose default only applies when the
key is missing, not when it is null. The reframe nudge math was
excluded (it raises a different `+` TypeError, not int(None)).
- Fix: `_safe_int(value, default)` (never raises on None/bad input)
  for clips/length/record_minutes, the cut loop, and trim start/end;
  frontend sends `+value || default` so it never sends NaN; reframe
  and recut nudge math use `(rec.get("nudge_x") or 0)` — three old
  records carried nudge_x/nudge_y as explicit None, and `.get(key, 0)`
  returns None for a present key. Those 6 fields were normalized to
  0 in clips.json. Server errors now log the full traceback (the old
  handler swallowed it, so the crash was undiagnosable from the log).
- Live test on the laptop: POST /api/process with null
  length/clips/record_minutes now returns `{"ok": true, "Job
  started."}` (then fails honestly on the fake URL, "not a YouTube
  link", no crash); reframe math on a None-nudge record gives the
  correct nudge (-100, 50); `_safe_int(None, 60)=60`,
  `_safe_int("abc", 5)=5`. Panel restarted as v1.5.1 on 8899,
  md5-identical to VM, state reset to idle ("Ready. Paste a YouTube
  link to start.").
