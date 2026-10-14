"""TSB-243 / TSB-244 — proxy duration acceptance and package version parity.

TSB-243: the proxy build only checked "file is non-empty and has a video
stream", so a wrong-length encode was published as ``proxied``. The doc requires
the *actual* duration to be verified against the requested range, with failures
reported and history preserved.

TSB-244: ``ReviewPackage.version`` was declared ``String(32)`` while the builder
writes up to 69 characters.
"""

from __future__ import annotations

import math
import uuid
from pathlib import Path

import pytest
from sqlalchemy import inspect, select

from app.database import engine, session_scope
from app.models import Asset, Job, ReviewPackage, ReviewRequest, Segment
from app.paths import project_cache_dir
from app.services.review_package import _build_package
from app.services.story_planner import (
    PROXY_DURATION_ABS_TOLERANCE_SEC,
    PROXY_DURATION_REL_TOLERANCE,
    _verify_proxy_duration,
)
from app.services.review_validation import ReviewValidationError


# --------------------------------------------------------------------------
# TSB-243: the duration gate itself
# --------------------------------------------------------------------------

def test_exact_duration_is_accepted():
    _verify_proxy_duration(10.0, 10.0)


@pytest.mark.parametrize(
    "requested,actual",
    [
        (10.0, 10.05),   # encoder rounding
        (10.0, 9.95),
        (0.6, 0.6),      # the doc's short-proxy case
        (0.6, 0.7),      # within the absolute window
        (60.0, 63.0),    # within 10%
    ],
)
def test_tolerable_drift_is_accepted(requested, actual):
    _verify_proxy_duration(requested, actual)


@pytest.mark.parametrize(
    "requested,actual",
    [
        (10.0, 1.0),     # way too short
        (10.0, 30.0),    # way too long
        (0.6, 1.2),      # doubled — the exact class of bug reported
        (2.0, 0.1),      # almost empty
        (60.0, 90.0),    # +50%
    ],
)
def test_material_drift_is_rejected(requested, actual):
    with pytest.raises(ReviewValidationError) as excinfo:
        _verify_proxy_duration(requested, actual)
    assert excinfo.value.code == "DURATION_MISMATCH"


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
def test_invalid_actual_duration_is_rejected(bad):
    with pytest.raises(ReviewValidationError) as excinfo:
        _verify_proxy_duration(5.0, bad)
    assert excinfo.value.code in ("MEDIA_UNREADABLE", "DURATION_MISMATCH")


def test_tolerance_is_the_more_generous_of_the_two():
    """Short clips need the absolute window; long clips need the relative one."""
    # Short request: absolute tolerance dominates.
    assert max(PROXY_DURATION_ABS_TOLERANCE_SEC,
               0.6 * PROXY_DURATION_REL_TOLERANCE) == PROXY_DURATION_ABS_TOLERANCE_SEC
    # Long request: relative tolerance dominates.
    assert max(PROXY_DURATION_ABS_TOLERANCE_SEC,
               60.0 * PROXY_DURATION_REL_TOLERANCE) == 60.0 * PROXY_DURATION_REL_TOLERANCE


def test_error_message_names_both_durations():
    with pytest.raises(ReviewValidationError) as excinfo:
        _verify_proxy_duration(10.0, 2.0)
    message = str(excinfo.value)
    assert "2.000" in message and "10.000" in message


# --------------------------------------------------------------------------
# TSB-243: end-to-end through the job, with a stubbed encoder
# --------------------------------------------------------------------------

def _source_file(tmp_path: Path, name: str = "source.mp4") -> Path:
    """A real file standing in for a scanned video.

    ``validate_review`` / ``verified_source`` require the source to exist, so the
    fixtures cannot use a placeholder path.
    """
    path = tmp_path / name
    path.write_bytes(b"not-a-real-video-but-present")
    return path


@pytest.fixture()
def project(client):
    res = client.post("/api/projects", json={"name": "proxy-duration", "source_roots": []})
    assert res.status_code in (200, 201), res.text
    return res.json()


