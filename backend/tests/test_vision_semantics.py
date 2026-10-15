"""TSB-304 — semantic job: selection, persistence, coverage, idempotency.

These exercise the real worker function against a stubbed provider, so what is
verified is the *product* behaviour — what gets stored, what gets skipped, what
the job reports — rather than the shape of a mock.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select

from app.database import session_scope
from app.models import Asset, Frame, Job, Segment, SegmentSemanticResult
from app.paths import frames_dir, rel_to_data, project_cache_dir
from app.services.vision import semantic
from app.services.vision.errors import ModelError, MODEL_NOT_CONFIGURED
from app.services.vision.providers.base import VisionResponse


def _jpeg(path: Path, colour=(90, 110, 130)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (320, 180), colour).save(path, format="JPEG")


@pytest.fixture()
def project(client):
    res = client.post("/api/projects", json={"name": "vision-test", "source_roots": []})
    assert res.status_code in (200, 201), res.text
    return res.json()


def _seed(project_id: str, layout: list[tuple[str, int, str]]) -> dict:
    """Seed assets/segments/frames; returns {'assets': {...}, 'segments': [...]}.

    Asset, segment and frame are written in one transaction so the foreign keys
    are satisfied regardless of flush order.
    """
    assets: dict[str, str] = {}
    segments: list[dict] = []
    for name, count, rating in layout:
        asset_id = str(uuid.uuid4())
        assets[name] = asset_id
        source = project_cache_dir(project_id) / 'test-sources' / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b'synthetic source for semantics unit tests')
        with session_scope() as s:
            s.add(
                Asset(
                    id=asset_id,
                    project_id=project_id,
                    source_path=str(source),
                    display_name=name,
                    file_size_bytes=1000,
                    mtime_ns=1,
                )
            )
            s.flush()
            for i in range(count):
                seg_id = str(uuid.uuid4())
                frame_dir = frames_dir(project_id, asset_id)
                frame_path = frame_dir / f"{seg_id}-0.jpg"
                _jpeg(frame_path, (30 + i * 20, 60, 90))
                s.add(
                    Segment(
                        id=seg_id,
                        project_id=project_id,
                        asset_id=asset_id,
                        start_sec=float(i * 10),
                        end_sec=float(i * 10 + 5),
                        detected_start_sec=float(i * 10),
                        detected_end_sec=float(i * 10 + 5),
                        user_rating=rating,
                        status="ready",
                    )
                )
                s.flush()
                s.add(
                    Frame(
                        id=str(uuid.uuid4()),
                        segment_id=seg_id,
                        asset_id=asset_id,
                        time_sec=0.0,
                        image_path=rel_to_data(frame_path),
                    )
                )
                segments.append({"segment_id": seg_id, "asset_id": asset_id, "rating": rating})
    return {"assets": assets, "segments": segments}


class _StubProvider:
    """Returns a valid semantics payload for whichever ids it was asked about."""

    name = "stub"

    def __init__(self, *, behaviour=None):
        self.calls: list = []
        self.behaviour = behaviour

    def complete(self, request, *, cancel_check=None):
        if cancel_check is not None and cancel_check():
            raise ModelError("CANCELLED", "cancelled")
        self.calls.append(request)
        if self.behaviour is not None:
            return self.behaviour(request, len(self.calls))

        # Recover the segment_id the server put in the prompt.
        import re

        match = re.search(r"segment_id=([0-9a-f-]{36})", request.prompt)
        seg_id = match.group(1) if match else "unknown"
        body = {
            "results": [
                {
                    "segment_id": seg_id,
                    "visual": {
                        "scene_tags": ["water"],
                        "subjects": ["lake"],
                        "visible_actions": [],
                        "lighting": "daylight",
                        "composition": "wide",
                        "mood": ["calm"],
                    },
                    "evidence": {
                        "state": "observed",
                        "observed_confidence": 0.8,
                        "limitations": [],
                    },
                    "review": {"needs_motion_review": False, "reasons": []},
                }
            ]
        }
        return VisionResponse(
            text=json.dumps(body),
            model_id="stub-model",
            usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            latency_ms=5,
            request_id="req-1",
        )


@pytest.fixture()
def stubbed(monkeypatch):
    """Enable the vision path with a stub provider and a generous budget."""
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "model_mode", "on", raising=False)
    monkeypatch.setattr(s, "model_base_url", "http://127.0.0.1:9/v1", raising=False)
    monkeypatch.setattr(s, "model_api_key", "test-key-abcdefgh", raising=False)
    monkeypatch.setattr(s, "model_name", "stub-model", raising=False)
    monkeypatch.setattr(s, "model_max_calls_per_job", 50, raising=False)
    monkeypatch.setattr(s, "vision_max_segments_per_job", 200, raising=False)

    provider = _StubProvider()
    monkeypatch.setattr(semantic, "get_provider", lambda: provider, raising=False)
    import app.services.vision as vision_pkg

    monkeypatch.setattr(vision_pkg, "get_provider", lambda: provider, raising=False)
    return provider


def _make_job(project_id: str, **config) -> str:
    job_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(
            Job(
                id=job_id,
                project_id=project_id,
                job_type="vision_analyze",
                status="pending",
                config_snapshot={"upload_acknowledged": True, **config},
                stats={},
                failures={"count": 0, "items": []},
            )
        )
    return job_id


def _job_row(job_id: str) -> Job:
    with session_scope() as s:
        return s.get(Job, job_id)


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------


def test_selection_excludes_excluded_and_invalid_shots(project):
    _seed(project["id"], [("a.mp4", 2, "pending"), ("b.mp4", 1, "exclude")])
    cands = semantic.select_candidates(project["id"])
    assert len(cands) == 2  # the excluded shot is not sent
    ratings = {c.user_rating for c in cands}
    assert ratings == {"pending"}


def test_selection_spreads_across_assets(project):
    """A long first video must not consume the entire batch budget."""
    _seed(project["id"], [("a.mp4", 5, "pending"), ("b.mp4", 1, "pending")])
    cands = semantic.select_candidates(project["id"], limit=2)
    assert len(cands) == 2
    assert len({c.asset_id for c in cands}) == 2, "first two must span both assets"


def test_selection_skips_shots_without_frames(project):
    _seed(project["id"], [("a.mp4", 1, "pending")])
    with session_scope() as s:
        seg = s.scalars(select(Segment).where(Segment.project_id == project["id"])).one()
        seg_id = seg.id
        # Give it a second segment with no frames at all.
    with session_scope() as s:
        s.add(
            Segment(
                id=str(uuid.uuid4()),
                project_id=project["id"],
                asset_id=seg.asset_id,
                start_sec=90.0,
                end_sec=95.0,
                detected_start_sec=90.0,
                detected_end_sec=95.0,
                status="ready",
            )
        )
    cands = semantic.select_candidates(project["id"])
    assert [c.segment_id for c in cands] == [seg_id]


# --------------------------------------------------------------------------
# job happy path
# --------------------------------------------------------------------------


def test_job_analyzes_and_stores_results(project, stubbed):
    _seed(project["id"], [("a.mp4", 2, "pending"), ("b.mp4", 1, "pending")])
    job_id = _make_job(project["id"])
    semantic.vision_analyze_task(job_id)

    row = _job_row(job_id)
    assert row.status == "succeeded", row.error
    assert row.stats["analyzed"] == 3
    assert row.stats["failed"] == 0
    assert row.stats["images_sent"] == 3
    assert row.stats["usage"]["total_tokens"] == 90

    with session_scope() as s:
        stored = s.scalars(
            select(SegmentSemanticResult).where(
                SegmentSemanticResult.project_id == project["id"]
            )
        ).all()
    assert len(stored) == 3
    assert all(r.is_current for r in stored)
    assert all(r.model_id == "stub-model" for r in stored)
    assert all(r.analysis_json["scene_tags"] == ["water"] for r in stored)


def test_job_reports_coverage_not_a_total_claim(project, stubbed):
    """Analysing a subset must be reported as a subset."""
    _seed(project["id"], [("a.mp4", 3, "pending"), ("b.mp4", 1, "exclude")])
    job_id = _make_job(project["id"], limit=2)
    semantic.vision_analyze_task(job_id)

    stats = _job_row(job_id).stats
    cov = stats["coverage"]
    assert stats["total"] == 2
    assert cov["library_segments"] == 4
    assert cov["eligible"] == 3
    assert cov["excluded"] == 1
    assert cov["considered"] == 2
    assert cov["not_covered"] == 1, "the un-analysed eligible shot must be visible"


def test_job_records_provenance(project, stubbed):
    _seed(project["id"], [("a.mp4", 1, "pending")])
    job_id = _make_job(project["id"])
    semantic.vision_analyze_task(job_id)

    from app.config import get_settings

    with session_scope() as s:
        row = s.scalars(
            select(SegmentSemanticResult).where(
                SegmentSemanticResult.project_id == project["id"]
            )
        ).one()
    assert row.model_provider == "stub"
    assert row.prompt_version == get_settings().vision_prompt_version
    assert row.schema_version == "1.0"
    assert row.source_signature and row.frame_manifest_hash
    assert row.request_usage["total_tokens"] == 30


def test_staging_images_are_cleaned_up(project, stubbed):
    _seed(project["id"], [("a.mp4", 1, "pending")])
    job_id = _make_job(project["id"])
    semantic.vision_analyze_task(job_id)
    assert not semantic.stage_image_dir(project["id"], job_id).exists()


def test_uploaded_bytes_carry_no_local_path(project, stubbed):
    """Privacy: the prompt names ids and labels, never a filesystem path."""
    _seed(project["id"], [("a.mp4", 1, "pending")])
    job_id = _make_job(project["id"])
    semantic.vision_analyze_task(job_id)

    assert stubbed.calls, "the provider should have been called"
    prompt = stubbed.calls[0].prompt
    assert "C:/footage" not in prompt and "C:\\" not in prompt
    for image in stubbed.calls[0].images:
        assert image.data and image.data.startswith(b"\xff\xd8")
        assert image.path == ""
        assert image.label.startswith("frame_")


# --------------------------------------------------------------------------
# consent gate
# --------------------------------------------------------------------------


def test_job_refuses_without_upload_acknowledgement(project, stubbed):
    _seed(project["id"], [("a.mp4", 1, "pending")])
    job_id = _make_job(project["id"], upload_acknowledged=False)
    semantic.vision_analyze_task(job_id)

    row = _job_row(job_id)
    assert row.status == "failed"
    assert "MODEL_UPLOAD_DENIED" in row.error
    assert stubbed.calls == [], "no image may leave without consent"
    with session_scope() as s:
        assert s.scalars(
            select(SegmentSemanticResult).where(
                SegmentSemanticResult.project_id == project["id"]
            )
        ).all() == []


def test_job_fails_when_mode_is_off(project, stubbed, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "model_mode", "off", raising=False)
    _seed(project["id"], [("a.mp4", 1, "pending")])
    job_id = _make_job(project["id"])
    semantic.vision_analyze_task(job_id)
    assert "MODEL_UPLOAD_DENIED" in _job_row(job_id).error
    assert stubbed.calls == []


def test_job_fails_when_model_not_configured(project, stubbed, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "model_api_key", "", raising=False)
    _seed(project["id"], [("a.mp4", 1, "pending")])
    job_id = _make_job(project["id"])
    semantic.vision_analyze_task(job_id)
    assert "MODEL_NOT_CONFIGURED" in _job_row(job_id).error


# --------------------------------------------------------------------------
# idempotency / re-run
# --------------------------------------------------------------------------


def test_rerun_skips_unchanged_shots(project, stubbed):
    _seed(project["id"], [("a.mp4", 1, "pending")])
    first = _make_job(project["id"])
    semantic.vision_analyze_task(first)
    assert _job_row(first).stats["analyzed"] == 1

    second = _make_job(project["id"])
    semantic.vision_analyze_task(second)
    row = _job_row(second)
    assert row.stats["analyzed"] == 0
    assert row.stats["skipped"] == 1
    assert len(stubbed.calls) == 1, "an identical run must not call the model again"

    with session_scope() as s:
        assert len(
            s.scalars(
                select(SegmentSemanticResult).where(
                    SegmentSemanticResult.project_id == project["id"]
                )
            ).all()
        ) == 1


def test_force_reruns_in_place_without_duplicating(project, stubbed):
    """A forced re-run of identical inputs replaces the row, not duplicates it.

    The version key is unique, so a second row is not an option; the contract is
    that the forced run really re-calls the model and leaves exactly one current
    row.
    """
    _seed(project["id"], [("a.mp4", 1, "pending")])
    first = _make_job(project["id"])
    semantic.vision_analyze_task(first)

    forced = _make_job(project["id"], force=True)
    semantic.vision_analyze_task(forced)
    row = _job_row(forced)
    assert row.status == "succeeded", row.error
    assert row.stats["analyzed"] == 1, "force must actually re-analyze"
    assert len(stubbed.calls) == 2, "the model is called again on force"

    with session_scope() as s:
        rows = s.scalars(
            select(SegmentSemanticResult).where(
                SegmentSemanticResult.project_id == project["id"]
            )
        ).all()
    assert len(rows) == 1
    assert rows[0].is_current is True


def test_history_is_kept_when_the_input_signature_changes(project, stubbed):
    """A different input signature is a different version and is retained."""
    seed = _seed(project["id"], [("a.mp4", 1, "pending")])
    seg_id = seed["segments"][0]["segment_id"]
    semantic.vision_analyze_task(_make_job(project["id"]))

    # Re-trim: new source signature, so the previous reading must survive.
    with session_scope() as s:
        seg = s.get(Segment, seg_id)
        seg.start_sec = 0.5
        seg.is_trimmed = True

    semantic.vision_analyze_task(_make_job(project["id"]))

    with session_scope() as s:
        rows = s.scalars(
            select(SegmentSemanticResult).where(
                SegmentSemanticResult.project_id == project["id"]
            )
        ).all()
    assert len(rows) == 2, "the earlier reading must remain for audit"
    assert sum(1 for r in rows if r.is_current) == 1
    assert sum(1 for r in rows if not r.is_current) == 1


def test_retrim_makes_the_result_stale_and_reanalyzed(project, stubbed):
    """Changing a shot's editable bounds must invalidate the cached reading."""
    seed = _seed(project["id"], [("a.mp4", 1, "pending")])
    seg_id = seed["segments"][0]["segment_id"]
    semantic.vision_analyze_task(_make_job(project["id"]))

    with session_scope() as s:
        seg = s.get(Segment, seg_id)
        seg.start_sec = 1.5  # human trim: detected_* stays untouched (C2)
        seg.is_trimmed = True

    again = _make_job(project["id"])
    semantic.vision_analyze_task(again)
    assert _job_row(again).stats["analyzed"] == 1, "a trimmed shot must be re-read"


