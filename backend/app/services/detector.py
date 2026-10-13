"""Scene / shot boundary detection.

Strategy (doc §1.3 B1-B3):
1. Sample low-resolution frames at a low fps.
2. Compute HSV-histogram distance between consecutive frames.
3. Distances above a threshold mark hard cuts.
4. Segments longer than `max_segment_sec` are split at the strongest
   content-change peak, or by max-duration fallback.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import cv2

from ..ffmpeg import locate_ffmpeg, run


@dataclass
class Boundary:
    start_sec: float
    end_sec: float
    reason: str  # hard_cut | content_change | max_duration | end | single


@dataclass
class DetectionResult:
    boundaries: list[Boundary] = field(default_factory=list)
    sample_times: list[float] = field(default_factory=list)
    # sample_diffs[i] = distance from frame i-1 to frame i (0 for i == 0).
    sample_diffs: list[float] = field(default_factory=list)


def _extract_sample_frames(
    source: str | Path,
    sample_fps: float,
    width: int,
    tmpdir: str,
) -> list[tuple[float, Path]]:
    pattern = str(Path(tmpdir) / "f_%06d.jpg")
    cmd = [
        locate_ffmpeg(),
        "-y",
        "-i", str(source),
        "-vf", f"fps={sample_fps},scale={width}:-2",
        "-q:v", "5",
        pattern,
    ]
    run(cmd)
    files = sorted(Path(tmpdir).glob("f_*.jpg"))
    return [(i / sample_fps, p) for i, p in enumerate(files)]


def _forced_splits(
    times: list[float],
    diffs: list[float],
    duration_sec: float,
    cut_times: set[float],
    max_sec: float,
) -> dict[float, str]:
    """Return extra split points (time -> reason) to enforce max segment length.

    Over-long gaps are split at the strongest content-change peak; when no
    meaningful peak exists, a duration fallback point is inserted.
    """
    forced: dict[float, str] = {}
    bounds = [0.0] + sorted(cut_times) + [duration_sec]

    while True:
        added: list[tuple[float, str]] = []
        for k in range(len(bounds) - 1):
            s, e = bounds[k], bounds[k + 1]
            if e - s <= max_sec:
                continue
            inside = [
                i for i in range(1, len(times)) if s + 3.0 < times[i] < e - 3.0
            ]
            if not inside:
                added.append((s + max_sec, "max_duration"))
                continue
            mean_d = sum(diffs[i] for i in inside) / len(inside)
            best_i = max(inside, key=lambda i: diffs[i])
            best_d = diffs[best_i]
            # A content-change peak must clearly stand out above the local
            # average motion, otherwise fall back to a duration split.
            if best_d > 0.05 and best_d >= mean_d * 1.3:
                added.append((times[best_i], "content_change"))
            else:
                added.append((s + max_sec, "max_duration"))

        if not added:
            break
        for t, reason in added:
            forced[t] = reason
        bounds = sorted(set(bounds) | {t for t, _ in added})

    return forced


def detect_segments(
    source: str | Path,
    duration_sec: float,
    sample_fps: float = 1.0,
    threshold: float = 0.35,
    max_segment_sec: float = 30.0,
    width: int = 160,
) -> DetectionResult:
    """Detect segment boundaries for a single video."""
    if duration_sec <= 0:
        return DetectionResult()

    with tempfile.TemporaryDirectory() as tmp:
        samples = _extract_sample_frames(source, sample_fps, width, tmp)
        times: list[float] = []
        hists: list = []
        for t, p in samples:
            img = cv2.imread(str(p))
            if img is None:
                continue
            hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
            hist = cv2.calcHist([hsv], [0, 1], None, [32, 32], [0, 180, 0, 256])
            cv2.normalize(hist, hist, 0, 1, cv2.NORM_MINMAX)
            times.append(t)
            hists.append(hist.flatten())

        if len(times) < 2:
            return DetectionResult(
                [Boundary(0.0, duration_sec, "single")], times, [0.0] * len(times)
            )

        diffs: list[float] = [0.0]
        for i in range(1, len(times)):
            d = cv2.compareHist(hists[i - 1], hists[i], cv2.HISTCMP_BHATTACHARYYA)
            diffs.append(float(d))

        cut_times = {times[i] for i in range(1, len(times)) if diffs[i] > threshold}
        forced = _forced_splits(times, diffs, duration_sec, cut_times, max_segment_sec)
        bounds = sorted(set([0.0]) | cut_times | set(forced.keys()) | {duration_sec})

        boundaries: list[Boundary] = []
        for k in range(len(bounds) - 1):
            s, e = bounds[k], bounds[k + 1]
            if e in cut_times:
                reason = "hard_cut"
            elif e >= duration_sec - 1e-6:
                reason = "end"
            else:
                reason = forced.get(e, "content_change")
            boundaries.append(Boundary(s, e, reason))

        return DetectionResult(boundaries, times, diffs)
