"""TSB-367 — the operation envelope and the eight operation types (§4).

Tests first. Nothing here touches a database: the schema layer is the trust
boundary for everything the server is willing to *consider*, so it is tested in
isolation.

The recurring theme is **refusal**: unknown operations, unknown fields, unknown
protocol versions, forged server-side fields, NaN/Infinity, negative durations,
oversized batches. A permissive parser here would let a client (or a future
model) smuggle something past every later check.
"""

from __future__ import annotations

import json

import pytest

from app.services.plan_operations_schema import (
    MAX_OPERATIONS_PER_REQUEST,
    MAX_REASON_LENGTH,
    OperationEnvelope,
    OperationSchemaError,
)


def _envelope(**overrides) -> dict:
    base = {
        "schema_version": 1,
        "board_id": "b" * 36,
        "base_version_id": "v" * 36,
        "idempotency_key": "key-1",
        "operations": [{
            "type": "move_shot",
            "shot_id": "shot-1",
            "after_shot_id": "shot-2",
        }],
        "reason": "reorder",
    }
    base.update(overrides)
    return base


def _parse(raw: dict) -> OperationEnvelope:
    return OperationEnvelope.from_dict(raw)


# --- envelope --------------------------------------------------------------


def test_a_well_formed_envelope_parses():
    env = _parse(_envelope())
    assert env.schema_version == 1
    assert len(env.operations) == 1
    assert env.operations[0].type == "move_shot"


@pytest.mark.parametrize("missing", [
    "schema_version", "base_version_id", "idempotency_key", "operations",
])
def test_required_envelope_fields_are_enforced(missing):
    raw = _envelope()
    del raw[missing]
    with pytest.raises(OperationSchemaError):
        _parse(raw)


def test_an_unknown_schema_version_is_refused():
    with pytest.raises(OperationSchemaError):
        _parse(_envelope(schema_version=99))


def test_an_unknown_envelope_field_is_refused():
    """Clients must not smuggle server-side fields into the envelope."""
    with pytest.raises(OperationSchemaError):
        _parse(_envelope(diff={"fake": "diff"}))


@pytest.mark.parametrize("forged", ["status", "result_version_id", "diff"])
def test_forged_server_side_fields_are_refused(forged):
    with pytest.raises(OperationSchemaError):
        _parse(_envelope(**{forged: "applied"}))


def test_an_empty_operation_list_is_refused():
    with pytest.raises(OperationSchemaError):
        _parse(_envelope(operations=[]))


def test_too_many_operations_are_refused():
    ops = [{"type": "move_shot", "shot_id": f"s{i}"} for i in range(MAX_OPERATIONS_PER_REQUEST + 1)]
    with pytest.raises(OperationSchemaError):
        _parse(_envelope(operations=ops))


def test_an_overlong_reason_is_refused():
    with pytest.raises(OperationSchemaError):
        _parse(_envelope(reason="x" * (MAX_REASON_LENGTH + 1)))


def test_an_unknown_operation_type_is_refused():
    with pytest.raises(OperationSchemaError) as excinfo:
        _parse(_envelope(operations=[{"type": "delete_everything"}]))
    assert "UNKNOWN_OPERATION" in str(excinfo.value) or "unknown" in str(excinfo.value).lower()


def test_an_operation_without_a_type_is_refused():
    with pytest.raises(OperationSchemaError):
        _parse(_envelope(operations=[{"shot_id": "s1"}]))


# --- per-operation schemas -------------------------------------------------


def test_move_shot_accepts_before_or_after_but_not_both():
    ok = {"type": "move_shot", "shot_id": "s1", "after_shot_id": "s2"}
    _parse(_envelope(operations=[ok]))

    both = {"type": "move_shot", "shot_id": "s1",
            "after_shot_id": "s2", "before_shot_id": "s3"}
    with pytest.raises(OperationSchemaError):
        _parse(_envelope(operations=[both]))

    neither = {"type": "move_shot", "shot_id": "s1"}
    with pytest.raises(OperationSchemaError):
        _parse(_envelope(operations=[neither]))


def test_insert_shot_requires_all_source_fields():
    base = {
        "type": "insert_shot", "asset_id": "a1", "segment_id": "g1",
        "source_in_sec": 0.0, "source_out_sec": 2.0, "target_duration_sec": 2.0,
    }
    _parse(_envelope(operations=[base]))

    for field in ("asset_id", "segment_id", "source_in_sec", "source_out_sec"):
        broken = {k: v for k, v in base.items() if k != field}
        with pytest.raises(OperationSchemaError):
            _parse(_envelope(operations=[broken]))


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0, "2.0", True])
def test_non_finite_or_negative_times_are_refused(bad):
    op = {
        "type": "insert_shot", "asset_id": "a1", "segment_id": "g1",
        "source_in_sec": 0.0, "source_out_sec": bad, "target_duration_sec": 1.0,
    }
    with pytest.raises(OperationSchemaError):
        _parse(_envelope(operations=[op]))