# --------------------------------------------------------------------------
# failure reporting (C6)
# --------------------------------------------------------------------------


def test_provider_failure_yields_partial_failed(project, monkeypatch, stubbed):
    _seed(project["id"], [("a.mp4", 2, "pending")])

    call = {"n": 0}

    def behaviour(request, n):
        call["n"] = n
        if n == 1:
            raise ModelError(MODEL_NOT_CONFIGURED, "boom")
        return _StubProvider().complete(request)

    monkeypatch.setattr(stubbed, "behaviour", behaviour)
    job_id = _make_job(project["id"])
    semantic.vision_analyze_task(job_id)

    row = _job_row(job_id)
    assert row.status == "partial_failed", "one failure must not read as success"
    assert row.stats["analyzed"] == 1
    assert row.stats["failed"] == 1
    assert row.failures["count"] == 1
    assert row.failures["items"][0]["error_code"] == MODEL_NOT_CONFIGURED


def test_invalid_model_output_is_a_visible_failure(project, monkeypatch, stubbed):
    _seed(project["id"], [("a.mp4", 1, "pending")])
    monkeypatch.setattr(
        stubbed,
        "behaviour",
        lambda request, n: VisionResponse(text="I cannot help with that.", model_id="stub"),
    )
    job_id = _make_job(project["id"])
    semantic.vision_analyze_task(job_id)
    row = _job_row(job_id)
    assert row.status == "partial_failed"
    assert row.failures["items"][0]["error_code"] == "MODEL_INVALID_OUTPUT"


