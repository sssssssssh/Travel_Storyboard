"""TSB-370 — the real-model attempt/byte ledger.

This is the single authority for "may I send another request right now?". It is
deliberately **not** a config value: the task book requires that a process restart
cannot reset the allowance, and that the count survives retries at any layer.

Design rules enforced here:

* Persisted to a file **outside the repository** (never committed, never in the DB
  alongside user data).
* An attempt is charged **before** the request is sent, atomically. A request that
  times out, 500s, or is cancelled still counts — it was sent.
* The per-channel caps and the global cap are checked together, so a caller cannot
  borrow headroom from one channel to exceed the total.
* Nothing here imports an HTTP client: the ledger can make a decision, it cannot
  make a call.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

#: Hard caps from the task book (§1.1). Retries are included: every HTTP attempt
#: spends one, regardless of which layer issued it.
CHANNEL_CAPS: dict[str, int] = {
    "static": 12,
    "multiframe": 4,
    "native_video": 2,
    "plan_text": 2,
    "capability_probe": 2,
}

#: Total attempts across every channel.
TOTAL_CAP = 20

#: Payload limits.
MAX_REQUEST_BYTES = 8_000_000
MAX_TOTAL_VIDEO_BYTES = 20_000_000
MAX_TOTAL_IMAGE_BYTES = 20_000_000
MAX_TOTAL_TEXT_BYTES = 2_000_000


class BudgetExhausted(Exception):
    """Raised instead of sending when the ledger has no room."""

    code = "BUDGET_EXHAUSTED"

    def __init__(self, channel: str, reason: str) -> None:
        self.channel = channel
        self.reason = reason
        super().__init__(f"{channel}: {reason}")


class BudgetGuardBlocked(Exception):
    """Raised when the ledger cannot be established at all (§1.1)."""

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


@dataclass
class LedgerState:
    charges: list[Charge] = field(default_factory=list)

    def used(self, channel: str) -> int:
        return sum(1 for c in self.charges if c.channel == channel)

    def total(self) -> int:
        return len(self.charges)

    def bytes_for(self, channel: str) -> int:
        return sum(c.bytes_sent for c in self.charges if c.channel == channel)

    def all_bytes(self) -> int:
        return sum(c.bytes_sent for c in self.charges)


class ModelCallLedger:
    """Persistent, atomic, restart-safe attempt accounting."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    # -- persistence ------------------------------------------------------
    def _load(self) -> LedgerState:
        if not self.path.exists():
            return LedgerState()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise BudgetGuardBlocked(f"ledger unreadable: {exc}") from exc
        charges = [Charge(**c) for c in raw.get("charges", [])]
        return LedgerState(charges=charges)

    def _save(self, state: LedgerState) -> None:
        tmp = self.path.with_suffix(".tmp")
        payload = {"charges": [asdict(c) for c in state.charges]}
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)          # atomic on both Windows and POSIX

    # -- policy -----------------------------------------------------------
    def _check(self, state: LedgerState, channel: str, size: int) -> None:
        if channel not in CHANNEL_CAPS:
            raise BudgetGuardBlocked(f"unknown channel {channel!r}")
        if state.total() >= TOTAL_CAP:
            raise BudgetExhausted(channel, f"total attempts {TOTAL_CAP} reached")
        if state.used(channel) >= CHANNEL_CAPS[channel]:
            raise BudgetExhausted(
                channel, f"channel cap {CHANNEL_CAPS[channel]} reached")
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
        with self._lock:
            state = self._load()
        return {
            "total_used": state.total(),
            "total_left": max(0, TOTAL_CAP - state.total()),
            "per_channel": {
                ch: {"used": state.used(ch), "cap": cap,
                     "left": max(0, cap - state.used(ch))}
                for ch, cap in CHANNEL_CAPS.items()
            },
            "bytes": {
                "video": state.bytes_for("native_video"),
                "image": state.bytes_for("static") + state.bytes_for("multiframe"),
                "text": state.bytes_for("plan_text"),
            },
        }

    # -- the one mutating operation --------------------------------------
    def charge(self, channel: str, segment_id: str, size: int, note: str = "") -> Charge:
        """Reserve one attempt. Call this *immediately before* sending.

        Raises :class:`BudgetExhausted` (nothing was sent) or
        :class:`BudgetGuardBlocked` (no ledger, do not proceed).
        """
        with self._lock:
            try:
                state = self._load()
            except BudgetGuardBlocked:
                raise
            self._check(state, channel, size)
            charge = Charge(channel=channel, segment_id=segment_id,
                            bytes_sent=int(size), at=time.time(), note=note)
            state.charges.append(charge)
            self._save(state)
            return charge

    # -- reporting --------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        """Desensitised ledger for the report: no paths, no payloads."""
        with self._lock:
            state = self._load()
        return {
            "total_attempts": state.total(),
            "by_channel": {
                ch: {"attempts": state.used(ch), "bytes": state.bytes_for(ch)}
                for ch in CHANNEL_CAPS
            },
            "caps": CHANNEL_CAPS,
            "total_cap": TOTAL_CAP,
            "entries": [
                {
                    "channel": c.channel,
                    "segment": c.segment_id[:8],
                    "bytes": c.bytes_sent,
                    "at": round(c.at, 3),
                    "note": c.note,
                }
                for c in state.charges
            ],
        }
