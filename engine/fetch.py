"""Fetch layer: yt-dlp metadata, VOD download, live recording, subtitles.

Only uses yt-dlp + ffmpeg as external tools. No API keys, no paid APIs.
Honest errors: a stream that has not started yet, or a private / blocked
video, raises FetchError with a clear message -- never a fake success.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time

YOUTUBE_RE = re.compile(
    r"^(https?://)?(www\.|m\.)?(youtube\.com/(watch|live|shorts|embed)|youtu\.be/)"
)


class FetchError(Exception):
    pass


def is_youtube_url(url: str) -> bool:
    return bool(url and YOUTUBE_RE.match(url.strip()))


def _ytdlp_cmd():
    """Return the argv prefix for yt-dlp (binary preferred, module fallback)."""
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]
    return ["python3", "-m", "yt_dlp"]


def _run(cmd, timeout=None):
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError as exc:
        raise FetchError(
            "yt-dlp is not installed. Install it with: "
            "pip install yt-dlp  (or brew install yt-dlp on macOS)"
        ) from exc
    except subprocess.TimeoutExpired:
        raise FetchError(f"yt-dlp timed out after {timeout}s: {' '.join(cmd[:3])}")


# --- quality (v1.2: MAX is the default — no more 1080p cap) --------------
# "max" always takes the best available format: 4K if the stream has it,
# 2K if that is the best, otherwise whatever the source offers.
QUALITY_FORMATS = {
    "max": "bv*+ba/b",
    "4k": "bv*[height<=2160]+ba/b[height<=2160]/bv*+ba/b",
    "2k": "bv*[height<=1440]+ba/b[height<=1440]/bv*+ba/b",
    "1080p": "bv*[height<=1080]+ba/b[height<=1080]/bv*+ba/b",
}


def quality_format(quality: str) -> str:
    return QUALITY_FORMATS.get(quality or "max", QUALITY_FORMATS["max"])


# VOD time ranges: yt-dlp --download-sections windows (from the start).
VOD_RANGES = {
    "full": None,
    "30min": "*00:00:00-00:30:00",
    "1h": "*00:00:00-01:00:00",
    "2h": "*00:00:00-02:00:00",
}


def resolve_vod_range(vod_range: str, duration: int) -> str:
    """'auto' -> first 1 hour for videos over 2 hours, else full."""
    if (vod_range or "auto") == "auto":
        return "1h" if (duration or 0) > 7200 else "full"
    return vod_range if vod_range in VOD_RANGES else "full"


def get_metadata(url: str, log=print) -> dict:
    """Return normalised metadata for a YouTube URL. Raises FetchError."""
    if not is_youtube_url(url):
        raise FetchError(
            "That does not look like a YouTube link. "
            "Paste a youtube.com or youtu.be link."
        )
    log(f"Fetching metadata for {url}")
    cmd = _ytdlp_cmd() + [
        "--dump-json", "--no-download", "--no-playlist",
        "--no-warnings", "--no-check-certificates", url,
    ]
    res = _run(cmd, timeout=120)
    if res.returncode != 0:
        err = (res.stderr or res.stdout or "").strip().splitlines()
        msg = err[-1] if err else "yt-dlp failed"
        low = " ".join(err).lower()
        if "private" in low:
            raise FetchError("This video is private. Use a public video or a stream you have access to.")
        if "not available" in low or "removed" in low:
            raise FetchError("This video is unavailable or was removed.")
        if "sign in" in low or "confirm you" in low:
            raise FetchError("YouTube asked for a sign-in / bot check for this video. Try a different public video.")
        raise FetchError(f"Could not read this link: {msg[:300]}")
    try:
        raw = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        raise FetchError("Could not parse YouTube's response for this link.") from exc

    live_status = raw.get("live_status") or ("is_live" if raw.get("is_live") else "not_live")
    thumbs = raw.get("thumbnails") or []
    thumb = thumbs[-1].get("url") if thumbs else ""
    meta = {
        "id": raw.get("id", ""),
        "title": raw.get("title", "Untitled"),
        "channel": raw.get("channel") or raw.get("uploader") or "Unknown channel",
        "channel_id": raw.get("channel_id") or raw.get("uploader_id") or "",
        "duration": raw.get("duration") or 0,
        "is_live": bool(raw.get("is_live")),
        "live_status": live_status,  # is_live | is_upcoming | was_live | not_live | post_live
        "view_count": raw.get("view_count"),
        "description": (raw.get("description") or "")[:500],
        "thumbnail": thumb,
        "webpage_url": raw.get("webpage_url") or url,
        "upload_date": raw.get("upload_date") or "",
    }
    if live_status == "is_upcoming":
        raise FetchError(
            "This stream has not started yet (scheduled / upcoming). "
            "Wait until it is live, or paste the link once it starts."
        )
    log(f"Found: {meta['title']} | {meta['channel']} | "
        f"status={live_status} duration={meta['duration']}s")
    return meta


_SIZE_UNITS = {"B": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3,
               "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3}
_PROGRESS_RE = re.compile(
    r"\[download\]\s+(\d+(?:\.\d+)?)% of\s+([\d.]+)(\w+) at\s+([\d.]+)(\w+/s)")
_PROGRESS_NAKED_RE = re.compile(
    r"\[download\]\s+([\d.]+)(\w+)(?:\s+at\s+([\d.]+)(\w+/s))?")


def _parse_progress_line(line: str) -> dict | None:
    """Parse a yt-dlp progress line into download progress, or None."""
    m = _PROGRESS_RE.search(line)
    if m:
        pct, total, tunit, speed, sunit = m.groups()
        return {
            "percent": float(pct),
            "total_bytes": int(float(total) * _SIZE_UNITS.get(tunit, 1)),
            "downloaded_bytes": int(float(pct) / 100.0 * float(total)
                                    * _SIZE_UNITS.get(tunit, 1)),
            "speed": f"{speed} {sunit}",
            "speed_bytes": float(speed) * _SIZE_UNITS.get(sunit.rstrip("/s"), 1),
        }
    m = _PROGRESS_NAKED_RE.search(line)
    if m and "of" not in line:
        size, unit, speed, sunit = m.groups()
        return {
            "percent": None,  # live stream: no total, show bytes instead
            "total_bytes": None,
            "downloaded_bytes": int(float(size) * _SIZE_UNITS.get(unit, 1)),
            "speed": f"{speed} {sunit}" if speed else "",
            "speed_bytes": float(speed) * _SIZE_UNITS.get((sunit or "").rstrip("/s"), 1)
            if speed else 0,
        }
    return None


def download_vod(meta: dict, downloads_dir: str, log=print,
                 quality: str = "max", vod_range: str = "auto",
                 progress_cb=None) -> str:
    """Download the VOD at the chosen quality and time range.

    quality: max (best available, incl. 4K) / 4k / 2k / 1080p.
    vod_range: full / 30min / 1h / 2h / auto (auto = first 1h when the
    video is over 2 hours long, otherwise full). A long 11-hour VOD is
    never downloaded in full by default — that is the laptop load the
    user wants to avoid.
    Returns the local file path.
    """
    os.makedirs(downloads_dir, exist_ok=True)
    vid = meta["id"] or "video"
    out_tmpl = os.path.join(downloads_dir, f"{vid}.%(ext)s")
    resolved = resolve_vod_range(vod_range, meta.get("duration") or 0)
    section = VOD_RANGES[resolved]
    if section:
        log(f"Downloading source video at quality={quality}, "
            f"range={resolved} ({section}) — not the full video.")
    else:
        log(f"Downloading source video at quality={quality}, full video.")
    cmd = _ytdlp_cmd() + [
        "-f", quality_format(quality),
        "--merge-output-format", "mp4",
        "--no-playlist", "--no-warnings",
        "--retries", "3", "--fragment-retries", "3",
        "-o", out_tmpl, meta["webpage_url"],
    ]
    if section:
        # --download-sections must come before the URL; insert it here.
        cmd = cmd[:-1] + ["--download-sections", section, meta["webpage_url"]]
    # Stream yt-dlp's output so download progress (%, MB, MB/s) can be
    # reported live to the panel via progress_cb and live_status.json.
    # --progress --newline gives one parseable line per update.
    # bufsize=1 (line-buffered) is essential: without it, Python's pipe
    # buffering holds progress lines until the buffer fills, and the
    # panel shows no download progress until the download is nearly
    # done (found in VM panel test 2026-11-17: download stayed None
    # for the first 16 s of a download).
    cmd = cmd[:-1] + ["--progress", "--newline", meta["webpage_url"]]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    last_report = {"pct": -1.0, "at": 0.0}
    last_err = ""
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            prog = _parse_progress_line(line)
            if prog and progress_cb:
                # Throttle: report on >=1% change or every 2 s.
                now = time.time()
                pct = prog.get("percent")
                if pct is None or pct - last_report["pct"] >= 1.0 \
                        or now - last_report["at"] >= 2.0:
                    last_report = {"pct": pct if pct is not None else -1.0, "at": now}
                    progress_cb(prog)
            if "[download]" in line and ("ERROR" in line or "error" in line.lower()):
                last_err = line.strip()[:300]
        proc.wait(timeout=7200)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise FetchError("Download timed out after 2 hours.")
    if proc.returncode != 0:
        raise FetchError(
            "Download failed: " + (last_err or "yt-dlp exited non-zero")
        )
    if progress_cb:
        progress_cb({"percent": 100.0, "total_bytes": None,
                     "downloaded_bytes": None, "speed": "", "speed_bytes": 0})
    for name in os.listdir(downloads_dir):
        if name.startswith(vid + ".") and name.lower().endswith((".mp4", ".mkv", ".webm")):
            path = os.path.join(downloads_dir, name)
            if os.path.getsize(path) > 0:
                log(f"Downloaded source: {path} ({os.path.getsize(path)/1e6:.1f} MB)")
                return path
    raise FetchError("Download finished but no video file was found.")


def get_live_stream_urls(meta: dict, quality: str = "max", log=print,
                         retries: int = 1) -> list[str]:
    """Return the direct stream URL(s) for a live stream via yt-dlp -g.

    WEEI failure (2026-11-17, fixed in v1.2): the old code used
    `-f "b[height<=1080]/b"`. For live streams there is NO combined
    format `b` — only separate HLS video/audio formats — so yt-dlp
    failed with "Requested format is not available" even though the
    stream was live and healthy. The fixed selector is `bv*+ba/b`,
    which returns the video URL first and the audio URL second.
    Retries once after 10 s; the error now includes yt-dlp's own
    last line so a real YouTube-side failure is visible.
    """
    last_err = ""
    for attempt in range(retries + 1):
        res = _run(_ytdlp_cmd() + ["-g", "-f", quality_format(quality),
                                   meta["webpage_url"]], timeout=120)
        urls = [u for u in (res.stdout or "").strip().splitlines() if u.startswith("http")]
        if res.returncode == 0 and urls:
            return urls
        err = (res.stderr or res.stdout or "").strip().splitlines()
        last_err = err[-1][:300] if err else "yt-dlp returned no stream URL"
        log(f"Could not get live stream URL (attempt {attempt + 1}): {last_err}")
        if attempt < retries:
            time.sleep(10)
    raise FetchError(
        "Could not get the live stream URL. The stream may have ended, "
        "or YouTube refused this format. "
        f"Last yt-dlp error: {last_err}"
    )


def _ffprobe_duration(path: str) -> float:
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=60)
        return float((res.stdout or "0").strip() or 0)
    except (ValueError, OSError):
        return 0.0


def _record_live_ytdlp(meta: dict, out: str, minutes: int,
                       quality: str, log=print, progress_cb=None) -> str | None:
    """Primary live capture: let yt-dlp itself record the live stream.

    yt-dlp writes progressively; we bound it with a timeout of
    record-minutes and then check the file is actually playable with
    ffprobe. Returns the path on success, None on failure (caller
    falls back to ffmpeg segments). A timeout-kill can leave an
    unfinalised mp4 — that is why playability is verified, not assumed.
    A watcher thread reports elapsed time + bytes to progress_cb every
    5 s so the panel shows live movement (no fake % — a live stream
    has no total size).
    """
    log(f"Live capture: yt-dlp recording live for up to {minutes} min…")
    cmd = _ytdlp_cmd() + [
        "-f", quality_format(quality),
        "--merge-output-format", "mp4",
        "--no-part", "--no-playlist", "--no-warnings",
        "--retries", "3", "--fragment-retries", "3",
        "-o", out, meta["webpage_url"],
    ]
    started = time.time()
    stop_watcher = threading.Event()

    def _watch():
        while not stop_watcher.wait(5):
            if progress_cb:
                size = os.path.getsize(out) if os.path.exists(out) else 0
                progress_cb({"live": True, "method": "yt-dlp live download",
                             "elapsed_seconds": int(time.time() - started),
                             "segments": 0, "downloaded_bytes": size,
                             "percent": None})

    watcher = threading.Thread(target=_watch, daemon=True)
    try:
        # start_new_session: yt-dlp spawns children (ffmpeg for the
        # merge) that inherit its stdout pipe. A plain terminate() only
        # signals the parent — the children can keep the recording
        # going. Found in live test 2026-11-17: a 1-minute record ran
        # 409 s because terminate() did not stop it. Kill the whole
        # process group instead.
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True,
                                start_new_session=True)
        watcher.start()
        try:
            # 30 s grace for yt-dlp to start writing; the kill at the
            # limit stops the whole process group (see above).
            proc.communicate(timeout=minutes * 60 + 30)
        except subprocess.TimeoutExpired:
            log("Live capture: record-time limit reached, stopping yt-dlp…")
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                proc.terminate()
            try:
                proc.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
                proc.communicate()
            log("Live capture: yt-dlp stopped at the record-time limit.")
    except OSError as exc:
        log(f"Live capture: could not start yt-dlp: {exc}")
        return None
    finally:
        stop_watcher.set()
    if os.path.exists(out) and os.path.getsize(out) > 0:
        dur = _ffprobe_duration(out)
        if dur > 0:
            log(f"Live capture (yt-dlp): {out} ({dur:.0f}s, "
                f"{os.path.getsize(out) / 1e6:.1f} MB)")
            return out
        log("Live capture: yt-dlp file exists but is not playable "
            "(no duration — likely unfinalised after the time limit).")
    else:
        log("Live capture: yt-dlp produced no file.")
    return None


def _record_live_ffmpeg_segments(meta: dict, downloads_dir: str, minutes: int,
                                 quality: str, log=print,
                                 segment_seconds: int = 60,
                                 progress_cb=None) -> str | None:
    """Fallback live capture: ffmpeg HLS capture in short segments.

    Records the running stream in 60 s chunks from the direct stream
    URL(s). A lost stream only loses the current chunk, never everything
    recorded so far. After each segment lands, its peak audio energy is
    measured right away (moment detection on the segments as they
    arrive) and reported via progress_cb, so the panel shows segments
    captured + current moment count while still live. Segments are then
    concatenated (stream copy) into one file for full moment detection.
    Returns the combined path or None.
    """
    try:
        urls = get_live_stream_urls(meta, quality, log=log)
    except FetchError:
        return None
    vid = meta["id"] or "live"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    seg_dir = os.path.join(downloads_dir, f"{vid}_live_{stamp}_segments")
    os.makedirs(seg_dir, exist_ok=True)
    inputs: list[str] = []
    if len(urls) >= 2:
        inputs = ["-i", urls[0], "-i", urls[1]]
    else:
        inputs = ["-i", urls[0]]
    total = minutes * 60
    elapsed = 0
    segments: list[str] = []
    seg_energies: list = []
    failures = 0
    seg_no = 0
    log(f"Live capture: ffmpeg segment recording ({segment_seconds}s chunks, "
        f"up to {minutes} min)…")
    while elapsed < total and failures < 2:
        seg_no += 1
        chunk = min(segment_seconds, total - elapsed)
        seg = os.path.join(seg_dir, f"seg_{seg_no:03d}.mp4")
        cmd = ["ffmpeg", "-y", "-v", "error"] + inputs + [
            "-t", str(chunk), "-c", "copy", "-movflags", "+faststart", seg]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True,
                                 timeout=chunk + 120)
        except subprocess.TimeoutExpired:
            res = None
        if res is not None and res.returncode == 0 and os.path.exists(seg) \
                and os.path.getsize(seg) > 0 and _ffprobe_duration(seg) > 0:
            segments.append(seg)
            elapsed += chunk
            failures = 0
            # Moment detection on the segment as it lands: peak energy.
            peak = None
            try:
                from engine import moments as _moments
                tmp_wav = os.path.join(seg_dir, f"seg_{seg_no:03d}.wav")
                _moments.extract_audio(seg, tmp_wav, log=lambda *a: None)
                energies = _moments.compute_energies(tmp_wav, log=lambda *a: None)
                peak = round(max(energies), 4) if energies else 0.0
                try:
                    os.remove(tmp_wav)
                except OSError:
                    pass
            except Exception:
                peak = None
            seg_energies.append(peak)
            log(f"Live capture: segment {seg_no} recorded "
                f"({elapsed // 60}m {elapsed % 60}s elapsed, "
                f"{len(segments)} segment(s) so far, "
                f"segment peak energy={peak})")
            if progress_cb:
                progress_cb({"live": True, "method": "ffmpeg HLS segments (60s chunks)",
                             "elapsed_seconds": elapsed,
                             "segments": len(segments),
                             "segment_energies": list(seg_energies),
                             "moments_so_far": sum(1 for e in seg_energies
                                                 if e is not None and e > 0.15),
                             "percent": None})
        else:
            failures += 1
            err = ((res.stderr or "")[:200] if res else "ffmpeg timed out")
            log(f"Live capture: segment {seg_no} failed ({err}) — "
                f"failure {failures}/2")
            if os.path.exists(seg):
                try:
                    os.remove(seg)
                except OSError:
                    pass
    if not segments:
        return None
    combined = os.path.join(downloads_dir, f"{vid}_live_{stamp}.mp4")
    if len(segments) == 1:
        shutil.copyfile(segments[0], combined)
    else:
        list_file = os.path.join(seg_dir, "concat.txt")
        with open(list_file, "w", encoding="utf-8") as f:
            for s in segments:
                f.write(f"file '{s}'\n")
        res = subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0",
             "-i", list_file, "-c", "copy", "-movflags", "+faststart", combined],
            capture_output=True, text=True, timeout=900)
        if res.returncode != 0 or not os.path.exists(combined):
            log(f"Live capture: segment concat failed: {(res.stderr or '')[:200]}")
            return None
    dur = _ffprobe_duration(combined)
    log(f"Live capture (ffmpeg segments): {combined} ({dur:.0f}s from "
        f"{len(segments)} segment(s), {os.path.getsize(combined) / 1e6:.1f} MB)")
    return combined


# Last live-capture info for the panel (method + segment count).
last_live_info: dict = {}


def record_live(meta: dict, downloads_dir: str, minutes: int, log=print,
                quality: str = "max", progress_cb=None) -> str:
    """Capture a running live stream in real time — never by downloading
    a finished VOD afterwards. This is the CORE live mode, not a fallback.

    1. Primary: yt-dlp records the live stream itself (bounded by the
       record time), verified playable with ffprobe. Retried through
       the caller's two attempts: yt-dlp direct first, then ffmpeg HLS.
    2. Fallback: ffmpeg HLS capture in 60 s segments, concatenated.
       Moment detection runs on each segment as it lands (peak energy
       per segment, reported via progress_cb).
    Raises FetchError only if both fail, with the exact honest error.
    Returns the recorded file path.
    """
    global last_live_info
    os.makedirs(downloads_dir, exist_ok=True)
    vid = meta["id"] or "live"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = os.path.join(downloads_dir, f"{vid}_live_{stamp}.mp4")
    log(f"Live stream detected — capturing live for the next {minutes} minute(s)...")
    # Attempt 1+2 are inside the helpers (yt-dlp once, then ffmpeg
    # segments with its own URL retry); the panel shows the honest error
    # if both fail — never a fake success, never a VOD download instead.
    path = _record_live_ytdlp(meta, out, minutes, quality, log=log,
                              progress_cb=progress_cb)
    if path:
        last_live_info = {"method": "yt-dlp live download", "segments": 1,
                          "elapsed_seconds": minutes * 60,
                          "segment_energies": [], "moments_so_far": 0}
        return path
    log("Live capture: yt-dlp path failed or unplayable — "
        "falling back to ffmpeg segment capture.")
    path = _record_live_ffmpeg_segments(meta, downloads_dir, minutes, quality,
                                        log=log, progress_cb=progress_cb)
    if path:
        dur = _ffprobe_duration(path)
        last_live_info = {"method": "ffmpeg HLS segments (60s chunks)",
                          "segments": max(1, int(dur // 60)),
                          "elapsed_seconds": int(dur),
                          "segment_energies": [],
                          "moments_so_far": 0}
        return path
    last_live_info = {"method": "failed", "segments": 0, "elapsed_seconds": 0,
                      "segment_energies": [], "moments_so_far": 0}
    raise FetchError(
        "Live capture failed: neither yt-dlp live download nor ffmpeg "
        "segment capture produced a playable recording. The stream may "
        "have ended, or YouTube refused the stream. Try again while the "
        "stream is live, or use a different link."
    )


def fetch_subtitles(meta: dict, downloads_dir: str, log=print) -> str | None:
    """Best-effort auto-subtitle download (English). Returns .vtt path or None."""
    os.makedirs(downloads_dir, exist_ok=True)
    vid = meta["id"] or "video"
    base = os.path.join(downloads_dir, vid)
    cmd = _ytdlp_cmd() + [
        "--write-auto-subs", "--write-subs", "--sub-lang", "en.*",
        "--sub-format", "vtt", "--skip-download", "--no-playlist",
        "--no-warnings", "-o", base, meta["webpage_url"],
    ]
    res = _run(cmd, timeout=300)
    # yt-dlp can exit non-zero (e.g. no manual subtitles) yet still write
    # the auto-subtitle file, so always check for the file first.
    for name in os.listdir(downloads_dir):
        if name.startswith(vid + ".") and name.endswith(".vtt"):
            log(f"Subtitles found: {name} (keyword boost enabled).")
            return os.path.join(downloads_dir, name)
    log("No subtitles available for this video (keyword boost skipped).")
    return None
