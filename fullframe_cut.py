#!/usr/bin/env python3
"""Cut full-frame horizontal clips (NOT vertical Shorts) from a VOD section.

Usage (on the Mac, in ~/YT/app):
    python3 fullframe_cut.py <video_url> <start_sec> <end_sec> <num_clips> <clip_length>

- Downloads the given section in 4K (yt-dlp --download-sections).
- Finds the best moments (audio + face + shot signals, same as the panel).
- Cuts each moment as a HORIZONTAL full-frame clip (16:9, at least 2K).
- Uploads each clip to Drive (Live_Clipping/07-cuts-sequence/<folder>/).
- Records each clip in ~/YT/clips.json so the panel shows it.

The user frames/crops to vertical later himself — no vertical cut here.
"""
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import clip as clip_mod
from engine import drive_upload as drive_mod
from engine import fetch as fetch_mod
from engine import moments as moments_mod
from engine import titles as titles_mod

DATA_DIR = os.path.expanduser("~/YT")
CLIPS_DIR = os.path.join(DATA_DIR, "clips")
DOWNLOADS_DIR = os.path.join(DATA_DIR, "downloads")
RAW_DIR = os.path.join(DATA_DIR, "raw")
PREVIEWS_DIR = os.path.join(DATA_DIR, "previews")
CLIPS_JSON = os.path.join(DATA_DIR, "clips.json")


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def slug_channel(name):
    keep = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in (name or "channel"))
    return (keep.strip("_") or "channel")[:30]


