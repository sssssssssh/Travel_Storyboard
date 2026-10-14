"""Filesystem path helpers for cached media artifacts.

Three distinct roots:

- ``data_path``          — global app data (SQLite DB, ``token.txt``). **Never served.**
- ``project_cache_dir``  — per-project derived artifacts (frames, proxies,
  packages, rough cut). This is the only tree that may be served over HTTP.
- ``source_roots``       — the user's original footage. **Read-only, never written.**

Where the per-project tree lives
--------------------------------
By default it is ``<data>/projects/<id>``. If a project sets ``output_dir``
(audit P1-02) the tree moves to ``<output_dir>/<project-id>`` so users can put
derived artifacts on a larger disk. Because ``project_cache_dir()`` is called
from worker threads and hot loops, the ``project_id -> root`` mapping is cached
in-process and invalidated whenever a project is created/updated/deleted.

Serving is always authorized by a database row (frame id, package id, review
request id) and additionally confined to that row's project cache directory, so
a corrupted/forged relative path cannot reach the database or token file.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path

from .config import get_settings

logger = logging.getLogger(__name__)

# project_id -> resolved cache root. Guarded by a lock; small and bounded by the
# number of projects.
_ROOTS: dict[str, Path] = {}
_ROOTS_LOCK = threading.Lock()


def data_root() -> Path:
    return get_settings().data_path


def projects_root() -> Path:
    return data_root() / "projects"


def default_project_root(project_id: str) -> Path:
    """The cache root used when a project has no ``output_dir``."""
    return projects_root() / project_id


def remember_project_root(project_id: str, output_dir: str | None) -> Path:
    """Resolve and cache the cache root for a project.

    ``output_dir`` is the user-configured workspace (may be empty/None). The
    project id is always appended so several projects can share one output drive
    without colliding.
    """
    root = project_root_for(project_id, output_dir)
    with _ROOTS_LOCK:
        _ROOTS[project_id] = root
    return root


def forget_project_root(project_id: str) -> None:
    with _ROOTS_LOCK:
        _ROOTS.pop(project_id, None)


def project_root_for(project_id: str, output_dir: str | None) -> Path:
    raw = (output_dir or "").strip()
    if not raw:
        return default_project_root(project_id)
    try:
        base = Path(os.path.expandvars(raw)).expanduser().resolve()
    except (OSError, RuntimeError):
        logger.warning("Ignoring unusable output_dir %r", output_dir, exc_info=True)
        return default_project_root(project_id)
    return base / project_id


def project_cache_dir(project_id: str) -> Path:
    """Cache root for a project, consulting ``output_dir`` once per process.

    Falls back to the default location if the project row is unavailable (the
    worker may briefly race a deletion), which keeps callers total.
    """
    with _ROOTS_LOCK:
        cached = _ROOTS.get(project_id)
    if cached is not None:
        return cached

    output_dir: str | None = None
    try:
        from .database import session_scope
        from .models import Project

        with session_scope() as s:
            project = s.get(Project, project_id)
            if project is not None:
                output_dir = project.output_dir
    except Exception:  # noqa: BLE001 - never let path resolution crash a job
        logger.warning("Could not read output_dir for project %s", project_id, exc_info=True)

    return remember_project_root(project_id, output_dir)


def frames_dir(project_id: str, asset_id: str) -> Path:
    return project_cache_dir(project_id) / "frames" / asset_id


def proxies_dir(project_id: str) -> Path:
    return project_cache_dir(project_id) / "proxies"


def packages_dir(project_id: str) -> Path:
    return project_cache_dir(project_id) / "packages"


def roughcut_dir(project_id: str) -> Path:
    return project_cache_dir(project_id) / "roughcut"


def contact_sheets_dir(project_id: str) -> Path:
    return project_cache_dir(project_id) / "contact_sheets"


def staging_dir(project_id: str, asset_id: str) -> Path:
    """Per-run staging area for frames; swapped in atomically on success.

    The run directory is added by the caller. Paths here are deliberately short:
    Windows still enforces a 260-character limit by default, and the full UUIDs
    already live in the database, so repeating them in every filename only
    pushed real projects over that limit.
    """
    return project_cache_dir(project_id) / "staging" / asset_id


# --- storing / resolving relative references ---------------------------------

def rel_to_data(path: Path) -> str:
    """Return a path relative to the data dir (posix form) for storage.

    Artifacts outside the default data dir (i.e. under a project ``output_dir``)
    are returned as an absolute posix path; ``resolve_from_data`` understands
    both forms and still confines them to the owning project.
    """
    resolved = path.resolve()
    try:
        return resolved.relative_to(data_root().resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _contained(base: Path, candidate: Path) -> bool:
    if candidate == base:
        return True
    return base in candidate.parents


def resolve_from_data(rel: str) -> Path:
    """Resolve a stored reference that is relative to the data dir.

    Absolute references only occur for projects with a custom ``output_dir``;
    those must go through :func:`resolve_in_project`, which confines them to the
    owning project. Calling this with an absolute path is therefore rejected —
    it keeps the data-dir guarantee unconditional for direct callers.
    """
    if not rel:
        raise ValueError("Empty path")

    candidate = Path(rel)
    if candidate.is_absolute():
        raise ValueError("Absolute paths must be resolved within a project")

    base = data_root().resolve()
    resolved = (base / rel).resolve()
    if not _contained(base, resolved):
        raise ValueError(f"Path escapes data dir: {rel}")
    return resolved


def resolve_in_project(project_id: str, rel: str) -> Path:
    """Resolve a stored reference and require it to sit inside *this* project's
    cache directory. Used by every media-serving route.

    Handles both storage forms: data-dir-relative (default location) and
    absolute (custom ``output_dir``).
    """
    if not rel:
        raise ValueError("Empty path")

    base = project_cache_dir(project_id).resolve()
    candidate = Path(rel)
    resolved = candidate.resolve() if candidate.is_absolute() else resolve_from_data(rel)

    if not _contained(base, resolved):
        raise ValueError("Path is outside the project cache directory")
    return resolved


# --- overlap protection ------------------------------------------------------

def _norm(p: Path) -> Path:
    try:
        return p.resolve()
    except (OSError, RuntimeError):
        return p.absolute()


def paths_overlap(a: Path, b: Path) -> bool:
    """True when one path is the other, or one contains the other."""
    na, nb = _norm(a), _norm(b)
    return _contained(na, nb) or _contained(nb, na)


def check_output_dir_safety(output_dir: str | None, source_roots: list[str] | None) -> None:
    """Reject an output dir that would write into the user's footage.

    Raises ``ValueError`` with a user-facing message. A no-op when no
    ``output_dir`` is configured.
    """
    raw = (output_dir or "").strip()
    if not raw:
        return

    out = _norm(Path(os.path.expandvars(raw)).expanduser())
    if out == _norm(data_root()):
        raise ValueError("输出目录不能等于应用数据目录，请另选一个目录。")

    for root in source_roots or []:
        if not str(root).strip():
            continue
        src = _norm(Path(os.path.expandvars(str(root))).expanduser())
        if paths_overlap(out, src):
            raise ValueError(
                f"输出目录与素材目录重叠（{out} ↔ {src}）。"
                "请把产物写到素材之外的目录，避免覆盖原片。"
            )