def test_inverted_ranges_are_refused_at_the_schema_level():
    op = {
        "type": "insert_shot", "asset_id": "a1", "segment_id": "g1",
        "source_in_sec": 5.0, "source_out_sec": 1.0, "target_duration_sec": 1.0,
    }
    with pytest.raises(OperationSchemaError):
        _parse(_envelope(operations=[op]))


def test_trim_shot_requires_a_finite_ordered_range():
    ok = {"type": "trim_shot", "shot_id": "s1",
          "source_in_sec": 1.0, "source_out_sec": 3.0}
    _parse(_envelope(operations=[ok]))

    with pytest.raises(OperationSchemaError):
        _parse(_envelope(operations=[{"type": "trim_shot", "shot_id": "s1",
                                      "source_in_sec": 3.0, "source_out_sec": 1.0}]))


def test_lock_shot_requires_known_dimensions():
    ok = {"type": "lock_shot", "shot_id": "s1", "dimensions": ["range"], "locked": True}
    _parse(_envelope(operations=[ok]))

    with pytest.raises(OperationSchemaError):
        _parse(_envelope(operations=[{"type": "lock_shot", "shot_id": "s1",
                                      "dimensions": ["bogus"], "locked": True}]))


def test_replace_shot_requires_new_source_and_identity_comes_from_server():
    ok = {
        "type": "replace_shot", "target_shot_id": "s1",
        "asset_id": "a2", "segment_id": "g2",
        "source_in_sec": 1.0, "source_out_sec": 3.0, "target_duration_sec": 2.0,
    }
    env = _parse(_envelope(operations=[ok]))
    assert env.operations[0].type == "replace_shot"

    # A client must not dictate the replacement's identity.
    forged = dict(ok, new_shot_id="chosen-by-client")
    with pytest.raises(OperationSchemaError):
        _parse(_envelope(operations=[forged]))


def test_update_constraints_separates_hard_and_soft():
    ok = {
        "type": "update_constraints",
        "hard": {"must_include": ["opening"], "exclude": []},
        "soft": {"target_duration_sec": 60},
    }
    _parse(_envelope(operations=[ok]))

    with pytest.raises(OperationSchemaError):
        _parse(_envelope(operations=[{
            "type": "update_constraints",
            "hard": {"mystery_constraint": 1},
        }]))


def test_move_section_requires_exactly_one_anchor():
    ok = {"type": "move_section", "section_id": "sec1", "after_section_id": "sec2"}
    _parse(_envelope(operations=[ok]))
    with pytest.raises(OperationSchemaError):
        _parse(_envelope(operations=[{"type": "move_section", "section_id": "sec1"}]))


def test_remove_shot_needs_only_the_id():
    _parse(_envelope(operations=[{"type": "remove_shot", "shot_id": "s1"}]))
    with pytest.raises(OperationSchemaError):
        _parse(_envelope(operations=[{"type": "remove_shot"}]))


def test_every_operation_rejects_unknown_fields():
    """``extra="forbid"`` on each concrete type, not just the envelope."""
    for op in (
        {"type": "remove_shot", "shot_id": "s1", "extra": 1},
        {"type": "move_shot", "shot_id": "s1", "after_shot_id": "s2", "extra": 1},
    ):
        with pytest.raises(OperationSchemaError):
            _parse(_envelope(operations=[op]))


def test_the_envelope_serialises_back_to_a_stable_dict():
    env = _parse(_envelope())
    round_tripped = OperationEnvelope.from_dict(json.loads(json.dumps({
        "schema_version": env.schema_version,
        "board_id": env.board_id,
        "base_version_id": env.base_version_id,
        "idempotency_key": env.idempotency_key,
        "operations": [o.model_dump() for o in env.operations],
        "reason": env.reason,
    })))
    assert [o.model_dump() for o in round_tripped.operations] == [
        o.model_dump() for o in env.operations
    ]


def test_the_payload_digest_is_stable_and_content_sensitive():
    a = _parse(_envelope())
    b = _parse(_envelope())
    assert a.payload_digest() == b.payload_digest(), "same payload must hash the same"

    c = _parse(_envelope(reason="a different reason"))
    assert c.payload_digest() != a.payload_digest()

    d = _parse(_envelope(idempotency_key="key-2"))
    # The key itself is not part of the *payload*, so a replayed request with a
    # new key still hashes by its content.
    assert d.payload_digest() == a.payload_digest()
