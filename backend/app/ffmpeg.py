"""FFmpeg / FFprobe wrapper.

All external calls use list-style argv (no shell string concatenation) so
Windows paths with spaces, CJK characters, emoji, etc. are handled safely.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import get_settings

logger = logging.getLogger(__name__)

_COMMON_WIN_DIRS = [
    r"C:\ffmpeg\bin",
    r"C:\Program Files\ffmpeg\bin",
    r"C:\ProgramData\chocolatey\bin",
]


class FFmpegError(RuntimeError):
    """Raised when an ffmpeg/ffprobe call fails.

    ``code`` is a stable, machine-readable identifier so a failure can be
    classified without parsing prose (TSB-241). Callers that surface errors to
    users should map it to a message rather than showing raw stderr.
    """

    #: Default classification for an unspecified external-tool failure.
    DEFAULT_CODE = "FFMPEG_FAILED"

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code or self.DEFAULT_CODE


# Stable error codes shared by the media boundary (TSB-241).
ERR_PATH_TOO_LONG = "PATH_TOO_LONG"
ERR_OUTPUT_NOT_CREATED = "OUTPUT_NOT_CREATED"
ERR_MEDIA_UNREADABLE = "MEDIA_UNREADABLE"
ERR_EXECUTABLE_MISSING = "EXECUTABLE_MISSING"
ERR_COMMAND_FAILED = "COMMAND_FAILED"
ERR_CANCELLED = "CANCELLED"
ERR_TIMEOUT = "TIMEOUT"

#: How long a media process gets to exit after a polite terminate before it is
#: killed outright. Kept short: a cancelled job should free its project lock
#: quickly, and ffmpeg flushes nothing worth waiting for on cancel.
CANCEL_GRACE_SEC = 5.0


class FFmpegCancelled(FFmpegError):
    """Raised when a media command was stopped because its job was cancelled."""

    def __init__(self, message: str = "媒体进程已取消") -> None:
        super().__init__(message, code=ERR_CANCELLED)


class FFmpegTimeout(FFmpegError):
    """Raised when a media command exceeded its timeout."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code=ERR_TIMEOUT)


def path_exceeds_limit(path: str | Path) -> bool:
    """True when *path* is long enough that Windows may refuse it.

    Windows enforces MAX_PATH (260) unless ``LongPathsEnabled`` is set AND every
    participating process opts in. Python and FFmpeg disagree in that state, so
    callers treat this as a hard, reportable condition rather than letting
    FFmpeg write a file Python cannot see.
    """
    if os.name != "nt":
        return False
    return len(str(path)) >= 260


def ensure_path_usable(path: str | Path) -> None:
    """Raise a classified error when *path* is too long for a safe round trip."""
    if path_exceeds_limit(path):
        raise FFmpegError(
            f"路径过长（{len(str(path))} 字符，Windows 上限 260）：{path}。"
            "请把项目输出工作区放到更短的目录（例如 D:\\TSB）。",
            code=ERR_PATH_TOO_LONG,
        )


def _which(name: str) -> str | None:
    return shutil.which(name)


def _find_common(name: str) -> str | None:
    exe = f"{name}.exe"
    for directory in _COMMON_WIN_DIRS:
        candidate = Path(directory) / exe
        if candidate.exists():
            return str(candidate)
    return None


def locate_ffmpeg() -> str:
    settings = get_settings()
    if settings.ffmpeg_path:
        return settings.ffmpeg_path
    env = os.environ.get("FFMPEG_PATH")
    if env:
        return env
    return _which("ffmpeg") or _find_common("ffmpeg") or "ffmpeg"


def locate_ffprobe() -> str:
    settings = get_settings()
    if settings.ffprobe_path:
        return settings.ffprobe_path
    env = os.environ.get("FFPROBE_PATH")
    if env:
        return env
    return _which("ffprobe") or _find_common("ffprobe") or "ffprobe"


def _resolve_exe(name: str) -> str:
    return locate_ffmpeg() if name == "ffmpeg" else locate_ffprobe()


def _exe_available(exe: str) -> bool:
    if not exe:
        return False
    if Path(exe).exists():
        return True
    return _which(exe) is not None


def ffmpeg_available() -> str | None:
    p = locate_ffmpeg()
    return p if _exe_available(p) else None


