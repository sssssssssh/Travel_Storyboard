"""TSB-253 proof-of-fix probe (not a test; run manually).

Demonstrates the defect the new regression tests guard:

* OLD behaviour — cancellation was only observed *between* items, so a running
  encode was waited out in full.
* NEW behaviour — ``cancel_check`` terminates the child promptly.

Run:  python <this file>
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from app.ffmpeg import FFmpegCancelled, run  # noqa: E402

CHILD_SECONDS = 8.0
CANCEL_AFTER = 0.6


def _sleep_cmd(seconds: float) -> list[str]:
    return [sys.executable, "-c", f"import time; time.sleep({seconds})"]


def old_style() -> float:
    """Pre-TSB-253: blocking subprocess.run, cancellation only checked after."""
    started = time.monotonic()
    subprocess.run(_sleep_cmd(CHILD_SECONDS), capture_output=True)
    return time.monotonic() - started


def new_style() -> float:
    """Post-TSB-253: polled cancel_check terminates the child."""
    state = {"cancelled": False}

    def _flip() -> None:
        time.sleep(CANCEL_AFTER)
        state["cancelled"] = True

    threading.Thread(target=_flip, daemon=True).start()
    started = time.monotonic()
    try:
        run(_sleep_cmd(CHILD_SECONDS), cancel_check=lambda: state["cancelled"],
            poll_interval=0.05)
    except FFmpegCancelled:
        return time.monotonic() - started
    raise AssertionError("expected FFmpegCancelled")


def main() -> int:
    old = old_style()
    new = new_style()
    print(f"child would run for      : {CHILD_SECONDS:.1f}s")
    print(f"cancel requested after   : {CANCEL_AFTER:.1f}s")
    print(f"OLD (no cancel_check)    : {old:.2f}s  <- waited the encode out")
    print(f"NEW (cancel_check)       : {new:.2f}s  <- stopped at the cancel")
    print(f"time reclaimed           : {old - new:.2f}s")
    ok = new < old / 2
    print("VERDICT:", "PASS - cancel now stops the process" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
