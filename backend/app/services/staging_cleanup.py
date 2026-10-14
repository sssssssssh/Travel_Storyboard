"""TSB-272: create-only ownership records and handle-confined stage deletion.

The registry is deliberately process-local. An old crash orphan is never
adopted by passing a job ID; aged frame reclamation has its own narrower path.
"""
from __future__ import annotations

import logging
import os
import stat
from contextlib import contextmanager, ExitStack
from dataclasses import dataclass, field, asdict
from pathlib import Path
from threading import RLock

from ..ffmpeg import long_path, safe_mkdir, safe_unlink
from ..paths import project_cache_dir

logger = logging.getLogger(__name__)
ERR_NOT_OWNED = "STAGING_NOT_OWNED"
ERR_CLEANUP_FAILED = "CLEANUP_FAILED"


@dataclass
class CleanupResult:
    path: str
    ok: bool
    error_code: str = ""
    message: str = ""
    removed_files: int = 0
    removed_bytes: int = 0
    residual_paths: list[str] = field(default_factory=list)
    retryable: bool = False
    residual_bytes: int | None = None
    residual_count: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class StageOwner:
    project_id: str
    job_id: str | None
    identity: tuple[int, int]
    # Every ancestor inside the project is pinned to the creator's identity.
    ancestors: tuple[tuple[int, int], ...]


_OWNERS: dict[Path, StageOwner] = {}
_GUARD = RLock()


class StagingNotOwned(RuntimeError):
    pass


def _lstat(path: Path):
    return os.lstat(long_path(path))


def _is_link(path: Path) -> bool:
    info = _lstat(path)  # Unreadability must not become "not a link".
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def _identity(info) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _listdir(path: Path) -> list[Path]:
    return [path / name for name in os.listdir(long_path(path))]


def _iter_tree(root: Path) -> list[Path]:
    files, stack = [], [root]
    while stack:
        current = stack.pop()
        if _is_link(current): raise StagingNotOwned('暂存目录含链接')
        for entry in _listdir(current):
            info = _lstat(entry)
            if _is_link(entry): raise StagingNotOwned('暂存目录含链接')
            if stat.S_ISDIR(info.st_mode): stack.append(entry)
            elif stat.S_ISREG(info.st_mode): files.append(entry)
            else: raise StagingNotOwned('暂存目录含非常规文件')
    return files


def count_tree(root: Path) -> tuple[int, int]:
    try: files = _iter_tree(root)
    except FileNotFoundError: return 0, 0
    return len(files), sum(_lstat(path).st_size for path in files)


def _is_staging_shape(root: Path, stage: Path) -> bool:
    try: p = stage.relative_to(root).parts
    except ValueError: return False
    return bool(
        len(p) == 2 and p[0] == 'roughcut' and p[1].startswith('staging-') and p[1][8:]
        or len(p) == 3 and p[:2] == ('proxies', 'staging') and p[2]
        or len(p) == 3 and p[0] == 'staging' and p[1] and p[2]
    )


def _raw_target(project_id: str, value: str | Path) -> tuple[Path, Path]:
    # Strings are checked BEFORE Path normalization can erase '.' / empty parts.
    text = os.fspath(value)
    normalized = text.replace('\\', '/') if os.name == 'nt' else text
    components = normalized.split('/')
    if normalized.startswith('/'): components = components[1:]
    if os.name == 'nt' and normalized.startswith('//'): components = components[1:]
    if any(part in ('', '.', '..') for part in components):
        raise StagingNotOwned('暂存路径含非规范相对段')
    root = project_cache_dir(project_id)
    stage = Path(text)
    if not stage.is_absolute(): stage = root / stage
    if not _is_staging_shape(root, stage): raise StagingNotOwned('不是精确的任务暂存路径')
    # No resolve() here: it could turn an alias into somebody else's stage.
    if _is_link(root) or not stat.S_ISDIR(_lstat(root).st_mode):
        raise StagingNotOwned('项目工作区不是有效真实目录')
    return root, stage


def _chain(root: Path, stage: Path) -> list[Path]:
    rel = stage.relative_to(root)
    return [root, *[root.joinpath(*rel.parts[:i]) for i in range(1, len(rel.parts) + 1)]]


def _validate_chain(root: Path, stage: Path, owner: StageOwner | None = None,
                    *, include_target: bool = True):
    chain = _chain(root, stage)
    if not include_target: chain = chain[:-1]
    ids = []
    for entry in chain:
        if _is_link(entry) or not stat.S_ISDIR(_lstat(entry).st_mode):
            raise StagingNotOwned('目录链含链接或无效目录')
        ids.append(_identity(_lstat(entry)))
    if owner:
        expected = (*owner.ancestors, owner.identity) if include_target else owner.ancestors
        if tuple(ids) != tuple(expected): raise StagingNotOwned('目录身份已改变')
    return tuple(ids)


