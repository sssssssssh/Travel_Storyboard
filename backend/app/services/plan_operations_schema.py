"""TSB-367 — the structured operation protocol (§4).

Every modification to a storyboard must be expressible as a `PlanOperation`, and
this module is the schema layer that decides what the server is even willing to
*look at*. It is deliberately pedantic:

* **unknown schema_version / operation type / field** → refused. A permissive
  parser would let a client smuggle anything through every later check.
* **server-side fields are not client-writable.** ``status``, ``diff`` and
  ``result_version_id`` are produced by the server; a client that sends them is
  rejected rather than silently ignored, because silent ignoring hides a broken
  client.
* **numbers must be real.** NaN, Infinity, negative durations and string-typed
  numbers are refused here, before any arithmetic can poison a snapshot.
* **bounded batches.** Operation count, reason length and id‑length all have
  ceilings so a single request cannot exhaust memory or the audit log.

What this module does *not* do: it never touches the database, never resolves an
asset, and never decides whether a shot exists. Those are the validator's job
(:mod:`app.services.plan_validation`) — separation keeps the pure executor
testable without a database.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Protocol version this build speaks. A request carrying any other value is
#: refused, so a future protocol cannot be half-understood.
OPERATION_SCHEMA_VERSION = 1

#: Batch and payload ceilings (§4.1).
MAX_OPERATIONS_PER_REQUEST = 50
MAX_REASON_LENGTH = 2000
MAX_ID_LENGTH = 128
MAX_LABEL_LENGTH = 200

#: A finite float in JSON. ``allow_inf_nan=False`` is what makes NaN/Infinity a
#: schema error rather than something that reaches the snapshot.
FiniteFloat = Annotated[float, Field(allow_inf_nan=True)]


class OperationSchemaError(ValueError):
    """The envelope or one of its operations is not acceptable.

    ``code`` carries a stable machine-readable reason so the API layer can map it
    onto ``INVALID_OPERATION_SCHEMA`` / ``UNKNOWN_OPERATION`` without parsing prose.
    """

    def __init__(self, message: str, code: str = "INVALID_OPERATION_SCHEMA",
                 issues: list[str] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.issues = list(issues or [])


#: Operation types implemented in this round (§4.2 A–H).
OPERATION_TYPES = (
    "move_shot",
    "move_section",
    "insert_shot",
    "remove_shot",
    "replace_shot",
    "trim_shot",
    "update_constraints",
    "lock_shot",
)


def _require_finite(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def _bounded_id(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    if len(value) > MAX_ID_LENGTH:
        raise ValueError(f"{label} is too long")
    return value


class _OpBase(BaseModel):
    """Shared strictness for every operation type."""

    #: Unknown fields are refused, not ignored (§4.1).
    model_config = ConfigDict(extra="forbid")


class MoveShot(_OpBase):
    """A. Move a shot to a new position (§4.2 A)."""

    type: Literal["move_shot"]
    shot_id: str
    #: Exactly one anchor is required; the schema refuses "both" and "neither"
    #: because either case has two equally plausible interpretations.
    before_shot_id: str | None = None
    after_shot_id: str | None = None
    #: Optional target section. When omitted the shot stays in its section.
    section_id: str | None = None

    @model_validator(mode="after")
    def _exactly_one_anchor(self) -> "MoveShot":
        anchors = [a for a in (self.before_shot_id, self.after_shot_id) if a]
        if len(anchors) != 1:
            raise ValueError(
                "move_shot requires exactly one of before_shot_id / after_shot_id"
            )
        return self


class MoveSection(_OpBase):
    """B. Move a section (§4.2 B)."""

    type: Literal["move_section"]
    section_id: str
    before_section_id: str | None = None
    after_section_id: str | None = None

    @model_validator(mode="after")
    def _exactly_one_anchor(self) -> "MoveSection":
        anchors = [a for a in (self.before_section_id, self.after_section_id) if a]
        if len(anchors) != 1:
            raise ValueError(
                "move_section requires exactly one of before_section_id / "
                "after_section_id"
            )
        return self


class _NewShotSource(_OpBase):
    """The media half of an insert/replace: where the picture comes from."""

    asset_id: str
    segment_id: str
    source_in_sec: float
    source_out_sec: float
    target_duration_sec: float = 0.0

    @field_validator("source_in_sec", "source_out_sec", "target_duration_sec",
                     mode="before")
    @classmethod
    def _finite(cls, value, info):
        return _require_finite(value, info.field_name)

    @model_validator(mode="after")
    def _ordered_range(self):
        if self.source_in_sec < 0:
            raise ValueError("source_in_sec must be >= 0")
        if self.source_out_sec <= self.source_in_sec:
            raise ValueError("source_out_sec must be greater than source_in_sec")
        if self.target_duration_sec < 0:
            raise ValueError("target_duration_sec must not be negative")
        return self


class InsertShot(_NewShotSource):
    """C. Insert a new shot (§4.2 C).

    Identity: a new shot gets a *server-minted* ``shot_id``. A client cannot
    choose it, which is what keeps "the array index is not an identity" true.
    """

    type: Literal["insert_shot"]
    section_id: str | None = None
    before_shot_id: str | None = None
    after_shot_id: str | None = None
    story_role: str = ""
    reason: str = ""
    evidence_refs: list[str] = Field(default_factory=list)


class RemoveShot(_OpBase):
    """D. Remove a shot from the plan — never from the library (§4.2 D)."""

    type: Literal["remove_shot"]
    shot_id: str
    #: Removing a locked shot requires an explicit unlock step first (§5.3).
    confirm_locked_removal: bool = False


class ReplaceShot(_NewShotSource):
    """E. Replace a shot's source (§4.2 E).

    The replacement is a **new identity**: passing the old ``shot_id`` as a new
    ``new_shot_id`` is refused, so the client cannot dress new media up as the
    old shot.
    """

    type: Literal["replace_shot"]
    target_shot_id: str
    story_role: str = ""
    reason: str = ""
    evidence_refs: list[str] = Field(default_factory=list)


class TrimShot(_OpBase):
    """F. Change a shot's in/out points (§4.2 F)."""

    type: Literal["trim_shot"]
    shot_id: str
    source_in_sec: float
    source_out_sec: float
    #: Optional new planned duration; omitted means "use the new span".
    target_duration_sec: float | None = None

    @field_validator("source_in_sec", "source_out_sec", "target_duration_sec",
                     mode="before")
    @classmethod
    def _finite(cls, value, info):
        if value is None:
            return None
        return _require_finite(value, info.field_name)

    @model_validator(mode="after")
    def _ordered(self) -> "TrimShot":
        if self.source_in_sec < 0:
            raise ValueError("source_in_sec must be >= 0")
        if self.source_out_sec <= self.source_in_sec:
            raise ValueError("source_out_sec must be greater than source_in_sec")
        if self.target_duration_sec is not None and self.target_duration_sec < 0:
            raise ValueError("target_duration_sec must not be negative")
        return self


