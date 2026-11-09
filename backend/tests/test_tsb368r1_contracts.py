"""TSB-368-R1 — RED tests for the three contract defects.

Each test here is written against the *current* implementation and is expected to
fail before the fix. They are deliberately precise — no ``pytest.raises(Exception)``
and no "200 means fine" — because each one describes a way the system could tell a
user something false about their own footage:

* P0-01: a shot the user excluded could still be offered for planning, because an
  auto-created annotation row (rating ``pending``) outranked the exclusion.
* P1-02: a shot awaiting human review could enter the automatic planning set.
* P1-03: the planning-input digest could stay identical after the human note or the
  model reading that the plan is based on actually changed.
"""

from __future__ import annotations

import uuid

import pytest

from app.database import session_scope
from app.models import (
    Asset,
    Project,
    Segment,
    SegmentMotionReview,
    SegmentSemanticResult,
    SegmentVideoObservation,
    UserAnnotation,
)
from app.services import director_candidates as dc
from app.services import director_query as dq


def _uid() -> str:
    return str(uuid.uuid4())


def _project(s, name: str = "r1") -> Project:
    p = Project(id=_uid(), name=name, source_roots=[], output_dir="", settings={})
    s.add(p)
    s.flush()
    return p


def _asset(s, project: Project, name: str = "clip.mp4") -> Asset:
    a = Asset(
        id=_uid(), project_id=project.id, source_path=f"/nonexistent/{_uid()}.mp4",
        display_name=name, file_size_bytes=1000, mtime_ns=1, quick_fingerprint="fp",
        duration_sec=60.0, fps_num=30, fps_den=1, vfr_flag=False, width=640, height=360,
        rotation=0, codec="h264", pixel_format="yuv420p", color_primaries="",
        color_transfer="", color_space="", has_audio=False, audio_codec="",
        scan_status="succeeded", analysis_version="", analyzed_fingerprint="",
        analyzed_source_signature="", analyzed_detect_config="",
        analyzed_at=None, scanned_at=None,
    )
    s.add(a)
    s.flush()
    return a


def _segment(s, project, asset, start=0.0, end=5.0, *, status="candidate",
             rating="pending", story_role="", metrics=None) -> Segment:
    g = Segment(
        id=_uid(), asset_id=asset.id, project_id=project.id,
        detected_start_sec=start, detected_end_sec=end,
        start_sec=start, end_sec=end, is_trimmed=False, boundary_reason="",
        quality_metrics=metrics if metrics is not None else {"blur_avg": 100.0},
        content_tags={}, story_role=story_role, user_rating=rating, status=status,
        frame_refs=[], proxy_ref="", parent_segment_id="", algorithm_version="1",
        created_at=None,
    )
    s.add(g)
    s.flush()
    return g


def _annotation(s, seg, rating="pending", note="") -> UserAnnotation:
    a = UserAnnotation(
        id=_uid(), segment_id=seg.id, rating=rating, note=note,
        in_sec=None, out_sec=None, created_at=None, updated_at=None,
    )
    s.add(a)
    s.flush()
    return a


def _semantic(s, project, asset, seg, *, signature="sig-1", tags=None,
              is_current=True) -> SegmentSemanticResult:
    r = SegmentSemanticResult(
        id=_uid(), project_id=project.id, asset_id=asset.id, segment_id=seg.id,
        status="succeeded", model_provider="mock", model_id="m", prompt_version="p",
        schema_version="1.0", source_signature=signature, frame_manifest_hash="h",
        analysis_json={"visual": {"scene_tags": tags or ["nature"]}},
        evidence_json={"state": "observed", "frame_ids": ["f1", "f2"]},
        request_usage={}, is_current=is_current, error_code="", error_message="",
        created_at=None, updated_at=None,
    )
    s.add(r)
    s.flush()
    return r


def _collect(project_id: str, *, verify: bool = False) -> list[dc.Candidate]:
    with session_scope() as s:
        return dc.collect_candidates(s, project_id, verify_freshness=verify)


