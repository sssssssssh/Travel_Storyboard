"""Image quality metrics and perceptual hashing (no heavy dependencies)."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from ..ffmpeg import safe_image_open, safe_open


def _imread_gray(path: str | Path):
    """Read an image as grayscale without hitting cv2's path limits.

    ``cv2.imread`` uses the C file API: it returns ``None`` (no exception) for
    Windows paths that are long or non-ASCII. Reading bytes in Python and using
    ``cv2.imdecode`` avoids that silent failure (TSB-241).
    """
    try:
        with safe_open(path, "rb") as handle:
            raw = handle.read()
    except OSError:
        return None
    if not raw:
        return None
    buffer = np.frombuffer(raw, dtype=np.uint8)
    return cv2.imdecode(buffer, cv2.IMREAD_GRAYSCALE)


def analyze_frame(path: str | Path) -> dict:
    """Return technical quality metrics for a single image.

    - blur: variance of the Laplacian (higher = sharper).
    - brightness: mean luma (0-255).
    - overexposed_ratio / underexposed_ratio: fraction of clipped pixels.
    """
    img = _imread_gray(path)
    if img is None:
        return {
            "blur": 0.0,
            "brightness": 0.0,
            "overexposed_ratio": 0.0,
            "underexposed_ratio": 0.0,
        }
    blur = float(cv2.Laplacian(img, cv2.CV_64F).var())
    brightness = float(img.mean())
    over = float((img > 235).mean())
    under = float((img < 20).mean())
    return {
        "blur": round(blur, 2),
        "brightness": round(brightness, 2),
        "overexposed_ratio": round(over, 4),
        "underexposed_ratio": round(under, 4),
    }


def phash(path: str | Path, hash_size: int = 8) -> str:
    """Difference hash (dHash) as a binary string of length hash_size^2."""
    from PIL import Image

    img = safe_image_open(path).convert("L")
    try:
        resample = Image.Resampling.LANCZOS
    except AttributeError:  # pragma: no cover - older Pillow
        resample = Image.LANCZOS
    img = img.resize((hash_size + 1, hash_size), resample)
    arr = np.asarray(img, dtype=np.float32)
    diff = arr[:, 1:] > arr[:, :-1]
    return "".join("1" if bit else "0" for row in diff for bit in row)


def hamming(a: str, b: str) -> int:
    """Hamming distance between two equal-length binary hash strings."""
    if len(a) != len(b):
        return max(len(a), len(b))
    return sum(c1 != c2 for c1, c2 in zip(a, b))
