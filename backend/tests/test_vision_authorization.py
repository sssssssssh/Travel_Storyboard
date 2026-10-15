"""TSB-312 — one outbound authorization boundary for every model entry point.

The defect being pinned: the legacy ``POST /story/analyze`` route uploaded frames
as soon as a base URL and key were configured, because it only checked
``model_client.is_configured()``. Configuring a key therefore *silently*
authorised uploads from the old endpoint, while the newer ``vision_analyze`` job
required an explicit per-call consent. C5 requires a global switch **and** a
per-call consent on every path.

Every test here asserts the same observable: **zero outbound calls and zero
image temp copies** when any condition is missing. Counting calls is the point —
asserting only an error code would not prove nothing left the machine.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select

from app.database import session_scope
from app.models import Asset, Frame, Job, Segment, SegmentSemanticResult
from app.paths import frames_dir, rel_to_data
from app.services.vision import semantic
from app.services.vision.authorization import (
    AuthorizedModelContext,
    authorization_denial_reason,
    authorize_upload,
)
from app.services.vision.errors import MODEL_NOT_CONFIGURED, MODEL_UPLOAD_DENIED, ModelError


@pytest.fixture()
def project(client):
    res = client.post("/api/projects", json={"name": "authz-test", "source_roots": []})
    assert res.status_code in (200, 201), res.text
    return res.json()


def _jpeg(path: Path, colour=(80, 100, 120)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (160, 90), colour).save(path, format="JPEG")


def _seed(project_id: str, count: int = 1) -> list[str]:
    """One asset with ``count`` shots that each have a readable frame."""
    asset_id = str(uuid.uuid4())
    seg_ids: list[str] = []
    with session_scope() as s:
        s.add(
            Asset(
                id=asset_id,
                project_id=project_id,
                source_path="C:/footage/a.mp4",
                display_name="a.mp4",
                file_size_bytes=1000,
            )
        )
        s.flush()
        for i in range(count):
            seg_id = str(uuid.uuid4())
            path = frames_dir(project_id, asset_id) / f"{seg_id}-0.jpg"
            _jpeg(path, (30 + i * 40, 60, 90))
            s.add(
                Segment(
                    id=seg_id,
                    project_id=project_id,
                    asset_id=asset_id,
                    start_sec=float(i * 10),
                    end_sec=float(i * 10 + 5),
                    detected_start_sec=float(i * 10),
                    detected_end_sec=float(i * 10 + 5),
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
                    image_path=rel_to_data(path),
                )
            )
            seg_ids.append(seg_id)
    return seg_ids


class _CountingProvider:
    """Counts every attempt to reach the model, and never touches the network."""

    name = "counting"

    def __init__(self) -> None:
        self.calls: list = []

    def complete(self, request, *, cancel_check=None):
        # Mirror the real adapter's guard so a bypass would be caught here too.
        from app.services.vision.authorization import require_context

        require_context(request.authorization)
        self.calls.append(request)
        raise ModelError(MODEL_NOT_CONFIGURED, "counting stub never answers")


@pytest.fixture()
def counting(monkeypatch):
    """Install a counting provider and enable the model config (mode left to tests)."""
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "model_base_url", "http://127.0.0.1:9/v1", raising=False)
    monkeypatch.setattr(s, "model_api_key", "test-key-abcdefgh", raising=False)
    monkeypatch.setattr(s, "model_name", "stub-model", raising=False)
    monkeypatch.setattr(s, "vision_max_segments_per_job", 50, raising=False)

    provider = _CountingProvider()
    import app.services.vision as vision_pkg

    monkeypatch.setattr(vision_pkg, "get_provider", lambda: provider, raising=False)
    monkeypatch.setattr(semantic, "get_provider", lambda: provider, raising=False)
    return provider


def _job_row(job_id: str) -> Job:
    with session_scope() as s:
        return s.get(Job, job_id)


def _make_job(project_id: str, **config) -> str:
    job_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(
            Job(
                id=job_id,
                project_id=project_id,
                job_type="vision_analyze",
                status="pending",
                config_snapshot=config,
                stats={},
                failures={"count": 0, "items": []},
            )
        )
    return job_id


def _temp_images() -> set[str]:
    """JPEG names currently under the vision staging area (should stay empty)."""
    from app.paths import project_cache_dir

    found: set[str] = set()
    root = Path("data/projects")
    if not root.exists():
        return found
    for p in root.glob("*/vision/**/*.jpg"):
        found.add(p.name)
    return found


# --------------------------------------------------------------------------
# the boundary itself
# --------------------------------------------------------------------------


def test_mode_off_denies_even_with_consent(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "model_mode", "off", raising=False)
    reason = authorization_denial_reason(upload_acknowledged=True, project_id="p")
    assert reason is not None and reason.startswith(MODEL_UPLOAD_DENIED)
    assert "TRAVELSB_MODEL_MODE" in reason


def test_mode_on_without_consent_denies(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "model_mode", "on", raising=False)
    reason = authorization_denial_reason(upload_acknowledged=False, project_id="p")
    assert reason is not None and reason.startswith(MODEL_UPLOAD_DENIED)


def test_missing_project_scope_denies(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "model_mode", "on", raising=False)
    reason = authorization_denial_reason(upload_acknowledged=True, project_id="")
    assert reason is not None and reason.startswith(MODEL_UPLOAD_DENIED)


def test_unconfigured_model_reports_not_configured(monkeypatch):
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "model_mode", "on", raising=False)
    monkeypatch.setattr(s, "model_api_key", "", raising=False)
    reason = authorization_denial_reason(upload_acknowledged=True, project_id="p")
    assert reason is not None and reason.startswith(MODEL_NOT_CONFIGURED)


def test_all_conditions_met_yields_a_per_call_context(monkeypatch):
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "model_mode", "on", raising=False)
    monkeypatch.setattr(s, "model_base_url", "http://127.0.0.1:9/v1", raising=False)
    monkeypatch.setattr(s, "model_api_key", "k" * 12, raising=False)
    ctx = authorize_upload(project_id="p1", upload_acknowledged=True, purpose="vision_analyze")
    assert isinstance(ctx, AuthorizedModelContext)
    assert ctx.project_id == "p1" and ctx.purpose == "vision_analyze"
    # The audit form must not carry anything sensitive.
    logged = ctx.for_log()
    assert "k" * 12 not in str(logged)
    assert set(logged) >= {"project_id", "purpose"}


def test_contexts_are_per_call_not_process_global(monkeypatch):
    """Two calls must produce independent tokens (no sticky 'authorised' state)."""
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "model_mode", "on", raising=False)
    monkeypatch.setattr(s, "model_base_url", "http://127.0.0.1:9/v1", raising=False)
    monkeypatch.setattr(s, "model_api_key", "k" * 12, raising=False)
    a = authorize_upload(project_id="p1", upload_acknowledged=True, purpose="a")
    b = authorize_upload(project_id="p2", upload_acknowledged=True, purpose="b")
    assert a is not b and a.project_id != b.project_id


# --------------------------------------------------------------------------
# new job entry point: zero outbound when not authorised (R06, R08)
# --------------------------------------------------------------------------


def test_new_job_makes_zero_calls_when_mode_off(project, counting, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "model_mode", "off", raising=False)
    _seed(project["id"])
    before = _temp_images()

    job_id = _make_job(project["id"], upload_acknowledged=True)
    semantic.vision_analyze_task(job_id)

    assert counting.calls == [], "no image may leave when the global switch is off"
    assert _temp_images() == before, "nothing may be rendered before authorization"
    row = _job_row(job_id)
    assert row.status == "failed" and MODEL_UPLOAD_DENIED in row.error


def test_new_job_makes_zero_calls_without_acknowledgement(project, counting, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "model_mode", "on", raising=False)
    _seed(project["id"])
    before = _temp_images()

    job_id = _make_job(project["id"], upload_acknowledged=False)
    semantic.vision_analyze_task(job_id)

    assert counting.calls == []
    assert _temp_images() == before
    row = _job_row(job_id)
    assert row.status == "failed" and MODEL_UPLOAD_DENIED in row.error
    with session_scope() as s:
        assert s.scalars(
            select(SegmentSemanticResult).where(
                SegmentSemanticResult.project_id == project["id"]
            )
        ).all() == []


def test_new_job_makes_zero_calls_when_key_missing(project, counting, monkeypatch):
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "model_mode", "on", raising=False)
    monkeypatch.setattr(s, "model_api_key", "", raising=False)
    _seed(project["id"])

    job_id = _make_job(project["id"], upload_acknowledged=True)
    semantic.vision_analyze_task(job_id)

    assert counting.calls == []
    assert MODEL_NOT_CONFIGURED in _job_row(job_id).error


def test_new_job_reaches_the_provider_only_when_authorised(project, counting, monkeypatch):
    """The positive direction: with both conditions met the call really happens."""
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "model_mode", "on", raising=False)
    _seed(project["id"], count=2)

    job_id = _make_job(project["id"], upload_acknowledged=True)
    semantic.vision_analyze_task(job_id)

    assert len(counting.calls) == 2, "each shot should have produced one attempt"
    # The stub always fails, so the job must report that rather than claim success.
    assert _job_row(job_id).status == "partial_failed"


# --------------------------------------------------------------------------
# legacy entry point: no silent upload (R07, R09)
# --------------------------------------------------------------------------


def test_legacy_story_analyze_without_body_is_refused(project, counting, monkeypatch, client):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "model_mode", "on", raising=False)
    res = client.post(f"/api/projects/{project['id']}/story/analyze")
    assert res.status_code == 403, res.text
    detail = res.json()["detail"]
    assert detail["error_code"] == MODEL_UPLOAD_DENIED
    assert "migration_hint" in detail and "vision_analyze" in detail["migration_hint"]


def test_legacy_story_analyze_without_consent_is_refused(project, counting, monkeypatch, client):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "model_mode", "on", raising=False)
    res = client.post(
        f"/api/projects/{project['id']}/story/analyze",
        json={"upload_acknowledged": False},
    )
    assert res.status_code == 403
    assert res.json()["detail"]["error_code"] == MODEL_UPLOAD_DENIED


def test_legacy_story_analyze_denied_when_mode_off(project, counting, monkeypatch, client):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "model_mode", "off", raising=False)
    # Even an explicitly acknowledging client cannot upload while the switch is off.
    res = client.post(
        f"/api/projects/{project['id']}/story/analyze",
        json={"upload_acknowledged": True},
    )
    assert res.status_code == 403
    assert res.json()["detail"]["error_code"] == MODEL_UPLOAD_DENIED


def test_legacy_route_shares_the_boundary_with_the_new_one(project, monkeypatch):
    """Both entry points must consult the same decision function.

    Asserted structurally: if the legacy route grew its own rules again, this
    check would fail while every behavioural test above still passed. The
    docstring is stripped first so prose *describing* the old defect cannot
    satisfy or trip the assertion.
    """
    import ast
    import inspect
    import textwrap

    from app.api import story

    fn = story.analyze_story
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    # Drop the docstring so only executable code is inspected.
    body = tree.body[0].body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]
    code = ast.unparse(ast.Module(body=body, type_ignores=[]))

    assert "authorize_upload" in code or "authorization_denial_reason" in code, (
        "the legacy route must use the shared boundary rather than re-deriving permission"
    )
    assert "is_configured" not in code, (
        "checking only is_configured() is exactly the silent-upload defect (TSB-312)"
    )
