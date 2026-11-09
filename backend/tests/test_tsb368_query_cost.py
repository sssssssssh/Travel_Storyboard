"""TSB-368-A — query-cost contracts.

The candidates endpoint must stay usable on a real library (1000+ segments). The
classic failure here is N+1: a per-segment query for each of the three analysis
channels, which turns a 1000-shot project into 3000+ round trips.

These tests count *actual SQL statements* through a SQLAlchemy event listener, so
they measure the database rather than trusting the code's shape.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import event

from app.database import session_scope
from app.models import Asset, Project, Segment, SegmentSemanticResult
from app.services import director_candidates as dc


def _uid() -> str:
    return str(uuid.uuid4())


def _seed(count: int) -> tuple[str, list[str]]:
    """One project with `count` segments spread over a few assets."""
    project_id = _uid()
    with session_scope() as s:
        p = Project(id=project_id, name=f"n1-{count}", source_roots=[], output_dir="", settings={})
        s.add(p)
        s.flush()
        assets = []
        for i in range(min(5, max(1, count // 10 or 1))):
            a = Asset(
                id=_uid(), project_id=p.id, source_path=f"/x/{i}.mp4", display_name=f"a{i}.mp4",
                file_size_bytes=1, mtime_ns=1, quick_fingerprint="f", duration_sec=600.0,
                fps_num=30, fps_den=1, vfr_flag=False, width=640, height=360, rotation=0,
                codec="h264", pixel_format="yuv420p", color_primaries="", color_transfer="",
                color_space="", has_audio=False, audio_codec="", scan_status="succeeded",
                analysis_version="", analyzed_fingerprint="", analyzed_source_signature="",
                analyzed_detect_config="", analyzed_at=None, scanned_at=None,
            )
            s.add(a)
            assets.append(a)
        s.flush()
        seg_ids = []
        for i in range(count):
            a = assets[i % len(assets)]
            start = float(i * 10)
            g = Segment(
                id=_uid(), asset_id=a.id, project_id=p.id,
                detected_start_sec=start, detected_end_sec=start + 8,
                start_sec=start, end_sec=start + 8, is_trimmed=False, boundary_reason="",
                quality_metrics={"blur_avg": 100.0, "frame_count": 2},
                content_tags={}, story_role="", user_rating="pending", status="candidate",
                frame_refs=[], proxy_ref="", parent_segment_id="", algorithm_version="1",
                created_at=None,
            )
            s.add(g)
            seg_ids.append(g.id)
        s.flush()
        # Half the shots get a semantic result so the reader exercises its join.
        for gid in seg_ids[::2]:
            s.add(SegmentSemanticResult(
                id=_uid(), project_id=p.id,
                asset_id=s.get(Segment, gid).asset_id, segment_id=gid,
                status="succeeded", model_provider="mock", model_id="m", prompt_version="p",
                schema_version="1.0", source_signature="s", frame_manifest_hash="h",
                analysis_json={"visual": {"scene_tags": ["nature"]}},
                evidence_json={"state": "observed", "frame_ids": ["a", "b"]},
                request_usage={}, is_current=True, error_code="", error_message="",
                created_at=None, updated_at=None,
            ))
    return project_id, seg_ids


def _count_statements(project_id: str) -> int:
    """Collect the collection over a fresh session and count emitted SQL."""
    statements: list[str] = []
    with session_scope() as s:
        engine = s.get_bind()

        def _listener(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", _listener)
        try:
            dc.collect_candidates(s, project_id, verify_freshness=False)
        finally:
            event.remove(engine, "before_cursor_execute", _listener)
    return len(statements)


def test_statement_count_does_not_grow_with_the_number_of_segments():
    """The whole point: cost must be bounded, not linear in shots.

    A per-segment lookup for three channels would add ~3 statements per shot, so
    40 shots would emit 120+ more statements than 4 shots. The allowed slack
    covers fixed overhead (project, assets, annotations, three channel queries).
    """
    small_pid, _ = _seed(4)
    big_pid, _ = _seed(40)

    small = _count_statements(small_pid)
    big = _count_statements(big_pid)

    # Linear-in-N would be ~36 extra segments x 3 channels = 108.
    assert big - small <= 2, (
        f"statement count grew with segment count: {small} -> {big}. "
        "This is an N+1 pattern."
    )
    # And the absolute cost must be small and constant-ish.
    assert big <= 12, f"expected a handful of statements, got {big}"


def test_one_thousand_segments_are_collected_in_bounded_statements():
    pid, seg_ids = _seed(1000)
    with session_scope() as s:
        cands = dc.collect_candidates(s, pid, verify_freshness=False)
    assert len(cands) == 1000

    cov = dc.coverage(cands)
    assert cov["library_segments"] == 1000

    statements = _count_statements(pid)
    assert statements <= 12, f"1000 segments should not need {statements} statements"


def test_segments_with_no_analysis_are_all_still_returned():
    """1000 shots, only half analysed — the other half must not vanish."""
    pid, _ = _seed(1000)
    with session_scope() as s:
        cands = dc.collect_candidates(s, pid, verify_freshness=False)
    cov = dc.coverage(cands)
    assert cov["library_segments"] == 1000
    assert cov["no_valid_model_reading"] >= 500, cov


def test_candidates_are_not_cached_across_projects():
    """A second call must reflect the database, not a module-level cache."""
    pid1, _ = _seed(3)
    with session_scope() as s:
        first = len(dc.collect_candidates(s, pid1, verify_freshness=False))
    with session_scope() as s:
        pid2, _ = _seed(5)
    with session_scope() as s:
        second = len(dc.collect_candidates(s, pid2, verify_freshness=False))
        again = len(dc.collect_candidates(s, pid1, verify_freshness=False))
    assert (first, second, again) == (3, 5, 3)
