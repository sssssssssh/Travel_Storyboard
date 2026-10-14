"""TSB-251 — serving deep (>MAX_PATH) artifacts over HTTP.

The confirmed defect: ``api/media.py`` used plain ``Path.is_file()``, which on
Windows returns ``False`` for a >MAX_PATH file that FFmpeg really wrote. The
database reference and the file both exist, yet the route answered 404.

Probing showed the second half of the problem: Starlette's ``FileResponse``
itself raises ``RuntimeError("File ... does not exist")`` for a long path, so
fixing only the existence check would turn a 404 into a 500. Both halves are
covered here.

The security model is asserted alongside the fix: containment, the artifact
sub-directory whitelist, the extension allow-list and ``..`` traversal must all
still be refused, and the extended-length path must never leak to the client.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.database import session_scope
from app.ffmpeg import long_path
from app.models import Asset, Frame, Project, ReviewRequest, Segment
from app.paths import project_cache_dir, rel_to_data

IS_WINDOWS = os.name == "nt"
requires_windows = pytest.mark.skipif(
    not IS_WINDOWS, reason="MAX_PATH only constrains Windows"
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _make_project(client, name: str, output_dir: str = "") -> dict:
    res = client.post(
        "/api/projects",
        json={"name": name, "source_roots": [], "output_dir": output_dir},
    )
    assert res.status_code in (200, 201), res.text
    return res.json()


def _make_deep_dir(root: Path, target_len: int) -> Path:
    """A directory whose path reaches at least *target_len* characters."""
    current = root
    chunk = "d" * 40
    while len(str(current)) < target_len:
        current = current / chunk
        os.makedirs(long_path(current), exist_ok=True)
    return current


def _write(path: Path, data: bytes) -> None:
    os.makedirs(long_path(path.parent), exist_ok=True)
    try:
        path.write_bytes(data)
    except (FileNotFoundError, OSError):
        with open(long_path(path), "wb") as handle:
            handle.write(data)


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except (FileNotFoundError, OSError):
        with open(long_path(path), "rb") as handle:
            return handle.read()


def _jpeg_bytes() -> bytes:
    """A real, fully decodable JPEG.

    Generated rather than pasted as a hex literal: a hand-written blob is easy
    to truncate, and "the bytes match" is only meaningful if they also decode.
    """
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), (10, 20, 30)).save(buffer, format="JPEG")
    return buffer.getvalue()


JPEG_BYTES = _jpeg_bytes()


@pytest.fixture()
def frame_project(client, tmp_path: Path):
    """A project whose frames live under a deep custom output_dir."""
    deep_root = _make_deep_dir(tmp_path, 270)
    project = _make_project(client, "deep-media", str(deep_root))
    pid = project["id"]

    asset_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(Asset(id=asset_id, project_id=pid, source_path="/tmp/src.mp4",
                    display_name="src.mp4", duration_sec=10.0, scan_status="succeeded"))
    seg_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(Segment(id=seg_id, project_id=pid, asset_id=asset_id,
                      detected_start_sec=0.0, detected_end_sec=10.0,
                      start_sec=0.0, end_sec=10.0, boundary_reason="hard_cut"))

    frame_id = str(uuid.uuid4())
    image = project_cache_dir(pid) / "frames" / asset_id / f"{frame_id}.jpg"
    _write(image, JPEG_BYTES)
    with session_scope() as s:
        s.add(Frame(id=frame_id, segment_id=seg_id, asset_id=asset_id,
                    time_sec=1.0, image_path=rel_to_data(image)))

    return {"project": project, "pid": pid, "asset_id": asset_id,
            "frame_id": frame_id, "image": image, "deep_root": deep_root}


# --------------------------------------------------------------------------
# RT251-01 / RT251-04: deep-path images are served
# --------------------------------------------------------------------------

@requires_windows
def test_deep_frame_served_by_path(client, frame_project):
    """RT251-01: a >=270 char frame path returns 200 with identical bytes."""
    image = frame_project["image"]
    assert len(str(image)) >= 270, len(str(image))

    res = client.get(f"/api/projects/{frame_project['pid']}/media",
                     params={"path": rel_to_data(image)})

    assert res.status_code == 200, res.text
    assert res.content == _read(image)
    assert res.headers["content-type"] == "image/jpeg"


@requires_windows
def test_deep_frame_served_by_frame_id(client, frame_project):
    """RT251-04: the id-based route works too, and the JPEG decodes."""
    res = client.get(f"/api/frames/{frame_project['frame_id']}/image")

    assert res.status_code == 200, res.text
    assert len(res.content) > 0

    import io

    from PIL import Image

    with Image.open(io.BytesIO(res.content)) as decoded:
        decoded.load()
        assert decoded.size == (4, 4)


@requires_windows
def test_deep_frame_head_request(client, frame_project):
    """HEAD must agree with GET on length."""
    image = frame_project["image"]
    res = client.head(f"/api/projects/{frame_project['pid']}/media",
                      params={"path": rel_to_data(image)})
    assert res.status_code == 200, res.text
    assert int(res.headers["content-length"]) == len(_read(image))


# --------------------------------------------------------------------------
# RT251-02 / RT251-03: video Range support on deep paths
# --------------------------------------------------------------------------

def _fake_mp4(size: int = 4096) -> bytes:
    """Deterministic bytes; Range behaviour does not require a real container."""
    return bytes((i * 7) % 251 for i in range(size))


@requires_windows
def test_deep_proxy_supports_range(client, tmp_path: Path):
    """RT251-02: >=320 char proxy path answers 206 with a correct Content-Range."""
    deep_root = _make_deep_dir(tmp_path, 320)
    project = _make_project(client, "deep-proxy", str(deep_root))
    pid = project["id"]

    asset_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(Asset(id=asset_id, project_id=pid, source_path="/tmp/src.mp4",
                    display_name="src.mp4", duration_sec=10.0, scan_status="succeeded"))
    seg_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(Segment(id=seg_id, project_id=pid, asset_id=asset_id,
                      detected_start_sec=0.0, detected_end_sec=10.0,
                      start_sec=0.0, end_sec=10.0))
    req_id = str(uuid.uuid4())
    payload = _fake_mp4()
    proxy = project_cache_dir(pid) / "proxies" / "deep.mp4"
    _write(proxy, payload)
    with session_scope() as s:
        s.add(ReviewRequest(id=req_id, project_id=pid, asset_id=asset_id,
                            segment_id=seg_id, start_sec=0.0, end_sec=1.0,
                            proxy_ref=rel_to_data(proxy)))
    assert len(str(proxy)) >= 320, len(str(proxy))

    url = f"/api/projects/{pid}/review-requests/{req_id}/proxy"

    full = client.get(url)
    assert full.status_code == 200, full.text
    assert full.content == payload
    assert full.headers["accept-ranges"] == "bytes"

    partial = client.get(url, headers={"Range": "bytes=0-99"})
    assert partial.status_code == 206, partial.text
    assert partial.content == payload[:100]
    assert partial.headers["content-range"] == f"bytes 0-99/{len(payload)}"
    assert partial.headers["content-length"] == "100"


@requires_windows
def test_deep_roughcut_supports_range(client, tmp_path: Path):
    """RT251-03: the roughcut route (which used its own is_file) also works."""
    deep_root = _make_deep_dir(tmp_path, 300)
    project = _make_project(client, "deep-rough", str(deep_root))
    pid = project["id"]

    payload = _fake_mp4(2048)
    cut = project_cache_dir(pid) / "roughcut" / "roughcut.mp4"
    _write(cut, payload)
    assert len(str(cut)) >= 300, len(str(cut))

    res = client.get(f"/api/projects/{pid}/roughcut")
    assert res.status_code == 200, res.text
    assert res.content == payload
    assert res.headers["x-roughcut-incomplete"] == "false"

    partial = client.get(f"/api/projects/{pid}/roughcut",
                         headers={"Range": "bytes=100-199"})
    assert partial.status_code == 206
    assert partial.content == payload[100:200]
    assert partial.headers["content-range"] == f"bytes 100-199/{len(payload)}"


@requires_windows
def test_deep_incomplete_roughcut_is_flagged(client, tmp_path: Path):
    """The incomplete-cut warning header survives the deep-path change."""
    deep_root = _make_deep_dir(tmp_path, 300)
    project = _make_project(client, "deep-rough-inc", str(deep_root))
    pid = project["id"]
    cut = project_cache_dir(pid) / "roughcut" / "roughcut_INCOMPLETE.mp4"
    _write(cut, _fake_mp4(512))

    res = client.get(f"/api/projects/{pid}/roughcut")
    assert res.status_code == 200, res.text
    assert res.headers["x-roughcut-incomplete"] == "true"


# --------------------------------------------------------------------------
# RT251-07: a genuinely missing deep artifact is still a clean 404
# --------------------------------------------------------------------------

@requires_windows
def test_missing_deep_artifact_is_404_not_500(client, frame_project):
    """RT251-07: absence must be 404, never an unhandled server error."""
    pid = frame_project["pid"]
    ghost = project_cache_dir(pid) / "frames" / frame_project["asset_id"] / "nope.jpg"
    assert len(str(ghost)) >= 270

    res = client.get(f"/api/projects/{pid}/media", params={"path": rel_to_data(ghost)})
    assert res.status_code == 404, res.text
    assert "does not exist" not in res.text.lower()

    res = client.head(f"/api/projects/{pid}/media", params={"path": rel_to_data(ghost)})
    assert res.status_code == 404


@requires_windows
def test_missing_deep_roughcut_is_404(client, tmp_path: Path):
    deep_root = _make_deep_dir(tmp_path, 300)
    project = _make_project(client, "deep-rough-none", str(deep_root))
    res = client.get(f"/api/projects/{project['id']}/roughcut")
    assert res.status_code == 404


# --------------------------------------------------------------------------
# RT251-05 / RT251-06: the security model is unchanged
# --------------------------------------------------------------------------

@requires_windows
def test_deep_path_cannot_escape_the_project(client, frame_project, tmp_path: Path):
    """RT251-05: containment still wins over long-path compatibility."""
    pid = frame_project["pid"]
    secret = tmp_path / "secret.jpg"
    _write(secret, b"top-secret")

    res = client.get(f"/api/projects/{pid}/media", params={"path": str(secret)})
    assert res.status_code == 400, res.text
    assert b"top-secret" not in res.content


@requires_windows
def test_deep_path_traversal_is_rejected(client, frame_project):
    """RT251-05: ``..`` traversal is refused even from a deep artifact dir."""
    pid = frame_project["pid"]
    traversal = rel_to_data(frame_project["image"]) + "/../../../../../etc/passwd"
    res = client.get(f"/api/projects/{pid}/media", params={"path": traversal})
    assert res.status_code in (400, 404), res.text


@requires_windows
def test_deep_path_outside_whitelisted_dirs_is_rejected(client, frame_project):
    """RT251-06: only frames/proxies/packages/roughcut/contact_sheets are served."""
    pid = frame_project["pid"]
    rogue = project_cache_dir(pid) / "secrets" / "token.jpg"
    _write(rogue, b"should-not-be-served")

    res = client.get(f"/api/projects/{pid}/media", params={"path": rel_to_data(rogue)})
    assert res.status_code == 400, res.text
    assert res.content != b"should-not-be-served" or res.status_code == 400


@requires_windows
def test_deep_path_staging_is_rejected(client, frame_project):
    """RT251-06: staging is internal and must never be served."""
    pid = frame_project["pid"]
    staged = project_cache_dir(pid) / "frames" / "staging" / "x" / "y.jpg"
    _write(staged, JPEG_BYTES)
    res = client.get(f"/api/projects/{pid}/media", params={"path": rel_to_data(staged)})
    assert res.status_code == 400, res.text


@requires_windows
def test_deep_path_disallowed_extension_is_rejected(client, frame_project):
    """RT251-06: the extension allow-list is enforced on deep paths too."""
    pid = frame_project["pid"]
    payload = project_cache_dir(pid) / "frames" / frame_project["asset_id"] / "evil.sh"
    _write(payload, b"#!/bin/sh")

    res = client.get(f"/api/projects/{pid}/media", params={"path": rel_to_data(payload)})
    assert res.status_code == 400, res.text


@requires_windows
def test_other_project_cannot_read_the_deep_artifact(client, frame_project):
    """RT251-05: a second project must not reach the first one's frames."""
    other = _make_project(client, "other-project")
    res = client.get(f"/api/projects/{other['id']}/media",
                     params={"path": rel_to_data(frame_project["image"])})
    assert res.status_code == 400, res.text


