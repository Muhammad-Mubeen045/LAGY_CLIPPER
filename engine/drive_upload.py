"""Drive upload: copy finished clips into Google Drive with rclone.

Destination: gdrive:Live_Clipping/07-cuts-sequence/<stream-folder>/
Clips are uploaded IN SEQUENCE, one at a time, immediately after each
clip is cut — before the next clip is cut — so the laptop never has to
hold a whole job's worth of clips.

rclone is searched the same way Sweeper finds its tools: PATH first,
then ~/go/bin, ~/.local/bin, /usr/local/bin, /usr/bin. The [gdrive]
remote must already be configured in ~/.config/rclone/rclone.conf.

Never logs or prints the rclone token / config contents.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess

DRIVE_ROOT = "Live_Clipping/07-cuts-sequence"
REMOTE = "gdrive"


def find_rclone() -> str | None:
    """Return the rclone binary path, or None if it is not installed."""
    exe = shutil.which("rclone")
    if exe:
        return exe
    home = os.path.expanduser("~")
    for cand in (
        os.path.join(home, "go", "bin", "rclone"),
        os.path.join(home, ".local", "bin", "rclone"),
        "/usr/local/bin/rclone",
        "/usr/bin/rclone",
    ):
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None


def _run_rclone(rclone: str, args: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    # --log-level ERROR keeps the token out of output. NOTE: do NOT add
    # --no-ask-password — it is not a real rclone flag. rclone treats it as
    # a flag that takes a value, so it swallows the next argument: with
    # ["--no-ask-password", "--log-level", "ERROR", "copy", ...] the word
    # "ERROR" becomes the command ("unknown command \"ERROR\""), and every
    # upload fails. Found live 2026-11-17 on rclone v1.75.2 (clip 1 of the
    # Total Gaming job). Plain flags only, verified against real rclone.
    return subprocess.run(
        [rclone, "--log-level", "ERROR"] + args,
        capture_output=True, text=True, timeout=timeout,
    )


def stream_folder_name(date_str: str, channel: str, video_id: str) -> str:
    """YYYY-MM-DD_<Channel>_<video-id>, sanitised, same style as clip names."""
    keep = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in (channel or "channel"))
    keep = (keep.strip("_") or "channel")[:30]
    vid = re.sub(r"[^A-Za-z0-9_-]", "_", video_id or "video")[:20]
    return f"{date_str}_{keep}_{vid}"


def sequence_name(index: int, filename: str) -> str:
    """001_<existing-name>.mp4 — cut order, so local + Drive names match."""
    return f"{index:03d}_{filename}"


def remote_path(stream_folder: str, filename: str) -> str:
    return f"{REMOTE}:{DRIVE_ROOT}/{stream_folder}/{filename}"


def verify_in_drive(rclone: str, stream_folder: str, filename: str,
                    local_size: int, log=print) -> bool:
    """Check the file exists in Drive with a matching size (rclone ls)."""
    remote_dir = f"{REMOTE}:{DRIVE_ROOT}/{stream_folder}"
    try:
        res = _run_rclone(rclone, ["ls", remote_dir], timeout=120)
    except (subprocess.TimeoutExpired, OSError) as exc:
        log(f"Drive verify timed out / failed: {exc}")
        return False
    if res.returncode != 0:
        log(f"Drive verify failed: {(res.stderr or '').strip()[:200]}")
        return False
    for line in (res.stdout or "").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[1] == filename:
            try:
                if int(parts[0]) == int(local_size):
                    return True
                log(f"Drive size mismatch for {filename}: "
                    f"drive={parts[0]} local={local_size}")
                return False
            except ValueError:
                return False
    log(f"Drive verify: {filename} not listed in {remote_dir}")
    return False


def delete_from_drive(stream_folder: str, filename: str,
                       log=print) -> tuple[bool, str]:
    """Delete one clip from Drive (rclone delete of the exact path).

    Returns (ok, message). A delete in the panel must really drop the
    clip — leaving the Drive copy behind would mean "deleted" is a
    lie. Only the exact file in the exact stream folder is removed,
    never a folder. Returns False with a reason when rclone is missing
    or the delete fails, so the caller can warn the user that the
    Drive copy is still there.
    """
    rclone = find_rclone()
    if not rclone:
        return False, "rclone not found — Drive copy NOT deleted"
    if not stream_folder or not filename:
        return False, "no Drive folder/file recorded — nothing to delete"
    remote = remote_path(stream_folder, filename)
    try:
        res = _run_rclone(rclone, ["delete", remote], timeout=300)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return False, f"Drive delete timed out / failed: {exc}"
    if res.returncode != 0:
        return False, f"Drive delete failed: {(res.stderr or '').strip()[:200]}"
    log(f"Deleted from Drive: {remote}")
    return True, "Drive copy deleted"


def upload_clip(local_path: str, stream_folder: str, log=print,
                retries: int = 1) -> tuple[bool, str, str]:
    """Upload one clip and verify it in Drive.

    Returns (ok, drive_path, error). drive_path is the rclone remote
    path, e.g. gdrive:Live_Clipping/07-cuts-sequence/<folder>/<file>.
    On failure the clip stays local and is marked Pending — never lost.
    """
    filename = os.path.basename(local_path)
    dest = remote_path(stream_folder, filename)
    if not os.path.exists(local_path):
        return False, dest, f"Local clip not found: {filename}"
    rclone = find_rclone()
    if not rclone:
        return False, dest, (
            "rclone not found (checked PATH, ~/go/bin, ~/.local/bin, "
            "/usr/bin). Clip kept locally as Pending — install rclone "
            "and configure the [gdrive] remote, then use Retry."
        )
    local_size = os.path.getsize(local_path)
    last_err = ""
    for attempt in range(retries + 1):
        try:
            res = _run_rclone(rclone, ["copy", local_path, dest.rsplit("/", 1)[0] + "/"],
                              timeout=1200)
        except subprocess.TimeoutExpired:
            last_err = "rclone copy timed out"
            log(f"Drive upload timeout (attempt {attempt + 1}) for {filename}")
            continue
        except OSError as exc:
            last_err = str(exc)
            continue
        if res.returncode != 0:
            last_err = (res.stderr or "rclone copy failed").strip().splitlines()[-1][:300] \
                if (res.stderr or "").strip() else "rclone copy failed"
            log(f"Drive upload failed (attempt {attempt + 1}) for {filename}: {last_err}")
            continue
        if verify_in_drive(rclone, stream_folder, filename, local_size, log=log):
            log(f"Drive upload verified: {dest} ({local_size / 1e6:.1f} MB)")
            return True, dest, ""
        last_err = "uploaded but verification failed (size/name mismatch)"
        log(f"Drive upload not verified for {filename}: {last_err}")
    return False, dest, last_err


def drive_folder_link(stream_folder: str) -> str:
    """Web link to the sequence folder (parent id known from Drive)."""
    return ("https://drive.google.com/drive/folders/"
            "1MLzGI9qbzD2Ms8_305bByQgGfGe-If4b")


def upload_manifest(stream_folder: str, records: list, log=print) -> tuple[bool, str]:
    """Upload the per-stream manifest (clips + approvals) to Drive.

    The manifest is how Buzz reads approvals from Drive later: each
    record carries approved / approved_at plus its title, description,
    hashtags and Drive path. Nothing is published by this step — the
    actual YouTube upload happens outside the laptop app (Buzz via
    Postiz). Returns (ok, error).
    """
    import tempfile
    rclone = find_rclone()
    if not rclone:
        return False, "rclone not found — manifest kept locally only"
    name = f"{stream_folder}_manifest.json"
    tmp = os.path.join(tempfile.gettempdir(), name)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({
                "stream_folder": stream_folder,
                "drive_root": DRIVE_ROOT,
                "updated_at": __import__("datetime").datetime.now().isoformat(),
                "note": ("Review/approve manifest. approved=true means the "
                         "user approved this clip for upload. The laptop app "
                         "never publishes to YouTube itself."),
                "clips": records,
            }, f, indent=2, ensure_ascii=False)
        size = os.path.getsize(tmp)
        dest_dir = f"{REMOTE}:{DRIVE_ROOT}/{stream_folder}"
        res = _run_rclone(rclone, ["copy", tmp, dest_dir + "/"], timeout=300)
        if res.returncode != 0:
            return False, (res.stderr or "rclone copy failed").strip()[:200]
        if verify_in_drive(rclone, stream_folder, name, size, log=log):
            log(f"Drive manifest uploaded: {dest_dir}/{name}")
            return True, ""
        return False, "manifest uploaded but not verified in Drive"
    except OSError as exc:
        return False, str(exc)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
