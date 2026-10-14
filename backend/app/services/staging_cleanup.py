"""Strictly-scoped staging cleanup (TSB-262).

A task's staging directory holds work-in-progress: sampled frames, encoded
clips, a proxy in flight. When a run ends — successfully, cancelled, timed out
or crashed — that directory must be collected. It must also be collected
*before* the worker releases the project lock, so the next job never sees a
half-written stage.

The danger in a cleanup helper is not failing to delete: it is deleting the
wrong thing. So the boundary here is positive rather than negative. A path is
only accepted when all of these hold:

1. it resolves to a real directory (not a file, not a missing path);
2. it is strictly inside the **owning project's** cache directory;
3. its first component under that root is one of the known staging shapes
   (``roughcut/staging-*``, ``proxies/staging/<token>``,
   ``staging/<asset>/<token>``) — never a published output directory;
4. the *resolved* path is still inside the project root, so a symlink or
   junction cannot redirect the delete somewhere else.

Anything else is refused with ``STAGING_NOT_OWNED`` rather than guessed at.

Failure handling: ``shutil.rmtree(ignore_errors=True)`` is explicitly not
acceptable evidence that cleanup happened, because it reports success either
way. This module returns a :class:`CleanupResult` that says what was removed,
what is left, and whether a retry is worth attempting.
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path

from ..ffmpeg import long_path, safe_is_file, safe_stat, safe_unlink
from ..paths import project_cache_dir

logger = logging.getLogger(__name__)

ERR_NOT_OWNED = "STAGING_NOT_OWNED"
ERR_CLEANUP_FAILED = "CLEANUP_FAILED"

#: Directory names directly under the project root that this module may enter.
#: Deliberately excludes ``frames``, ``proxies`` (the published root),
#: ``packages``, ``contact_sheets`` and ``roughcut`` itself.
_ALLOWED_PARENTS = ("staging", "roughcut", "proxies")

#: Directories under the project root that are published artifacts, never
#: staging. Refused even when named explicitly.
_PUBLISHED = {
    "frames", "packages", "contact_sheets", "proxies", "roughcut", "staging",
}


@dataclass
class CleanupResult:
    """Outcome of one staging cleanup, with enough detail to be traceable."""

    path: str
    ok: bool
    error_code: str = ""
    message: str = ""
    removed_files: int = 0
    removed_bytes: int = 0
    residual_paths: list[str] = field(default_factory=list)
    retryable: bool = False

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "ok": self.ok,
            "error_code": self.error_code,
            "message": self.message,
            "removed_files": self.removed_files,
            "removed_bytes": self.removed_bytes,
            "residual_paths": list(self.residual_paths),
            "retryable": self.retryable,
        }


def _is_link(path: Path) -> bool:
    """Whether *path* is a symlink/junction rather than a real directory.

    This is the check that keeps a delete inside the project. Following a link
    out of the tree would let a stray junction in staging reach an arbitrary
    directory and delete it — the single most dangerous failure mode this module
    can have. ``is_symlink`` covers POSIX symlinks; Windows directory junctions
    need the reparse-point attribute.
    """
    try:
        if path.is_symlink():
            return True
    except OSError:
        pass
    if os.name != "nt":
        return False
    try:
        stat_result = os.lstat(long_path(path))
    except OSError:
        return False
    FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    return bool(getattr(stat_result, "st_file_attributes", 0)
                & FILE_ATTRIBUTE_REPARSE_POINT)


def _listdir(path: Path) -> list[Path]:
    """List *path*, falling back to the extended-length form for deep trees.

    Order matters: on Windows a >MAX_PATH directory raises
    ``FileNotFoundError`` (WinError 3) from the plain call — it does **not**
    raise a distinct "path too long" error. Catching that as "empty" would make
    a real staging directory look like it had nothing in it, so cleanup would
    report success while deleting nothing. The extended-length attempt is
    therefore made before concluding the directory is empty.
    """
    try:
        return list(path.iterdir())
    except OSError:
        pass
    try:
        # `os.listdir` returns bare names: re-attach the directory so callers
        # always receive usable paths, not children of the process CWD.
        return [path / entry for entry in os.listdir(long_path(path))]
    except OSError:
        return []


def _iter_tree(root: Path) -> list[Path]:
    """Every file under *root*, using a walk that tolerates long paths.

    ``Path.rglob`` relies on ``os.scandir`` with the plain path, which fails on
    Windows for >MAX_PATH trees; the extended-length form is used instead.

    Reparse points (symlinks/junctions) are **not descended into**: the link
    itself is returned as an entry so it can be unlinked, but its target is left
    untouched. Cleanup must never delete through a link that escapes the project.
    """
    files: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        entries = _listdir(current)
        for entry in entries:
            link = _is_link(entry)
            try:
                is_dir = entry.is_dir() and not link
            except OSError:
                is_dir = False
            if is_dir:
                stack.append(entry)
            else:
                files.append(entry)
    return files


def count_tree(root: Path) -> tuple[int, int]:
    """Return ``(file_count, total_bytes)`` for *root* without deleting."""
    if not _is_dir(root):
        return 0, 0
    files = _iter_tree(root)
    total = 0
    for f in files:
        try:
            total += safe_stat(f).st_size
        except OSError:
            pass
    return len(files), total


def _is_dir(path: Path) -> bool:
    """Whether *path* is a directory, tolerating Windows' MAX_PATH limit.

    ``Path.is_dir()`` routes through ``os.stat`` on the *plain* string, so it
    returns False for a >260-character directory that genuinely exists. The
    extended-length form is used as a fallback before giving up.
    """
    try:
        if path.is_dir():
            return True
    except OSError:
        pass
    try:
        return stat.S_ISDIR(os.stat(long_path(path)).st_mode)
    except OSError:
        return False


def _exists(path: Path) -> bool:
    """Whether *path* exists at all, tolerating Windows' MAX_PATH limit."""
    try:
        if path.exists():
            return True
    except OSError:
        pass
    try:
        os.stat(long_path(path))
        return True
    except OSError:
        return False


