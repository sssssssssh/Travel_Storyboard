#!/usr/bin/env python3
"""AI review pass (round 1) over detected segments.

`direct_analyze` in the app needs a configured OpenAI-compatible *vision* model.
When none is configured this script does the same job with signals that can be
measured rather than guessed, so the review loop is exercised on real footage:

* **camera shake / motion** — median absolute difference between consecutive
  representative frames of the same segment (frame 0 vs 1 vs 2). A tripod-like
  or smoothly-graded shot has small deltas; a shake has large ones.
* **exposure risk** — the segment's own `overexposed_ratio` /
  `underexposed_ratio` from detection.
* **sharpness** — `blur_avg` (variance of Laplacian; higher = sharper).

Every emitted request is validated by the server on import, so an invented or
stale id is rejected rather than silently accepted (C6).

Usage::

    python tools/ai_review.py --base http://127.0.0.1:8737 --project <id> \
        --out /path/to/review_requests.json [--post]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

import cv2
import httpx
import numpy as np


def _read_gray(path: Path):
    """Decode via imdecode, not cv2.imread.

    AGENTS.md §5.1: cv2.imread does not handle long/extended paths; reading the
    bytes ourselves is the same workaround the product uses.
    """
    data = path.read_bytes()
    arr = np.frombuffer(data, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)


def frame_delta(a: Path, b: Path) -> float | None:
    """Mean absolute per-pixel difference, normalised to 0..1."""
    ia, ib = _read_gray(a), _read_gray(b)
    if ia is None or ib is None:
        return None
    if ia.shape != ib.shape:
        ib = cv2.resize(ib, (ia.shape[1], ia.shape[0]))
    return float(np.mean(cv2.absdiff(ia, ib)) / 255.0)


def analyse(segment: dict) -> dict | None:
    """Measure motion/exposure for one segment.

    The list endpoint returns only a single `thumbnail` (the full `frame_refs`
    live on the detail endpoint), so fetch the detail record to compare several
    frames of the same shot.
    """
    refs = [Path(p) for p in (segment.get("frame_refs") or [])]
    existing = [p for p in refs if p.exists()]
    if len(existing) < 2:
        return None

    deltas = []
    for i in range(len(existing) - 1):
        d = frame_delta(existing[i], existing[i + 1])
        if d is not None:
            deltas.append(d)
    if not deltas:
        return None

    q = segment.get("quality_metrics") or {}
    return {
        "segment_id": segment["id"],
        "asset_id": segment["asset_id"],
        "start_sec": segment["start_sec"],
        "end_sec": segment["end_sec"],
        "duration_sec": segment["duration_sec"],
        "motion_median": round(statistics.median(deltas), 4),
        "motion_max": round(max(deltas), 4),
        "blur_avg": q.get("blur_avg"),
        "overexposed_ratio": q.get("overexposed_ratio", 0.0),
        "underexposed_ratio": q.get("underexposed_ratio", 0.0),
        "frames_used": len(existing),
    }


def decide(a: dict) -> tuple[bool, str, str]:
    """Return (wants_review, priority, reason). Bands are deliberately broad."""
    motion = a["motion_median"]
    over = a["overexposed_ratio"] or 0.0
    under = a["underexposed_ratio"] or 0.0
    blur = a["blur_avg"] or 0.0

    reasons, priority = [], "medium"

    # High inter-frame delta on a representative sample is the signature of
    # shake or a fast whip — the editor must look before trusting the shot.
    if motion >= 0.20:
        reasons.append(f"帧间差异高({motion:.3f})，疑似抖动或快速甩镜，需目视确认")
        priority = "high"
    elif motion >= 0.12:
        reasons.append(f"帧间差异偏高({motion:.3f})，运动幅度需确认")

    if over >= 0.15:
        reasons.append(f"过曝比例 {over:.1%}，需确认高光是否丢失细节")
        priority = "high"
    if under >= 0.35:
        reasons.append(f"欠曝比例 {under:.1%}，需确认暗部可用性")

    if blur and blur < 120:
        reasons.append(f"清晰度偏低(blur_avg={blur:.0f})，需确认是否失焦")
        priority = "high"

    if not reasons:
        return False, "low", ""
    return True, priority, "；".join(reasons)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8737")
    ap.add_argument("--project", required=True)
    ap.add_argument("--token-file", default="data/token.txt")
    ap.add_argument("--out", default=None)
    ap.add_argument("--post", action="store_true", help="import the requests")
    ap.add_argument("--min-duration", type=float, default=2.0)
    ap.add_argument("--max-requests", type=int, default=12)
    args = ap.parse_args()

    token = Path(args.token_file).read_text(encoding="utf-8").strip()
    http = httpx.Client(base_url=args.base, headers={"X-Access-Token": token},
                        timeout=httpx.Timeout(60.0, read=300.0))

    segs = http.get(f"/api/projects/{args.project}/segments",
                    params={"limit": 500}).json()["items"]
    candidates = [s for s in segs if s["duration_sec"] >= args.min_duration]
    print(f"segments={len(segs)}  with dur>={args.min_duration}s: {len(candidates)}")

    analysed = []
    for i, s in enumerate(candidates):
        detail = http.get(f"/api/segments/{s['id']}").json()
        detail.setdefault("duration_sec", s["duration_sec"])
        a = analyse(detail)
        if a is not None:
            analysed.append(a)
        if (i + 1) % 15 == 0:
            print(f"  ...{i + 1}/{len(candidates)}", flush=True)
    print(f"analyseable (>=2 readable frames): {len(analysed)}")

    want, skip = [], 0
    for a in analysed:
        needs, priority, reason = decide(a)
        if needs:
            want.append((a, priority, reason))
        else:
            skip += 1

    print(f"flagged for review: {len(want)}   cleared: {skip}")
    want.sort(key=lambda t: -t[0]["motion_median"])
    want = want[:args.max_requests]

    requests = [{
        "asset_id": a["asset_id"],
        "segment_id": a["segment_id"],
        "start_sec": a["start_sec"],
        "end_sec": a["end_sec"],
        "priority": priority,
        "reason": reason,
    } for a, priority, reason in want]

    payload = {
        "schema_version": "2.0",
        "project_id": args.project,
        "requests": requests,
    }

    print("\n== review requests ==")
    for a, priority, reason in want:
        print(f"  {a['segment_id'][:8]} {a['start_sec']:7.2f}-{a['end_sec']:7.2f} "
              f"motion={a['motion_median']:.3f} [{priority}] {reason}")

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
        print(f"\nwritten: {args.out}")

    if args.post:
        r = http.post(f"/api/projects/{args.project}/review-requests/import", json=payload)
        print(f"\nimport -> HTTP {r.status_code}")
        print(json.dumps(r.json(), ensure_ascii=False, indent=2)[:1200])

    # Report the measured distribution so the record is auditable.
    if analysed:
        motions = sorted(a["motion_median"] for a in analysed)
        print(f"\nmotion median across {len(analysed)} segments: "
              f"min={motions[0]:.3f} p50={motions[len(motions)//2]:.3f} max={motions[-1]:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
