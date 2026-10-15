"""TSB-307 — timestamped multi-frame motion review.

Two things are under test and they matter equally:

* the *sampling contract* — frames land inside the shot's valid interval, carry
  real timestamps, and never exceed the source duration;
* the *truthfulness contract* — the result is labelled ``multiframe`` (not video
  understanding), the sample count is recorded, an unsupported conclusion is
  forced to ``unknown``, and a human verdict survives a re-run.

**Layering (TSB-311).** This file deliberately mixes two tiers:

* the pure-function tests at the top (sampling maths, response validation, prompt
  wording) need no media toolchain and always run;
* every test that takes the ``source_video`` fixture is marked
  ``@pytest.mark.needs_ffmpeg`` and encodes real video, so it needs ffmpeg AND
  ffprobe.

The marker plus the fixture guard means a runner without FFmpeg reports a clean
skip instead of an ``Executable not found`` error, while the mandatory-media job
(``TSB_REQUIRE_FFMPEG=1``) turns the same absence into a hard failure. Adding a
new media-dependent case without the fixture cannot drift silently into a
tool-less runner — ``test_media_layering_is_explicit`` below enforces that.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select

from app.database import session_scope
from app.models import Asset, Job, Segment, SegmentMotionReview
from app.services.vision import motion
from app.services.vision.errors import ModelError
from app.services.vision.providers.base import VisionResponse


# --------------------------------------------------------------------------
# pure sampling maths
# --------------------------------------------------------------------------


def test_sample_times_are_inside_the_interval():
    times = motion.sample_times(10.0, 20.0, 5)
    assert len(times) == 5
    assert all(10.0 < t < 20.0 for t in times), "never sample exactly on a cut"


def test_sample_times_are_strictly_increasing():
    times = motion.sample_times(0.0, 12.0, 4)
    assert times == sorted(times)
    assert len(set(times)) == len(times)


def test_single_sample_is_the_midpoint():
    assert motion.sample_times(4.0, 8.0, 1) == [6.0]


def test_zero_length_interval_does_not_crash():
    assert motion.sample_times(5.0, 5.0, 3) == [5.0]


def test_sample_count_is_respected():
    assert len(motion.sample_times(0, 100, 8)) == 8


# --------------------------------------------------------------------------
# response validation
# --------------------------------------------------------------------------


def _payload(**over) -> dict:
    base = {
        "motion_type": "pan",
        "stability_risk": "medium",
        "action_summary": "the view rotates left across the lake",
        "evidence_timestamps_sec": [1.0, 2.0],
        "limitations": [],
        "needs_human_review": False,
    }
    base.update(over)
    return base


def test_valid_motion_payload_passes():
    out = motion.validate_motion(_payload(), [1.0, 2.0, 3.0])
    assert out["motion_type"] == "pan"
    assert out["stability_risk"] == "medium"
    assert out["evidence_timestamps_sec"] == [1.0, 2.0]


def test_off_vocabulary_motion_type_becomes_unknown():
    out = motion.validate_motion(_payload(motion_type="drone swooping dramatically"), [1.0])
    assert out["motion_type"] == "unknown"


def test_off_vocabulary_risk_becomes_unknown():
    out = motion.validate_motion(_payload(stability_risk="catastrophic"), [1.0])
    assert out["stability_risk"] == "unknown"


def test_invented_timestamps_are_dropped():
    """A timestamp the server never sent is an invented observation."""
    out = motion.validate_motion(_payload(evidence_timestamps_sec=[1.0, 99.0]), [1.0, 2.0])
    assert 99.0 not in out["evidence_timestamps_sec"]
    assert out["evidence_timestamps_sec"] == [1.0]


def test_all_invented_timestamps_fall_back_to_the_real_samples():
    out = motion.validate_motion(_payload(evidence_timestamps_sec=[50.0, 60.0]), [1.0, 2.0])
    assert out["evidence_timestamps_sec"] == [1.0, 2.0]


def test_timestamp_tolerance_is_small():
    """Nearby values are accepted (rounding), far ones are not."""
    out = motion.validate_motion(_payload(evidence_timestamps_sec=[1.02]), [1.0])
    assert out["evidence_timestamps_sec"] == [1.02]
    out2 = motion.validate_motion(_payload(evidence_timestamps_sec=[1.5]), [1.0])
    assert out2["evidence_timestamps_sec"] == [1.0]


def test_unknown_motion_always_requests_human_review():
    """An unresolved verdict must not be left looking settled."""
    out = motion.validate_motion(_payload(motion_type="unknown", needs_human_review=False), [1.0])
    assert out["needs_human_review"] is True
    assert "motion_type_unknown" in out["limitations"]


def test_summary_and_limitations_are_length_capped():
    out = motion.validate_motion(
        _payload(action_summary="x" * 5000, limitations=["y" * 900]), [1.0]
    )
    assert len(out["action_summary"]) == 2000
    assert len(out["limitations"][0]) == 300


def test_non_object_payload_is_rejected():
    with pytest.raises(ModelError):
        motion.validate_motion(["not", "an", "object"], [1.0])  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# JSON recovery
# --------------------------------------------------------------------------


def test_fenced_json_is_recovered():
    assert motion.extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}


def test_prose_wrapped_json_is_recovered():
    assert motion.extract_json_object('Here you go: {"a": 1} done') == {"a": 1}


@pytest.mark.parametrize("raw", ["", "   ", "no json at all"])
def test_unparseable_response_raises(raw):
    with pytest.raises(ModelError):
        motion.extract_json_object(raw)


# --------------------------------------------------------------------------
# prompt honesty
# --------------------------------------------------------------------------


def test_prompt_states_it_is_not_video():
    prompt = motion.build_prompt(
        [{"label": "t=1.00s", "segment_id": "s1"}], frame_count=1, audio_analyzed=False
    )
    assert "STILL FRAMES" in prompt
    assert "not a video" in prompt
    assert "cannot know what happened between two frames" in prompt


def test_prompt_forbids_audio_claims():
    prompt = motion.build_prompt([{"label": "t=1.00s", "segment_id": "s1"}], frame_count=1, audio_analyzed=False)
    assert "cannot hear" in prompt
    assert "Do not comment on sound" in prompt


def test_prompt_prefers_unknown_over_guessing():
    prompt = motion.build_prompt([{"label": "t=1.00s", "segment_id": "s1"}], frame_count=1, audio_analyzed=False)
    assert '"unknown"' in prompt
    assert "worse than unknown" in prompt


def test_prompt_lists_every_timestamp():
    prompt = motion.build_prompt(
        [{"label": f"t={i}.00s", "segment_id": "s1"} for i in range(3)],
        frame_count=3,
        audio_analyzed=False,
    )
    for i in range(3):
        assert f"t={i}.00s" in prompt


# --------------------------------------------------------------------------
# job integration
# --------------------------------------------------------------------------


@pytest.fixture()
def project(client):
    res = client.post("/api/projects", json={"name": "motion-job-test", "source_roots": []})
    assert res.status_code in (200, 201), res.text
    return res.json()


class _StubProvider:
    name = "stub"

    def __init__(self, behaviour=None):
        self.calls: list = []
        self.behaviour = behaviour

    def complete(self, request, *, cancel_check=None):
        self.calls.append(request)
        if self.behaviour is not None:
            return self.behaviour(request, len(self.calls))
        times = [float(m.group(1)) for m in __import__("re").finditer(r"t=([\d.]+)s", request.prompt)]
        return VisionResponse(
            text=json.dumps(
                {
                    "motion_type": "pan",
                    "stability_risk": "low",
                    "action_summary": "view moves across the scene",
                    "evidence_timestamps_sec": times,
                    "limitations": [],
                    "needs_human_review": False,
                }
            ),
            model_id="stub-model",
            usage={"total_tokens": 42},
        )


@pytest.fixture()
def stubbed(monkeypatch):
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "model_mode", "on", raising=False)
    monkeypatch.setattr(s, "model_base_url", "http://127.0.0.1:9/v1", raising=False)
    monkeypatch.setattr(s, "model_api_key", "test-key-abcdefgh", raising=False)
    monkeypatch.setattr(s, "model_name", "stub-model", raising=False)
    monkeypatch.setattr(s, "vision_target_width", 160, raising=False)

    provider = _StubProvider()
    import app.services.vision as vp

    monkeypatch.setattr(motion, "get_provider", lambda: provider, raising=False)
    monkeypatch.setattr(vp, "get_provider", lambda: provider, raising=False)
    return provider


@pytest.fixture()
def source_video(tmp_path):
    """A tiny real video so frame extraction genuinely runs.

    TSB-311: this fixture encodes with FFmpeg, so the regular ``backend`` CI job
    (which historically had no FFmpeg) must get a clean *skip* rather than an
    ``Executable not found`` ERROR. Under ``TSB_REQUIRE_FFMPEG=1`` — the
    mandatory-media job — the same absence is a hard failure, so the toolchain
    can never go missing unnoticed.
    """
    from app.ffmpeg import run

    # `tests/` is not a package (no __init__.py), so this is an absolute import
    # resolved via pytest's rootdir insertion — the same style the other suites
    # use for cross-file helpers.
    from _media import require_ffmpeg

    exe = require_ffmpeg()
    path = tmp_path / "clip.mp4"
    run([
        exe, "-y", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10:duration=6",
        "-pix_fmt", "yuv420p", str(path),
    ], expect_output=path)
    return path


def _seed(project_id: str, source: Path, *, start=1.0, end=5.0, duration=6.0) -> str:
    asset_id, seg_id = str(uuid.uuid4()), str(uuid.uuid4())
    with session_scope() as s:
        s.add(
            Asset(
                id=asset_id,
                project_id=project_id,
                source_path=str(source),
                display_name=source.name,
                file_size_bytes=1,
                duration_sec=duration,
                has_audio=False,
                fps_num=10,
                fps_den=1,
                scan_status="succeeded",
            )
        )
        s.flush()
        s.add(
            Segment(
                id=seg_id,
                project_id=project_id,
                asset_id=asset_id,
                start_sec=start,
                end_sec=end,
                detected_start_sec=0.0,
                detected_end_sec=6.0,
                status="ready",
            )
        )
    return seg_id


def _job(project_id: str, **config) -> str:
    job_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(
            Job(
                id=job_id,
                project_id=project_id,
                job_type="motion_review",
                status="pending",
                config_snapshot={"upload_acknowledged": True, **config},
                stats={},
                failures={"count": 0, "items": []},
            )
        )
    return job_id


def _row(job_id: str) -> Job:
    with session_scope() as s:
        return s.get(Job, job_id)


@pytest.mark.needs_ffmpeg
def test_job_samples_multiple_frames_and_stores_a_multiframe_result(project, stubbed, source_video):
    seg_id = _seed(project["id"], source_video)
    job_id = _job(project["id"], frame_count=3)
    motion.motion_review_task(job_id)

    row = _row(job_id)
    assert row.status == "succeeded", row.error
    assert row.stats["method"] == "multiframe"
    assert row.stats["frames_sampled"] == 3

    with session_scope() as s:
        stored = s.scalars(
            select(SegmentMotionReview).where(SegmentMotionReview.segment_id == seg_id)
        ).one()
    assert stored.method == "multiframe"
    assert stored.motion_type == "pan"
    # The evidence must record the limits of the method.
    assert stored.evidence_json["audio_analyzed"] is False
    assert stored.evidence_json["video_analyzed"] is False
    assert len(stored.evidence_json["frames"]) == 3


@pytest.mark.needs_ffmpeg
def test_sampled_timestamps_lie_inside_the_valid_interval(project, stubbed, source_video):
    seg_id = _seed(project["id"], source_video, start=1.0, end=5.0)
    motion.motion_review_task(_job(project["id"], frame_count=4))

    with session_scope() as s:
        stored = s.scalars(
            select(SegmentMotionReview).where(SegmentMotionReview.segment_id == seg_id)
        ).one()
    times = [f["time_sec"] for f in stored.evidence_json["frames"]]
    assert len(times) == 4
    assert all(1.0 < t < 5.0 for t in times), f"outside the editable interval: {times}"


@pytest.mark.needs_ffmpeg
def test_frames_are_removed_after_the_job(project, stubbed, source_video):
    _seed(project["id"], source_video)
    job_id = _job(project["id"], frame_count=3)
    motion.motion_review_task(job_id)
    assert not motion.stage_dir(project["id"], job_id).exists()


@pytest.mark.needs_ffmpeg
def test_uploaded_frames_never_carry_the_source_path(project, stubbed, source_video):
    _seed(project["id"], source_video)
    motion.motion_review_task(_job(project["id"], frame_count=3))
    assert stubbed.calls, "provider should have been called"
    request = stubbed.calls[0]
    assert str(source_video) not in request.prompt
    # Every image must be labelled with a timestamp, which is the evidence anchor.
    assert all(img.label.startswith("t=") for img in request.images)


@pytest.mark.needs_ffmpeg
def test_audio_is_never_claimed_to_be_analyzed(project, stubbed, source_video):
    _seed(project["id"], source_video)
    motion.motion_review_task(_job(project["id"], frame_count=2))
    prompt = stubbed.calls[0].prompt
    assert "audio content NOT analysed" in prompt
    assert "audio_present=" in prompt


@pytest.mark.needs_ffmpeg
def test_short_interval_that_cannot_yield_two_frames_is_a_visible_failure(project, stubbed, source_video):
    """Too-short shots must be reported, not silently counted as success."""
    seg_id = _seed(project["id"], source_video, start=2.0, end=2.0, duration=6.0)
    job_id = _job(project["id"], frame_count=3)
    motion.motion_review_task(job_id)

    row = _row(job_id)
    assert row.status == "partial_failed"
    assert row.failures["count"] == 1
    assert row.failures["items"][0]["error_code"] in ("INSUFFICIENT_FRAMES", "OUTPUT_NOT_CREATED")
    with session_scope() as s:
        assert s.scalars(
            select(SegmentMotionReview).where(SegmentMotionReview.segment_id == seg_id)
        ).all() == []


@pytest.mark.needs_ffmpeg
def test_source_longer_than_reported_duration_cannot_seek_past_the_end(project, stubbed, source_video):
    """A stale duration must not make the sampler seek beyond the file."""
    _seed(project["id"], source_video, start=5.0, end=999.0, duration=6.0)
    job_id = _job(project["id"], frame_count=3)
    motion.motion_review_task(job_id)
    # Either it samples inside 5-6s or it reports a failure; it must never hang
    # or claim frames it could not read.
    row = _row(job_id)
    assert row.status in ("succeeded", "partial_failed")


@pytest.mark.needs_ffmpeg
def test_consent_gate_blocks_extraction(project, stubbed, source_video):
    _seed(project["id"], source_video)
    job_id = _job(project["id"], upload_acknowledged=False, frame_count=3)
    motion.motion_review_task(job_id)
    row = _row(job_id)
    assert row.status == "failed"
    assert "MODEL_UPLOAD_DENIED" in row.error
    assert stubbed.calls == []


@pytest.mark.needs_ffmpeg
def test_model_failure_is_recorded_per_item(project, stubbed, source_video, monkeypatch):
    _seed(project["id"], source_video)
    monkeypatch.setattr(
        stubbed, "behaviour",
        lambda request, n: (_ for _ in ()).throw(ModelError("MODEL_TIMEOUT", "slow")),
    )
    job_id = _job(project["id"], frame_count=3)
    motion.motion_review_task(job_id)
    row = _row(job_id)
    assert row.status == "partial_failed"
    assert row.failures["items"][0]["error_code"] == "MODEL_TIMEOUT"


@pytest.mark.needs_ffmpeg
def test_invalid_model_output_is_a_visible_failure(project, stubbed, source_video, monkeypatch):
    _seed(project["id"], source_video)
    monkeypatch.setattr(
        stubbed, "behaviour",
        lambda request, n: VisionResponse(text="I cannot judge this.", model_id="stub"),
    )
    job_id = _job(project["id"], frame_count=2)
    motion.motion_review_task(job_id)
    assert _row(job_id).status == "partial_failed"


@pytest.mark.needs_ffmpeg
def test_token_budget_comes_from_config_and_is_not_a_small_literal(project, stubbed, source_video):
    """Reasoning models need headroom before the JSON answer.

    Seen on the live endpoint: with a 1200-token ceiling the model spent the
    whole budget on its separate `reasoning` field and returned `content: null`,
    which the adapter correctly reported as MODEL_INVALID_OUTPUT. The ceiling is
    therefore configurable, and it must be generous enough to cover reasoning
    *and* the answer.
    """
    from app.config import get_settings

    settings = get_settings()
    assert settings.vision_max_tokens_motion >= 2000
    assert settings.vision_max_tokens >= 2000

    _seed(project["id"], source_video)
    motion.motion_review_task(_job(project["id"], frame_count=2))
    assert stubbed.calls, "provider should have been called"
    assert stubbed.calls[0].max_tokens == settings.vision_max_tokens_motion


@pytest.mark.needs_ffmpeg
def test_configured_token_ceiling_is_honoured(project, stubbed, source_video, monkeypatch):
    """Wiring check: the task must pass the *configured* value, not a literal."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "vision_max_tokens_motion", 6000, raising=False)
    _seed(project["id"], source_video)
    motion.motion_review_task(_job(project["id"], frame_count=2))
    assert stubbed.calls[0].max_tokens == 6000


