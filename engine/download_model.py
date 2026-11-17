"""Download the YuNet face-detection model for face-centred framing.

The model file (face_detection_yunet_2023mar.onnx, ~233 KB) is NOT
committed to the repo — it is a binary asset from the OpenCV Zoo, and
the GitHub snapshot push only carries text files. Run this once after
cloning:

    python3 engine/download_model.py

It fetches the model from the OpenCV Zoo (media.githubusercontent.com
serves the real binary, not the Git LFS pointer) into
engine/models/ and verifies the md5. Without the model, face-centred
framing falls back to the motion/centre crop — the tool still works.
"""
from __future__ import annotations

import hashlib
import os
import sys
import urllib.request

MODEL_NAME = "face_detection_yunet_2023mar.onnx"
MODEL_URL = ("https://media.githubusercontent.com/media/opencv/opencv_zoo/main/"
             "models/face_detection_yunet/" + MODEL_NAME)
EXPECTED_MD5 = "4ae92eeb150c82ce15ac80738b3b8167"
EXPECTED_BYTES = 232589


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.join(here, "models")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, MODEL_NAME)
    if os.path.exists(out_path):
        with open(out_path, "rb") as f:
            digest = hashlib.md5(f.read()).hexdigest()
        if digest == EXPECTED_MD5:
            print(f"Model already present and verified: {out_path}")
            return 0
        print(f"Existing model has wrong md5 ({digest}) — re-downloading.")
    print(f"Downloading {MODEL_URL} ...")
    try:
        urllib.request.urlretrieve(MODEL_URL, out_path)
    except Exception as exc:  # noqa: BLE001
        print(f"Download failed: {exc}", file=sys.stderr)
        return 1
    size = os.path.getsize(out_path)
    with open(out_path, "rb") as f:
        digest = hashlib.md5(f.read()).hexdigest()
    if size != EXPECTED_BYTES or digest != EXPECTED_MD5:
        print(f"Downloaded file failed verification (size {size}, "
              f"md5 {digest}) — deleting.", file=sys.stderr)
        os.remove(out_path)
        return 1
    print(f"Model verified ({size} bytes, md5 {digest}): {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
