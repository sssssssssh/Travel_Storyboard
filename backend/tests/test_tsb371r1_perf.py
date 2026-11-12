"""TSB-371-R1 P2-01 — the review lookup must not scale with the shot count.

``review_verdict()`` handled one shot: one query for its items, then one per item
to check currency, then one per item to fetch the rating. The candidate list called
it per shot, so a library of N reviewed shots cost several queries *per shot*.

That is the classic N+1: fine on 3 shots, ruinous on 1000. The fix is a batched
form that answers the whole page in a constant number of queries.

The requirement is measured, not assumed: the tests count SQL statements actually
issued, at 100 and 1000 shots, and require the count not to grow with N. They also
require the batched result to equal the per-shot result — an optimisation that
changes behaviour is not an optimisation.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import event, select

from app.database import engine, session_scope
from app.models import (
    Asset,
    EvaluationItem,
    EvaluationRating,
    EvaluationSession,
    Segment,
    SegmentSemanticResult,
)
from app.services import evaluation_feedback as fb


def _uid() -> str:
    return str(uuid.uuid4())


def _tmp_root() -> Path:
    root = Path(os.environ.get("TEMP") or ".") / "tsb371r1-perf"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _seed_bulk(count: int, *, reviewed: int = 0, name="perf") -> tuple[str, list[str]]:
    """A project with `count` shots, `reviewed` of which carry a verdict."""
    with session_scope() as s:
        pid = _uid()
        from app.models import Project

        s.add(Project(id=pid, name=name, source_roots=[], output_dir="", settings={}))
        s.flush()

        root = _tmp_root()
        src = root / f"{pid[:8]}.mp4"
        if not src.exists():
            src.write_bytes(b"\x00" * 64)

        a = Asset(
            id=_uid(), project_id=pid, source_path=str(src), display_name="c.mp4",
            file_size_bytes=src.stat().st_size, mtime_ns=src.stat().st_mtime_ns,
            quick_fingerprint="f", duration_sec=3600.0, fps_num=25, fps_den=1,
            vfr_flag=False, width=1280, height=720, rotation=0, codec="h264",
            pixel_format="yuv420p", color_primaries="", color_transfer="",
            color_space="", has_audio=False, audio_codec="", scan_status="succeeded",
            analysis_version="", analyzed_fingerprint="",
            analyzed_source_signature="", analyzed_detect_config="",
            analyzed_at=None,
            scanned_at=datetime(2026, 10, 14, tzinfo=timezone.utc),
        )
        s.add(a)
        s.flush()

        seg_ids: list[str] = []
        for i in range(count):
            seg = Segment(
                id=_uid(), asset_id=a.id, project_id=pid,
                detected_start_sec=float(i * 10), detected_end_sec=float(i * 10 + 8),
                start_sec=float(i * 10), end_sec=float(i * 10 + 8), is_trimmed=False,
                boundary_reason="", quality_metrics={"blur_avg": 100.0},
                content_tags={}, story_role="", user_rating="pending",
                status="candidate", frame_refs=[], proxy_ref="",
                parent_segment_id="", algorithm_version="1", created_at=None,
            )
            s.add(seg)
            s.flush()
            seg_ids.append(seg.id)
            s.add(SegmentSemanticResult(
                id=_uid(), project_id=pid, asset_id=a.id, segment_id=seg.id,
                status="succeeded", model_provider="openai_compatible", model_id="GLM",
                prompt_version="p", schema_version="1.0", source_signature=_uid(),
                frame_manifest_hash="h",
                analysis_json={"scene_tags": ["lake"], "subjects": ["boats"]},
                evidence_json={"state": "observed", "frame_ids": ["f1"]},
                request_usage={}, is_current=True, error_code="", error_message="",
                created_at=None, updated_at=None,
            ))
        s.flush()

        if reviewed:
            sess = EvaluationSession(
                id=_uid(), project_id=pid, kind="footage", status="IN_PROGRESS",
                sample_seed="s", sampling_rule_version="1.0", reviewer="",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc), context_json={},
            )
            s.add(sess)
            s.flush()
            results = {
                r.segment_id: r.id for r in s.scalars(
                    select(SegmentSemanticResult).where(
                        SegmentSemanticResult.project_id == pid)).all()
            }
            for i, seg_id in enumerate(seg_ids[:reviewed]):
                item = EvaluationItem(
                    id=_uid(), session_id=sess.id, project_id=pid,
                    segment_id=seg_id, asset_id=a.id, channel="static",
                    result_id=results[seg_id], source_signature="sig",
                    evidence_state="current", start_abs_sec=float(i * 10),
                    end_abs_sec=float(i * 10 + 8), source_available=True,
                    item_key=f"k{i}", position=i, frame_ids=[],
                    readings_json={"subjects": ["boats"]},
                )
                s.add(item)
                s.flush()
                verdict = "wrong" if i % 3 == 0 else "right"
                s.add(EvaluationRating(
                    id=_uid(), item_id=item.id, session_id=sess.id, project_id=pid,
                    subject=0 if verdict == "wrong" else 2,
                    claim_verdicts=[{"key": "subjects:0", "verdict": verdict}],
                    reviewer="", version=1,
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                ))
        return pid, seg_ids


class _Counter:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def __call__(self, conn, cursor, statement, parameters, context, executemany):
        self.statements.append(statement)

    def reset(self) -> None:
        self.statements.clear()

    @property
    def selects(self) -> int:
        return sum(1 for s in self.statements if s.lstrip().upper().startswith("SELECT"))


@pytest.fixture()
def counter():
    c = _Counter()
    event.listen(engine, "before_cursor_execute", c)
    try:
        yield c
    finally:
        event.remove(engine, "before_cursor_execute", c)


# ---------------------------------------------------------------------------
# The batched API must exist and agree with the per-shot one
# ---------------------------------------------------------------------------

def test_the_batch_matches_the_per_shot_result():
    pid, seg_ids = _seed_bulk(12, reviewed=6)
    subset = seg_ids[:8]
    with session_scope() as s:
        batch = fb.verdicts_for_project(s, pid, subset)
        for seg_id in subset:
            one = fb.review_verdict(s, pid, seg_id)
            assert batch[seg_id] == one, seg_id


def test_the_batch_handles_shots_with_no_review():
    pid, seg_ids = _seed_bulk(6, reviewed=2)
    with session_scope() as s:
        batch = fb.verdicts_for_project(s, pid, seg_ids)
    assert batch[seg_ids[0]]["state"] == "wrong"
    assert batch[seg_ids[1]]["state"] == "trusted"
    assert batch[seg_ids[5]]["state"] == "unreviewed"


# ---------------------------------------------------------------------------
# Query count must not grow with the shot count
# ---------------------------------------------------------------------------

def test_100_shots_cost_a_bounded_number_of_queries(counter):
    pid, seg_ids = _seed_bulk(100, reviewed=100)
    with session_scope() as s:
        counter.reset()
        fb.verdicts_for_project(s, pid, seg_ids)
        used = counter.selects
    # A handful, not ~300.
    assert used <= 12, f"100 shots issued {used} SELECTs"


def test_1000_shots_cost_the_same_number_of_queries(counter):
    pid, seg_ids = _seed_bulk(1000, reviewed=1000)
    with session_scope() as s:
        counter.reset()
        fb.verdicts_for_project(s, pid, seg_ids)
        used = counter.selects
    assert used <= 12, f"1000 shots issued {used} SELECTs"


def test_the_query_count_does_not_grow_with_the_library(counter):
    """The point of the fix: 10x the shots must not mean 10x the queries."""
    pid_small, small = _seed_bulk(50, reviewed=50, name="small")
    adjusted_small = 0
    with session_scope() as s:
        counter.reset()
        fb.verdicts_for_project(s, pid_small, small)
        adjusted_small = counter.selects

    pid_big, big = _seed_bulk(500, reviewed=500, name="big")
    with session_scope() as s:
        counter.reset()
        fb.verdicts_for_project(s, pid_big, big)
        adjusted_big = counter.selects

    assert adjusted_big == adjusted_small, (
        f"queries grew with the library: {adjusted_small} -> {adjusted_big}")


def test_the_candidate_route_stays_bounded(counter, client):
    """The route is where the N+1 actually hurt."""
    pid, seg_ids = _seed_bulk(60, reviewed=60, name="route")
    counter.reset()
    r = client.get(f"/api/projects/{pid}/director/candidates?limit=100")
    assert r.status_code == 200
    # Count only the review-related selects to avoid coupling this test to every
    # other query the route legitimately makes.
    review_selects = sum(
        1 for st in counter.statements
        if "evaluation_items" in st or "evaluation_ratings" in st)
    assert review_selects <= 6, f"the candidates route issued {review_selects} review queries"
