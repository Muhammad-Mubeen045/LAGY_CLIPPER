"""Face-centred framing for vertical cuts (v1.3).

A 9:16 vertical crop only keeps the middle third of a 16:9 frame's
width, so a face standing left or right of centre gets cut off. This
module finds where the main face actually is across a segment and
returns an anchor — the horizontal centre of the face's travel, as a
fraction of the frame width — so the crop window can be centred on
the face instead of the frame centre.

Detector: OpenCV YuNet (FaceDetectorYN), a small DNN that handles
profile views, dark scenes and small faces far better than a Haar
cascade (Haar found faces in only 4 of 24 test frames on GTA 6
footage; YuNet found them in 14). The model file
(face_detection_yunet_2023mar.onnx, ~233 KB, from the OpenCV Zoo) is
bundled in engine/models/.

OpenCV is an OPTIONAL dependency: if it is not installed, the model
file is missing, or no face is found, the functions return None and
the caller falls back to the centre crop. Nothing here is required
for the tool to run.
"""
from __future__ import annotations

import os
import subprocess
import tempfile

# How often to sample a frame inside the segment (seconds).
SAMPLE_EVERY_S = 2.0
# Working width for detection (small = fast, still enough for YuNet).
DETECT_WIDTH = 640
# YuNet score threshold: 0.5 keeps real faces in dark/profile shots
# that a strict 0.9 would drop (tested on GTA 6 footage).
YUNET_SCORE_THRESHOLD = 0.5
YUNET_NMS_THRESHOLD = 0.3

_MODEL_NAME = "face_detection_yunet_2023mar.onnx"


def model_path() -> str | None:
    """Bundled YuNet model, or None when it is not present."""
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "models", _MODEL_NAME)
    if os.path.exists(here):
        return here
    return None


def _cv2():
    try:
        import cv2  # noqa: PLC0415  (optional dependency)
        return cv2
    except Exception:
        return None


def _grab_frame(video_path: str, timestamp: float, out_jpg: str,
                width: int = DETECT_WIDTH) -> bool:
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", str(timestamp),
           "-i", video_path, "-frames:v", "1",
           "-vf", f"scale={width}:-1", out_jpg]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    return res.returncode == 0 and os.path.exists(out_jpg)