def test_budget_is_enforced(project, stubbed, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "model_max_calls_per_job", 1, raising=False)
    _seed(project["id"], [("a.mp4", 3, "pending")])
    job_id = _make_job(project["id"])
    semantic.vision_analyze_task(job_id)

    row = _job_row(job_id)
    assert row.stats["analyzed"] == 1
    assert row.status == "partial_failed"
    assert any(i.get("error_code") == "MODEL_BUDGET_EXCEEDED" for i in row.failures["items"])


def test_no_eligible_shots_is_reported_not_silently_skipped(project, stubbed):
    job_id = _make_job(project["id"])
    semantic.vision_analyze_task(job_id)
    row = _job_row(job_id)
    assert row.status == "failed"
    assert "INSUFFICIENT_FRAMES" in row.error


def test_cancel_stops_the_loop(project, stubbed):
    _seed(project["id"], [("a.mp4", 4, "pending")])
    job_id = _make_job(project["id"])
    with session_scope() as s:
        s.get(Job, job_id).status = "cancelling"

    from app.services.runner import JobCancelled

    with pytest.raises(JobCancelled):
        semantic.vision_analyze_task(job_id)
    assert stubbed.calls == []


# --------------------------------------------------------------------------
# human state is untouchable
# --------------------------------------------------------------------------


