"""TSB-251 / TSB-252 — deep-path HTTP serving and safe cleanup.

Two confirmed defects in v2.0.5's baseline:

* ``api/media.py`` used plain ``Path.is_file()``, which on Windows returns
  ``False`` (no exception) for a >MAX_PATH file that FFmpeg *did* write. The
  database reference exists, the file exists, yet the API answers 404.
* ``ffmpeg.safe_unlink()`` called ``Path.unlink(missing_ok=True)`` first. On a
  long path that call swallows the resulting ``FileNotFoundError``, returns, and
  the file is never deleted — the extended-length fallback was dead code.

The Windows-specific tests skip elsewhere; the contract tests run everywhere.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.ffmpeg import (
    long_path,
    safe_is_file,
    safe_stat,
    safe_unlink,
)

IS_WINDOWS = os.name == "nt"

requires_windows = pytest.mark.skipif(
    not IS_WINDOWS, reason="long-path behaviour is Windows-specific"
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _make_deep_dir(root: Path, target_len: int) -> Path:
    """Create a directory whose path reaches at least *target_len* characters."""
    current = root
    chunk = "d" * 40
    while len(str(current)) < target_len:
        current = current / chunk
        os.makedirs(long_path(current), exist_ok=True)
    return current


def _write(path: Path, data: bytes) -> None:
    try:
        path.write_bytes(data)
    except (FileNotFoundError, OSError):
        with open(long_path(path), "wb") as handle:
            handle.write(data)


def _exists(path: Path) -> bool:
    if path.is_file():
        return True
    try:
        return os.path.isfile(long_path(path))
    except (OSError, ValueError):
        return False


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except (FileNotFoundError, OSError):
        with open(long_path(path), "rb") as handle:
            return handle.read()


# --------------------------------------------------------------------------
# TSB-252: safe_unlink must actually delete
# --------------------------------------------------------------------------

def test_safe_unlink_removes_a_normal_file(tmp_path: Path):
    target = tmp_path / "victim.bin"
    target.write_bytes(b"x")
    safe_unlink(target)
    assert not target.exists()


def test_safe_unlink_is_idempotent_for_missing_files(tmp_path: Path):
    """Deleting something that never existed is a no-op, not an error."""
    safe_unlink(tmp_path / "never-existed.bin")
    safe_unlink(tmp_path / "never-existed.bin")


@requires_windows
def test_safe_unlink_removes_a_270_char_path_file(tmp_path: Path):
    """The regression: >MAX_PATH deletion used to silently do nothing."""
    deep = _make_deep_dir(tmp_path, 270)
    target = deep / "victim.bin"
    _write(target, b"payload")
    assert len(str(target)) >= 270, len(str(target))
    assert _exists(target), "fixture must exist before the delete"

    safe_unlink(target)

    assert not _exists(target), (
        "safe_unlink left a >MAX_PATH file behind: "
        "unlink(missing_ok=True) swallowed the error and skipped the fallback"
    )


@requires_windows
def test_safe_unlink_removes_a_320_char_path_file(tmp_path: Path):
    deep = _make_deep_dir(tmp_path, 320)
    target = deep / "victim.bin"
    _write(target, b"payload")
    assert len(str(target)) >= 320, len(str(target))

    safe_unlink(target)
    assert not _exists(target)


@requires_windows
def test_safe_unlink_missing_long_path_is_idempotent(tmp_path: Path):
    """A long path that genuinely does not exist must not raise."""
    deep = _make_deep_dir(tmp_path, 270)
    for _ in range(3):
        safe_unlink(deep / "absent.bin")


def test_safe_unlink_does_not_swallow_permission_errors(tmp_path: Path, monkeypatch):
    """A real failure must surface, not be reported as a successful delete."""
    target = tmp_path / "locked.bin"
    target.write_bytes(b"x")
    original = Path.unlink

    def locked(self, *args, **kwargs):
        if self == target:
            raise PermissionError("file is locked")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked)
    with pytest.raises(PermissionError):
        safe_unlink(target)


def test_safe_unlink_refuses_a_directory(tmp_path: Path):
    """Passing a directory must fail loudly rather than pretend to succeed."""
    with pytest.raises((IsADirectoryError, PermissionError, OSError)):
        safe_unlink(tmp_path)


@requires_windows
def test_safe_unlink_leaves_its_sibling_alone(tmp_path: Path):
    """Scope check: only the named file is removed."""
    deep = _make_deep_dir(tmp_path, 270)
    victim = deep / "victim.bin"
    keeper = deep / "keeper.bin"
    _write(victim, b"a")
    _write(keeper, b"b")

    safe_unlink(victim)

    assert not _exists(victim)
    assert _exists(keeper), "an unrelated file was deleted"


# --------------------------------------------------------------------------
# TSB-252: the other safe_* helpers must classify errors, not hide them
# --------------------------------------------------------------------------

def test_safe_is_file_distinguishes_missing_from_directory(tmp_path: Path):
    assert safe_is_file(tmp_path / "absent") is False
    assert safe_is_file(tmp_path) is False
    file_path = tmp_path / "present.bin"
    file_path.write_bytes(b"x")
    assert safe_is_file(file_path) is True


def test_safe_stat_raises_for_missing_file(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        safe_stat(tmp_path / "absent.bin")


def test_safe_stat_raises_for_permission_error(tmp_path: Path, monkeypatch):
    target = tmp_path / "locked.bin"
    target.write_bytes(b"x")
    original = Path.stat

    def locked(self, *args, **kwargs):
        if self == target:
            raise PermissionError("locked")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", locked)
    with pytest.raises(PermissionError):
        safe_stat(target)


@requires_windows
def test_safe_is_file_and_stat_agree_on_a_deep_file(tmp_path: Path):
    """Both helpers must see the same reality for a >MAX_PATH file."""
    deep = _make_deep_dir(tmp_path, 270)
    target = deep / "present.bin"
    _write(target, b"12345")

    assert safe_is_file(target) is True
    assert safe_stat(target).st_size == 5


@requires_windows
def test_safe_mkdir_and_write_read_delete_roundtrip(tmp_path: Path):
    """The full lifecycle of a deep artifact, which is the real product need."""
    deep = _make_deep_dir(tmp_path, 270)
    target = deep / "artifact.bin"
    payload = b"round-trip-payload" * 4

    _write(target, payload)
    assert safe_stat(target).st_size == len(payload)
    assert _read(target) == payload

    safe_unlink(target)
    assert not _exists(target)
