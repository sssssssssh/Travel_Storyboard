"""TSB-367-R1 — shot identity must be decided once, at proposal time (R1-01).

Tests first. On `eb63003` every case here fails, because ``insert_shot`` and
``replace_shot`` call ``_new_shot_id()`` on *each* execution: the preview the user
sees and the version that gets saved therefore carry **different** ids.

The fix is not "reuse a random seed" — it is that the proposal is the place where
identity is decided, and the id is persisted with it. These tests pin down:

* preview id == persisted proposal id == final version id;
* several inserts/replaces in one batch each get distinct, stable ids;
* confirming the same operation twice does not mint a second identity;
* a client still cannot forge the internal field;
* applying a proposal that has no recorded identity fails rather than guessing.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.database import SessionLocal, init_db
from app.models import Asset, PlanOperation, Project, Segment, Storyboard, new_id


@pytest.fixture(scope="module", autouse=True)
def _schema():
    init_db()


def _seeded(name: str) -> dict[str, str]:
    ids = {
        "project": new_id(), "asset": new_id(),
        "seg1": new_id(), "seg2": new_id(), "seg3": new_id(),
    }
    with SessionLocal() as s:
        s.add(Project(id=ids["project"], name=name, source_roots=[],
                      output_dir="", settings={}))
        s.flush()
        s.add(Asset(id=ids["asset"], project_id=ids["project"],
                    source_path=f"/tmp/{name}.mp4", display_name=f"{name}.mp4",
                    file_size_bytes=10, mtime_ns=1, duration_sec=60.0,
                    width=1920, height=1080))
        s.flush()
        for key, (start, end) in (("seg1", (0.0, 5.0)), ("seg2", (5.0, 10.0)),
                                  ("seg3", (10.0, 15.0))):
            s.add(Segment(id=ids[key], project_id=ids["project"],
                          asset_id=ids["asset"], start_sec=start, end_sec=end,
                          detected_start_sec=start, detected_end_sec=end))
        s.commit()

    from app.services.storyboard_service import get_current_version, save_version

    base = get_current_version(project_id=ids["project"])
    view = save_version(
        project_id=ids["project"], base_version_id=base.version_id,
        shots=[
            {"shot_id": "shot-1", "asset_id": ids["asset"], "segment_id": ids["seg1"],
             "source_in_sec": 0.0, "source_out_sec": 2.0, "target_duration_sec": 2.0},
            {"shot_id": "shot-2", "asset_id": ids["asset"], "segment_id": ids["seg2"],
             "source_in_sec": 5.0, "source_out_sec": 7.0, "target_duration_sec": 2.0},
        ],
    )
    ids["board"] = view.board_id
    ids["version"] = view.version_id
    return ids


def _env(ids: dict[str, str], ops: list[dict], key: str | None = None) -> dict:
    return {
        "schema_version": 1,
        "base_version_id": ids["version"],
        "idempotency_key": key or str(uuid.uuid4()),
        "operations": ops,
        "reason": "r1-01",
    }


def _insert(ids: dict[str, str], seg: str = "seg3", start: float = 10.0,
            end: float = 12.0) -> dict:
    return {
        "type": "insert_shot", "asset_id": ids["asset"], "segment_id": ids[seg],
        "source_in_sec": start, "source_out_sec": end,
        "target_duration_sec": end - start,
        "after_shot_id": "shot-1",
    }


def _replace(ids: dict[str, str], target: str = "shot-2", seg: str = "seg3") -> dict:
    seg_bounds = {"seg1": (0.0, 5.0), "seg2": (5.0, 10.0), "seg3": (10.0, 15.0)}
    start, _ = seg_bounds[seg]
    return {
        "type": "replace_shot", "target_shot_id": target,
        "asset_id": ids["asset"], "segment_id": ids[seg],
        "source_in_sec": start, "source_out_sec": start + 2.0,
        "target_duration_sec": 2.0,
    }


def _created_ids(diff: dict) -> list[str]:
    return list(diff["shots"]["added"])


# ---------------------------------------------------------------------------
# insert
# ---------------------------------------------------------------------------


def test_r1_01_insert_preview_id_equals_final_version_id():
    from app.services.plan_operation_service import apply_operation, propose_operation

    ids = _seeded("r101-insert")
    op = propose_operation(project_id=ids["project"], envelope=_env(ids, [_insert(ids)]))
    preview = _created_ids(op.diff)
    assert len(preview) == 1

    applied = apply_operation(project_id=ids["project"], operation_id=op.id)
    # The diff returned by apply must name the same id as the preview...
    assert _created_ids(applied.diff) == preview

    # ...and so must the saved version.
    with SessionLocal() as s:
        from app.models import StoryboardVersion

        version = s.get(StoryboardVersion, applied.result_version_id)
        saved_ids = [shot["shot_id"] for shot in version.shots]
    assert set(preview) <= set(saved_ids), (
        "the preview id is not in the saved version — identity was regenerated"
    )


def test_r1_01_insert_identity_is_persisted_on_the_proposal():
    from app.services.plan_operation_service import propose_operation

    ids = _seeded("r101-persist")
    op = propose_operation(project_id=ids["project"], envelope=_env(ids, [_insert(ids)]))
    preview = _created_ids(op.diff)[0]

    with SessionLocal() as s:
        row = s.get(PlanOperation, op.id)
        stored = row.operations[0]
    assert stored.get("resolved_shot_id") == preview, (
        "the proposal must persist the identity it previewed"
    )


def test_r1_01_multiple_inserts_each_get_a_distinct_stable_id():
    from app.services.plan_operation_service import apply_operation, propose_operation

    ids = _seeded("r101-multi-insert")
    ops = [
        _insert(ids, "seg3", 10.0, 11.0),
        _insert(ids, "seg3", 12.0, 13.0),
        _insert(ids, "seg3", 13.5, 14.5),
    ]
    op = propose_operation(project_id=ids["project"], envelope=_env(ids, ops))
    preview = _created_ids(op.diff)
    assert len(preview) == 3
    assert len(set(preview)) == 3, "identities must be distinct"

    applied = apply_operation(project_id=ids["project"], operation_id=op.id)
    assert _created_ids(applied.diff) == preview

    with SessionLocal() as s:
        from app.models import StoryboardVersion

        version = s.get(StoryboardVersion, applied.result_version_id)
        saved = [shot["shot_id"] for shot in version.shots]
    assert set(preview) <= set(saved)


# ---------------------------------------------------------------------------
# replace
# ---------------------------------------------------------------------------


def test_r1_01_replace_preview_id_equals_final_version_id():
    from app.services.plan_operation_service import apply_operation, propose_operation

    ids = _seeded("r101-replace")
    op = propose_operation(project_id=ids["project"],
                           envelope=_env(ids, [_replace(ids)]))
    preview = _created_ids(op.diff)
    assert len(preview) == 1

    applied = apply_operation(project_id=ids["project"], operation_id=op.id)
    assert _created_ids(applied.diff) == preview

    with SessionLocal() as s:
        from app.models import StoryboardVersion

        version = s.get(StoryboardVersion, applied.result_version_id)
        saved = [shot["shot_id"] for shot in version.shots]
    assert set(preview) <= set(saved)
    # And the old identity is gone from the plan (replace creates a new one).
    assert "shot-2" not in saved


def test_r1_01_replace_mapping_is_recorded_per_operation():
    """The diff must tie *this* replacement to *its* new id, not guess by asset."""
    from app.services.plan_operation_service import propose_operation

    ids = _seeded("r101-replace-map")
    op = propose_operation(project_id=ids["project"],
                           envelope=_env(ids, [_replace(ids, target="shot-2")]))

    replaced = op.diff["shots"]["replaced"]
    assert len(replaced) == 1
    assert replaced[0]["old_shot_id"] == "shot-2"
    assert replaced[0]["new_shot_id"] == _created_ids(op.diff)[0]


def test_r1_01_multiple_replaces_map_correctly():
    from app.services.plan_operation_service import apply_operation, propose_operation

    ids = _seeded("r101-multi-replace")
    ops = [
        _replace(ids, target="shot-1", seg="seg3"),
        _replace(ids, target="shot-2", seg="seg3"),
    ]
    # Segments must be inside their own ranges; both replacements use seg3 with
    # distinct sub-ranges so they are individually valid.
    ops[1]["source_in_sec"] = 13.0
    ops[1]["source_out_sec"] = 15.0
    ops[1]["target_duration_sec"] = 2.0

    op = propose_operation(project_id=ids["project"], envelope=_env(ids, ops))
    replaced = op.diff["shots"]["replaced"]
    assert len(replaced) == 2
    mapping = {r["old_shot_id"]: r["new_shot_id"] for r in replaced}
    assert mapping["shot-1"] != mapping["shot-2"], "each replacement needs its own id"
    assert set(mapping.values()) == set(_created_ids(op.diff))

    applied = apply_operation(project_id=ids["project"], operation_id=op.id)
    assert {r["old_shot_id"]: r["new_shot_id"]
            for r in applied.diff["shots"]["replaced"]} == mapping


# ---------------------------------------------------------------------------
# replay / confirmation
# ---------------------------------------------------------------------------


def test_r1_01_confirming_twice_does_not_mint_a_second_identity():
    from app.services.plan_operation_service import apply_operation, propose_operation

    ids = _seeded("r101-double")
    op = propose_operation(project_id=ids["project"], envelope=_env(ids, [_insert(ids)]))
    first = apply_operation(project_id=ids["project"], operation_id=op.id)
    second = apply_operation(project_id=ids["project"], operation_id=op.id)

    assert second.result_version_id == first.result_version_id
    assert _created_ids(second.diff) == _created_ids(first.diff)

    with SessionLocal() as s:
        board = s.scalars(select(Storyboard).where(
            Storyboard.project_id == ids["project"])).one()
        from app.models import StoryboardVersion

        version = s.get(StoryboardVersion, board.current_version_id)
        ids_now = [shot["shot_id"] for shot in version.shots]
    assert len([i for i in ids_now if i.startswith(first.diff["shots"]["added"][0])]) <= 1
    assert _created_ids(first.diff)[0] in ids_now


# ---------------------------------------------------------------------------
# forging / missing identity
# ---------------------------------------------------------------------------


def test_r1_01_a_client_cannot_forge_the_internal_identity(client):
    ids = _seeded("r101-forge")
    body = _env(ids, [_insert(ids)])
    body["operations"][0]["resolved_shot_id"] = "i-choose-this"

    resp = client.post(
        f"/api/projects/{ids['project']}/storyboards/{ids['board']}/operations",
        json=body,
    )
    assert resp.status_code in (400, 422), resp.text
    detail = resp.json().get("detail")
    text = str(detail).lower()
    assert "resolved_shot_id" in text or "extra" in text or "unknown" in text


def test_r1_01_applying_a_proposal_without_identity_fails_instead_of_guessing():
    """A proposal written by an older build has no recorded identity."""
    from app.services.plan_operation_service import OperationFailed, apply_operation

    ids = _seeded("r101-missing-id")
    with SessionLocal() as s:
        row = PlanOperation(
            id=new_id(), project_id=ids["project"], board_id=ids["board"],
            base_version_id=ids["version"], idempotency_key=new_id(),
            schema_version=1,
            operations=[{k: v for k, v in _insert(ids).items()
                         if k != "resolved_shot_id"}],
            diff={}, status="proposed", reason="", actor="user", payload_digest="",
        )
        s.add(row)
        s.commit()
        op_id = row.id

    with pytest.raises(Exception) as excinfo:
        apply_operation(project_id=ids["project"], operation_id=op_id)
    assert "identity" in str(excinfo.value).lower() or \
           getattr(excinfo.value, "code", "") in {"MISSING_SHOT_IDENTITY", "OPERATION_FAILED"}