def detect_face_centre(video_path: str, start: float, length: float,
                       log=print) -> dict | None:
    """Sample frames across [start, start+length) and find the main face.

    Per frame, the face closest to the frame centre wins (the main
    subject — not a foreground passer-by at the edge, and not the
    largest face, which can be someone at the extreme edge). The
    anchor is the midpoint of the face's travel (min+max)/2, so the
    crop covers the whole movement instead of one instant.

    Returns {"face_x": anchor (0..1), "face_y": median face height
    (0..1), "median": median face_x, "confidence": fraction of
    sampled frames with a face, "movement": max-min face_x, "frames":
    sampled count, "faces_found": frames with a face} — or None when
    OpenCV/model is missing or no face was found in any frame.
    """
    cv2 = _cv2()
    if cv2 is None:
        log("Face framing unavailable: OpenCV (opencv-python-headless "
            "4.10) is not installed — falling back to centre crop.")
        return None
    if not hasattr(cv2, "FaceDetectorYN_create"):
        log("Face framing unavailable: this OpenCV build has no "
            "FaceDetectorYN (need opencv-python-headless 4.10) — "
            "falling back to centre crop.")
        return None
    model = model_path()
    if model is None:
        log("Face framing unavailable: YuNet model "
            f"{_MODEL_NAME} not found in engine/models/ — "
            "falling back to centre crop.")
        return None

    n_samples = max(1, int(length // SAMPLE_EVERY_S))
    centres: list[float] = []
    centres_y: list[float] = []
    widths: list[float] = []
    with tempfile.TemporaryDirectory(prefix="face_frame_") as tmp:
        for i in range(n_samples):
            t = start + 1.0 + i * SAMPLE_EVERY_S
            if t >= start + length:
                break
            jpg = os.path.join(tmp, f"f{i}.jpg")
            if not _grab_frame(video_path, t, jpg):
                continue
            img = cv2.imread(jpg)
            if img is None:
                continue
            h, w = img.shape[:2]
            detector = cv2.FaceDetectorYN_create(
                model, "", (w, h),
                YUNET_SCORE_THRESHOLD, YUNET_NMS_THRESHOLD, 5000)
            _, faces = detector.detect(img)
            if faces is None or len(faces) == 0:
                continue
            # Face closest to the frame centre = main subject.
            best = min(faces, key=lambda f: abs((f[0] + f[2] / 2.0) / w - 0.5))
            # float() — OpenCV returns numpy float32, which json can't
            # serialise (and which would leak numpy types into records).
            centres.append(float((best[0] + best[2] / 2.0) / w))
            centres_y.append(float((best[1] + best[3] / 2.0) / h))
            widths.append(float(best[2] / w))
    if not centres:
        log(f"No face found in {n_samples} sampled frames "
            f"@{start}s — falling back to centre crop.")
        return None
    centres_sorted = sorted(centres)
    median = centres_sorted[len(centres_sorted) // 2]
    median_y = sorted(centres_y)[len(centres_y) // 2]
    anchor = (min(centres) + max(centres)) / 2.0
    median_w = sorted(widths)[len(widths) // 2] if widths else 0.0
    result = {
        "face_x": round(anchor, 3),
        "face_y": round(median_y, 3),
        "face_w": round(median_w, 3),
        "median": round(median, 3),
        "confidence": round(len(centres) / n_samples, 2),
        "movement": round(max(centres) - min(centres), 3),
        "frames": n_samples,
        "faces_found": len(centres),
    }
    log(f"Face found @{start}s: anchor x={result['face_x']} "
        f"y={result['face_y']} (median {result['median']}, "
        f"confidence {result['confidence']}, "
        f"movement {result['movement']}, "
        f"{result['faces_found']}/{result['frames']} frames).")
    return result


def face_nudge_x(face_x: float, scaled_w: int, tw: int) -> int:
    """Nudge (in scaled pixels) that centres the crop on a face.

    face_x is the face anchor as a 0..1 fraction of the source width.
    scaled_w is the frame width after the increase-scale, tw the crop
    width. The caller clamps the result into range via _crop_window.
    """
    centre_x = (scaled_w - tw) // 2
    desired_x = int(face_x * scaled_w - tw / 2.0)
    return desired_x - centre_x


def face_nudge_y(face_y: float, scaled_h: int, th: int) -> int:
    """Nudge (in scaled pixels) that centres the crop on a face vertically.

    Only matters when the scaled frame is taller than the crop
    (vertical slack). For a 16:9 source the scaled height equals the
    crop height, so this is 0 after clamping in _crop_window.
    """
    centre_y = (scaled_h - th) // 2
    desired_y = int(face_y * scaled_h - th / 2.0)
    return desired_y - centre_y


# Motion fallback: small working size, and the gates that decide
# whether the motion is worth following.
MOTION_WIDTH = 160
MOTION_HEIGHT = 90
MOTION_MIN_PEAK_RATIO = 1.5  # peak column must beat the mean motion by this much
MOTION_CUT_THRESHOLD = 40.0  # mean frame diff above this = a scene cut, skip the pair


def detect_motion_centre(video_path: str, start: float, length: float,
                         log=print) -> dict | None:
    """Find where the action is when no face is found.

    Samples small grayscale frames across the segment, measures the
    frame-to-frame change per column, and returns the x of the
    strongest motion region. Scene cuts (a huge uniform diff) are
    skipped, and uniform motion (a camera pan, or a static scene)
    returns None — in those cases the centre crop is as good as
    anything. Needs numpy; returns None without it.

    Returns {"motion_x": 0..1, "confidence": 0..1, "frames": sampled
    count, "pairs": usable frame pairs} — or None.
    """
    try:
        import numpy as np  # noqa: PLC0415  (optional dependency)
    except Exception:
        return None
    cmd = ["ffmpeg", "-v", "error", "-ss", str(start), "-i", video_path,
           "-t", str(length),
           "-vf", f"fps=1/{SAMPLE_EVERY_S},scale={MOTION_WIDTH}:{MOTION_HEIGHT},format=gray",
           "-f", "rawvideo", "-pix_fmt", "gray", "-"]
    res = subprocess.run(cmd, capture_output=True, timeout=300)
    if res.returncode != 0 or not res.stdout:
        return None
    frame_bytes = MOTION_WIDTH * MOTION_HEIGHT
    n_frames = len(res.stdout) // frame_bytes
    if n_frames < 3:
        return None
    frames = np.frombuffer(res.stdout[:n_frames * frame_bytes],
                           dtype=np.uint8).reshape(
        n_frames, MOTION_HEIGHT, MOTION_WIDTH).astype(np.float32)
    profile = np.zeros(MOTION_WIDTH, dtype=np.float64)
    pairs = 0
    for i in range(n_frames - 1):
        diff = np.abs(frames[i + 1] - frames[i])
        if float(diff.mean()) > MOTION_CUT_THRESHOLD:
            continue  # scene cut, not motion
        profile += diff.mean(axis=0)
        pairs += 1
    if pairs < 2:
        return None
    profile /= pairs
    # Smooth over ~10% of the width: find a region, not one pixel.
    kernel = max(3, MOTION_WIDTH // 16)
    smooth = np.convolve(profile, np.ones(kernel) / kernel, mode="same")
    peak = int(np.argmax(smooth))
    mean_motion = float(smooth.mean())
    peak_motion = float(smooth[peak])
    if mean_motion <= 0 or peak_motion < MOTION_MIN_PEAK_RATIO * mean_motion:
        return None  # motion is uniform — centre is as good as anything
    motion_x = (peak + 0.5) / MOTION_WIDTH
    # Confidence grows with how much the peak beats the mean:
    # 1.5x -> 0.25, 2x -> 0.5, 3x and up -> 1.0.
    confidence = min(1.0, (peak_motion / mean_motion - 1.0) / 2.0)
    result = {"motion_x": round(float(motion_x), 3),
              "confidence": round(float(confidence), 2),
              "frames": n_frames, "pairs": pairs}
    log(f"Motion found @{start}s: anchor x={result['motion_x']} "
        f"(confidence {result['confidence']}, {pairs} frame pairs).")
    return result


def frame_anchor(video_path: str, start: float, length: float,
                 log=print) -> dict:
    """Pick the crop anchor for a segment: face first, then motion.

    The frame is set by what is actually in the shot, not by the
    frame centre: the main face when one is found, otherwise the
    strongest motion (the action), otherwise the centre.

    Returns {"anchor_x": 0..1, "anchor_y": 0..1,
    "source": "face" | "motion" | "centre", "confidence": 0..1,
    "detail": the face/motion result, or None for centre}.
    """
    face = detect_face_centre(video_path, start, length, log=log)
    if face:
        return {"anchor_x": face["face_x"],
                "anchor_y": face.get("face_y", 0.5),
                "source": "face", "confidence": face["confidence"],
                "detail": face}
    motion = detect_motion_centre(video_path, start, length, log=log)
    if motion:
        return {"anchor_x": motion["motion_x"], "anchor_y": 0.5,
                "source": "motion", "confidence": motion["confidence"],
                "detail": motion}
    log(f"No face or usable motion @{start}s — centre crop.")
    return {"anchor_x": 0.5, "anchor_y": 0.5, "source": "centre",
            "confidence": 0.0, "detail": None}


def frame_anchor_noface(video_path: str, start: float, length: float,
                        log=print) -> dict:
    """Pick the crop side that keeps the streamer's face OUT of frame.

    For a gameplay clip where the facecam must not appear: detect the
    face, then crop the OPPOSITE side of the frame. A 16:9 source's
    vertical crop covers 9/16 (0.5625) of the width, so with a safety
    margin:
    - face's left edge past 0.60 -> crop the LEFT side (face excluded)
    - face's right edge before 0.40 -> crop the RIGHT side (face excluded)
    - face straddling the centre -> neither side fully excludes it;
      fall back to motion (the action usually sits away from the
      facecam) and flag it, so the reviewer can check that frame.
    - no face found at all -> motion, then centre (nothing to exclude).

    Returns {"side": "left" | "right" | None, "anchor_x": 0..1,
    "anchor_y": 0..1, "source": "noface" | "motion" | "centre",
    "confidence": 0..1, "face_maybe_visible": bool, "detail": ...}.
    "side" is the crop side to use; None means centre crop.
    """
    face = detect_face_centre(video_path, start, length, log=log)
    if face:
        fx = face["face_x"]
        fw = face.get("face_w", 0.15) or 0.15
        half = fw / 2.0
        left_edge = fx - half
        right_edge = fx + half
        if left_edge > 0.60:
            log(f"No-face framing @{start}s: face at x={fx} "
                f"(left edge {left_edge:.2f}) — cropping LEFT side, "
                f"face stays out of frame.")
            return {"side": "left", "anchor_x": 0.0, "anchor_y": 0.5,
                    "source": "noface", "confidence": face["confidence"],
                    "face_maybe_visible": False, "detail": face}
        if right_edge < 0.40:
            log(f"No-face framing @{start}s: face at x={fx} "
                f"(right edge {right_edge:.2f}) — cropping RIGHT side, "
                f"face stays out of frame.")
            return {"side": "right", "anchor_x": 1.0, "anchor_y": 0.5,
                    "source": "noface", "confidence": face["confidence"],
                    "face_maybe_visible": False, "detail": face}
        log(f"No-face framing @{start}s: face at x={fx} straddles the "
            f"centre (edges {left_edge:.2f}–{right_edge:.2f}) — no side "
            f"fully excludes it. Picking the side with less face "
            f"overlap; check this clip's frame before approving.")
        # Overlap of the face with each crop side (16:9 vertical crop
        # covers 0.5625 of the width). Pick the side that shows less
        # of the face.
        left_overlap = max(0.0, min(right_edge, 0.5625) - max(left_edge, 0.0))
        right_overlap = max(0.0, min(right_edge, 1.0) - max(left_edge, 0.4375))
        side = "left" if left_overlap <= right_overlap else "right"
        return {"side": side, "anchor_x": 0.0 if side == "left" else 1.0,
                "anchor_y": 0.5, "source": "noface-partial",
                "confidence": face["confidence"],
                "face_maybe_visible": True, "detail": face}
    motion = detect_motion_centre(video_path, start, length, log=log)
    if motion:
        return {"side": None, "anchor_x": motion["motion_x"],
                "anchor_y": 0.5, "source": "motion",
                "confidence": motion["confidence"],
                "face_maybe_visible": False, "detail": motion}
    log(f"No face or usable motion @{start}s — centre crop.")
    return {"side": None, "anchor_x": 0.5, "anchor_y": 0.5,
            "source": "centre", "confidence": 0.0,
            "face_maybe_visible": False, "detail": None}


def sample_face_presence(video_path: str, duration: float,
                         every_s: float = 5.0, log=print) -> list[dict]:
    """Sample frames across the whole video and record face presence.

    One frame every `every_s` seconds (default 5 s). Each sample is
    {"t": seconds, "faces": count, "max_w": largest face width as a
    fraction of the frame width, "max_area": largest face area as a
    fraction of the frame}. A big sustained face (a close-up, a
    romantic or kiss scene) is exactly the quiet moment audio-only
    detection misses, so moment finding uses these samples as a
    visual signal. Returns [] when OpenCV/model is missing — the
    caller falls back to audio-only moments.
    """
    cv2 = _cv2()
    if cv2 is None or not hasattr(cv2, "FaceDetectorYN_create"):
        return []
    model = model_path()
    if model is None:
        return []
    samples: list[dict] = []
    n = max(1, int(duration // every_s))
    with tempfile.TemporaryDirectory(prefix="face_sample_") as tmp:
        for i in range(n):
            t = 1.0 + i * every_s
            if t >= duration:
                break
            jpg = os.path.join(tmp, f"s{i}.jpg")
            if not _grab_frame(video_path, t, jpg):
                continue
            img = cv2.imread(jpg)
            if img is None:
                continue
            h, w = img.shape[:2]
            detector = cv2.FaceDetectorYN_create(
                model, "", (w, h),
                YUNET_SCORE_THRESHOLD, YUNET_NMS_THRESHOLD, 5000)
            _, faces = detector.detect(img)
            if faces is None or len(faces) == 0:
                samples.append({"t": round(t, 1), "faces": 0,
                                "max_w": 0.0, "max_area": 0.0})
                continue
            widths = [float(f[2]) / w for f in faces]
            areas = [float(f[2] * f[3]) / (w * h) for f in faces]
            samples.append({"t": round(t, 1), "faces": len(faces),
                            "max_w": round(max(widths), 3),
                            "max_area": round(max(areas), 3)})
    found = sum(1 for s in samples if s["faces"] > 0)
    log(f"Face sampling: {found}/{len(samples)} sampled frames have a face.")
    return samples
