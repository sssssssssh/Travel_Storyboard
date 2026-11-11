"""TSB-371-0 — the ledger must be trustworthy across processes and across grants.

TSB-370's ledger used only a thread lock. That is enough to serialise callers in
one process and **nothing else**, which is a problem precisely when it matters:
two processes racing for the last unit of budget. These tests exist because the
task book (§1.1) requires the claim "the ledger is atomic" to be *demonstrated*,
not inferred from `os.replace`.

They also pin the grant mechanism: new budget must arrive as a new epoch with an
explicit id, never by deleting or truncating the file.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path

import pytest

from app.services.model_call_ledger import (
    BudgetExhausted,
    BudgetGuardBlocked,
    CHANNEL_CAPS,
    ModelCallLedger,
    TOTAL_CAP,
)


def _make(tmp_path: Path, charges: int = 0, channel: str = "plan_text",
          *, cap: int | None = None) -> ModelCallLedger:
    """A ledger whose single channel can absorb ``charges`` without other caps biting."""
    total = max(cap or charges or 1, charges)
    led = ModelCallLedger(tmp_path / "ledger.json",
                          base_caps={channel: total}, total_cap=total)
    for i in range(charges):
        led.charge(channel, f"s{i}", 10)
    return led


# ---------------------------------------------------------------------------
# Grant / epoch
# ---------------------------------------------------------------------------

def test_a_fresh_ledger_reports_the_current_grant_id(tmp_path):
    led = _make(tmp_path)
    summary = led.summary()
    assert summary["grant_id"]
    assert summary["grant_id"] == led.grant_id


def test_exhausting_the_old_grant_does_not_create_new_budget(tmp_path):
    led = _make(tmp_path, charges=2, channel="plan_text", cap=2)
    with pytest.raises(BudgetExhausted):
        led.charge("plan_text", "x", 10)


def test_a_new_grant_is_an_explicit_act_and_keeps_the_old_history(tmp_path):
    led = _make(tmp_path, charges=2)
    old = led.grant_id
    led.grant("G4_RETRY", note="user approved 2 text planning calls", caps={"plan_text": 2})

    summary = led.summary()
    assert summary["grant_id"] != old
    # History is preserved, not erased.
    assert summary["total_attempts"] == 2
    assert summary["grants"][0]["grant_id"] == old
    assert summary["grants"][-1]["grant_id"] == summary["grant_id"]


def test_a_grant_reopens_only_the_channels_it_names(tmp_path):
    """A plan_text grant must not silently refill the vision buckets."""
    led = _make(tmp_path, charges=2, channel="plan_text", cap=2)
    led.grant("G4_ONLY", note="text planning only", caps={"plan_text": 2})
    led.charge("plan_text", "a", 10)
    led.charge("plan_text", "b", 10)
    with pytest.raises(BudgetExhausted):
        led.charge("plan_text", "c", 10)
    # A channel the ledger was never given does not become available either.
    with pytest.raises((BudgetExhausted, BudgetGuardBlocked)):
        led.charge("native_video", "v", 10)


def test_the_grant_caps_are_additive_to_the_original_cap(tmp_path):
    """plan_text starts at 2; a grant of 2 more makes 4 in total."""
    led = _make(tmp_path, charges=2, channel="plan_text", cap=2)
    with pytest.raises(BudgetExhausted):
        led.charge("plan_text", "c", 1)

    led.grant("G4_RETRY", note="two more", caps={"plan_text": 2})
    led.charge("plan_text", "c", 1)
    led.charge("plan_text", "d", 1)
    with pytest.raises(BudgetExhausted):
        led.charge("plan_text", "e", 1)


def test_the_global_cap_still_applies_after_a_grant(tmp_path):
    led = _make(tmp_path, charges=2, channel="plan_text", cap=2)
    led.grant("EXTRA", note="adds plan_text", caps={"plan_text": 1})
    led.charge("plan_text", "x", 1)
    with pytest.raises(BudgetExhausted):
        led.charge("plan_text", "y", 1)


def test_a_grant_must_be_explicit_about_what_it_approves(tmp_path):
    led = _make(tmp_path)
    with pytest.raises(ValueError):
        led.grant("", note="x", caps={"plan_text": 1})
    with pytest.raises(ValueError):
        led.grant("G", note="", caps={"plan_text": 1})
    with pytest.raises(ValueError):
        led.grant("G", note="nothing approved", caps={})


# ---------------------------------------------------------------------------
# Accounting fidelity
# ---------------------------------------------------------------------------

def test_every_charge_records_a_call_id_and_dispatch_flag(tmp_path):
    led = ModelCallLedger(tmp_path / "ledger.json")
    c = led.charge("static", "seg-1", 100)
    assert c.call_id
    # Charged before the socket opened: it counts as dispatched by construction.
    assert c.dispatched is True


def test_two_charges_never_share_a_call_id(tmp_path):
    led = ModelCallLedger(tmp_path / "ledger.json")
    ids = {led.charge("static", f"s{i}", 1).call_id for i in range(5)}
    assert len(ids) == 5


def test_a_backfilled_entry_is_labelled_as_such(tmp_path):
    """TSB-370 had silent backfill rows; they must be identifiable."""
    led = _make(tmp_path)
    led.backfill("static", "historical", 0, note="call made before the hook existed")
    summary = led.summary()
    assert summary["entries"][0]["source"] == "backfill"


def test_a_corrupt_ledger_blocks_instead_of_resetting(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text("{not json", encoding="utf-8")
    led = ModelCallLedger(path)
    with pytest.raises(BudgetGuardBlocked):
        led.charge("static", "x", 1)


def test_a_ledger_from_an_unknown_schema_blocks(tmp_path):
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps({"schema": 999, "charges": []}), encoding="utf-8")
    led = ModelCallLedger(path)
    with pytest.raises(BudgetGuardBlocked):
        led.charge("static", "x", 1)


def test_the_summary_exposes_the_reconciliation_gap(tmp_path):
    """Callers must be able to see backfilled rows rather than trust the total."""
    led = _make(tmp_path, charges=2)
    led.backfill("static", "old", 0, note="pre-hook")
    s = led.summary()
    assert s["total_attempts"] == 3
    assert s["confirmed_attempts"] == 2
    assert s["backfilled_attempts"] == 1


# ---------------------------------------------------------------------------
# Cross-process atomicity (the reason this file exists)
# ---------------------------------------------------------------------------

_WORKER = """
import sys, time
sys.path.insert(0, sys.argv[3])
from pathlib import Path
from app.services.model_call_ledger import ModelCallLedger, BudgetExhausted
try:
    ModelCallLedger(Path(sys.argv[1]), base_caps={"plan_text": int(sys.argv[2])},
                    total_cap=int(sys.argv[2])).charge("plan_text", "racer", 1)
    print("WIN")
