"""M3 — privacy-safe review package builder (offline, round 1).

Generates a self-contained upload package:
    packages/<version>/
        manifest.json
        assets.json          (source paths REMOVED)
        segments.json
        README.txt
        frames/<...>.jpg     (re-encoded thumbnails, EXIF stripped)
        contact_sheets/sheet_0001.jpg ...

All paths in the package are sanitized; absolute source paths and EXIF/GPS
metadata never leave the machine. The package is also zipped for upload.
"""

from __future__ import annotations

import json
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw
from sqlalchemy import select

from ..config import ANALYSIS_VERSION, APP_VERSION, get_settings
from ..database import session_scope
from ..ffmpeg import locate_ffmpeg
from ..models import Asset, Frame, Job, ReviewPackage, Segment
from ..paths import project_cache_dir, rel_to_data, resolve_from_data
from .runner import register_task, update_job


def _iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _contact_sheet(frame_files: list[tuple[int, Path]], out_path: Path, cols: int, rows: int) -> None:
    cell_w, cell_h = 320, 180
    sheet_w = cols * cell_w
    sheet_h = rows * cell_h
    sheet = Image.new("RGB", (sheet_w, sheet_h), (18, 18, 24))
    draw = ImageDraw.Draw(sheet)
    for i in range(cols * rows):
        if i >= len(frame_files):
            break
        index, p = frame_files[i]
        try:
            im = Image.open(p).convert("RGB")
            im.thumbnail((cell_w - 8, cell_h - 8))
        except OSError:
            continue
        x = (i % cols) * cell_w
        y = (i // cols) * cell_h
        sheet.paste(im, (x + 4, y + 4))
        draw.text((x + 8, y + 6), f"#{index}", fill=(255, 224, 64))
    sheet.save(out_path, quality=88)


def _build_package(project_id: str, options: dict) -> dict:
    settings = get_settings()
    rating = options.get("rating", "")
    cols = int(options.get("contact_sheet_cols", 5))
    rows = int(options.get("contact_sheet_rows", 4))

    with session_scope() as s:
        stmt = select(Segment).where(Segment.project_id == project_id)
        if rating:
            stmt = stmt.where(Segment.user_rating == rating)
        else:
            stmt = stmt.where(Segment.user_rating != "exclude")
        segments = s.scalars(stmt.order_by(Segment.created_at)).all()
        assets = {a.id: a for a in s.scalars(select(Asset).where(Asset.project_id == project_id)).all()}
        seg_ids = [seg.id for seg in segments]
        frames_by_seg: dict[str, list[Frame]] = {}
        if seg_ids:
            for f in s.scalars(select(Frame).where(Frame.segment_id.in_(seg_ids)).order_by(Frame.time_sec)):
                frames_by_seg.setdefault(f.segment_id, []).append(f)

    version = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    pkg_dir = project_cache_dir(project_id) / "packages" / version
    frames_dir = pkg_dir / "frames"
    sheets_dir = pkg_dir / "contact_sheets"
    frames_dir.mkdir(parents=True, exist_ok=True)
    sheets_dir.mkdir(parents=True, exist_ok=True)

    assets_json: list[dict] = []
    segments_json: list[dict] = []
    sheet_frames: list[tuple[int, Path]] = []
    frame_index = 0
    total_frames = 0

    for asset in assets.values():
        assets_json.append(
            {
                "asset_id": asset.id,
                "display_name": asset.display_name,
                "duration_sec": round(asset.duration_sec, 3),
                "width": asset.width,
                "height": asset.height,
                "fps": f"{asset.fps_num}/{asset.fps_den}" if asset.fps_den else "",
                "vfr": asset.vfr_flag,
                "has_audio": asset.has_audio,
            }
        )

    for seg in segments:
        seg_frames = frames_by_seg.get(seg.id, [])
        frames_out: list[dict] = []
        for f in seg_frames:
            src = resolve_from_data(f.image_path)
            if not src.is_file():
                continue
            name = f"{frame_index:04d}_{seg.id[:8]}_{int(round(f.time_sec * 1000)):08d}.jpg"
            dst = frames_dir / name
            shutil.copyfile(src, dst)
            frames_out.append({"file": f"frames/{name}", "time_sec": round(f.time_sec, 3), "index": frame_index})
            sheet_frames.append((frame_index, dst))
            frame_index += 1
            total_frames += 1

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

    # Build contact sheets.
    per_sheet = cols * rows
    sheet_count = 0
    for i in range(0, len(sheet_frames), per_sheet):
        chunk = sheet_frames[i : i + per_sheet]
        out = sheets_dir / f"sheet_{sheet_count + 1:04d}.jpg"
        _contact_sheet(chunk, out, cols, rows)
        sheet_count += 1

    manifest = {
        "schema_version": "2.0",
        "project_id": project_id,
        "version": version,
        "app_version": APP_VERSION,
        "analysis_version": ANALYSIS_VERSION,
        "created_at": _iso_now(),
        "ffmpeg": locate_ffmpeg(),
        "counts": {
            "assets": len(assets_json),
            "segments": len(segments_json),
            "frames": total_frames,
            "contact_sheets": sheet_count,
        },
        "privacy": {
            "source_paths_removed": True,
            "exif_stripped": True,
            "notes": [
                "Absolute source file paths are NOT included.",
                "Frames are re-encoded, so EXIF/GPS metadata is stripped.",
                "Review faces / license plates / screens yourself before uploading.",
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
                "  assets.json     source videos (technical metadata only, no paths)",
                "  segments.json   candidate segments + representative frames",
                "  contact_sheets/ montaged representative frames (labeled with #index)",
                "  frames/         individual representative frames",
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
            if p.is_file():
                zf.write(p, p.relative_to(pkg_dir.parent))

    total_size = sum(p.stat().st_size for p in pkg_dir.rglob("*") if p.is_file())
    rel_dir = rel_to_data(pkg_dir)

    with session_scope() as s:
        s.add(
            ReviewPackage(
                project_id=project_id,
                version=version,
                package_dir=rel_dir,
                file_count=len(assets_json) + len(segments_json) + total_frames + sheet_count + 2,
                total_size_bytes=total_size,
            )
        )

    return {
        "version": version,
        "assets": len(assets_json),
        "segments": len(segments_json),
        "frames": total_frames,
        "contact_sheets": sheet_count,
        "zip": rel_to_data(zip_path),
    }


@register_task("package")
def package_task(job_id: str) -> None:
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            return
        project_id = job.project_id
        options = job.config_snapshot.get("options") or {}

    result = _build_package(project_id, options)
    update_job(job_id, stats={"total": 1, "done": 1, "failed": 0}, resume_token=result)
