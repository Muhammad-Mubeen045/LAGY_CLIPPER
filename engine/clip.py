"""Clip cutting: ffmpeg -> vertical Shorts, 30 fps, AAC, loudnorm.

v1.2 quality: the output resolution follows the SOURCE, never a 1080p
cap. Max quality: 4K source -> 2160x3840, 2K/1440p source -> 1440x2560,
otherwise 1080x1920. A lower source is upscaled with lanczos +
unsharp + light denoise and labelled honestly as upscaled — never
claimed as native 4K. CRF 18, AAC 192k.
"""
from __future__ import annotations

import json
import os
import subprocess

# (width, height) per quality target.
TARGETS = {"max": None, "4k": (2160, 3840), "2k": (1440, 2560), "1080p": (1080, 1920)}

# Last cut info for the caller (upscale labelling).
last_cut_info: dict = {}


def source_height(path: str) -> int:
    info = probe(path)
    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            return int(s.get("height") or 0)
    return 0


def source_dimensions(path: str) -> tuple[int, int]:
    """(width, height) of the source video stream."""
    info = probe(path)
    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            return int(s.get("width") or 0), int(s.get("height") or 0)
    return 0, 0


def target_size(src_h: int, quality: str) -> tuple[int, int]:
    """Output (w, h) for a source of height src_h and a quality setting."""
    if quality == "4k":
        return (2160, 3840)
    if quality == "2k":
        return (1440, 2560)
    if quality == "1080p":
        return (1080, 1920)
    # max: follow the source, never downscale a better source.
    if src_h >= 2160:
        return (2160, 3840)
    if src_h >= 1440:
        return (1440, 2560)
    return (1080, 1920)


def probe(path: str) -> dict:
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json",
           "-show_format", "-show_streams", path]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if res.returncode != 0:
        return {}
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return {}


