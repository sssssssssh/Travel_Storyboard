"""TSB-366 — the validated shape of a storyboard version snapshot.

A version stores ``brief`` / ``sections`` / ``shots`` / ``constraints`` as JSON.
Keeping them as one versioned document (instead of seven relational tables) is
deliberate: the task book asks for "章节和镜头首期可以放在 JSON 快照中，避免过早
拆成多张关系表", and a snapshot is the natural unit of immutability.

Three rules make the JSON safe to trust:

1. **``schema_version`` is mandatory.** A snapshot with no version cannot be
   migrated later, so it is refused on the way *in* rather than discovered later.
2. **Unknown fields are refused.** ``extra="forbid"`` means a typo or a future
   field cannot silently land in a stored version and then be read back as if it
   had always been part of the contract.
3. **Seconds are never rounded.** Times stay ``float`` end to end; rounding here
   would break the "不要提前取整" rule and make the migration reconciliation
   (§3.3) compare unequal values.

Identity: every shot carries a stable ``shot_id``. Array position is *ordering*,
never identity — the task book is explicit that an index must not be an object's
long-term identity.
"""

from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Bumped whenever the on-disk snapshot shape changes in a way that needs a
#: migration to read. Stored per version so old snapshots stay readable.
SNAPSHOT_SCHEMA_VERSION = "1.0"

#: Where a version came from. Kept as a closed set so a reader can branch on it
#: without parsing prose.
ACTORS = frozenset({"migration", "user", "system", "model"})
REASONS = frozenset(
    {"legacy_import", "manual_edit", "restore", "initial", "director_plan"}
)


class SnapshotError(ValueError):
    """A snapshot did not validate. Carries the field-level reasons."""

    def __init__(self, message: str, issues: list[str] | None = None) -> None:
        super().__init__(message)
        self.issues = list(issues or [])


def _check_finite(value: float | None, fieldname: str) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{fieldname} must be a finite number")
    return float(value)


class SectionSnapshot(BaseModel):
    """One narrative section. ``legacy_import`` marks a plan we did not design."""

    model_config = ConfigDict(extra="forbid")

    section_id: str
    title: str = ""
    #: ``legacy_import`` is essential honesty: a migrated plan had no chapters,
    #: so the section says so instead of pretending an AI designed it.
    kind: Literal["legacy_import", "opening", "body", "closing", "custom"] = "custom"
    purpose: str = ""
    #: Anything the source had that we cannot interpret, kept verbatim.
    notes: str = ""

    @field_validator("section_id")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value:
            raise ValueError("section_id must not be empty")
        return value


