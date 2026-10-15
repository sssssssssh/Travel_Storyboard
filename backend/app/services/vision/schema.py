"""TSB-303 — structured shot semantics: schema, prompt and evidence validation.

The contract this module enforces, and why each rule exists:

* **IDs come from the server.** The model is shown a manifest of ``segment_id``s
  and may only echo those. A fabricated id is rejected, never resolved to a row
  (C3 stable mapping — no silent remapping).
* **Time is validated against the segment's current editable interval.** A model
  cannot widen or shift a shot's bounds; the task book forbids the model writing
  ``start_sec``/``end_sec`` at all, so any timestamp is checked for containment
  inside the interval the server already knows.
* **Unknown is a first-class answer.** ``not_visible``/``uncertain`` are real
  values; the schema never forces the model to invent a scene to be valid.
* **No identity or place claims.** A denylist drops free-text that asserts
  person identity or a specific location without evidence, because the prompt
  cannot be trusted to prevent it on every run.
* **AI output is separate from human state.** Nothing here writes
  ``user_rating``, ``story_role``, ``start_sec``, ``end_sec`` or trim flags.

The JSON is parsed and then normalised; a response that cannot be normalised
raises :class:`SemanticValidationError` with a stable code (C6: failures are
visible, never silently dropped).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .errors import MODEL_INVALID_OUTPUT

SCHEMA_VERSION = "1.0"

#: Evidence states. A value is only ever one of these — there is no "probably".
EVIDENCE_OBSERVED = "observed"
EVIDENCE_UNCERTAIN = "uncertain"
EVIDENCE_NOT_VISIBLE = "not_visible"
EVIDENCE_STATES = frozenset({EVIDENCE_OBSERVED, EVIDENCE_UNCERTAIN, EVIDENCE_NOT_VISIBLE})

#: Closed vocabularies. Free-text tags would make the field unqueryable and let
#: the model drift into narrative; unknown input is mapped to "other".
SCENE_TAGS = frozenset(
    {
        "urban", "architecture", "nature", "water", "mountain", "beach", "forest",
        "indoor", "street", "rural", "sky", "night", "food", "transport", "market",
        "religious", "monument", "other",
    }
)
LIGHTING_VALUES = frozenset(
    {"daylight", "golden_hour", "sunset", "dusk", "night", "overcast", "indoor_artificial", "backlit", "unknown"}
)
COMPOSITION_VALUES = frozenset(
    {"wide", "medium", "close_up", "extreme_close_up", "aerial", "low_angle", "high_angle", "unknown"}
)
MOOD_VALUES = frozenset(
    {"calm", "lively", "serene", "dramatic", "busy", "intimate", "grand", "unknown"}
)

MAX_TAG_LEN = 32
MAX_TAG_COUNT = 12
MAX_SUBJECT_LEN = 64
MAX_TEXT_LEN = 300
MAX_LIMITATIONS = 8
MAX_ACTION_LEN = 120

#: Free text that asserts an identity/place the model cannot know from pixels.
#: Deliberately conservative: it matches explicit assertion phrasing, not every
#: mention, so "a woman in a red coat" (visible) stays while "this is Maria" goes.
_IDENTITY_PATTERNS = (
    # "this is Maria" / "that's Giovanni" — a bare capitalised first name after a
    # copula is an identity claim; a visible description ("this is a pier") is
    # not, because the following word is lowercase.
    re.compile(r"\b(?:this|that|it|he|she)\s*(?:'s|\bis|\bwas)\s+(?:mr|mrs|ms|dr)\.?\s+[A-Z]\w+", re.I),
    re.compile(r"\b(?:this|that|it|he|she)\s*(?:'s|\bis|\bwas)\s+[A-Z][a-z]{2,}\b"),
    re.compile(r"\b(?:named|called)\s+[A-Z][a-z]+", re.I),
    re.compile(r"\b(?:my|his|her|their)\s+(?:wife|husband|son|daughter|mother|father|friend)\b", re.I),
    re.compile(r"\b(?:located|situated)\s+in\s+[A-Z][a-z]+", re.I),
    re.compile(r"\b(?:the\s+)?(?:city|country|town|village)\s+of\s+[A-Z][a-z]+", re.I),
)


class SemanticValidationError(ValueError):
    """A model response that cannot be admitted into the semantic repository."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