def ffprobe_available() -> str | None:
    p = locate_ffprobe()
    return p if _exe_available(p) else None


def long_path(path: str | Path) -> str:
    r"""Return a form of *path* that survives Windows' MAX_PATH limit.

    Windows still enforces a 260-character limit unless the ``LongPathsEnabled``
    policy is on. FFmpeg is a native program and happily writes to a longer
    path, but Python's own ``os.path`` calls then fail to see the file — which
    used to look like "ffmpeg succeeded but produced nothing".

    Prefixing with ``\\?\`` (or ``\\?\UNC\`` for network shares) opts into the
    extended-length API for both sides, so a deep workspace keeps working.
    """
    text = str(path)
    if os.name != "nt" or text.startswith("\\\\?\\"):
        return text
    try:
        absolute = os.path.abspath(text)
    except (OSError, ValueError):
        return text
    if absolute.startswith("\\\\"):  # UNC share: \\server\share -> \\?\UNC\server\share
        return "\\\\?\\UNC\\" + absolute.lstrip("\\")
    return "\\\\?\\" + absolute


def path_length_warning(path: str | Path) -> str | None:
    """A user-facing hint when a path is close to (or past) Windows' limit."""
    if os.name != "nt":
        return None
    length = len(str(path))
    if length < 240:
        return None
    return (
        f"路径长度 {length} 接近或超过 Windows 的 260 字符上限，"
        "建议把项目输出工作区放到更短的目录（例如 D:\\TSB）。"
    )


def safe_stat(path: str | Path):
    """``Path.stat`` that transparently retries through the extended-length form.

    Python's own ``Path.stat()`` obeys MAX_PATH when ``LongPathsEnabled`` is off,
    so a source file that FFmpeg reads happily can look missing to the indexer.
    Retrying with ``\\\\?\\`` makes deep source paths behave like shallow ones.

    Only a *missing path* triggers the retry. ``PermissionError`` and other real
    failures propagate unchanged, so a locked file is still reported as
    ``source_unavailable`` rather than being mistaken for a long-path problem.
    """
    target = Path(path)
    try:
        return target.stat()
    except FileNotFoundError:
        if os.name != "nt":
            raise
        return os.stat(long_path(target))


def safe_open(path: str | Path, mode: str = "rb"):
    """``open`` that falls back to the extended-length form on Windows.

    Only a missing path triggers the fallback; permission errors propagate.
    """
    try:
        return open(path, mode)
    except FileNotFoundError:
        if os.name != "nt":
            raise
        return open(long_path(path), mode)


def safe_is_file(path: str | Path) -> bool:
    """``Path.is_file`` that is not defeated by a long path.

    A permission error is **re-raised**, never folded into ``False``. Reporting a
    locked/unreadable file as "not a file" makes a scanner skip it silently and
    end up with a green job — the exact failure mode the scan pipeline must
    report as a per-item ``source_unavailable`` error. Only genuine
    "path too long / missing" cases fall back to the extended-length form.
    """
    target = Path(path)
    try:
        if target.is_file():
            return True
    except PermissionError:
        raise
    except OSError:
        # e.g. OSError for an over-long path: fall through to the retry below.
        pass
    if os.name != "nt":
        return False
    try:
        return os.path.isfile(long_path(target))
    except (OSError, ValueError):
        return False


def safe_mkdir(path: str | Path) -> None:
    """Create a directory (and parents) without hitting MAX_PATH.

    ``Path.mkdir(parents=True)`` walks the path itself and fails past 260 chars
    even when the target is a normal video/JPEG output directory.
    """
    target = Path(path)
    try:
        target.mkdir(parents=True, exist_ok=True)
        return
    except FileNotFoundError:
        if os.name != "nt":
            raise
        os.makedirs(long_path(target), exist_ok=True)


