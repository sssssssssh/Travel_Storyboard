"""TSB-272: create-only ownership records and handle-confined stage deletion.

The registry is deliberately process-local. An old crash orphan is never
adopted by passing a job ID; aged frame reclamation has its own narrower path.
"""
from __future__ import annotations

import logging
import os
import secrets
import stat
from contextlib import contextmanager, ExitStack
from collections import OrderedDict
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
    # --- TSB-345: generation identity -------------------------------------
    #: High-entropy nonce written into a marker file *inside* the owned leaf at
    #: creation time. Path, ``st_dev`` and ``st_ino`` are all properties of a
    #: *location*, and Linux filesystems reuse a freed inode immediately when a
    #: directory is removed and recreated at the same path -- so a substituted
    #: directory can present an identical (dev, ino) and be mistaken for ours.
    #: The nonce belongs to the creation *event*: a new directory cannot contain
    #: it unless it was created by this registration, so reuse cannot forge it.
    #: It is process-local and never serialised into a report, API response or
    #: Job stat (see ``_generation_token``).
    nonce: str = ""
    #: Marker filename inside the leaf holding ``nonce``.
    marker: str = ""


#: Marker written into every owned staging leaf. Deliberately not a dotfile so
#: it is visible to an operator inspecting a stuck directory.
_GENERATION_MARKER = ".tsb-generation"


def _generation_token() -> str:
    """A fresh, unguessable creation token for one owned leaf."""
    return secrets.token_hex(32)