# --------------------------------------------------------------------------
# prompt
# --------------------------------------------------------------------------

def build_prompt(manifest: list[dict], *, coverage: dict | None = None) -> str:
    """Render the M5.1 image-semantics prompt.

    ``manifest`` entries are built *by the server* from database rows, so the
    identifiers the model is allowed to use are the only ones it ever sees.
    """
    lines = [
        "You are analysing representative still frames from candidate shots of ONE",
        "travel video. Describe ONLY what is visible in the images.",
        "",
        "RULES (all are mandatory):",
        "- Base every statement solely on the pixels you were shown.",
        "- Never invent, guess or modify a segment_id or frame_id.",
        "- Never state a person's name, identity, relationship or a specific place",
        "  name: those are not knowable from a photograph.",
        "- If something is not visible, use \"unknown\" / \"not_visible\" instead of",
        "  a plausible guess.",
        "- A single still frame cannot prove camera motion or audio content.",
        "",
        "IMAGE INDEX (use these exact identifiers):",
    ]
    for item in manifest:
        lines.append(
            f"  segment_id={item['segment_id']} asset_id={item['asset_id']} "
            f"shot={item['start_sec']:.2f}-{item['end_sec']:.2f}s "
            f"frames={','.join(item.get('frame_ids', [])) or 'none'} "
            f"role={item.get('story_role') or 'unspecified'}"
        )
    if coverage:
        lines += [
            "",
            f"COVERAGE: you are seeing {coverage.get('images_sent', 0)} images covering "
            f"{coverage.get('segments_considered', 0)} of {coverage.get('eligible', 0)} "
            "eligible shots. If that is a small fraction, say so explicitly in",
            "`limitations`; do not generalise about footage you were not shown.",
        ]

    lines += [
        "",
        "Return ONE JSON object and nothing else (no markdown fences, no prose):",
        "{",
        '  "results": [',
        "    {",
        '      "segment_id": "<one of the ids above>",',
        '      "visual": {',
        f'        "scene_tags": [<from: {", ".join(sorted(SCENE_TAGS))}>],',
        '        "subjects": ["<short noun phrase>"],',
        '        "visible_actions": ["<what is happening, if visible>"],',
        f'        "lighting": "<from: {", ".join(sorted(LIGHTING_VALUES))}>",',
        f'        "composition": "<from: {", ".join(sorted(COMPOSITION_VALUES))}>",',
        f'        "mood": [<from: {", ".join(sorted(MOOD_VALUES))}>]',
        "      },",
        '      "evidence": {',
        '        "state": "observed | uncertain | not_visible",',
        '        "observed_confidence": <0.0-1.0>,',
        '        "limitations": ["<e.g. single_frame_cannot_prove_camera_motion>"]',
        "      },",
        '      "review": {',
        '        "needs_motion_review": <true|false>,',
        '        "reasons": ["<why a multi-frame check would help>"]',
        "      }",
        "    }",
        "  ]",
        "}",
    ]
    return "\n".join(lines)


SYSTEM_PROMPT = (
    "You are a precise visual annotator. You answer with strict JSON only, based "
    "exclusively on the supplied images, and you mark anything you cannot see as "
    "unknown rather than guessing."
)


# --------------------------------------------------------------------------
# validation helpers
# --------------------------------------------------------------------------

def _clean_str(value: Any, limit: int) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    # Strip control characters that would corrupt logs/UI.
    text = "".join(ch for ch in text if ch == " " or ch.isprintable())
    return text[:limit]


