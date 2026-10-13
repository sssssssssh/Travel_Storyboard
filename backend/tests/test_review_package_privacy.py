"""Audit P1-07 — analysis package privacy claims must be *enforced*.

These tests build a real package (Pillow only, no FFmpeg) and inspect the
resulting bytes, so the "EXIF stripped / no paths / only used assets" claims in
the manifest are verified rather than asserted in prose.
"""

from __future__ import annotations

import json
import uuid
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from app.database import session_scope
from app.models import Asset, Frame, ReviewPackage, Segment
from app.paths import frames_dir, project_cache_dir
from app.services.review_package import (
    DEFAULT_FRAME_QUALITY,
    _Budget,
    _build_package,
    _sanitized_copy,
    _tool_label,
)


def _jpeg_with_exif(path: Path, size=(640, 360)) -> None:
    """Write a JPEG carrying recognisable ASCII in its metadata."""
    im = Image.new("RGB", size, (120, 90, 60))
    exif = Image.Exif()
    exif[0x010F] = "SecretCameraMake"  # Make
    exif[0x0110] = "SecretCameraModel"  # Model
    exif[0x0131] = "SecretSoftware"  # Software
    exif[0x9286] = "SecretUserComment"  # UserComment
    exif[0x0112] = 1  # Orientation
    path.parent.mkdir(parents=True, exist_ok=True)
    im.save(path, format="JPEG", exif=exif, quality=95)


def _seed_project(client, name: str, n_assets: int = 1, frames_per_seg: int = 2):
    """Create a project with real JPEGs on disk and matching DB rows."""
    res = client.post("/api/projects", json={"name": name, "source_roots": []})
    assert res.status_code in (200, 201), res.text
    project_id = res.json()["id"]

    asset_ids: list[str] = []
    seg_ids: list[str] = []
    for a in range(n_assets):
        asset_id = str(uuid.uuid4())
        # A source path that looks like a private absolute location.
        src = str(project_cache_dir(project_id) / f'private album_{a}.mp4')
        Path(src).parent.mkdir(parents=True, exist_ok=True)
        Path(src).write_bytes(b'generated-source-placeholder')
        with session_scope() as s:
            s.add(
                Asset(
                    id=asset_id,
                    project_id=project_id,
                    source_path=src,
                    display_name=f"album_{a}.mp4",
                    duration_sec=30.0,
                    width=1920,
                    height=1080,
                    fps_num=30,
                    fps_den=1,
                    has_audio=True,
                    scan_status="succeeded",
                )
            )
        asset_ids.append(asset_id)

        seg_id = str(uuid.uuid4())
        with session_scope() as s:
            s.add(
                Segment(
                    id=seg_id,
                    project_id=project_id,
                    asset_id=asset_id,
                    detected_start_sec=0.0,
                    detected_end_sec=10.0,
                    start_sec=1.0,
                    end_sec=9.0,
                    boundary_reason="hard_cut",
                )
            )
        seg_ids.append(seg_id)

        for i in range(frames_per_seg):
            frame_id = str(uuid.uuid4())
            jpg = frames_dir(project_id, asset_id) / f"{frame_id}.jpg"
            _jpeg_with_exif(jpg)
            rel = jpg.resolve().as_posix()
            with session_scope() as s:
                s.add(
                    Frame(
                        id=frame_id,
                        segment_id=seg_id,
                        asset_id=asset_id,
                        time_sec=float(i + 2),
                        image_path=rel,
                    )
                )

    return project_id, asset_ids, seg_ids


# --- re-encoding actually strips metadata ------------------------------------

def test_sanitized_copy_drops_exif(tmp_path: Path):
    src = tmp_path / "dirty.jpg"
    _jpeg_with_exif(src)

    with Image.open(src) as im:
        assert dict(im.getexif()), "fixture must actually carry EXIF"

    dst = tmp_path / "clean.jpg"
    _sanitized_copy(src, dst, DEFAULT_FRAME_QUALITY)

    with Image.open(dst) as im:
        assert dict(im.getexif()) == {}

    raw = dst.read_bytes()
    for needle in (b"SecretCameraMake", b"SecretCameraModel", b"SecretUserComment"):
        assert needle not in raw, f"{needle!r} survived re-encode"


def test_sanitized_copy_output_is_a_valid_jpeg(tmp_path: Path):
    src = tmp_path / "in.jpg"
    _jpeg_with_exif(src, size=(900, 500))
    dst = tmp_path / "out.jpg"
    _sanitized_copy(src, dst, DEFAULT_FRAME_QUALITY)
    with Image.open(dst) as im:
        assert im.format == "JPEG"
        assert im.size[0] > 0


def test_tool_label_has_no_directory():
    label = _tool_label()
    assert "/" not in label and "\\" not in label
    assert ":" not in label


# --- end-to-end package contents ---------------------------------------------