def _write_generation_marker(stage: Path, nonce: str, *, dir_fd: int | None = None) -> str:
    """Write the generation marker into a just-created leaf.

    Returns the marker filename. Opened with ``O_NOFOLLOW|O_EXCL`` where a dir fd
    is available, so the marker cannot be planted through a link and a pre-existing
    file can never be adopted as our generation proof.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    payload = nonce.encode("ascii")
    if dir_fd is not None:
        fd = os.open(_GENERATION_MARKER, flags, 0o600, dir_fd=dir_fd)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
    else:
        fd = os.open(long_path(stage / _GENERATION_MARKER), flags, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
    return _GENERATION_MARKER


def _read_generation_marker(stage: Path, marker: str, *, dir_fd: int | None = None) -> str | None:
    """Read the leaf's generation marker without following links.

    ``None`` means "no usable proof" and the caller must fail closed: a missing,
    unreadable, linked or empty marker is *not* evidence of ownership.
    """
    if not marker:
        return None
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        if dir_fd is not None:
            fd = os.open(marker, flags, dir_fd=dir_fd)
        else:
            fd = os.open(long_path(stage / marker), flags)
    except (FileNotFoundError, OSError):
        return None
    try:
        info = os.fstat(fd)
        # The marker itself must be a regular file: a link or a device is not proof.
        if not stat.S_ISREG(info.st_mode):
            return None
        data = os.read(fd, 256)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        return data.decode("ascii").strip() or None
    except UnicodeDecodeError:
        return None


def _verify_generation(stage: Path, owner: StageOwner, *, dir_fd: int | None = None) -> None:
    """Assert the directory at this path is the *creation event* we registered.

    TSB-345: ``(dev, ino)`` alone is insufficient because Linux reuses freed
    inodes. This checks the nonce that only our own creation could have written.
    Any mismatch, absence or unreadability raises -- never a fallback to path or
    inode matching.
    """
    live = _read_generation_marker(stage, owner.marker, dir_fd=dir_fd)
    if not owner.nonce or live is None or live != owner.nonce:
        raise StagingNotOwned('暂存目录代际身份不一致（疑似被替换）')


_OWNERS: dict[Path, StageOwner] = {}
# Bounded absence receipts preserve immediate idempotence, never deletion rights.
_CLEANED: OrderedDict[Path, StageOwner] = OrderedDict()
_CLEANED_LIMIT = 256
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


def _is_generation_marker(path: Path) -> bool:
    """Is this entry our own generation marker (bookkeeping, not user data)?"""
    return path.name == _GENERATION_MARKER


def count_tree(root: Path) -> tuple[int, int]:
    try:
        files = [p for p in _iter_tree(root) if not _is_generation_marker(p)]
    except FileNotFoundError:
        return 0, 0
    return len(files), sum(_lstat(path).st_size for path in files)


def _is_staging_shape(root: Path, stage: Path) -> bool:
    try: p = stage.relative_to(root).parts
    except ValueError: return False
    return bool(
        len(p) == 2 and p[0] == 'roughcut' and p[1].startswith('staging-') and p[1][8:]
        or len(p) == 3 and p[:2] == ('proxies', 'staging') and p[2]
        or len(p) == 3 and p[0] == 'staging' and p[1] and p[2]
        or len(p) == 3 and p[0] == 'vision' and p[1] in ('staging', 'motion') and p[2]
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
        parts = stage.relative_to(project_cache_dir(project_id)).parts
        if parts[0] == 'vision':
            expected_type = {'staging': 'vision_analyze', 'motion': 'motion_review'}[parts[1]]
            if not job_id or parts[2] != job_id or job.job_type != expected_type:
                raise StagingNotOwned('视觉暂存叶子与创建任务类型不匹配')
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
            # TSB-345: stamp the creation event with a nonce that cannot be
            # reproduced by deleting the directory and recreating it at the same
            # path (Linux reuses the inode, so (dev, ino) is forgeable).
            nonce = _generation_token()
            child_fd = None
            try:
                if handles[-1] is not None:
                    child_fd = os.open(
                        target.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=handles[-1],
                    )
                marker = _write_generation_marker(target, nonce, dir_fd=child_fd)
            except Exception:
                # No generation proof means no deletion right: remove what we just
                # made and report, rather than registering an unverifiable leaf.
                try:
                    if handles[-1] is not None:
                        os.rmdir(target.name, dir_fd=handles[-1])
                    else:
                        os.rmdir(long_path(target))
                except OSError:
                    pass
                raise StagingNotOwned('无法写入代际标记，暂存目录不可授予删除权') from None
            finally:
                if child_fd is not None:
                    os.close(child_fd)
            _OWNERS[target] = StageOwner(
                project_id, owner_job_id, ids[-1], ids[:-1], nonce, marker
            )
            _CLEANED.pop(target, None)
    return target


def _remove_file(path: Path, fd=None):
    if fd is not None: os.unlink(path.name, dir_fd=fd)
    else: safe_unlink(path, missing_ok=False)


def _remove_contents(path: Path, fd, result: CleanupResult, *, marker: str = ""):
    """Delete everything inside an owned leaf.

    TSB-345: ``marker`` is skipped during the walk and removed by the caller once
    all content is gone. The marker *is* the generation proof, so deleting it
    mid-walk would make the post-removal identity check read a missing proof and
    refuse a perfectly legitimate cleanup.
    """
    names = os.listdir(fd) if fd is not None else os.listdir(long_path(path))
    if marker:
        names = [n for n in names if n != marker]
    for name in names:
        entry = path / name
        info = os.stat(name, dir_fd=fd, follow_symlinks=False) if fd is not None else _lstat(entry)
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise StagingNotOwned('删除前发现链接替换')
        if stat.S_ISDIR(info.st_mode):
            if fd is not None:
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try: _remove_contents(entry, child, result, marker=marker)
                finally: os.close(child)
                current = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if _identity(current) != _identity(info): raise StagingNotOwned('目录被替换')
                os.rmdir(name, dir_fd=fd)
            else:
                with pinned_directory(entry):
                    if _identity(_lstat(entry)) != _identity(info): raise StagingNotOwned('目录被替换')
                    _remove_contents(entry, None, result, marker=marker)
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
            if owner is None:
                receipt = _CLEANED.get(target)
                if receipt and (receipt.project_id, receipt.job_id) == (project_id, owner_job_id):
                    _check_creator(project_id, target, owner_job_id)
                    _validate_chain(root, target, receipt, include_target=False)
                    try: _lstat(target)
                    except FileNotFoundError:
                        result.ok, result.residual_bytes = True, 0
                        result.message = '已清理；未获得再次删除的权限'
                        return result
                    raise StagingNotOwned('清理后出现的新目录不能继承删除权')
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
                    _retire_owner(target, owner)
                    result.residual_bytes = 0
                    return result
                _validate_chain(root, target, owner)
                # TSB-345: (dev, ino) is forgeable by inode reuse, so assert the
                # creation generation as well before touching any file.
                _verify_generation(target, owner)
                _iter_tree(target)  # preflight refuses links BEFORE deleting any file
                # Revalidate after enumeration: a test/adversary can swap here.
                _validate_chain(root, target, owner)
                _verify_generation(target, owner)
                parent_fd = handles[-1]
                if parent_fd is not None:
                    fd = os.open(target.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
                    try:
                        if _identity(os.fstat(fd)) != owner.identity: raise StagingNotOwned('目录被替换')
                        # Anchor the generation proof to the fd we are about to
                        # delete through: no path lookup can be swapped underneath.
                        _verify_generation(target, owner, dir_fd=fd)
                        _remove_contents(target, fd, result, marker=owner.marker)
                        # Proof has served its purpose: drop it last, then
                        # re-verify once more that the leaf is still ours.
                        os.unlink(owner.marker, dir_fd=fd)
                        if _identity(os.fstat(fd)) != owner.identity:
                            raise StagingNotOwned('目录被替换')
                    finally: os.close(fd)
                    # The generation proof was unlinked above on purpose, so the
                    # final assertions here are about *this* object still being at
                    # *this* entry: the pinned fd's identity and the parent-relative
                    # stat. Re-reading the marker now would wrongly report a
                    # substituted directory for a perfectly legitimate cleanup.
                    current = os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
                    if _identity(current) != owner.identity:
                        raise StagingNotOwned('目录被替换')
                    if not stat.S_ISDIR(current.st_mode):
                        raise StagingNotOwned('目录被替换')
                    os.rmdir(target.name, dir_fd=parent_fd)
                else:
                    with pinned_directory(target):
                        _validate_chain(root, target, owner)
                        _verify_generation(target, owner)
                        _remove_contents(target, None, result, marker=owner.marker)
                        safe_unlink(target / owner.marker, missing_ok=True)
                        if _identity(_lstat(target)) != owner.identity:
                            raise StagingNotOwned('目录被替换')
                    os.rmdir(long_path(target))
                try: _lstat(target)
                except FileNotFoundError:
                    result.ok, result.message = True, '已清理本任务暂存目录'
                    _retire_owner(target, owner)
                    result.residual_bytes = 0
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
            paths = [p for p in _iter_tree(Path(stage)) if not _is_generation_marker(p)]
            result.residual_paths = [str(p) for p in paths[:50]] or [str(stage)]
            result.residual_count = len(paths)
            result.residual_bytes = sum(_lstat(p).st_size for p in paths)
        except Exception:
            # Not observable is a failure, never an empty successful result.
            result.residual_count = 1
        logger.warning('CLEANUP_FAILED project=%s job=%s error=%s', project_id, owner_job_id, type(exc).__name__)
    return result


def _retire_owner(target: Path, owner: StageOwner) -> None:
    """Retire a cleaned leaf, keeping only what idempotence needs.

    TSB-345: the receipt deliberately **drops the generation nonce and marker**.
    Its only jobs are to answer "was this already cleaned by this job?" and to
    prove absence, both of which depend on project/job/ancestor identity alone.
    Keeping the token alive after cleanup would be needless retention of the one
    secret that authorises deletion, and would leak it into any repr of the
    receipt.
    """
    _OWNERS.pop(target, None)
    _CLEANED[target] = StageOwner(
        owner.project_id, owner.job_id, owner.identity, owner.ancestors
    )
    _CLEANED.move_to_end(target)
    while len(_CLEANED) > _CLEANED_LIMIT:
        _CLEANED.popitem(last=False)


def is_registered_leaf(project_id: str, stage: str | Path, *, owner_job_id: str | None) -> bool:
    """Is this exact path a leaf *this job* created and still owns?

    TSB-345: used wherever a pre-existing directory might otherwise be adopted.
    Returns False for an unregistered path, a differently-owned leaf, a retired
    (already cleaned) path, or a directory whose generation identity no longer
    matches its registration -- so callers must report the situation rather than
    assume ownership (C6: no silent skip).

    This never grants deletion rights; it only reports whether the registry still
    vouches for the leaf. ``cleanup_owned_staging`` remains the only delete path.
    """
    try:
        root, target = _raw_target(project_id, stage)
    except (StagingNotOwned, ValueError, OSError):
        return False
    with _GUARD:
        owner = _OWNERS.get(target)
        if owner is None:
            return False
        if (owner.project_id, owner.job_id) != (project_id, owner_job_id):
            return False
        try:
            _check_creator(project_id, target, owner_job_id)
            _validate_chain(root, target, owner)
            _verify_generation(target, owner)
        except (StagingNotOwned, ValueError, OSError, Exception):
            return False
        return True


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
        except Exception as exc:
            logger.exception('Could not record cleanup outcome job=%s', owner_job_id)
            raise RuntimeError(f'CLEANUP_REPORT_FAILED (cleanup={result.error_code or "ok"})') from exc
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
            with session_scope() as s:
                row = s.get(Job, job_id)
                if (row.stats or {}).get('cleanup', {}).get('ok') is True:
                    return  # already completed; no paths discovered or deleted
            raise CleanupRetryError(409, ERR_NOT_OWNED)
        # Bound each invocation to this job's recorded set; each deletion repeats
        # every safety check. No discovery, no adoption after process restart.
        for stage in stages:
            cleanup_staging_or_log(project_id, stage, owner_job_id=job_id)
    finally: lock.release()