def _context(project_id: str, *, limit: int = 50, theme: str = "",
             duration=None) -> dict:
    cands = _collect(project_id)
    return dq.build_context(
        project_id=project_id, candidates=cands, coverage=dc.coverage(cands),
        brief={"theme": theme, "target_duration_sec": duration},
        max_candidates=limit,
    )


# ===========================================================================
# P0-01 — human exclusion must win over a `pending` annotation row
# ===========================================================================

def test_segment_exclude_with_pending_annotation_is_still_excluded():
    """The defect: an auto-created `pending` annotation erases the user's exclude."""
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, rating="exclude")
        # This is what the app creates when a user merely types a note.
        _annotation(s, g, rating="pending", note="只是记一笔")
        pid, gid = p.id, g.id

    cands = _collect(pid)
    assert len(cands) == 1
    c = cands[0]
    assert c.human["rating"] == "exclude", (
        "a pending annotation must not override an explicit exclude"
    )
    assert c.eligibility == dc.EXCLUDED, (
        "a shot the user excluded must not be eligible"
    )
    assert "human_excluded" in c.exclusion_reasons


def test_excluded_shot_never_reaches_the_planning_context():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, rating="exclude")
        _annotation(s, g, rating="pending", note="note only")
        pid, gid = p.id, g.id

    ctx = _context(pid)
    assert gid not in ctx["candidate_segment_ids"]


def test_excluded_shot_is_invisible_to_the_default_eligible_query():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, rating="exclude")
        _annotation(s, g, rating="pending")
        pid, gid = p.id, g.id

    cands = _collect(pid)
    page, total = dq.select(cands, eligibility=dc.ELIGIBLE, limit=50)
    assert total == 0, "eligible query must not return an excluded shot"
    assert page == []


def test_annotation_exclude_overrides_a_pending_segment():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, rating="pending")
        _annotation(s, g, rating="exclude")
        pid = p.id

    c = _collect(pid)[0]
    assert c.human["rating"] == "exclude"
    assert c.eligibility == dc.EXCLUDED


def test_annotation_favorite_overrides_a_segment_exclude():
    """Documented precedence: an explicit annotation outranks the segment column."""
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, rating="exclude")
        _annotation(s, g, rating="favorite", note="反悔了，保留")
        pid = p.id

    c = _collect(pid)[0]
    assert c.human["rating"] == "favorite"
    assert c.human["rating_source"] == "annotation"
    assert c.eligibility == dc.ELIGIBLE


def test_a_note_without_a_rating_does_not_change_segment_rating():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a, rating="like")
        _annotation(s, g, rating="pending", note="keep this one")
        pid = p.id

    c = _collect(pid)[0]
    assert c.human["rating"] == "like"
    assert c.human["note"] == "keep this one"
    assert c.human["rating_source"] == "segment"


def test_coverage_denominator_still_counts_excluded_shots():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        _segment(s, p, a, 0.0, 5.0, rating="exclude")
        _segment(s, p, a, 5.0, 10.0, rating="pending")
        pid = p.id

    cov = dc.coverage(_collect(pid))
    assert cov["library_segments"] == 2
    assert cov["excluded_segments"] == 1
    assert cov["eligible_segments"] == 1


def test_human_rating_and_eligibility_can_never_contradict():
    """A single source of truth: whatever `human.rating` says drives eligibility."""
    cases = [
        ("exclude", "pending", "exclude", dc.EXCLUDED),
        ("pending", "exclude", "exclude", dc.EXCLUDED),
        ("exclude", "favorite", "favorite", dc.ELIGIBLE),
        ("favorite", "pending", "favorite", dc.ELIGIBLE),
        ("like", "pending", "like", dc.ELIGIBLE),
        ("pending", "pending", "pending", dc.ELIGIBLE),
    ]
    for seg_rating, ann_rating, expected_rating, expected_elig in cases:
        with session_scope() as s:
            p = _project(s, f"r1-{seg_rating}-{ann_rating}")
            a = _asset(s, p)
            g = _segment(s, p, a, rating=seg_rating)
            _annotation(s, g, rating=ann_rating)
            pid = p.id
        c = _collect(pid)[0]
        assert c.human["rating"] == expected_rating, (
            f"segment={seg_rating} annotation={ann_rating}"
        )
        assert c.eligibility == expected_elig, (
            f"segment={seg_rating} annotation={ann_rating}: "
            f"rating={c.human['rating']} but eligibility={c.eligibility}"
        )