def _resolve(path: Path) -> Path | None:
    """Resolve *path* (following links), or ``None`` when unusable."""
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        # A very long path may defeat resolve(); fall back to a long-path form.
        try:
            return Path(long_path(path)).resolve()
        except (OSError, RuntimeError):
            return None


def _within(base: Path, candidate: Path) -> bool:
    return candidate == base or base in candidate.parents


def _is_staging_shape(root: Path, stage: Path) -> bool:
    """Whether *stage* matches one of the known per-task staging layouts."""
    try:
        rel = stage.relative_to(root)
    except ValueError:
        return False
    parts = rel.parts
    if not parts:
        return False  # the project root itself
    head = parts[0]
    if head not in _ALLOWED_PARENTS:
        return False
    if head in _PUBLISHED and len(parts) == 1:
        # e.g. `<root>/roughcut` or `<root>/proxies` — a published output dir.
        return False
    return True


def _unlink_dir_link(path: Path) -> bool:
    """Remove a directory symlink/junction without touching its target."""
    try:
        os.rmdir(long_path(path))
        return True
    except OSError:
        pass
    try:
        os.unlink(long_path(path))
        return True
    except OSError:
        pass
    if os.name == "nt":
        # A junction may need `rmdir` through the shell on some Windows builds.
        try:
            import subprocess

            proc = subprocess.run(["cmd", "/c", "rmdir", str(path)],
                                  capture_output=True)
            return proc.returncode == 0
        except OSError:
            return False
    return False


