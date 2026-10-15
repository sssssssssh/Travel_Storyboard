"""TSB-306 — dynamic-review candidate generation.

The central claim under test is truthfulness of labelling: a rule-based
frame-difference hint must be presented as an observation that needs
verification, never as a proven camera shake, and a single-frame shot must be
flagged as unmeasurable rather than dropped.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select

from app.database import session_scope
from app.models import Asset, Frame, Segment, SegmentSemanticResult
from app.paths import frames_dir, rel_to_data
from app.services.vision import review_candidates as rc
from app.services.vision.semantic import INVALID_SEGMENT_STATUSES


@pytest.fixture()
def project(client):
    res = client.post("/api/projects", json={"name": "motion-test", "source_roots": []})
    assert res.status_code in (200, 201), res.text
    return res.json()


def _frame(path: Path, colour: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (160, 90), colour).save(path, format="JPEG")


def _make_shot(
    project_id: str,
    *,
    colours: list[tuple[int, int, int]],
    rating: str = "pending",
    status: str = "ready",
    start: float = 0.0,
) -> str:
    """Create one asset+segment with the given representative frames."""
    asset_id = str(uuid.uuid4())
    seg_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(
            Asset(
                id=asset_id,
                project_id=project_id,
                source_path=f"C:/footage/{asset_id}.mp4",
                display_name=f"{asset_id}.mp4",
                file_size_bytes=1,
            )
        )
        s.flush()
        s.add(
            Segment(
                id=seg_id,
                project_id=project_id,
                asset_id=asset_id,
                start_sec=start,
                end_sec=start + 5,
                detected_start_sec=start,
                detected_end_sec=start + 5,
                user_rating=rating,
                status=status,
            )
        )
        s.flush()
        for index, colour in enumerate(colours):
            path = frames_dir(project_id, asset_id) / f"{seg_id}-{index}.jpg"
            _frame(path, colour)
            s.add(
                Frame(
                    id=str(uuid.uuid4()),
                    segment_id=seg_id,
                    asset_id=asset_id,
                    time_sec=float(index),
                    image_path=rel_to_data(path),
                )
            )
    return seg_id


def _candidates(project_id: str) -> list[dict]:
    with session_scope() as s:
        return rc.build_candidates(s, project_id)


def _for(candidates: list[dict], segment_id: str) -> dict | None:
    return next((c for c in candidates if c["segment_id"] == segment_id), None)


# --------------------------------------------------------------------------
# rule source
# --------------------------------------------------------------------------


def test_static_shot_is_not_a_candidate(project):
    """A shot with near-identical frames must not be queued for motion review."""
    seg_id = _make_shot(project["id"], colours=[(10, 10, 10), (12, 12, 12)])
    assert _for(_candidates(project["id"]), seg_id) is None


def test_high_frame_difference_is_a_candidate(project):
    seg_id = _make_shot(project["id"], colours=[(0, 0, 0), (255, 255, 255)])
    cand = _for(_candidates(project["id"]), seg_id)
    assert cand is not None
    assert "rule" in cand["sources"]
    assert cand["temporal_coverage_insufficient"] is False


def test_rule_reason_never_asserts_a_shake(project):
    """The wording must report the measurement and ask for verification."""
    seg_id = _make_shot(project["id"], colours=[(0, 0, 0), (255, 255, 255)])
    cand = _for(_candidates(project["id"]), seg_id)
    joined = " ".join(cand["reasons"])
    assert "疑似画面变化，需核验" in joined
    assert "不能证明抖动" in joined
    # The old over-claiming wording must be gone.
    assert "抖动或快速甩镜" not in joined


# --------------------------------------------------------------------------
# coverage source
# --------------------------------------------------------------------------


def test_single_frame_shot_is_flagged_not_dropped(project):
    """A still cannot establish motion; the shot is kept and labelled."""
    seg_id = _make_shot(project["id"], colours=[(50, 50, 50)])
    cand = _for(_candidates(project["id"]), seg_id)
    assert cand is not None, "a single-frame shot must not be silently excluded"
    assert cand["temporal_coverage_insufficient"] is True
    assert cand["frame_count"] == 1
    assert "temporal_coverage_insufficient" in " ".join(cand["reasons"])


# --------------------------------------------------------------------------
# semantic source
# --------------------------------------------------------------------------


def test_semantic_flag_produces_a_high_priority_candidate(project):
    seg_id = _make_shot(project["id"], colours=[(10, 10, 10)])
    with session_scope() as s:
        seg = s.get(Segment, seg_id)
        s.add(
            SegmentSemanticResult(
                project_id=project["id"],
                asset_id=seg.asset_id,
                segment_id=seg_id,
                status="succeeded",
                model_id="stub",
                evidence_json={
                    "state": "observed",
                    "review": {
                        "needs_motion_review": True,
                        "reasons": ["insufficient_temporal_evidence"],
                    },
                },
                analysis_json={"scene_tags": ["water"]},
                is_current=True,
            )
        )
    cand = _for(_candidates(project["id"]), seg_id)
    assert cand is not None
    assert "semantic" in cand["sources"]
    assert cand["suggested_priority"] == "high"
    assert any("模型建议" in r for r in cand["reasons"])


def test_semantic_and_rule_sources_are_merged(project):
    """One shot may qualify from both; sources must be reported, not deduped away."""
    seg_id = _make_shot(project["id"], colours=[(0, 0, 0), (255, 255, 255)])
    with session_scope() as s:
        seg = s.get(Segment, seg_id)
        s.add(
            SegmentSemanticResult(
                project_id=project["id"],
                asset_id=seg.asset_id,
                segment_id=seg_id,
                status="succeeded",
                model_id="stub",
                evidence_json={
                    "review": {"needs_motion_review": True, "reasons": ["why"]},
                },
                analysis_json={},
                is_current=True,
            )
        )
    cand = _for(_candidates(project["id"]), seg_id)
    assert set(cand["sources"]) >= {"semantic", "rule"}


def test_non_current_semantic_rows_are_ignored(project):
    """A superseded reading must not drive a new review."""
    # Two near-identical frames: with no rule signal and no *current* semantic
    # flag, this shot must not appear at all.
    seg_id = _make_shot(project["id"], colours=[(10, 10, 10), (10, 10, 10)])
    with session_scope() as s:
        seg = s.get(Segment, seg_id)
        s.add(
            SegmentSemanticResult(
                project_id=project["id"],
                asset_id=seg.asset_id,
                segment_id=seg_id,
                status="succeeded",
                model_id="stub",
                evidence_json={"review": {"needs_motion_review": True, "reasons": ["old"]}},
                analysis_json={},
                is_current=False,  # superseded: must not drive a new review
            )
        )
    assert _for(_candidates(project["id"]), seg_id) is None


# --------------------------------------------------------------------------
# exclusions
# --------------------------------------------------------------------------


def test_excluded_and_invalid_shots_are_not_candidates(project):
    excluded = _make_shot(project["id"], colours=[(0, 0, 0), (255, 255, 255)], rating="exclude")
    invalid = _make_shot(
        project["id"], colours=[(0, 0, 0), (255, 255, 255)], status=INVALID_SEGMENT_STATUSES[0]
    )
    found = {c["segment_id"] for c in _candidates(project["id"])}
    assert excluded not in found
    assert invalid not in found


def test_works_without_a_configured_model(project):
    """Rule and coverage candidates need no model at all (A01)."""
    seg_id = _make_shot(project["id"], colours=[(0, 0, 0), (255, 255, 255)])
    found = {c["segment_id"] for c in _candidates(project["id"])}
    assert seg_id in found


def test_measured_difference_is_deterministic(project):
    """The same pixels must always yield the same hint (reproducible evidence)."""
    seg_id = _make_shot(project["id"], colours=[(0, 0, 0), (128, 128, 128)])
    first = _for(_candidates(project["id"]), seg_id)
    second = _for(_candidates(project["id"]), seg_id)
    assert first["reasons"] == second["reasons"]
    assert first["suggested_priority"] == second["suggested_priority"]
