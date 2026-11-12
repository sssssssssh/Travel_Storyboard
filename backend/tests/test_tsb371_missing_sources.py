"""TSB-371 — say which files are missing, without leaking where they live.

A reviewer who sees "2 items unavailable" cannot tell whether the tool is broken
or their library changed. The useful answer names the file and when it was last
seen working, and gives a location hint that is recognisable to the owner but does
not publish an absolute path (C5: no real paths leave the API).

The rule this module encodes:

* name the file, its size and the time it was first recorded;
* hint at the location with the *parent folder name only* — enough for the owner
  to recognise, not enough to reveal a full path;
* never claim a file is gone: say it was not found at the recorded location,
  because a re-mount or rename would make "deleted" a lie;
* never offer a path back to the UI.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import PureWindowsPath

import pytest

from app.database import session_scope
from app.models import Asset, Project, Segment, SegmentSemanticResult
from app.services import evaluation_samples as samples

import os
from pathlib import Path


def _uid() -> str:
    return str(uuid.uuid4())


def _tmp_root() -> Path:
    root = Path(os.environ.get("TEMP") or ".") / "tsb371-missing-src"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _seed(client, *, present: bool, name="DJI_20261003164352_0007_D.MP4"):
    pid = client.post("/api/projects", json={"name": "missing"}).json()["id"]
    root = _tmp_root()
    src = root / f"{pid[:8]}-{name}"
    if present:
        src.write_bytes(b"\x00" * 128)
    elif src.exists():
        src.unlink()
    with session_scope() as s:
        a = Asset(
            id=_uid(), project_id=pid, source_path=str(src), display_name=name,
            file_size_bytes=842_100_000, mtime_ns=1_700_000_000_000_000_000,
            quick_fingerprint="f", duration_sec=60.0, fps_num=25, fps_den=1,
            vfr_flag=False, width=3840, height=2160, rotation=0, codec="h264",
            pixel_format="yuv420p", color_primaries="", color_transfer="",
            color_space="", has_audio=False, audio_codec="", scan_status="succeeded",
            analysis_version="", analyzed_fingerprint="",
            analyzed_source_signature="", analyzed_detect_config="",
            analyzed_at=None,
            scanned_at=datetime(2026, 10, 3, 8, 0, tzinfo=timezone.utc),
        )
        s.add(a)
        s.flush()
        seg = Segment(
            id=_uid(), asset_id=a.id, project_id=pid,
            detected_start_sec=0.0, detected_end_sec=16.0,
            start_sec=0.0, end_sec=16.0, is_trimmed=False, boundary_reason="",
            quality_metrics={"blur_avg": 100.0}, content_tags={}, story_role="",
            user_rating="pending", status="candidate", frame_refs=[], proxy_ref="",
            parent_segment_id="", algorithm_version="1", created_at=None,
        )
        s.add(seg)
        s.flush()
        s.add(SegmentSemanticResult(
            id=_uid(), project_id=pid, asset_id=a.id, segment_id=seg.id,
            status="succeeded", model_provider="openai_compatible", model_id="GLM",
            prompt_version="p", schema_version="1.0", source_signature=_uid(),
            frame_manifest_hash="h",
            analysis_json={"scene_tags": ["lake"], "subjects": ["pier"]},
            evidence_json={"state": "observed", "frame_ids": ["f1"]},
            request_usage={}, is_current=True, error_code="", error_message="",
            created_at=None, updated_at=None,
        ))
    return pid


# ---------------------------------------------------------------------------
# The list of missing files
# ---------------------------------------------------------------------------

def test_a_missing_file_is_named(client):
    pid = _seed(client, present=False)
    body = client.get(
        f"/api/projects/{pid}/evaluation/missing-sources").json()
    assert body["count"] == 1
    entry = body["items"][0]
    assert entry["display_name"] == "DJI_20261003164352_0007_D.MP4"
    assert entry["available"] is False


def test_a_present_file_is_not_listed(client):
    pid = _seed(client, present=True)
    body = client.get(
        f"/api/projects/{pid}/evaluation/missing-sources").json()
    assert body["count"] == 0
    assert body["items"] == []


def test_no_absolute_path_is_ever_returned(client):
    """C5: the API must not publish where the user keeps their footage.

    The folder *name* is deliberately included as a recognisable hint, so this
    asserts on structure, not on substrings: no drive letter, no leading slash,
    and no path separator inside any returned field. Asserting "the temp dir name
    must not appear" was wrong -- on Linux that name *is* the folder hint, so the
    check fired on behaviour the design calls for.
    """
    pid = _seed(client, present=False)
    body = client.get(
        f"/api/projects/{pid}/evaluation/missing-sources").json()

    def fields(obj, path=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                yield from fields(v, f"{path}.{k}")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                yield from fields(v, f"{path}[{i}]")
        else:
            yield path, obj

    for path, value in fields(body):
        if not isinstance(value, str):
            continue
        assert not value.startswith("/"), f"{path} is an absolute POSIX path: {value}"
        assert ":" not in value[:3], f"{path} carries a drive letter: {value}"
        # A path separator means a path; a bare folder name means a hint.
        assert "/" not in value and "\\" not in value, (
            f"{path} contains a path separator: {value}")

    # The hint is the parent folder's *name*, nothing more.
    entry = body["items"][0]
    assert entry["folder_hint"] == _tmp_root().name


def test_the_entry_carries_a_recognisable_hint(client):
    pid = _seed(client, present=False)
    entry = client.get(
        f"/api/projects/{pid}/evaluation/missing-sources").json()["items"][0]
    # The parent folder name is enough for the owner to recognise.
    assert entry["folder_hint"] == _tmp_root().name
    assert entry["size_mb"] == pytest.approx(842_100_000 / (1024 * 1024), rel=1e-3)


def test_the_wording_does_not_claim_deletion(client):
    """A rename or an unmounted drive would make "deleted" a lie."""
    pid = _seed(client, present=False)
    body = client.get(
        f"/api/projects/{pid}/evaluation/missing-sources").json()
    assert body["statement"] == "file_not_found_at_the_recorded_location"
    assert body["items"][0]["status"] == "not_found"


def test_the_affected_shot_count_is_reported(client):
    pid = _seed(client, present=False)
    body = client.get(
        f"/api/projects/{pid}/evaluation/missing-sources").json()
    # One file, and the shot that was found in it.
    assert body["items"][0]["affected_segments"] == 1
    assert body["summary"]["assets_missing"] == 1
    assert body["summary"]["assets_total"] == 1


# ---------------------------------------------------------------------------
# The sample summary points at it
# ---------------------------------------------------------------------------

def test_the_sample_response_reports_the_missing_file_names(client):
    pid = _seed(client, present=False)
    body = client.get(
        f"/api/projects/{pid}/evaluation/eligible-samples?limit=5").json()
    assert body["summary"]["source_unavailable"] >= 1
    names = body["summary"]["missing_sources"]
    assert "DJI_20261003164352_0007_D.MP4" in names