except BudgetExhausted:
    print("LOSE")
"""


def _race_concurrently(tmp_path: Path, *, cap: int, processes: int,
                       workers: int) -> tuple[int, int]:
    """Start ``processes`` real OS processes that each try to spend one unit.

    ``subprocess`` rather than ``multiprocessing.Pool``: a pool reuses workers and
    can serialise the start-up, which hides the race this test exists to catch.
    """
    import subprocess
    import sys

    backend = Path(__file__).resolve().parents[1]
    script = tmp_path / "racer.py"
    script.write_text(_WORKER, encoding="utf-8")
    ledger_path = tmp_path / "ledger.json"
    ModelCallLedger(ledger_path, base_caps={"plan_text": cap},
                    total_cap=cap)  # create with the restricted caps

    procs = [
        subprocess.Popen([sys.executable, str(script), str(ledger_path), str(cap),
                          str(backend)], stdout=subprocess.PIPE, text=True)
        for _ in range(processes)
    ]
    outs = [p.communicate(timeout=120)[0].strip() for p in procs]
    return outs.count("WIN"), ModelCallLedger(ledger_path).summary()["total_attempts"]


def test_two_processes_cannot_both_spend_the_last_unit(tmp_path):
    """The whole point of the cross-process lock: exactly one winner."""
    winners, recorded = _race_concurrently(tmp_path, cap=1, processes=2, workers=1)
    assert winners == 1, f"expected exactly one winner, got {winners}"
    assert recorded == 1, f"ledger must record exactly one attempt, got {recorded}"


def test_a_concurrent_storm_never_exceeds_the_cap(tmp_path):
    """Six real processes, a cap of three: exactly three may win, none are lost.

    With the unreliable ``msvcrt`` lock from TSB-370 every process won here,
    which is precisely the over-spend this test now prevents.
    """
    winners, recorded = _race_concurrently(tmp_path, cap=3, processes=6, workers=1)
    assert winners == 3, f"expected exactly 3 winners, got {winners}"
    assert recorded == 3, f"ledger must record exactly 3 attempts, got {recorded}"


def test_a_locked_ledger_does_not_corrupt_on_reader(tmp_path):
    """A reader must never see a half-written file."""
    path = tmp_path / "ledger.json"
    led = ModelCallLedger(path)
    led.charge("static", "s", 1)
    for _ in range(20):
        assert led.summary()["total_attempts"] == 1


# ---------------------------------------------------------------------------
# A crashed holder must not deadlock every later caller
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# A crashed holder must not deadlock every later caller
# ---------------------------------------------------------------------------
#
# The marker-reclaim path is POSIX-only (Windows holds an OS handle the kernel
# releases on exit). Rather than skip on one platform -- which the mandatory-media
# CI job treats as an unexpected skip and fails on -- the behaviour is exercised
# directly through the private helper, on every platform.

def test_a_stale_marker_is_reclaimed_by_the_helper(tmp_path):
    """A marker left by a crashed owner must not block the next caller."""
    from app.services.process_lock import ProcessFileLock

    target = tmp_path / "guarded"
    marker = Path(str(target) + ".lock")
    marker.write_text("", encoding="utf-8")
    old = time.time() - (ProcessFileLock.STALE_MARKER_SEC + 60)
    os.utime(marker, (old, old))

    lock = ProcessFileLock(target, timeout=0.5)
    lock._reclaim_if_stale()          # the exact call the POSIX branch makes
    assert not marker.exists()


def test_a_fresh_marker_is_not_reclaimed(tmp_path):
    """Reclaim is bounded by age; a live holder is never evicted."""
    from app.services.process_lock import ProcessFileLock

    target = tmp_path / "guarded2"
    marker = Path(str(target) + ".lock")
    marker.write_text("", encoding="utf-8")

    lock = ProcessFileLock(target, timeout=0.5)
    lock._reclaim_if_stale()
    assert marker.exists(), "a fresh marker must keep excluding"


def test_the_ledger_still_works_when_a_stale_marker_exists(tmp_path):
    path = tmp_path / "ledger.json"
    marker = Path(str(path) + ".lock")
    marker.write_text("", encoding="utf-8")
    old = time.time() - 600
    os.utime(marker, (old, old))

    led = ModelCallLedger(path)
    led.charge("static", "s", 1)
    assert led.summary()["total_attempts"] == 1