def safe_serving_path(path: str | Path) -> str:
    """Return a form of *path* that Starlette's ``FileResponse`` can open.

    ``FileResponse`` resolves and stats the file itself and raises
    ``RuntimeError("File ... does not exist")`` on Windows when the path exceeds
    MAX_PATH — even though the file is really there. Handing it the
    extended-length form makes GET / Range / HEAD all work unchanged.

    The extended-length prefix is only ever used *internally* to open the file:
    authorization and containment are decided on the ordinary resolved path by
    the caller, and the caller's URL/DB references are never rewritten.
    Returns a ``str`` because that is what ``FileResponse`` stores and echoes in
    error messages.
    """
    target = Path(path)
    if os.name != "nt":
        return str(target)
    # Prefer the plain form; only reach for \\?\ when the plain form is unusable.
    try:
        if target.is_file():
            return str(target)
    except OSError:
        pass
    return long_path(target)


def safe_scandir(path: str | Path):
    """``os.scandir`` that also works past MAX_PATH on Windows.

    At ~271 characters ``os.listdir``/``iterdir`` raise ``FileNotFoundError``
    even though the directory exists. ``Path.rglob`` swallows that error and
    simply stops descending, so a scan silently loses every file beneath a deep
    directory — the "scan job quietly misses files" release veto (TSB-254).

    Yields ``os.DirEntry`` objects like ``os.scandir`` does, so callers keep
    ``is_dir()`` / ``is_file()`` / ``stat()`` without extra syscalls.
    """
    target = Path(path)
    try:
        return os.scandir(target)
    except FileNotFoundError:
        if os.name != "nt":
            raise
        return os.scandir(long_path(target))


def walk_files(root: str | Path):
    """Yield every regular file under *root*, tolerating >MAX_PATH directories.

    Iterative (no recursion limit), does **not** follow directory symlinks, and
    re-raises unexpected errors so a caller can report a per-item failure rather
    than silently returning a short list. Directory entries that cannot be read
    are reported through the returned generator's ``errors`` accessor only when
    the caller asks; by default an unreadable sub-directory raises so the scan
    can record it.
    """
    base = Path(root)
    stack = [base]
    while stack:
        current = stack.pop()
        try:
            entries = list(safe_scandir(current))
        except FileNotFoundError:
            # Vanished between listing and reading: nothing to report.
            continue
        except NotADirectoryError:
            continue
        except PermissionError:
            raise
        for entry in entries:
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                # Cannot classify: treat as a file so the caller reports it.
                yield Path(entry.path)
                continue
            if is_dir:
                stack.append(Path(entry.path))
            else:
                yield Path(entry.path)


def safe_unlink(path: str | Path, *, missing_ok: bool = True) -> None:
    """Remove a file, tolerating >MAX_PATH paths on Windows.

    Deliberately does **not** call ``Path.unlink(missing_ok=True)`` first. On
    Windows an over-long path raises ``FileNotFoundError`` (WinError 3) even when
    the file exists, and ``missing_ok=True`` swallows exactly that error — so the
    call returned "successfully" while the file stayed on disk, and the
    extended-length fallback below was unreachable dead code (TSB-252).

    The delete is attempted without suppression so a genuine ``FileNotFoundError``
    (or ``PermissionError``) is distinguishable, and the retry only runs for the
    path-shape failures that ``\\\\?\\`` can actually fix.
    """
    target = Path(path)
    try:
        target.unlink()
        return
    except FileNotFoundError as exc:
        if os.name != "nt":
            if missing_ok:
                return
            raise
        # Either the file is genuinely gone, or the path is too long for the
        # non-prefixed form. Retry through \\?\ and let *that* decide.
        try:
            os.unlink(long_path(target))
        except FileNotFoundError:
            if missing_ok:
                return
            raise exc from None
        except IsADirectoryError:
            raise
    except IsADirectoryError:
        # Never silently "succeed" on a directory: callers use this to reclaim
        # files, and a directory here means the caller passed the wrong thing.
        raise


def safe_image_open(path: str | Path):
    """Open an image with Pillow, tolerating >MAX_PATH paths.

    Pillow calls ``builtins.open`` internally, which obeys the Windows MAX_PATH
    limit — so a JPEG this app just wrote can be invisible to it. Read the bytes
    ourselves and hand Pillow a buffer instead.
    """
    import io as _io

    from PIL import Image

    target = Path(path)
    try:
        with target.open("rb") as handle:
            return Image.open(_io.BytesIO(handle.read()))
    except (FileNotFoundError, OSError):
        if os.name != "nt":
            raise
        with open(long_path(target), "rb") as handle:
            return Image.open(_io.BytesIO(handle.read()))


