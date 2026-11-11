"""TSB-371-0 — the real-model attempt ledger, rebuilt to be auditable.

TSB-370's ledger worked well enough to stop over-spending in one process, but the
TSB-371 reconciliation found three gaps that made its numbers impossible to prove:

* a **thread** lock cannot serialise two processes racing for the last unit;
* a charge recorded no ``call_id``, so it could not be matched to a server response;
* backfilled rows were indistinguishable from measured ones, which is how "17/20"
  and "20/20" both ended up in the docs.

This version addresses all three, and adds the **grant** mechanism the task book
requires: new budget arrives as a new epoch with an explicit id, note and per
channel caps. Budget is never obtained by deleting the file or truncating it.

Everything here decides; nothing here sends.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field

from .process_lock import LockTimeout, ProcessFileLock
from pathlib import Path
from typing import Any

#: Hard caps from the task book. Retries are included: every HTTP attempt spends
#: one, whichever layer issued it.
CHANNEL_CAPS: dict[str, int] = {
    "static": 12,
    "multiframe": 4,
    "native_video": 2,
    "plan_text": 2,
    "capability_probe": 2,
}

TOTAL_CAP = 20

MAX_REQUEST_BYTES = 8_000_000
MAX_TOTAL_VIDEO_BYTES = 20_000_000
MAX_TOTAL_IMAGE_BYTES = 20_000_000
MAX_TOTAL_TEXT_BYTES = 2_000_000

#: Bumped when the on-disk shape changes incompatibly. An older/newer file blocks
#: rather than being silently reinterpreted.
SCHEMA_VERSION = 2


class BudgetExhausted(Exception):
    """Raised instead of sending when the ledger has no room."""

    code = "BUDGET_EXHAUSTED"

    def __init__(self, channel: str, reason: str) -> None:
        self.channel = channel
        self.reason = reason
        super().__init__(f"{channel}: {reason}")


class BudgetGuardBlocked(Exception):
    """Raised when the ledger cannot be trusted at all; do not send."""

    code = "BUDGET_GUARD_BLOCKED"

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass
class Charge:
    channel: str
    segment_id: str
    bytes_sent: int
    at: float
    note: str = ""
    #: TSB-371: a stable identity per attempt, so a charge can be matched to a
    #: server response or a job record. TSB-370 had no such thing, which is why the
    #: reconciliation could not close its gaps.
    call_id: str = ""
    #: False only for ``backfill()`` rows, which assert that a call happened
    #: without this ledger having seen it.
    dispatched: bool = True
    grant_id: str = ""
    source: str = "measured"          # measured | backfill


@dataclass
class Grant:
    grant_id: str
    note: str
    caps: dict[str, int]
    at: float
    granted_by: str = "user"


@dataclass
class LedgerState:
    schema: int = SCHEMA_VERSION
    grant_id: str = ""
    grants: list[Grant] = field(default_factory=list)
    charges: list[Charge] = field(default_factory=list)

    def used(self, channel: str) -> int:
        return sum(1 for c in self.charges if c.channel == channel)

    def total(self) -> int:
        return len(self.charges)

    def bytes_for(self, channel: str) -> int:
        return sum(c.bytes_sent for c in self.charges if c.channel == channel)


def _process_lock(path: Path):
    """Exclusive, cross-process lock (see :mod:`app.services.process_lock`)."""
    return ProcessFileLock(path, timeout=30.0)


class ModelCallLedger:
    """Persistent, cross-process-safe, grant-aware attempt accounting."""

    def __init__(self, path: Path, *, base_caps: dict[str, int] | None = None,
                 total_cap: int = TOTAL_CAP) -> None:
        self.path = Path(path)
        self._thread_lock = threading.Lock()
        self.base_caps = dict(base_caps or CHANNEL_CAPS)
        self.total_cap = total_cap
        self._ensure()

    # -- persistence ------------------------------------------------------
    @property
    def grant_id(self) -> str:
        return self._read().grant_id

    def _ensure(self) -> None:
        with _process_lock(self.path):
            if not self.path.exists():
                state = LedgerState(grant_id=uuid.uuid4().hex[:12])
                state.grants.append(Grant(
                    grant_id=state.grant_id, note="initial experiment grant",
                    caps=dict(self.base_caps), at=time.time()))
                self._write(state)

    def _read(self) -> LedgerState:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise BudgetGuardBlocked("ledger missing") from exc
        except (OSError, ValueError) as exc:
            raise BudgetGuardBlocked(f"ledger unreadable: {exc}") from exc

        if not isinstance(raw, dict):
            raise BudgetGuardBlocked("ledger is not an object")
        if raw.get("schema") != SCHEMA_VERSION:
            raise BudgetGuardBlocked(
                f"ledger schema {raw.get('schema')!r} is not {SCHEMA_VERSION}; "
                "refusing to reinterpret old budget")

        charges = []
        for item in raw.get("charges", []):
            try:
                charges.append(Charge(**item))
            except TypeError as exc:
                raise BudgetGuardBlocked(f"malformed charge: {exc}") from exc
        grants = [Grant(**g) for g in raw.get("grants", [])]
        return LedgerState(schema=SCHEMA_VERSION, grant_id=raw.get("grant_id", ""),
                           grants=grants, charges=charges)

    def _write(self, state: LedgerState) -> None:
        # A unique temp name: two writers (or a retry) must never fight over
        # the same intermediate file.
        tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        payload = {
            "schema": state.schema,
            "grant_id": state.grant_id,
            "grants": [asdict(g) for g in state.grants],
            "charges": [asdict(c) for c in state.charges],
        }
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        deadline = time.monotonic() + 10.0
        while True:
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:
                # Windows: a reader inside the lock still holds a share that blocks
                # the rename for a few milliseconds. Retry briefly, then fail loudly
                # rather than leaving a half-applied budget.
                if time.monotonic() > deadline:
                    tmp.unlink(missing_ok=True)
                    raise BudgetGuardBlocked(
                        "could not commit the ledger (file busy); refusing to send")
                time.sleep(0.01)

    # -- caps -------------------------------------------------------------
    def effective_caps(self, state: LedgerState) -> dict[str, int]:
        """Base caps plus everything granted so far (grants are additive)."""
        caps = dict(self.base_caps)
        for g in state.grants[1:]:          # grants[0] is the initial epoch
            for ch, n in (g.caps or {}).items():
                caps[ch] = caps.get(ch, 0) + int(n)
        return caps

    def effective_total(self, state: LedgerState) -> int:
        extra = sum(int(n) for g in state.grants[1:]
                    for n in (g.caps or {}).values())
        return self.total_cap + extra

    # -- policy -----------------------------------------------------------
    def _check(self, state: LedgerState, channel: str, size: int) -> None:
        caps = self.effective_caps(state)
        if channel not in caps:
            raise BudgetGuardBlocked(f"unknown channel {channel!r}")
        total_cap = self.effective_total(state)
        if state.total() >= total_cap:
            raise BudgetExhausted(channel, f"total attempts {total_cap} reached")
        if state.used(channel) >= caps[channel]:
            raise BudgetExhausted(channel, f"channel cap {caps[channel]} reached")
        if size > MAX_REQUEST_BYTES:
            raise BudgetExhausted(
                channel, f"payload {size}B exceeds {MAX_REQUEST_BYTES}B")

        if channel == "native_video":
            if state.bytes_for(channel) + size > MAX_TOTAL_VIDEO_BYTES:
                raise BudgetExhausted(
                    channel, f"video total would exceed {MAX_TOTAL_VIDEO_BYTES}B")
        elif channel in ("static", "multiframe"):
            if state.bytes_for(channel) + size > MAX_TOTAL_IMAGE_BYTES:
                raise BudgetExhausted(
                    channel, f"image total would exceed {MAX_TOTAL_IMAGE_BYTES}B")
        elif channel == "plan_text":
            if state.bytes_for(channel) + size > MAX_TOTAL_TEXT_BYTES:
                raise BudgetExhausted(
                    channel, f"text total would exceed {MAX_TOTAL_TEXT_BYTES}B")

    def remaining(self) -> dict[str, Any]:
        with _process_lock(self.path):
            state = self._read()
        caps = self.effective_caps(state)
        total_cap = self.effective_total(state)
        return {
            "grant_id": state.grant_id,
            "total_used": state.total(),
            "total_left": max(0, total_cap - state.total()),
            "per_channel": {
                ch: {"used": state.used(ch), "cap": cap,
                     "left": max(0, cap - state.used(ch))}
                for ch, cap in caps.items()
            },
        }

    # -- the one mutating operation --------------------------------------
    def charge(self, channel: str, segment_id: str, size: int, note: str = "") -> Charge:
        """Reserve one attempt. Call this *immediately before* sending."""
        with self._thread_lock, _process_lock(self.path):
            state = self._read()
            self._check(state, channel, size)
            charge = Charge(
                channel=channel, segment_id=segment_id, bytes_sent=int(size),
                at=time.time(), note=note, call_id=uuid.uuid4().hex[:16],
                dispatched=True, grant_id=state.grant_id, source="measured",
            )
            state.charges.append(charge)
            self._write(state)
            return charge

    def backfill(self, channel: str, segment_id: str, size: int, note: str = "") -> Charge:
        """Record an attempt this ledger did not see (historical import).

        Exists so TSB-370's manually-added rows can be kept **and identified**,
        instead of being indistinguishable from measured ones.
        """
        with self._thread_lock, _process_lock(self.path):
            state = self._read()
            charge = Charge(
                channel=channel, segment_id=segment_id, bytes_sent=int(size),
                at=time.time(), note=note or "backfilled",
                call_id=uuid.uuid4().hex[:16], dispatched=False,
                grant_id=state.grant_id, source="backfill",
            )
            state.charges.append(charge)
            self._write(state)
            return charge

    def grant(self, grant_id: str, *, note: str, caps: dict[str, int],
              granted_by: str = "user") -> Grant:
        """Open a new, explicitly identified budget epoch.

        Refuses to run without a real id, a real note and at least one channel —
        "new budget" must always be traceable to someone approving something.
        """
        if not grant_id or not str(grant_id).strip():
            raise ValueError("grant_id is required")
        if not note or not str(note).strip():
            raise ValueError("a grant needs a note saying what was approved")
        if not caps or all(int(v) <= 0 for v in caps.values()):
            raise ValueError("a grant must name at least one channel with a positive cap")

        with self._thread_lock, _process_lock(self.path):
            state = self._read()
            if any(g.grant_id == grant_id for g in state.grants):
                raise ValueError(f"grant {grant_id} already exists")
            g = Grant(grant_id=grant_id, note=note,
                      caps={k: int(v) for k, v in caps.items()},
                      at=time.time(), granted_by=granted_by)
            state.grants.append(g)
            state.grant_id = grant_id
            self._write(state)
            return g

    # -- reporting --------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        """Desensitised ledger for reports: no paths, no payloads."""
        with _process_lock(self.path):
            state = self._read()
        caps = self.effective_caps(state)
        measured = [c for c in state.charges if c.source == "measured"]
        backfilled = [c for c in state.charges if c.source == "backfill"]
        return {
            "grant_id": state.grant_id,
            "schema": state.schema,
            "total_attempts": state.total(),
            "confirmed_attempts": len(measured),
            "backfilled_attempts": len(backfilled),
            "by_channel": {
                ch: {"attempts": state.used(ch), "bytes": state.bytes_for(ch)}
                for ch in caps
            },
            "caps": caps,
            "total_cap": self.effective_total(state),
            "grants": [
                {"grant_id": g.grant_id, "note": g.note, "caps": g.caps,
                 "at": round(g.at, 3), "granted_by": g.granted_by}
                for g in state.grants
            ],
            "entries": [
                {
                    "call_id": c.call_id,
                    "channel": c.channel,
                    "segment": c.segment_id[:8],
                    "bytes": c.bytes_sent,
                    "at": round(c.at, 3),
                    "note": c.note[:80],
                    "source": c.source,
                    "dispatched": c.dispatched,
                    "grant_id": c.grant_id,
                }
                for c in state.charges
            ],
        }
