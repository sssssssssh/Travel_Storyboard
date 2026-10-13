"""Audit P1-02 — the project ``output_dir`` must actually be used.

Acceptance: with an output dir configured, storyboard frames / proxies /
packages land there (not under ``<data>/projects/<id>``), and the source
footage is never written to.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from app.database import session_scope
from app.models import Asset
from app.paths import (
    check_output_dir_safety,
    default_project_root,
    forget_project_root,
    frames_dir,
    packages_dir,
    paths_overlap,
    project_cache_dir,
    proxies_dir,
    remember_project_root,
    roughcut_dir,
)


@pytest.fixture(autouse=True)
def _clear_root_cache():
    """Keep the process-wide root cache from leaking between tests."""
    yield


def _make_project(client, name: str, output_dir: str, source_roots=None) -> dict:
    res = client.post(
        "/api/projects",
        json={"name": name, "source_roots": source_roots or [], "output_dir": output_dir},
    )
    assert res.status_code in (200, 201), res.text
    return res.json()


def test_output_dir_is_reflected_in_cache_dir(client, tmp_path: Path):
    out = tmp_path / "work"
    project = _make_project(client, "out-dir", str(out))
    assert Path(project["cache_dir"]) == (out / project["id"]).resolve()


def test_default_location_when_output_dir_empty(client):
    project = _make_project(client, "no-out-dir", "")
    assert Path(project["cache_dir"]) == default_project_root(project["id"])


def test_artifact_dirs_follow_output_dir(client, tmp_path: Path):
    out = tmp_path / "media-root"
    project = _make_project(client, "artifacts", str(out))
    pid = project["id"]

    root = (out / pid).resolve()
    assert project_cache_dir(pid) == root
    assert frames_dir(pid, "asset-1") == root / "frames" / "asset-1"
    assert proxies_dir(pid) == root / "proxies"
    assert packages_dir(pid) == root / "packages"
    assert roughcut_dir(pid) == root / "roughcut"


def test_output_dir_change_moves_workspace(client, tmp_path: Path):
    first = tmp_path / "one"
    second = tmp_path / "two"
    project = _make_project(client, "moving", str(first))
    pid = project["id"]
    assert project_cache_dir(pid) == (first / pid).resolve()

    res = client.patch(f"/api/projects/{pid}", json={"output_dir": str(second)})
    assert res.status_code == 200, res.text
    assert Path(res.json()["cache_dir"]) == (second / pid).resolve()
    assert project_cache_dir(pid) == (second / pid).resolve()


def test_output_dir_matching_data_dir_is_rejected(client, tmp_path: Path):
    from app.paths import data_root

    res = client.post(
        "/api/projects",
        json={"name": "bad", "source_roots": [], "output_dir": str(data_root())},
    )
    assert res.status_code == 422
    assert "数据目录" in res.json()["detail"]


def test_output_dir_overlapping_source_root_is_rejected(client, tmp_path: Path):
    footage = tmp_path / "footage"
    footage.mkdir()
    res = client.post(
        "/api/projects",
        json={
            "name": "overlap",
            "source_roots": [str(footage)],
            "output_dir": str(footage / "derived"),
        },
    )
    assert res.status_code == 422
    assert "重叠" in res.json()["detail"]


def test_output_dir_containing_source_root_is_rejected(client, tmp_path: Path):
    """The output dir must not swallow the footage either."""
    outer = tmp_path / "outer"
    footage = outer / "footage"
    footage.mkdir(parents=True)
    res = client.post(
        "/api/projects",
        json={
            "name": "swallow",
            "source_roots": [str(footage)],
            "output_dir": str(outer),
        },
    )
    assert res.status_code == 422


def test_patch_to_overlapping_output_dir_is_rejected(client, tmp_path: Path):
    footage = tmp_path / "clips"
    footage.mkdir()
    project = _make_project(client, "patch-overlap", "", source_roots=[str(footage)])
    res = client.patch(
        f"/api/projects/{project['id']}", json={"output_dir": str(footage)}
    )
    assert res.status_code == 422


def test_sibling_dirs_are_allowed(client, tmp_path: Path):
    footage = tmp_path / "footage"
    footage.mkdir()
    project = _make_project(
        client, "siblings", str(tmp_path / "derived"), source_roots=[str(footage)]
    )
    assert project["cache_dir"]


def test_paths_overlap_semantics(tmp_path: Path):
    a = tmp_path / "a"
    b = a / "b"
    c = tmp_path / "c"
    assert paths_overlap(a, b) is True
    assert paths_overlap(b, a) is True
    assert paths_overlap(a, a) is True
    assert paths_overlap(a, c) is False


def test_check_output_dir_safety_is_noop_without_output_dir():
    check_output_dir_safety("", ["C:/footage"])
    check_output_dir_safety(None, ["C:/footage"])


def test_root_cache_is_forgotten_on_delete(client, tmp_path: Path):
    out = tmp_path / "temp-root"
    project = _make_project(client, "deletable", str(out))
    pid = project["id"]
    assert project_cache_dir(pid) == (out / pid).resolve()

    res = client.delete(f"/api/projects/{pid}")
    assert res.status_code == 204
    forget_project_root(pid)
    # Without a DB row the resolver falls back to the default location.
    assert project_cache_dir(pid) == default_project_root(pid)


def test_media_route_serves_artifact_from_custom_output_dir(client, tmp_path: Path):
    """A frame written under output_dir must still be servable (P0-01 + P1-02)."""
    out = tmp_path / "custom-out"
    project = _make_project(client, "serving", str(out))
    pid = project["id"]

    asset_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(
            Asset(
                id=asset_id,
                project_id=pid,
                source_path=str(tmp_path / "src" / "clip.mp4"),
                display_name="clip.mp4",
            )
        )

    frame_dir = frames_dir(pid, asset_id)
    frame_dir.mkdir(parents=True, exist_ok=True)
    jpg = frame_dir / "shot.jpg"
    jpg.write_bytes(b"\xff\xd8\xff\xe0jpeg")

    rel = jpg.resolve().as_posix()  # absolute, as rel_to_data stores it
    res = client.get(f"/api/projects/{pid}/media", params={"path": rel})
    assert res.status_code == 200, res.text
    assert res.headers["content-type"] == "image/jpeg"


def test_media_route_still_rejects_outside_artifacts_with_custom_root(
    client, tmp_path: Path
):
    """The whitelist must hold even when paths are absolute."""
    out = tmp_path / "guarded-out"
    project = _make_project(client, "guarded", str(out))
    pid = project["id"]

    root = (out / pid).resolve()
    root.mkdir(parents=True, exist_ok=True)
    secret = root / "secret.jpg"
    secret.write_bytes(b"\xff\xd8\xff\xe0nope")

    res = client.get(f"/api/projects/{pid}/media", params={"path": secret.as_posix()})
    assert res.status_code == 400

    # And a file outside the project root entirely.
    outside = tmp_path / "elsewhere.jpg"
    outside.write_bytes(b"\xff\xd8\xff\xe0nope")
    res = client.get(f"/api/projects/{pid}/media", params={"path": outside.as_posix()})
    assert res.status_code == 400


def test_relative_data_paths_still_resolve(client):
    """Projects without output_dir keep storing data-relative paths."""
    project = _make_project(client, "relative", "")
    pid = project["id"]
    frames = frames_dir(pid, "asset-r")
    frames.mkdir(parents=True, exist_ok=True)
    (frames / "f.jpg").write_bytes(b"\xff\xd8\xff\xe0jpeg")

    rel = f"projects/{pid}/frames/asset-r/f.jpg"
    res = client.get(f"/api/projects/{pid}/media", params={"path": rel})
    assert res.status_code == 200, res.text


def test_remember_and_resolve_roundtrip(tmp_path: Path):
    pid = str(uuid.uuid4())
    remember_project_root(pid, str(tmp_path / "out"))
    assert project_cache_dir(pid) == (tmp_path / "out" / pid).resolve()
    forget_project_root(pid)


@pytest.mark.parametrize('artifact', ['frames/a/f.jpg', 'packages/history.zip', 'roughcut/roughcut.mp4'])
def test_workspace_with_artifacts_cannot_change(client, tmp_path, artifact):
    project = _make_project(client, 'protected', str(tmp_path / 'old'))
    pid = project['id']
    path = project_cache_dir(pid) / artifact
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'preserved')
    response = client.patch(f'/api/projects/{pid}', json={'name': 'must-not-change', 'output_dir': str(tmp_path / 'new')})
    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'WORKSPACE_HAS_DATA'
    assert client.get(f'/api/projects/{pid}').json()['name'] == 'protected'
    assert project_cache_dir(pid) == Path(project['cache_dir'])
    assert client.get(f'/api/projects/{pid}/media', params={'path': str(path)}).content == b'preserved'
    assert client.patch(f'/api/projects/{pid}', json={'name': 'renamed'}).status_code == 200
    assert client.patch(f'/api/projects/{pid}', json={'output_dir': str(tmp_path / 'old' / '.')}).status_code == 200