def cleanup_owned_staging(
    project_id: str,
    stage: str | Path,
    *,
    owner_job_id: str | None = None,
) -> CleanupResult:
    """Delete this task's staging directory, refusing anything out of scope.

    ``owner_job_id`` is recorded for traceability (which run created the
    directory). Returns a :class:`CleanupResult`; never raises for a refusal or
    a partial failure, because callers run this from ``finally`` blocks where an
    exception would mask the original error.
    """
    stage_path = Path(stage)
    result = CleanupResult(path=str(stage_path), ok=False)

    root = _resolve(project_cache_dir(project_id))
    resolved = _resolve(stage_path)
    if root is None or resolved is None:
        result.error_code = ERR_NOT_OWNED
        result.message = "无法解析暂存目录或项目工作区路径"
        return result

    # 1. strictly inside this project's cache dir (the root itself is refused).
    if resolved == root or not _within(root, resolved):
        result.error_code = ERR_NOT_OWNED
        result.message = f"暂存目录不在项目工作区内：{stage_path}"
        logger.warning(
            "Refusing to clean staging outside project %s: %s (job=%s)",
            project_id, stage_path, owner_job_id,
        )
        return result

    # 2. a known staging shape, never a published artifact directory.
    if not _is_staging_shape(root, resolved):
        result.error_code = ERR_NOT_OWNED
        result.message = f"不是本任务可清理的暂存目录：{stage_path}"
        logger.warning(
            "Refusing to clean non-staging path in project %s: %s (job=%s)",
            project_id, stage_path, owner_job_id,
        )
        return result

    # 3. nothing to do (already collected, or never created). A link is not a
    #    missing path: refuse it rather than deleting through it.
    if _is_link(resolved) and not _is_link(stage_path):
        result.error_code = ERR_NOT_OWNED
        result.message = "暂存路径是指向项目外的链接"
        logger.warning(
            "Refusing to clean staging reached through a link: %s (job=%s)",
            stage_path, owner_job_id,
        )
        return result
    if not _is_dir(resolved):
        if _exists(resolved):
            result.error_code = ERR_NOT_OWNED
            result.message = "暂存路径不是目录"
            return result
        result.ok = True
        result.message = "暂存目录不存在，无需清理"
        return result

    files, total = count_tree(resolved)

    # 4. delete bottom-up so parents are empty when removed. Files first (and
    #    with safe_unlink, which understands long paths), then directories.
    failures: list[str] = []
    removed = 0
    removed_bytes = 0
    known_files = _iter_tree(resolved)
    sizes: dict[Path, int] = {}
    for f in known_files:
        try:
            sizes[f] = safe_stat(f).st_size
        except OSError:
            sizes[f] = 0

    for f in known_files:
        try:
            safe_unlink(f)
            removed += 1
            removed_bytes += sizes[f]
        except FileNotFoundError:
            removed += 1
            removed_bytes += sizes[f]
        except OSError as exc:
            failures.append(f"{f}: {exc}")
            logger.warning("Could not remove staging file %s: %s", f, exc)

    # Remove the (now hopefully empty) directories, deepest first. Links are
    # unlinked, never descended into — a junction must not become a doorway out
    # of the project.
    dirs: list[Path] = []
    stack = [resolved]
    while stack:
        current = stack.pop()
        dirs.append(current)
        for entry in _listdir(current):
            try:
                if entry.is_dir() and not _is_link(entry):
                    stack.append(entry)
            except OSError:
                continue
    for d in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
        try:
            if _is_link(d):
                # Remove the link itself; its target is not ours to delete.
                if d.is_dir() and not _unlink_dir_link(d):
                    failures.append(f"{d}: 无法删除目录链接")
                    logger.warning("Could not remove staging directory link %s", d)
                continue
            shutil.rmtree(long_path(d), ignore_errors=False)
        except FileNotFoundError:
            continue
        except OSError as exc:
            if _exists(d):
                failures.append(f"{d}: {exc}")
                logger.warning("Could not remove staging dir %s: %s", d, exc)

    # Honest accounting: a failed per-file unlink can still be swept away by the
    # directory removal (Windows removes a closed file regardless of the earlier
    # error), so "removed" must be measured against what is actually left rather
    # than inferred from which calls raised. Otherwise a run where every unlink
    # failed — because the files were locked — would report `ok=True, 0 removed`.
    still_there = [f for f in known_files if _exists(f)]
    if still_there and not _exists(resolved):
        # The tree is gone; the individual probes lied (e.g. the path is now too
        # long to stat). Trust the tree-level result.
        still_there = []
    removed = len(known_files) - len(still_there)
    removed_bytes = sum(sizes[f] for f in known_files if f not in still_there)

    result.removed_files = removed
    result.removed_bytes = removed_bytes

    if _exists(resolved):
        residual = _iter_tree(resolved) if _is_dir(resolved) else [resolved]
        result.residual_paths = [str(p) for p in residual[:50]]
        result.ok = False
        result.error_code = ERR_CLEANUP_FAILED
        result.retryable = True
        result.message = (
            f"暂存目录未能完全删除（剩余 {len(result.residual_paths)} 项）"
        )
        logger.error(
            "Staging cleanup incomplete for project %s: %s (job=%s) — %d residual item(s): %s",
            project_id, stage_path, owner_job_id, len(result.residual_paths),
            failures[:5],
        )
        return result

    result.ok = True
    result.message = f"已清理暂存目录（{removed} 个文件，{removed_bytes} 字节）"
    logger.info(
        "Cleaned staging for project %s: %s (%d files, %d bytes, job=%s)",
        project_id, stage_path, removed, removed_bytes, owner_job_id,
    )
    return result


def cleanup_staging_or_log(
    project_id: str,
    stage: str | Path,
    *,
    owner_job_id: str | None = None,
) -> CleanupResult:
    """``cleanup_owned_staging`` that also records failures on the job.

    Callers in ``finally`` blocks want the cleanup attempted and any residual
    reported to the user, but must not raise over the original exception. The
    result is mirrored into ``job.stats['cleanup']`` so a partially-cleaned run
    is visible in the UI instead of silently leaving bytes behind.
    """
    result = cleanup_owned_staging(project_id, stage, owner_job_id=owner_job_id)
    if not result.ok and owner_job_id:
        try:
            from .runner import merge_job_stats

            merge_job_stats(owner_job_id, {"cleanup": result.as_dict()})
        except Exception:  # noqa: BLE001 - cleanup reporting must never raise
            logger.warning(
                "Could not record cleanup failure for job %s", owner_job_id,
                exc_info=True,
            )
    return result


__all__ = [
    "CleanupResult",
    "ERR_CLEANUP_FAILED",
    "ERR_NOT_OWNED",
    "cleanup_owned_staging",
    "cleanup_staging_or_log",
    "count_tree",
]
