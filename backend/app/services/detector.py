"""Scene / shot boundary detection.

Strategy (doc §1.3 B1-B3):
1. Sample low-resolution frames at a low fps.
2. Compute HSV-histogram distance between consecutive frames.
3. Distances above a threshold mark hard cuts.
4. Segments longer than `max_segment_sec` are split at the strongest
   content-change peak, or by max-duration fallback.
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import cv2

from ..ffmpeg import (
    ERR_MEDIA_UNREADABLE,
    FFmpegError,
    locate_ffmpeg,
    long_path,
    run,
)

logger = logging.getLogger(__name__)


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
    cancel_check=None,
) -> list[tuple[float, Path]]:
    """Sample frames into *tmpdir*; raises when the extraction produced nothing.

    Both the -i input and the output pattern go through ``long_path`` so a deep
    Windows workspace behaves like a shallow one. The previous version ignored
    FFmpeg's exit code and returned an empty list, which downstream turned into
    a confident "single shot" result — a silent success on a failed extraction.

    ``cancel_check`` lets a cancelled detect job stop ffmpeg mid-sampling.
    """
    directory = Path(tmpdir)
    # The %06d pattern must stay in the *raw* path: FFmpeg expands it, and
    # prefixing is applied by long_path() without touching the pattern itself.
    pattern = str(directory / "f_%06d.jpg")
    cmd = [
        locate_ffmpeg(),
        "-y",
        "-i", long_path(source),
        "-vf", f"fps={sample_fps},scale={width}:-2",
        "-q:v", "5",
        long_path(pattern),
    ]
    run(cmd, expect_pattern=str(directory / "f_*.jpg"), cancel_check=cancel_check)
    files = sorted(directory.glob("f_*.jpg"))
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


def _read_image(path: Path):
    """Read an image via bytes + ``cv2.imdecode``.

    ``cv2.imread`` goes through the C file API and fails on Windows for long or
    non-ASCII paths (and returns ``None`` without an exception). Reading the
    bytes in Python and decoding in memory keeps behaviour identical on a deep
    Chinese-named workspace.

    Returns ``None`` only for a genuinely undecodable image.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        try:
            # Retry through the extended-length form for >MAX_PATH paths.
            with open(long_path(path), "rb") as handle:
                raw = handle.read()
        except OSError:
            return None
    if not raw:
        return None
    import numpy as np

    buffer = np.frombuffer(raw, dtype=np.uint8)
    return cv2.imdecode(buffer, cv2.IMREAD_COLOR)


def detect_segments(
    source: str | Path,
    duration_sec: float,
    sample_fps: float = 1.0,
    threshold: float = 0.35,
    max_segment_sec: float = 30.0,
    width: int = 160,
    cancel_check=None,
) -> DetectionResult:
    """Detect segment boundaries for a single video.

    ``cancel_check`` is optional (TSB-253): the one-shot detection used by
    callers without a job keeps the original signature and behaviour.
    """
    if duration_sec <= 0:
        return DetectionResult()

    with tempfile.TemporaryDirectory() as tmp:
        samples = _extract_sample_frames(source, sample_fps, width, tmp,
                                         cancel_check=cancel_check)
        times: list[float] = []
        hists: list = []
        unreadable = 0
        for t, p in samples:
            img = _read_image(p)
            if img is None:
                unreadable += 1
                continue
            hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
            hist = cv2.calcHist([hsv], [0, 1], None, [32, 32], [0, 180, 0, 256])
            cv2.normalize(hist, hist, 0, 1, cv2.NORM_MINMAX)
            times.append(t)
            hists.append(hist.flatten())

        # A frame we cannot decode is an error, not a reason to invent a single
        # shot covering the whole clip. Report it so the caller fails visibly.
        if samples and unreadable == len(samples):
            raise FFmpegError(
                f"抽取的 {len(samples)} 张抽样帧全部无法解码（路径或编码问题）：{source}",
                code=ERR_MEDIA_UNREADABLE,
            )
        if unreadable:
            # Some frames readable: detection still works, but say so — a partial
            # read used to be completely invisible in the job record.
            logger.warning(
                "detector: %d/%d sampled frames could not be decoded for %s",
                unreadable,
                len(samples),
                source,
            )

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
