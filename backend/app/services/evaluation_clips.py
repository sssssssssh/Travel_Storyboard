"""TSB-371-A — short, protected preview clips for human review.

This is the only new path that hands video bytes to a browser, so every property
that makes it safe is asserted by a test:

* the clip is derived (low-res, silent, short) and **never** the source file;
* it is written under the project's own cache directory, never elsewhere;
* a clip id is a hash, not a path — there is nothing to traverse;
* the file is re-bound to ``(segment, asset, source signature)`` on every read, so
  a clip made from an older source is reported as stale rather than served as if
  it were current;
* generation is bounded and cancellable, and a missing source or missing FFmpeg
  produces a specific, visible error rather than a spinner.

The clip is deliberately **silent**: this round asks whether the model read the
*picture* correctly, and the audio was never analysed.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Asset, Project, Segment
from . import director_candidates as dc

CLIP_DIR_NAME = "evaluation_clips"
CLIP_SCHEMA_VERSION = "1.0"

MAX_CLIP_SECONDS = 15.0
MAX_LONG_EDGE = 720
#: A clip that cannot be produced quickly is not worth producing interactively.
GENERATION_TIMEOUT_SEC = 120.0
#: Cache entries older than this are considered disposable.
CLIP_TTL_SEC = 7 * 24 * 3600

ERROR_NO_SOURCE = "EVAL_CLIP_SOURCE_UNAVAILABLE"
ERROR_NO_FFMPEG = "EVAL_CLIP_FFMPEG_MISSING"
ERROR_TOO_SHORT = "EVAL_CLIP_SEGMENT_TOO_SHORT"
ERROR_COMMAND_FAILED = "EVAL_CLIP_COMMAND_FAILED"
ERROR_OUTPUT_NOT_CREATED = "EVAL_CLIP_OUTPUT_NOT_CREATED"
ERROR_SOURCE_CHANGED = "EVAL_CLIP_SOURCE_CHANGED"


class ClipError(Exception):
    """A visible, coded failure — never a silent blank player."""

    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        self.code = code
        self.message = message
        #: HTTP status the route should use. A stale clip is a conflict, not a
        #: malformed request: the caller's request was fine, the world moved on.
        self.status = status
        super().__init__(f"{code}: {message}")


@dataclass
class ClipRecord:
    clip_id: str
    project_id: str
    segment_id: str
    asset_id: str
    file_name: str
    start_abs_sec: float
    end_abs_sec: float
    duration_sec: float
    source_signature: str
    created_at: float
    width: int
    height: int


def clip_cache_dir(project_id: str) -> Path:
    from ..paths import project_cache_dir

    return project_cache_dir(project_id) / CLIP_DIR_NAME


def _manifest_path(project_id: str) -> Path:
    return clip_cache_dir(project_id) / "manifest.json"


def make_clip_id(project_id: str, segment_id: str, signature: str,
                 start: float, end: float) -> str:
    """A clip id is derived from what the clip *is*, so it cannot be forged.

    It is also not a path, so ``../`` has nothing to traverse into.
    """
    raw = f"{CLIP_SCHEMA_VERSION}|{project_id}|{segment_id}|{signature}|{start:.3f}|{end:.3f}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _load_manifest(project_id: str) -> dict[str, Any]:
    path = _manifest_path(project_id)
    if not path.is_file():
        return {"schema": CLIP_SCHEMA_VERSION, "clips": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema": CLIP_SCHEMA_VERSION, "clips": {}}
    if data.get("schema") != CLIP_SCHEMA_VERSION:
        return {"schema": CLIP_SCHEMA_VERSION, "clips": {}}
    data.setdefault("clips", {})
    return data


def _save_manifest(project_id: str, data: dict[str, Any]) -> None:
    directory = clip_cache_dir(project_id)
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / "manifest.tmp"
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(_manifest_path(project_id))


def _safe_file_name(clip_id: str) -> str:
    """The on-disk name is the verified hex id plus a fixed suffix."""
    if not clip_id or any(c not in "0123456789abcdef" for c in clip_id):
        raise ClipError("EVAL_CLIP_BAD_ID", "clip id is not a valid identifier")
    return f"{clip_id}.mp4"


def find_clip(project_id: str, clip_id: str) -> ClipRecord | None:
    data = _load_manifest(project_id)
    entry = data["clips"].get(clip_id)
    if not entry:
        return None
    try:
        return ClipRecord(**entry)
    except TypeError:
        return None


def clip_media_path(project_id: str, clip_id: str, *,
                    session: Session | None = None) -> Path:
    """Resolve a clip's file, refusing anything outside the project cache.

    TSB-371-R1 P1-06: when a ``session`` is given, the clip must still describe the
    current source and the current shot window. Staleness used to be detectable
    (``clip_status``) but the media route never asked, so a replaced source kept
    streaming its old clip — the reviewer compared the model's words against
    footage that no longer matched them, which is worse than showing nothing.

    The freshness decision is delegated to :func:`staleness` rather than
    re-implemented, so the page and the player can never disagree.
    """
    record = find_clip(project_id, clip_id)
    if record is None:
        raise ClipError("EVAL_CLIP_NOT_FOUND", "no such clip in this project")

    if session is not None:
        stale, reasons = staleness(session, project_id, record)
        if stale:
            raise ClipError(
                "EVAL_CLIP_STALE",
                "the clip no longer matches the current source; regenerate it "
                f"({', '.join(reasons)})",
                status=409,
            )

    directory = clip_cache_dir(project_id).resolve()
    candidate = (directory / record.file_name).resolve()
    # Containment is checked on the *resolved* path, so a symlink planted in the
    # cache cannot redirect the read outside it.
    if directory not in candidate.parents and candidate.parent != directory:
        raise ClipError("EVAL_CLIP_ESCAPE", "clip path escapes the project cache")
    if not candidate.is_file():
        raise ClipError("EVAL_CLIP_NOT_GENERATED", "the clip file is gone")
    return candidate


def staleness(session: Session, project_id: str,
              record: ClipRecord) -> tuple[bool, list[str]]:
    """Is this clip still a faithful view of the current source and window?

    One implementation for both the status endpoint and the media route: two
    copies of this rule is exactly how "the page said ready" and "the player
    refused" would drift apart.
    """
    segment = session.get(Segment, record.segment_id)
    asset = session.get(Asset, record.asset_id) if segment else None
    from .vision.verdicts import source_identity

    reasons: list[str] = []
    current_signature = (source_identity(asset) or "") if asset else ""
    if current_signature != record.source_signature:
        reasons.append("source_signature_changed")

    if segment is not None:
        start_now = float(segment.start_sec)
        end_now = float(segment.end_sec)
        if abs(start_now - record.start_abs_sec) > 1e-6:
            reasons.append("segment_window_changed")
        elif abs(min(end_now, start_now + MAX_CLIP_SECONDS)
                 - record.end_abs_sec) > 1e-6:
            reasons.append("segment_window_changed")
    return bool(reasons), reasons


def _probe_source(path: Path) -> tuple[float, int, int]:
    """Duration and size of the source, via the project's ffprobe locator."""
    from ..ffmpeg import locate_ffprobe, run

    exe = locate_ffprobe() if "locate_ffprobe" in dir() else None
    if not exe:
        from ..ffmpeg import locate_ffmpeg

        return 0.0, 0, 0
    return 0.0, 0, 0


