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


@pytest.mark.parametrize("status", [400, 415, 422])
def test_a_rejection_of_the_payload_is_unsupported(status):
    state, _ = classify_probe_outcome(_outcome(http_status=status))
    assert state == vc.UNSUPPORTED


def test_a_413_is_unsupported_because_the_size_was_refused():
    state, scope = classify_probe_outcome(_outcome(http_status=413))
    assert state == vc.UNSUPPORTED
    assert "413" in scope


@pytest.mark.parametrize("status", [401, 403])
def test_an_auth_failure_is_not_a_capability_verdict(status):
    state, _ = classify_probe_outcome(_outcome(http_status=status))
    assert state == vc.UNVERIFIED


def test_a_timeout_is_inconclusive():
    state, _ = classify_probe_outcome(_outcome(http_status=None, error_code="MODEL_TIMEOUT"))
    assert state == vc.UNVERIFIED


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


def test_a_healthy_control_lets_a_video_rejection_stand():
    state, _ = classify_probe_outcome(_outcome(http_status=415, control_http_status=200))
    assert state == vc.UNSUPPORTED


def test_a_healthy_control_lets_a_verification_stand():
    state, _ = classify_probe_outcome(
        _outcome(http_status=200, control_http_status=200, temporal_signal=True)
    )
    assert state == vc.VERIFIED_NATIVE_VIDEO


def test_a_control_404_does_not_poison_the_verdict():
    """Only server-side failure means "the endpoint is broken"."""
    state, _ = classify_probe_outcome(_outcome(http_status=415, control_http_status=404))
    assert state == vc.UNSUPPORTED


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