@pytest.mark.needs_ffmpeg
def test_unknown_verdict_is_recorded_as_needing_human_review(project, stubbed, source_video, monkeypatch):
    seg_id = _seed(project["id"], source_video)
    monkeypatch.setattr(
        stubbed, "behaviour",
        lambda request, n: VisionResponse(
            text=json.dumps(
                {
                    "motion_type": "unknown",
                    "stability_risk": "unknown",
                    "action_summary": "cannot tell",
                    "evidence_timestamps_sec": [],
                    "limitations": [],
                    "needs_human_review": False,
                }
            ),
            model_id="stub",
        ),
    )
    motion.motion_review_task(_job(project["id"], frame_count=2))
    with session_scope() as s:
        stored = s.scalars(
            select(SegmentMotionReview).where(SegmentMotionReview.segment_id == seg_id)
        ).one()
    assert stored.motion_type == "unknown"
    assert stored.evidence_json["needs_human_review"] is True


# --------------------------------------------------------------------------
# human verdict (C4)
# --------------------------------------------------------------------------


@pytest.mark.needs_ffmpeg
def test_human_verdict_is_stored_and_survives_a_rerun(project, stubbed, source_video):
    """The model may be re-run; a person's judgement must not be erased."""
    seg_id = _seed(project["id"], source_video)
    motion.motion_review_task(_job(project["id"], frame_count=2))
    assert motion.record_human_verdict(seg_id, "confirm", "checked against the original")

    # Re-run the model over the same shot.
    motion.motion_review_task(_job(project["id"], frame_count=2))

    with session_scope() as s:
        rows = s.scalars(
            select(SegmentMotionReview).where(SegmentMotionReview.segment_id == seg_id)
        ).all()
        current = [r for r in rows if r.is_current]
        historical = [r for r in rows if not r.is_current]
    assert len(current) == 1
    # The verdict was written on the earlier row; it must still exist somewhere
    # and must never have been fabricated onto the new one.
    assert any(r.human_verdict == "confirm" for r in rows)
    assert current[0].human_verdict != "confirm" or historical