# --------------------------------------------------------------------------
# No internal path leakage
# --------------------------------------------------------------------------

@requires_windows
def test_extended_length_prefix_never_reaches_the_client(client, frame_project):
    """The \\\\?\\ form is an internal detail; it must not appear in responses."""
    image = frame_project["image"]
    res = client.get(f"/api/projects/{frame_project['pid']}/media",
                     params={"path": rel_to_data(image)})
    assert res.status_code == 200
    for header, value in res.headers.items():
        assert "\\\\?\\" not in value, f"{header} leaked an extended-length path"
        assert "\\\\\\\\?\\\\" not in value


@requires_windows
def test_deep_media_url_uses_the_ordinary_reference(client, frame_project):
    """The stored reference stays an ordinary path — no migration, no rewrite."""
    stored = rel_to_data(frame_project["image"])
    assert not stored.startswith("\\\\?\\")

    with session_scope() as s:
        frame = s.get(Frame, frame_project["frame_id"])
        assert frame.image_path == stored
        assert not frame.image_path.startswith("\\\\?\\")


# --------------------------------------------------------------------------
# RT251-08: shallow paths keep working (both platforms)
# --------------------------------------------------------------------------

def test_shallow_frame_still_served(client, tmp_path: Path):
    """RT251-08: the ordinary short-path case is unaffected."""
    project = _make_project(client, "shallow-media")
    pid = project["id"]

    asset_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(Asset(id=asset_id, project_id=pid, source_path="/tmp/s.mp4",
                    display_name="s.mp4", duration_sec=5.0, scan_status="succeeded"))
    seg_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(Segment(id=seg_id, project_id=pid, asset_id=asset_id,
                      detected_start_sec=0.0, detected_end_sec=5.0,
                      start_sec=0.0, end_sec=5.0))
    image = project_cache_dir(pid) / "frames" / asset_id / "plain.jpg"
    _write(image, JPEG_BYTES)
    with session_scope() as s:
        s.add(Frame(id=str(uuid.uuid4()), segment_id=seg_id, asset_id=asset_id,
                    time_sec=1.0, image_path=rel_to_data(image)))

    res = client.get(f"/api/projects/{pid}/media", params={"path": rel_to_data(image)})
    assert res.status_code == 200
    assert res.content == JPEG_BYTES


def test_shallow_range_still_works(client):
    """RT251-08: Range on a normal-length path is unchanged."""
    project = _make_project(client, "shallow-range")
    pid = project["id"]
    payload = _fake_mp4(1024)
    cut = project_cache_dir(pid) / "roughcut" / "roughcut.mp4"
    _write(cut, payload)

    full = client.get(f"/api/projects/{pid}/roughcut")
    assert full.status_code == 200 and full.content == payload

    part = client.get(f"/api/projects/{pid}/roughcut", headers={"Range": "bytes=10-19"})
    assert part.status_code == 206
    assert part.content == payload[10:20]


def test_cjk_and_space_path_still_served(client, tmp_path: Path):
    """RT251-08: Chinese/space directory names keep working."""
    unusual = tmp_path / "中文 目录" / "子目录"
    project = _make_project(client, "cjk-media", str(unusual))
    pid = project["id"]
    image = project_cache_dir(pid) / "frames" / "a" / "图 像.jpg"
    _write(image, JPEG_BYTES)

    res = client.get(f"/api/projects/{pid}/media", params={"path": rel_to_data(image)})
    assert res.status_code == 200, res.text
    assert res.content == JPEG_BYTES