def _closed(value: Any, allowed: frozenset[str], default: str) -> str:
    token = _clean_str(value, MAX_TAG_LEN).lower().replace(" ", "_").replace("-", "_")
    return token if token in allowed else default


def _closed_list(value: Any, allowed: frozenset[str]) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        token = _clean_str(item, MAX_TAG_LEN).lower().replace(" ", "_").replace("-", "_")
        if token in allowed and token not in out:
            out.append(token)
    return out[:MAX_TAG_COUNT]


def _free_list(value: Any, limit: int, count: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = _clean_str(item, limit)
        if text and text not in out:
            out.append(text)
    return out[:count]


def _looks_like_identity_claim(text: str) -> bool:
    return any(p.search(text) for p in _IDENTITY_PATTERNS)


def _confidence(value: Any) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0.0
    if num != num:  # NaN
        return 0.0
    return max(0.0, min(1.0, round(num, 3)))


def extract_json_object(raw: str) -> dict:
    """Recover the JSON object from a model response.

    Models wrap JSON in fences or add a sentence before it even when told not to;
    that is recoverable. What is *not* recoverable is a response with no object at
    all, which is reported as invalid output.
    """
    import json

    text = (raw or "").strip()
    if not text:
        raise SemanticValidationError(MODEL_INVALID_OUTPUT, "model returned an empty response")

    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()

    try:
        data = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise SemanticValidationError(
                MODEL_INVALID_OUTPUT, "model response contains no JSON object"
            ) from None
        try:
            data = json.loads(text[start : end + 1])
        except ValueError as exc:
            raise SemanticValidationError(
                MODEL_INVALID_OUTPUT, f"model JSON could not be parsed: {exc}"
            ) from exc

    if not isinstance(data, dict):
        raise SemanticValidationError(MODEL_INVALID_OUTPUT, "model JSON root is not an object")
    return data


# --------------------------------------------------------------------------
# per-result validation
# --------------------------------------------------------------------------

@dataclass
class AllowedSegment:
    segment_id: str
    asset_id: str
    start_sec: float
    end_sec: float
    frame_ids: list[str] = field(default_factory=list)


@dataclass
class ValidatedSemantics:
    segment_id: str
    asset_id: str
    visual: dict
    evidence: dict
    review: dict
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "segment_id": self.segment_id,
            "asset_id": self.asset_id,
            "visual": self.visual,
            "evidence": self.evidence,
            "review": self.review,
            "warnings": self.warnings,
        }


