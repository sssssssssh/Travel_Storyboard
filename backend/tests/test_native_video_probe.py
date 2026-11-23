"""TSB-325 — probe outcome classification and the dry-run/consent gates.

The probe decides whether a video channel may ever carry a user's footage, so its
*classification* logic is as safety-critical as the transport. The failure this
file exists to prevent is a specific, plausible mistake: reading "the request
failed" as "the endpoint evaluated our video". A dead gateway that 503s
everything looks identical to a video rejection unless a control request is made,
and the two conclusions have opposite consequences.

Every case below is expressed as an observation, not a live call: the real network
result is recorded in ``docs/validation/v2.1.3/CAPABILITY_REPORT.json``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from app.services.vision import video_capabilities as vc
from tools.probe_video_capability import (
    ProbeOutcome,
    build_report,
    classify_probe_outcome,
    clip_source_is_real,
    plan_probe,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
TOOL = BACKEND_ROOT / "tools" / "probe_video_capability.py"


def _outcome(**kwargs) -> ProbeOutcome:
    base = {"attempted": True, "http_status": 200}
    base.update(kwargs)
    return ProbeOutcome(**base)


# --- the four states, from observations -------------------------------------

def test_not_attempted_stays_unverified():
    state, _scope = classify_probe_outcome(ProbeOutcome(attempted=False))
    assert state == vc.UNVERIFIED


def test_a_2xx_without_temporal_content_is_sampling_unknown_not_verified():
    """V04: the headline rule — accepted is not the same as understood."""
    state, scope = classify_probe_outcome(_outcome(http_status=200, temporal_signal=False))
    assert state == vc.VIDEO_ACCEPTED_SAMPLING_UNKNOWN
    assert state != vc.VERIFIED_NATIVE_VIDEO
    assert "no usable temporal information" in scope


def test_a_2xx_with_temporal_content_can_be_verified():
    state, scope = classify_probe_outcome(
        _outcome(http_status=200, temporal_signal=True, temporal_signal_basis="mentions motion")
    )
    assert state == vc.VERIFIED_NATIVE_VIDEO
    assert "mentions motion" in scope


@pytest.mark.parametrize(("status", "message"), [
    (400, "invalid JSON"),
    (400, "this field does not support negative lengths"),
    (400, "At most 1 video(s) may be provided in one prompt. Set --limit-mm-per-prompt."),
    (400, "Unsupported chat content part type: 'image_url'."),
    (422, "invalid content schema"),
])
def test_tsb372r2_fix01_generic_request_errors_are_unverified(status, message):
    state, scope = classify_probe_outcome(
        _outcome(http_status=status, control_http_status=200, response_excerpt=message)
    )
    assert state == vc.UNVERIFIED
    assert f"HTTP {status}" in scope
    assert "video=1" not in scope


def test_a_413_asks_to_retry_with_a_smaller_payload():
    state, scope = classify_probe_outcome(_outcome(http_status=413, control_http_status=200))
    assert state == vc.UNVERIFIED
    assert "413" in scope and "smaller" in scope


@pytest.mark.parametrize("status", [401, 403])
def test_an_auth_failure_is_not_a_capability_verdict(status):
    state, _ = classify_probe_outcome(_outcome(http_status=status))
    assert state == vc.UNVERIFIED


@pytest.mark.parametrize("code", ["MODEL_TIMEOUT", "MODEL_NETWORK_ERROR"])
def test_a_transport_failure_is_inconclusive(code):
    state, scope = classify_probe_outcome(_outcome(http_status=None, error_code=code))
    assert state == vc.UNVERIFIED
    assert code in scope


@pytest.mark.parametrize("status", [404, 429, 500, 502, 503])
def test_tsb372r2_fix01_http_failures_preserve_the_status(status):
    state, scope = classify_probe_outcome(_outcome(http_status=status, control_http_status=200))
    assert state == vc.UNVERIFIED
    assert f"HTTP {status}" in scope


# --- the control request is what makes an outage readable -------------------

def test_a_dead_endpoint_cannot_be_reported_as_unsupported():
    """The real case observed on 2026-10-16: video 503, text-only control 503."""
    state, scope = classify_probe_outcome(
        _outcome(http_status=503, control_http_status=503, response_excerpt="no healthy upstream")
    )
    assert state == vc.UNVERIFIED, (
        "an unhealthy gateway tells us nothing about video support; calling it "
        "unsupported would be a fabricated negative"
    )
    assert "endpoint unhealthy" in scope
    assert "inconclusive" in scope


def test_a_control_500_also_poisons_the_video_verdict():
    state, _ = classify_probe_outcome(_outcome(http_status=415, control_http_status=500))
    assert state == vc.UNVERIFIED, (
        "even a video-shaped 415 means nothing while the endpoint is failing everything"
    )


@pytest.mark.parametrize("control_status", [200, 204])
def test_a_healthy_control_lets_a_video_rejection_stand(control_status):
    state, _ = classify_probe_outcome(_outcome(http_status=415, control_http_status=control_status))
    assert state == vc.UNSUPPORTED


# --- the rejection body must produce an actionable reason -------------------

#: The exact body this endpoint returned on 2026-10-16 (vLLM, video limit 0).
_VLLM_ZERO_VIDEO_LIMIT = (
    '{"error":{"message":"At most 0 video(s) may be provided in one prompt. '
    'Set `--limit-mm-per-prompt` to increase this limit. (parameter=video)",'
    '"type":"BadRequestError","param":"video","code":400}}'
)


@pytest.mark.parametrize("body", [_VLLM_ZERO_VIDEO_LIMIT, "--limit-mm-per-prompt video=0"])
def test_a_zero_video_limit_names_the_serving_flag(body):
    """'HTTP 400' alone sends the operator looking in the wrong place.

    The remedy here is a server-side serving flag, not a client-side change, so
    the reason must say so.
    """
    state, scope = classify_probe_outcome(
        _outcome(http_status=400, control_http_status=200, response_excerpt=body)
    )
    assert state == vc.UNSUPPORTED
    assert "--limit-mm-per-prompt video=1" in scope
    assert "server-side" in scope, "the fix is server-side; the reason must not imply otherwise"


def test_an_unrecognised_content_part_is_described_as_such():
    state, scope = classify_probe_outcome(
        _outcome(
            http_status=400,
            control_http_status=200,
            response_excerpt="Unsupported chat content part type: 'video'.",
        )
    )
    assert state == vc.UNSUPPORTED
    assert "does not recognise" in scope


def test_an_unparsed_rejection_still_reports_the_status():
    """No guessing at a cause we cannot read."""
    state, scope = classify_probe_outcome(
        _outcome(http_status=400, control_http_status=200, response_excerpt="something odd")
    )
    assert state == vc.UNVERIFIED
    assert "HTTP 400" in scope
    assert "limit-mm-per-prompt" not in scope


def test_a_healthy_control_lets_a_verification_stand():
    state, _ = classify_probe_outcome(
        _outcome(http_status=200, control_http_status=200, temporal_signal=True)
    )
    assert state == vc.VERIFIED_NATIVE_VIDEO


@pytest.mark.parametrize("control_status", [301, 401, 403, 404, 429, 503])
def test_tsb372r2_fix01_unhealthy_control_blocks_a_video_rejection(control_status):
    state, scope = classify_probe_outcome(_outcome(http_status=415, control_http_status=control_status))
    assert state == vc.UNVERIFIED
    assert "HTTP 415" in scope and f"HTTP {control_status}" in scope


def test_tsb372r2_fix01_missing_control_blocks_a_video_rejection():
    state, scope = classify_probe_outcome(_outcome(http_status=415))
    assert state == vc.UNVERIFIED
    assert "text-only control" in scope


@pytest.mark.parametrize(("status", "code", "body", "expected"), [
    (400, "", _VLLM_ZERO_VIDEO_LIMIT, vc.UNSUPPORTED),
    (400, "", "bad request schema", vc.UNVERIFIED),
    (None, "MODEL_TIMEOUT", "", vc.UNVERIFIED),
])
def test_tsb372r2_fix01_multi_round_cli_preserves_failed_video_diagnosis(
    tmp_path, monkeypatch, status, code, body, expected,
):
    import json
    from types import SimpleNamespace

    from app.services.vision import authorization
    from tools import probe_video_capability as probe

    settings = SimpleNamespace(model_base_url="https://example.invalid/v1",
                               model_name="test", model_mode="on")
    clip = tmp_path / "synthetic.mp4"
    clip.write_bytes(b"synthetic")
    monkeypatch.setattr(probe, "get_settings", lambda: settings)
    monkeypatch.setattr(probe, "build_synthetic_clip", lambda **_: clip)
    monkeypatch.setattr(probe, "clip_source_is_real", lambda _: False)
    monkeypatch.setattr(probe, "control_probe", lambda **_: (200, ""))
    monkeypatch.setattr(authorization, "authorize_upload", lambda **_: object())

    def probe_round(*, include_video=True, frame_only=False, **_):
        if include_video and not frame_only:
            return _outcome(http_status=status, error_code=code, response_excerpt=body)
        return _outcome()

    monkeypatch.setattr(probe, "run_probe", probe_round)
    report_file = tmp_path / "report.json"
    assert probe.main(["--execute", "--i-authorize-real-upload", "--json", str(report_file)]) == 0
    report = json.loads(report_file.read_text(encoding="utf-8"))
    assert report["capabilities"]["state"] == expected
    assert report["observation"]["http_status"] == status
    assert report["observation"]["error_code"] == code
    assert report["observation"]["response_excerpt"] == body


# --- report assembly --------------------------------------------------------

def _plan(tmp_path: Path, clip: Path | None = None):
    return plan_probe(
        clip=clip,
        model_id="test-model",
        endpoint="https://example.invalid/v1",
        model_mode="on",
    )


def test_a_dry_run_report_is_unverified_even_with_a_good_observation(tmp_path):
    """A dry-run cannot manufacture a verification."""
    report = build_report(
        plan=_plan(tmp_path),
        outcome=_outcome(temporal_signal=True),
        executed=False,
    )
    assert report["capabilities"]["state"] == vc.UNVERIFIED
    assert report["capabilities"]["sendable"] is False
    assert report["mode"] == "dry_run"


def test_an_executed_report_reflects_the_observation(tmp_path):
    report = build_report(
        plan=_plan(tmp_path),
        outcome=_outcome(temporal_signal=True, control_http_status=200),
        executed=True,
    )
    assert report["capabilities"]["state"] == vc.VERIFIED_NATIVE_VIDEO
    assert report["capabilities"]["sendable"] is True
    assert report["mode"] == "real_probe"


def test_the_report_keeps_limits_unknown_when_nothing_measured_them(tmp_path):
    report = build_report(
        plan=_plan(tmp_path),
        outcome=_outcome(temporal_signal=True, control_http_status=200),
        executed=True,
    )
    caps = report["capabilities"]
    assert caps["max_bytes"] is None
    assert caps["max_duration_sec"] is None
    assert "max_bytes" in caps["unknown_fields"]


def test_the_report_carries_no_credentials(tmp_path):
    import json

    report = build_report(plan=_plan(tmp_path), outcome=_outcome(), executed=False)
    blob = json.dumps(report, ensure_ascii=False)
    for forbidden in ("Bearer ", "api_key", "sk-"):
        assert forbidden not in blob


# --- plan honesty -----------------------------------------------------------

def test_a_synthetic_clip_is_not_labelled_as_operator_footage(tmp_path):
    """Regression: the first version mislabelled the generated clip."""
    synth_dir = tmp_path / "tsb-video-probe-abc123"
    synth_dir.mkdir()
    clip = synth_dir / "probe.mp4"
    clip.write_bytes(b"\x00" * 10)
    assert clip_source_is_real(clip) is False

    real = tmp_path / "my_trip.mp4"
    real.write_bytes(b"\x00" * 10)
    assert clip_source_is_real(real) is True


def test_the_plan_states_when_mode_on_means_a_real_send(tmp_path):
    plan = plan_probe(
        clip=None, model_id="m", endpoint="https://h/v1", model_mode="on"
    )
    assert any("真正离开本机" in r for r in plan.risks)


def test_the_plan_states_when_mode_off_will_refuse(tmp_path):
    plan = plan_probe(
        clip=None, model_id="m", endpoint="https://h/v1", model_mode="off"
    )
    assert any("授权边界拒绝" in r for r in plan.risks)


def test_the_plan_flags_cleartext_non_loopback(tmp_path):
    plan = plan_probe(
        clip=None, model_id="m", endpoint="http://remote.example/v1", model_mode="on"
    )
    assert any("明文" in r for r in plan.risks)


def test_the_plan_does_not_flag_loopback_cleartext(tmp_path):
    plan = plan_probe(
        clip=None, model_id="m", endpoint="http://127.0.0.1:8000/v1", model_mode="on"
    )
    assert not any("非回环" in r for r in plan.risks)


def test_a_real_clip_plan_warns_about_unmasked_faces():
    plan = plan_probe(
        clip=Path("C:/footage/trip.mp4"),
        model_id="m",
        endpoint="https://h/v1",
        model_mode="on",
    )
    assert any("人脸/车牌" in r for r in plan.risks)


# --- the command-line gates (the part a human actually touches) -------------

def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), *args],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def test_dry_run_exits_zero_and_sends_nothing():
    proc = _run()
    assert proc.returncode == 0
    assert "DRY-RUN: nothing was sent" in proc.stdout


def test_execute_without_explicit_consent_is_refused():
    """A real upload must not be reachable by remembering one flag."""
    proc = _run("--execute")
    assert proc.returncode == 2
    assert "REFUSED" in proc.stdout
    assert "--i-authorize-real-upload" in proc.stdout


def test_dry_run_does_not_require_the_consent_flag():
    proc = _run("--json", str(Path(__import__("tempfile").gettempdir()) / "tsb-probe-dry.json"))
    assert proc.returncode == 0


def test_a_missing_clip_is_refused_rather_than_silently_substituted():
    proc = _run(
        "--execute", "--i-authorize-real-upload", "--clip", "C:/definitely/not/here.mp4"
    )
    assert proc.returncode == 2
    assert "clip not found" in proc.stdout


def test_tsb372r2_cli_accounts_for_the_health_and_video_requests(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    import httpx

    from app.services.model_call_ledger import ModelCallLedger
    from app.services.vision import authorization
    from tools import probe_video_capability as probe

    settings = SimpleNamespace(model_base_url="https://example.invalid/v1",
                               model_name="test", model_mode="on", model_api_key="test")
    monkeypatch.setattr(probe, "get_settings", lambda: settings)
    monkeypatch.setattr(authorization, "authorize_upload", lambda **_: object())
    monkeypatch.setattr(authorization, "require_context", lambda _: None)
    clip = tmp_path / "synthetic.mp4"
    clip.write_bytes(b"synthetic")
    monkeypatch.setattr(probe, "build_synthetic_clip", lambda **_: clip)
    monkeypatch.setattr(probe, "clip_source_is_real", lambda _: False)
    monkeypatch.setattr(probe, "evaluate_probe_answer", lambda *_, **__: (True, "synthetic evidence"))
    ledger = ModelCallLedger(tmp_path / "ledger.json")
    captured = []

    def post(url, *, json, **_):
        # The charge must exist before making the outgoing request.
        size = len(httpx.Request("POST", url, json=json).content)
        captured.append(size)
        assert ledger.remaining()["per_channel"]["capability_probe"]["used"] == len(captured)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(httpx, "post", post)
    report_file = tmp_path / "report.json"
    assert probe.main(["--execute", "--i-authorize-real-upload", "--rounds", "1",
                       "--ledger", str(ledger.path), "--json", str(report_file)]) == 0
    assert len(captured) == 2
    assert ledger.summary()["by_channel"]["capability_probe"]["bytes"] == sum(captured)
    report = json.loads(report_file.read_text(encoding="utf-8"))
    assert report["observation"]["control_http_status"] == 200
    assert report["observation"]["single_round_signal"] is True
    assert report["capabilities"]["state"] == vc.VIDEO_ACCEPTED_SAMPLING_UNKNOWN