def _seed_request(project_id: str, start: float, end: float, source: Path,
                  duration: float = 60.0) -> tuple[str, str]:
    """Create an asset + segment + pending review request; return (asset, request).

    ``source`` must be a real file: ``validate_review`` refuses to build anything
    from an asset whose source has disappeared.
    """
    asset_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(Asset(id=asset_id, project_id=project_id, source_path=str(source),
                    display_name=source.name, duration_sec=duration,
                    file_size_bytes=source.stat().st_size, scan_status="succeeded"))
    seg_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(Segment(id=seg_id, project_id=project_id, asset_id=asset_id,
                      detected_start_sec=0.0, detected_end_sec=duration,
                      start_sec=0.0, end_sec=duration, boundary_reason="hard_cut"))
    req_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(ReviewRequest(id=req_id, project_id=project_id, asset_id=asset_id,
                            segment_id=seg_id, start_sec=start, end_sec=end,
                            priority="high", source="ai", status="pending"))
    return asset_id, req_id


def _run_proxies_job(project_id: str) -> tuple[str, Job, ReviewRequest]:
    from app.services.runner import JobRunner
    from app.services.story_planner import proxies_task

    with session_scope() as s:
        job = Job(project_id=project_id, job_type="proxies")
        s.add(job)
        s.flush()
        job_id = job.id
    JobRunner()._run(job_id, proxies_task)
    with session_scope() as s:
        row = s.get(Job, job_id)
        req = s.scalars(
            select(ReviewRequest).where(ReviewRequest.project_id == project_id)
        ).one()
        s.expunge(row)
        s.expunge(req)
    return job_id, row, req


def test_wrong_length_proxy_is_not_published(project, monkeypatch, tmp_path: Path):
    """A stub encoder that writes a wrong-length file must not end as `proxied`."""
    source = _source_file(tmp_path)
    _seed_request(project["id"], 0.0, 10.0, source)

    def wrong_length(source, start, duration, out, **kwargs):
        # Emit a file whose probed duration is far from what was requested.
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x00" * 128)

    monkeypatch.setattr("app.services.story_planner.extract_preview", wrong_length)

    from types import SimpleNamespace

    def fake_probe(path):
        return SimpleNamespace(
            duration_sec=1.0,  # requested 10.0 -> drift 9.0
            video=SimpleNamespace(width=480, height=270),
            has_video=True,
            has_audio=False,
        )

    monkeypatch.setattr("app.services.story_planner.probe_media", fake_probe)

    _, job, req = _run_proxies_job(project["id"])

    # A duration mismatch is a *validation* problem (the request/asset pair needs
    # human attention), which this codebase surfaces as `needs_review`; a crash
    # would be `failed`. Either way it must not be `proxied`.
    assert req.status == "needs_review", (req.status, req.reason)
    assert not req.proxy_ref, "a wrong-length proxy must not be published"

    assert job.status == "partial_failed"
    assert job.failures["count"] >= 1
    messages = " ".join(item["message"] for item in job.failures["items"])
    assert "DURATION_MISMATCH" in messages, messages


def test_correct_length_proxy_is_published(project, monkeypatch, tmp_path: Path):
    """A matching duration still publishes normally."""
    source = _source_file(tmp_path)
    _seed_request(project["id"], 0.0, 10.0, source)

    def good(source, start, duration, out, **kwargs):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x00" * 128)

    monkeypatch.setattr("app.services.story_planner.extract_preview", good)

    from types import SimpleNamespace

    monkeypatch.setattr(
        "app.services.story_planner.probe_media",
        lambda path: SimpleNamespace(
            duration_sec=10.0,
            video=SimpleNamespace(width=480, height=270),
            has_video=True,
            has_audio=False,
        ),
    )

    _, job, req = _run_proxies_job(project["id"])

    assert req.status == "proxied", (req.status, req.reason)
    assert req.proxy_ref
    assert job.status == "succeeded"
    # The measured duration is recorded so drift is auditable.
    assert job.resume_token.get("proxy_publications") or job.stats.get("output_duration_sec")


