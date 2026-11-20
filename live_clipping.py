#!/usr/bin/env python3
"""Live_Clipping — turn a YouTube live stream / VOD into vertical Shorts.

Cross-platform (macOS / Windows / Linux), Python 3.11+, stdlib only
for the web panel (no web framework needed). Needs ffmpeg + yt-dlp.

Usage — panel (recommended):
    python3 live_clipping.py --serve            # http://127.0.0.1:8899
    python3 live_clipping.py --serve --port 8899 --data-dir ~/YT

Usage — CLI:
    python3 live_clipping.py --url <youtube-link> --clips 5 --length 60
    python3 live_clipping.py --url <live-link> --record-minutes 10

RIGHTS NOTE: only clip streams you own or have permission for, or that
are fair-use / transformative with source credit. Every clip carries a
source channel + credit field. Do not remove this note.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine import clip as clip_mod       # noqa: E402
from engine import drive_upload as drive_mod  # noqa: E402
from engine import fetch as fetch_mod     # noqa: E402
from engine import moments as moments_mod  # noqa: E402
from engine import titles as titles_mod   # noqa: E402

def _safe_int(value, default: int) -> int:
    """int() that never raises on None / bad input — returns `default`.

    The panel once crashed with "int() argument must be ... not
    'NoneType'" when a request body carried a null (JSON turns a JS
    NaN into null). A bad number must fall back to the default, never
    take the panel down.
    """
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


VERSION = "1.8.11"
DEFAULT_PORT = 8899
RIGHTS_NOTE = (
    "Only clip streams you own or have permission for, or that are "
    "fair-use / transformative with source credit. Every clip keeps its "
    "source channel and link attached."
)

# ---------------------------------------------------------------- data dirs

class Store:
    def __init__(self, data_dir: str):
        self.base = os.path.expanduser(data_dir)
        self.clips = os.path.join(self.base, "clips")
        self.raw = os.path.join(self.base, "raw")
        self.downloads = os.path.join(self.base, "downloads")
        self.logs = os.path.join(self.base, "logs")
        self.posted = os.path.join(self.base, "posted")
        self.previews = os.path.join(self.base, "previews")
        for d in (self.clips, self.raw, self.downloads, self.logs, self.posted,
                  self.previews):
            os.makedirs(d, exist_ok=True)
        self.state_file = os.path.join(self.base, "state.json")
        self.clips_file = os.path.join(self.base, "clips.json")
        self.log_file = os.path.join(self.logs, "live_clipping.log")
        # Lock serialising every clips.json read-modify-write (panel
        # approve/reject/delete/bulk AND the job's final save). Without
        # it, two requests that load the same list and save back
        # clobber each other's change — e.g. deleting two clips at
        # once restored one of them, and a reject made mid-job was
        # undone by the job's save. Every clips.json write goes
        # through this lock (see update_clips / merge_save_clips).
        self.clips_lock = threading.Lock()
        # Shared live status: EVERY job (panel, CLI, rerun script) writes
        # here on every stage change, so a browser refresh always shows
        # the live work — never a stale in-memory state.
        self.live_status_file = os.path.join(self.base, "live_status.json")

    def load_clips(self) -> list:
        if os.path.exists(self.clips_file):
            try:
                with open(self.clips_file, encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                return []
        return []

    def save_clips(self, clips: list) -> None:
        with open(self.clips_file, "w", encoding="utf-8") as f:
            json.dump(clips, f, indent=2, ensure_ascii=False)

    def update_clips(self, fn):
        """Atomically load clips.json, apply fn(clips), save it back.

        Holds the clips lock for the whole read-modify-write, so two
        panel requests (or a request and the job's save) cannot load
        the same stale list and clobber each other. fn receives the
        list and may mutate it in place; its return value is returned.
        """
        with self.clips_lock:
            clips = self.load_clips()
            result = fn(clips)
            self.save_clips(clips)
            return result

    def merge_save_clips(self, new_records: list) -> list:
        """Atomically merge a job's new records into clips.json.

        Re-reads clips.json from disk (so approve/reject/delete
        decisions the user made while the job was running are kept),
        keeps the on-disk version of every record that already exists
        there, drops records the user deleted, and appends only the
        new records that are not already on disk. Returns the merged
        list. This replaces the old `existing.extend(made); save`
        which overwrote the file with a stale in-memory copy and
        undid the user's mid-job decisions.
        """
        with self.clips_lock:
            on_disk = self.load_clips()
            on_disk_files = {c.get("file") for c in on_disk}
            merged = list(on_disk)
            for rec in new_records:
                if rec.get("file") not in on_disk_files:
                    merged.append(rec)
                    on_disk_files.add(rec.get("file"))
            self.save_clips(merged)
            return merged


# ---------------------------------------------------------------- job state

class Job:
    def __init__(self, store: Store):
        self.store = store
        self.lock = threading.Lock()
        self.state = {
            "status": "idle", "stage": "", "progress": 0,
            "message": "Ready. Paste a YouTube link to start.",
            "url": "", "meta": None, "error": "",
            "started_at": "", "finished_at": "",
            "updated_at": 0.0, "download": None, "live_capture": None,
            "drive_folder": None,
            # v1.7 — progress details the panel shows: the job's
            # settings and the moments found (rank, time, score,
            # signals), so the user sees what is coming before the
            # clips are cut.
            "settings": None, "moments": [],
        }
        self.log_lines: deque[str] = deque(maxlen=300)
        self._load_state()

    def _load_state(self):
        # Prefer the shared live_status.json when it is fresher — it may
        # have been written by another process (CLI, rerun script).
        for path in (self.store.live_status_file, self.store.state_file):
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        data = json.load(f)
                    if data.get("updated_at", 0) >= self.state.get("updated_at", 0):
                        self.state.update(data)
                except (json.JSONDecodeError, OSError):
                    pass

    def _save_state(self):
        self.state["updated_at"] = time.time()
        payload = json.dumps(self.state, indent=2, ensure_ascii=False)
        for path in (self.store.state_file, self.store.live_status_file):
            try:
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(payload)
                os.replace(tmp, path)
            except OSError:
                pass

    def log(self, msg: str):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        with self.lock:
            self.log_lines.append(line)
        try:
            with open(self.store.log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        print(line, flush=True)

    def set(self, **kw):
        with self.lock:
            self.state.update(kw)
        self._save_state()

    def _read_disk_status(self) -> dict:
        """Read live_status.json from disk (may be from another process)."""
        try:
            with open(self.store.live_status_file, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, OSError):
            return {}

    def snapshot(self) -> dict:
        """Live status, preferring the fresher source.

        The panel server's memory is NOT the truth: a CLI job, the
        rerun script, or a previous panel process may have written a
        fresher live_status.json to disk. A browser refresh must show
        the live work, not a stale in-memory error.
        """
        with self.lock:
            mem = dict(self.state)
        disk = self._read_disk_status()
        if disk and disk.get("updated_at", 0) >= mem.get("updated_at", 0):
            merged = dict(mem)
            merged.update(disk)
            return merged
        return mem

    def log_tail(self, n: int = 60) -> list[str]:
        """Log tail from the log FILES on disk (not only memory).

        Reads live_clipping.log and rerun_1h.log, merges by timestamp,
        returns the last n lines — so a refresh shows the real log,
        including work done by the rerun script (which never touches
        this process's memory).
        """
        lines: list[tuple[str, str]] = []
        for name in ("live_clipping.log", "rerun_1h.log"):
            path = os.path.join(self.store.logs, name)
            try:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    tail = f.readlines()[-80:]
                for raw in tail:
                    line = raw.rstrip("\n")
                    if not line:
                        continue
                    stamp = line[1:9] if line.startswith("[") and len(line) > 9 else ""
                    lines.append((stamp, f"[{name.replace('.log', '')}] {line}"
                                  if name.startswith("rerun") else line))
            except OSError:
                pass
        if lines:
            lines.sort(key=lambda t: t[0])
            return [line for _, line in lines[-n:]]
        with self.lock:
            return list(self.log_lines)[-n:]


# ---------------------------------------------------------------- pipeline

def slug_channel(name: str) -> str:
    keep = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in (name or "channel"))
    return (keep.strip("_") or "channel")[:30]


def _upload_and_free(job: Job, out: str, rec: dict, free_space: bool) -> float:
    """Upload one cut to Drive in sequence.

    v1.8.4: the local copy is ALWAYS kept now, so the panel's video
    player can play the clip while the user reviews it. (Freeing it
    after upload meant /api/clip-file 404'd and the panel showed only
    a still preview image — the user could not watch the clip to
    decide approve/reject.) Local copies are removed only when the
    user deletes the clip in the panel (Drive + local). The
    `free_space` flag is kept for API compatibility but no longer
    deletes clip files; the big source recording is still freed by
    `delete_source` after the job. Returns MB freed (always 0 now).
    """
    ok, drive_path, err = drive_mod.upload_clip(
        out, rec["stream_folder"], log=job.log)
    rec["drive_path"] = drive_path
    if not ok:
        rec["drive_status"] = "failed"
        rec["drive_error"] = err
        job.log(f"Drive upload FAILED for {rec['file']}: {err} "
                f"— clip kept locally as Pending, use Retry in the panel.")
        return 0.0
    rec["drive_status"] = "uploaded"
    rec["uploaded_at"] = datetime.now().isoformat()
    rec["local_freed"] = False
    job.log(f"Clip uploaded to Drive and kept locally for panel viewing: "
            f"{rec['file']}")
    return 0.0


def _cut_and_upload_full_frame(job: Job, src: str, rec: dict, start: int,
                               length: int, quality: str,
                               free_space: bool) -> float:
    """v1.8 — two folders per video. Every SHORT also gets a full-frame
    (horizontal, no crop — the video's default cut) twin, uploaded to
    the sibling Drive folder `<stream_folder>_FULL`. The record keeps
    `full_frame_file` / `full_frame_drive_*` so the panel can show it.
    Returns MB freed (0 when the file is kept or the cut failed)."""
    folder = (rec.get("stream_folder") or "") + "_FULL"
    base = rec["file"]
    ff_name = base[:-4] + "_FULL.mp4" if base.endswith(".mp4") else base + "_FULL"
    # NOTE: `store` is a local inside run_job, not a module global —
    # the clips dir lives on the job (job.store.clips). Using bare
    # `store` here raised NameError and killed the whole job.
    out = os.path.join(job.store.clips, ff_name)
    try:
        clip_mod.cut_horizontal(src, start, length, out,
                                log=job.log, quality=quality)
    except Exception as exc:  # noqa: BLE001
        job.log(f"WARNING: full-frame cut failed for {rec['file']}: {exc}")
        rec["full_frame_file"] = ff_name
        rec["full_frame_folder"] = folder
        rec["full_frame_drive_status"] = "failed"
        rec["full_frame_drive_error"] = str(exc)[:200]
        return 0.0
    rec["full_frame_file"] = ff_name
    rec["full_frame_folder"] = folder
    ok, drive_path, err = drive_mod.upload_clip(out, folder, log=job.log)
    rec["full_frame_drive_path"] = drive_path
    if not ok:
        rec["full_frame_drive_status"] = "failed"
        rec["full_frame_drive_error"] = err
        job.log(f"Full-frame Drive upload FAILED for {ff_name}: {err} "
                f"— kept locally, retry from the panel.")
        return 0.0
    rec["full_frame_drive_status"] = "uploaded"
    rec["full_frame_uploaded_at"] = datetime.now().isoformat()
    job.log(f"Full-frame cut uploaded: {ff_name} -> "
            f"07-cuts-sequence/{folder}/")
    # v1.8.4: keep the local full-frame copy too, so the panel can
    # play it (same reason as _upload_and_free — freeing it after
    # upload left the panel with no video to show).
    rec["full_frame_local_freed"] = False
    return 0.0


def run_job(job: Job, url: str, num_clips: int = 5, clip_length: int = 60,
            sensitivity: str = "med", record_minutes: int = 10,
            quality: str = "max", focus: str = "face",
            vod_range: str = "auto",
            free_space: bool = True, delete_source: bool = False) -> list[dict]:
    """Full pipeline. Returns the list of clip records created.

    v1.1/v1.2 flow: laptop cuts -> each clip is uploaded to Drive
    Live_Clipping/07-cuts-sequence/<stream-folder>/ IN SEQUENCE right
    after it is cut -> if the upload verifies and free_space is on, the
    local clip file is deleted so the laptop stays light. Next work
    (titles / thumbnails / posting) is received from Drive, not the
    laptop. Quality defaults to Max (best available, incl. 4K).
    """
    store = job.store
    job.set(status="running", stage="fetch", progress=2,
            message="Reading stream info…", url=url, meta=None, error="",
            started_at=datetime.now().isoformat(), finished_at="",
            live_capture=None, drive_folder=None, download=None,
            settings={"num_clips": num_clips, "clip_length": clip_length,
                      "sensitivity": sensitivity,
                      "record_minutes": record_minutes, "quality": quality,
                      "focus": focus, "vod_range": vod_range,
                      "free_space": free_space,
                      "delete_source": delete_source},
            moments=[])
    job.log(f"=== Live_Clipping job started: {url} ===")
    job.log(f"Settings: quality={quality}, focus={focus}, vod_range={vod_range}, "
            f"free_space_after_upload={free_space}, "
            f"delete_source_after_job={delete_source}")
    src = ""
    try:
        meta = fetch_mod.get_metadata(url, log=job.log)
        job.set(meta=meta, stage="fetch", progress=10,
                message=f"Found: {meta['title'][:80]}")
        if meta["live_status"] == "is_live":
            job.set(stage="download", progress=15,
                    message=f"LIVE CAPTURE — recording live now for {record_minutes} min…",
                    live_capture={"method": "capturing live now",
                                  "segments": 0, "elapsed_seconds": 0,
                                  "moments_so_far": 0,
                                  "status": "capturing live now"})

            def _live_cb(prog: dict):
                job.set(live_capture={
                    "method": prog.get("method", "capturing live now"),
                    "segments": prog.get("segments", 0),
                    "elapsed_seconds": prog.get("elapsed_seconds", 0),
                    "moments_so_far": prog.get("moments_so_far", 0),
                    "downloaded_bytes": prog.get("downloaded_bytes"),
                    "status": "capturing live now",
                })

            src = fetch_mod.record_live(meta, store.downloads, record_minutes,
                                        log=job.log, quality=quality,
                                        progress_cb=_live_cb)
            info = dict(fetch_mod.last_live_info)
            info["status"] = "captured live — processing recorded segment"
            job.set(live_capture=info)
        else:
            job.set(stage="download", progress=15, message="Downloading source video…")

            def _dl_cb(prog: dict):
                job.set(download={
                    "percent": prog.get("percent"),
                    "downloaded_bytes": prog.get("downloaded_bytes"),
                    "total_bytes": prog.get("total_bytes"),
                    "speed": prog.get("speed", ""),
                })

            src = fetch_mod.download_vod(meta, store.downloads, log=job.log,
                                         quality=quality, vod_range=vod_range,
                                         progress_cb=_dl_cb)
            job.set(download=None)

        job.set(stage="audio", progress=35, message="Extracting audio for moment detection…")
        wav = os.path.join(store.raw, f"{meta['id'] or 'video'}_audio.wav")
        moments_mod.extract_audio(src, wav, log=job.log)

        job.set(stage="moments", progress=50,
                message="Finding the best moments (audio + faces + shots)…")
        energies = moments_mod.compute_energies(wav, log=job.log)
        duration = clip_mod.video_duration(src) or len(energies)
        vtt = fetch_mod.fetch_subtitles(meta, store.downloads, log=job.log)
        sub_hits = moments_mod.parse_subtitle_keywords(vtt)
        sub_segments = moments_mod.parse_subtitle_segments(vtt)
        # Visual signals (v1.4): shot boundaries are a fast ffmpeg
        # pass; face sampling needs OpenCV + the YuNet model and
        # returns [] without them (audio-only moments, as before).
        shot_changes = moments_mod.detect_shot_changes(src, log=job.log)
        face_samples: list[dict] = []
        try:
            from engine import face_framing as face_mod
            face_samples = face_mod.sample_face_presence(
                src, duration, log=job.log)
        except Exception as exc:  # noqa: BLE001
            job.log(f"Face sampling skipped ({exc}) — audio + shots only.")
        moments = moments_mod.find_moments(
            energies, duration, clip_length, num_clips, sensitivity,
            subtitle_hits=sub_hits, shot_changes=shot_changes,
            face_samples=face_samples, subtitle_segments=sub_segments,
            log=job.log)
        if not moments:
            raise RuntimeError("No moments found in this video.")
        # v1.7 — publish the moments so the panel shows what was found
        # (and what is about to be cut) before the clips exist.
        job.set(moments=[{
            "rank": m["rank"], "start": m["start"],
            "timestamp": moments_mod.fmt_timestamp(m["start"]),
            "score": m["score"], "length": _safe_int(m.get("length"), clip_length),
            "signals": list(m.get("signals", [])),
        } for m in moments])

        job.set(stage="clipping", progress=60, message="Cutting vertical Shorts…")
        date = datetime.now().strftime("%Y-%m-%d")
        chan = slug_channel(meta["channel"])
        stream_folder = drive_mod.stream_folder_name(date, meta["channel"], meta["id"])
        job.set(drive_folder=stream_folder)
        job.log(f"Drive sequence folder: Live_Clipping/07-cuts-sequence/{stream_folder}")
        existing = store.load_clips()
        made: list[dict] = []
        freed_mb_total = 0.0
        for i, m in enumerate(moments, 1):
            job.set(progress=60 + int(35 * i / len(moments)),
                    message=f"Cutting clip {i}/{len(moments)} (moment at {moments_mod.fmt_timestamp(m['start'])})…")
            base_name = f"{date}_{chan}_moment{m['rank']:02d}_{m['start']//60:02d}-{m['start']%60:02d}.mp4"
            fname = drive_mod.sequence_name(i, base_name)
            out = os.path.join(store.clips, fname)
            # Story-complete length: the moment window may be longer
            # or shorter than clip_length (shot/dialogue aligned).
            m_len = _safe_int(m.get("length"), clip_length)
            clip_mod.cut_vertical(src, m["start"], m_len, out,
                                  log=job.log, quality=quality, focus=focus)
            cut_info = dict(clip_mod.last_cut_info)
            check = clip_mod.verify_clip(
                out, m_len,
                target=(cut_info["target_width"], cut_info["target_height"]))
            if not check["size_ok"] or not check["audio_ok"]:
                job.log(f"WARNING: clip {fname} failed verification: {check}")
            # Preview frame for the review step (framing check).
            preview_name = ""
            try:
                preview_name = fname + ".jpg"
                clip_mod.extract_preview(src, m["start"] + m_len // 2,
                                         os.path.join(store.previews, preview_name),
                                         log=job.log)
            except Exception as exc:  # noqa: BLE001
                job.log(f"WARNING: preview frame failed for {fname}: {exc}")
            cut_type, reasons = moments_mod.classify_short(m)
            m["timestamp"] = moments_mod.fmt_timestamp(m["start"])
            pkg = titles_mod.make_package(meta, m, m["rank"])
            quality_label = (
                f"upscaled from {cut_info['upscaled_from']}p"
                if cut_info["upscaled"]
                else f"native {cut_info['source_height']}p")
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
                # v1.8: full-frame twin (horizontal, no crop) of this
                # SHORT, in the sibling <stream_folder>_FULL Drive
                # folder. Set by _cut_and_upload_full_frame.
                "full_frame_file": "", "full_frame_folder": "",
                "full_frame_drive_status": "pending",
                "full_frame_drive_path": "", "full_frame_uploaded_at": "",
                "full_frame_local_freed": False, "full_frame_freed_mb": 0.0,
                # Review step: approval is recorded here and in the Drive
                # manifest. It does NOT publish anything by itself.
                "approved": False, "approved_at": "", "rejected": False,
                "cut_type": cut_type, "reasons": reasons,
                "focus": cut_info.get("focus", focus),
                "facecam_corner": cut_info.get("facecam_corner", "top-right"),
                "framing": cut_info.get("framing"),
                "nudge_x": cut_info.get("nudge_x", 0),
                "nudge_y": cut_info.get("nudge_y", 0), "preview": preview_name,
            }
            made.append(rec)
            job.log(f"Clip {i}: {fname} ({check['duration']}s, {check['size_mb']} MB, "
                    f"score {m['score']}, {rec['resolution']} {quality_label}, "
                    f"type={cut_type})")

            # --- v1.8: full-frame twin for this Short (same moment,
            # horizontal, no crop) into the sibling _FULL Drive folder.
            if cut_type == "SHORT":
                freed_mb_total += _cut_and_upload_full_frame(
                    job, src, rec, m["start"], m_len, quality, free_space)

            # --- v1.1: upload THIS clip to Drive in sequence, right now,
            # before cutting the next one. Only then free the laptop copy.
            job.set(message=f"Uploading clip {i}/{len(moments)} to Drive…")
            freed_mb_total += _upload_and_free(job, out, rec, free_space)

        # --- VIDEO cuts: sustained segments (2–10 min, horizontal 16:9).
        sustained = moments_mod.find_sustained_segments(
            energies, duration, sub_hits, log=job.log)
        for j, seg in enumerate(sustained, 1):
            seq_no = len(made) + 1
            seg_len = seg["length"]
            job.set(progress=95, message=f"Cutting VIDEO {j}/{len(sustained)} "
                    f"({seg_len // 60}m {seg_len % 60}s sustained)…")
            base_name = (f"{date}_{chan}_video{j:02d}_"
                         f"{seg['start'] // 60:02d}-{seg['start'] % 60:02d}.mp4")
            fname = drive_mod.sequence_name(seq_no, base_name)
            out = os.path.join(store.clips, fname)
            clip_mod.cut_horizontal(src, seg["start"], seg_len, out,
                                    log=job.log, quality=quality)
            cut_info = dict(clip_mod.last_cut_info)
            check = clip_mod.verify_clip(
                out, seg_len,
                target=(cut_info["target_width"], cut_info["target_height"]))
            if not check["size_ok"] or not check["audio_ok"]:
                job.log(f"WARNING: VIDEO {fname} failed verification: {check}")
            preview_name = ""
            try:
                preview_name = fname + ".jpg"
                clip_mod.extract_preview(src, seg["start"] + seg_len // 2,
                                         os.path.join(store.previews, preview_name),
                                         log=job.log)
            except Exception as exc:  # noqa: BLE001
                job.log(f"WARNING: preview frame failed for {fname}: {exc}")
            pkg = titles_mod.make_package(
                meta, {"start": seg["start"], "end": seg["end"],
                       "score": seg["score"], "keywords": seg["keywords"]}, j)
            quality_label = (
                f"upscaled from {cut_info['upscaled_from']}p"
                if cut_info["upscaled"]
                else f"native {cut_info['source_height']}p")
            rec = {
                "id": fname, "file": fname,
                "title": pkg["title"], "description": pkg["description"],
                "hashtags": pkg["hashtags"], "credit": pkg["credit"],
                "source_title": pkg["source_title"],
                "channel": meta["channel"], "source_url": meta["webpage_url"],
                "start": seg["start"], "end": seg["end"],
                "timestamp": moments_mod.fmt_timestamp(seg["start"]),
                "score": seg["score"], "keywords": seg["keywords"],
                "length": seg_len, "posted": False,
                "created_at": datetime.now().isoformat(),
                "size_mb": check["size_mb"], "duration": check["duration"],
                "verified": check,
                "quality": quality, "resolution": f"{check['width']}x{check['height']}",
                "quality_label": quality_label,
                "stream_folder": stream_folder,
                "drive_status": "pending", "drive_path": "", "uploaded_at": "",
                "local_freed": False, "freed_mb": 0.0,
                "approved": False, "approved_at": "", "rejected": False,
                "cut_type": "VIDEO", "reasons": seg["reasons"],
                "focus": "full-frame", "facecam_corner": "",
                "nudge_x": 0, "nudge_y": 0, "preview": preview_name,
            }
            made.append(rec)
            job.log(f"VIDEO {j}: {fname} ({check['duration']}s, {check['size_mb']} MB, "
                    f"score {seg['score']}, {rec['resolution']} {quality_label})")
            job.set(message=f"Uploading VIDEO {j}/{len(sustained)} to Drive…")
            freed_mb_total += _upload_and_free(job, out, rec, free_space)

        # Merge (not overwrite): the user may have approved / rejected /
        # deleted clips while this job was running. merge_save_clips
        # re-reads clips.json from disk, keeps the on-disk version of
        # every record (the user's latest decision), drops records the
        # user deleted, and only appends this job's new records.
        existing = store.merge_save_clips(made)
        # Manifest in Drive from the start, so review/approve state is
        # readable from Drive (Buzz uploads approved clips later).
        _ok, _err = drive_mod.upload_manifest(
            stream_folder, [c for c in existing
                            if c.get("stream_folder") == stream_folder],
            log=job.log)
        if not _ok:
            job.log(f"Manifest upload skipped/failed: {_err}")
        uploaded = sum(1 for c in made if c["drive_status"] == "uploaded")
        # --- after the job: delete the big source download only if the
        # user asked for it AND every clip is safely in Drive.
        source_deleted = ""
        if delete_source and src and uploaded == len(made) and os.path.exists(src):
            try:
                size_gb = os.path.getsize(src) / 1e9
                os.remove(src)
                source_deleted = f" Source download deleted ({size_gb:.2f} GB freed)."
                job.log(f"Deleted source download {src} ({size_gb:.2f} GB freed).")
            except OSError as exc:
                job.log(f"WARNING: could not delete source download: {exc}")
        elif delete_source and uploaded != len(made):
            job.log("Source download kept: not all clips uploaded to Drive yet.")
        n_short = sum(1 for c in made if c.get("cut_type") == "SHORT")
        n_video = sum(1 for c in made if c.get("cut_type") == "VIDEO")
        done_msg = (f"Done — {len(made)} cuts ({n_short} SHORT, {n_video} VIDEO), "
                    f"{uploaded} uploaded to Drive "
                    f"07-cuts-sequence/{stream_folder}"
                    + (f", {freed_mb_total:.1f} MB freed from the laptop."
                       if freed_mb_total else ".")
                    + source_deleted)
        job.set(status="done", stage="done", progress=100,
                message=done_msg,
                finished_at=datetime.now().isoformat())
        job.log(f"=== Job done: {len(made)} clips, {uploaded} in Drive ===")
        return made
    except Exception as exc:  # noqa: BLE001 - surface honestly in panel + CLI
        job.log(f"ERROR: {exc}")
        traceback.print_exc()
        job.set(status="error", stage="error", progress=0,
                message=f"Failed: {exc}", error=str(exc),
                finished_at=datetime.now().isoformat())
        raise


# ---------------------------------------------------------------- panel server

PANEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "panel")
MIME = {".html": "text/html; charset=utf-8", ".css": "text/css",
        ".js": "application/javascript", ".json": "application/json"}


def _check_tools() -> dict:
    import shutil
    return {
        "python": sys.version.split()[0],
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "ffprobe": bool(shutil.which("ffprobe")),
        "yt_dlp": bool(shutil.which("yt-dlp")),
        "rclone": drive_mod.find_rclone() is not None,
        "rclone_path": drive_mod.find_rclone() or "",
        "version": VERSION,
        "rights_note": RIGHTS_NOTE,
    }


class Handler(BaseHTTPRequestHandler):
    job: Job = None  # set by serve()
    store: Store = None

    # -- helpers
    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: str, ctype: str):
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return {}

    def log_message(self, *args):  # keep console clean
        pass

    # -- routes
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path in ("/", "/index.html"):
                self._send_file(os.path.join(PANEL_DIR, "index.html"), MIME[".html"])
            elif path == "/style.css":
                self._send_file(os.path.join(PANEL_DIR, "style.css"), MIME[".css"])
            elif path == "/app.js":
                self._send_file(os.path.join(PANEL_DIR, "app.js"), MIME[".js"])
            elif path == "/api/status":
                st = self.job.snapshot()
                st["tools"] = _check_tools()
                st["log_tail"] = self.job.log_tail(40)
                # Download progress from the part file on disk — covers
                # jobs that do not write live_status.json themselves
                # (e.g. the rerun script): bytes downloaded so far.
                if not st.get("download"):
                    try:
                        parts = [f for f in os.listdir(self.store.downloads)
                                 if f.endswith(".part")]
                        if parts:
                            biggest = max(
                                parts,
                                key=lambda f: os.path.getsize(
                                    os.path.join(self.store.downloads, f)))
                            st["download"] = {
                                "percent": None,
                                "downloaded_bytes": os.path.getsize(
                                    os.path.join(self.store.downloads, biggest)),
                                "total_bytes": None,
                                "speed": "",
                                "file": biggest,
                            }
                    except OSError:
                        pass
                self._send_json(st)
            elif path == "/api/clips":
                self._send_json({"clips": self.store.load_clips()})
            elif path == "/api/drive-status":
                st = self.job.snapshot()
                self._send_json({
                    "rclone": drive_mod.find_rclone(),
                    "drive_root": drive_mod.DRIVE_ROOT,
                    "drive_folder_link": drive_mod.drive_folder_link(""),
                    "stream_folder": st.get("drive_folder") or "",
                })
            elif path == "/api/log":
                self._send_json({"log": self.job.log_tail(100)})
            elif path == "/api/clip-file":
                qs = parse_qs(parsed.query)
                name = unquote(qs.get("name", [""])[0])
                safe = os.path.basename(name)
                for folder in (self.store.clips, self.store.posted):
                    full = os.path.join(folder, safe)
                    if os.path.exists(full):
                        self._send_video(full)
                        return
                # v1.8.4: the local copy was freed after upload — fetch
                # it back from Drive on demand so the clip can still be
                # watched in the panel (cached locally afterwards).
                rec = next((c for c in self.store.load_clips()
                            if c.get("file") == safe), None)
                if rec and rec.get("drive_status") == "uploaded":
                    ok, dest = drive_mod.download_clip(
                        rec.get("stream_folder") or "", safe,
                        self.store.clips, log=self.job.log)
                    if ok and os.path.exists(dest):
                        self._send_video(dest)
                        return
                self.send_error(404, "Clip not found")
            elif path == "/api/preview":
                qs = parse_qs(parsed.query)
                name = unquote(qs.get("name", [""])[0])
                safe = os.path.basename(name)
                full = os.path.join(self.store.previews, safe)
                if os.path.exists(full) and safe.lower().endswith((".jpg", ".jpeg")):
                    self._send_file(full, "image/jpeg")
                else:
                    self.send_error(404, "Preview not found")
            else:
                self.send_error(404, "Not found")
        except (BrokenPipeError, ConnectionResetError):
            pass  # client closed the connection early (e.g. video seek) — not an error
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            try:
                self._send_json({"error": str(exc)}, status=500)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _send_video(self, full: str):
        size = os.path.getsize(full)
        range_h = self.headers.get("Range")
        if range_h:
            try:
                start, end = range_h.replace("bytes=", "").split("-")
                start = int(start) if start else 0
                end = int(end) if end else size - 1
            except ValueError:
                start, end = 0, size - 1
            # Clamp the range to the file (a browser may ask past EOF).
            start = max(0, min(start, size - 1))
            end = max(start, min(end, size - 1))
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            # NOTE: shutil.copyfileobj(f, w, length) does NOT copy only
            # `length` bytes — its 3rd arg is the BUFFER size, so it
            # copied the whole rest of the file while the header promised
            # only the range. The browser got extra bytes, the next
            # range request on the keep-alive connection read garbage,
            # and playback stalled after 1–2s. Copy exactly `length`
            # bytes instead.
            with open(full, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            return
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        with open(full, "rb") as f:
            shutil.copyfileobj(f, self.wfile)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/fetch":
                body = self._read_json()
                url = (body.get("url") or "").strip()
                try:
                    meta = fetch_mod.get_metadata(url, log=self.job.log)
                    self._send_json({"ok": True, "meta": meta})
                except Exception as exc:  # noqa: BLE001
                    self._send_json({"ok": False, "error": str(exc)})
            elif path == "/api/process":
                body = self._read_json()
                url = (body.get("url") or "").strip()
                if not url:
                    self._send_json({"ok": False, "error": "Paste a YouTube link first."})
                    return
                if self.job.snapshot()["status"] == "running":
                    self._send_json({"ok": False, "error": "A job is already running. Wait for it to finish."})
                    return
                args = {
                    "num_clips": _safe_int(body.get("clips"), 5),
                    "clip_length": _safe_int(body.get("length"), 60),
                    "sensitivity": str(body.get("sensitivity", "med")),
                    "record_minutes": _safe_int(body.get("record_minutes"), 10),
                    "quality": str(body.get("quality", "max")),
                    "focus": str(body.get("focus", "face")),
                    "vod_range": str(body.get("vod_range", "auto")),
                    "free_space": bool(body.get("free_space", True)),
                    "delete_source": bool(body.get("delete_source", False)),
                }
                t = threading.Thread(target=self._run_bg, args=(url, args), daemon=True)
                t.start()
                self._send_json({"ok": True, "message": "Job started."})
            elif path == "/api/clips/bulk":
                self._bulk_clips(self._read_json())
            elif path.startswith("/api/clips/") and path.endswith("/posted"):
                name = unquote(path.split("/")[3])
                self._mark_posted(name)
            elif path.startswith("/api/clips/") and path.endswith("/delete"):
                name = unquote(path.split("/")[3])
                self._delete_clip(name)
            elif path.startswith("/api/clips/") and path.endswith("/retry-upload"):
                name = unquote(path.split("/")[3])
                self._retry_upload(name)
            elif path.startswith("/api/clips/") and path.endswith("/approve"):
                name = unquote(path.split("/")[3])
                self._approve_clip(name)
            elif path.startswith("/api/clips/") and path.endswith("/reject"):
                name = unquote(path.split("/")[3])
                self._reject_clip(name)
            elif path.startswith("/api/clips/") and path.endswith("/change-type"):
                name = unquote(path.split("/")[3])
                self._change_type(name, self._read_json())
            elif path.startswith("/api/clips/") and path.endswith("/reframe"):
                name = unquote(path.split("/")[3])
                self._reframe_clip(name, self._read_json())
            elif path.startswith("/api/clips/") and path.endswith("/edit"):
                name = unquote(path.split("/")[3])
                self._edit_clip(name, self._read_json())
            else:
                self.send_error(404, "Not found")
        except Exception as exc:  # noqa: BLE001
            # Log the full traceback (the panel used to swallow it and
            # only show the one-line error in the browser — the 2026-11-18
            # int(None) crash was undiagnosable from the log alone).
            traceback.print_exc()
            self.job.log(f"API error on {path}: {exc!r}")
            self._send_json({"ok": False, "error": str(exc)}, status=500)

    def _run_bg(self, url, args):
        try:
            run_job(self.job, url, **args)
        except Exception:
            pass  # error already recorded in job state

    def _mark_posted(self, name: str):
        safe = os.path.basename(name)
        clips = self.store.load_clips()
        found = False
        for c in clips:
            if c.get("file") == safe:
                c["posted"] = True
                found = True
        if not found:
            self._send_json({"ok": False, "error": "Clip not found."})
            return
        src = os.path.join(self.store.clips, safe)
        dst = os.path.join(self.store.posted, safe)
        if os.path.exists(src):
            shutil.move(src, dst)
        self.store.save_clips(clips)
        self.job.log(f"Marked as posted: {safe} (moved to posted/)")
        self._send_json({"ok": True})

    def _retry_upload(self, name: str):
        safe = os.path.basename(name)
        clips = self.store.load_clips()
        rec = next((c for c in clips if c.get("file") == safe), None)
        if not rec:
            self._send_json({"ok": False, "error": "Clip not found."})
            return
        local = os.path.join(self.store.clips, safe)
        if not os.path.exists(local):
            self._send_json({"ok": False, "error": (
                "Local clip file is gone (it was freed after a successful "
                "upload, or deleted). The clip record says Drive status: "
                f"{rec.get('drive_status')}. Nothing to re-upload from.")})
            return
        folder = rec.get("stream_folder") or "unknown"
        ok, drive_path, err = drive_mod.upload_clip(local, folder, log=self.job.log)
        rec["drive_path"] = drive_path
        if ok:
            rec["drive_status"] = "uploaded"
            rec["uploaded_at"] = datetime.now().isoformat()
            self.store.save_clips(clips)
            self._send_json({"ok": True, "drive_path": drive_path})
        else:
            rec["drive_status"] = "failed"
            rec["drive_error"] = err
            self.store.save_clips(clips)
            self._send_json({"ok": False, "error": err})

    def _save_manifest_for(self, stream_folder: str):
        """Re-upload the Drive manifest for one stream folder (best effort).

        Runs in a background daemon thread (v1.8.10): the manifest upload
        to Drive can take 10+ seconds (slow/rate-limited), and Approve /
        Reject used to block on it BEFORE answering, so the panel showed
        no feedback and Approve looked broken even though the record WAS
        marked approved. Now the API answers immediately and the manifest
        uploads behind it.
        """
        if not stream_folder:
            return

        def _upload():
            try:
                clips = [c for c in self.store.load_clips()
                         if c.get("stream_folder") == stream_folder]
                ok, err = drive_mod.upload_manifest(stream_folder, clips, log=self.job.log)
                if not ok:
                    self.job.log(f"Manifest upload skipped/failed for {stream_folder}: {err}")
            except Exception:
                self.job.log(f"Manifest upload error for {stream_folder} (ignored).")
                traceback.print_exc()

        threading.Thread(target=_upload, daemon=True).start()

    def _approve_clip(self, name: str):
        safe = os.path.basename(name)
        # Atomic read-modify-write under the clips lock (see Store):
        # two concurrent requests must not load the same list and
        # clobber each other's change.
        def _do(clips):
            rec = next((c for c in clips if c.get("file") == safe), None)
            if not rec:
                return None
            rec["approved"] = True
            rec["approved_at"] = datetime.now().isoformat()
            rec["rejected"] = False
            return rec
        rec = self.store.update_clips(_do)
        if not rec:
            self._send_json({"ok": False, "error": "Clip not found."})
            return
        self.job.log(f"Approved for upload: {safe} (recorded in clips.json + Drive manifest)")
        self._save_manifest_for(rec.get("stream_folder") or "")
        self._send_json({"ok": True, "approved_at": rec["approved_at"]})

    def _find_source(self, rec: dict) -> str:
        """Find the source video for a clip in downloads/ (for trimming)."""
        folder = rec.get("stream_folder") or ""
        vid = folder.rsplit("_", 1)[-1] if "_" in folder else ""
        if not vid:
            return ""
        for name in sorted(os.listdir(self.store.downloads)):
            if name.startswith(vid) and name.lower().endswith((".mp4", ".mkv", ".webm")):
                path = os.path.join(self.store.downloads, name)
                if os.path.getsize(path) > 0:
                    return path
        return ""

    def _edit_clip(self, name: str, body: dict):
        safe = os.path.basename(name)
        clips = self.store.load_clips()
        rec = next((c for c in clips if c.get("file") == safe), None)
        if not rec:
            self._send_json({"ok": False, "error": "Clip not found."})
            return
        # Metadata edits always work (even after the local file was freed).
        changed = []
        if body.get("title") is not None and str(body["title"]).strip():
            rec["title"] = str(body["title"]).strip()
            changed.append("title")
        if body.get("description") is not None:
            rec["description"] = str(body["description"])
            changed.append("description")
        if body.get("hashtags") is not None:
            tags = body["hashtags"]
            if isinstance(tags, str):
                tags = [t.strip() for t in tags.replace(",", " ").split() if t.strip()]
            rec["hashtags"] = tags
            changed.append("hashtags")
        # Trim: needs the source video still on the laptop.
        new_start = body.get("start")
        new_end = body.get("end")
        if new_start is not None and new_end is not None:
            new_start = _safe_int(new_start, rec.get("start", 0))
            new_end = _safe_int(new_end, rec.get("end", 0))
            new_len = new_end - new_start
            if new_start < 0 or new_len < 5 or new_len > 60:
                self._send_json({"ok": False, "error": "Trim must be 5–60 seconds long, start >= 0."})
                return
            if new_start == rec.get("start") and new_len == rec.get("length"):
                pass  # no trim needed
            else:
                src = self._find_source(rec)
                if not src:
                    self._send_json({"ok": False, "error": (
                        "Cannot trim: the source video is no longer on this "
                        "laptop (it was deleted after the job to free space). "
                        "Title / description / hashtags were still saved.")})
                    self.store.save_clips(clips)
                    return
                tmp = os.path.join(self.store.clips, safe + ".trim.mp4")
                clip_mod.cut_vertical(src, new_start, new_len, tmp,
                                      log=self.job.log,
                                      quality=rec.get("quality", "max"),
                                      focus=rec.get("focus", "gameplay"),
                                      facecam_corner=rec.get("facecam_corner", "top-right"),
                                      nudge_x=rec.get("nudge_x", 0),
                                      nudge_y=rec.get("nudge_y", 0))
                cut_info = dict(clip_mod.last_cut_info)
                check = clip_mod.verify_clip(
                    tmp, new_len,
                    target=(cut_info["target_width"], cut_info["target_height"]))
                if not check["vertical_ok"] or not check["audio_ok"]:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                    self._send_json({"ok": False, "error": f"Re-cut failed verification: {check}"})
                    return
                local = os.path.join(self.store.clips, safe)
                os.replace(tmp, local)
                rec.update({
                    "start": new_start, "end": new_end, "length": new_len,
                    "timestamp": moments_mod.fmt_timestamp(new_start),
                    "size_mb": check["size_mb"], "duration": check["duration"],
                    "verified": check,
                    "resolution": f"{check['width']}x{check['height']}",
                    "quality_label": (
                        f"upscaled from {cut_info['upscaled_from']}p"
                        if cut_info["upscaled"]
                        else f"native {cut_info['source_height']}p"),
                    "approved": False, "approved_at": "",
                    "local_freed": False, "freed_mb": 0.0,
                })
                changed.append(f"trim {new_start}s–{new_end}s")
                # Re-upload the trimmed clip over the same Drive path.
                ok, drive_path, err = drive_mod.upload_clip(
                    local, rec.get("stream_folder") or "", log=self.job.log)
                rec["drive_path"] = drive_path
                rec["drive_status"] = "uploaded" if ok else "failed"
                if ok:
                    rec["uploaded_at"] = datetime.now().isoformat()
                else:
                    rec["drive_error"] = err
                self.job.log(f"Trimmed + re-uploaded {safe}: {', '.join(changed)} "
                             f"(Drive: {rec['drive_status']}) — approval reset, please re-approve.")
        self.store.save_clips(clips)
        self._save_manifest_for(rec.get("stream_folder") or "")
        self.job.log(f"Edited clip {safe}: {', '.join(changed) or 'no changes'}")
        self._send_json({"ok": True, "clip": rec})

    def _reject_clip(self, name: str):
        safe = os.path.basename(name)
        # Atomic read-modify-write under the clips lock (see Store).
        def _do(clips):
            rec = next((c for c in clips if c.get("file") == safe), None)
            if not rec:
                return None
            rec["rejected"] = True
            rec["approved"] = False
            rec["approved_at"] = ""
            return rec
        rec = self.store.update_clips(_do)
        if not rec:
            self._send_json({"ok": False, "error": "Clip not found."})
            return
        self.job.log(f"Rejected (not worthy): {safe}")
        self._save_manifest_for(rec.get("stream_folder") or "")
        self._send_json({"ok": True})

    def _recut_record(self, rec: dict, cut_type: str, focus: str = None,
                      facecam_corner: str = None,
                      nudge_x: int = None, nudge_y: int = None) -> tuple[bool, str]:
        """Re-cut a clip in the requested orientation/framing from the
        source video. Returns (ok, error). Needs the source still on
        the laptop; otherwise returns False with an honest error."""
        src = self._find_source(rec)
        if not src:
            return False, ("Cannot re-cut: the source video is no longer on "
                           "this laptop (it was deleted after the job to free "
                           "space).")
        safe = rec["file"]
        local = os.path.join(self.store.clips, safe)
        tmp = local + ".recut.mp4"
        start, length = rec["start"], rec["length"]
        try:
            if cut_type == "VIDEO":
                clip_mod.cut_horizontal(src, start, length, tmp,
                                        log=self.job.log,
                                        quality=rec.get("quality", "max"))
            else:
                clip_mod.cut_vertical(
                    src, start, length, tmp, log=self.job.log,
                    quality=rec.get("quality", "max"),
                    focus=focus if focus is not None else rec.get("focus", "gameplay"),
                    facecam_corner=(facecam_corner if facecam_corner is not None
                                    else rec.get("facecam_corner", "top-right")),
                    nudge_x=nudge_x if nudge_x is not None else (rec.get("nudge_x") or 0),
                    nudge_y=nudge_y if nudge_y is not None else (rec.get("nudge_y") or 0))
            cut_info = dict(clip_mod.last_cut_info)
            check = clip_mod.verify_clip(
                tmp, length,
                target=(cut_info["target_width"], cut_info["target_height"]))
            if not check["size_ok"] or not check["audio_ok"]:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                return False, f"Re-cut failed verification: {check}"
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        os.replace(tmp, local)
        try:
            preview_name = safe + ".jpg"
            clip_mod.extract_preview(src, start + length // 2,
                                     os.path.join(self.store.previews, preview_name),
                                     log=self.job.log)
            rec["preview"] = preview_name
        except Exception as exc:  # noqa: BLE001
            self.job.log(f"WARNING: preview frame failed for {safe}: {exc}")
        rec.update({
            "cut_type": cut_type,
            "size_mb": check["size_mb"], "duration": check["duration"],
            "verified": check,
            "resolution": f"{check['width']}x{check['height']}",
            "quality_label": (
                f"upscaled from {cut_info['upscaled_from']}p"
                if cut_info["upscaled"]
                else f"native {cut_info['source_height']}p"),
            "focus": cut_info.get("focus", rec.get("focus", "gameplay")),
            "facecam_corner": cut_info.get("facecam_corner", ""),
            "nudge_x": cut_info.get("nudge_x", 0),
            "nudge_y": cut_info.get("nudge_y", 0),
            "approved": False, "approved_at": "", "rejected": False,
            "local_freed": False, "freed_mb": 0.0,
        })
        ok, drive_path, err = drive_mod.upload_clip(
            local, rec.get("stream_folder") or "", log=self.job.log)
        rec["drive_path"] = drive_path
        rec["drive_status"] = "uploaded" if ok else "failed"
        if ok:
            rec["uploaded_at"] = datetime.now().isoformat()
        else:
            rec["drive_error"] = err
        return True, ""

    def _change_type(self, name: str, body: dict):
        safe = os.path.basename(name)
        new_type = str(body.get("type", "")).upper()
        if new_type not in ("SHORT", "VIDEO"):
            self._send_json({"ok": False, "error": "type must be SHORT or VIDEO."})
            return
        clips = self.store.load_clips()
        rec = next((c for c in clips if c.get("file") == safe), None)
        if not rec:
            self._send_json({"ok": False, "error": "Clip not found."})
            return
        if rec.get("cut_type") == new_type:
            self._send_json({"ok": True, "clip": rec})
            return
        ok, err = self._recut_record(rec, new_type)
        if not ok:
            self._send_json({"ok": False, "error": err})
            return
        rec["reasons"] = list(rec.get("reasons") or []) + [
            f"changed to {new_type} by user in review"]
        self.store.save_clips(clips)
        self._save_manifest_for(rec.get("stream_folder") or "")
        self.job.log(f"Changed type to {new_type}: {safe} "
                     f"({rec['resolution']}) — approval reset, please re-approve.")
        self._send_json({"ok": True, "clip": rec})

    def _reframe_clip(self, name: str, body: dict):
        safe = os.path.basename(name)
        clips = self.store.load_clips()
        rec = next((c for c in clips if c.get("file") == safe), None)
        if not rec:
            self._send_json({"ok": False, "error": "Clip not found."})
            return
        if rec.get("cut_type") == "VIDEO":
            self._send_json({"ok": False, "error": (
                "VIDEO cuts are full-frame 16:9 (no crop) — reframing only "
                "applies to vertical SHORT cuts.")})
            return
        focus = body.get("focus") or rec.get("focus", "gameplay")
        corner = body.get("facecam_corner") or rec.get("facecam_corner", "top-right")
        # `or 0` (not .get(key, 0)): old records carry nudge_x/nudge_y
        # as explicit None, and .get returns None for a present key —
        # None + int() then crashes the panel (2026-11-18).
        nudge_x = (rec.get("nudge_x") or 0) + _safe_int(body.get("nudge_dx"), 0)
        nudge_y = (rec.get("nudge_y") or 0) + _safe_int(body.get("nudge_dy"), 0)
        if body.get("nudge_x") is not None:
            nudge_x = _safe_int(body.get("nudge_x"), nudge_x)
        if body.get("nudge_y") is not None:
            nudge_y = _safe_int(body.get("nudge_y"), nudge_y)
        ok, err = self._recut_record(rec, "SHORT", focus=focus,
                                     facecam_corner=corner,
                                     nudge_x=nudge_x, nudge_y=nudge_y)
        if not ok:
            self._send_json({"ok": False, "error": err})
            return
        self.store.save_clips(clips)
        self._save_manifest_for(rec.get("stream_folder") or "")
        self.job.log(f"Reframed {safe}: focus={focus} corner={corner} "
                     f"nudge=({nudge_x},{nudge_y}) — approval reset.")
        self._send_json({"ok": True, "clip": rec})

    def _delete_clip(self, name: str):
        safe = os.path.basename(name)
        # Delete the Drive copy FIRST, so a failed Drive delete is
        # reported instead of silently leaving the clip in Drive.
        # The record removal itself is an atomic read-modify-write
        # under the clips lock, so a concurrent request (or the job's
        # save) cannot restore the record after we remove it.
        clips_now = self.store.load_clips()
        rec_now = next((c for c in clips_now if c.get("file") == safe), None)
        stream_folder = (rec_now or {}).get("stream_folder") or ""
        drive_ok, drive_msg = drive_mod.delete_from_drive(
            stream_folder, safe, log=self.job.log)
        def _do(clips):
            clips[:] = [c for c in clips if c.get("file") != safe]
        self.store.update_clips(_do)
        for folder in (self.store.clips, self.store.posted):
            p = os.path.join(folder, safe)
            if os.path.exists(p):
                os.remove(p)
        self.job.log(f"Deleted clip: {safe} (Drive: {drive_msg})")
        self._save_manifest_for(stream_folder)
        self._send_json({"ok": True, "drive_deleted": drive_ok,
                         "drive_message": drive_msg})

    def _bulk_clips(self, body: dict):
        """Bulk review action: approve / reject / delete many clips.

        Body: {"action": "approve"|"reject"|"delete", "files": [names]}.
        Returns per-file results so the panel can show what worked.
        Delete uses the same path as single delete (Drive copy too).
        """
        action = body.get("action")
        files = body.get("files") or []
        if action not in ("approve", "reject", "delete"):
            self._send_json({"ok": False, "error": "Unknown bulk action."})
            return
        if not isinstance(files, list) or not files:
            self._send_json({"ok": False, "error": "No clips given."})
            return
        results: list[dict] = []
        # Drive deletes are slow (network) — do them OUTSIDE the lock,
        # then apply all record changes in ONE atomic update_clips so
        # a concurrent request cannot clobber them.
        to_remove: set[str] = set()
        to_flag: dict[str, dict] = {}
        clips_now = self.store.load_clips()
        for name in files:
            safe = os.path.basename(str(name))
            rec = next((c for c in clips_now if c.get("file") == safe), None)
            if not rec:
                results.append({"file": safe, "ok": False,
                                "error": "Clip not found."})
                continue
            if action == "approve":
                to_flag[safe] = {"approved": True,
                                 "approved_at": datetime.now().isoformat(),
                                 "rejected": False}
                results.append({"file": safe, "ok": True})
            elif action == "reject":
                to_flag[safe] = {"rejected": True, "approved": False}
                results.append({"file": safe, "ok": True})
            else:  # delete
                stream_folder = rec.get("stream_folder") or ""
                drive_ok, drive_msg = drive_mod.delete_from_drive(
                    stream_folder, safe, log=self.job.log)
                to_remove.add(safe)
                for folder in (self.store.clips, self.store.posted):
                    p = os.path.join(folder, safe)
                    if os.path.exists(p):
                        os.remove(p)
                results.append({"file": safe, "ok": True,
                                "drive_deleted": drive_ok,
                                "drive_message": drive_msg})
                self._save_manifest_for(stream_folder)
        def _do(clips):
            if to_remove:
                clips[:] = [c for c in clips
                            if c.get("file") not in to_remove]
            for c in clips:
                flag = to_flag.get(c.get("file") or "")
                if flag:
                    c.update(flag)
        self.store.update_clips(_do)
        self.job.log(f"Bulk {action}: {len([r for r in results if r['ok']])}/{len(results)} ok")
        self._send_json({"ok": True, "results": results})


def serve(store: Store, port: int):
    job = Job(store)
    Handler.job = job
    Handler.store = store
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"Live_Clipping v{VERSION} panel: http://127.0.0.1:{port}")
    print(f"Data folder: {store.base}")
    print("Press Ctrl+C to stop. Panel is localhost-only.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nPanel stopped.")
        server.shutdown()


def main():
    ap = argparse.ArgumentParser(description="Live_Clipping — YouTube live -> Shorts")
    ap.add_argument("--url", help="YouTube live / VOD link")
    ap.add_argument("--clips", type=int, default=5, help="Number of clips (default 5)")
    ap.add_argument("--length", type=int, default=60, choices=[30, 45, 60, 90],
                    help="Clip length in seconds (30/45/60/90). Default 60: "
                         "a Short should be long enough to complete its story.")
    ap.add_argument("--sensitivity", default="med", choices=["low", "med", "high"])
    ap.add_argument("--quality", default="max", choices=["max", "4k", "2k", "1080p"],
                    help="Max = best available (4K if the stream has it)")
    ap.add_argument("--focus", default="face", choices=["gameplay", "face", "facecam"],
                    help="Vertical framing: face = crop follows the main "
                         "face, else the strongest motion, else centre "
                         "(default); gameplay = centre crop; facecam = "
                         "anchor to a corner (see --facecam-corner)")
    ap.add_argument("--vod-range", default="auto",
                    choices=["auto", "full", "30min", "1h", "2h"],
                    help="VOD time range. auto = first 1h for videos over 2h, else full")
    ap.add_argument("--no-free-space", action="store_true",
                    help="Keep local clip files after Drive upload (default: free them)")
    ap.add_argument("--delete-source", action="store_true",
                    help="Delete the source download after the job, only if all clips uploaded")
    ap.add_argument("--record-minutes", type=int, default=10,
                    help="Minutes to record when the link is a live stream")
    ap.add_argument("--out", help="Output folder for clips (default: <data-dir>/clips)")
    ap.add_argument("--data-dir", default="~/YT", help="Data folder (default ~/YT)")
    ap.add_argument("--serve", action="store_true", help="Start the web panel")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args()

    store = Store(args.data_dir)
    if args.out:
        store.clips = os.path.expanduser(args.out)
        os.makedirs(store.clips, exist_ok=True)

    if args.serve or not args.url:
        if args.url and not args.serve:
            pass
        serve(store, args.port)
        return

    job = Job(store)
    try:
        made = run_job(job, args.url, args.clips, args.length,
                       args.sensitivity, args.record_minutes,
                       quality=args.quality, focus=args.focus,
                       vod_range=args.vod_range,
                       free_space=not args.no_free_space,
                       delete_source=args.delete_source)
        print(f"\nDone: {len(made)} clips in {store.clips}")
        for c in made:
            print(f"  - {c['file']}  [{c['timestamp']}] score {c['score']}  "
                  f"{c.get('resolution', '')} {c.get('quality_label', '')}  "
                  f"Drive: {c.get('drive_status', 'pending')}  {c['title']}")
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
