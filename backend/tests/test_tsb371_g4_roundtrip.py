"""TSB-371-0 — the G4 response path, tested end to end without a network.

TSB-370 lost two real, paid-for attempts because the local code after the response
was wrong (a ``NameError``) or the budget was misjudged. The lesson is not "be more
careful"; it is that the *post-response* path had no test that ran it for real.

So every test here feeds :class:`ExternalPlannerProvider` a provider whose
``complete()`` returns **text shaped like an actual model reply** — not a
pre-parsed object — and drives it all the way to a validated ``PlanDraft``. The
injections below are the specific failures that cost TSB-370 its budget.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from app.database import session_scope
from app.models import Asset, Project, Segment, SegmentSemanticResult
from app.services import director_plan_input as dpi
from app.services import director_plan_validation as val
from app.services.director_plan_model import ModelPlanError, build_evidence_text
from app.services.director_plan_providers import ExternalPlannerProvider
from app.services.director_plan_schema import PlanningBrief
from app.services.vision.authorization import authorize_upload


def _uid() -> str:
    return str(uuid.uuid4())


def _seed() -> tuple[str, dict]:
    with session_scope() as s:
        p = Project(id=_uid(), name="t371g4", source_roots=[], output_dir="", settings={})
        s.add(p)
        s.flush()
        a = Asset(
            id=_uid(), project_id=p.id, source_path=f"/nonexistent/{_uid()}.mp4",
            display_name="c.mp4", file_size_bytes=1, mtime_ns=1, quick_fingerprint="f",
            duration_sec=600.0, fps_num=30, fps_den=1, vfr_flag=False, width=640,
            height=360, rotation=0, codec="h264", pixel_format="yuv420p",
            color_primaries="", color_transfer="", color_space="", has_audio=False,
            audio_codec="", scan_status="succeeded", analysis_version="",
            analyzed_fingerprint="", analyzed_source_signature="",
            analyzed_detect_config="", analyzed_at=None, scanned_at=None,
        )
        s.add(a)
        s.flush()
        ids = {"segments": [], "results": []}
        for i in range(3):
            g = Segment(
                id=_uid(), asset_id=a.id, project_id=p.id,
                detected_start_sec=i * 10.0, detected_end_sec=i * 10.0 + 6,
                start_sec=i * 10.0, end_sec=i * 10.0 + 6, is_trimmed=False,
                boundary_reason="", quality_metrics={"blur_avg": 100.0},
                content_tags={}, story_role="", user_rating="pending",
                status="candidate", frame_refs=[], proxy_ref="", parent_segment_id="",
                algorithm_version="1", created_at=None,
            )
            s.add(g)
            s.flush()
            r = SegmentSemanticResult(
                id=_uid(), project_id=p.id, asset_id=a.id, segment_id=g.id,
                status="succeeded", model_provider="openai_compatible",
                model_id="GLM-5.3-Flash", prompt_version="p", schema_version="1.0",
                source_signature=_uid(), frame_manifest_hash="h",
                analysis_json={"scene_tags": ["lake", "boats"], "subjects": ["a pier"],
                               "visible_actions": ["people walking"],
                               "lighting": "daylight", "composition": "wide",
                               "mood": ["calm"]},
                evidence_json={"state": "observed", "frame_ids": ["f1"]},
                request_usage={}, is_current=True, error_code="", error_message="",
                created_at=None, updated_at=None,
            )
            s.add(r)
            s.flush()
            ids["segments"].append(g.id)
            ids["results"].append(r.id)
        pid = p.id
    return pid, ids


class _FakeResponse:
    def __init__(self, text):
        self.text = text


class _TextProvider:
    """A provider whose *only* job is to hand back a string, like a real one."""

    def __init__(self, text):
        self._text = text
        self.calls = []

    def complete(self, request, *, cancel_check=None):
        self.calls.append({
            "text_only": getattr(request, "text_only", None),
            "images": len(request.images),
            "has_auth": request.authorization is not None,
            "prompt_chars": len(request.prompt),
        })
        if isinstance(self._text, Exception):
            raise self._text
        return _FakeResponse(self._text)


@pytest.fixture(autouse=True)
def _model_gate_open(monkeypatch):
    """Open the global gate for these tests, with a placeholder endpoint.

    CI runs with ``TRAVELSB_MODEL_MODE=off``; a test that needs to get past the
    gate has to say so. No network is used: the provider is always a stub here.
    """
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "model_mode", "on", raising=False)
    monkeypatch.setattr(settings, "model_base_url", "https://example.invalid/v1",
                        raising=False)
    monkeypatch.setattr(settings, "model_api_key", "test-key-not-real", raising=False)
    return settings


def _ctx(pid: str):
    return authorize_upload(project_id=pid, upload_acknowledged=True,
                            purpose="director_plan")


def _valid_reply(ids: dict, *, n_shots: int = 2) -> str:
    shots = []
    for i in range(n_shots):
        shots.append({
            "segment_id": ids["segments"][i],
            "source_in_sec": i * 10.0,
            "source_out_sec": i * 10.0 + 3.0,
            "section_id": "s1",
            "narrative_role": "opening" if i == 0 else "development",
            "selection_reason": "有静帧观察记录",
            "claims": [{
                "text": "静帧观察：场景标签 lake,boats",
                "kind": "observed_signal",
                "evidence_refs": [{"channel": "static", "result_id": ids["results"][i]}],
            }],
            "evidence_refs": [{"channel": "static", "result_id": ids["results"][i]}],
        })
    return json.dumps({
        "variants": [{
            "title": "模型方案", "narrative_angle": "chronological",
            "logline": "按证据顺序串联",
            "sections": [{"section_id": "s1", "kind": "body", "title": "主体"}],
            "shots": shots,
        }]
    }, ensure_ascii=False)


def _run(pid: str, text, *, max_candidates: int = 12):
    pi = dpi.build_planning_input(pid, max_candidates=max_candidates)
    provider = _TextProvider(text)
    planner = ExternalPlannerProvider(authorized=_ctx(pid), provider=provider)
    brief = PlanningBrief(theme="从出发到抵达", target_duration_sec=60, variant_count=1)
    return planner.plan(pi, brief), pi, provider


# ---------------------------------------------------------------------------
# The happy path, all the way to a validated draft
# ---------------------------------------------------------------------------

def test_a_real_shaped_reply_becomes_a_validated_plan():
    pid, ids = _seed()
    plan, pi, provider = _run(pid, _valid_reply(ids))

    assert provider.calls[0]["text_only"] is True
    assert provider.calls[0]["images"] == 0, "no image may be sent"
    assert provider.calls[0]["has_auth"] is True

    assert plan.planning_mode == "model"
    assert plan.source_input_digest == pi.source_input_digest
    assert len(plan.variants) == 1
    assert sum(len(v.shots) for v in plan.variants) == 2
    assert val.validate_plan(plan, pi) == []


def test_the_prompt_the_provider_receives_is_the_whitelist():
    pid, ids = _seed()
    plan, pi, provider = _run(pid, _valid_reply(ids))
    assert provider.calls[0]["prompt_chars"] > 0
    assert plan.request_summary["sends_images"] is False
    assert plan.request_summary["sends_user_notes"] is False
    assert plan.request_summary["sends_paths"] is False


def test_the_request_summary_is_populated_without_recomputing_the_prompt():
    """TSB-370 crashed here with a NameError after the paid request."""
    pid, ids = _seed()
    plan, pi, _ = _run(pid, _valid_reply(ids))
    assert plan.request_summary["shots_listed"] == len(pi.entries)
    assert plan.request_summary["chars"] > 0


# ---------------------------------------------------------------------------
# Injections that must fail cleanly (never a silent fake success)
# ---------------------------------------------------------------------------

def test_a_null_response_text_is_an_invalid_output_failure():
    """A reasoning model that spends its budget returns ``content=null``."""
    pid, ids = _seed()
    with pytest.raises(ModelPlanError) as exc:
        _run(pid, None)
    assert exc.value.code == "MODEL_INVALID_OUTPUT"


def test_an_empty_response_text_is_an_invalid_output_failure():
    pid, ids = _seed()
    with pytest.raises(ModelPlanError) as exc:
        _run(pid, "   ")
    assert exc.value.code == "MODEL_INVALID_OUTPUT"


def test_non_json_prose_is_an_invalid_output_failure():
    pid, ids = _seed()
    with pytest.raises(ModelPlanError) as exc:
        _run(pid, "I am sorry, but I cannot help with that request.")
    assert exc.value.code == "MODEL_INVALID_OUTPUT"


def test_a_truncated_json_body_is_refused():
    pid, ids = _seed()
    good = _valid_reply(ids)
    with pytest.raises(ModelPlanError) as exc:
        _run(pid, good[: len(good) // 2])
    assert exc.value.code == "MODEL_INVALID_OUTPUT"


def test_an_empty_variants_array_is_refused():
    pid, ids = _seed()
    with pytest.raises(ModelPlanError) as exc:
        _run(pid, json.dumps({"variants": []}))
    assert exc.value.code == "MODEL_INVALID_OUTPUT"


def test_a_structurally_valid_but_fabricated_plan_is_rejected_by_validation():
    pid, ids = _seed()
    reply = json.loads(_valid_reply(ids))
    reply["variants"][0]["shots"][0]["segment_id"] = str(uuid.uuid4())
    plan, pi, _ = _run(pid, json.dumps(reply))
    codes = [p.code for p in val.validate_plan(plan, pi)]
    assert "UNKNOWN_SEGMENT" in codes


def test_a_reply_claiming_audio_is_rejected():
    pid, ids = _seed()
    reply = json.loads(_valid_reply(ids))
    reply["variants"][0]["shots"][0]["claims"][0]["text"] = "音乐节拍在 1.2 秒处重击"
    plan, pi, _ = _run(pid, json.dumps(reply))
    codes = [p.code for p in val.validate_plan(plan, pi)]
    assert "AUDIO_CLAIM_WITHOUT_ANALYSIS" in codes


def test_a_wrong_evidence_id_leaves_the_claim_unsupported():
    pid, ids = _seed()
    reply = json.loads(_valid_reply(ids))
    # Shot 0 cites shot 1's evidence id: not its own.
    reply["variants"][0]["shots"][0]["claims"][0]["evidence_refs"] = [
        {"channel": "static", "result_id": ids["results"][1]}]
    plan, pi, _ = _run(pid, json.dumps(reply))
    codes = [p.code for p in val.validate_plan(plan, pi)]
    assert "UNSUPPORTED_VISUAL_CLAIM" in codes


def test_identical_variants_are_not_two_options():
    pid, ids = _seed()
    one = json.loads(_valid_reply(ids))["variants"][0]
    reply = {"variants": [one, dict(one, title="换了个名字")]}
    plan, pi, _ = _run(pid, json.dumps(reply))
    codes = [p.code for p in val.validate_plan(plan, pi)]
    assert "VARIANTS_NOT_DISTINCT" in codes


def test_a_provider_transport_error_keeps_its_own_code():
    from app.services.vision.errors import ModelError

    pid, ids = _seed()
    with pytest.raises(ModelError) as exc:
        _run(pid, ModelError("MODEL_TIMEOUT", "timed out"))
    assert exc.value.code == "MODEL_TIMEOUT"


def test_the_provider_is_called_exactly_once_per_plan():
    """No retry loop here: the ledger counts attempts, and one plan is one attempt."""
    pid, ids = _seed()
    _, _, provider = _run(pid, _valid_reply(ids))
    assert len(provider.calls) == 1


def test_a_failed_plan_never_falls_back_to_the_offline_result():
    """The TSB-370 hazard: a failed model call must not be reported as a success."""
    pid, ids = _seed()
    with pytest.raises(ModelPlanError):
        _run(pid, "not json at all")
    # Nothing was written and no plan object escaped.
    pi = dpi.build_planning_input(pid, max_candidates=12)
    assert pi.entries  # input still intact


def test_without_authorization_no_provider_call_happens():
    from app.services.director_plan_providers import ModelCallNotAuthorized

    pid, ids = _seed()
    pi = dpi.build_planning_input(pid, max_candidates=12)
    provider = _TextProvider(_valid_reply(ids))
    planner = ExternalPlannerProvider(authorized=None, provider=provider)
    with pytest.raises(ModelCallNotAuthorized):
        planner.plan(pi, PlanningBrief(theme="t", target_duration_sec=60, variant_count=1))
    assert provider.calls == []