def safe_replace(src: str | Path, dst: str | Path) -> None:
    """Move *src* onto *dst*, tolerating >MAX_PATH paths on either side."""
    source, target = Path(src), Path(dst)
    try:
        source.replace(target)
        return
    except FileNotFoundError:
        if os.name != "nt":
            raise
        os.replace(long_path(source), long_path(target))


def safe_write_text(path: str | Path, text: str, encoding: str = "utf-8") -> None:
    """Write text to a possibly >MAX_PATH path."""
    target = Path(path)
    try:
        target.write_text(text, encoding=encoding)
        return
    except FileNotFoundError:
        if os.name != "nt":
            raise
        with open(long_path(target), "w", encoding=encoding, newline="") as handle:
            handle.write(text)


def _popen_kwargs() -> dict:
    """Process-group flags so a whole media tree can be reclaimed.

    Detached from our console/process group, so terminating it cannot take the
    server down with it, and so a media tree can be addressed as a unit.
    """
    if os.name == "nt":
        # CREATE_NEW_PROCESS_GROUP: Ctrl-Break/terminate semantics stay local.
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}  # setsid: own process group


# --- Windows Job Object ------------------------------------------------------
#
# CREATE_NEW_PROCESS_GROUP only makes the child a group leader; it does not give
# us a handle that covers its *descendants*. A Job Object does: every process the
# child spawns is automatically a member, and KILL_ON_JOB_CLOSE reclaims the
# whole set even if the original parent has already exited — which is precisely
# the case a `terminate()` on the parent cannot handle (TSB-263).
#
# Deliberately scoped to one Job per media process: no global kill, no
# name-based lookup, so another project's concurrent encode is never touched.

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1


def _create_kill_on_close_job(pid: int):
    """Bind process *pid* into a new Job Object that kills on close.

    Returns an opaque handle to keep alive for the process's lifetime, or
    ``None`` when the platform refuses (the caller then falls back to signalling
    the process group). Never raises: a failure here must degrade, not break the
    pipeline.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            wintypes.HANDLE(job),
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            kernel32.CloseHandle(wintypes.HANDLE(job))
            return None

        PROCESS_SET_QUOTA = 0x0100
        PROCESS_TERMINATE = 0x0001
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        handle = kernel32.OpenProcess(
            PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid
        )
        if not handle:
            kernel32.CloseHandle(wintypes.HANDLE(job))
            return None

        assigned = kernel32.AssignProcessToJobObject(
            wintypes.HANDLE(job), wintypes.HANDLE(handle)
        )
        kernel32.CloseHandle(wintypes.HANDLE(handle))
        if not assigned:
            # Already in a job that forbids nesting (older Windows / CI hosts):
            # degrade to group signalling rather than failing the run.
            kernel32.CloseHandle(wintypes.HANDLE(job))
            return None
        return job
    except Exception:  # noqa: BLE001 - optional hardening, never fatal
        logger.debug("Could not create a kill-on-close job object", exc_info=True)
        return None


def _close_job(job) -> None:
    """Closing a KILL_ON_JOB_CLOSE job reclaims every member process."""
    if job is None or os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(
            wintypes.HANDLE(job)
        )
    except Exception:  # noqa: BLE001
        logger.debug("Could not close the media job object", exc_info=True)


def _terminate_tree(proc: subprocess.Popen, job=None, pgid: int | None = None) -> None:
    """Stop the whole process *tree* started for this run.

    Order of business (TSB-263):

    1. Windows: closing the kill-on-close Job Object reclaims parent **and**
       descendants, including ones whose parent already exited.
    2. POSIX: signal the child's own process group (it called ``setsid``), then
       escalate to ``SIGKILL`` for survivors.
    3. Always finish by reaping the direct child so no zombie is left and the
       pipes can be closed.

    Scope is deliberately limited to this run: a per-process group or per-process
    Job. There is no name-based or system-wide kill, because that would reach
    another project's concurrent encode — the task book forbids it, and
    ``test_no_global_name_based_kill_is_used`` guards the rule.
    """
    if job is not None:
        _close_job(job)

    if os.name == "nt":
        # No portable group signal on Windows; the child was told to terminate,
        # and the Job Object (when available) has already taken the descendants.
        try:
            if proc.poll() is None:
                proc.terminate()
        except OSError:
            pass
    else:
        _signal_group(proc, pgid)

    _reap(proc)


def _signal_group(proc: subprocess.Popen, pgid: int | None) -> None:
    """POSIX: terminate this run's process group, escalating if it lingers."""
    import signal

    # Prefer the group recorded at spawn time. Re-reading it from a possibly
    # reaped child could target a *recycled* group id, so a live lookup is only
    # a fallback and is verified against the pid we started.
    group = pgid
    if group is None:
        try:
            group = os.getpgid(proc.pid)
        except (ProcessLookupError, OSError):
            group = None
    if group is None or group <= 0:
        try:
            proc.terminate()
        except OSError:
            pass
        return

    def _killpg(sig) -> None:
        try:
            os.killpg(group, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    _killpg(signal.SIGTERM)
    deadline = time.monotonic() + CANCEL_GRACE_SEC
    while time.monotonic() < deadline:
        if _group_gone(group):
            return
        time.sleep(0.05)
    # Still alive after the grace period: force it, group-wide.
    _killpg(signal.SIGKILL)


def _group_gone(group: int) -> bool:
    import signal

    try:
        os.killpg(group, 0)
    except (ProcessLookupError, PermissionError):
        return True
    except OSError:
        return True
    return False


def _reap(proc: subprocess.Popen) -> None:
    """Wait for the direct child to exit, killing it outright if it will not."""
    try:
        proc.wait(timeout=CANCEL_GRACE_SEC)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=CANCEL_GRACE_SEC)
    except subprocess.TimeoutExpired:
        logger.warning("Media process %s did not exit after kill()", proc.pid)



