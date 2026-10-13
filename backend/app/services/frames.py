"""Representative-frame time selection.

Doc §1.3 B4: pick 1-3 frames per candidate segment, avoid the very first
frame (often black), prefer a mid segment frame and a content-change frame.
"""

from __future__ import annotations


def select_rep_times(
    start_sec: float,
    end_sec: float,
    sample_times: list[float] | None = None,
    sample_diffs: list[float] | None = None,
    max_frames: int = 3,
) -> list[float]:
    """Return representative frame timestamps (seconds) within [start, end]."""
    sample_times = sample_times or []
    sample_diffs = sample_diffs or []
    dur = end_sec - start_sec
    if dur <= 0:
        return [round(start_sec, 3)]

    if dur < 5.0:
        return [round(start_sec + dur / 2, 3)]

    n = 2 if dur < 15.0 else 3
    n = min(n, max_frames)

    candidates: list[float] = []
    # Anchor just after the start to dodge black/slate frames.
    candidates.append(start_sec + min(0.5, dur * 0.1))
    # Mid-segment representative frame.
    candidates.append(start_sec + dur * 0.45)

    # Content-change peak within the segment.
    best_t: float | None = None
    best_d = -1.0
    for i in range(1, len(sample_times)):
        t = sample_times[i]
        if start_sec <= t <= end_sec and sample_diffs[i] > best_d:
            best_d = sample_diffs[i]
            best_t = t
    if best_t is not None:
        candidates.append(best_t)

    # Clamp into a safe extraction window, dedupe, order, cap to n.
    safe_end = end_sec - min(0.25, dur * 0.05)
    clamped = sorted({round(min(max(c, start_sec), safe_end), 3) for c in candidates})
    return clamped[:n]