def test_package_contains_no_source_paths_or_exif(client):
    project_id, _, _ = _seed_project(client, "privacy-pkg")
    result = _build_package(project_id, {})
    pkg_dir = project_cache_dir(project_id) / "packages" / result["version"]

    blob = b"".join(p.read_bytes() for p in pkg_dir.rglob("*") if p.is_file())

    assert b"C:/Users/private" not in blob
    assert b"private album_" not in blob
    assert b"C:\\Users" not in blob
    for needle in (b"SecretCameraMake", b"SecretCameraModel", b"SecretUserComment"):
        assert needle not in blob

    manifest = json.loads((pkg_dir / "manifest.json").read_text(encoding="utf-8"))
    assert ":" not in manifest["ffmpeg"]
    assert manifest["privacy"]["exif_stripped"] is True
    assert manifest["privacy"]["assets_limited_to_selection"] is True
    assert manifest["privacy"]["absolute_paths_removed"] is True
    assert manifest["budget"]["truncated"] is False


def test_package_only_lists_assets_used_by_selection(client):
    """Excluded footage must not be enumerated (audit P1-07)."""
    project_id, asset_ids, seg_ids = _seed_project(client, "selection", n_assets=3)

    with session_scope() as s:
        s.get(Segment, seg_ids[0]).user_rating = "favorite"
        s.get(Segment, seg_ids[1]).user_rating = "exclude"

    result = _build_package(project_id, {"rating": "favorite"})
    pkg_dir = project_cache_dir(project_id) / "packages" / result["version"]

    assets_json = json.loads((pkg_dir / "assets.json").read_text(encoding="utf-8"))
    listed = {a["asset_id"] for a in assets_json}
    assert listed == {asset_ids[0]}, f"expected only the used asset, got {listed}"

    segments_json = json.loads((pkg_dir / "segments.json").read_text(encoding="utf-8"))
    assert {s["segment_id"] for s in segments_json} == {seg_ids[0]}


def test_frames_in_package_are_reencoded_and_readable(client):
    project_id, _, _ = _seed_project(client, "reencoded", frames_per_seg=3)
    result = _build_package(project_id, {})
    pkg_dir = project_cache_dir(project_id) / "packages" / result["version"]

    frames = sorted((pkg_dir / "frames").glob("*.jpg"))
    assert len(frames) == result["frames"] == 3
    for f in frames:
        with Image.open(f) as im:
            assert im.format == "JPEG"
            assert dict(im.getexif()) == {}


def test_contact_sheets_are_produced_and_clean(client):
    project_id, _, _ = _seed_project(client, "sheets", frames_per_seg=2)
    result = _build_package(project_id, {"contact_sheet_cols": 2, "contact_sheet_rows": 1})
    pkg_dir = project_cache_dir(project_id) / "packages" / result["version"]
    sheets = list((pkg_dir / "contact_sheets").glob("*.jpg"))
    assert len(sheets) == result["contact_sheets"] >= 1
    with Image.open(sheets[0]) as im:
        assert dict(im.getexif()) == {}


# --- size budget -------------------------------------------------------------

def test_budget_never_deletes_files_it_does_not_own(tmp_path: Path):
    """Regression: `add()` used to unlink any path it was handed."""
    victim = tmp_path / "important.txt"
    victim.write_bytes(b"x" * 100)

    b = _Budget(limit_bytes=10)  # owned_dir intentionally unset
    assert b.add(victim) is False
    assert b.truncated is True
    assert victim.exists(), "budget deleted a file outside the package"


def test_budget_deletes_only_owned_files(tmp_path: Path):
    owned_dir = tmp_path / "pkg"
    owned_dir.mkdir()
    inside = owned_dir / "frame.jpg"
    inside.write_bytes(b"x" * 100)
    outside = tmp_path / "outside.jpg"
    outside.write_bytes(b"x" * 100)

    b = _Budget(limit_bytes=10, owned_dir=owned_dir)
    assert b.add(inside) is False
    assert not inside.exists()
    assert b.add(outside) is False
    assert outside.exists(), "budget escaped its own directory"


def test_tiny_budget_truncates_package(client):
    project_id, _, _ = _seed_project(client, "tiny-budget", frames_per_seg=2)
    with pytest.raises(ValueError, match='无当前有效镜头.*预算'):
        _build_package(project_id, {"max_package_mb": 1 / 1024})
    assert not list((project_cache_dir(project_id) / 'packages').glob('*.zip'))


def test_generous_budget_does_not_truncate(client):
    project_id, _, _ = _seed_project(client, "big-budget", frames_per_seg=2)
    result = _build_package(project_id, {"max_package_mb": 50})
    assert result["truncated"] is False
    assert result["frames"] == 2


# --- zip + persisted row -----------------------------------------------------

def test_zip_excludes_paths_and_metadata(client):
    project_id, _, _ = _seed_project(client, "zip-check")
    result = _build_package(project_id, {})
    zip_path = project_cache_dir(project_id) / "packages" / f"{result['version']}.zip"
    assert zip_path.is_file()

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        assert not any("C:" in n or "Users" in n for n in names)
        payload = b"".join(zf.read(n) for n in names if not n.endswith("/"))

    assert b"C:/Users/private" not in payload
    for needle in (b"SecretCameraMake", b"SecretUserComment"):
        assert needle not in payload


def test_package_row_is_persisted(client):
    from sqlalchemy import select

    project_id, _, _ = _seed_project(client, "row-check")
    result = _build_package(project_id, {})
    with session_scope() as s:
        row = s.scalars(
            select(ReviewPackage).where(ReviewPackage.project_id == project_id)
        ).first()
    assert row is not None
    assert row.version == result["version"]
    assert row.total_size_bytes > 0