class LockShot(_OpBase):
    """H. Lock or unlock dimensions of a shot (§4.2 H)."""

    type: Literal["lock_shot"]
    shot_id: str
    locked: bool
    dimensions: list[str] = Field(default_factory=lambda: ["all"])

    @field_validator("dimensions")
    @classmethod
    def _known_dimensions(cls, value: list[str]) -> list[str]:
        from .storyboard_schema import LOCK_DIMENSIONS

        dims = [str(v).strip().lower() for v in value]
        if not dims:
            raise ValueError("dimensions must not be empty")
        unknown = [d for d in dims if d not in LOCK_DIMENSIONS]
        if unknown:
            raise ValueError(f"unknown lock dimension(s): {unknown}")
        return sorted(set(dims))


class HardConstraints(_OpBase):
    """Constraints that must hold; a conflict is reported, never silently dropped."""

    must_include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    max_total_duration_sec: float | None = None
    min_total_duration_sec: float | None = None

    @field_validator("max_total_duration_sec", "min_total_duration_sec", mode="before")
    @classmethod
    def _finite(cls, value, info):
        if value is None:
            return None
        return _require_finite(value, info.field_name)


class SoftPreferences(_OpBase):
    """Preferences that guide choices but never block a save (§4.2 G)."""

    target_duration_sec: float | None = None
    pacing: str | None = None
    people_ratio: float | None = None
    scenery_ratio: float | None = None
    narrative_focus: str | None = None

    @field_validator("target_duration_sec", "people_ratio", "scenery_ratio",
                     mode="before")
    @classmethod
    def _finite(cls, value, info):
        if value is None:
            return None
        return _require_finite(value, info.field_name)


class UpdateConstraints(_OpBase):
    """G. Change the plan's constraints (§4.2 G).

    Never adds or removes shots: a constraint change that the current plan
    violates comes back as validation issues, not as an automatic edit.
    """

    type: Literal["update_constraints"]
    hard: HardConstraints = Field(default_factory=HardConstraints)
    soft: SoftPreferences = Field(default_factory=SoftPreferences)


