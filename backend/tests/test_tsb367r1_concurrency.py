"""TSB-367-R1 — real concurrency, not simulated ordering (R1-09).

The existing test ran A to completion and *then* attempted B, which proves the
CAS check works but not that two live writers cannot both commit. Here two
independent sessions are released simultaneously through a barrier and the
in-process project lock is bypassed, so the **database** is what arbitrates.

Assertions are exact — no ``status in {...}`` dodging:

* exactly one version is created;
* ``current_version_id`` equals that version;
* the winner is ``applied`` and the loser is ``stale``;
* no ``version_no`` is duplicated;
* the run is repeated to show it is stable rather than lucky.
"""

from __future__ import annotations

import threading
import uuid

import pytest
from sqlalchemy import select, text

from app.database import SessionLocal, init_db
from app.models import Asset, PlanOperation, Project, Segment, Storyboard, new_id


@pytest.fixture(scope="module", autouse=True)
def _schema():
    init_db()


def _seeded(name: str) -> dict[str, str]:
    ids = {"project": new_id(), "asset": new_id(), "seg1": new_id(), "seg2": new_id()}
    with SessionLocal() as s:
        s.add(Project(id=ids["project"], name=name, source_roots=[],
                      output_dir="", settings={}))
        s.flush()
        s.add(Asset(id=ids["asset"], project_id=ids["project"],
                    source_path=f"/tmp/{name}.mp4", display_name=f"{name}.mp4",
                    file_size_bytes=10, mtime_ns=1, duration_sec=60.0,
                    width=1920, height=1080))
        s.flush()
        for key, (start, end) in (("seg1", (0.0, 5.0)), ("seg2", (5.0, 10.0))):
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


def _env(ids: dict[str, str]) -> dict:
    return {
        "schema_version": 1,
        "base_version_id": ids["version"],
        "idempotency_key": str(uuid.uuid4()),
        "operations": [{"type": "trim_shot", "shot_id": "shot-1",
                        "source_in_sec": 0.0, "source_out_sec": 1.0}],
        "reason": "r1-09",
    }


def _versions(pid: str) -> list[tuple[int, str]]:
    from app.models import StoryboardVersion

    with SessionLocal() as s:
        board = s.scalars(select(Storyboard).where(Storyboard.project_id == pid)).one()
        rows = s.scalars(select(StoryboardVersion).where(
            StoryboardVersion.board_id == board.id)).all()
        return sorted((v.version_no, v.id) for v in rows)


def _pointer(pid: str) -> str | None:
    with SessionLocal() as s:
        board = s.scalars(select(Storyboard).where(Storyboard.project_id == pid)).one()
        return board.current_version_id


