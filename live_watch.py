#!/usr/bin/env python3
"""Bridge: watch the running rerun_1h job and write ~/YT/live_status.json
so the v1.2 panel shows LIVE + download progress on every refresh.
Reads only: .part size, rerun log tail, DONE/FAILED markers. Never touches the job."""
import json, os, time
BASE = "/home/b1-noxx/YT"
STATUS = os.path.join(BASE, "live_status.json")
LOG = os.path.join(BASE, "logs", "rerun_1h.log")
PART = os.path.join(BASE, "downloads", "GpIWRLQBTvU_1h.mp4.part")
DONE = os.path.join(BASE, "rerun_1h_DONE.txt")
FAILED = os.path.join(BASE, "rerun_1h_FAILED.txt")

def write(d):
    d["updated_at"] = time.time()
    tmp = STATUS + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f)
    os.replace(tmp, STATUS)

prev_bytes, prev_t = 0, time.time()
while True:
    now = time.time()
    if os.path.exists(DONE):
        write({"status": "done", "stage": "done", "progress": 100,
               "message": "Done — clips uploaded to Drive 07-cuts-sequence.",
               "url": "https://www.youtube.com/watch?v=GpIWRLQBTvU",
               "download": None, "live_capture": None})
        break
    if os.path.exists(FAILED):
        write({"status": "error", "stage": "error", "progress": 0,
               "message": "Rerun failed — see ~/YT/logs/rerun_1h.log",
               "url": "https://www.youtube.com/watch?v=GpIWRLQBTvU",
               "download": None, "live_capture": None})
        break
    tail = ""
    try:
        with open(LOG, encoding="utf-8", errors="ignore") as f:
            lines = f.read().splitlines()
        tail = lines[-1] if lines else ""
    except Exception:
        pass
    part_bytes = os.path.getsize(PART) if os.path.exists(PART) else 0
    dt = max(1.0, now - prev_t)
    speed = (part_bytes - prev_bytes) / dt if part_bytes >= prev_bytes else 0
    prev_bytes, prev_t = part_bytes, now
    spd = f"{speed/1e6:.1f} MB/s" if speed > 0 else ""
    if "Uploaded" in tail or "Uploading" in tail:
        stage, msg, prog = "upload", "Uploading clips to Drive in sequence…", 90
        dl = None
    elif "Clip " in tail and "moment" in tail.lower() or tail.startswith("[") and "Clip " in tail:
        stage, msg, prog = "clipping", "Cutting vertical Shorts at max quality…", 70
        dl = None
    elif "Top " in tail and "moments" in tail:
        stage, msg, prog = "moments", "Moments found — cutting clips…", 60
        dl = None
    elif "Audio extracted" in tail or "Energy:" in tail:
        stage, msg, prog = "audio", "Audio extracted — finding the loudest moments…", 45
        dl = None
    else:
        stage, msg, prog = "download", "Downloading first hour at max quality…", 15
        dl = {"downloaded_bytes": part_bytes, "total_bytes": None,
              "percent": None, "speed": spd}
    write({"status": "running", "stage": stage, "progress": prog,
           "message": f"{msg} ({part_bytes/1e6:.0f} MB so far)" if dl else msg,
           "url": "https://www.youtube.com/watch?v=GpIWRLQBTvU",
           "download": dl, "live_capture": None})
    time.sleep(5)
