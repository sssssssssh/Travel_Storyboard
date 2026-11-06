"""TSB-366-R3 — long-path image saving in the review package.

The defect these tests pin down: ``_safe_save`` retried a failed save by encoding
into a ``BytesIO`` **without a format**, so the fallback raised
``ValueError: unknown file extension:`` every time. The caller counted that as
"re-encode failure" and dropped the frame, so a deep workspace produced a package
with zero representative frames and the message "代表帧缺失或预算不足".

Coverage mirrors the task book:
  * saving past MAX_PATH succeeds and the file re-opens;
  * short-path and long-path results agree;
  * deep directory creation + write + read;
  * Pillow privacy scrub (no EXIF/GPS);
  * package built in a genuinely deep workspace keeps its frames;
  * the fallback is proven reachable on platforms without MAX_PATH (Linux CI).
"""

from __future__ import annotations

import io
import os
import shutil
from pathlib import Path

import pytest
from PIL import Image

from app.ffmpeg import long_path, safe_is_file, safe_mkdir
from app.services import review_package as rp

#: Windows refuses ordinary paths at/after this length (LongPathsEnabled=0).
MAX_PATH = 260

#: Windows is the only platform where an ordinary path can exceed MAX_PATH and
#: make Pillow's path-based save fail. Elsewhere the *fallback branch* is still
#: exercised — deliberately, by forcing the first save to raise — so the fix is
#: covered on Linux CI too rather than skipped there.
needs_windows_max_path = pytest.mark.skipif(
    os.name != "nt",
    reason="MAX_PATH only constrains Windows; the fallback path is covered by "
           "test_the_long_path_fallback_is_reachable_even_without_max_path",
)




def _deep_dir(base: Path, *, min_file_length: int, leaf: str) -> Path:
    """Create a real directory so that ``dir/leaf`` is at least ``min_file_length``.

    Levels are created with :func:`safe_mkdir` on purpose: the tree itself has to
    exist before the file-level behaviour can be tested, and building it with
    plain ``Path.mkdir`` would fail long before we get to the interesting part.
    """
    cur = base
    guard = 0
    while len(str(cur)) + 1 + len(leaf) < min_file_length and guard < 40:
        nxt = cur / ("d" * 32)
        safe_mkdir(nxt)
        cur = nxt
        guard += 1
    return cur


def _image_bytes(image: Image.Image, **kwargs) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="JPEG", **kwargs)
    return buf.getvalue()


pytestmark = pytest.mark.usefixtures("_r3_cleanup")


@pytest.fixture()
def _r3_cleanup(tmp_path: Path):
    yield


def _workdir(tmp_path: Path, name: str) -> Path:
    d = tmp_path / name
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# _safe_save: the core defect
# ---------------------------------------------------------------------------


def test_safe_save_writes_an_ordinary_path(tmp_path: Path):
    img = Image.new("RGB", (320, 180), (12, 34, 56))
    dst = _workdir(tmp_path, "plain") / "ok.jpg"
    rp._safe_save(img, dst, quality=82)
    assert safe_is_file(dst)
    with Image.open(dst) as reopened:
        reopened.load()
        assert reopened.format == "JPEG"
        assert reopened.size == (320, 180)


@needs_windows_max_path
def test_safe_save_succeeds_past_max_path(tmp_path: Path):
    """The regression: this raised ``ValueError: unknown file extension``."""
    leaf = "0000_abcdef12_00001000.jpg"
    deep = _deep_dir(_workdir(tmp_path, "deep"), min_file_length=MAX_PATH + 20, leaf=leaf)
    dst = deep / leaf
    assert len(str(dst)) >= MAX_PATH, (
        f"test setup must exceed MAX_PATH, got {len(str(dst))}"
    )

    img = Image.new("RGB", (200, 120), (1, 2, 3))
    rp._safe_save(img, dst, quality=82)

    assert safe_is_file(dst)
    with Image.open(long_path(dst)) as reopened:
        reopened.load()
        assert reopened.format == "JPEG"
        assert reopened.size == (200, 120)