#: Discriminated union: the ``type`` literal chooses the concrete model, so an
#: unknown type raises instead of falling back to something permissive.
Operation = Annotated[
    Union[MoveShot, MoveSection, InsertShot, RemoveShot, ReplaceShot, TrimShot,
          UpdateConstraints, LockShot],
    Field(discriminator="type"),
]

_BY_TYPE = {
    "move_shot": MoveShot,
    "move_section": MoveSection,
    "insert_shot": InsertShot,
    "remove_shot": RemoveShot,
    "replace_shot": ReplaceShot,
    "trim_shot": TrimShot,
    "update_constraints": UpdateConstraints,
    "lock_shot": LockShot,
}


class OperationEnvelope(BaseModel):
    """The request envelope (§4.1).

    ``board_id`` is optional in the body because the route already carries it; it
    is only used to catch a client that contradicts the URL.
    """

    model_config = ConfigDict(extra="forbid")

    #: Required, not defaulted: a body that omits the protocol version is a
    #: client this build cannot vouch for, and silently assuming v1 would make a
    #: future v2 client look valid.
    schema_version: int
    board_id: str | None = None
    base_version_id: str
    idempotency_key: str
    operations: list[Operation] = Field(min_length=1, max_length=MAX_OPERATIONS_PER_REQUEST)
    reason: str = ""

    @field_validator("schema_version")
    @classmethod
    def _known_version(cls, value: int) -> int:
        if value != OPERATION_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version {value}; this build speaks "
                f"{OPERATION_SCHEMA_VERSION}"
            )
        return value

    @field_validator("base_version_id", "idempotency_key", "board_id")
    @classmethod
    def _non_empty(cls, value, info):
        return _bounded_id(value, info.field_name)

    @field_validator("reason")
    @classmethod
    def _reason_length(cls, value: str) -> str:
        if len(value) > MAX_REASON_LENGTH:
            raise ValueError(f"reason exceeds {MAX_REASON_LENGTH} characters")
        return value

    # -- construction ------------------------------------------------------
    @classmethod
    def from_dict(cls, raw: Any) -> "OperationEnvelope":
        """Validate a raw request body, raising :class:`OperationSchemaError`.

        The error carries a stable ``code`` (``UNKNOWN_OPERATION`` for an unknown
        type) so the API can answer ``400`` with a machine-readable reason instead
        of leaking a pydantic traceback.
        """
        if not isinstance(raw, dict):
            raise OperationSchemaError("request body must be a JSON object")

        ops = raw.get("operations")
        if isinstance(ops, list):
            for index, op in enumerate(ops):
                if not isinstance(op, dict):
                    raise OperationSchemaError(
                        f"operations[{index}] must be an object"
                    )
                op_type = op.get("type")
                if not op_type:
                    raise OperationSchemaError(
                        f"operations[{index}] is missing \"type\""
                    )
                if op_type not in _BY_TYPE:
                    raise OperationSchemaError(
                        f"unknown operation type {op_type!r}; supported: "
                        f"{', '.join(OPERATION_TYPES)}",
                        code="UNKNOWN_OPERATION",
                    )

        try:
            return cls.model_validate(raw)
        except Exception as exc:  # pydantic.ValidationError and friends
            issues: list[str] = []
            errors = getattr(exc, "errors", None)
            if callable(errors):
                for err in exc.errors():
                    loc = ".".join(str(p) for p in err.get("loc", ()))
                    issues.append(f"{loc}: {err.get('msg', '')}")
            raise OperationSchemaError(
                "operation envelope failed validation: " + "; ".join(issues) if issues
                else f"operation envelope failed validation: {exc}",
                issues=issues,
            ) from None

    # -- helpers -----------------------------------------------------------
    def payload_digest(self) -> str:
        """A stable digest of the *content* (not the key) of the request.

        Used to distinguish "the same request replayed" (fine, idempotent) from
        "the same key with a different body" (``IDEMPOTENCY_KEY_REUSED``).
        """
        material = json.dumps(
            {
                "schema_version": self.schema_version,
                "board_id": self.board_id,
                "base_version_id": self.base_version_id,
                "operations": [o.model_dump() for o in self.operations],
                "reason": self.reason,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def to_operation_dicts(self) -> list[dict]:
        """The operations as plain dicts, for storage and for the executor."""
        return [o.model_dump() for o in self.operations]