def test_failed_proxy_preserves_previous_publication(project, monkeypatch, tmp_path: Path):
    """A rejected re-run must not clobber an already-published proxy."""
    source = _source_file(tmp_path)
    _, req_id = _seed_request(project["id"], 0.0, 10.0, source)

    # First: a good run publishes a proxy.
    with session_scope() as s:
        rr = s.get(ReviewRequest, req_id)
        rr.status = "pending"
    def good(source, start, duration, out, **kwargs):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"\x00" * 128)

    from types import SimpleNamespace

    monkeypatch.setattr("app.services.story_planner.extract_preview", good)
    monkeypatch.setattr(
        "app.services.story_planner.probe_media",
        lambda path: SimpleNamespace(
            duration_sec=10.0, video=SimpleNamespace(width=480, height=270),
            has_video=True, has_audio=False),
    )
    _, _, first = _run_proxies_job(project["id"])
    assert first.status == "proxied"
    original_ref = first.proxy_ref
    assert original_ref

    # Re-open the request and make the encoder produce a wrong length.
    with session_scope() as s:
        rr = s.get(ReviewRequest, req_id)
        rr.status = "pending"

    monkeypatch.setattr(
        "app.services.story_planner.probe_media",
        lambda path: SimpleNamespace(
            duration_sec=1.0, video=SimpleNamespace(width=480, height=270),
            has_video=True, has_audio=False),
    )
    _, _, second = _run_proxies_job(project["id"])

    # The re-run was rejected for a duration mismatch, not published...
    assert second.status != "proxied", second.status
    # ...and the previously published proxy reference is preserved verbatim.
    assert second.proxy_ref == original_ref


# --------------------------------------------------------------------------
# TSB-244: package version fits its declared column
# --------------------------------------------------------------------------

def test_version_column_matches_generated_length():
    """The declared length must accommodate what the builder writes."""
    columns = {c["name"]: c for c in inspect(engine).get_columns("review_packages")}
    declared = columns["version"]["type"].length
    assert declared is not None
    # job_id (36) + '-' + 32 hex chars = 69; the column must not be shorter.
    assert declared >= 69, f"version column is only {declared} chars"


def test_generated_version_fits_and_is_unique(project, tmp_path: Path):
    """Two builds produce distinct versions that both fit the column."""
    source = _source_file(tmp_path, "pkg_source.mp4")
    asset_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(Asset(id=asset_id, project_id=project["id"], source_path=str(source),
                    display_name=source.name, duration_sec=5.0,
                    file_size_bytes=source.stat().st_size, scan_status="succeeded"))
    seg_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(Segment(id=seg_id, project_id=project["id"], asset_id=asset_id,
                      detected_start_sec=0.0, detected_end_sec=5.0,
                      start_sec=0.0, end_sec=5.0))
        s.flush()
    frame_id = str(uuid.uuid4())
    with session_scope() as s:
        from app.models import Frame
        from app.paths import frames_dir
        from PIL import Image

        path = frames_dir(project["id"], asset_id) / f"{frame_id}.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (64, 36), (10, 20, 30)).save(path)
        s.add(Frame(id=frame_id, segment_id=seg_id, asset_id=asset_id,
                    time_sec=1.0, image_path=path.resolve().as_posix()))

    a = _build_package(project["id"], {})
    b = _build_package(project["id"], {})

    columns = {c["name"]: c for c in inspect(engine).get_columns("review_packages")}
    limit = columns["version"]["type"].length

    assert a["version"] != b["version"]
    for version in (a["version"], b["version"]):
        assert len(version) <= limit, f"{version} ({len(version)}) > {limit}"

    with session_scope() as s:
        stored = [r.version for r in s.scalars(
            select(ReviewPackage).where(ReviewPackage.project_id == project["id"])
        ).all()]
    assert set(stored) == {a["version"], b["version"]}
    assert all(len(v) <= limit for v in stored)
