"""TSB-335 — the capability record's probe → load → invalidate → use loop.

The defect
----------
``current_capabilities()`` returned ``unverified`` unconditionally, so the video
route answered 422 forever: a successful probe could never enable the channel.

The dangerous "fix" is to read the committed ``docs/validation/.../
CAPABILITY_REPORT.json`` and grant permission from it. That file is public,
belongs to one past deployment, and is trivially forgeable by anyone who can open
a pull request. Granting send permission from it would turn a privacy guard into a
rubber stamp. TSB-335 §2 forbids it, and these tests pin that refusal.

What is required instead
------------------------
A record produced by an **actually performed probe**, stored in a local,
operator-controlled, non-repository location, bound to the **full server
identity** (base URL + model + protocol). If any of those changes, or the record
is damaged, or it is unreadable, or it is stale, the answer must fall back to
``unverified`` -- never to a remembered positive.

Acceptance criteria covered here (task book §2 list ①‑⑥):

* ① fresh install / no record → ``unverified``, refusal before any send
* ② a report measured as ``unsupported`` keeps refusing
* ③ a local verified record bound to *this* endpoint permits the channel
* ④ changing URL / model / protocol / deployment, or expiry, refuses again
* ⑤ a forged or evidence-free report never unlocks; a bare HTTP 200 without
  temporal evidence is ``video_accepted_sampling_unknown`` and still not sendable
* ⑥ restart behaviour is explicit (a fresh process re-reads the store)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.vision import video_capabilities as vc
from app.services.vision.video_capabilities import (
    UNSUPPORTED,
    UNVERIFIED,
    VERIFIED_NATIVE_VIDEO,
)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """Point the capability store at an isolated, non-repository directory."""
    from app.services.vision import capability_store as cs

    path = tmp_path / "runtime" / "video_capability.json"
    monkeypatch.setattr(cs, "capability_store_path", lambda: path)
    cs.reset_cache()
    yield path
    cs.reset_cache()


def _identity(url="https://models.example/v1", model="GLM-5.3-Flash"):
    from app.services.vision.capability_store import EndpointIdentity

    return EndpointIdentity(base_url=url, model_name=model, protocol="video_url_data_uri")


def _verified_record(**over):
    from app.services.vision.capability_store import build_record

    base = {
        "state": VERIFIED_NATIVE_VIDEO,
        "protocol": "video_url_data_uri",
        "model_id": "GLM-5.3-Flash",
        "verification_scope": "synthetic 3s clip; temporal order echoed back correctly",
        "max_duration_sec": 15.0,
        "max_bytes": 8_000_000,
        "accepts_audio": False,
        "temporal_sampling_disclosed": False,
        "deployment_id": "vllm-0.9.2-eas-glm53",
        "evidence_digest": "a" * 64,
    }
    base.update(over)
    return build_record(identity=_identity(), **base)


# --- ① fresh install --------------------------------------------------------


def test_a1_no_record_is_unverified_and_refuses(store, monkeypatch):
    from app.services.vision import capability_store as cs

    monkeypatch.setattr(
        "app.services.vision.capability_store.current_identity", lambda: _identity()
    )
    caps = cs.load_capabilities()
    assert caps.state == UNVERIFIED
    assert caps.sendable is False
    assert not store.exists(), "loading must not create a record"


# --- ③ a bound positive record permits --------------------------------------


def test_a3_a_bound_record_permits_the_channel(store, monkeypatch):
    from app.services.vision import capability_store as cs

    cs.save_record(_verified_record(), identity=_identity())
    monkeypatch.setattr("app.services.vision.capability_store.current_identity", _identity)

    caps = cs.load_capabilities()
    assert caps.state == VERIFIED_NATIVE_VIDEO
    assert caps.sendable is True
    assert caps.protocol == "video_url_data_uri"
    assert caps.max_duration_sec == 15.0


# --- ④ any identity change revokes ------------------------------------------


@pytest.mark.parametrize(
    "field,new_value",
    [
        ("base_url", "https://other.example/v1"),
        ("model_name", "some-other-model"),
        ("protocol", "video_upload_multipart"),
    ],
)
def test_a4_identity_change_revokes_the_record(store, monkeypatch, field, new_value):
    from app.services.vision import capability_store as cs

    cs.save_record(_verified_record(), identity=_identity())

    from dataclasses import replace

    changed = replace(_identity(), **{field: new_value})
    monkeypatch.setattr("app.services.vision.capability_store.current_identity", lambda: changed)

    caps = cs.load_capabilities()
    assert caps.state == UNVERIFIED, f"a changed {field} must revoke the positive record"
    assert caps.sendable is False


def test_a4b_a_changed_deployment_id_revokes_the_record(store, monkeypatch):
    from app.services.vision import capability_store as cs

    cs.save_record(_verified_record(), identity=_identity())
    monkeypatch.setattr("app.services.vision.capability_store.current_identity", _identity)

    # Same URL/model/protocol, but the server reports a different deployment.
    from dataclasses import replace

    live = replace(_identity(), deployment_id="vllm-OTHER-build")
    monkeypatch.setattr("app.services.vision.capability_store.current_identity", lambda: live)

    caps = cs.load_capabilities()
    assert caps.state == UNVERIFIED, "a changed deployment identity must revoke"
    assert caps.sendable is False


def test_a4c_an_expired_record_revokes(store, monkeypatch):
    from app.services.vision import capability_store as cs

    old = _verified_record(probe_time="2020-01-01T00:00:00+00:00")
    cs.save_record(old, identity=_identity())
    monkeypatch.setattr("app.services.vision.capability_store.current_identity", _identity)

    caps = cs.load_capabilities(max_age_days=30)
    assert caps.state == UNVERIFIED, "a stale record must not keep granting permission"
    assert caps.sendable is False


# --- ② an unsupported measurement keeps refusing -----------------------------


def test_a2_an_unsupported_record_still_refuses(store, monkeypatch):
    from app.services.vision import capability_store as cs
    from app.services.vision.capability_store import build_record

    rec = build_record(
        identity=_identity(),
        state=UNSUPPORTED,
        protocol="video_url_data_uri",
        model_id="GLM-5.3-Flash",
        verification_scope="measured: server replied 'At most 0 video(s)'",
        evidence_digest="b" * 64,
    )
    cs.save_record(rec, identity=_identity())
    monkeypatch.setattr("app.services.vision.capability_store.current_identity", _identity)

    caps = cs.load_capabilities()
    assert caps.state == UNSUPPORTED
    assert caps.sendable is False


# --- ⑤ forged / evidence-free records must never unlock ---------------------


def test_a5_a_record_without_evidence_never_unlocks(store, monkeypatch):
    from app.services.vision import capability_store as cs

    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "identity": {
                    "base_url": "https://models.example/v1",
                    "model_name": "GLM-5.3-Flash",
                    "protocol": "video_url_data_uri",
                },
                "capabilities": {"state": VERIFIED_NATIVE_VIDEO, "sendable": True},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("app.services.vision.capability_store.current_identity", _identity)

    caps = cs.load_capabilities()
    assert caps.state == UNVERIFIED, (
        "a record asserting sendable without probe evidence and a verification "
        "scope must not unlock the channel"
    )


def test_a5b_a_corrupt_record_is_unverified_not_a_crash(store, monkeypatch):
    from app.services.vision import capability_store as cs

    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text("{not json at all", encoding="utf-8")
    monkeypatch.setattr("app.services.vision.capability_store.current_identity", _identity)

    caps = cs.load_capabilities()
    assert caps.state == UNVERIFIED
    assert caps.sendable is False


def test_a5c_a_record_asserting_verified_without_scope_is_rejected(store, monkeypatch):
    from app.services.vision import capability_store as cs

    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "identity": {
                    "base_url": "https://models.example/v1",
                    "model_name": "GLM-5.3-Flash",
                    "protocol": "video_url_data_uri",
                },
                "capabilities": {
                    "state": VERIFIED_NATIVE_VIDEO,
                    "protocol": "video_url_data_uri",
                    "model_id": "GLM-5.3-Flash",
                    "verification_scope": "",
                    "evidence_digest": "c" * 64,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("app.services.vision.capability_store.current_identity", _identity)

    caps = cs.load_capabilities()
    assert caps.state == UNVERIFIED, "a claim with no scope is exactly 'HTTP 200 means it works'"


def test_a5d_a_plain_200_without_temporal_evidence_is_not_sendable():
    """A 200 that showed no temporal content is its own, weaker state."""
    caps = vc.record_verification(
        state="video_accepted_sampling_unknown",
        protocol="video_url_data_uri",
        model_id="GLM-5.3-Flash",
        verification_scope="HTTP 200, but the response showed no temporal content",
    )
    assert caps.sendable is False, (
        "accepting bytes is not the same as understanding time; user video must "
        "not be sent on this evidence"
    )


# --- ⑥ restart behaviour ----------------------------------------------------


def test_a6_a_restart_reloads_from_the_store(store, monkeypatch):
    from app.services.vision import capability_store as cs

    cs.save_record(_verified_record(), identity=_identity())
    monkeypatch.setattr("app.services.vision.capability_store.current_identity", _identity)
    assert cs.load_capabilities().sendable is True

    cs.reset_cache()  # simulate a fresh process
    again = cs.load_capabilities()
    assert again.sendable is True
    assert again.protocol == "video_url_data_uri"


def test_a6b_the_store_is_not_in_the_repository(store):
    """The record must live outside the repo so a PR cannot grant permission."""
    from app.services.vision import capability_store as cs

    import app.services.vision.capability_store as mod

    repo_root = Path(mod.__file__).resolve().parents[3].parent
    real = Path(cs.default_capability_store_path()).resolve()
    assert repo_root not in real.parents, (
        f"capability store {real} must not live inside the repository {repo_root}"
    )


def test_a6c_the_store_never_contains_a_key_or_a_source_path(store, monkeypatch):
    from app.services.vision import capability_store as cs

    cs.save_record(_verified_record(), identity=_identity())
    blob = store.read_text(encoding="utf-8")

    assert "api_key" not in blob.lower()
    assert "authorization" not in blob.lower()
    assert "/Users/" not in blob and "C:\\\\" not in blob
    # The digest stands in for the evidence; the endpoint host may appear as an
    # identity binding, but never a credential.
    assert "Bearer" not in blob


# --- wiring: the production path must actually consult the store -------------


def test_w1_current_capabilities_reads_the_store(store, monkeypatch):
    """TSB-335 wiring: ``current_capabilities()`` is the production entry point.

    A store that loads correctly is worthless if the job still hardcodes
    ``unverified`` (the TSB-253 lesson: the unit works, the call site does not).
    This asserts the production function itself changes with the store.
    """
    from app.services.vision import capability_store as cs
    from app.services.vision import video_job

    monkeypatch.setattr(cs, "current_identity", _identity)
    monkeypatch.setattr(video_job.get_settings(), "model_base_url", _identity().base_url, raising=False)
    monkeypatch.setattr(video_job.get_settings(), "model_name", _identity().model_name, raising=False)

    # No record -> refusal, and the reason must be actionable.
    before = video_job.current_capabilities()
    assert before.state == UNVERIFIED
    assert before.sendable is False

    cs.save_record(_verified_record(), identity=_identity())
    cs.reset_cache()

    after = video_job.current_capabilities()
    assert after.state == VERIFIED_NATIVE_VIDEO, (
        "the production capability entry point must reflect a bound probe record"
    )
    assert after.sendable is True


def test_w2_a_store_fault_never_grants_permission(store, monkeypatch):
    """A broken store must degrade to 'not allowed', not crash or allow."""
    from app.services.vision import capability_store as cs
    from app.services.vision import video_job

    def _boom(*a, **kw):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(cs, "load_capabilities", _boom)

    caps = video_job.current_capabilities()
    assert caps.state == UNVERIFIED
    assert caps.sendable is False


def test_w3_plan_video_job_refuses_without_a_record(store, monkeypatch):
    """①② : no record -> the plan refuses before any bytes are sent."""
    from app.services.vision import capability_store as cs
    from app.services.vision import video_job

    monkeypatch.setattr(cs, "current_identity", _identity)
    caps = video_job.current_capabilities()
    assert caps.sendable is False, "fresh install must not be able to send"

    # And a positive bound record flips the same call.
    cs.save_record(_verified_record(), identity=_identity())
    cs.reset_cache()
    monkeypatch.setattr(
        video_job.get_settings(), "model_base_url", _identity().base_url, raising=False
    )
    monkeypatch.setattr(
        video_job.get_settings(), "model_name", _identity().model_name, raising=False
    )
    assert video_job.current_capabilities().sendable is True


def test_w4_the_execute_path_also_consults_the_store(store, monkeypatch):
    """§2.4: capability is validated when queued *and* when executed.

    ``plan_video_job`` is what the worker calls. It used to default to a hardcoded
    ``unverified``, so the store was consulted once (if at all) and the channel
    stayed shut regardless of what had been proven.
    """
    from app.services.vision import capability_store as cs
    from app.services.vision import video_job

    monkeypatch.setattr(cs, "current_identity", _identity)
    monkeypatch.setattr(
        video_job.get_settings(), "model_base_url", _identity().base_url, raising=False
    )
    monkeypatch.setattr(
        video_job.get_settings(), "model_name", _identity().model_name, raising=False
    )

    # With no record the worker plans a non-sending mode.
    assert video_job.current_capabilities().sendable is False

    cs.save_record(_verified_record(), identity=_identity())
    cs.reset_cache()
    caps = video_job.current_capabilities()
    assert caps.sendable is True

    effective, reason = video_job.resolve_mode(
        requested_mode="video_native", capabilities=caps, allow_fallback=False
    )
    assert effective == "video_native", f"a proven endpoint must be usable; reason={reason}"


def test_w5_endpoint_switch_after_queueing_is_caught_at_execute(store, monkeypatch):
    """§2.4: a job must not ride on a positive that belonged to another endpoint."""
    from dataclasses import replace

    from app.services.vision import capability_store as cs

    cs.save_record(_verified_record(), identity=_identity())

    # The operator repoints the app at a different model before the job runs.
    switched = replace(_identity(), base_url="https://somewhere-else.example/v1")
    monkeypatch.setattr("app.services.vision.capability_store.current_identity", lambda: switched)

    caps = cs.load_capabilities()
    assert caps.sendable is False, (
        "a queued job must be re-validated against the endpoint that will actually "
        "receive the bytes, not the one that was current when it was queued"
    )
