"""TSB-368-B — response contracts for the read-only director endpoints.

Field whitelists, not ORM serialization: a path, a token, or a raw model payload
cannot leak through here because those keys simply do not exist in the models.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

Eligibility = Literal["eligible", "needs_review", "excluded"]
ObservationState = Literal[
    "current", "stale", "freshness_unverified", "not_analyzed", "failed"
]


class WindowOut(BaseModel):
    start_sec: float
    end_sec: float


class HumanOut(BaseModel):
    rating: str
    rating_source: str
    story_role: str
    note: str


class TechnicalOut(BaseModel):
    quality_metrics: dict[str, Any] = Field(default_factory=dict)
    duration_sec: float = 0.0
    is_trimmed: bool = False
    content_tags: dict[str, Any] = Field(default_factory=dict)


class ObservationOut(BaseModel):
    """One channel. ``state`` is never collapsed into a shared boolean."""

    state: ObservationState
    result_id: str | None = None
    method: str = ""
    status: str = ""
    source_version: str = ""
    status_detail: dict[str, Any] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)


class ObservationsOut(BaseModel):
    static: ObservationOut
    multiframe: ObservationOut
    video_native: ObservationOut


class EditorialOut(BaseModel):
    priority: Literal["preferred", "noted", "unrated", "excluded"]
    reasons: list[str] = Field(default_factory=list)


class EvidenceRefOut(BaseModel):
    type: str
    result_id: str
    segment_id: str
    method: str
    source_version: str
    state: str


class CandidateOut(BaseModel):
    segment_id: str
    asset_id: str
    asset_display_name: str
    source_window: WindowOut
    detected_window: WindowOut
    duration_sec: float
    eligibility: Eligibility
    exclusion_reasons: list[str] = Field(default_factory=list)
    human: HumanOut
    technical: TechnicalOut
    observations: ObservationsOut
    editorial_hints: EditorialOut
    evidence_refs: list[EvidenceRefOut] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    is_representative: bool = True
    #: TSB-371: what a human reviewer concluded about this shot's reading.
    #: "unreviewed" means nobody has judged it -- never that it was rejected.
    review_state: str = "unreviewed"
    review_reasons: list[str] = Field(default_factory=list)


class CoverageOut(BaseModel):
    library_segments: int
    eligible_segments: int
    needs_review_segments: int
    excluded_segments: int
    static_valid: int
    multiframe_valid: int
    video_native_valid: int
    segments_with_any_valid_reading: int
    no_valid_model_reading: int
    coverage_complete: bool


class CandidatesOut(BaseModel):
    project_id: str
    query_version: str = "1.0"
    total: int
    offset: int
    limit: int
    items: list[CandidateOut]
    coverage: CoverageOut
    limitations: list[str] = Field(default_factory=list)


class BriefIn(BaseModel):
    """Client-supplied planning constraints. Never written, never a fact."""

    theme: str = Field(default="", max_length=200)
    target_duration_sec: float | None = Field(default=None, ge=1, le=36000)


class ContextOut(BaseModel):
    schema_version: str = "1.0"
    project_id: str
    input_digest: str
    brief: dict[str, Any]
    coverage: CoverageOut
    candidate_segment_ids: list[str]
    candidate_summaries: list[dict[str, Any]]
    important_human_labels: list[dict[str, Any]]
    limitations: list[str]
    needs_more_analysis: bool
    uncovered_segment_count: int