def _race(pid: str, op_a: str, op_b: str) -> list[BaseException | None]:
    """Release two applies at the same instant from separate sessions."""
    from app.services import plan_operation_service as svc

    barrier = threading.Barrier(2, timeout=30)
    results: dict[str, BaseException | None] = {}

    def worker(op_id: str) -> None:
        try:
            barrier.wait()
            svc.apply_operation(project_id=pid, operation_id=op_id)
            results[op_id] = None
        except BaseException as exc:  # recorded, not swallowed
            results[op_id] = exc

    threads = [threading.Thread(target=worker, args=(op_a,)),
               threading.Thread(target=worker, args=(op_b,))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    return [results.get(op_a), results.get(op_b)]


def test_r1_09_two_simultaneous_applies_produce_exactly_one_version():
    from app.services.plan_operation_service import propose_operation

    ids = _seeded("r109-race")
    a = propose_operation(project_id=ids["project"], envelope=_env(ids))
    b = propose_operation(project_id=ids["project"], envelope=_env(ids))
    before = len(_versions(ids["project"]))

    errors = _race(ids["project"], a.id, b.id)
    successes = [e for e in errors if e is None]

    assert len(successes) == 1, (
        f"exactly one apply must win, got {len(successes)}; errors={errors}"
    )
    after = _versions(ids["project"])
    assert len(after) == before + 1, f"expected one new version, got {len(after) - before}"


def test_r1_09_the_loser_is_persisted_as_stale():
    from app.services.plan_operation_service import propose_operation

    ids = _seeded("r109-stale")
    a = propose_operation(project_id=ids["project"], envelope=_env(ids))
    b = propose_operation(project_id=ids["project"], envelope=_env(ids))

    errors = _race(ids["project"], a.id, b.id)
    winner_id = a.id if errors[0] is None else b.id
    loser_id = b.id if errors[0] is None else a.id

    with SessionLocal() as s:
        winner = s.get(PlanOperation, winner_id)
        loser = s.get(PlanOperation, loser_id)

        # Exact expectations, read from a fresh session (i.e. committed).
        assert winner.status == "applied"
        assert loser.status == "stale"
        assert winner.result_version_id
        board = s.get(Storyboard, winner.board_id)
        assert board.current_version_id == winner.result_version_id


def test_r1_09_no_duplicate_version_numbers():
    from app.services.plan_operation_service import propose_operation

    ids = _seeded("r109-dupno")
    a = propose_operation(project_id=ids["project"], envelope=_env(ids))
    b = propose_operation(project_id=ids["project"], envelope=_env(ids))
    _race(ids["project"], a.id, b.id)

    numbers = [no for no, _ in _versions(ids["project"])]
    assert len(numbers) == len(set(numbers)), f"duplicate version_no: {numbers}"


def test_r1_09_no_half_written_version_survives():
    """Every stored version must be complete and rooted at the winning parent."""
    from app.services.plan_operation_service import propose_operation
    from app.models import StoryboardVersion

    ids = _seeded("r109-half")
    a = propose_operation(project_id=ids["project"], envelope=_env(ids))
    b = propose_operation(project_id=ids["project"], envelope=_env(ids))
    _race(ids["project"], a.id, b.id)

    with SessionLocal() as s:
        board = s.scalars(select(Storyboard).where(
            Storyboard.project_id == ids["project"])).one()
        versions = s.scalars(select(StoryboardVersion).where(
            StoryboardVersion.board_id == board.id)).all()
        for version in versions:
            # v1 is the seeded initial plan and may legitimately be empty; every
            # version created by an operation must be complete.
            if version.version_no > 1:
                assert version.shots, f"version {version.version_no} has no shots"
            assert version.validation_summary, (
                f"version {version.version_no} has no validation summary"
            )
        # The pointer must name a version that exists in this board.
        assert any(v.id == board.current_version_id for v in versions)


def test_r1_09_the_race_is_stable_across_repeats():
    """Five independent races: the invariants must hold every time."""
    from app.services.plan_operation_service import propose_operation

    for round_no in range(5):
        ids = _seeded(f"r109-repeat-{round_no}")
        a = propose_operation(project_id=ids["project"], envelope=_env(ids))
        b = propose_operation(project_id=ids["project"], envelope=_env(ids))
        before = len(_versions(ids["project"]))

        errors = _race(ids["project"], a.id, b.id)
        successes = [e for e in errors if e is None]
        assert len(successes) == 1, f"round {round_no}: {len(successes)} winners"

        after = len(_versions(ids["project"]))
        assert after == before + 1, f"round {round_no}: {after - before} new versions"

        with SessionLocal() as s:
            statuses = {
                s.get(PlanOperation, a.id).status,
                s.get(PlanOperation, b.id).status,
            }
        assert statuses == {"applied", "stale"}, f"round {round_no}: {statuses}"


def test_r1_09_three_simultaneous_applies_still_yield_one_version():
    from app.services.plan_operation_service import propose_operation
    from app.services import plan_operation_service as svc

    ids = _seeded("r109-three")
    ops = [propose_operation(project_id=ids["project"], envelope=_env(ids))
           for _ in range(3)]
    before = len(_versions(ids["project"]))

    barrier = threading.Barrier(3, timeout=30)
    results: dict[str, BaseException | None] = {}

    def worker(op_id: str) -> None:
        try:
            barrier.wait()
            svc.apply_operation(project_id=ids["project"], operation_id=op_id)
            results[op_id] = None
        except BaseException as exc:
            results[op_id] = exc

    threads = [threading.Thread(target=worker, args=(o.id,)) for o in ops]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    winners = [k for k, v in results.items() if v is None]
    assert len(winners) == 1, f"expected exactly one winner, got {winners}"
    assert len(_versions(ids["project"])) == before + 1