def run(
    cmd: list[str],
    timeout: int = 3600,
    *,
    expect_output: str | Path | None = None,
    expect_pattern: str | None = None,
    cancel_check=None,
    poll_interval: float = 0.25,
) -> subprocess.CompletedProcess:
    """Run an external command, raising a classified ``FFmpegError`` on failure.

    FFmpeg can exit **0** while writing nothing. Two known causes are handled at
    this boundary so they never surface later as a confusing
    ``FileNotFoundError`` from Pillow or OpenCV:

    * the ``image2`` muxer refuses a filename with no ``%03d`` pattern;
    * on Windows a path past ``MAX_PATH`` can be written by FFmpeg yet stay
      invisible to Python's ``os.path``.

    Pass ``expect_output`` for a single file, or ``expect_pattern`` (a glob) for
    a frame sequence, and the result is verified before returning.

    ``cancel_check`` (TSB-253) is an optional zero-argument callable. When it
    returns truthy the child is terminated and ``FFmpegCancelled`` is raised, so
    cancelling a job stops the *encoder* instead of waiting for it to finish.
    Omit it and the call behaves exactly as before.
    """
    if cancel_check is None:
        # Fast path: unchanged behaviour, no extra threads or polling.
        try:
            proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
        except FileNotFoundError as exc:
            raise FFmpegError(
                f"Executable not found: {cmd[0]}", code=ERR_EXECUTABLE_MISSING
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise FFmpegTimeout(
                f"命令超时（{timeout}s）：{' '.join(cmd[:6])} ..."
            ) from exc
        return _verify_expected(proc, cmd, expect_output, expect_pattern)

    try:
        child = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **_popen_kwargs(),
        )
    except FileNotFoundError as exc:
        raise FFmpegError(
            f"Executable not found: {cmd[0]}", code=ERR_EXECUTABLE_MISSING
        ) from exc

    # Bind the tree so descendants can be reclaimed as a unit (TSB-263). On
    # Windows this is a kill-on-close Job Object; on POSIX we signal the group
    # the child created for itself via setsid.
    job = _create_kill_on_close_job(child.pid)
    pgid: int | None
    if os.name == "nt":
        pgid = None
    else:
        try:
            pgid = os.getpgid(child.pid)
        except OSError:
            pgid = None

    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                # communicate(timeout=) drains both pipes on background threads,
                # so a chatty ffmpeg cannot deadlock on a full pipe buffer.
                stdout, stderr = child.communicate(timeout=poll_interval)
                break
            except subprocess.TimeoutExpired:
                if cancel_check():
                    _terminate_tree(child, job=job, pgid=pgid)
                    job = None
                    raise FFmpegCancelled(
                        f"媒体进程已取消：{' '.join(cmd[:6])} ..."
                    ) from None
                if time.monotonic() >= deadline:
                    _terminate_tree(child, job=job, pgid=pgid)
                    job = None
                    raise FFmpegTimeout(
                        f"命令超时（{timeout}s）：{' '.join(cmd[:6])} ..."
                    ) from None
    finally:
        # Never leave the child (or its descendants) running, and never leak the
        # job handle — closing it is what reclaims the tree.
        if job is not None or child.poll() is None:
            _terminate_tree(child, job=job, pgid=pgid)
            job = None
        for stream in (child.stdout, child.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    proc = subprocess.CompletedProcess(cmd, child.returncode, stdout, stderr)
    return _verify_expected(proc, cmd, expect_output, expect_pattern)


def _verify_expected(
    proc: subprocess.CompletedProcess,
    cmd: list[str],
    expect_output: str | Path | None,
    expect_pattern: str | None,
) -> subprocess.CompletedProcess:
    """Shared post-run validation (exit code + declared artefacts)."""
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace")[-2000:]
        preview = " ".join(cmd[:6])
        raise FFmpegError(
            f"Command failed ({proc.returncode}): {preview} ...\n{stderr}",
            code=ERR_COMMAND_FAILED,
        )

    if expect_output is not None:
        target = Path(expect_output)
        if not target.is_file() and not Path(long_path(target)).is_file():
            stderr = proc.stderr.decode("utf-8", errors="replace")[-2000:]
            if path_exceeds_limit(target):
                raise FFmpegError(
                    f"输出路径过长，FFmpeg 退出码为 0 但 Python 读不到文件：{target}\n{stderr}",
                    code=ERR_PATH_TOO_LONG,
                )
            raise FFmpegError(
                f"Command reported success but wrote no output: {target}\n{stderr}",
                code=ERR_OUTPUT_NOT_CREATED,
            )
    elif expect_pattern is not None:
        # Frame sequences: verify at least one file matched.
        import glob as _glob

        matches = _glob.glob(expect_pattern)
        if not matches:
            directory = Path(expect_pattern).parent
            # glob() itself can miss >MAX_PATH output; retry via the prefix form.
            if directory.is_dir():
                matches = [str(p) for p in directory.iterdir() if p.is_file()]
            if not matches:
                stderr = proc.stderr.decode("utf-8", errors="replace")[-2000:]
                code = (
                    ERR_PATH_TOO_LONG
                    if path_exceeds_limit(expect_pattern)
                    else ERR_OUTPUT_NOT_CREATED
                )
                raise FFmpegError(
                    f"Command reported success but produced no frames matching "
                    f"{expect_pattern}\n{stderr}",
                    code=code,
                )
    return proc


# --- Probe data structures -------------------------------------------------

@dataclass
class VideoStreamInfo:
    width: int = 0
    height: int = 0
    rotation: int = 0
    codec: str = ""
    pixel_format: str = ""
    fps_num: int = 0
    fps_den: int = 0
    vfr: bool = False
    duration_sec: float = 0.0
    color_primaries: str = ""
    color_transfer: str = ""
    color_space: str = ""


@dataclass
class MediaInfo:
    duration_sec: float = 0.0
    has_video: bool = False
    has_audio: bool = False
    audio_codec: str = ""
    container: str = ""
    video: VideoStreamInfo = field(default_factory=VideoStreamInfo)


def _parse_rational(value: str | None) -> tuple[int, int]:
    """Parse an ffprobe rational like '30000/1001' into (num, den)."""
    if not value or "/" not in value:
        return (0, 0)
    try:
        num_s, den_s = value.split("/", 1)
        return int(num_s), int(den_s)
    except ValueError:
        return (0, 0)


def _parse_rotation(stream: dict[str, Any]) -> int:
    # Prefer side-data Display Matrix rotation.
    for side in stream.get("side_data_list") or []:
        if side.get("side_data_type") == "Display Matrix":
            try:
                rotation = int(round(float(side.get("rotation", 0))))
            except (TypeError, ValueError):
                rotation = 0
            return ((-rotation) % 360) if rotation else 0
    tags = stream.get("tags") or {}
    raw = tags.get("rotate", "0")
    try:
        return int(float(raw)) % 360
    except (TypeError, ValueError):
        return 0


def probe(source: str | Path) -> dict[str, Any]:
    """Return raw ffprobe JSON for a media file."""
    cmd = [
        locate_ffprobe(),
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        long_path(source),
    ]
    proc = run(cmd)
    try:
        return json.loads(proc.stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise FFmpegError(
            f"Invalid ffprobe JSON for {source}", code=ERR_MEDIA_UNREADABLE
        ) from exc


def probe_media(source: str | Path) -> MediaInfo:
    """Probe a media file and normalize the result into a MediaInfo."""
    data = probe(source)
    info = MediaInfo()
    fmt = data.get("format") or {}
    info.container = fmt.get("format_name", "")
    try:
        info.duration_sec = float(fmt.get("duration", 0.0) or 0.0)
    except (TypeError, ValueError):
        info.duration_sec = 0.0

    for stream in data.get("streams") or []:
        codec_type = stream.get("codec_type")
        if codec_type == "video" and not info.has_video:
            info.has_video = True
            v = info.video
            v.width = int(stream.get("width", 0) or 0)
            v.height = int(stream.get("height", 0) or 0)
            v.codec = stream.get("codec_name", "")
            v.pixel_format = stream.get("pix_fmt", "")
            v.rotation = _parse_rotation(stream)
            avg = stream.get("avg_frame_rate", "")
            rfr = stream.get("r_frame_rate", "")
            v.fps_num, v.fps_den = _parse_rational(avg)
            v.vfr = bool(avg) and bool(rfr) and avg != rfr
            try:
                v.duration_sec = float(stream.get("duration", 0.0) or 0.0)
            except (TypeError, ValueError):
                v.duration_sec = 0.0
            v.color_primaries = stream.get("color_primaries", "") or ""
            v.color_transfer = stream.get("color_transfer", "") or ""
            v.color_space = stream.get("color_space", "") or ""
        elif codec_type == "audio" and not info.has_audio:
            info.has_audio = True
            info.audio_codec = stream.get("codec_name", "")

    # Prefer the video stream's duration when available.
    if info.video.duration_sec:
        info.duration_sec = info.video.duration_sec
    return info


# --- Media operations ------------------------------------------------------

def extract_frame(
    source: str | Path,
    time_sec: float,
    out_path: str | Path,
    width: int | None = None,
    cancel_check=None,
) -> Path:
    """Extract a single frame at an exact-ish time to a JPEG file."""
    out = Path(out_path)
    safe_mkdir(out.parent)
    cmd = [
        locate_ffmpeg(),
        "-y",
        "-accurate_seek",
        "-ss", f"{time_sec:.3f}",
        "-i", long_path(source),
        "-frames:v", "1",
    ]
    if width:
        cmd += ["-vf", f"scale={width}:-2"]
    # `-update 1` is required: without it the image2 muxer refuses a filename
    # that has no `%03d` pattern, prints a warning, and exits 0 without writing.
    cmd += ["-q:v", "3", "-update", "1", long_path(out)]
    run(cmd, expect_output=out, cancel_check=cancel_check)
    return out


def extract_preview(
    source: str | Path,
    start_sec: float,
    duration_sec: float,
    out_path: str | Path,
    width: int | None = None,
    crf: int = 28,
    keep_audio: bool = False,
    cancel_check=None,
) -> Path:
    """Generate a low-bitrate proxy clip (used for dynamic review in M3+).

    ``cancel_check`` lets a cancelled proxy job stop this encode immediately
    rather than after the clip finishes (TSB-253).
    """
    out = Path(out_path)
    safe_mkdir(out.parent)
    cmd = [
        locate_ffmpeg(),
        "-y",
        "-ss", f"{start_sec:.3f}",
        "-i", long_path(source),
        "-t", f"{duration_sec:.3f}",
    ]
    if width:
        cmd += ["-vf", f"scale={width}:-2"]
    if not keep_audio:
        cmd += ["-an"]
    cmd += [
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        long_path(out),
    ]
    run(cmd, expect_output=out, cancel_check=cancel_check)
    return out