def _check_creator(project_id: str, stage: Path, job_id: str | None):
    from ..database import session_scope
    from ..models import Job, Asset, Project
    with session_scope() as s:
        if s.get(Project, project_id) is None: raise StagingNotOwned('项目不存在')
        if job_id:
            job = s.get(Job, job_id)
            if job is None or job.project_id != project_id:
                raise StagingNotOwned('创建任务不属于此项目')
        if stage.relative_to(project_cache_dir(project_id)).parts[0] == 'staging':
            asset_id = stage.relative_to(project_cache_dir(project_id)).parts[1]
            asset = s.get(Asset, asset_id)
            if asset is None or asset.project_id != project_id:
                raise StagingNotOwned('素材不属于此项目')


@contextmanager
def pinned_directory(path: Path):
    """No-follow directory handle; Windows disallows ancestor rename/delete."""
    if os.name != 'nt':
        try:
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except (NotADirectoryError, OSError):
            if _is_link(path): raise StagingNotOwned('拒绝链接目录') from None
            raise
        try: yield fd
        finally: os.close(fd)
    else:
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        # OPEN_EXISTING | BACKUP_SEMANTICS | OPEN_REPARSE_POINT, share read/write
        handle = kernel.CreateFileW(long_path(path), 0, 3, None, 3, 0x02200000, None)
        if handle == wintypes.HANDLE(-1).value: raise ctypes.WinError(ctypes.get_last_error())
        try:
            if _is_link(path): raise StagingNotOwned('拒绝打开链接目录')
            yield None
        finally: kernel.CloseHandle(handle)


def create_owned_staging(project_id: str, stage: str | Path, *, owner_job_id: str | None = None) -> Path:
    """Only a newly-created directory can be registered, never an existing one."""
    root = project_cache_dir(project_id)
    try:
        if _is_link(root): raise StagingNotOwned('工作区是链接')
    except FileNotFoundError:
        safe_mkdir(root)
    root, target = _raw_target(project_id, stage)
    _check_creator(project_id, target, owner_job_id)
    with _GUARD:
        # Build only the known parent chain, checking every existing component.
        safe_mkdir(root)
        for parent in _chain(root, target)[1:-1]:
            try: os.mkdir(long_path(parent))
            except FileExistsError: pass
            _validate_chain(root, parent)
        with ExitStack() as stack:
            handles = [stack.enter_context(pinned_directory(parent)) for parent in _chain(root, target)[:-1]]
            _validate_chain(root, target, include_target=False)
            # exist_ok=False is the ownership proof; an old run cannot be adopted.
            if handles[-1] is not None: os.mkdir(target.name, dir_fd=handles[-1])
            else: os.mkdir(long_path(target))
            ids = _validate_chain(root, target)
            _OWNERS[target] = StageOwner(project_id, owner_job_id, ids[-1], ids[:-1])
    return target


def _remove_file(path: Path, fd=None):
    if fd is not None: os.unlink(path.name, dir_fd=fd)
    else: safe_unlink(path, missing_ok=False)


def _remove_contents(path: Path, fd, result: CleanupResult):
    names = os.listdir(fd) if fd is not None else os.listdir(long_path(path))
    for name in names:
        entry = path / name
        info = os.stat(name, dir_fd=fd, follow_symlinks=False) if fd is not None else _lstat(entry)
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise StagingNotOwned('删除前发现链接替换')
        if stat.S_ISDIR(info.st_mode):
            if fd is not None:
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try: _remove_contents(entry, child, result)
                finally: os.close(child)
                current = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if _identity(current) != _identity(info): raise StagingNotOwned('目录被替换')
                os.rmdir(name, dir_fd=fd)
            else:
                with pinned_directory(entry):
                    if _identity(_lstat(entry)) != _identity(info): raise StagingNotOwned('目录被替换')
                    _remove_contents(entry, None, result)
                os.rmdir(long_path(entry))  # no recursive fallback / no link following
        elif stat.S_ISREG(info.st_mode):
            _remove_file(entry, fd)
            result.removed_files += 1
            result.removed_bytes += info.st_size
        else: raise StagingNotOwned('拒绝非常规文件')