# ===========================================================================
# P0-02 (found in real-library UAT) — analysis_json shape
# ===========================================================================

def test_a_flat_analysis_json_is_read_correctly():
    """The production writer stores `validated.visual` at the TOP level.

    `semantic._store_result` does `analysis_json=validated.visual`, so real rows
    look like {"scene_tags": [...], "subjects": [...]}. An earlier version of the
    director layer assumed {"visual": {...}} and therefore reported every real
    still-frame reading as empty — the content was there, the reader missed it.
    """
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a)
        r = SegmentSemanticResult(
            id=_uid(), project_id=p.id, asset_id=a.id, segment_id=g.id,
            status="succeeded", model_provider="mock", model_id="m",
            prompt_version="p", schema_version="1.0",
            source_signature="sig", frame_manifest_hash="h",
            # Flat, exactly as the production writer produces it.
            analysis_json={"scene_tags": ["water"], "subjects": ["boats on a lake"],
                           "visible_actions": ["boats drifting"], "lighting": "daylight",
                           "composition": "wide", "mood": ["calm"]},
            evidence_json={"state": "observed", "frame_ids": ["f1"]},
            request_usage={}, is_current=True, error_code="", error_message="",
            created_at=None, updated_at=None,
        )
        s.add(r)
        s.flush()
        pid = p.id

    c = _collect(pid)[0]
    visual = (c.static.payload.get("visual") or {})
    assert visual.get("scene_tags") == ["water"], (
        "a flat analysis_json must be surfaced under 'visual'"
    )
    assert visual.get("subjects") == ["boats on a lake"]
    assert visual.get("visible_actions") == ["boats drifting"]
    assert c.static.payload.get("evidence_state") == "observed"


def test_a_nested_analysis_json_is_still_read():
    """Historical rows are nested; both shapes must remain supported."""
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a)
        _semantic(s, p, a, g, tags=["nature"])   # helper writes {"visual": {...}}
        pid = p.id

    c = _collect(pid)[0]
    assert (c.static.payload.get("visual") or {}).get("scene_tags") == ["nature"]


def test_a_flat_reading_changes_the_digest():
    """Content that is actually read must influence the digest."""
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a)
        r = SegmentSemanticResult(
            id=_uid(), project_id=p.id, asset_id=a.id, segment_id=g.id,
            status="succeeded", model_provider="mock", model_id="m",
            prompt_version="p", schema_version="1.0",
            source_signature="same", frame_manifest_hash="h",
            analysis_json={"scene_tags": ["water"], "subjects": [], "visible_actions": []},
            evidence_json={"state": "observed", "frame_ids": ["f1"]},
            request_usage={}, is_current=True, error_code="", error_message="",
            created_at=None, updated_at=None,
        )
        s.add(r)
        s.flush()
        pid, seg_id = p.id, g.id

    before = _digest(pid)
    with session_scope() as s:
        row = s.query(SegmentSemanticResult).filter(
            SegmentSemanticResult.segment_id == seg_id).one()
        row.analysis_json = {"scene_tags": ["mountain"], "subjects": [],
                             "visible_actions": []}
    assert _digest(pid) != before


# ===========================================================================
# P1-02 — needs_review must not become an automatic planning candidate
# ===========================================================================

@pytest.mark.parametrize("status", ["needs_review", "archived", "invalid"])
def test_a_shot_awaiting_review_is_not_an_automatic_planning_candidate(status):
    with session_scope() as s:
        p = _project(s, f"r1-{status}")
        a = _asset(s, p)
        g = _segment(s, p, a, status=status)
        pid, gid = p.id, g.id

    # Still visible for human inspection...
    all_page, all_total = dq.select(_collect(pid), eligibility="all", limit=50)
    assert all_total == 1
    assert all_page[0].segment_id == gid

    # ...but never offered to the planner.
    ctx = _context(pid)
    assert gid not in ctx["candidate_segment_ids"], (
        f"a {status} shot must not be an automatic planning candidate"
    )
    assert gid not in [s["segment_id"] for s in ctx["candidate_summaries"]]