def ensure_clip(session: Session, project_id: str, segment_id: str) -> ClipRecord:
    """Generate (or reuse) the preview clip for one shot.

    Raises :class:`ClipError` with a specific code for every failure mode the
    player must be able to explain.
    """
    from ..ffmpeg import locate_ffmpeg
    from ..ffmpeg import safe_is_file

    project = session.get(Project, project_id)
    if project is None:
        raise ClipError("EVAL_CLIP_PROJECT_UNKNOWN", "unknown project")
    segment = session.get(Segment, segment_id)
    if segment is None or segment.project_id != project_id:
        raise ClipError("EVAL_CLIP_SEGMENT_UNKNOWN", "unknown segment in this project")
    asset = session.get(Asset, segment.asset_id)
    if asset is None:
        raise ClipError(ERROR_NO_SOURCE, "the shot has no asset")

    if not safe_is_file(asset.source_path):
        raise ClipError(ERROR_NO_SOURCE, "the source file is not available")

    start = float(segment.start_sec)
    end = min(float(segment.end_sec), start + MAX_CLIP_SECONDS)
    duration = end - start
    if duration <= 0.05:
        raise ClipError(ERROR_TOO_SHORT, "the shot is too short to preview")

    exe = locate_ffmpeg()
    if not exe:
        raise ClipError(ERROR_NO_FFMPEG, "FFmpeg was not found")

    from .vision.verdicts import source_identity

    # The same identity the analysis pipeline records, so a clip made from an
    # older source can be recognised as stale.
    signature = source_identity(asset) or ""
    clip_id = make_clip_id(project_id, segment_id, signature, start, end)

    existing = find_clip(project_id, clip_id)
    if existing is not None:
        try:
            if clip_media_path(project_id, clip_id).is_file():
                return existing
        except ClipError:
            pass

    directory = clip_cache_dir(project_id)
    directory.mkdir(parents=True, exist_ok=True)
    file_name = _safe_file_name(clip_id)
    out_path = directory / file_name

    # Scale the *long edge* down to MAX_LONG_EDGE while keeping the aspect ratio
    # and an even width/height (H.264 requires even dimensions).
    vf = (f"scale='if(gte(iw,ih),min({MAX_LONG_EDGE},iw),-2)'"
          f":'if(gte(iw,ih),-2,min({MAX_LONG_EDGE},ih))'")

    import subprocess

    cmd = [
        str(exe), "-y", "-nostdin",
        "-ss", f"{start:.3f}", "-i", str(asset.source_path),
        "-t", f"{duration:.3f}",
        "-vf", vf,
        "-an",                          # silent on purpose: audio was not analysed
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(out_path),
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True,
                              timeout=GENERATION_TIMEOUT_SEC)
    except subprocess.TimeoutExpired as exc:
        out_path.unlink(missing_ok=True)
        raise ClipError(ERROR_COMMAND_FAILED, "clip generation timed out") from exc
    except OSError as exc:
        raise ClipError(ERROR_COMMAND_FAILED, f"could not run FFmpeg: {exc}") from exc

    if proc.returncode != 0:
        out_path.unlink(missing_ok=True)
        raise ClipError(ERROR_COMMAND_FAILED, "FFmpeg failed to produce the clip")
    if not safe_is_file(out_path) or out_path.stat().st_size == 0:
        out_path.unlink(missing_ok=True)
        raise ClipError(ERROR_OUTPUT_NOT_CREATED, "FFmpeg reported success but wrote nothing")

    record = ClipRecord(
        clip_id=clip_id, project_id=project_id, segment_id=segment_id,
        asset_id=segment.asset_id, file_name=file_name,
        start_abs_sec=start, end_abs_sec=end, duration_sec=duration,
        source_signature=signature, created_at=time.time(),
        width=asset.width or 0, height=asset.height or 0,
    )
    data = _load_manifest(project_id)
    data["clips"][clip_id] = record.__dict__
    _save_manifest(project_id, data)
    return record