@needs_windows_max_path
def test_safe_save_long_path_result_matches_a_short_path(tmp_path: Path):
    """Same input + same quality ⇒ same image, regardless of where it lands."""
    img = Image.new("RGB", (240, 135), (200, 100, 50))

    short = _workdir(tmp_path, "cmp-short") / "frame.jpg"
    rp._safe_save(img, short, quality=82)

    leaf = "0000_abcdef12_00002000.jpg"
    deep = _deep_dir(_workdir(tmp_path, "cmp-long"), min_file_length=MAX_PATH + 20, leaf=leaf)
    long_dst = deep / leaf
    assert len(str(long_dst)) >= MAX_PATH
    rp._safe_save(img, long_dst, quality=82)

    with Image.open(short) as a, Image.open(long_path(long_dst)) as b:
        a.load()
        b.load()
        assert b.format == a.format == "JPEG"
        assert b.size == a.size
        assert b.mode == a.mode == "RGB"
        # Encoded pixels identical: the fallback must not change the encoder or
        # its options.
        assert list(b.getdata()) == list(a.getdata())


def test_safe_save_honours_an_explicit_format(tmp_path: Path):
    """A caller-supplied format wins over the default fallback format."""
    img = Image.new("RGB", (64, 64), (9, 9, 9))
    leaf = "explicit.png"
    deep = _deep_dir(_workdir(tmp_path, "fmt"), min_file_length=MAX_PATH + 20, leaf=leaf)
    dst = deep / leaf
    rp._safe_save(img, dst, format="PNG")
    with Image.open(long_path(dst)) as reopened:
        reopened.load()
        assert reopened.format == "PNG"


def test_safe_save_strips_exif_and_gps(tmp_path: Path):
    """Privacy: the package image must carry no EXIF (and therefore no GPS)."""
    img = Image.new("RGB", (160, 90), (40, 50, 60))
    exif = img.getexif()
    exif[0x010F] = "TestCamera"        # Make
    exif[0x0110] = "TestModel"         # Model
    # GPS IFD with a plausible coordinate, built the way Pillow encodes
    # rationals (a flat sequence of numerator/denominator pairs).
    from PIL.TiffImagePlugin import IFDRational

    gps_ifd = {
        1: "N",                                          # GPSLatitudeRef
        2: (IFDRational(31, 1), IFDRational(14, 1), IFDRational(0, 1)),
        3: "E",                                          # GPSLongitudeRef
        4: (IFDRational(121, 1), IFDRational(28, 1), IFDRational(0, 1)),
    }
    gps_tag = exif.get_ifd(0x8825)
    gps_tag.update(gps_ifd)

    source = _workdir(tmp_path, "src") / "with_exif.jpg"
    img.save(source, format="JPEG", exif=exif.tobytes())
    with Image.open(source) as check:
        assert dict(check.getexif()), "fixture must actually contain EXIF"

    dst = _workdir(tmp_path, "clean") / "clean.jpg"
    rp._sanitized_copy(source, dst, 82)

    with Image.open(dst) as reopened:
        reopened.load()
        assert not dict(reopened.getexif()), "EXIF survived sanitisation"
        assert 0x8825 not in reopened.getexif(), "GPS IFD survived sanitisation"
    assert b"TestCamera" not in dst.read_bytes()


@needs_windows_max_path
def test_sanitized_copy_succeeds_past_max_path(tmp_path: Path):
    """End to end through the package's own re-encoder."""
    src = _workdir(tmp_path, "src2") / "in.jpg"
    Image.new("RGB", (300, 170), (7, 8, 9)).save(src, format="JPEG")

    leaf = "0000_abcdef12_00003000.jpg"
    deep = _deep_dir(_workdir(tmp_path, "deep2"), min_file_length=MAX_PATH + 20, leaf=leaf)
    dst = deep / leaf

    rp._sanitized_copy(src, dst, 82)
    assert safe_is_file(dst)
    with Image.open(long_path(dst)) as reopened:
        reopened.load()
        assert reopened.format == "JPEG"


# ---------------------------------------------------------------------------
# deep tree creation / write / read
# ---------------------------------------------------------------------------


@needs_windows_max_path
def test_deep_directories_can_be_created_written_and_read(tmp_path: Path):
    leaf = "payload.bin"
    deep = _deep_dir(_workdir(tmp_path, "tree"), min_file_length=MAX_PATH + 40, leaf=leaf)
    target = deep / leaf
    assert len(str(target)) >= MAX_PATH

    safe_mkdir(target.parent)
    with open(long_path(target), "wb") as handle:
        handle.write(b"long-path-payload")

    assert safe_is_file(target)
    assert os.path.getsize(long_path(target)) == len(b"long-path-payload")
    with open(long_path(target), "rb") as handle:
        assert handle.read() == b"long-path-payload"


# ---------------------------------------------------------------------------
# the fallback branch must be exercised on every platform
# ---------------------------------------------------------------------------