def test_an_unanalysed_eligible_shot_is_still_a_planning_candidate():
    """'No model reading' is not a reason to withhold a legitimate shot."""
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a)   # candidate + pending, no readings at all
        pid, gid = p.id, g.id

    ctx = _context(pid)
    assert gid in ctx["candidate_segment_ids"], (
        "an unanalysed but eligible shot must remain available to the planner"
    )


def test_context_reports_review_and_exclusion_counts():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        _segment(s, p, a, 0.0, 5.0, status="needs_review")
        _segment(s, p, a, 5.0, 10.0, rating="exclude")
        _segment(s, p, a, 10.0, 15.0)
        pid = p.id

    ctx = _context(pid)
    assert len(ctx["candidate_segment_ids"]) == 1
    cov = ctx["coverage"]
    assert cov["needs_review_segments"] == 1
    assert cov["excluded_segments"] == 1
    assert cov["library_segments"] == 3


def test_context_ids_and_summaries_stay_consistent():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        _segment(s, p, a, 0.0, 5.0)
        _segment(s, p, a, 5.0, 10.0, status="needs_review")
        _segment(s, p, a, 10.0, 15.0, rating="exclude")
        pid = p.id

    ctx = _context(pid)
    ids = ctx["candidate_segment_ids"]
    summary_ids = [s["segment_id"] for s in ctx["candidate_summaries"]]
    assert ids == summary_ids, "ids and summaries must describe the same set"


# ===========================================================================
# P1-03 — input_digest must track the input the planner actually consumes
# ===========================================================================

def _digest(project_id: str, **kw) -> str:
    return _context(project_id, **kw)["input_digest"]


def test_digest_is_deterministic_across_calls():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        _segment(s, p, a, 0.0, 5.0)
        pid = p.id

    assert _digest(pid) == _digest(pid)


def test_digest_changes_when_only_the_human_note_changes():
    """The defect: the note is part of the planning input but was not digested."""
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a)
        _annotation(s, g, rating="pending", note="普通海景")
        pid, ann_seg = p.id, g.id

    before = _digest(pid)
    with session_scope() as s:
        ann = s.query(UserAnnotation).filter(UserAnnotation.segment_id == ann_seg).one()
        ann.note = "旅程情绪高潮"
    after = _digest(pid)
    assert before != after, "changing a human note must change the input digest"


def test_digest_changes_when_the_current_semantic_result_changes():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a)
        _semantic(s, p, a, g, signature="sig-A", tags=["nature"])
        pid, seg_id = p.id, g.id

    before = _digest(pid)
    with session_scope() as s:
        r = s.query(SegmentSemanticResult).filter(
            SegmentSemanticResult.segment_id == seg_id
        ).one()
        # A re-run produces different evidence for the same shot.
        r.analysis_json = {"visual": {"scene_tags": ["water"]}}
        r.source_signature = "sig-B"
    after = _digest(pid)
    assert before != after, "changing the model reading must change the digest"


def test_digest_changes_when_a_motion_review_is_replaced():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a)
        r = SegmentMotionReview(
            id=_uid(), project_id=p.id, asset_id=a.id, segment_id=g.id,
            method="multiframe", status="succeeded", model_provider="mock",
            model_id="m", prompt_version="p", start_sec=g.start_sec, end_sec=g.end_sec,
            frame_manifest_hash="h", input_signature="in-A", motion_type="pan",
            stability_risk="low", action_summary="camera pans right",
            analysis_json={}, evidence_json={}, request_usage={}, is_current=True,
            human_verdict="", human_note="", human_at=None, error_code="",
            error_message="", created_at=None, updated_at=None,
        )
        s.add(r)
        s.flush()
        pid, seg_id = p.id, g.id

    before = _digest(pid)
    with session_scope() as s:
        r = s.query(SegmentMotionReview).filter(
            SegmentMotionReview.segment_id == seg_id
        ).one()
        r.action_summary = "camera tilts up"
        r.input_signature = "in-B"
    after = _digest(pid)
    assert before != after, "changing a motion reading must change the digest"


