"""Independent human audit and read-time validity projection (TSB-314)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sqlalchemy import select

from ...ffmpeg import safe_stat
from ...models import Asset, Segment, SegmentMotionVerdict
from ..indexer import quick_fingerprint


class VerdictConflict(ValueError):
    pass


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


def source_identity(asset: Asset) -> str:
    """Bounded source sampling; never persist a source path in public evidence."""
    try:
        st = safe_stat(asset.source_path)
        if st is None:
            return ""
        sampled = quick_fingerprint(Path(asset.source_path), st.st_size)
        after = safe_stat(asset.source_path)
        identity = lambda stat: (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        if after is None or identity(st) != identity(after):
            return ""
        return digest([asset.project_id, asset.id, asset.source_path, identity(st), sampled,
                       asset.quick_fingerprint, asset.analyzed_fingerprint,
                       asset.analyzed_source_signature])
    except OSError:
        return ""


def input_identity(project_id, segment, frames_hash, source_sig) -> str:
    if not source_sig:
        return ""
    return digest(["motion-input-v1", project_id, segment["asset_id"], segment["segment_id"],
                   segment["start_sec"], segment["end_sec"], source_sig, frames_hash])


def evidence_identity(row) -> str:
    return digest([row.input_signature, row.method, row.model_id, row.prompt_version,
                   row.analysis_json or {}])


def invalid_reason(session, row) -> str:
    segment = session.get(Segment, row.segment_id)
    asset = session.get(Asset, row.asset_id)
    if (segment is None or asset is None or segment.project_id != row.project_id
            or segment.asset_id != row.asset_id or asset.project_id != row.project_id):
        return "SEGMENT_INVALID"
    if segment.status in ("needs_review", "archived", "invalid") or segment.user_rating == "exclude":
        return "SEGMENT_INVALID"
    if (segment.start_sec, segment.end_sec) != (row.start_sec, row.end_sec):
        return "INTERVAL_CHANGED"
    if not row.input_signature:
        return "LEGACY_EVIDENCE_UNKNOWN"
    current = source_identity(asset)
    if not current or current != (row.evidence_json or {}).get("source_signature"):
        return "SOURCE_CHANGED"
    return ""


def human_projection(session, row, *, limit=20, offset=0) -> dict:
    stmt = select(SegmentMotionVerdict).where(
        SegmentMotionVerdict.project_id == row.project_id,
        SegmentMotionVerdict.asset_id == row.asset_id,
        SegmentMotionVerdict.segment_id == row.segment_id,
    ).order_by(SegmentMotionVerdict.revision.desc())
    latest = session.scalars(stmt.limit(1)).first()
    reason = invalid_reason(session, row) if latest else ""
    effective = bool(latest and not reason and latest.input_signature
                     and latest.input_signature == row.input_signature
                     and latest.evidence_signature == evidence_identity(row))
    if latest and not effective and not reason:
        reason = "EVIDENCE_CHANGED"
    state = "confirmed_current" if effective else "needs_reconfirmation" if latest else "not_reviewed"
    history = [{"id": v.id, "motion_review_id": v.motion_review_id,
                "input_signature": v.input_signature, "verdict": v.verdict,
                "note": v.note, "created_at": v.created_at,
                "is_effective": bool(effective and v.id == latest.id)}
               for v in session.scalars(stmt.limit(limit).offset(offset)).all()]
    return {"human_state": state, "human_verdict_id": latest.id if effective else None,
            "human_invalid_reason": reason, "human_history": history,
            "human_verdict": latest.verdict if effective else "",
            "human_note": latest.note if effective else "",
            "human_at": latest.created_at if effective else None}
