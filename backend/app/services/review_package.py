"""M3 — privacy-safe review package builder (offline, round 1).

Generates a self-contained upload package:
    packages/<version>/
        manifest.json
        assets.json          (only assets actually used; no paths)
        segments.json
        README.txt
        frames/<...>.jpg     (NEWLY RE-ENCODED thumbnails, EXIF/GPS stripped)
        contact_sheets/sheet_0001.jpg ...

Privacy guarantees (audit P1-07). These are enforced, not merely advertised:

* Frames are decoded and **re-encoded** with Pillow — never copied — so EXIF,
  GPS, camera make/model and thumbnails cannot survive.
* ``assets.json`` lists only the assets referenced by the selected segments;
  the rest of the library is not disclosed.
* No absolute paths anywhere: ``manifest.ffmpeg`` records the tool *name* and
  version only.
* A size budget is enforced; the package is marked as truncated rather than
  silently growing past the limit.

The package is also zipped for upload. Nothing here is a substitute for the
user actually looking at the images: ``manifest.privacy.notes`` says so.
"""

from __future__ import annotations

import json
import zipfile
import uuid
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw
from sqlalchemy import select, func

from ..config import ANALYSIS_VERSION, APP_VERSION
from ..database import begin_write, session_scope
from ..ffmpeg import (
    locate_ffmpeg,
    safe_image_open,
    safe_is_file,
    safe_mkdir,
    safe_stat,
    safe_unlink,
)
from ..models import Asset, Frame, Job, ReviewPackage, Segment
from ..paths import project_cache_dir, rel_to_data, resolve_in_project
from .review_validation import EPSILON_SEC, INVALID_SEGMENT_STATUSES, ReviewValidationError, validate_review, verified_source
from .runner import JobFailures, raise_if_cancelled, register_task, update_job

# Defaults; overridable per job via options.
DEFAULT_MAX_PACKAGE_MB = 200
DEFAULT_FRAME_QUALITY = 82
MAX_FRAME_EDGE = 1280


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tool_label() -> str:
    """Name of the ffmpeg binary only — never its local directory.

    Audit P1-07: writing the resolved absolute path leaked the user's folder
    layout into an upload. The executable name is enough for triage.
    """
    path = locate_ffmpeg()
    return Path(path).name if path else "unavailable"


def _safe_save(image, path: Path, **kwargs) -> None:
    """Save a Pillow image without hitting Windows' MAX_PATH limit."""
    import io as _io

    from ..ffmpeg import long_path

    try:
        image.save(path, **kwargs)
        return
    except (FileNotFoundError, OSError):
        buffer = _io.BytesIO()
        image.save(buffer, **kwargs)
        with open(long_path(path), "wb") as handle:
            handle.write(buffer.getvalue())


def _sanitized_copy(src: Path, dst: Path, quality: int) -> None:
    """Decode *src* and write a clean JPEG to *dst*.

    Re-encoding is what removes EXIF/GPS: Pillow does not carry metadata over
    unless explicitly asked, and we never ask. ``Image.new`` guarantees the
    output has no ``info``/EXIF block even for an input that had one.
    """
    with safe_image_open(src) as im:
        im = im.convert("RGB")
        if max(im.size) > MAX_FRAME_EDGE:
            im.thumbnail((MAX_FRAME_EDGE, MAX_FRAME_EDGE))
        clean = Image.new("RGB", im.size)
        clean.paste(im)
    _safe_save(clean, dst, quality=quality)


@dataclass
class _Budget:
    """Tracks the running package size so the build can stop before it balloons."""

    limit_bytes: int
    #: Only files inside this directory may be deleted when over budget. Without
    #: it, ``add()`` would happily unlink whatever path it was handed.
    owned_dir: Path | None = None
    used_bytes: int = 0
    truncated: bool = False
    skipped: int = 0
    _files: list[tuple[Path, int]] = field(default_factory=list)

    def _is_owned(self, path: Path) -> bool:
        if self.owned_dir is None:
            return False
        try:
            path.resolve().relative_to(self.owned_dir.resolve())
        except (ValueError, OSError):
            return False
        return True

    def add(self, path: Path) -> bool:
        """Record a written file. Returns False when the budget is exhausted."""
        try:
            size = safe_stat(path).st_size
        except OSError:
            size = 0
        if self.used_bytes + size > self.limit_bytes:
            self.truncated = True
            if self._is_owned(path):
                try:
                    safe_unlink(path)
                except OSError:
                    pass
            return False
        self.used_bytes += size
        self._files.append((path, size))
        return True

    def remaining(self) -> int:
        return max(0, self.limit_bytes - self.used_bytes)