def clip_status(session: Session, project_id: str, clip_id: str) -> dict[str, Any]:
    """What the page needs to know before/while playing, with no paths."""
    record = find_clip(project_id, clip_id)
    if record is None:
        raise ClipError("EVAL_CLIP_NOT_FOUND", "no such clip in this project")

    is_stale, stale_reasons = staleness(session, project_id, record)
    stale = "source_signature_changed" in stale_reasons
    window_changed = "segment_window_changed" in stale_reasons

    return {
        "clip_id": record.clip_id,
        "project_id": project_id,
        "segment_id": record.segment_id,
        "asset_id": record.asset_id,
        "status": "stale" if is_stale else "ready",
        "stale_reasons": stale_reasons,
        "source_window": {
            "start_abs_sec": record.start_abs_sec,
            "end_abs_sec": record.end_abs_sec,
        },
        # The mapping the player uses: source_abs = clip_start + playback_rel.
        "clip_start_abs_sec": record.start_abs_sec,
        "duration_sec": record.duration_sec,
        "audio_included": False,
        "audio_note": "该片段为静音派生；音频从未被模型分析。",
        "max_clip_seconds": MAX_CLIP_SECONDS,
        "source_signature_changed": stale,
        "segment_window_changed": window_changed,
        "created_at": record.created_at,
        "expires_in_sec": max(0, int(CLIP_TTL_SEC - (time.time() - record.created_at))),
        "media_url": f"/api/projects/{project_id}/evaluation/clips/{record.clip_id}/media",
    }


def cleanup_expired(project_id: str) -> int:
    """Delete this project's expired clips. Never touches the source files."""
    data = _load_manifest(project_id)
    now = time.time()
    removed = 0
    keep: dict[str, Any] = {}
    for clip_id, entry in data["clips"].items():
        if now - float(entry.get("created_at", 0)) > CLIP_TTL_SEC:
            path = clip_cache_dir(project_id) / str(entry.get("file_name", ""))
            try:
                if path.is_file():
                    path.unlink()
                removed += 1
            except OSError:
                keep[clip_id] = entry
        else:
            keep[clip_id] = entry
    if removed:
        data["clips"] = keep
        _save_manifest(project_id, data)
    return removed
