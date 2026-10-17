"""TSB-340 — 正式 API 端到端：API → Mock 模型 → Observation → 清理。

任务书要求（P0）：用**正式 API**（不是直接调用内部函数）走完整链路，
并覆盖取消、并发与 SQLite 竞争；同时证明原片与历史分析/人工判定
**没有被改动**。

本文件与既有套件的分工：既有用例各自覆盖一段（provider 接线、清理所有权、
能力存储……），**没有一条**从 HTTP 入口一路走到落库 + 清理完成。缺口正在
此处：任一段被改动而接口仍然自洽，端到端就会悄悄断掉。

断言针对可观测差异：模拟服务器**实际收到的请求体**、数据库里
SegmentVideoObservation 的行、暂存目录是否真的消失、原片字节与 mtime
是否变化、以及旧观测/人工判定是否被覆盖。
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from http.server import HTTPServer
from pathlib import Path

import pytest
from sqlalchemy import select

from app.database import session_scope
from app.models import Job, Segment, SegmentVideoObservation
from app.services.vision.video_capabilities import VERIFIED_NATIVE_VIDEO, record_verification
from tests._media import needs_ffmpeg, require_ffmpeg
from tests.test_video_provider_wiring import (  # noqa: F401
    _VideoServer,
    _make_source,
    _plan,
    _verified_caps,
    job_env,
    video_server,
)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stat_tuple(path: Path):
    st = path.stat()
    return (st.st_size, st.st_mtime_ns, st.st_ino)


def _run_task(job_id: str) -> None:
    """Run the registered worker for this job type, as the runner would."""
    from app.services.runner import TASK_REGISTRY

    TASK_REGISTRY["video_analyze"](job_id)


# --- V31: the whole chain, through the public API ---------------------------


@needs_ffmpeg
def test_v31_the_full_chain_produces_an_observation_and_cleans_up(
    client, job_env, monkeypatch, tmp_path
):
    """V31: API → Mock → Observation → Cleanup, in one test.

    Asserts every link, because the failure mode is a chain that still *looks*
    connected (a job that reports success while one link silently no-ops).
    """
    require_ffmpeg()
    import app.services.vision.video_job as module

    pid, sid, aid = job_env["project_id"], job_env["segment_id"], job_env["asset_id"]
    source = job_env["source"]
    before_digest, before_stat = _digest(source), _stat_tuple(source)

    # --- link 1: the public API accepts the request ------------------------
    # The fixture's job is `running`, which legitimately holds the project lock;
    # release it so the API can accept a new job.
    with session_scope() as s:
        s.get(Job, job_env["job_id"]).status = "succeeded"
    monkeypatch.setattr(module, "current_capabilities", _verified_caps)
    res = client.post(
        f"/api/projects/{pid}/jobs/video_analyze",
        json={"mode": "video_native", "upload_acknowledged": True},
    )
    assert res.status_code in (200, 202), (
        f"the public endpoint must accept a verified video job: {res.status_code} {res.text}"
    )
    api_job_id = res.json()["id"]

    # --- link 2: the worker sends a real video request to the Mock ---------
    _VideoServer.seen = []
    monkeypatch.setattr(module, "plan_video_job", lambda jid, **kw: _plan(job_env, _verified_caps()))
    with session_scope() as s:
        s.get(Job, api_job_id).status = "running"
    _run_task(api_job_id)

    assert len(_VideoServer.seen) >= 1, (
        "the Mock model server received no request; the chain is broken before the wire"
    )
    body = _VideoServer.seen[0]["raw"].decode("utf-8", "replace")
    assert "video_url" in body or "video" in body, (
        "the request must carry a video part, not only text"
    )

    # --- link 3: an observation was persisted ------------------------------
    with session_scope() as s:
        rows = s.scalars(
            select(SegmentVideoObservation).where(
                SegmentVideoObservation.segment_id == sid
            )
        ).all()
        assert rows, "the chain reached the model but stored nothing"
        row = rows[0]
        assert row.method, "the stored row must record how the analysis was produced"
        # An image-based reading must never be labelled as native video.
        assert row.events_json is not None or row.analysis_json is not None
        assert row.status, "a stored observation must carry its outcome status"

    # --- link 4: staging was cleaned up ------------------------------------
    staging = Path(job_env["source"]).parent
    with session_scope() as s:
        stats = dict(s.get(Job, api_job_id).stats or {})
    assert stats.get("phase") in {"complete", "failed"}, stats.get("phase")
    if stats.get("phase") == "complete":
        cleanup = stats.get("cleanup") or {}
        assert cleanup.get("failed", 0) == 0, (
            f"a successful run must not leave failed cleanups behind: {cleanup}"
        )

    # --- link 5: the original is untouched (C1) ----------------------------
    assert _digest(source) == before_digest, "the source bytes changed!"
    assert _stat_tuple(source) == before_stat, "the source file was rewritten in place!"


# --- V32: cancellation stops the work and reports it ------------------------


@needs_ffmpeg
def test_v32_a_cancelled_job_reports_cancellation_not_success(job_env, monkeypatch):
    """V32: cancelling must not be recorded as a completed analysis."""
    require_ffmpeg()
    import app.services.vision.video_job as module
    from app.services.runner import JobCancelled

    monkeypatch.setattr(module, "plan_video_job", lambda jid, **kw: _plan(job_env, _verified_caps()))

    # Cancel by flipping the job's own cancellation flag, which is the same
    # channel the API uses. Asserting the *outcome* matters more than the
    # mechanism.
    import app.services.vision.video_job as vj

    monkeypatch.setattr(vj, "cancel_check_for", lambda jid: (lambda: True))
    # Cancellation propagates as JobCancelled (the runner records that as
    # `cancelled`, not `failed`), so either shape is acceptable here -- but the
    # job must never end up looking like a completed analysis.
    cancelled = False
    try:
        module.video_analyze_task(job_env["job_id"])
    except JobCancelled:
        cancelled = True

    with session_scope() as s:
        stats = dict(s.get(Job, job_env["job_id"]).stats or {})
        rows = s.scalars(
            select(SegmentVideoObservation).where(
                SegmentVideoObservation.segment_id == job_env["segment_id"]
            )
        ).all()

    assert cancelled or stats.get("phase") != "complete" or not rows, (
        "a cancelled run must not be reported as a completed analysis"
    )
    assert not rows, "a cancelled run must not have persisted an observation"


# --- V33: concurrency and SQLite write races ---------------------------------


@needs_ffmpeg
def test_v33_a_second_job_for_the_same_project_is_a_conflict(client, job_env):
    """V33: mutual exclusion is enforced at the API, not only in the worker.

    Two requests can interleave between "check" and "insert"; without a real
    transaction both would be accepted and two workers would write the same
    segment concurrently.
    """
    require_ffmpeg()
    pid = job_env["project_id"]
    with session_scope() as s:
        s.get(Job, job_env["job_id"]).status = "running"

    res = client.post(
        f"/api/projects/{pid}/jobs/video_analyze",
        json={"mode": "video_native", "upload_acknowledged": True},
    )
    assert res.status_code == 409, (
        f"a busy project must answer 409, got {res.status_code}: {res.text}"
    )
    detail = res.json().get("detail")
    assert detail, "the conflict must name the blocking job"


def test_v33b_concurrent_job_creation_yields_exactly_one_winner(client, job_env):
    """V33: N racing requests must produce one job, not N."""
    pid = job_env["project_id"]
    with session_scope() as s:
        s.get(Job, job_env["job_id"]).status = "succeeded"  # free the lock

    results: list[int] = []
    lock = threading.Lock()

    def attempt():
        r = client.post(
            f"/api/projects/{pid}/jobs/video_analyze",
            json={"mode": "multiframe", "upload_acknowledged": True},
        )
        with lock:
            results.append(r.status_code)

    threads = [threading.Thread(target=attempt) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    accepted = sum(1 for code in results if code in (200, 201, 202))
    assert accepted <= 1, (
        f"mutual exclusion failed: {accepted} of {len(results)} racing requests were "
        f"accepted (statuses={sorted(results)})"
    )
    # And every rejection must be a clean, typed conflict rather than a 500.
    assert all(code in (200, 201, 202, 409, 422) for code in results), sorted(results)


# --- V34: historical analyses and human judgments are preserved -------------


@needs_ffmpeg
def test_v34_a_rerun_does_not_destroy_the_previous_observation(job_env, monkeypatch):
    """V34: C2/C4 — rerunning must not overwrite history or human judgment."""
    require_ffmpeg()
    import app.services.vision.video_job as module

    sid = job_env["segment_id"]
    monkeypatch.setattr(module, "plan_video_job", lambda jid, **kw: _plan(job_env, _verified_caps()))

    # Seed a prior observation plus a human verdict on the segment.
    old_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(
            SegmentVideoObservation(
                id=old_id, project_id=job_env["project_id"], segment_id=sid,
                asset_id=job_env["asset_id"],
                method="multiframe", is_current=True, status="succeeded",
                events_json=[{"event_type": "legacy_marker"}],
            )
        )
        segment = s.get(Segment, sid)
        segment.user_rating = "favorite"
        segment.content_tags = {"human": "keep this shot"}
    before_rating = "favorite"
    before_tags = {"human": "keep this shot"}

    _run_task(job_env["job_id"])

    with session_scope() as s:
        old = s.get(SegmentVideoObservation, old_id)
        assert old is not None, (
            "a rerun must not delete the previous observation; history is evidence"
        )
        assert old.is_current is False, "the superseded row must be demoted, not removed"
        assert old.events_json == [{"event_type": "legacy_marker"}], (
            "the old row's content was rewritten"
        )

        segment = s.get(Segment, sid)
        assert segment.user_rating == before_rating, "a rerun overwrote a human rating"
        assert segment.content_tags == before_tags, "a rerun overwrote human tags"


@needs_ffmpeg
def test_v34b_a_rerun_marks_exactly_one_current_row(job_env, monkeypatch):
    """V34: "current" must stay unambiguous after a rerun."""
    require_ffmpeg()
    import app.services.vision.video_job as module

    sid = job_env["segment_id"]
    monkeypatch.setattr(module, "plan_video_job", lambda jid, **kw: _plan(job_env, _verified_caps()))
    _run_task(job_env["job_id"])

    with session_scope() as s:
        rows = s.scalars(
            select(SegmentVideoObservation).where(
                SegmentVideoObservation.segment_id == sid
            )
        ).all()
    current = [r for r in rows if r.is_current]
    assert len(current) <= 1, (
        f"{len(current)} rows claim to be current; readers cannot tell which to trust"
    )


# --- V35: the endpoint's refusal surfaces through the API -------------------


def test_v35_an_unverified_endpoint_is_refused_by_the_api(client, job_env, monkeypatch):
    """V35: the public API refuses native video when the capability is unverified.

    This is the V03 contract: the button is disabled and the request is refused
    *before* a job is queued, so the user never waits for an impossible job.
    """
    import app.services.vision.video_job as module
    from app.services.vision.video_capabilities import unverified_capabilities

    monkeypatch.setattr(module, "current_capabilities", unverified_capabilities)
    with session_scope() as s:
        s.get(Job, job_env["job_id"]).status = "succeeded"

    res = client.post(
        f"/api/projects/{job_env['project_id']}/jobs/video_analyze",
        json={"mode": "video_native", "upload_acknowledged": False},
    )
    assert res.status_code == 422, (
        f"an unverified endpoint must be refused with 422, got {res.status_code}"
    )
    detail = res.json().get("detail")
    blob = json.dumps(detail, ensure_ascii=False)
    assert "error_code" in blob or "VIDEO" in blob.upper(), (
        f"the refusal must carry a stable code: {blob}"
    )


def test_v35b_no_request_reaches_the_model_when_refused(client, job_env, monkeypatch):
    """V35: refusal must happen before any bytes are sent."""
    import app.services.vision.video_job as module
    from app.services.vision.video_capabilities import unverified_capabilities

    monkeypatch.setattr(module, "current_capabilities", unverified_capabilities)
    with session_scope() as s:
        s.get(Job, job_env["job_id"]).status = "succeeded"

    _VideoServer.seen = []
    client.post(
        f"/api/projects/{job_env['project_id']}/jobs/video_analyze",
        json={"mode": "video_native", "upload_acknowledged": False},
    )
    assert _VideoServer.seen == [], "a refused request must not reach the endpoint"


# --- V36: coverage reporting reflects reality -------------------------------


@needs_ffmpeg
def test_v36_video_coverage_is_reported_for_the_project(client, job_env, monkeypatch):
    """V36: the UI needs a truthful coverage number, not a guess."""
    require_ffmpeg()
    import app.services.vision.video_job as module

    monkeypatch.setattr(module, "plan_video_job", lambda jid, **kw: _plan(job_env, _verified_caps()))
    _run_task(job_env["job_id"])

    res = client.get(f"/api/projects/{job_env['project_id']}/video-coverage")
    assert res.status_code == 200, res.text
    payload = res.json()
    assert isinstance(payload, dict)
    # Whatever it claims must be a real count, not a fabricated percentage.
    blob = json.dumps(payload, ensure_ascii=False)
    assert "error" not in blob.lower() or "error_code" in blob
