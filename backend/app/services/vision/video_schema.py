"""TSB-328 — the temporal-event contract for native-video observations.

The model answers with a *clip-relative* window and free-text description. Nothing
it returns is trusted: every field is re-validated here before a row may be
written, because the failure mode is not a crash but a plausible-looking event that
points at the wrong moment of a person's footage.

Rules this module enforces, and why each exists:

* **One clip, one shot.** An observation may only describe the ``clip_id`` /
  ``segment_id`` it was asked about. A model that answers about a different shot
  is not corrected, it is rejected (C3: no automatic re-mapping).
* **Windows must be finite, ordered and inside the clip.** ``validate_temporal_events``
  delegates the arithmetic to :func:`video_clip.map_event_window`, so the boundary
  rules live in exactly one place and cannot drift between the two modules.
* **Confidence is reported, never calibrated.** The task book is explicit that
  ``confidence`` is the model's self-reported number. It is clamped to ``[0, 1]``
  and labelled as uncalibrated wherever it surfaces; no code here multiplies it by
  a probability or calls it accuracy.
* **An event is a candidate, not a finding.** ``event_type`` is a closed set and
  the "wow moment" reading is presented as a candidate for a human to confirm.
* **Identity/place claims are dropped.** Pixels cannot prove who or where; the same
  filter the image path uses applies here.

The module is deliberately free of database and network access so it can be tested
as pure logic.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

from .video_clip import ClipArtifact, VideoClipError, map_event_window

#: Version of this contract. Bump when the accepted shape changes, so stored rows
#: can be told apart from ones produced by an older reading (task book §3 TSB-328.3).
VIDEO_SCHEMA_VERSION = "1.0"
VIDEO_PROMPT_VERSION = "1.0"

#: Closed set of event kinds. An open vocabulary would let the model invent labels
#: that no UI or downstream rule can interpret.
EVENT_TYPES = frozenset(
    {
        "subject_action",
        "camera_motion",
        "scene_change",
        "object_appears",
        "object_leaves",
        "light_change",
        "other",
    }
)

#: How much temporal structure the model claims it could see. ``none`` is a
#: legitimate answer and must survive as-is rather than being normalised away.
TEMPORAL_QUALITY = frozenset({"none", "limited", "good", "unknown"})

#: Evidence kinds an event can cite. ``video_window`` is the only one a native
#: video call can honestly claim; the others exist so an image-based reading that
#: reuses this shape cannot overstate itself.
EVIDENCE_KINDS = frozenset({"video_window", "frame", "uncertain"})

MAX_EVENTS = 30
MAX_SUMMARY_LEN = 500
MAX_DESCRIPTION_LEN = 300
MAX_LIMITATIONS = 10

#: Reasons a whole response is unusable. Stable codes (C6).
INVALID_OUTPUT = "VIDEO_INVALID_OUTPUT"
CLIP_MISMATCH = "VIDEO_CLIP_MISMATCH"
NO_EVENTS = "VIDEO_NO_EVENTS"


class VideoSchemaError(ValueError):
    """A response that cannot be accepted, with a stable code."""

    def __init__(self, code: str, message: str, *, detail: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


@dataclass
class TemporalEvent:
    """One validated, clip-relative event window."""

    event_type: str
    start_rel_sec: float
    end_rel_sec: float
    description: str
    #: The model's own number, clamped. NOT a calibrated probability.
    confidence: float
    evidence: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    #: Filled in by the caller that knows the segment bounds.
    start_abs_sec: float | None = None
    end_abs_sec: float | None = None

    def to_dict(self) -> dict:
        return {
            "event_type": self.event_type,
            "start_rel_sec": round(self.start_rel_sec, 3),
            "end_rel_sec": round(self.end_rel_sec, 3),
            "start_abs_sec": None if self.start_abs_sec is None else round(self.start_abs_sec, 3),
            "end_abs_sec": None if self.end_abs_sec is None else round(self.end_abs_sec, 3),
            "description": self.description,
            "confidence": self.confidence,
            "confidence_is_calibrated": False,
            "evidence": list(self.evidence),
            "limitations": list(self.limitations),
        }


@dataclass
class ValidatedVideoObservation:
    """A response that passed validation, ready to be stored."""

    segment_id: str
    clip_id: str
    summary: str
    events: list[TemporalEvent]
    observed_temporal_quality: str
    audio_analyzed: bool
    limitations: list[str]
    dropped: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "segment_id": self.segment_id,
            "clip_id": self.clip_id,
            "summary": self.summary,
            "events": [e.to_dict() for e in self.events],
            "observed_temporal_quality": self.observed_temporal_quality,
            "audio_analyzed": self.audio_analyzed,
            "limitations": list(self.limitations),
            "dropped": list(self.dropped),
            "schema_version": VIDEO_SCHEMA_VERSION,
        }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _clean_str(value: Any, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return " ".join(value.split())[:limit]


def _clean_list(value: Any, *, limit: int, count: int) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    out = [text for text in (_clean_str(v, limit) for v in value) if text]
    return out[:count]


def _confidence(value: Any) -> float:
    """Clamp to [0, 1]; anything unparseable becomes 0.0 rather than a guess."""
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(number) or math.isinf(number):
        return 0.0
    return round(min(1.0, max(0.0, number)), 3)


#: Same intent as the image path's filter: a sentence naming a specific person or
#: a specific place is not something a pixel buffer can establish.
_SUSPICIOUS_IDENTITY = (
    "this is ", "identity of", "passport", "license plate", "number plate",
    "is named", "whose name",
)


def extract_json_object(raw: str) -> dict:
    """Pull the first JSON object out of a model reply.

    Models intermittently wrap JSON in prose or fences. Returns ``{}`` when
    nothing parses, so the caller reports a classified failure instead of
    persisting an empty success (C6).
    """
    if not isinstance(raw, str) or not raw.strip():
        return {}
    text = raw.strip()

    if text.startswith("```"):
        lines = text.splitlines()
        body = [ln for ln in lines if not ln.strip().startswith("```")]
        text = "\n".join(body).strip()

    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except ValueError:
        pass

    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for idx in range(start, len(text)):
            ch = text[idx]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(text[start : idx + 1])
                    except ValueError:
                        break
                    if isinstance(parsed, dict):
                        return parsed
                    break
        start = text.find("{", start + 1)
    return {}


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def validate_temporal_events(
    item: Any,
    *,
    clip: ClipArtifact,
    segment_start_sec: float,
    segment_end_sec: float,
    expected_clip_id: str = "",
) -> ValidatedVideoObservation:
    """Validate one model response against the clip it was asked about.

    Individual malformed *events* are dropped and recorded in ``dropped`` so the
    failure is visible and the batch continues; a malformed *envelope* (wrong
    clip, no usable summary) raises, because there is nothing to salvage and
    storing a partial row would misrepresent what the model said.
    """
    if not isinstance(item, dict):
        raise VideoSchemaError(INVALID_OUTPUT, f"response must be an object, got {type(item).__name__}")

    # --- identity binding (C3) -------------------------------------------
    clip_id = _clean_str(item.get("clip_id"), 64)
    segment_id = _clean_str(item.get("segment_id"), 64)
    wanted = expected_clip_id or clip.clip_id

    if clip_id and clip_id != wanted:
        raise VideoSchemaError(
            CLIP_MISMATCH,
            f"response describes clip {clip_id!r} but this call was about {wanted!r}",
            detail={"expected": wanted, "got": clip_id},
        )
    if segment_id and segment_id != clip.segment_id:
        raise VideoSchemaError(
            CLIP_MISMATCH,
            f"response describes segment {segment_id!r} but this call was about "
            f"{clip.segment_id!r}",
            detail={"expected": clip.segment_id, "got": segment_id},
        )

    # --- envelope ---------------------------------------------------------
    summary = _clean_str(item.get("summary"), MAX_SUMMARY_LEN)
    limitations = _clean_list(item.get("limitations"), limit=MAX_DESCRIPTION_LEN,
                              count=MAX_LIMITATIONS)

    quality = _clean_str(item.get("observed_temporal_quality"), 16).lower() or "unknown"
    if quality not in TEMPORAL_QUALITY:
        # An unknown word is not silently promoted to a confident one.
        quality = "unknown"

    # ``audio_analyzed`` is forced to False regardless of what the model claims:
    # derived clips are silent, so any claim to have heard audio is false. This is
    # the V21 rule expressed in code.
    raw_audio = item.get("audio_analyzed")
    audio_analyzed = False
    if raw_audio is True:
        limitations = limitations + ["model claimed audio analysis, but the clip is silent"]

    raw_events = item.get("events")
    if raw_events is None:
        raw_events = []
    if not isinstance(raw_events, list):
        raise VideoSchemaError(
            INVALID_OUTPUT, f"'events' must be a list, got {type(raw_events).__name__}"
        )

    events: list[TemporalEvent] = []
    dropped: list[dict] = []

    for index, raw_event in enumerate(raw_events[:MAX_EVENTS]):
        if not isinstance(raw_event, dict):
            dropped.append({"index": index, "reason": "event is not an object"})
            continue

        event_type = _clean_str(raw_event.get("event_type"), 32).lower()
        if event_type not in EVENT_TYPES:
            dropped.append(
                {"index": index, "reason": f"unknown event_type {event_type!r}"}
            )
            continue

        try:
            window = map_event_window(
                clip=clip,
                segment_start_sec=segment_start_sec,
                segment_end_sec=segment_end_sec,
                start_rel_sec=raw_event.get("start_rel_sec"),
                end_rel_sec=raw_event.get("end_rel_sec"),
            )
        except VideoClipError as exc:
            # The temporal arithmetic is shared with TSB-327 so the boundary rule
            # has exactly one implementation.
            dropped.append({"index": index, "reason": exc.code, "message": exc.message})
            continue

        description = _clean_str(raw_event.get("description"), MAX_DESCRIPTION_LEN)
        if not description:
            dropped.append({"index": index, "reason": "event has no description"})
            continue
        lowered = description.lower()
        if any(marker in lowered for marker in _SUSPICIOUS_IDENTITY):
            dropped.append(
                {"index": index, "reason": "identity/place claim cannot be evidenced"}
            )
            continue

        evidence = [
            kind
            for kind in _clean_list(raw_event.get("evidence"), limit=32, count=6)
            if kind in EVIDENCE_KINDS
        ]

        events.append(
            TemporalEvent(
                event_type=event_type,
                start_rel_sec=window.start_rel_sec,
                end_rel_sec=window.end_rel_sec,
                description=description,
                confidence=_confidence(raw_event.get("confidence")),
                evidence=evidence or ["video_window"],
                limitations=_clean_list(raw_event.get("limitations"),
                                        limit=MAX_DESCRIPTION_LEN, count=4),
                start_abs_sec=window.start_abs_sec,
                end_abs_sec=window.end_abs_sec,
            )
        )

    if raw_events and not events:
        raise VideoSchemaError(
            NO_EVENTS,
            "every event in the response was invalid; refusing to store an empty result",
            detail={"dropped": dropped},
        )

    if not summary and not events:
        raise VideoSchemaError(
            INVALID_OUTPUT,
            "response carried neither a usable summary nor a valid event",
        )

    return ValidatedVideoObservation(
        segment_id=clip.segment_id,
        clip_id=wanted,
        summary=summary,
        events=events,
        observed_temporal_quality=quality,
        audio_analyzed=audio_analyzed,
        limitations=limitations,
        dropped=dropped,
    )


def build_prompt(
    *,
    clip_id: str,
    segment_id: str,
    duration_sec: float,
    currency_note: str = "",
) -> str:
    """The instruction sent with the clip.

    States the honesty constraints in the prompt *and* enforces them in
    validation, since a model can agree with the instructions and still
    overstate -- and the prompt wording is what the A/B harness holds constant.
    """
    lines = [
        "You are shown ONE short silent video clip cut from a single shot of a",
        "travel film. Describe what visibly changes over time, in order.",
        "",
        f"clip_id={clip_id}",
        f"segment_id={segment_id}",
        f"clip_duration_sec={duration_sec:.2f}",
        "",
        "Rules:",
        "1. Report only what you can actually see in this clip.",
        "2. Times are seconds FROM THE START OF THE CLIP, not from any other origin.",
        "3. The clip has NO audio track. Never claim to have heard anything.",
        "4. Do not identify people or name places; you cannot establish that.",
        "5. Use only these event_type values: " + ", ".join(sorted(EVENT_TYPES)) + ".",
        "6. confidence is your own rough estimate in [0,1]; it is recorded as",
        "   uncalibrated and is not treated as an accuracy figure.",
        "7. If you cannot see motion or ordering, say so plainly instead of inventing",
        "   events. An empty events list is an acceptable answer.",
    ]
    if currency_note:
        lines += ["", currency_note]
    lines += [
        "",
        "Respond with ONLY this JSON object (no markdown fences):",
        "{",
        f'  "segment_id": "{segment_id}",',
        f'  "clip_id": "{clip_id}",',
        '  "summary": "what happens across the clip, briefly",',
        '  "events": [{"event_type": "subject_action", "start_rel_sec": 1.2,',
        '              "end_rel_sec": 3.4, "description": "what changes",',
        '              "confidence": 0.6, "evidence": ["video_window"],',
        '              "limitations": []}],',
        '  "observed_temporal_quality": "limited",',
        '  "audio_analyzed": false,',
        '  "limitations": ["clip is silent", "may have been sampled server-side"]',
        "}",
    ]
    return "\n".join(lines)


SYSTEM_PROMPT = (
    "You return strict JSON only, no markdown fences. You describe only what is "
    "visible in the provided clip and never invent identifiers, times or audio."
)
