"""TSB-328 — persist and read native-video observations.

Sits between the schema module (pure validation) and the job runner (TSB-329).
Two responsibilities, both borrowed deliberately from the image path so the three
result families behave identically:

* **Publication is re-checked inside the write transaction.** A model call can
  take tens of seconds; a person may trim the shot, mark it excluded, or the file
  may change in that window. The check runs in the same transaction as the insert,
  so a stale answer cannot be committed (``validate_publication``).
* **Versioning never overwrites history.** The version key is derived from the
  inputs, so an identical forced re-run replaces the current row in place while a
  changed input appends a new one and demotes the old to ``is_current=False``.

The clip fingerprint is part of the key: if the derived clip bytes differ, the
observation describes different evidence and must not be treated as the same
result, even when the prompt and model are unchanged.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from sqlalchemy import select, update

from ...database import begin_write, session_scope
from ...models import (
    Asset,
    Job,
    Segment,
    SegmentVideoObservation,
    utcnow,
)
from .publication import validate_publication
from .verdicts import digest, source_identity
from .video_schema import (
    VIDEO_SCHEMA_VERSION,
    ValidatedVideoObservation,
)

logger = logging.getLogger(__name__)

#: The job type allowed to publish video observations. Kept as a constant so the
#: publication check cannot be satisfied by an unrelated job.
VIDEO_JOB_TYPE = "video_analyze"


@dataclass
class VideoCandidate:
    """One shot queued for video analysis, with its resolved coordinates."""

    segment_id: str
    asset_id: str
    project_id: str
    source_path: str
    #: The *editable* interval in force when the candidate was selected.
    start_sec: float
    end_sec: float
    source_identity: str = ""
    user_rating: str = "pending"

    def snapshot(self) -> dict:
        return {
            "segment_id": self.segment_id,
            "asset_id": self.asset_id,
            "start_sec": self.start_sec,
            "end_sec": self.end_sec,
            "source_signature": self.source_identity,
        }


def version_key(
    *,
    candidate: VideoCandidate,
    clip_sha256: str,
    model_id: str,
    prompt_version: str,
    schema_version: str,
    protocol: str,
    method: str,
) -> str:
    """Identity of every input that decides this result.

    ``clip_sha256`` is included on purpose: two runs over different derived bytes
    are different evidence, and collapsing them would let a re-encode silently
    inherit an older observation.
    """
    return digest(
        "|".join(
            [
                candidate.segment_id,
                f"{candidate.start_sec:.3f}",
                f"{candidate.end_sec:.3f}",
                candidate.source_identity or "",
                clip_sha256 or "",
                model_id or "",
                prompt_version or "",
                schema_version or "",
                protocol or "",
                method or "",
            ]
        )
    )


def store_observation(
    *,
    candidate: VideoCandidate,
    validated: ValidatedVideoObservation,
    clip,
    job_id: str,
    provider_name: str,
    model_id: str,
    prompt_version: str = "1.0",
    protocol: str = "",
    usage: dict | None = None,
    method: str = "video_native",
) -> str:
    """Persist one validated observation, demoting any previous current row.

    Returns the row id. Raises ``ModelError`` (via ``validate_publication``) when
    the shot, source or job changed while the model was working — the caller
    records that as a visible failure rather than publishing a stale answer.
    """
    src_sig = candidate.source_identity
    key = version_key(
        candidate=candidate,
        clip_sha256=clip.sha256,
        model_id=model_id,
        prompt_version=prompt_version,
        schema_version=VIDEO_SCHEMA_VERSION,
        protocol=protocol,
        method=method,
    )

    with session_scope() as s:
        begin_write(s)
        # Same-transaction re-check: cancellation, job validity, segment/source
        # drift. Runs before the insert so nothing stale can be committed.
        validate_publication(s, job_id, candidate.project_id, candidate.snapshot(), VIDEO_JOB_TYPE)

        existing = s.scalars(
            select(SegmentVideoObservation).where(
                SegmentVideoObservation.segment_id == candidate.segment_id,
                SegmentVideoObservation.version_key == key,
            )
        ).first()

        # Demote the previous current row for this shot, whichever version it was.
        s.execute(
            update(SegmentVideoObservation)
            .where(
                SegmentVideoObservation.segment_id == candidate.segment_id,
                SegmentVideoObservation.is_current.is_(True),
            )
            .values(is_current=False)
        )

        payload = {
            "asset_id": candidate.asset_id,
            "status": "succeeded",
            "method": method,
            "model_provider": provider_name,
            "model_id": model_id,
            "prompt_version": prompt_version,
            "schema_version": VIDEO_SCHEMA_VERSION,
            "protocol": protocol,
            "source_signature": src_sig,
            "clip_sha256": clip.sha256,
            "version_key": key,
            "clip_start_sec": clip.source_clip_start_sec,
            "clip_end_sec": clip.source_clip_end_sec,
            "clip_duration_sec": clip.duration_sec,
            "clip_size_bytes": clip.size_bytes,
            "segment_start_sec": candidate.start_sec,
            "segment_end_sec": candidate.end_sec,
            "analysis_json": {
                "summary": validated.summary,
                "limitations": validated.limitations,
                "dropped": validated.dropped,
                "clip_notes": list(getattr(clip, "notes", []) or []),
                "rotation": getattr(clip, "rotation", 0),
                "vfr": getattr(clip, "vfr", False),
            },
            "events_json": [event.to_dict() for event in validated.events],
            "request_usage": {
                **(usage or {}),
                "clip_bytes_sent": clip.size_bytes,
                "clip_sha256": clip.sha256,
                "clip_duration_sec": round(clip.duration_sec, 3),
            },
            # These three travel with the row so a reader does not have to infer
            # them, and so the UI cannot accidentally show green success for an
            # analysis that was partial (task book TSB-331).
            "audio_analyzed": False,
            "color_unverified": bool(getattr(clip, "color_unverified", False)),
            "observed_temporal_quality": validated.observed_temporal_quality,
            "is_current": True,
            "error_code": "",
            "error_message": "",
            "updated_at": utcnow(),
        }

        if existing is not None:
            for fieldname, value in payload.items():
                setattr(existing, fieldname, value)
            row_id = existing.id
        else:
            row = SegmentVideoObservation(
                project_id=candidate.project_id,
                segment_id=candidate.segment_id,
                **payload,
            )
            s.add(row)
            s.flush()
            row_id = row.id

    return row_id


def find_current(segment_id: str) -> SegmentVideoObservation | None:
    """The live observation for a shot, or None."""
    with session_scope() as s:
        return s.scalars(
            select(SegmentVideoObservation).where(
                SegmentVideoObservation.segment_id == segment_id,
                SegmentVideoObservation.is_current.is_(True),
            )
        ).first()


def already_done(
    *,
    candidate: VideoCandidate,
    clip_sha256: str,
    model_id: str,
    prompt_version: str,
    protocol: str,
    job_id: str,
    method: str = "video_native",
) -> bool:
    """True when a *successful* observation already exists for these exact inputs.

    A failed row does not count: the task book requires a retry to actually retry,
    and counting a failure as "done" is how a batch reports success while having
    produced nothing.
    """
    key = version_key(
        candidate=candidate,
        clip_sha256=clip_sha256,
        model_id=model_id,
        prompt_version=prompt_version,
        schema_version=VIDEO_SCHEMA_VERSION,
        protocol=protocol,
        method=method,
    )
    with session_scope() as s:
        begin_write(s)
        validate_publication(s, job_id, candidate.project_id, candidate.snapshot(), VIDEO_JOB_TYPE)
        row = s.scalars(
            select(SegmentVideoObservation).where(
                SegmentVideoObservation.segment_id == candidate.segment_id,
                SegmentVideoObservation.version_key == key,
                SegmentVideoObservation.status == "succeeded",
            )
        ).first()
        return row is not None


def record_failure(
    *,
    candidate: VideoCandidate,
    job_id: str,
    error_code: str,
    error_message: str,
    model_id: str = "",
    protocol: str = "",
) -> None:
    """Persist a failed attempt so the failure is visible and retryable (C6).

    Deliberately does *not* demote the current successful row: a failed re-run
    must not destroy the last good reading.
    """
    for_version = digest(f"fail|{candidate.segment_id}|{error_code}|{utcnow().isoformat()}")
    with session_scope() as s:
        begin_write(s)
        s.add(
            SegmentVideoObservation(
                project_id=candidate.project_id,
                asset_id=candidate.asset_id,
                segment_id=candidate.segment_id,
                status="failed",
                method="video_native",
                model_id=model_id,
                protocol=protocol,
                schema_version=VIDEO_SCHEMA_VERSION,
                source_signature=candidate.source_identity,
                segment_start_sec=candidate.start_sec,
                segment_end_sec=candidate.end_sec,
                version_key=for_version,
                is_current=False,
                error_code=error_code[:64],
                error_message=error_message[:2000],
            )
        )


def observation_payload(row: SegmentVideoObservation) -> dict:
    """Serialise one row for the API.

    The shape is explicit rather than ``row.__dict__`` so an internal column can
    never leak into the response by accident, and so the honesty flags are always
    present even when false.
    """
    return {
        "id": row.id,
        "segment_id": row.segment_id,
        "asset_id": row.asset_id,
        "status": row.status,
        "method": row.method,
        "is_current": bool(row.is_current),
        "model_id": row.model_id,
        "prompt_version": row.prompt_version,
        "schema_version": row.schema_version,
        "protocol": row.protocol,
        "summary": (row.analysis_json or {}).get("summary", ""),
        "events": row.events_json or [],
        "limitations": (row.analysis_json or {}).get("limitations", []),
        "dropped": (row.analysis_json or {}).get("dropped", []),
        "clip": {
            "start_sec": row.clip_start_sec,
            "end_sec": row.clip_end_sec,
            "duration_sec": row.clip_duration_sec,
            "size_bytes": row.clip_size_bytes,
            "sha256": row.clip_sha256,
        },
        "segment_window": {
            "start_sec": row.segment_start_sec,
            "end_sec": row.segment_end_sec,
        },
        # Honesty surface: a consumer must be able to tell an incomplete reading
        # from a complete one without parsing prose.
        "audio_analyzed": bool(row.audio_analyzed),
        "color_unverified": bool(row.color_unverified),
        "observed_temporal_quality": row.observed_temporal_quality,
        "request_usage": row.request_usage or {},
        "error_code": row.error_code,
        "error_message": row.error_message,
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "updated_at": row.updated_at.isoformat() if row.updated_at else "",
    }


def coverage_report(project_id: str) -> dict:
    """How much of the project actually has a video reading.

    Reports the subset honestly: an analysis of 8 shots in a 99-shot library is
    "8 of 99", never "analysed" (the M5 lesson, restated for video).
    """
    with session_scope() as s:
        total_segments = len(
            s.scalars(select(Segment.id).where(Segment.project_id == project_id)).all()
        )
        current = s.scalars(
            select(SegmentVideoObservation).where(
                SegmentVideoObservation.project_id == project_id,
                SegmentVideoObservation.is_current.is_(True),
                SegmentVideoObservation.status == "succeeded",
            )
        ).all()
        failed = s.scalars(
            select(SegmentVideoObservation.id).where(
                SegmentVideoObservation.project_id == project_id,
                SegmentVideoObservation.status == "failed",
            )
        ).all()

    covered = {row.segment_id for row in current}
    events = sum(len(row.events_json or []) for row in current)
    return {
        "library_segments": total_segments,
        "segments_with_video_reading": len(covered),
        "segments_not_covered": max(0, total_segments - len(covered)),
        "failed_attempts": len(failed),
        "event_candidates": events,
        "complete": total_segments > 0 and len(covered) >= total_segments,
        "method": "video_native",
        "audio_analyzed": False,
    }


def clip_fingerprint(data: bytes) -> str:
    """SHA-256 of derived clip bytes, used as part of the version key."""
    return hashlib.sha256(data).hexdigest()