def _contact_sheet(
    frame_files: list[tuple[int, Path]],
    out_path: Path,
    cols: int,
    rows: int,
    labels: dict[int, str] | None = None,
) -> None:
    """Montage frames, labelling each cell with something human-meaningful.

    Audit P2-9: a bare ``#index`` does not let a reviewer connect a cell back to
    a shot. Labels carry the asset alias, the time range and a stable id
    fragment, so a recommendation can be traced to a real segment.
    """
    cell_w, cell_h = 320, 200
    sheet = Image.new("RGB", (cols * cell_w, rows * cell_h), (18, 18, 24))
    draw = ImageDraw.Draw(sheet)
    for i in range(cols * rows):
        if i >= len(frame_files):
            break
        index, p = frame_files[i]
        try:
            with safe_image_open(p) as raw:
                im = raw.convert("RGB")
            im.thumbnail((cell_w - 8, cell_h - 40))
        except OSError:
            continue
        x = (i % cols) * cell_w
        y = (i // cols) * cell_h
        sheet.paste(im, (x + 4, y + 4))
        label = (labels or {}).get(index) or f"#{index}"
        # Two lines: alias + time range on the first, stable id on the second.
        first, _, second = label.partition("\n")
        draw.text((x + 8, y + cell_h - 34), first[:46], fill=(255, 224, 64))
        if second:
            draw.text((x + 8, y + cell_h - 18), second[:46], fill=(150, 200, 255))
    _safe_save(sheet, out_path, format="JPEG", quality=88, optimize=True)


def _build_package(project_id: str, options: dict, job_id: str | None = None) -> dict:
    rating = options.get("rating", "")
    cols = int(options.get("contact_sheet_cols", 5))
    rows = int(options.get("contact_sheet_rows", 4))
    max_mb = float(options.get("max_package_mb", DEFAULT_MAX_PACKAGE_MB))
    quality = int(options.get("frame_quality", DEFAULT_FRAME_QUALITY))
    budget = _Budget(limit_bytes=int(max_mb * 1024 * 1024))

    with session_scope() as s:
        base = select(Segment).where(Segment.project_id == project_id)
        total_candidates = s.scalar(select(func.count()).select_from(base.subquery())) or 0
        excluded_by_rating = s.scalar(select(func.count()).select_from(base.where(
            Segment.user_rating != rating if rating else Segment.user_rating == 'exclude').subquery())) or 0
        rated = base.where(Segment.user_rating == rating if rating else Segment.user_rating != 'exclude')
        excluded_needs_review = s.scalar(select(func.count()).select_from(rated.where(
            Segment.status.in_(INVALID_SEGMENT_STATUSES)).subquery())) or 0
        stmt = base.where(Segment.user_rating != 'exclude', Segment.status.not_in(INVALID_SEGMENT_STATUSES))
        if rating:
            stmt = stmt.where(Segment.user_rating == rating)
        else:
            stmt = stmt.where(Segment.user_rating != "exclude")
        segments = s.scalars(stmt.order_by(Segment.created_at)).all()
        seg_ids = [seg.id for seg in segments]
        frames_by_seg: dict[str, list[Frame]] = {}
        if seg_ids:
            for f in s.scalars(select(Frame).where(Frame.segment_id.in_(seg_ids)).order_by(Frame.time_sec)):
                frames_by_seg.setdefault(f.segment_id, []).append(f)
        # Audit P1-07: only the assets that the selected segments actually use.
        # Listing the whole project disclosed the existence of footage the user
        # deliberately left out of this round.
        used_asset_ids = {seg.asset_id for seg in segments}
        assets = {
            a.id: a
            for a in s.scalars(select(Asset).where(Asset.id.in_(used_asset_ids), Asset.project_id == project_id)).all()
        } if used_asset_ids else {}

    # One immutable selection feeds JSON, images and sheets. Sources are read
    # only; invalid/missing media cannot enter an AI package via old thumbnails.
    eligible, excluded_invalid_source = [], 0
    source_snapshots = {}
    for seg in segments:
        with session_scope() as s:
            try:
                asset, current, _, _ = validate_review(s, project_id, seg.asset_id, seg.id,
                                                 seg.start_sec, seg.end_sec)
                source_snapshots[asset.id] = verified_source(asset)
                assets[asset.id] = asset
            except ReviewValidationError:
                excluded_invalid_source += 1
                continue
        eligible.append(current)
    segments = eligible
    if not segments:
        raise ValueError('无当前有效镜头：请重新扫描/检测、人工复核镜头或调整评分筛选')

    # A restarted uncommitted job may leave files from an older selection.
    # Every attempt gets a fresh directory; committed resume_token reuse is
    # handled by package_task, so successful replay still returns one artifact.
    version = (job_id or datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")) + '-' + uuid.uuid4().hex
    pkg_dir = project_cache_dir(project_id) / "packages" / version
    frames_dir = pkg_dir / "frames"
    sheets_dir = pkg_dir / "contact_sheets"
    safe_mkdir(frames_dir)
    safe_mkdir(sheets_dir)
    # From here on the budget may delete files it just wrote — but only inside
    # the package directory.
    budget.owned_dir = pkg_dir

    assets_json: list[dict] = []
    segments_json: list[dict] = []
    sheet_frames: list[tuple[int, Path]] = []
    sheet_labels: dict[int, str] = {}
    frame_index = 0
    total_frames = 0
    reencode_failures = 0
    excluded_missing_frames = 0
    excluded_by_budget = 0

    for seg in segments:
        seg_frames = frames_by_seg.get(seg.id, [])
        alias = (assets.get(seg.asset_id).display_name if assets.get(seg.asset_id) else "unknown")
        frames_out: list[dict] = []
        budget_omitted = False
        for f in seg_frames:
            if not seg.start_sec - EPSILON_SEC <= f.time_sec <= seg.end_sec + EPSILON_SEC:
                continue
            if budget.remaining() <= 0:
                budget_omitted = budget.truncated = True
                budget.skipped += 1
                continue
            try:
                src = resolve_in_project(project_id, f.image_path)
            except ValueError:
                continue
            if not safe_is_file(src):
                continue

            name = f"{frame_index:04d}_{seg.id[:8]}_{int(round(f.time_sec * 1000)):08d}.jpg"
            dst = frames_dir / name
            try:
                _sanitized_copy(src, dst, quality)
            except (OSError, ValueError):
                reencode_failures += 1
                continue

            if not budget.add(dst):
                budget_omitted = True
                # Budget exhausted; stop adding frames but keep the JSON entry
                # valid by simply not referencing a file we deleted.
                budget.skipped += 1
                continue

            frames_out.append(
                {"file": f"frames/{name}", "time_sec": round(f.time_sec, 3), "index": frame_index}
            )
            sheet_frames.append((frame_index, dst))
            # Cell label: alias + time range, then a stable id fragment.
            sheet_labels[frame_index] = (
                f"{alias} {seg.start_sec:.1f}-{seg.end_sec:.1f}s\n"
                f"{seg.id[:8]} @{f.time_sec:.1f}s"
            )
            frame_index += 1
            total_frames += 1

        if not frames_out:
            if budget_omitted:
                excluded_by_budget += 1
            else:
                excluded_missing_frames += 1
            continue
        segments_json.append(
            {
                "segment_id": seg.id,
                "asset_id": seg.asset_id,
                "start_sec": round(seg.start_sec, 3),
                "end_sec": round(seg.end_sec, 3),
                "boundary_reason": seg.boundary_reason,
                "story_role": seg.story_role,
                "user_rating": seg.user_rating,
                "quality_metrics": seg.quality_metrics or {},
                "frames": frames_out,
            }
        )

    included_asset_ids = {seg['asset_id'] for seg in segments_json}
    assets_json = [{
        'asset_id': a.id, 'display_name': a.display_name, 'duration_sec': round(a.duration_sec, 3),
        'width': a.width, 'height': a.height,
        'fps': f'{a.fps_num}/{a.fps_den}' if a.fps_den else '',
        'vfr': a.vfr_flag, 'has_audio': a.has_audio,
    } for a in assets.values() if a.id in included_asset_ids]
    if not segments_json:
        shutil.rmtree(pkg_dir)
        raise ValueError('无当前有效镜头：代表帧缺失或预算不足，请重新检测或增加分析包预算')

    # Build contact sheets.
    per_sheet = cols * rows
    sheet_count = 0
    for i in range(0, len(sheet_frames), per_sheet):
        chunk = sheet_frames[i : i + per_sheet]
        if budget.remaining() <= 0:
            budget.truncated = True
            budget.skipped += len(chunk)
            continue
        out = sheets_dir / f"sheet_{sheet_count + 1:04d}.jpg"
        _contact_sheet(chunk, out, cols, rows, sheet_labels)
        if not budget.add(out):
            budget.skipped += len(chunk)
            continue
        sheet_count += 1

    manifest = {
        "schema_version": "2.0",
        "project_id": project_id,
        "version": version,
        "app_version": APP_VERSION,
        "analysis_version": ANALYSIS_VERSION,
        "created_at": _iso_now(),
        # Tool NAME only — the resolved absolute path used to leak the user's
        # folder layout into an upload (audit P1-07).
        "ffmpeg": _tool_label(),
        "counts": {
            "assets": len(assets_json),
            "segments": len(segments_json),
            "frames": total_frames,
            "contact_sheets": sheet_count,
        },
        "selection": {
            "rating_filter": rating or "(all except excluded)",
            "assets_included": len(assets_json),
            "total_candidates": total_candidates,
            "included_segments": len(segments_json),
            "excluded_by_rating": excluded_by_rating,
            "excluded_needs_review": excluded_needs_review,
            "excluded_invalid_source": excluded_invalid_source,
            "excluded_missing_frames": excluded_missing_frames,
            "excluded_by_budget": excluded_by_budget,
            "package_truncated": budget.truncated,
            "note": (
                "Only assets referenced by the selected segments are listed. "
                "Shots you excluded are not described here."
            ),
        },
        "budget": {
            "max_mb": max_mb,
            "used_bytes": budget.used_bytes,
            "truncated": budget.truncated,
            "items_skipped": budget.skipped,
        },
        "privacy": {
            "source_paths_removed": True,
            "exif_stripped": True,
            "assets_limited_to_selection": True,
            "absolute_paths_removed": True,
            "notes": [
                "Absolute source file paths are NOT included.",
                "Frames are decoded and re-encoded with Pillow, so EXIF/GPS/camera",
                "metadata and embedded thumbnails cannot survive.",
                "Only assets used by the selected segments are listed.",
                "This does NOT make faces, plates, screens or documents anonymous —",
                "review every contact sheet yourself before uploading.",
            ],
        },
        "package": {"contact_sheet_cols": cols, "contact_sheet_rows": rows},
    }

    (pkg_dir / "assets.json").write_text(
        json.dumps(assets_json, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (pkg_dir / "segments.json").write_text(
        json.dumps(segments_json, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (pkg_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (pkg_dir / "README.txt").write_text(
        "\n".join(
            [
                "Travel Storyboard — Round 1 analysis package",
                "=" * 50,
                "",
                "This package contains candidate shots (segments) as contact sheets and",
                "structured JSON. Use it to plan one or more story lines.",
                "",
                "Files:",
                "  manifest.json   package metadata & privacy notes",
                "  assets.json     ONLY the videos used by the selected shots (no paths)",
                "  segments.json   candidate segments + representative frames",
                "  contact_sheets/ montage, each cell labelled with alias, time range",
                "                  and a stable segment id fragment",
                "  frames/         individual representative frames (re-encoded)",
                "",
                "When you reply with review requests, return a JSON object:",
                "  {",
                "    \"schema_version\": \"2.0\",",
                "    \"requests\": [",
                "      {\"asset_id\": \"...\", \"segment_id\": \"...\",",
                "       \"start_sec\": 123.5, \"end_sec\": 135.0,",
                "       \"priority\": \"high\", \"reason\": \"...\"}",
                "    ]",
                "  }",
                "The app will import this file and auto-generate low-res previews for",
                "the requested time ranges — no CSV editing needed.",
                "",
            ]
        ),
        encoding="utf-8",
    )

    # Zip the whole package for upload.
    zip_path = project_cache_dir(project_id) / "packages" / f"{version}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(pkg_dir.rglob("*")):
            if safe_is_file(p):
                zf.write(p, p.relative_to(pkg_dir.parent))

    total_size = sum(
        safe_stat(p).st_size for p in pkg_dir.rglob("*") if safe_is_file(p)
    )
    rel_dir = rel_to_data(pkg_dir)

    result = {
        "version": version,
        "assets": len(assets_json),
        "segments": len(segments_json),
        "frames": total_frames,
        "contact_sheets": sheet_count,
        "zip": rel_to_data(zip_path),
        "truncated": budget.truncated,
        "items_skipped": budget.skipped,
        "reencode_failures": reencode_failures,
        "used_bytes": budget.used_bytes,
        "max_bytes": budget.limit_bytes,
    }

    try:
        with session_scope() as s:
            begin_write(s)
            if job_id:
                raise_if_cancelled(job_id)
            for seg in segments:
                if seg.id not in {g['segment_id'] for g in segments_json}:
                    continue
                asset, current, _, _ = validate_review(s, project_id, seg.asset_id, seg.id,
                                                        seg.start_sec, seg.end_sec)
                fields = ('start_sec', 'end_sec', 'status', 'user_rating')
                if any(getattr(seg, f) != getattr(current, f) for f in fields):
                    raise ReviewValidationError('SELECTION_CHANGED', '镜头选择在打包期间变化，请重新生成分析包')
                if verified_source(asset) != source_snapshots[asset.id]:
                    raise ReviewValidationError('SOURCE_CHANGED', '源文件在打包期间变化，请重新扫描/检测')
            s.add(ReviewPackage(project_id=project_id, version=version, package_dir=rel_dir,
                                file_count=len(assets_json) + len(segments_json) + total_frames + sheet_count + 2,
                                total_size_bytes=total_size))
            if job_id:
                job = s.get(Job, job_id)
                if job is not None:
                    job.resume_token = result
    except Exception:
        # Unique version/job attempt only; never remove historical packages.
        zip_path.unlink(missing_ok=True)
        shutil.rmtree(pkg_dir, ignore_errors=True)
        raise
    return result


@register_task("package")
def package_task(job_id: str) -> None:
    failures = JobFailures(job_id)
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            return
        project_id = job.project_id
        options = (job.config_snapshot or {}).get("options") or {}
        saved = dict(job.resume_token or {})
        committed = s.scalars(select(ReviewPackage).where(
            ReviewPackage.project_id == project_id, ReviewPackage.version == saved.get("version", ""))).first()
        s.expunge(job)

    raise_if_cancelled(job_id)
    result = None
    if committed is not None and saved.get("zip"):
        try:
            if resolve_in_project(project_id, saved["zip"]).is_file():
                result = saved
        except ValueError:
            pass
    if result is None:
        result = _build_package(project_id, options, job_id=job_id)

    if result["reencode_failures"]:
        failures.record(
            kind="frame",
            ref=project_id,
            message=f"{result['reencode_failures']} 张分镜图重编码失败，已从包中排除",
        )
    if result["truncated"]:
        # The package is still usable, but the user must know it is incomplete.
        failures.record(
            kind="budget",
            ref=project_id,
            message=(
                f"分析包达到体积上限，跳过 {result['items_skipped']} 项（"
                f"{result['used_bytes'] // 1024 // 1024}MB / {result['max_bytes'] // 1024 // 1024}MB）"
            ),
        )

    update_job(job_id, stats={"total": 1, "done": 1, "failed": len(failures)}, resume_token=result)