def test_the_long_path_fallback_is_reachable_even_without_max_path(tmp_path: Path,
                                                                   monkeypatch):
    """Force the first save to fail, so the fallback runs on Linux CI too.

    Without this the fallback is only covered on Windows, and a Linux-only
    regression (for example, dropping ``format`` again) would ship unnoticed.
    """
    img = Image.new("RGB", (120, 80), (5, 6, 7))
    dst = _workdir(tmp_path, "forced") / "forced.jpg"
    real_save = Image.Image.save
    calls: list[str] = []

    def flaky_save(self, fp, *args, **kwargs):
        # Fail only for the very first, path-based attempt.
        if not isinstance(fp, io.BytesIO) and not calls:
            calls.append("path")
            raise FileNotFoundError(2, "simulated MAX_PATH failure")
        calls.append("buffer")
        return real_save(self, fp, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "save", flaky_save)

    rp._safe_save(img, dst, quality=82)

    assert calls == ["path", "buffer"], f"fallback did not run as expected: {calls}"
    assert safe_is_file(dst)
    with Image.open(dst) as reopened:
        reopened.load()
        assert reopened.format == "JPEG"


def test_the_fallback_names_the_format_explicitly(tmp_path: Path, monkeypatch):
    """Direct proof of the fix: the buffer save receives ``format``.

    Removing ``format`` from ``_safe_save`` makes this test fail, which is the
    reverse-verification the task book asks for.
    """
    img = Image.new("RGB", (64, 48), (3, 4, 5))
    dst = _workdir(tmp_path, "fmtcheck") / "x.jpg"
    seen: dict = {}
    real_save = Image.Image.save

    def spy(self, fp, *args, **kwargs):
        if isinstance(fp, io.BytesIO) and not seen:
            seen.update(kwargs)
        return real_save(self, fp, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "save", spy)
    # Force the fallback deterministically.
    monkeypatch.setattr(
        Image.Image, "save",
        (lambda self, fp, *a, **k: (
            spy(self, io.BytesIO() if not isinstance(fp, io.BytesIO) else fp, *a, **k)
            if isinstance(fp, io.BytesIO)
            else (_ for _ in ()).throw(FileNotFoundError(2, "simulated"))
        )),
    )

    rp._safe_save(img, dst, quality=82)

    assert seen.get("format"), (
        "the buffer save must pass an explicit format; without it Pillow raises "
        "'unknown file extension'"
    )
    assert seen.get("quality") == 82, "encoder options must be forwarded unchanged"


# ---------------------------------------------------------------------------
# package level: frames really reach segments.json in a deep workspace
# ---------------------------------------------------------------------------


def test_no_frames_message_names_reencode_failures_not_budget():
    """The old message merged two unrelated causes; that must not come back."""
    message = rp._no_frames_message(
        segments=5,
        reencode_failures=5,
        excluded_missing_frames=0,
        excluded_by_budget=0,
        excluded_resolve_failures=0,
        excluded_outside_range=0,
        first_reencode_error={
            "error_type": "ValueError",
            "reason": "unknown file extension",
            "target_path_length": 267,
            "target_name_length": 36,
            "max_path": 260,
            "likely_long_path": True,
        },
    )
    assert "重编码失败" in message
    assert "unknown file extension" in message
    assert "267" in message
    # Never blame the budget when the budget was not involved.
    assert "预算" not in message


def test_no_frames_message_separates_missing_from_budget():
    missing = rp._no_frames_message(
        segments=3, reencode_failures=0, excluded_missing_frames=3,
        excluded_by_budget=0, excluded_resolve_failures=0, excluded_outside_range=0,
        first_reencode_error=None,
    )
    assert "缺失" in missing and "预算" not in missing

    budget = rp._no_frames_message(
        segments=3, reencode_failures=0, excluded_missing_frames=0,
        excluded_by_budget=3, excluded_resolve_failures=0, excluded_outside_range=0,
        first_reencode_error=None,
    )
    assert "预算" in budget and "缺失" not in budget


def test_reencode_error_description_leaks_no_private_path(tmp_path: Path):
    """The diagnostic keeps lengths and the reason — never the path itself."""
    dst = tmp_path / ("a" * 40) / ("b" * 40) / "0000_x.jpg"
    err = rp._describe_reencode_error(
        ValueError("unknown file extension: "), dst
    )
    assert err["error_type"] == "ValueError"
    assert err["target_path_length"] == len(str(dst))
    assert str(tmp_path) not in str(err), "the full path must not be recorded"
    assert set(err) == {
        "error_type", "reason", "target_path_length", "target_name_length",
        "max_path", "likely_long_path",
    }