@pytest.mark.needs_ffmpeg
def test_invalid_human_verdict_is_rejected(project, stubbed, source_video):
    seg_id = _seed(project["id"], source_video)
    motion.motion_review_task(_job(project["id"], frame_count=2))
    with pytest.raises(ValueError):
        motion.record_human_verdict(seg_id, "definitely-a-shake")


def test_human_verdict_on_unknown_segment_returns_false(project):
    assert motion.record_human_verdict(str(uuid.uuid4()), "confirm") is False


# --------------------------------------------------------------------------
# layering guard (TSB-311)
# --------------------------------------------------------------------------


def test_media_layering_is_explicit():
    """Every source_video user must be marked; every marked test must use it.

    This is the mechanism that stops the TSB-311 regression from coming back:
    a future case that encodes media without the marker would land in the
    tool-less regular CI job and error there instead of skipping. Asserting both
    directions also catches a stale marker left behind after a test is rewritten
    to use a synthetic fixture.
    """
    import inspect

    module = inspect.getmodule(test_media_layering_is_explicit)
    source = inspect.getsource(module)
    assert "@pytest.mark.needs_ffmpeg" in source

    unmarked: list[str] = []
    overmarked: list[str] = []
    pending_marker = False
    for line in source.splitlines():
        stripped = line.strip()
        if stripped == "@pytest.mark.needs_ffmpeg":
            pending_marker = True
            continue
        if stripped.startswith("def test_"):
            name = stripped[4:].split("(")[0]
            uses_fixture = "source_video" in stripped
            if uses_fixture and not pending_marker:
                unmarked.append(name)
            if pending_marker and not uses_fixture:
                overmarked.append(name)
            pending_marker = False
        elif stripped.startswith("def ") or stripped.startswith("@"):
            # Any other decorator/def resets the association.
            if not stripped.startswith("@pytest.mark"):
                pending_marker = False

    assert not unmarked, f"media-dependent tests missing @pytest.mark.needs_ffmpeg: {unmarked}"
    assert not overmarked, f"marked tests that do not use source_video: {overmarked}"
