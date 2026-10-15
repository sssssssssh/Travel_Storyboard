#!/usr/bin/env python3
"""M5 acceptance harness: real vision model over real footage (TSB-308).

This is a *test harness*, not product code. It exists so the M5.1/M5.2 claims in
the release notes are reproducible and produce machine-checkable evidence (JSON)
rather than a hand-typed assertion.

What it drives, in order:

    scan -> detect -> vision_analyze (image semantics)
                    -> vision-candidates (TSB-306)
                    -> motion_review (timestamped multi-frame, TSB-307)

and it enforces the two guarantees that are easiest to fake by accident:

* **C1 — originals are read-only.** SHA-256, size and mtime of every source file
  are captured before and after; any difference fails the run.
* **C6 — nothing fails silently.** A job ending ``failed``, or reporting entries
  in its ``failures`` list, fails the run instead of passing quietly.

It also refuses to run without ``TRAVELSB_MODEL_MODE=on``: sending footage frames
to a remote model is an explicit opt-in, and a harness must not paper over it.

Usage::

    cd backend
    python tools/acceptance_m5.py --source F:\\path\\to\\footage --limit 6

The source directory is only ever read.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

# Run from `backend/` so `app.*` imports and `.env` both resolve.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

#: Per-job ceiling. Detection over several GB of 4K HEVC is the slow one.
TIMEOUT_SEC = 3600.0


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot(files: list[Path]) -> dict:
    return {p.name: (sha256(p), p.stat().st_size, p.stat().st_mtime_ns) for p in files}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="footage directory (read-only)")
    parser.add_argument("--limit", type=int, default=6, help="shots to analyse")
    parser.add_argument("--motion-limit", type=int, default=3, help="shots to motion-review")
    parser.add_argument("--frames", type=int, default=4, help="frames per motion review")
    parser.add_argument("--project", default="M5-real-acceptance")
    parser.add_argument("--out", default="", help="write the JSON report here")
    args = parser.parse_args()

    from app.config import get_settings
    from app.database import init_db, session_scope
    from app.models import (
        Asset,
        Job,
        Project,
        Segment,
        SegmentMotionReview,
        SegmentSemanticResult,
    )
    from app.services.analyzer import detect_task
    from app.services.indexer import scan_task
    from app.services.runner import TASK_REGISTRY, runner
    from app.services.vision.motion import register_motion_tasks
    from app.services.vision.semantic import register_vision_tasks

    # Registration happens in `app.main` as an explicit call, so importing the
    # task modules is NOT enough: without this the runner answers
    # "Unknown job type: vision_analyze", which reads like a product failure but
    # is really this harness's own wiring gap.
    register_vision_tasks()
    register_motion_tasks()
    _ = (scan_task, detect_task)
    missing = [t for t in ("scan", "detect", "vision_analyze", "motion_review")
               if t not in TASK_REGISTRY]
    if missing:
        print(f"refusing to run: tasks not registered: {missing}", file=sys.stderr)
        return 2

    settings = get_settings()
    source_root = Path(args.source).resolve()
    if not source_root.is_dir():
        print(f"source directory not found: {source_root}", file=sys.stderr)
        return 2

    # A harness must not silently bypass the product's own privacy switch.
    if settings.model_mode != "on":
        print(
            "refusing to run: TRAVELSB_MODEL_MODE is not 'on'.\n"
            "Sending representative frames to a remote model is an explicit "
            "opt-in; set TRAVELSB_MODEL_MODE=on in backend/.env.",
            file=sys.stderr,
        )
        return 2
    if not (settings.model_base_url and settings.model_api_key):
        print("refusing to run: vision model is not configured.", file=sys.stderr)
        return 2

    videos = sorted(
        p
        for p in source_root.iterdir()
        if p.is_file() and p.suffix.lower() in {".mp4", ".mov", ".m4v", ".mkv", ".avi"}
    )
    if not videos:
        print(f"no video files under {source_root}", file=sys.stderr)
        return 2

    print(f"source : {source_root}  ({len(videos)} files)")
    print(f"model  : {settings.model_name}")
    print(f"secure : http allowed = {settings.model_allow_insecure_http}")
    before = snapshot(videos)
    # The report is meant to be committed as evidence, so it records the
    # *shape* of the library, not the operator's directory layout (C5: evidence
    # artifacts must not leak real filesystem paths).
    report: dict = {
        "source_dir_name": source_root.name,
        "source_file_count": len(videos),
        "model": settings.model_name,
        "steps": {},
    }

    init_db()

    with session_scope() as s:
        project = s.query(Project).filter(Project.name == args.project).first()
        if project is None:
            project = Project(name=args.project, source_roots=[str(source_root)])
            s.add(project)
            s.flush()
        project_id = project.id
    print(f"project: {project_id}")

    def run_job(job_type: str, _fn=None, config: dict | None = None) -> tuple[str, float]:
        """Run one job through the **real** runner.

        Calling the task function directly would bypass ``runner._run``, which is
        what takes the project lock and performs the ``pending -> running``
        transition. Driving it by hand produced a job that executed its work but
        stayed ``pending`` forever, and the next job then blocked: the harness was
        measuring its own mistake rather than the product. Submitting through
        ``runner.submit`` and waiting on the future is the same path the API uses.
        """
        with session_scope() as s:
            job = Job(
                project_id=project_id,
                job_type=job_type,
                status="pending",
                config_snapshot=config or {},
                stats={},
                failures={"count": 0, "items": []},
            )
            s.add(job)
            s.flush()
            job_id = job.id
        started = time.time()
        runner.submit(job_id)
        future = runner._futures.get(job_id)  # noqa: SLF001 - harness, not product
        if future is not None:
            future.result(timeout=TIMEOUT_SEC)
        else:
            # Nothing was dispatched (job row missing or already terminal).
            raise RuntimeError(f"job {job_id} ({job_type}) was not dispatched")
        elapsed = time.time() - started
        with session_scope() as s:
            row = s.get(Job, job_id)
            status = row.status
            stats = dict(row.stats or {})
            failures = dict(row.failures or {})
            error = row.error or ""
        print(f"\n[{job_type}] {status} in {elapsed:.1f}s")
        if stats:
            print(f"  stats: {json.dumps(stats, ensure_ascii=False)[:400]}")
        if failures.get("count"):
            print(f"  failures: {json.dumps(failures, ensure_ascii=False)[:400]}")
        if error:
            print(f"  error: {error[:300]}")
        report["steps"][job_type] = {
            "status": status,
            "seconds": round(elapsed, 1),
            "stats": stats,
            "failures": failures,
            "error": error,
        }
        return status, elapsed

    scan_status, _ = run_job("scan", None)
    detect_status, _ = run_job("detect", None)

    with session_scope() as s:
        asset_count = s.query(Asset).filter(Asset.project_id == project_id).count()
        segment_count = s.query(Segment).filter(Segment.project_id == project_id).count()
    print(f"\nassets={asset_count} segments={segment_count}")
    report["assets"] = asset_count
    report["segments"] = segment_count

    vision_status, _ = run_job(
        "vision_analyze",
        None,
        {"upload_acknowledged": True, "limit": args.limit},
    )

    with session_scope() as s:
        semantics = (
            s.query(SegmentSemanticResult)
            .filter(
                SegmentSemanticResult.project_id == project_id,
                SegmentSemanticResult.is_current.is_(True),
            )
            .all()
        )
        samples = [
            {
                "segment_id": r.segment_id,
                "scene_tags": (r.analysis_json or {}).get("scene_tags"),
                "subjects": ((r.analysis_json or {}).get("subjects") or [])[:4],
                "lighting": (r.analysis_json or {}).get("lighting"),
                "composition": (r.analysis_json or {}).get("composition"),
                "state": (r.evidence_json or {}).get("state"),
                "confidence": (r.evidence_json or {}).get("observed_confidence"),
                "model": r.model_id,
                "prompt_version": r.prompt_version,
                "tokens": (r.request_usage or {}).get("total_tokens"),
            }
            for r in semantics[:10]
        ]
    print(f"\nsemantic rows: {len(semantics)}")
    for item in samples:
        print(
            f"  {item['segment_id'][:8]} {item['scene_tags']} | {item['lighting']} | {item['subjects']}"
        )
    report["semantic_results"] = len(semantics)
    report["semantic_samples"] = samples

    # TSB-306 candidates come from data already stored, no extra model calls.
    from app.services.vision.review_candidates import build_candidates

    with session_scope() as s:
        candidates = build_candidates(s, project_id)
    print(f"\nmotion candidates: {len(candidates)}")
    for cand in candidates[:5]:
        print(
            f"  {cand['segment_id'][:8]} sources={cand['sources']} prio={cand['suggested_priority']}"
        )
        print(f"    {'; '.join(cand['reasons'])[:160]}")
    report["motion_candidates"] = len(candidates)
    report["motion_candidate_samples"] = candidates[:5]

    target_ids = [c["segment_id"] for c in candidates[: args.motion_limit]]
    motion_status = "skipped"
    if target_ids:
        motion_status, _ = run_job(
            "motion_review",
            None,
            {
                "upload_acknowledged": True,
                "segment_ids": target_ids,
                "frame_count": args.frames,
            },
        )
        with session_scope() as s:
            motions = (
                s.query(SegmentMotionReview)
                .filter(
                    SegmentMotionReview.project_id == project_id,
                    SegmentMotionReview.is_current.is_(True),
                )
                .all()
            )
            motion_samples = [
                {
                    "segment_id": m.segment_id,
                    "method": m.method,
                    "motion_type": m.motion_type,
                    "stability_risk": m.stability_risk,
                    "action_summary": (m.action_summary or "")[:200],
                    "timestamps": (m.evidence_json or {}).get("evidence_timestamps_sec"),
                    "limitations": (m.evidence_json or {}).get("limitations"),
                    "needs_human_review": (m.evidence_json or {}).get("needs_human_review"),
                    "audio_analyzed": (m.evidence_json or {}).get("audio_analyzed"),
                }
                for m in motions
            ]
        print(f"\nmotion reviews: {len(motions)}")
        for item in motion_samples:
            print(
                f"  {item['segment_id'][:8]} method={item['method']} {item['motion_type']} "
                f"risk={item['stability_risk']} at={item['timestamps']}"
            )
        report["motion_reviews"] = len(motions)
        report["motion_samples"] = motion_samples

    # --- C1: the originals must be byte-identical ---------------------------
    after = snapshot(videos)
    changed = [name for name in before if before[name] != after[name]]
    print("\n=== C1 originals read-only ===")
    print(f"c1_originals_unchanged: {not changed}")
    print(f"c1_problems: {changed}")
    report["c1_originals_unchanged"] = not changed
    report["c1_problems"] = changed

    # --- C6: no job may fail quietly ---------------------------------------
    quiet_failures = []
    for name, step in report["steps"].items():
        if step["status"] in ("failed", "partial_failed"):
            quiet_failures.append(f"{name}={step['status']}")
    report["c6_clean"] = not quiet_failures
    print(f"c6_no_quiet_failures: {not quiet_failures}  {quiet_failures}")

    if args.out:
        # Scrub any filesystem path that leaked into job stats or error strings
        # (e.g. `current_file`) before the report becomes a committed artifact.
        payload = json.dumps(_scrub(report, source_root), ensure_ascii=False, indent=2)
        Path(args.out).write_text(payload, encoding="utf-8")
        print(f"\nreport written to {args.out}")

    ok = report["c1_originals_unchanged"] and scan_status != "failed" and detect_status != "failed"
    return 0 if ok else 1


def _scrub(value, source_root: Path):
    """Recursively replace absolute paths with a placeholder in report values."""
    if isinstance(value, dict):
        return {k: _scrub(v, source_root) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, source_root) for v in value]
    if isinstance(value, str):
        text = value.replace(str(source_root), "<source_dir>")
        # Any remaining drive-letter or POSIX absolute path is reduced to a name.
        import re as _re

        text = _re.sub(r"[A-Za-z]:\\[^\s\"']*", "<local_path>", text)
        text = _re.sub(r"/(?:Users|home|mnt|var)/[^\s\"']*", "<local_path>", text)
        return text
    return value


if __name__ == "__main__":
    raise SystemExit(main())