def test_digest_tracks_reading_content_even_when_the_version_id_is_unchanged():
    """The reason content is digested separately from the result id.

    Re-running a model can rewrite a row in place (or reuse the same signature
    while correcting the payload). If the digest only tracked ids and versions it
    would report "nothing changed" while the plan was built on different facts.
    """
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a)
        _semantic(s, p, a, g, signature="same-sig", tags=["nature"])
        pid, seg_id = p.id, g.id

    before = _digest(pid)
    with session_scope() as s:
        r = s.query(SegmentSemanticResult).filter(
            SegmentSemanticResult.segment_id == seg_id
        ).one()
        # Same row, same id, same source_signature — only the observed content moves.
        r.analysis_json = {"visual": {"scene_tags": ["water", "beach"]}}
    after = _digest(pid)
    assert before != after, (
        "content changed while the result id and source version stayed identical, "
        "so the digest must still change"
    )


def test_digest_tracks_motion_summary_content_with_a_stable_id():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a)
        r = SegmentMotionReview(
            id=_uid(), project_id=p.id, asset_id=a.id, segment_id=g.id,
            method="multiframe", status="succeeded", model_provider="mock",
            model_id="m", prompt_version="p", start_sec=g.start_sec, end_sec=g.end_sec,
            frame_manifest_hash="h", input_signature="stable", motion_type="pan",
            stability_risk="low", action_summary="camera pans right",
            analysis_json={}, evidence_json={}, request_usage={}, is_current=True,
            human_verdict="", human_note="", human_at=None, error_code="",
            error_message="", created_at=None, updated_at=None,
        )
        s.add(r)
        s.flush()
        pid, seg_id = p.id, g.id

    before = _digest(pid)
    with session_scope() as s:
        row = s.query(SegmentMotionReview).filter(
            SegmentMotionReview.segment_id == seg_id
        ).one()
        row.action_summary = "camera tilts up"     # id and signature unchanged
    assert _digest(pid) != before


def test_digest_changes_when_theme_or_duration_changes():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        _segment(s, p, a)
        pid = p.id

    base = _digest(pid)
    assert _digest(pid, theme="海边日落") != base
    assert _digest(pid, duration=120) != base


def test_digest_changes_when_the_candidate_set_changes():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a)
        pid, gid = p.id, g.id

    before = _digest(pid)
    with session_scope() as s:
        s.get(Segment, gid).user_rating = "exclude"   # drops out of candidates
    after = _digest(pid)
    assert before != after


def test_digest_ignores_other_projects():
    with session_scope() as s:
        p1 = _project(s, "r1-a")
        p2 = _project(s, "r1-b")
        a1 = _asset(s, p1)
        a2 = _asset(s, p2)
        _segment(s, p1, a1)
        g2 = _segment(s, p2, a2)
        pid1, pid2, seg2 = p1.id, p2.id, g2.id

    before = _digest(pid1)
    with session_scope() as s:
        s.get(Segment, seg2).user_rating = "favorite"
    assert _digest(pid1) == before, "another project must not perturb this digest"


def test_digest_ignores_a_superseded_reading():
    """Only the *current* reading is input; a historical row is not."""
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a)
        _semantic(s, p, a, g, signature="cur")
        old = _semantic(s, p, a, g, signature="old", tags=["urban"], is_current=False)
        pid, old_id = p.id, old.id

    before = _digest(pid)
    with session_scope() as s:
        r = s.get(SegmentSemanticResult, old_id)
        r.analysis_json = {"visual": {"scene_tags": ["market"]}}
    assert _digest(pid) == before, (
        "editing a superseded result must not change the current planning input"
    )


def test_digest_has_no_paths_tokens_or_random_noise():
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        g = _segment(s, p, a)
        _semantic(s, p, a, g)
        _annotation(s, g, rating="favorite", note="keep")
        pid = p.id

    ctx = _context(pid)
    digest = ctx["input_digest"]
    assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
    blob = str(ctx)
    assert "/nonexistent/" not in blob
    assert "source_path" not in blob
