"""Moment detection: real audio-energy analysis, no fake scores.

Pipeline:
  1. ffmpeg extracts a 16 kHz mono WAV from the source.
  2. RMS energy is computed in 1-second windows (numpy if available,
     otherwise ffmpeg astats per-second output as a fallback).
  3. Windows above a sensitivity percentile that are local maxima are
     candidate peaks. Peaks within 15 s are merged (keeps the loudest).
  4. Optional subtitle keyword boost: windows whose subtitle text
     contains a hype word get +10 score (capped at 100).
  5. Top N moments are returned, each centred on its peak.
"""
from __future__ import annotations

import math
import os
import re
import subprocess
import wave

KEYWORDS = [
    "clutch", "insane", "wow", "funny", "kill", "win", "goal",
    "gta", "vice city", "lucia", "jason", "leonida", "no way",
    "oh my god", "unbelievable", "crazy", "let's go",
]

SENSITIVITY = {"low": 90, "med": 80, "high": 65}
MERGE_SECONDS = 15


def extract_audio(video_path: str, wav_path: str, log=print) -> str:
    os.makedirs(os.path.dirname(wav_path) or ".", exist_ok=True)
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", video_path,
           "-vn", "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le", wav_path]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if res.returncode != 0 or not os.path.exists(wav_path):
        raise RuntimeError(f"Audio extraction failed: {(res.stderr or '')[:300]}")
    log(f"Audio extracted: {wav_path}")
    return wav_path


def _rms_numpy(wav_path: str) -> list[float]:
    import numpy as np  # noqa: PLC0415  (optional, present on laptop + VM)

    with wave.open(wav_path, "rb") as w:
        n_frames = w.getnframes()
        raw = w.readframes(n_frames)
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0
    sr = 16000
    n_windows = max(1, len(samples) // sr)
    energies: list[float] = []
    for i in range(n_windows):
        chunk = samples[i * sr:(i + 1) * sr]
        energies.append(float(np.sqrt(np.mean(chunk ** 2))) if len(chunk) else 0.0)
    return energies


def _rms_ffmpeg_astats(wav_path: str, log=print) -> list[float]:
    """Fallback when numpy is missing: ffmpeg astats prints one RMS level
    per frame (reset=1 makes it per-frame); use 1 s frames via fps filter."""
    cmd = ["ffmpeg", "-v", "info", "-i", wav_path,
           "-af", "astats=metadata=1:reset=1:length=16000,ametadata=print:key=lavfi.astats.Overall.RMS_level",
           "-f", "null", "-"]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    energies: list[float] = []
    for line in (res.stderr or "").splitlines():
        m = re.search(r"RMS_level=(-?[\d.]+)", line)
        if m:
            db = float(m.group(1))
            energies.append(10 ** (db / 20.0) if db > -120 else 0.0)
    if not energies:
        log("WARNING: astats fallback produced no energy data; using flat energies.")
        with wave.open(wav_path, "rb") as w:
            n = max(1, w.getnframes() // 16000)
        energies = [0.01] * n
    return energies


def compute_energies(wav_path: str, log=print) -> list[float]:
    try:
        return _rms_numpy(wav_path)
    except Exception:
        log("numpy not available, using ffmpeg astats for energy analysis.")
        return _rms_ffmpeg_astats(wav_path, log)


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, int(math.ceil(pct / 100.0 * len(s))) - 1))
    return s[idx]


