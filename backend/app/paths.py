"""Filesystem path helpers for cached media artifacts."""

from __future__ import annotations

from pathlib import Path

from .config import get_settings


def project_cache_dir(project_id: str) -> Path:
    return get_settings().data_path / "projects" / project_id


def frames_dir(project_id: str, asset_id: str) -> Path:
    return project_cache_dir(project_id) / "frames" / asset_id


def rel_to_data(path: Path) -> str:
    """Return a path relative to the data dir (posix form) for storage."""
    try:
        return path.resolve().relative_to(get_settings().data_path).as_posix()
    except ValueError:
        return path.as_posix()


def resolve_from_data(rel: str) -> Path:
    """Resolve a stored relative path, guaranteeing it stays inside data_dir."""
    base = get_settings().data_path
    candidate = (base / rel).resolve()
    if base not in candidate.parents and candidate != base:
        raise ValueError(f"Path escapes data dir: {rel}")
    return candidate