class ShotSnapshot(BaseModel):
    """One shot inside a version. Mirrors the legacy columns field for field.

    Fields the legacy plan did **not** have are optional and default to ``None``
    / empty — never to an invented value (§3.1-7: 不编造选择理由、章节叙事、
    语义证据、置信度或人工确认状态).
    """

    model_config = ConfigDict(extra="forbid")

    #: Stable identity across versions. Never the array index.
    shot_id: str
    section_id: str | None = None

    asset_id: str
    segment_id: str
    source_in_sec: float
    source_out_sec: float
    target_duration_sec: float = 0.0
    timeline_start_sec: float = 0.0

    story_role: str = ""
    transition_hint: str = ""
    music_cue: str = ""
    audio_hint: str = ""
    color_hint: str = ""
    evidence: str = ""
    confidence: float = 0.0
    review_status: str = "pending"

    #: Added by later tasks; absent (``None``) in a migrated legacy plan so a
    #: reader can tell "not stated" from "stated as empty".
    reason: str | None = None
    locked: bool | None = None
    source_signature: str | None = None

    @field_validator("shot_id")
    @classmethod
    def _shot_id_present(cls, value: str) -> str:
        if not value:
            raise ValueError(
                "shot_id is required: array position must not be an object's identity"
            )
        return value

    @field_validator("source_in_sec", "source_out_sec", "target_duration_sec",
                     "timeline_start_sec", "confidence")
    @classmethod
    def _finite(cls, value: float, info) -> float:
        checked = _check_finite(value, info.field_name)
        assert checked is not None
        return checked

    @field_validator("confidence")
    @classmethod
    def _unit_range(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("confidence must be between 0 and 1")
        return value

    @model_validator(mode="after")
    def _ordered_range(self) -> "ShotSnapshot":
        """A shot whose out-point precedes its in-point is structurally invalid.

        This is checked here rather than only against the source, because an
        inverted range is wrong no matter which asset it points at — and an
        invalid snapshot must be refused before anything is written.
        """
        if self.source_out_sec <= self.source_in_sec:
            raise ValueError("source_out_sec must be greater than source_in_sec")
        if self.source_in_sec < 0:
            raise ValueError("source_in_sec must be >= 0")
        return self


class BriefSnapshot(BaseModel):
    """The plan brief. Empty for a legacy import — nothing is invented."""

    model_config = ConfigDict(extra="forbid")

    theme: str = ""
    target_duration_sec: float | None = None
    notes: str = ""

    @field_validator("target_duration_sec")
    @classmethod
    def _finite(cls, value: float | None) -> float | None:
        return _check_finite(value, "target_duration_sec")


class ConstraintsSnapshot(BaseModel):
    """Hard constraints. Empty for a legacy import."""

    model_config = ConfigDict(extra="forbid")

    must_include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    max_total_duration_sec: float | None = None


class ValidationIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shot_id: str = ""
    code: str
    message: str


class ValidationSummary(BaseModel):
    """What the server concluded about this snapshot's references.

    ``state`` is the one value a caller branches on:
    ``ok`` / ``needs_review`` / ``invalid``.
    """

    model_config = ConfigDict(extra="forbid")

    state: Literal["ok", "needs_review", "invalid"] = "ok"
    issues: list[ValidationIssue] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.state == "ok"


@dataclass
class StoryboardSnapshot:
    """The whole version document, validated on the way in and out."""

    schema_version: str = SNAPSHOT_SCHEMA_VERSION
    brief: BriefSnapshot = field(default_factory=BriefSnapshot)
    sections: list[SectionSnapshot] = field(default_factory=list)
    shots: list[ShotSnapshot] = field(default_factory=list)
    constraints: ConstraintsSnapshot = field(default_factory=ConstraintsSnapshot)
    validation_summary: ValidationSummary = field(default_factory=ValidationSummary)

    # -- construction ------------------------------------------------------
    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "StoryboardSnapshot":
        """Validate a stored or submitted snapshot.

        Raises :class:`SnapshotError` with field-level issues rather than
        letting a partially-valid document through.
        """
        if not isinstance(raw, dict):
            raise SnapshotError("snapshot must be an object")

        schema_version = raw.get("schema_version")
        if not schema_version:
            raise SnapshotError(
                "snapshot.schema_version is required: an unversioned snapshot "
                "cannot be migrated later"
            )

        issues: list[str] = []
        brief = _model_or_issue(BriefSnapshot, raw.get("brief") or {}, "brief", issues)
        constraints = _model_or_issue(
            ConstraintsSnapshot, raw.get("constraints") or {}, "constraints", issues
        )
        validation = _model_or_issue(
            ValidationSummary, raw.get("validation_summary") or {},
            "validation_summary", issues,
        )

        sections: list[SectionSnapshot] = []
        for index, item in enumerate(raw.get("sections") or []):
            parsed = _model_or_issue(
                SectionSnapshot, item, f"sections[{index}]", issues
            )
            if parsed is not None:
                sections.append(parsed)

        shots: list[ShotSnapshot] = []
        seen: set[str] = set()
        for index, item in enumerate(raw.get("shots") or []):
            parsed = _model_or_issue(ShotSnapshot, item, f"shots[{index}]", issues)
            if parsed is None:
                continue
            if parsed.shot_id in seen:
                issues.append(f"shots[{index}].shot_id duplicated: {parsed.shot_id}")
                continue
            seen.add(parsed.shot_id)
            shots.append(parsed)

        if issues:
            raise SnapshotError("snapshot failed validation: " + "; ".join(issues), issues)

        return cls(
            schema_version=str(schema_version),
            brief=brief or BriefSnapshot(),
            sections=sections,
            shots=shots,
            constraints=constraints or ConstraintsSnapshot(),
            validation_summary=validation or ValidationSummary(),
        )

    def to_dict(self) -> dict[str, Any]:
        """The exact document stored in ``StoryboardVersion``."""
        return {
            "schema_version": self.schema_version,
            "brief": self.brief.model_dump(),
            "sections": [s.model_dump() for s in self.sections],
            "shots": [s.model_dump() for s in self.shots],
            "constraints": self.constraints.model_dump(),
            "validation_summary": self.validation_summary.model_dump(),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    # -- helpers -----------------------------------------------------------
    @property
    def shot_count(self) -> int:
        return len(self.shots)

    def shot_ids(self) -> list[str]:
        return [s.shot_id for s in self.shots]


def new_shot_id() -> str:
    return str(uuid.uuid4())


def new_section_id() -> str:
    return str(uuid.uuid4())


def _model_or_issue(model: type[BaseModel], raw, label: str, issues: list[str]):
    """Validate one sub-document, appending readable issues on failure."""
    if not isinstance(raw, dict):
        issues.append(f"{label} must be an object")
        return None
    try:
        return model.model_validate(raw)
    except Exception as exc:  # pydantic.ValidationError and friends
        messages = []
        errors = getattr(exc, "errors", None)
        if callable(errors):
            for err in exc.errors():
                loc = ".".join(str(p) for p in err.get("loc", ()))
                messages.append(f"{loc}: {err.get('msg', '')}")
        issues.append(f"{label}: {'; '.join(messages) or str(exc)}")
        return None