def video_duration(path: str) -> float:
    info = probe(path)
    try:
        return float(info.get("format", {}).get("duration", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


# Framing anchors for the vertical crop. The crop is the main quality
# bar: gameplay action must stay centred, and a facecam in a corner
# must not be cut off. "gameplay" = centre crop. "facecam" anchors the
# crop to the corner the user picks, so that corner stays in frame.
# nudge_x / nudge_y shift the crop window in pixels (clamped in-range).
FACE_CAM_ANCHORS = {
    "top-left": ("left", "top"), "top-right": ("right", "top"),
    "bottom-left": ("left", "bottom"), "bottom-right": ("right", "bottom"),
}


def _crop_window(scaled_w: int, scaled_h: int, tw: int, th: int,
                 h_anchor: str, v_anchor: str,
                 nudge_x: int = 0, nudge_y: int = 0) -> tuple[int, int]:
    if h_anchor == "left":
        x = 0
    elif h_anchor == "right":
        x = scaled_w - tw
    else:
        x = (scaled_w - tw) // 2
    if v_anchor == "top":
        y = 0
    elif v_anchor == "bottom":
        y = scaled_h - th
    else:
        y = (scaled_h - th) // 2
    x = max(0, min(scaled_w - tw, x + nudge_x))
    y = max(0, min(scaled_h - th, y + nudge_y))
    return x, y


def cut_vertical(src: str, start: int, length: int, out_path: str, log=print,
                 quality: str = "max", focus: str = "gameplay",
                 facecam_corner: str = "top-right",
                 nudge_x: int = 0, nudge_y: int = 0) -> str:
    """Cut one vertical Short from `src` starting at `start` seconds.

    Output size follows the source (see target_size). If the source is
    lower than the target, it is upscaled with lanczos + unsharp +
    hqdn3d and the cut is labelled upscaled in last_cut_info — the
    clip record must carry that label, never a fake native claim.

    Framing: focus="gameplay" centres the crop on the action.
    focus="facecam" anchors the crop to `facecam_corner` so a facecam
    in that corner stays in frame. focus="face" (v1.3, the default)
    sets the frame by what is actually in the shot — the main face
    when one is found (OpenCV YuNet, see engine/face_framing.py),
    otherwise the strongest motion (the action), otherwise the
    centre. If nothing is found, or the detectors are unavailable,
    it falls back to the centre crop. nudge_x/nudge_y shift the crop
    window in pixels (the panel's nudge controls).
    """
    global last_cut_info
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    src_w, src_h = source_dimensions(src)
    tw, th = target_size(src_h, quality)
    # Vertical output: the source's WIDTH becomes the vertical HEIGHT
    # after the 90-degree crop logic (a 3840x2160 source fills a
    # 2160x3840 vertical frame natively). Upscale only when the source
    # width is smaller than the target vertical height — comparing
    # source height to target height would wrongly call native 4K
    # "upscaled from 2160p".
    upscaled = bool(src_w and src_w < th)
    # Scaled size after force_original_aspect_ratio=increase: the scale
    # factor is max(tw/src_w, th/src_h).
    factor = max(tw / max(1, src_w), th / max(1, src_h))
    scaled_w = int(src_w * factor + 0.5)
    scaled_h = int(src_h * factor + 0.5)
    framing = None
    noface_side = None
    if focus == "noface":
        # Keep the streamer's face OUT of the clip: crop the side of
        # the frame opposite the facecam. Falls back to the action
        # (motion) when the face straddles the centre, and to centre
        # when nothing is found. Any failure falls back to centre.
        try:
            from .face_framing import (face_nudge_x, face_nudge_y,
                                       frame_anchor_noface)
            framing = frame_anchor_noface(src, start, length, log=log)
            noface_side = framing.get("side")
            if noface_side in ("left", "right"):
                log(f"Framing (noface): face excluded — cropping "
                    f"{noface_side} side, confidence "
                    f"{framing['confidence']}.")
            elif framing["source"] == "motion":
                nudge_x = nudge_x + face_nudge_x(framing["anchor_x"], scaled_w, tw)
                log(f"Framing (noface → motion): anchor "
                    f"({framing['anchor_x']}, {framing['anchor_y']}) "
                    f"confidence {framing['confidence']} "
                    f"-> nudge=({nudge_x},{nudge_y}) — face may be "
                    f"partially visible, check the frame.")
            else:
                log("Framing (noface): no face/motion — centre crop.")
        except Exception as exc:  # noqa: BLE001
            log(f"No-face framing unavailable ({exc}) — centre crop.")
            framing = None
    elif focus == "face":
        # Smart framing: the crop follows the main face; when no face
        # is found it follows the strongest motion (the action); when
        # neither is usable it stays centred. Any failure falls back
        # to the centre crop — the cut still succeeds.
        try:
            from .face_framing import (face_nudge_x, face_nudge_y,
                                       frame_anchor)
            framing = frame_anchor(src, start, length, log=log)
            nudge_x = nudge_x + face_nudge_x(framing["anchor_x"], scaled_w, tw)
            nudge_y = nudge_y + face_nudge_y(framing["anchor_y"], scaled_h, th)
            log(f"Framing ({framing['source']}): anchor "
                f"({framing['anchor_x']}, {framing['anchor_y']}) "
                f"confidence {framing['confidence']} "
                f"-> nudge=({nudge_x},{nudge_y}).")
        except Exception as exc:  # noqa: BLE001
            log(f"Framing unavailable ({exc}) — centre crop.")
            framing = None
    if focus == "facecam":
        h_anchor, v_anchor = FACE_CAM_ANCHORS.get(facecam_corner, ("right", "top"))
    elif focus == "noface" and noface_side in ("left", "right"):
        h_anchor, v_anchor = noface_side, "center"
    else:
        h_anchor, v_anchor = "center", "center"
    cx, cy = _crop_window(scaled_w, scaled_h, tw, th,
                          h_anchor, v_anchor, nudge_x, nudge_y)
    crop = f"crop={tw}:{th}:{cx}:{cy}"
    if upscaled:
        vf = (f"scale={tw}:{th}:force_original_aspect_ratio=increase:"
              f"flags=lanczos,{crop},hqdn3d=1.5:1.5:6:6,"
              f"unsharp=5:5:0.8:5:5:0.0,setsar=1,fps=30")
        log(f"Upscaling source {src_w}x{src_h} ({src_h}p) -> {tw}x{th} "
            f"(lanczos + unsharp + denoise) — clip will be labelled "
            f"'upscaled from {src_h}p'.")
    else:
        vf = (f"scale={tw}:{th}:force_original_aspect_ratio=increase,"
              f"{crop},setsar=1,fps=30")
        log(f"Cutting {src_w}x{src_h} ({src_h}p) -> {tw}x{th} vertical, native, "
            f"focus={focus} anchor={h_anchor}/{v_anchor} crop=({cx},{cy}).")
    last_cut_info = {"source_height": src_h, "source_width": src_w,
                     "target_width": tw,
                     "target_height": th, "upscaled": bool(upscaled),
                     "upscaled_from": src_h if upscaled else None,
                     "focus": focus, "facecam_corner": facecam_corner,
                     "framing": framing,
                     "crop_anchor": f"{h_anchor}/{v_anchor}",
                     "nudge_x": nudge_x, "nudge_y": nudge_y,
                     "crop_x": cx, "crop_y": cy}
    # medium preset + CRF 18 keeps quality high on the laptop; 4K is
    # slower than the old veryfast/CRF23 path — that is the price of Max.
    common = ["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-i", src,
              "-t", str(length), "-vf", vf,
              "-c:v", "libx264", "-preset", "medium", "-crf", "18",
              "-c:a", "aac", "-b:a", "192k",
              "-movflags", "+faststart"]
    with_norm = common + ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11", out_path]
    res = subprocess.run(with_norm, capture_output=True, text=True, timeout=3600)
    if res.returncode != 0:
        log(f"loudnorm failed for {out_path}, retrying without it: {(res.stderr or '')[:200]}")
        res = subprocess.run(common + [out_path], capture_output=True, text=True, timeout=3600)
        if res.returncode != 0:
            raise RuntimeError(f"ffmpeg clip failed: {(res.stderr or '')[:300]}")
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError(f"ffmpeg produced no output for {out_path}")
    return out_path


def verify_clip(path: str, expected_length: int,
                target: tuple[int, int] | None = None) -> dict:
    """ffprobe check: expected vertical size, duration near expected, audio."""
    info = probe(path)
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    dur = float(info.get("format", {}).get("duration", 0) or 0)
    w, h = video.get("width"), video.get("height")
    if target:
        vertical_ok = (w, h) == target
    else:
        vertical_ok = (w, h) in ((1080, 1920), (1440, 2560), (2160, 3840))
    # size_ok is the orientation-neutral check (vertical SHORT or
    # horizontal VIDEO); vertical_ok is kept for backward compatibility.
    size_ok = (w, h) == target if target else vertical_ok
    return {
        "path": path,
        "size_mb": round(os.path.getsize(path) / 1e6, 2),
        "width": w,
        "height": h,
        "duration": round(dur, 2),
        "duration_ok": abs(dur - expected_length) <= 1.5,
        "vertical_ok": vertical_ok,
        "size_ok": size_ok,
        "audio_ok": bool(audio),
        "codec": video.get("codec_name"),
    }


def horizontal_target(src_h: int, quality: str) -> tuple[int, int]:
    """16:9 output size for a VIDEO cut — no crop, full frame kept."""
    if quality == "4k" or (quality == "max" and src_h >= 2160):
        return (3840, 2160)
    if quality == "2k" or (quality == "max" and src_h >= 1440):
        return (2560, 1440)
    return (1920, 1080)


def cut_horizontal(src: str, start: int, length: int, out_path: str, log=print,
                   quality: str = "max") -> str:
    """Cut a VIDEO segment: horizontal 16:9, NO crop — the full frame
    (gameplay + facecam) is kept. Length 2–10 min. CRF 18, AAC 192k."""
    global last_cut_info
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    src_w, src_h = source_dimensions(src)
    tw, th = horizontal_target(src_h, quality)
    upscaled = bool(src_h and src_h < th)
    vf = (f"scale={tw}:{th}:force_original_aspect_ratio=decrease,"
          f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30")
    if upscaled:
        vf = (f"scale={tw}:{th}:force_original_aspect_ratio=decrease:"
              f"flags=lanczos,pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2,"
              f"hqdn3d=1.5:1.5:6:6,unsharp=5:5:0.8:5:5:0.0,setsar=1,fps=30")
    last_cut_info = {"source_height": src_h, "source_width": src_w,
                     "target_width": tw, "target_height": th,
                     "upscaled": upscaled,
                     "upscaled_from": src_h if upscaled else None,
                     "focus": "full-frame", "facecam_corner": "",
                     "crop_anchor": "full-frame", "nudge_x": 0, "nudge_y": 0,
                     "crop_x": 0, "crop_y": 0}
    log(f"Cutting VIDEO {src_w}x{src_h} -> {tw}x{th} horizontal (no crop), "
        f"{length}s.")
    common = ["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-i", src,
              "-t", str(length), "-vf", vf,
              "-c:v", "libx264", "-preset", "medium", "-crf", "18",
              "-c:a", "aac", "-b:a", "192k",
              "-movflags", "+faststart"]
    with_norm = common + ["-af", "loudnorm=I=-14:TP=-1.5:LRA=11", out_path]
    res = subprocess.run(with_norm, capture_output=True, text=True, timeout=3600)
    if res.returncode != 0:
        log(f"loudnorm failed for {out_path}, retrying without it: {(res.stderr or '')[:200]}")
        res = subprocess.run(common + [out_path], capture_output=True, text=True, timeout=3600)
        if res.returncode != 0:
            raise RuntimeError(f"ffmpeg clip failed: {(res.stderr or '')[:300]}")
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError(f"ffmpeg produced no output for {out_path}")
    return out_path


def extract_preview(src: str, timestamp: int, out_jpg: str, log=print) -> str:
    """Extract one preview frame (JPEG) at `timestamp` seconds."""
    os.makedirs(os.path.dirname(out_jpg) or ".", exist_ok=True)
    cmd = ["ffmpeg", "-y", "-v", "error", "-ss", str(timestamp), "-i", src,
           "-frames:v", "1", "-q:v", "3", out_jpg]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if res.returncode != 0 or not os.path.exists(out_jpg):
        raise RuntimeError(f"Preview frame failed: {(res.stderr or '')[:200]}")
    return out_jpg
