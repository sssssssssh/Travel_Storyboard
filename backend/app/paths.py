"""Filesystem path helpers for cached media artifacts.

Two distinct roots:
- ``data_path``      — global app data (SQLite DB, token.txt). **Never served.**
- ``project_cache_dir`` — per-project derived artifacts (frames, proxies,
  packages). This is the only tree that may be served over HTTP.

Serving is always authorized by a database row (frame id, package id, review
request id) and additionally confined to that row's project cache directory, so
a corrupted/forged relative path cannot reach the database or token file.
"""

from __future__ import annotations

from pathlib import Path

from .config import get_settings


def data_root() -> Path:
    return get_settings().data_path


def projects_root() -> Path:
    return data_root() / "projects"


def project_cache_dir(project_id: str) -> Path:
    return projects_root() / project_id


def frames_dir(project_id: str, asset_id: str) -> Path:
    return project_cache_dir(project_id) / "frames" / asset_id


def proxies_dir(project_id: str) -> Path:
    return project_cache_dir(project_id) / "proxies"


def packages_dir(project_id: str) -> Path:
    return project_cache_dir(project_id) / "packages"


def roughcut_dir(project_id: str) -> Path:
    return project_cache_dir(project_id) / "roughcut"


def staging_dir(project_id: str, asset_id: str) -> Path:
    """Per-run staging area for frames; swapped in atomically on success."""
    return project_cache_dir(project_id) / "staging" / asset_id


def rel_to_data(path: Path) -> str:
    """Return a path relative to the data dir (posix form) for storage."""
    try:
        return path.resolve().relative_to(data_root()).as_posix()
    except ValueError:
        return path.as_posix()


def _contained(base: Path, candidate: Path) -> bool:
    if candidate == base:
        return True
    return base in candidate.parents


def resolve_from_data(rel: str) -> Path:
    """Resolve a stored relative path, guaranteeing it stays inside data_dir."""
    base = data_root().resolve()
    candidate = (base / rel).resolve()
    if not _contained(base, candidate):
        raise ValueError(f"Path escapes data dir: {rel}")
    return candidate


def resolve_in_project(project_id: str, rel: str) -> Path:
    """Resolve a stored relative path and require it to sit inside *this*
    project's cache directory. Used by every media-serving route.
    """
    base = project_cache_dir(project_id).resolve()
    candidate = resolve_from_data(rel)
    if not _contained(base, candidate):
        raise ValueError("Path is outside the project cache directory")
    return candidate
