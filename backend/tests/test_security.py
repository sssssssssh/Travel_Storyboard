"""Audit P0-01 — local auth boundary, media whitelist, loopback origin checks."""

from __future__ import annotations

import pytest

from app.paths import project_cache_dir


@pytest.fixture()
def project(client):
    res = client.post("/api/projects", json={"name": "security-test", "source_roots": []})
    assert res.status_code in (200, 201), res.text
    return res.json()


def test_token_endpoint_does_not_exist(client):
    """The token must never be disclosed by an unauthenticated endpoint."""
    res = client.get("/api/system/token")
    assert res.status_code == 404


def test_api_requires_token(client):
    res = client.get("/api/projects", headers={"X-Access-Token": "definitely-wrong"})
    assert res.status_code == 401


def test_health_and_config_are_reachable_without_token(client):
    """Liveness/config stay exempt but leak nothing sensitive."""
    for path in ("/api/system/health", "/api/system/config"):
        res = client.get(path, headers={"X-Access-Token": ""})
        assert res.status_code == 200, path
        assert "token" not in res.text.lower()


def test_exempt_endpoints_do_not_disclose_filesystem_layout(client):
    """These two routes need no token, so they must not reveal local paths.

    Regression guard: `/system/health` used to return the absolute ffmpeg path
    and `/system/config` the absolute data directory.
    """
    import re

    from app.paths import data_root

    for path in ("/api/system/health", "/api/system/config"):
        body = client.get(path, headers={"X-Access-Token": ""}).text
        # No Windows drive letters, no POSIX absolute roots.
        assert not re.search(r"[A-Za-z]:[\\/]", body), f"{path} leaked a drive path: {body}"
        assert str(data_root()) not in body, f"{path} leaked the data directory"
        assert "\\\\" not in body, f"{path} leaked a UNC path: {body}"
        # No usernames either.
        assert "Users" not in body and "home" not in body.lower(), body


def test_health_reports_tool_names_without_directories(client):
    body = client.get("/api/system/health", headers={"X-Access-Token": ""}).json()
    for key in ("ffmpeg", "ffprobe"):
        value = body.get(key)
        if value:
            assert "/" not in value and "\\" not in value and ":" not in value, value


def test_database_file_is_not_downloadable(client, project):
    """`path=` must not reach files outside the project cache."""
    res = client.get(
        f"/api/projects/{project['id']}/media", params={"path": "travel_storyboard.db"}
    )
    assert res.status_code == 400


def test_token_file_is_not_downloadable(client, project):
    res = client.get(f"/api/projects/{project['id']}/media", params={"path": "token.txt"})
    assert res.status_code == 400


@pytest.mark.parametrize(
    "rel",
    [
        "projects/{pid}/../../../token.txt",
        "projects/{pid}/../../travel_storyboard.db",
        "projects/other-project/frames/x.jpg",
        "projects/{pid}/secrets/leak.jpg",
    ],
)
def test_media_rejects_escapes_and_foreign_projects(client, project, rel):
    path = rel.format(pid=project["id"])
    res = client.get(f"/api/projects/{project['id']}/media", params={"path": path})
    assert res.status_code in (400, 404), res.text


def test_media_rejects_disallowed_extension(client, project):
    """Even inside the cache, only whitelisted media types are served."""
    frames = project_cache_dir(project["id"]) / "frames" / "asset-x"
    frames.mkdir(parents=True, exist_ok=True)
    evil = frames / "payload.exe"
    evil.write_bytes(b"MZ")
    res = client.get(
        f"/api/projects/{project['id']}/media",
        params={"path": f"projects/{project['id']}/frames/asset-x/payload.exe"},
    )
    assert res.status_code == 400


def test_media_serves_whitelisted_artifact(client, project):
    frames = project_cache_dir(project["id"]) / "frames" / "asset-y"
    frames.mkdir(parents=True, exist_ok=True)
    good = frames / "ok.jpg"
    good.write_bytes(b"\xff\xd8\xff\xe0jpeg")
    res = client.get(
        f"/api/projects/{project['id']}/media",
        params={"path": f"projects/{project['id']}/frames/asset-y/ok.jpg"},
    )
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/jpeg"


def test_cross_site_origin_is_rejected(client):
    res = client.get("/api/projects", headers={"Origin": "http://evil.example.com"})
    assert res.status_code == 403


def test_cross_site_referer_is_rejected(client):
    res = client.get("/api/projects", headers={"Referer": "http://evil.example.com/page"})
    assert res.status_code == 403


def test_non_loopback_host_is_rejected(client):
    res = client.get("/api/projects", headers={"Host": "evil.example.com"})
    assert res.status_code == 403


@pytest.mark.parametrize("host", ["127.0.0.1:8737", "localhost", "localhost:8737"])
def test_loopback_hosts_are_allowed(client, host):
    res = client.get("/api/projects", headers={"Host": host})
    assert res.status_code == 200


def test_loopback_origin_is_allowed(client):
    res = client.get("/api/projects", headers={"Origin": "http://127.0.0.1:8737"})
    assert res.status_code == 200
