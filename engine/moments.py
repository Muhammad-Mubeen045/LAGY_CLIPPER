"""Moment detection: audio + visual + motion signals, no fake scores.

Pipeline:
  1. ffmpeg extracts a 16 kHz mono WAV from the source.
  2. RMS energy is computed in 1-second windows (numpy if available,
     otherwise ffmpeg astats per-second output as a fallback).
  3. Visual signals are sampled too: shot boundaries (fast ffmpeg
     scene detection), face presence (YuNet, one frame every 5 s),
     and motion energy (OpenCV frame difference at 2 fps — car
     drifting and action are motion, not loud audio). Audio alone
     misses the quiet moments people love — a romantic scene or a
     kiss is a sustained close-up face, not a loud peak — so the
     score combines audio + face + shot + motion + sharp onsets
     (gunshots) + quiet close-up faces (romance/kiss).
  4. Windows above a sensitivity percentile of the combined score
     that are local maxima are candidate peaks. Peaks within 15 s
     are merged (keeps the highest).
  5. Optional subtitle keyword boost: windows whose subtitle text
     contains a hype word get +10 score (capped at 100).
  6. Selection is type-diverse: the best moment of each signal type
     (loud, motion, gunshot, romance, face, shot, dialogue,
     surprise, arc, reaction) is taken first, so one chunk returns
     a mix of kinds instead of five loud peaks.
  7. Top N moments are returned, each with a story-complete window:
     the clip edges align to shot boundaries and never cut a subtitle
     line in half, so the Short tells its whole beat — best moves and
     full dialogue — instead of a mid-shot fragment.
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

# Combined moment score weights -- human psyche first. A human does
# not pick a moment because it is loud. They pick it because
# something is SAID (dialogue -- a joke, a reaction, a story beat),
# because it SURPRISES them (quiet, then a burst), because it BUILDS
# to a payoff (tension rising into a peak), or because someone
# REACTS (a face appearing where there was none). Loudness, faces
# and motion are still signals, but they no longer lead. Dialogue is
# the strongest single signal: what people say is what viewers
# remember, quote and share.
W_AUDIO = 0.10
W_FACE = 0.05
W_SHOT = 0.05
W_MOTION = 0.10
W_TRANSIENT = 0.05
W_QUIET_FACE = 0.05
W_DIALOGUE = 0.20
W_CONTRAST = 0.15
W_ARC = 0.10
W_REACTION = 0.05
W_INTIMATE = 0.10
# A face counts as a close-up when it covers this much of the width.
FACE_CLOSEUP_W = 0.25
# How far around a window to look for a face sample / shot change.
FACE_WINDOW_S = 6.0
SHOT_WINDOW_S = 2.0


def compute_motion_energies(video_path: str, duration: float,
                            log=print) -> list[float]:
    """Per-second motion energy: mean frame difference at low fps.

    Car drifting, chases and big action are high sustained motion,
    not loud audio. Sample at 2 fps, downscaled to 320x180, and
    measure the mean absolute difference between consecutive frames.
    Returns one value per second (0..1, normalised to the max).
    Falls back to zeros if OpenCV is missing.
    """
    try:
        import cv2  # noqa: PLC0415
        import numpy as np  # noqa: PLC0415
    except Exception as exc:
        log(f"Motion sampling skipped (no cv2/numpy: {exc}) — motion=0.")
        return [0.0] * max(1, int(duration))
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        log("Motion sampling skipped (cannot open video) — motion=0.")
        return [0.0] * max(1, int(duration))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    step = max(1, int(round(fps / 2.0)))  # 2 fps
    per_sec: dict[int, list[float]] = {}
    prev: object = None
    idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if idx % step == 0:
            small = cv2.resize(frame, (320, 180))
            gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)
            if prev is not None:
                diff = float(np.mean(np.abs(gray - prev)))
                sec = int(idx / fps)
                per_sec.setdefault(sec, []).append(diff)
            prev = gray
        idx += 1
    cap.release()
    n = max(1, int(duration))
    out = [0.0] * n
    for sec, vals in per_sec.items():
        if 0 <= sec < n:
            out[sec] = sum(vals) / len(vals)
    mx = max(out) or 1.0
    out = [v / mx for v in out]
    log(f"Motion energies: {len(out)} windows, max normalised to 1.0.")
    return out


def detect_shot_changes(video_path: str, log=print) -> list[float]:
    """Shot boundaries via ffmpeg scene detection (one fast pass).

    Returns sorted shot-change timestamps in seconds. A new shot is
    a new beat — a moment should start near one, not mid-shot.
    """
    cmd = ["ffmpeg", "-v", "info", "-i", video_path,
           "-vf", "select='gt(scene,0.35)',showinfo",
           "-f", "null", "-"]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    times: list[float] = []
    for line in (res.stderr or "").splitlines():
        if "showinfo" not in line:
            continue
        m = re.search(r"pts_time:([\d.]+)", line)
        if m:
            times.append(float(m.group(1)))
    times = sorted(set(round(t, 2) for t in times))
    log(f"Shot changes: {len(times)} detected.")
    return times


def parse_subtitle_segments(vtt_path: str | None) -> list[dict]:
    """Return [{start, end, text}] dialogue lines from a .vtt file."""
    segs: list[dict] = []
    if not vtt_path or not os.path.exists(vtt_path):
        return segs
    time_re = re.compile(
        r"(\d+):(\d+):(\d+)\.(\d+)\s+-->\s+(\d+):(\d+):(\d+)\.(\d+)")

    def _to_s(h: str, mi: str, s: str, ms: str) -> float:
        return int(h) * 3600 + int(mi) * 60 + int(s) + int(ms) / 1000.0

    cur: dict | None = None
    with open(vtt_path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            m = time_re.search(line)
            if m:
                g = m.groups()
                cur = {"start": _to_s(*g[:4]), "end": _to_s(*g[4:]),
                       "text": ""}
                segs.append(cur)
                continue
            if cur is not None and line and not line.isdigit():
                cur["text"] += (" " + line) if cur["text"] else line
    return [s for s in segs if s["text"]]


def _face_score(second: float, face_samples: list[dict]) -> tuple[float, str]:
    """Visual face score for one second of video.

    Sustained face presence wins: a close-up held across samples is
    the romantic/kiss case. A single brief face flash scores low.
    Returns (score 0..1, label).
    """
    if not face_samples:
        return 0.0, ""
    nearby = [s for s in face_samples
              if abs(s["t"] - second) <= FACE_WINDOW_S and s["faces"] > 0]
    if not nearby:
        return 0.0, ""
    max_w = max(s["max_w"] for s in nearby)
    if len(nearby) >= 2 and max_w >= FACE_CLOSEUP_W:
        return 1.0, "close-up face"
    if len(nearby) >= 2:
        return 0.6, "faces on screen"
    if max_w >= FACE_CLOSEUP_W:
        return 0.4, "brief close-up"
    return 0.0, ""


def _shot_score(second: float, shot_changes: list[float]) -> float:
    """1.0 when a shot starts near this second, else 0.0."""
    if not shot_changes:
        return 0.0
    return 1.0 if any(abs(t - second) <= SHOT_WINDOW_S
                      for t in shot_changes) else 0.0


def _transient_score(second: int, energies: list[float], max_e: float) -> float:
    """Sharp audio onset: a sudden jump from the previous second.

    Gunshots and impacts are short, sharp bursts — a big jump in
    energy, not a sustained loud level. Normalised 0..1.
    """
    if second <= 0 or second >= len(energies) or max_e <= 0:
        return 0.0
    jump = energies[second] - energies[second - 1]
    return max(0.0, min(1.0, jump / max_e))


def _quiet_face_score(second: float, audio_norm: float,
                      face_samples: list[dict]) -> float:
    """Quiet close-up face: the romance/kiss case.

    A love scene or kiss is quiet audio with a sustained close-up
    face held on screen — the opposite of a loud peak. Returns 1.0
    when the audio is low and a close-up face is held across
    samples, else 0.0.
    """
    if audio_norm >= 0.30 or not face_samples:
        return 0.0
    nearby = [s for s in face_samples
              if abs(s["t"] - second) <= FACE_WINDOW_S and s["faces"] > 0]
    if not nearby:
        return 0.0
    max_w = max(s["max_w"] for s in nearby)
    if len(nearby) >= 2 and max_w >= FACE_CLOSEUP_W:
        return 1.0
    return 0.0


def _intimate_score(second: float, audio_norm: float,
                    face_samples: list[dict]) -> float:
    """Two faces close in a quiet moment: the romance/adult case.

    An intimate scene (bed, kiss, close two-person moment) is quiet
    audio with TWO faces held on screen together -- two people close,
    not one face alone. Returns 1.0 when the audio is low and two or
    more faces are present across sustained samples, else 0.0.
    """
    if audio_norm >= 0.30 or not face_samples:
        return 0.0
    nearby = [s for s in face_samples
              if abs(s["t"] - second) <= FACE_WINDOW_S and s["faces"] > 0]
    if not nearby:
        return 0.0
    max_faces = max(s["faces"] for s in nearby)
    if len(nearby) >= 2 and max_faces >= 2:
        return 1.0
    return 0.0


def _dialogue_score(second: float, subtitle_segments: list[dict]) -> float:
    """How much dialogue is spoken around this second.

    A human picks moments for what people SAY -- a joke, a reaction,
    a story beat. Count the subtitle words overlapping a 10 s window
    centred on this second. Dense dialogue (~40 words per 10 s) is
    1.0. Returns 0.0 when no subtitles are available, so the signal
    simply stays off instead of breaking the run.
    """
    if not subtitle_segments:
        return 0.0
    lo, hi = second - 5.0, second + 5.0
    words = 0
    for seg in subtitle_segments:
        if seg["end"] > lo and seg["start"] < hi:
            words += len(seg["text"].split())
    return min(1.0, words / 40.0)


def _contrast_score(second: int, energies: list[float], max_e: float) -> float:
    """Surprise: quiet before, loud now.

    A jumpscare, a reveal, a punchline landing -- a human notices the
    CHANGE, not the level. Mean energy in the 3 s from this second
    minus mean energy in the 10 s before it, normalised. Returns
    0..1 (0 when the before-window is already loud).
    """
    if second < 10 or second >= len(energies) or max_e <= 0:
        return 0.0
    before = energies[max(0, second - 10):second]
    after = energies[second:min(len(energies), second + 3)]
    if not before or not after:
        return 0.0
    jump = (sum(after) / len(after)) - (sum(before) / len(before))
    return max(0.0, min(1.0, jump / max_e))


def _arc_score(second: int, energies: list[float], max_e: float) -> float:
    """Build-up: energy rising into this peak.

    A scene that builds -- a chase getting closer, an argument heating
    up, tension rising -- then pays off is a story beat, not a random
    spike. Compare the smoothed energy right at this second with the
    smoothed energy 15 s earlier. Returns 0..1.
    """
    if second < 15 or second >= len(energies) or max_e <= 0:
        return 0.0

    def _mean(a: int, b: int) -> float:
        vals = energies[max(0, a):min(len(energies), b)]
        return sum(vals) / len(vals) if vals else 0.0

    now = _mean(second - 1, second + 2)
    before = _mean(second - 16, second - 13)
    return max(0.0, min(1.0, (now - before) / max_e))


def _reaction_score(second: float, face_samples: list[dict]) -> float:
    """A face reacting: faces appear after being absent.

    A human notices when someone turns to camera, laughs, or reacts
    -- a face that was NOT there a moment ago. Returns 1.0 when no
    face was sampled in the 9 s before but a face is present now,
    else 0.0. On a stream with a permanent facecam this stays 0,
    which is correct: a face that never leaves is not a reaction.
    """
    if not face_samples:
        return 0.0
    now = [s for s in face_samples
           if abs(s["t"] - second) <= 3 and s["faces"] > 0]
    if not now:
        return 0.0
    before = [s for s in face_samples
              if second - 9 <= s["t"] < second - 3 and s["faces"] > 0]
    if before:
        return 0.0
    return 1.0


def expand_to_story(start: int, end: int, peak: int, clip_length: int,
                    duration: float,
                    shot_changes: list[float] | None = None,
                    subtitle_segments: list[dict] | None = None,
                    log=print) -> tuple[int, int]:
    """Stretch a moment window so the Short tells its whole beat.

    1. Edges align to shot boundaries (within 8 s) so the clip does
       not start or end mid-shot.
    2. Edges never cut a subtitle line in half (within 5 s) so the
       dialogue is complete.
    The window stays within [0, duration] and within
    [clip_length - 5, clip_length + 10] seconds long — a Short stays
    a Short; a story that needs more than the cap belongs in a VIDEO.
    """
    new_start, new_end = start, end
    shot_changes = shot_changes or []
    subtitle_segments = subtitle_segments or []

    # Shot boundaries: latest change at/before the start, earliest
    # change at/after the end, each within 8 s.
    before = [t for t in shot_changes if t <= start]
    if before and start - before[-1] <= 8:
        new_start = int(before[-1])
    after = [t for t in shot_changes if t >= end]
    if after and after[0] - end <= 8:
        new_end = int(round(after[0]))

    # Dialogue: a line overlapping an edge moves the edge to the
    # line's start/end, so no line is cut in half.
    for seg in subtitle_segments:
        if seg["start"] <= new_start < seg["end"]:
            if new_start - seg["start"] <= 5:
                new_start = int(seg["start"])
        if seg["start"] < new_end <= seg["end"]:
            if seg["end"] - new_end <= 5:
                new_end = int(round(seg["end"]))

    new_start = max(0, new_start)
    new_end = min(int(duration), new_end)
    # Keep the length near the intended clip length; if the shifts
    # stretched it too far, fall back to the centred window.
    if not (clip_length - 5 <= new_end - new_start <= clip_length + 10):
        log(f"Story window @{peak}s kept centred "
            f"(shot/dialogue shift would make it "
            f"{new_end - new_start}s vs {clip_length}s).")
        return start, end
    if (new_start, new_end) != (start, end):
        log(f"Story window @{peak}s: {start}-{end}s -> "
            f"{new_start}-{new_end}s (shot/dialogue aligned).")
    return new_start, new_end


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
                 shot_changes: list[float] | None = None,
                 face_samples: list[dict] | None = None,
                 subtitle_segments: list[dict] | None = None,
                 motion_energies: list[float] | None = None,
                 log=print) -> list[dict]:
    """Rank moments by audio + visual + motion signals. Returns moment dicts.

    Combined score per second = 0.10*audio + 0.05*face + 0.05*shot
    + 0.10*motion + 0.05*transient + 0.05*quiet-face + 0.20*dialogue
    + 0.15*contrast + 0.10*arc + 0.05*reaction + 0.10*intimate.
    It thinks like a
    human watching, not a meter: what is SAID (dialogue) matters
    most, then surprise (quiet, then a burst) and build-up (a scene
    rising to a payoff). A quiet close-up (romance/kiss), a
    high-motion stretch (car drifting/action) or a sharp onset
    (gunshot) can still win. Selection is type-diverse: the best
    moment of each signal type (loud, motion, gunshot, romance,
    adult, face, shot, dialogue, surprise, arc, reaction) is taken
    first,
    so one chunk returns a mix of kinds instead of five loud peaks.
    Each moment's window is then expanded to a story-complete beat.
    """
    if not energies:
        raise RuntimeError("No audio energy data — cannot detect moments.")
    max_e = max(energies) or 1.0
    shot_changes = shot_changes or []
    face_samples = face_samples or []
    motion_energies = motion_energies or [0.0] * len(energies)
    subtitle_segments = subtitle_segments or []

    # Combined score per 1-second window, plus each window's type.
    combined: list[float] = []
    types: list[str] = []
    for i, e in enumerate(energies):
        audio = e / max_e
        face, _ = _face_score(float(i), face_samples)
        shot = _shot_score(float(i), shot_changes)
        motion = motion_energies[i] if i < len(motion_energies) else 0.0
        transient = _transient_score(i, energies, max_e)
        quiet_face = _quiet_face_score(float(i), audio, face_samples)
        dialogue = _dialogue_score(float(i), subtitle_segments)
        contrast = _contrast_score(i, energies, max_e)
        arc = _arc_score(i, energies, max_e)
        reaction = _reaction_score(float(i), face_samples)
        intimate = _intimate_score(float(i), audio, face_samples)
        combined.append(W_AUDIO * audio + W_FACE * face + W_SHOT * shot
                        + W_MOTION * motion + W_TRANSIENT * transient
                        + W_QUIET_FACE * quiet_face
                        + W_DIALOGUE * dialogue + W_CONTRAST * contrast
                        + W_ARC * arc + W_REACTION * reaction
                        + W_INTIMATE * intimate)
        # Dominant type for diversity: the strongest WEIGHTED
        # contribution wins, so the type is what actually drove the
        # score -- dialogue, surprise, arc, reaction included.
        parts = [("loud", W_AUDIO * audio), ("face", W_FACE * face),
                 ("shot", W_SHOT * shot), ("motion", W_MOTION * motion),
                 ("gunshot", W_TRANSIENT * transient),
                 ("romance", W_QUIET_FACE * quiet_face),
                 ("dialogue", W_DIALOGUE * dialogue),
                 ("surprise", W_CONTRAST * contrast),
                 ("arc", W_ARC * arc),
                 ("reaction", W_REACTION * reaction),
                 ("adult", W_INTIMATE * intimate)]
        types.append(max(parts, key=lambda p: p[1])[0])

    pct = SENSITIVITY.get(sensitivity, 80)
    threshold = _percentile(combined, pct)
    max_c = max(combined) or 1.0
    log(f"Combined scores: {len(combined)} windows, max={max_c:.3f}, "
        f"threshold p{pct}={threshold:.3f} "
        f"(audio + face + shot + motion + transient + quiet-face "
        f"+ dialogue + surprise + arc + reaction + intimate; "
        f"{len(face_samples)} face samples, "
        f"{len(shot_changes)} shot changes, "
        f"{len(subtitle_segments)} subtitle lines)")

    # Candidate peaks: local maxima above threshold.
    candidates: list[tuple[int, float]] = []
    for i, c in enumerate(combined):
        if c < threshold:
            continue
        neighbours = combined[max(0, i - 2):i + 3]
        if c >= max(neighbours):
            candidates.append((i, c))
    if not candidates:  # very flat scores: fall back to global maxima
        top_idx = sorted(range(len(combined)), key=lambda i: combined[i], reverse=True)
        candidates = [(i, combined[i]) for i in top_idx[: count * 3]]
        log("Combined scores are very flat — using global maxima as moments.")

    # Type-diverse selection: take the best peak of each signal type
    # first (loud, motion, gunshot, romance, adult, face, shot),
    # then fill
    # the remaining slots with the highest scorers. This is what makes
    # one chunk return a mix of kinds instead of five loud peaks.
    # min_sep scales with the clip length: two peaks closer than a
    # third of a clip are one beat, not two.
    min_sep = max(MERGE_SECONDS, clip_length // 3)
    ranked = sorted(candidates, key=lambda p: p[1], reverse=True)
    selected: list[tuple[int, float]] = []
    # Pass 1: best peak of each type, in type order by best score.
    type_best: dict[str, tuple[int, float]] = {}
    for peak, c in ranked:
        t = types[peak] if peak < len(types) else "loud"
        if t not in type_best:
            type_best[t] = (peak, c)
    for t, (peak, c) in sorted(type_best.items(),
                               key=lambda kv: kv[1][1], reverse=True):
        if all(abs(peak - p) >= min_sep for p, _ in selected):
            selected.append((peak, c))
        if len(selected) >= count:
            break
    # Pass 2: fill the rest with the highest scorers.
    if len(selected) < count * 3:
        for peak, c in ranked:
            if all(abs(peak - p) >= min_sep for p, _ in selected):
                selected.append((peak, c))
            if len(selected) >= count * 3:
                break
    if not selected:
        selected = ranked[:count]
    merged = selected

    subtitle_hits = subtitle_hits or {}
    subtitle_segments = subtitle_segments or []
    scored: list[dict] = []
    for peak, c in merged:
        score = round(100.0 * c / max_c, 1)
        start = int(max(0, min(peak - clip_length // 2,
                               max(0, duration - clip_length))))
        end = start + clip_length
        # What drove this moment — the panel shows it as the reason.
        signals: list[str] = []
        audio = energies[peak] / max_e
        if audio >= 0.7:
            signals.append("loud peak")
        face, face_label = _face_score(float(peak), face_samples)
        if face > 0:
            signals.append(face_label)
        if _shot_score(float(peak), shot_changes):
            signals.append("new shot")
        motion = motion_energies[peak] if peak < len(motion_energies) else 0.0
        if motion >= 0.7:
            signals.append("high motion (car/action)")
        if _transient_score(peak, energies, max_e) >= 0.5:
            signals.append("sharp onset (gunshot/impact)")
        if _quiet_face_score(float(peak), audio, face_samples) > 0:
            signals.append("quiet close-up (romance/kiss)")
        if _intimate_score(float(peak), audio, face_samples) > 0:
            signals.append("two faces close (romance/adult)")
        if _dialogue_score(float(peak), subtitle_segments) >= 0.5:
            signals.append("dialogue (people talking)")
        if _contrast_score(peak, energies, max_e) >= 0.5:
            signals.append("surprise (quiet then burst)")
        if _arc_score(peak, energies, max_e) >= 0.5:
            signals.append("build-up (rising to a payoff)")
        if _reaction_score(float(peak), face_samples) > 0:
            signals.append("reaction (face appears)")
        moment_type = types[peak] if peak < len(types) else "loud"
        # Keyword boost: any hype word spoken anywhere inside the clip
        # window (not just at the exact peak second) counts.
        kws: list[str] = []
        for sec in range(start, end):
            kws.extend(subtitle_hits.get(sec, []))
        kws = sorted(set(kws))
        if kws:
            score = min(100.0, score + 10.0)
            signals.append("keyword: " + ", ".join(kws))
        # Story-complete window: shot boundaries + full dialogue.
        start, end = expand_to_story(
            start, end, peak, clip_length, duration,
            shot_changes=shot_changes, subtitle_segments=subtitle_segments,
            log=log)
        # User rule: if the moment is still going at the window end
        # (energy stays high just past the end), the clip cuts off
        # the main point — extend by 30 s so it completes.
        if end < int(duration) and end < len(energies):
            after = energies[end:min(len(energies), end + 5)]
            if after and max(after) >= 0.5 * max_e:
                new_end = min(int(duration), end + 30,
                              start + clip_length + 30)
                if new_end > end:
                    log(f"Story window @{peak}s: extended {end}-{new_end}s "
                        f"(action still hot past the end).")
                    end = new_end
        scored.append({
            "peak_time": peak,
            "start": start,
            "end": end,
            "length": end - start,
            "score": score,
            "energy": round(energies[peak], 4),
            "combined": round(c, 3),
            "signals": signals,
            "keywords": kws,
            "moment_type": moment_type,
        })
    # Final selection is type-diverse too: best of each type first,
    # then the highest scorers, so the returned moments are a mix of
    # kinds (loud, motion, gunshot, romance, face, shot, dialogue,
    # surprise, arc, reaction) rather than the top-N by score alone,
    # which would be all loud peaks.
    scored.sort(key=lambda m: m["score"], reverse=True)
    top: list[dict] = []
    seen_types: set[str] = set()
    for m in scored:
        if m.get("moment_type") not in seen_types:
            top.append(m)
            seen_types.add(m.get("moment_type"))
        if len(top) >= count:
            break
    if len(top) < count:
        for m in scored:
            if m not in top:
                top.append(m)
            if len(top) >= count:
                break
    # Present in timeline order for the panel, keep rank in the record.
    for rank, m in enumerate(top, 1):
        m["rank"] = rank
    top.sort(key=lambda m: m["start"])
    log(f"Top {len(top)} moments: " +
        ", ".join(f"#{m['rank']} @{m['start']}s score={m['score']} "
                  f"({', '.join(m['signals']) or 'audio'})" for m in top))
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
    if moment.get("signals"):
        reasons.append("signals: " + ", ".join(moment["signals"]))
    elif moment.get("keywords"):
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
