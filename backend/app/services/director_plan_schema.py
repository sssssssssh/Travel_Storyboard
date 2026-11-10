"""TSB-369 plan contract — the shape a draft plan must have.

Deliberately separate from ``storyboard_schema.StoryboardSnapshot``. This is a
*preview*: it is never written to the storyboard tables, and a future "apply"
step would have to translate it explicitly rather than letting a preview object
masquerade as a saved version.

Every model here forbids unknown keys. A planner (offline rules, mock, or a
future language model) cannot smuggle extra structure past validation.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "1.0"

PlanningMode = Literal["offline_rules", "model"]
GenerationStatus = Literal["complete", "provisional", "insufficient_evidence"]
NarrativeAngle = Literal["chronological", "highlights_first", "atmospheric"]
SectionKind = Literal["opening", "body", "closing", "custom"]
NarrativeRole = Literal["opening", "development", "turning_point", "climax", "closing"]

#: How a claim is grounded. Anything asserted about the footage must be one of
#: the first two; ``editorial_suggestion`` is a craft opinion; ``unknown`` is an
#: explicit admission.
ClaimKind = Literal["observed_signal", "human_supplied", "editorial_suggestion", "unknown"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanningBrief(Strict):
    """User intent. Never a statement about what the footage contains."""

    theme: str = Field(default="", max_length=200)
    target_duration_sec: float = Field(default=120.0, ge=1, le=36000)
    style: NarrativeAngle = "chronological"
    variant_count: int = Field(default=2, ge=1, le=3)


class EvidenceRef(Strict):
    """A pointer to one reading, identified the same way the API identifies it."""

    channel: Literal["static", "multiframe", "video_native"]
    result_id: str
    segment_id: str
    evidence_state: str = ""
    #: Set only for native video events, in SOURCE absolute seconds.
    event_start_sec: float | None = None
    event_end_sec: float | None = None


class Claim(Strict):
    """One assertion, tagged with where it came from.

    Free prose is *not* allowed to be a factual claim about the footage: a claim
    that names what is visible must carry ``evidence_refs``.
    """

    text: str = Field(max_length=300)
    kind: ClaimKind
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)


class ShotDraft(Strict):
    shot_id: str = Field(min_length=1, max_length=64)
    segment_id: str
    asset_id: str
    section_id: str
    source_in_sec: float
    source_out_sec: float
    planned_duration_sec: float
    timeline_start_sec: float
    narrative_role: NarrativeRole
    selection_reason: str = Field(max_length=300)
    claims: list[Claim] = Field(default_factory=list)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    needs_review: list[str] = Field(default_factory=list)
    transition_hint: str = Field(default="", max_length=80)
    pacing_hint: str = Field(default="", max_length=80)
    audio_hint: str = Field(default="", max_length=80)


class SectionDraft(Strict):
    section_id: str = Field(min_length=1, max_length=64)
    kind: SectionKind
    title: str = Field(default="", max_length=80)
    narrative_role: str = Field(default="", max_length=40)


class ReviewItem(Strict):
    segment_id: str
    reason: str = Field(max_length=200)


class VariantDraft(Strict):
    variant_id: str = Field(min_length=1, max_length=64)
    title: str = Field(max_length=120)
    narrative_angle: NarrativeAngle
    logline: str = Field(default="", max_length=300)
    status: GenerationStatus
    sections: list[SectionDraft] = Field(default_factory=list)
    shots: list[ShotDraft] = Field(default_factory=list)
    total_duration_sec: float = 0.0
    target_duration_sec: float = 120.0
    duration_gap_sec: float = 0.0
    review_items: list[ReviewItem] = Field(default_factory=list)
    differences: list[str] = Field(default_factory=list, max_length=5)


class CoverageSummary(Strict):
    library_segments: int = 0
    segments_with_any_valid_reading: int = 0
    no_valid_model_reading: int = 0
    video_native_valid: int = 0


class PlanDraft(Strict):
    schema_version: str = SCHEMA_VERSION
    project_id: str
    planning_mode: PlanningMode
    generation_status: GenerationStatus
    source_input_digest: str
    base_storyboard_version_id: str | None = None
    coverage: CoverageSummary = Field(default_factory=CoverageSummary)
    limitations: list[str] = Field(default_factory=list)
    variants: list[VariantDraft] = Field(default_factory=list)
    withheld: dict[str, int] = Field(default_factory=dict)
    needs_more_analysis: bool = False
    warnings: list[str] = Field(default_factory=list)
    #: What the model request actually contained (counts only). Present so the UI
    #: can prove what was sent without exposing the payload.
    request_summary: dict[str, Any] = Field(default_factory=dict)


class PlanPreviewRequest(Strict):
    """What the browser posts. No planner implementation is selectable by users."""

    expected_input_digest: str | None = None
    theme: str = Field(default="", max_length=200)
    target_duration_sec: float = Field(default=120.0, ge=1, le=36000)
    style: NarrativeAngle = "chronological"
    max_candidates: int = Field(default=40, ge=1, le=200)
    variant_count: int = Field(default=2, ge=1, le=3)
    planning_mode: PlanningMode = "offline_rules"
    #: Explicit per-request grant for a real model call. Absent/false -> the model
    #: path refuses with 403 and touches no network. This is in addition to
    #: TRAVELSB_MODEL_MODE and the endpoint checks, never instead of them.
    upload_acknowledged: bool = False


def as_dict(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")