def cleanup_owned_staging(project_id: str, stage: str | Path, *, owner_job_id: str | None = None) -> CleanupResult:
    result = CleanupResult(path=str(stage), ok=False)
    try:
        root, target = _raw_target(project_id, stage)
        with _GUARD:
            owner = _OWNERS.get(target)
            if not owner or (owner.project_id, owner.job_id) != (project_id, owner_job_id):
                raise StagingNotOwned('未登记为本任务创建的暂存目录')
            _check_creator(project_id, target, owner_job_id)
            _validate_chain(root, target, owner, include_target=False)
            with ExitStack() as stack:
                handles = []
                for parent in _chain(root, target)[:-1]:
                    handles.append(stack.enter_context(pinned_directory(parent)))
                _validate_chain(root, target, owner, include_target=False)
                try: _lstat(target)
                except FileNotFoundError:
                    result.ok, result.message = True, '已登记暂存目录已清理'
                    return result
                _validate_chain(root, target, owner)
                _iter_tree(target)  # preflight refuses links BEFORE deleting any file
                # Revalidate after enumeration: a test/adversary can swap here.
                _validate_chain(root, target, owner)
                parent_fd = handles[-1]
                if parent_fd is not None:
                    fd = os.open(target.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
                    try:
                        if _identity(os.fstat(fd)) != owner.identity: raise StagingNotOwned('目录被替换')
                        _remove_contents(target, fd, result)
                    finally: os.close(fd)
                    current = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
                    if _identity(current) != owner.identity: raise StagingNotOwned('目录被替换')
                    os.rmdir(target.name, dir_fd=parent_fd)
                else:
                    with pinned_directory(target):
                        _validate_chain(root, target, owner)
                        _remove_contents(target, None, result)
                    os.rmdir(long_path(target))
                try: _lstat(target)
                except FileNotFoundError:
                    result.ok, result.message = True, '已清理本任务暂存目录'
                    return result
                raise OSError('删除后目录仍存在')
    except (StagingNotOwned, ValueError) as exc:
        result.error_code, result.message = ERR_NOT_OWNED, str(exc)
        result.residual_paths = [str(stage)]
        result.residual_count = 1
    except Exception as exc:  # finally callers must never mask the task's error
        result.error_code, result.message, result.retryable = ERR_CLEANUP_FAILED, '暂存回收失败，需安全重试', True
        result.residual_paths = [str(stage)]
        try:
            paths = _iter_tree(Path(stage))
            result.residual_paths = [str(p) for p in paths[:50]] or [str(stage)]
            result.residual_count = len(paths)
            result.residual_bytes = sum(_lstat(p).st_size for p in paths)
        except Exception:
            # Not observable is a failure, never an empty successful result.
            result.residual_count = 1
        logger.warning('CLEANUP_FAILED project=%s job=%s error=%s', project_id, owner_job_id, type(exc).__name__)
    return result


def cleanup_staging_or_log(project_id: str, stage: str | Path, *, owner_job_id: str | None = None) -> CleanupResult:
    result = cleanup_owned_staging(project_id, stage, owner_job_id=owner_job_id)
    if owner_job_id:
        try:
            from .runner import record_cleanup_result
            root = project_cache_dir(project_id)
            report = result.as_dict()
            # Only project-relative summaries are persisted/exposed to the UI.
            def redact(value):
                try: return Path(value).relative_to(root).as_posix()
                except ValueError: return '[unavailable]'
            report['path'] = redact(report['path'])
            report['residual_paths'] = [redact(p) for p in report['residual_paths']]
            record_cleanup_result(owner_job_id, report)
        except Exception:
            logger.exception('Could not record cleanup outcome job=%s', owner_job_id)
    return result


class CleanupRetryError(RuntimeError):
    def __init__(self, status: int, code: str):
        self.status, self.code = status, code
        super().__init__(code)


def retry_job_cleanup(project_id: str, job_id: str) -> None:
    """Retry registered stages of a stopped job; never take a path from HTTP."""
    from ..database import session_scope
    from ..models import Job
    from .runner import project_lock, TERMINAL_STATUSES, EXCLUSIVE_STATUSES
    from sqlalchemy import select
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None or job.project_id != project_id:
            raise CleanupRetryError(404, 'JOB_NOT_FOUND')
    lock = project_lock(project_id)
    if not lock.acquire(blocking=False): raise CleanupRetryError(409, 'JOB_CONFLICT')
    try:
        with session_scope() as s:
            job = s.get(Job, job_id)
            if job.status not in TERMINAL_STATUSES:
                raise CleanupRetryError(409, 'JOB_STILL_ACTIVE')
            if s.scalars(select(Job).where(Job.project_id == project_id,
                        Job.status.in_(EXCLUSIVE_STATUSES))).first() is not None:
                raise CleanupRetryError(409, 'JOB_CONFLICT')
        with _GUARD:
            stages = [p for p, owner in _OWNERS.items()
                      if (owner.project_id, owner.job_id) == (project_id, job_id)]
        if not stages:
            raise CleanupRetryError(409, ERR_NOT_OWNED)
        # Bound each invocation to this job's recorded set; each deletion repeats
        # every safety check. No discovery, no adoption after process restart.
        for stage in stages:
            cleanup_staging_or_log(project_id, stage, owner_job_id=job_id)
    finally: lock.release()