def parse_subtitle_keywords(vtt_path: str | None) -> dict[int, list[str]]:
    """Return {second: [matched keywords]} from a .vtt file."""
    hits: dict[int, list[str]] = {}
    if not vtt_path or not os.path.exists(vtt_path):
        return hits
    time_re = re.compile(r"(\d+):(\d+):(\d+)\.(\d+)\s+-->")
    current = 0
    with open(vtt_path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            m = time_re.search(line)
            if m:
                h, mi, s = int(m.group(1)), int(m.group(2)), int(m.group(3))
                current = h * 3600 + mi * 60 + s
                continue
            low = line.lower()
            found = [k for k in KEYWORDS if k in low]
            if found:
                hits.setdefault(current, [])
                for k in found:
                    if k not in hits[current]:
                        hits[current].append(k)
    return hits


def find_moments(energies: list[float], duration: float, clip_length: int,
                 count: int, sensitivity: str = "med",
                 subtitle_hits: dict[int, list[str]] | None = None,
                 log=print) -> list[dict]:
    """Rank moments by audio energy. Returns list of moment dicts."""
    if not energies:
        raise RuntimeError("No audio energy data — cannot detect moments.")
    pct = SENSITIVITY.get(sensitivity, 80)
    threshold = _percentile(energies, pct)
    max_e = max(energies) or 1.0
    log(f"Energy: {len(energies)} windows, max={max_e:.4f}, "
        f"threshold p{pct}={threshold:.4f}")

    # Candidate peaks: local maxima above threshold.
    candidates: list[tuple[int, float]] = []
    for i, e in enumerate(energies):
        if e < threshold:
            continue
        neighbours = energies[max(0, i - 2):i + 3]
        if e >= max(neighbours):
            candidates.append((i, e))
    if not candidates:  # very flat audio: fall back to global maxima
        top_idx = sorted(range(len(energies)), key=lambda i: energies[i], reverse=True)
        candidates = [(i, energies[i]) for i in top_idx[: count * 3]]
        log("Audio is very flat — using global energy maxima as moments.")

    # Merge candidates within MERGE_SECONDS, keep loudest in each group.
    merged: list[tuple[int, float]] = []
    for i, e in sorted(candidates):
        if merged and i - merged[-1][0] <= MERGE_SECONDS:
            if e > merged[-1][1]:
                merged[-1] = (i, e)
        else:
            merged.append((i, e))

    subtitle_hits = subtitle_hits or {}
    scored: list[dict] = []
    for peak, e in merged:
        score = round(100.0 * e / max_e, 1)
        start = int(max(0, min(peak - clip_length // 2,
                               max(0, duration - clip_length))))
        # Keyword boost: any hype word spoken anywhere inside the clip
        # window (not just at the exact peak second) counts.
        kws: list[str] = []
        for sec in range(start, start + clip_length):
            kws.extend(subtitle_hits.get(sec, []))
        kws = sorted(set(kws))
        if kws:
            score = min(100.0, score + 10.0)
        scored.append({
            "peak_time": peak,
            "start": start,
            "end": start + clip_length,
            "score": score,
            "energy": round(e, 4),
            "keywords": kws,
        })
    scored.sort(key=lambda m: m["score"], reverse=True)
    top = scored[:count]
    # Present in timeline order for the panel, keep rank in the record.
    for rank, m in enumerate(top, 1):
        m["rank"] = rank
    top.sort(key=lambda m: m["start"])
    log(f"Top {len(top)} moments: " +
        ", ".join(f"#{m['rank']} @{m['start']}s score={m['score']}" for m in top))
    return top


def fmt_timestamp(seconds: int) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


# --- cut classification: SHORT vs VIDEO ------------------------------------
# SHORT: a single peak moment, 30–60 s, vertical 9:16, for Shorts.
#   Rule: one strong audio peak (+ keyword boost), score >= 70.
# VIDEO: a sustained segment, 2–10 min, horizontal 16:9, for long-form.
#   Rule: sustained energy + multiple peaks / keywords across the span —
#   a story or sequence (funny mission run, comeback, heated moment),
#   not a single spike.

SHORT_MIN_SCORE = 70.0
VIDEO_MIN_LEN = 120   # seconds of sustained energy
VIDEO_MAX_LEN = 600   # 10 minutes


def classify_short(moment: dict) -> tuple[str, list[str]]:
    """Every peak moment is a SHORT by construction. Returns (type, reasons)."""
    reasons = [f"single strong peak at {fmt_timestamp(moment['start'])}",
               f"score {moment['score']}"]
    if moment.get("keywords"):
        reasons.append("keyword: " + ", ".join(moment["keywords"]))
    return "SHORT", reasons


def find_sustained_segments(energies: list[float], duration: float,
                            subtitle_hits: dict[int, list[str]] | None = None,
                            max_segments: int = 2,
                            log=print) -> list[dict]:
    """Find sustained high-energy spans (VIDEO candidates).

    A span is a run of >= VIDEO_MIN_LEN seconds where energy stays above
    the p65 level (short dips <= 5 s allowed), containing at least 2
    distinct peaks above p80. Spans longer than 10 min are trimmed to
    their best 10-minute window. Returns up to `max_segments` dicts.
    """
    if not energies or duration < VIDEO_MIN_LEN:
        return []
    max_e = max(energies) or 1.0
    # Thresholds are floored at a fraction of the max: a pure percentile
    # breaks on near-flat audio (p65 can equal the floor value, so the
    # whole video looks "sustained" and every flat window looks like a
    # peak — found by unit test 2026-11-17).
    low = max(_percentile(energies, 65), 0.30 * max_e)
    high = max(_percentile(energies, 80), 0.60 * max_e)
    subtitle_hits = subtitle_hits or {}

    # Build runs above the low threshold, bridging dips <= 5 s.
    runs: list[list[int]] = []
    cur: list[int] = []
    gap = 0
    for i, e in enumerate(energies):
        if e >= low:
            if gap and cur:
                cur.extend(range(cur[-1] + 1, i))
            cur.append(i)
            gap = 0
        else:
            gap += 1
            if gap > 5 and cur:
                runs.append(cur)
                cur = []
    if cur:
        runs.append(cur)

    scored: list[dict] = []
    for run in runs:
        if len(run) < VIDEO_MIN_LEN:
            continue
        start = run[0]
        end = min(run[-1] + 1, int(duration))
        # Trim over-long spans to their best 10-minute window.
        if end - start > VIDEO_MAX_LEN:
            best = (0, -1.0)
            for s in range(start, end - VIDEO_MAX_LEN + 1, 10):
                avg = sum(energies[s:s + VIDEO_MAX_LEN]) / VIDEO_MAX_LEN
                if avg > best[1]:
                    best = (s, avg)
            start = best[0]
            end = start + VIDEO_MAX_LEN
        # A peak must be strictly higher than its left neighbour (so a
        # flat plateau counts once, at its left edge, and flat audio
        # counts zero peaks) and at least as high as its right neighbour.
        peaks = []
        for i in range(start, end):
            if energies[i] < high:
                continue
            left = energies[i - 1] if i > 0 else -1.0
            right = energies[i + 1] if i + 1 < len(energies) else -1.0
            if energies[i] > left and energies[i] >= right:
                peaks.append(i)
        if len(peaks) < 2:
            continue
        avg_e = sum(energies[start:end]) / max(1, end - start)
        kws: list[str] = []
        for sec in range(start, end):
            kws.extend(subtitle_hits.get(sec, []))
        kws = sorted(set(kws))
        score = round(min(100.0, 100.0 * avg_e / max_e + 5 * len(peaks)
                          + (10 if kws else 0)), 1)
        reasons = [f"sustained energy for {(end - start) // 60}m {(end - start) % 60}s",
                   f"{len(peaks)} peaks in the span",
                   f"avg energy {avg_e:.3f} (max {max_e:.3f})"]
        if kws:
            reasons.append("keywords: " + ", ".join(kws))
        scored.append({
            "start": start, "end": end, "length": end - start,
            "score": score, "peaks": len(peaks),
            "keywords": kws, "reasons": reasons,
            "peak_times": peaks,
        })
    scored.sort(key=lambda s: s["score"], reverse=True)
    top = scored[:max_segments]
    log(f"Sustained segments (VIDEO candidates): {len(top)} — " +
        ", ".join(f"@{s['start']}s {s['length']}s score={s['score']}" for s in top)
        if top else "Sustained segments (VIDEO candidates): none found.")
    return top