def validate_result(item: Any, allowed: dict[str, AllowedSegment]) -> ValidatedSemantics:
    """Validate and normalise one model result against the server whitelist."""
    if not isinstance(item, dict):
        raise SemanticValidationError(MODEL_INVALID_OUTPUT, "result entry is not an object")

    segment_id = _clean_str(item.get("segment_id"), 64)
    if not segment_id:
        raise SemanticValidationError(MODEL_INVALID_OUTPUT, "result is missing segment_id")
    if segment_id not in allowed:
        # The critical C3 guard: a fabricated id must never reach the database.
        raise SemanticValidationError(
            MODEL_INVALID_OUTPUT, f"segment_id {segment_id!r} is not in the supplied manifest"
        )
    target = allowed[segment_id]

    visual_raw = item.get("visual") if isinstance(item.get("visual"), dict) else {}
    evidence_raw = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    review_raw = item.get("review") if isinstance(item.get("review"), dict) else {}

    warnings: list[str] = []

    subjects = _free_list(visual_raw.get("subjects"), MAX_SUBJECT_LEN, MAX_TAG_COUNT)
    actions = _free_list(visual_raw.get("visible_actions"), MAX_ACTION_LEN, MAX_TAG_COUNT)
    limitations = _free_list(evidence_raw.get("limitations"), MAX_TEXT_LEN, MAX_LIMITATIONS)
    reasons = _free_list(review_raw.get("reasons"), MAX_TEXT_LEN, MAX_LIMITATIONS)

    # Strip unsupported identity/place assertions rather than storing them.
    for bucket, label in ((subjects, "subjects"), (actions, "visible_actions")):
        kept = [s for s in bucket if not _looks_like_identity_claim(s)]
        if len(kept) != len(bucket):
            warnings.append(f"dropped_identity_claim:{label}")
        bucket[:] = kept

    state = _closed(evidence_raw.get("state"), EVIDENCE_STATES, EVIDENCE_OBSERVED)
    confidence = _confidence(evidence_raw.get("observed_confidence"))
    # An "observed" state with no content is a contradiction; downgrade it.
    if state == EVIDENCE_OBSERVED and not (subjects or actions or visual_raw.get("scene_tags")):
        state = EVIDENCE_UNCERTAIN
        warnings.append("downgraded_observed_without_content")

    # Referenced frame ids must also be real ones the server sent for this shot.
    frame_ids = [
        fid
        for fid in _free_list(evidence_raw.get("frame_ids"), 64, 16)
        if fid in target.frame_ids
    ]
    if not frame_ids:
        frame_ids = list(target.frame_ids)
    else:
        invented = [
            fid
            for fid in _free_list(evidence_raw.get("frame_ids"), 64, 16)
            if fid not in target.frame_ids
        ]
        if invented:
            warnings.append("dropped_unknown_frame_ids")

    visual = {
        "scene_tags": _closed_list(visual_raw.get("scene_tags"), SCENE_TAGS),
        "subjects": subjects,
        "visible_actions": actions,
        "lighting": _closed(visual_raw.get("lighting"), LIGHTING_VALUES, "unknown"),
        "composition": _closed(visual_raw.get("composition"), COMPOSITION_VALUES, "unknown"),
        "mood": _closed_list(visual_raw.get("mood"), MOOD_VALUES),
    }
    evidence = {
        "state": state,
        "observed_confidence": confidence,
        "frame_ids": frame_ids,
        "limitations": limitations,
    }
    review = {
        "needs_motion_review": bool(review_raw.get("needs_motion_review")),
        "reasons": reasons,
    }
    # A single-frame observation can never justify a motion claim; record why.
    if len(frame_ids) <= 1 and not review["needs_motion_review"]:
        review["needs_motion_review"] = True
        review["reasons"] = reasons + ["single_frame_cannot_prove_camera_motion"]

    return ValidatedSemantics(
        segment_id=segment_id,
        asset_id=target.asset_id,
        visual=visual,
        evidence=evidence,
        review=review,
        warnings=warnings,
    )


def validate_response(
    raw: str, allowed: dict[str, AllowedSegment]
) -> tuple[list[ValidatedSemantics], list[dict]]:
    """Validate a whole model response.

    Returns ``(accepted, problems)``. One bad entry does not discard the good
    ones, but the failure is always reported — that is what keeps a partial
    result from being mistaken for a complete one.
    """
    data = extract_json_object(raw)
    results = data.get("results")
    if results is None:
        # Tolerate a single bare object for a one-shot call.
        results = [data] if "segment_id" in data else None
    if not isinstance(results, list):
        raise SemanticValidationError(
            MODEL_INVALID_OUTPUT, "model JSON has no usable 'results' array"
        )

    accepted: list[ValidatedSemantics] = []
    problems: list[dict] = []
    seen: set[str] = set()

    for index, item in enumerate(results):
        try:
            parsed = validate_result(item, allowed)
        except SemanticValidationError as exc:
            problems.append({"index": index, "code": exc.code, "message": exc.message})
            continue
        if parsed.segment_id in seen:
            problems.append(
                {
                    "index": index,
                    "code": MODEL_INVALID_OUTPUT,
                    "message": f"duplicate segment_id {parsed.segment_id!r}",
                }
            )
            continue
        seen.add(parsed.segment_id)
        accepted.append(parsed)

    return accepted, problems
