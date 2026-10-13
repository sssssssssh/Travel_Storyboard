"""Image quality metrics and perceptual hashing (no heavy dependencies)."""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
from PIL import Image


def analyze_frame(path: str | Path) -> dict:
    """Return technical quality metrics for a single image.

    - blur: variance of the Laplacian (higher = sharper).
    - brightness: mean luma (0-255).
    - overexposed_ratio / underexposed_ratio: fraction of clipped pixels.
    """
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
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
    img = Image.open(path).convert("L")
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