def load_clips():
    if os.path.exists(CLIPS_JSON):
        try:
            return json.load(open(CLIPS_JSON, encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
    return []


def save_clips(clips):
    with open(CLIPS_JSON, "w", encoding="utf-8") as f:
        json.dump(clips, f, indent=2, ensure_ascii=False)


def download_section(meta, downloads_dir, section, log=print, quality="max"):
    """Download one arbitrary yt-dlp --download-sections window.

    The engine's download_vod only offers fixed from-start ranges
    (30min/1h/2h/full). For 30-min chunks through a long VOD we need
    an arbitrary window like *1800-3600, so build the yt-dlp call
    directly here, same merge flags as download_vod.

    Format: 2K VP9 (308+251). The 4K/2K AV1 formats (401/400) fail
    the section cut — ffmpeg exits with code 8 when yt-dlp tries to
    cut + merge AV1. VP9 2K works (verified on this video), and 2K
    is what the user asked for (at least 2K, native, no upscale).
    """
    import re
    import subprocess
    os.makedirs(downloads_dir, exist_ok=True)
    vid = meta["id"] or "video"
    # The section filename carries its time range (e.g. _sec_1800_3600).
    # An earlier version reused ANY {vid}_sec*.mp4 file, so every chunk
    # re-analysed the first chunk's video and returned the same moments
    # with shifted labels — the duplicate-clips bug (same 5 moments,
    # same scores, in every chunk). Reuse now only matches the exact
    # range being requested.
    m = re.match(r"\*(\d+)-(\d+)", section or "")
    range_tag = f"_{m.group(1)}_{m.group(2)}" if m else ""
    out_tmpl = os.path.join(downloads_dir, f"{vid}_sec{range_tag}.%(ext)s")
    expected = os.path.join(downloads_dir, f"{vid}_sec{range_tag}.mp4")
    if os.path.exists(expected) and os.path.getsize(expected) > 10_000_000:
        log(f"Reusing finished section file: {expected} "
            f"({os.path.getsize(expected)/1e6:.0f} MB) — no re-download.")
        return expected
    # Native 2K VP9 + Opus. Falls back to best <=1440p if 308 is gone.
    fmt = "308+251/bv*[height<=1440][vcodec^=vp9]+ba/b[height<=1440]"
    cmd = fetch_mod._ytdlp_cmd() + [
        "-f", fmt,
        "--merge-output-format", "mp4",
        "--no-playlist", "--no-warnings",
        "--retries", "3", "--fragment-retries", "3",
        "--download-sections", section,
        "-o", out_tmpl, meta["webpage_url"],
    ]
    log(f"Running: {' '.join(cmd[:6])} ... --download-sections {section}")
    subprocess.run(cmd, check=True)
    # Find the downloaded file (the range-specific name).
    if os.path.exists(expected):
        return expected
    for name in sorted(os.listdir(downloads_dir)):
        if name.startswith(f"{vid}_sec{range_tag}") and name.lower().endswith(".mp4"):
            return os.path.join(downloads_dir, name)
    raise RuntimeError(f"Section download produced no file in {downloads_dir}")


def fetch_subs(meta, url, log=print):
    """Download auto-subtitles (English) for the video, best-effort.

    Returns the .vtt path, or raises. Subtitles are small (a few MB
    even for a 23 h stream) and come from a separate --skip-download
    yt-dlp call, so a subs failure can never break the video download.
    The caller catches the raise and runs with dialogue off.
    """
    import glob
    import subprocess
    vid = meta["id"] or "video"
    out_tmpl = os.path.join(RAW_DIR, f"{vid}_subs.%(ext)s")
    cmd = fetch_mod._ytdlp_cmd() + [
        "--skip-download", "--write-auto-subs", "--sub-lang", "en",
        "--sub-format", "vtt", "--no-playlist", "--no-warnings",
        "-o", out_tmpl, url,
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True,
                   timeout=600)
    for path in sorted(glob.glob(os.path.join(RAW_DIR, f"{vid}_subs*.vtt"))):
        return path
    raise RuntimeError("no .vtt subtitle file produced")


def main():
    if len(sys.argv) < 5:
        print("usage: fullframe_cut.py <url> <start_sec> <end_sec> <num_clips> [clip_length]")
        sys.exit(2)
    url = sys.argv[1]
    start_sec = int(sys.argv[2])
    end_sec = int(sys.argv[3])
    num_clips = int(sys.argv[4])
    clip_length = int(sys.argv[5]) if len(sys.argv) > 5 else 60
    # quality "2k": horizontal_target gives native 2560x1440 from the
    # 1440p VP9 source. ("max" would drop to 1080p for a <4K source
    # per v1.8.8, and the user asked for at least 2K.)
    quality = "2k"
    log(f"=== Full-frame cut: {url} [{start_sec}-{end_sec}] top {num_clips} x {clip_length}s ===")

    meta = fetch_mod.get_metadata(url, log=log)
    log(f"Found: {meta['title'][:80]} ({meta.get('duration')}s, {meta.get('height')}p)")

    # Download just this section in 4K (yt-dlp --download-sections).
    section = f"*{start_sec}-{end_sec}"
    log(f"Downloading section {section} at quality={quality} ...")
    src = download_section(meta, DOWNLOADS_DIR, section, log=log, quality=quality)
    log(f"Source: {src} ({os.path.getsize(src)/1e9:.2f} GB)")

    # Audio + moments.
    wav = os.path.join(RAW_DIR, f"{meta['id'] or 'video'}_{start_sec}_{end_sec}_audio.wav")
    moments_mod.extract_audio(src, wav, log=log)
    energies = moments_mod.compute_energies(wav, log=log)
    duration = clip_mod.video_duration(src) or len(energies)
    shot_changes = moments_mod.detect_shot_changes(src, log=log)
    # Motion energy (car drifting / action) — a signal audio cannot see.
    motion_energies = moments_mod.compute_motion_energies(src, duration, log=log)
    # Subtitles (dialogue) -- the human-psyche signal: what people SAY
    # is what viewers remember. Best-effort: if subs are missing or
    # the fetch hits a bot-check, moments fall back to audio+visual.
    subtitle_segments = []
    subtitle_hits = {}
    try:
        vtt_path = fetch_subs(meta, url, log=log)
        subtitle_segments = moments_mod.parse_subtitle_segments(vtt_path)
        subtitle_hits = moments_mod.parse_subtitle_keywords(vtt_path)
        rel_segs = []
        for seg in subtitle_segments:
            s = seg["start"] - start_sec
            e = seg["end"] - start_sec
            if e > 0 and s < duration:
                rel_segs.append({"start": max(0.0, s),
                                 "end": min(float(duration), e),
                                 "text": seg["text"]})
        subtitle_segments = rel_segs
        rel_hits = {}
        for sec, kws in subtitle_hits.items():
            r = sec - start_sec
            if 0 <= r < duration:
                rel_hits[r] = kws
        subtitle_hits = rel_hits
        log(f"Subtitles: {len(subtitle_segments)} dialogue lines in this chunk.")
    except Exception as exc:
        log(f"Subtitles skipped ({exc}) -- dialogue signal off for this chunk.")
    face_samples = []
    try:
        from engine import face_framing as face_mod
        face_samples = face_mod.sample_face_presence(src, duration, log=log)
    except Exception as exc:
        log(f"Face sampling skipped ({exc}) — audio + shots only.")
    # Moments are relative to the section start; shift them to absolute.
    moments = moments_mod.find_moments(
        energies, duration, clip_length, num_clips, "med",
        shot_changes=shot_changes, face_samples=face_samples,
        motion_energies=motion_energies,
        subtitle_segments=subtitle_segments,
        subtitle_hits=subtitle_hits, log=log)
    if not moments:
        log("No moments found in this section.")
        return
    for m in moments:
        m["start"] += start_sec
        m["end"] += start_sec
    log(f"Found {len(moments)} moments: " +
        ", ".join(f"@{m['start']}s score {m['score']}" for m in moments))

    # Cut + upload each moment as a full-frame horizontal clip.
    date = datetime.now().strftime("%Y-%m-%d")
    chan = slug_channel(meta["channel"])
    stream_folder = drive_mod.stream_folder_name(date, meta["channel"], meta["id"])
    log(f"Drive folder: Live_Clipping/07-cuts-sequence/{stream_folder}")
    existing = load_clips()
    made = []
    for i, m in enumerate(moments, 1):
        m_len = int(m.get("length") or clip_length)
        base_name = f"{date}_{chan}_moment{m['rank']:02d}_{m['start']//60:02d}-{m['start']%60:02d}.mp4"
        fname = drive_mod.sequence_name(len(existing) + len(made) + 1, base_name)
        out = os.path.join(CLIPS_DIR, fname)
        log(f"Cutting full-frame clip {i}/{len(moments)}: {fname} @ {m['start']}s ({m_len}s)")
        # Cut from the section file: its time 0 = absolute start_sec.
        clip_mod.cut_horizontal(src, m["start"] - start_sec, m_len, out,
                                log=log, quality=quality)
        cut_info = dict(clip_mod.last_cut_info)
        check = clip_mod.verify_clip(
            out, m_len, target=(cut_info["target_width"], cut_info["target_height"]))
        if not check["size_ok"] or not check["audio_ok"]:
            log(f"WARNING: clip {fname} failed verification: {check}")
        preview_name = ""
        try:
            preview_name = fname + ".jpg"
            clip_mod.extract_preview(src, (m["start"] - start_sec) + m_len // 2,
                                     os.path.join(PREVIEWS_DIR, preview_name), log=log)
        except Exception as exc:
            log(f"WARNING: preview failed for {fname}: {exc}")
        cut_type, reasons = moments_mod.classify_short(m)
        m["timestamp"] = moments_mod.fmt_timestamp(m["start"])
        pkg = titles_mod.make_package(meta, m, m["rank"])
        quality_label = (
            f"upscaled from {cut_info['upscaled_from']}p"
            if cut_info["upscaled"] else f"native {cut_info['source_height']}p")
        if cut_info.get("enhanced"):
            quality_label += " + enhanced"
        rec = {
            "id": fname, "file": fname,
            "title": pkg["title"], "description": pkg["description"],
            "hashtags": pkg["hashtags"], "credit": pkg["credit"],
            "source_title": pkg["source_title"],
            "channel": meta["channel"], "source_url": meta["webpage_url"],
            "start": m["start"], "end": m["end"], "timestamp": m["timestamp"],
            "score": m["score"], "keywords": m.get("keywords", []),
            "length": m_len, "posted": False,
            "created_at": datetime.now().isoformat(),
            "size_mb": check["size_mb"], "duration": check["duration"],
            "verified": check,
            "quality": quality, "resolution": f"{check['width']}x{check['height']}",
            "quality_label": quality_label,
            "stream_folder": stream_folder,
            "drive_status": "pending", "drive_path": "", "uploaded_at": "",
            "local_freed": False, "freed_mb": 0.0,
            "approved": False, "approved_at": "", "rejected": False,
            "cut_type": "VIDEO", "reasons": reasons,
            "moment_type": m.get("moment_type", "loud"),
            "signals": m.get("signals", []),
            "focus": "full-frame", "facecam_corner": "",
            "nudge_x": 0, "nudge_y": 0, "preview": preview_name,
        }
        ok, drive_path, err = drive_mod.upload_clip(out, stream_folder, log=log)
        rec["drive_path"] = drive_path
        if ok:
            rec["drive_status"] = "uploaded"
            rec["uploaded_at"] = datetime.now().isoformat()
            log(f"Uploaded: {fname} -> 07-cuts-sequence/{stream_folder}/")
        else:
            rec["drive_status"] = "failed"
            rec["drive_error"] = err
            log(f"Drive upload FAILED for {fname}: {err}")
        made.append(rec)
        existing.append(rec)
        save_clips(existing)

    ok_n = sum(1 for c in made if c["drive_status"] == "uploaded")
    log(f"=== Done: {len(made)} full-frame clips, {ok_n} uploaded to Drive ===")
    log(f"Panel: http://127.0.0.1:8899 — {len(made)} new clips waiting for review.")


if __name__ == "__main__":
    main()
