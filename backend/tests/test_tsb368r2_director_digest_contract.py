"""TSB-368-R2 — P1-02: `input_digest` must cover what the context actually returns.

The defect: ``build_context`` returns ``important_human_labels`` drawn from *all*
usable shots, while ``input_digest`` was computed over ``chosen`` only. With
``max_candidates`` smaller than the library, editing a label on a shot that fell
outside ``candidate_summaries`` changed the returned context but left the digest
untouched — so a downstream cache would reuse a plan built on different input.

The RED below is deliberately built the way the task book demands: one candidate
stays stable and selected, a *second* one is out of the summaries but present in
the labels, and only that second one's note changes.
"""

from __future__ import annotations

import uuid

import pytest

from app.database import session_scope
from app.models import Asset, Project, Segment, UserAnnotation
from app.services import director_candidates as dc
from app.services import director_query as dq


def _uid() -> str:
    return str(uuid.uuid4())


def _project(s, name: str = "r2-digest") -> Project:
    p = Project(id=_uid(), name=name, source_roots=[], output_dir="", settings={})
    s.add(p)
    s.flush()
    return p


def _asset(s, project: Project) -> Asset:
    a = Asset(
        id=_uid(), project_id=project.id, source_path=f"/nonexistent/{_uid()}.mp4",
        display_name="clip.mp4", file_size_bytes=1000, mtime_ns=1, quick_fingerprint="fp",
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


def _segment(s, project, asset, start, end, *, rating="pending", role="") -> Segment:
    g = Segment(
        id=_uid(), asset_id=asset.id, project_id=project.id,
        detected_start_sec=start, detected_end_sec=end,
        start_sec=start, end_sec=end, is_trimmed=False, boundary_reason="",
        quality_metrics={}, content_tags={}, story_role=role, user_rating=rating,
        status="candidate", frame_refs=[], proxy_ref="", parent_segment_id="",
        algorithm_version="1", created_at=None,
    )
    s.add(g)
    s.flush()
    return g


def _note(s, seg, note: str, rating: str = "pending") -> UserAnnotation:
    a = UserAnnotation(
        id=_uid(), segment_id=seg.id, rating=rating, note=note,
        in_sec=None, out_sec=None, created_at=None, updated_at=None,
    )
    s.add(a)
    s.flush()
    return a


def _context(project_id: str, *, max_candidates: int = 1, theme: str = "") -> dict:
    with session_scope() as s:
        cands = dc.collect_candidates(s, project_id, verify_freshness=False)
    return dq.build_context(
        project_id=project_id, candidates=cands, coverage=dc.coverage(cands),
        brief={"theme": theme, "target_duration_sec": None},
        max_candidates=max_candidates,
    )


def _three_candidates():
    """A library where one shot is always summarized and another only reaches labels.

    Deterministic by construction: `first` is an explicit human favourite with no
    annotation row, `second` carries a note and an explicit `like` annotation. The
    favourite sorts strictly above, so `max_candidates=1` always picks `first`
    while `second` still appears in `important_human_labels`.
    """
    with session_scope() as s:
        p = _project(s)
        a = _asset(s, p)
        first = _segment(s, p, a, 0.0, 5.0, rating="favorite", role="开场")
        second = _segment(s, p, a, 5.0, 10.0, rating="like")
        _note(s, second, "老朋友重逢的画面", rating="like")
        third = _segment(s, p, a, 10.0, 15.0)
        return p.id, first.id, second.id, third.id


# ---------------------------------------------------------------------------
# The exact RED scenario from the task book
# ---------------------------------------------------------------------------

def test_changing_a_label_outside_the_candidates_changes_the_digest():
    pid, first, second, _third = _three_candidates()

    # With max_candidates=1 exactly one shot is summarized; identify it rather
    # than assuming, because editorial order is a property under test elsewhere.
    before = _context(pid, max_candidates=1)
    assert len(before["candidate_segment_ids"]) == 1
    # The edited shot must be OUTSIDE the summaries but INSIDE the labels.
    assert before["candidate_segment_ids"][0] == first, (
        "precondition: the favourite must be the single summarized shot"
    )
    label_ids = [x["segment_id"] for x in before["important_human_labels"]]
    assert second in label_ids, "test precondition: the second shot must be a label"

    with session_scope() as s:
        ann = s.query(UserAnnotation).filter(UserAnnotation.segment_id == second).one()
        ann.note = "孩子在桥上回头挥手"

    after = _context(pid, max_candidates=1)
    assert [x["note"] for x in before["important_human_labels"]] != \
           [x["note"] for x in after["important_human_labels"]], "context output changed"
    assert before["input_digest"] != after["input_digest"], (
        "the returned context changed but the digest did not — a downstream cache "
        "would reuse a plan built on different input"
    )


def test_changing_a_label_rating_outside_the_candidates_changes_the_digest():
    pid, _first, second, _third = _three_candidates()
    before = _context(pid, max_candidates=1)
    with session_scope() as s:
        ann = s.query(UserAnnotation).filter(UserAnnotation.segment_id == second).one()
        ann.rating = "favorite"
    after = _context(pid, max_candidates=1)
    assert before["input_digest"] != after["input_digest"]


def test_changing_a_label_story_role_outside_the_candidates_changes_the_digest():
    pid, _first, _second, third = _three_candidates()
    with session_scope() as s:
        g = s.get(Segment, third)
        g.story_role = "高潮"
        _note(s, g, "结尾想要这个感觉", rating="like")
    before = _context(pid, max_candidates=1)
    with session_scope() as s:
        s.get(Segment, third).story_role = "结尾"
    after = _context(pid, max_candidates=1)
    assert before["input_digest"] != after["input_digest"]


# ---------------------------------------------------------------------------
# Digest must stay a function of the whole returned input
# ---------------------------------------------------------------------------

def test_digest_covers_every_field_the_context_returns():
    """Mutable-return check: change a consumed field, digest must follow.

    Each case starts from its own fresh library. Mutating one project repeatedly
    would let an earlier change remove a shot from the output, after which later
    edits to it are legitimately invisible — that would be a flaky test, not a
    real signal.
    """
    mutations = {
        "human note": lambda s, ids: setattr(
            s.query(UserAnnotation).filter(UserAnnotation.segment_id == ids[1]).one(),
            "note", "changed note"),
        "segment rating": lambda s, ids: setattr(s.get(Segment, ids[0]), "user_rating", "like"),
        "segment story role": lambda s, ids: setattr(s.get(Segment, ids[0]), "story_role", "高潮"),
        "shot window": lambda s, ids: setattr(s.get(Segment, ids[0]), "end_sec", 4.0),
    }
    for label, mutate in mutations.items():
        pid, first, second, _third = _three_candidates()
        before = _context(pid, max_candidates=3)["input_digest"]
        with session_scope() as s:
            mutate(s, (first, second))
        after = _context(pid, max_candidates=3)["input_digest"]
        assert after != before, f"{label}: digest did not react"


def test_coverage_changes_are_reflected():
    """Coverage is part of what the planner sees, so it belongs in the digest."""
    pid, first, _second, _third = _three_candidates()
    before = _context(pid, max_candidates=2)
    with session_scope() as s:
        s.get(Segment, first).status = "needs_review"
    after = _context(pid, max_candidates=2)
    assert before["coverage"]["needs_review_segments"] != after["coverage"]["needs_review_segments"]
    assert before["input_digest"] != after["input_digest"]


# ---------------------------------------------------------------------------
# Stability guarantees must not regress
# ---------------------------------------------------------------------------

def test_identical_input_gives_an_identical_digest_across_calls_and_orders():
    pid, *_ = _three_candidates()
    a = _context(pid, max_candidates=3)
    b = _context(pid, max_candidates=3)
    assert a["input_digest"] == b["input_digest"]

    with session_scope() as s:
        cands = dc.collect_candidates(s, pid, verify_freshness=False)
    forward = dq.build_context(project_id=pid, candidates=cands, coverage={},
                               brief={}, max_candidates=3)["input_digest"]
    backward = dq.build_context(project_id=pid, candidates=list(reversed(cands)),
                                coverage={}, brief={}, max_candidates=3)["input_digest"]
    assert forward == backward, "row read order must not change the digest"


def test_a_different_project_does_not_move_the_digest():
    pid, *_ = _three_candidates()
    before = _context(pid, max_candidates=3)
    with session_scope() as s:
        other = _project(s, "unrelated")
        oa = _asset(s, other)
        og = _segment(s, oa, None, 0.0, 5.0, rating="favorite") if False else None
    with session_scope() as s:
        other = _project(s, "unrelated-2")
        oa = _asset(s, other)
        g = _segment(s, other, oa, 0.0, 5.0, rating="favorite")
        _note(s, g, "other project's label", rating="favorite")
    after = _context(pid, max_candidates=3)
    assert before["input_digest"] == after["input_digest"]


def test_excluded_shots_never_enter_the_candidates_or_the_digest_input():
    pid, _first, _second, third = _three_candidates()
    # `third` has no annotation row, so the segment rating is the effective one.
    with session_scope() as s:
        s.get(Segment, third).user_rating = "exclude"
    ctx = _context(pid, max_candidates=5)
    assert third not in ctx["candidate_segment_ids"]
    assert third not in [x["segment_id"] for x in ctx["candidate_summaries"]]


def test_an_explicit_annotation_still_outranks_a_segment_exclude():
    """Documented precedence must not regress while fixing the digest."""
    pid, _first, second, _third = _three_candidates()
    with session_scope() as s:
        s.get(Segment, second).user_rating = "exclude"
    ctx = _context(pid, max_candidates=5)
    # The shot carries an explicit `like` annotation, which is the newer,
    # more specific human statement — it wins over the segment column.
    assert second in ctx["candidate_segment_ids"]


def test_digest_has_no_paths_or_tokens():
    pid, *_ = _three_candidates()
    ctx = _context(pid, max_candidates=3)
    blob = str(ctx)
    assert "/nonexistent/" not in blob
    assert "source_path" not in blob
    digest = ctx["input_digest"]
    assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest)
