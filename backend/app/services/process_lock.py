"""Cross-process mutual exclusion that actually excludes, on Windows and POSIX.

Why this is a module of its own
-------------------------------
TSB-370 used ``msvcrt.locking`` behind a thread lock and *claimed* cross-process
atomicity. A minimal probe on this machine shows that claim was false: two spawned
processes both acquired ``LK_NBLCK`` on the same file (``results: [1, 1]``). The
budget guarantee therefore rested on a property the code did not have.

The reliable primitive on Windows is ``CreateFileW`` with **no sharing**. Two
handles cannot both hold it, at the OS level, regardless of locking ranges or
append mode. On POSIX the equivalent is ``O_CREAT | O_EXCL`` on a lock file, or
``flock``; ``O_EXCL`` is used to keep one code path.

Everything here is about exclusion, not accounting.
"""

from __future__ import annotations

import os
import time
from pathlib import Path


class LockTimeout(Exception):
    """Could not take the lock in time; the caller must not proceed."""


class ProcessFileLock:
    """An exclusive lock held by opening a side-car file with no sharing.

    Windows: ``CreateFileW(..., dwShareMode=0)`` — a second open fails with
    ``ERROR_SHARING_VIOLATION`` (32), which is the property we want.
    POSIX: ``open(path, O_CREAT | O_EXCL)`` — a second create fails with EEXIST.
    """

    #: A POSIX marker older than this is treated as left behind by a crashed
    #: process. Without it, one abnormal exit would make every later acquire time
    #: out -- which is exactly the cascade CI showed on Linux.
    STALE_MARKER_SEC = 120.0

    def __init__(self, target: Path, *, timeout: float = 30.0) -> None:
        self.path = Path(str(target) + ".lock")
        self.timeout = timeout
        self._handle = None
        self._fd = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            if os.name == "nt":
                try:
                    import ctypes
                    from ctypes import wintypes

                    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                    kernel32.CreateFileW.restype = wintypes.HANDLE
                    kernel32.CreateFileW.argtypes = [
                        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                        wintypes.HANDLE,
                    ]

                    class SECURITY_ATTRIBUTES(ctypes.Structure):
                        _fields_ = [("nLength", wintypes.DWORD),
                                    ("lpSecurityDescriptor", ctypes.c_void_p),
                                    ("bInheritHandle", wintypes.BOOL)]

                    # bInheritHandle=False: a spawned child must NOT inherit this
                    # handle. Inheriting it lets the child reopen the same object as
                    # if it were the owner, which is why the storm test passed while
                    # the lock was "exclusive".
                    sa = SECURITY_ATTRIBUTES()
                    sa.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
                    sa.lpSecurityDescriptor = None
                    sa.bInheritHandle = False
                    GENERIC_READ = 0x80000000
                    GENERIC_WRITE = 0x40000000
                    OPEN_ALWAYS = 4
                    FILE_ATTRIBUTE_NORMAL = 0x80
                    handle = kernel32.CreateFileW(
                        str(self.path),
                        GENERIC_READ | GENERIC_WRITE,
                        0,                     # dwShareMode = 0 -> exclusive
                        ctypes.byref(sa), OPEN_ALWAYS, FILE_ATTRIBUTE_NORMAL, None,
                    )
                    # ``CreateFileW`` returns INVALID_HANDLE_VALUE (all bits set) on
                    # failure. Comparing against ``wintypes.HANDLE(-1).value`` is
                    # signed (-1) and silently never matches the unsigned result,
                    # which made every contender "succeed" -- the exact bug the
                    # storm test caught.
                    invalid = ctypes.c_void_p(-1).value
                    if handle not in (None, invalid):
                        self._handle = handle
                        return self
                    err = ctypes.get_last_error()
                    if err not in (32, 5):          # sharing violation / access denied
                        raise LockTimeout(f"lock open failed (winerror {err})")
                except LockTimeout:
                    raise
                except Exception as exc:            # pragma: no cover - platform
                    raise LockTimeout(f"lock open failed: {exc}") from exc
            else:
                try:
                    fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o644)
                    self._fd = fd
                    return self
                except FileExistsError:
                    self._reclaim_if_stale()

            if time.monotonic() > deadline:
                raise LockTimeout(f"lock timeout on {self.path.name}")
            time.sleep(0.005)

    def __exit__(self, *exc) -> None:
        if os.name == "nt":
            if self._handle is not None:
                try:
                    import ctypes
                    from ctypes import wintypes

                    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
                    kernel32.CloseHandle(self._handle)
                finally:
                    self._handle = None
        else:
            if self._fd is not None:
                try:
                    os.close(self._fd)
                finally:
                    self._fd = None
                try:
                    os.unlink(self.path)        # O_EXCL marker is the lock itself
                except OSError:
                    # A failed unlink leaves a marker; _reclaim_if_stale bounds the
                    # damage instead of letting it block the next caller forever.
                    pass
        return None

    def _reclaim_if_stale(self) -> None:
        """Delete a marker left behind by a crashed process."""
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return
        if age <= self.STALE_MARKER_SEC:
            return
        try:
            self.path.unlink()
        except OSError:
            pass