def test_job_never_modifies_human_fields(project, stubbed):
    """C4: the model must not be able to alter a person's marks."""
    seed = _seed(project["id"], [("a.mp4", 1, "favorite")])
    seg_id = seed["segments"][0]["segment_id"]
    with session_scope() as s:
        seg = s.get(Segment, seg_id)
        seg.story_role = "opening"
        seg.start_sec, seg.end_sec = 1.0, 4.0
        seg.is_trimmed = True

    semantic.vision_analyze_task(_make_job(project["id"]))

    with session_scope() as s:
        seg = s.get(Segment, seg_id)
    assert seg.user_rating == "favorite"
    assert seg.story_role == "opening"
    assert (seg.start_sec, seg.end_sec) == (1.0, 4.0)
    assert seg.is_trimmed is True


# --------------------------------------------------------------------------
# wiring (AGENTS.md §3.2)
# --------------------------------------------------------------------------


def test_m5_job_types_are_registered_and_valid():
    """A job type must be dispatchable, not merely present in a set.

    The M5 acceptance harness hit exactly this: importing the task modules does
    not register them, because registration is an explicit call in `app.main`.
    The runner then answered ``Unknown job type: vision_analyze`` — which reads
    like a product failure while actually being a wiring gap. Asserting
    membership in both the valid set *and* the task registry keeps the two in
    step.
    """
    from app.services.job_service import VALID_JOB_TYPES
    from app.services.runner import EXCLUSIVE_JOB_TYPES, TASK_REGISTRY

    for job_type in ("vision_analyze", "motion_review"):
        assert job_type in VALID_JOB_TYPES, f"{job_type} not accepted by the API"
        assert job_type in TASK_REGISTRY, f"{job_type} has no registered task function"
        assert job_type in EXCLUSIVE_JOB_TYPES, f"{job_type} must hold the project lock"


def test_vision_task_callable_is_registered_by_its_own_register_function():
    """`register_vision_tasks()` is what `app.main` calls; it must be idempotent."""
    from app.services.runner import TASK_REGISTRY

    before = TASK_REGISTRY.get("vision_analyze")
    semantic.register_vision_tasks()
    assert TASK_REGISTRY["vision_analyze"] is before
    assert TASK_REGISTRY["vision_analyze"] is semantic.vision_analyze_task


def test_semantic_token_budget_comes_from_config(project, stubbed):
    """Reasoning models need a ceiling that covers reasoning plus the JSON answer."""
    from app.config import get_settings

    settings = get_settings()
    assert settings.vision_max_tokens >= 2000

    _seed(project["id"], [("a.mp4", 1, "pending")])
    semantic.vision_analyze_task(_make_job(project["id"]))
    assert stubbed.calls, "provider should have been called"
    assert stubbed.calls[0].max_tokens == settings.vision_max_tokens
