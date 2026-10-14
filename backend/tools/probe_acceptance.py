"""Probe: check the task book's delivery criteria against the working tree.

Task book §8 lists what must exist for "code-level acceptance". This script
asserts that each artefact is present and that the documented counts agree with
what the test files actually contain, so a claim of completion is backed by
inspection rather than recollection.

Run:  python tools/probe_acceptance.py
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _run(cmd: list[str]) -> str:
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    return proc.stdout.strip()


def _count_tests(path: Path) -> int:
    return len(re.findall(r"^def test_", path.read_text(encoding="utf-8"), re.M))


def main() -> int:
    problems: list[str] = []

    print("== task book deliverables (TSB-266) ==")
    deliverables = {
        "code + regression tests": ROOT / "backend/tests/test_cancel_lifecycle.py",
        "RELEASE_V2_0_6.md": ROOT / "docs/RELEASE_V2_0_6.md",
        "REGRESSION.md mapping": ROOT / "docs/REGRESSION.md",
        "DESIGN.md state machine": ROOT / "docs/DESIGN.md",
        "validation evidence": ROOT / "docs/validation/v2.0.6/backend-windows-py3.13.md",
        "real-footage record": ROOT / "docs/validation/v2.0.6/WINDOWS_REAL_FOOTAGE.md",
        "staging cleanup module": ROOT / "backend/app/services/staging_cleanup.py",
        "CI skip guard": ROOT / "backend/tests/test_ci_skip_guard.py",
    }
    for label, path in deliverables.items():
        ok = path.exists()
        print(f"  {'OK  ' if ok else 'MISS'} {label}")
        if not ok:
            problems.append(f"missing deliverable: {label}")

    print("\n== TSB-265 must be recorded as NOT executed ==")
    footage = (ROOT / "docs/validation/v2.0.6/WINDOWS_REAL_FOOTAGE.md").read_text(
        encoding="utf-8")
    if "未执行" in footage:
        print("  OK   real-footage record says 未执行")
    else:
        print("  FAIL real-footage record does not say 未执行")
        problems.append("TSB-265 record must state 未执行")

    print("\n== new suites and their test counts ==")
    suites = {
        "test_cancel_lifecycle.py": 14,
        "test_cancel_cleanup.py": 21,
        "test_media_process_tree.py": 10,
        "test_cancel_recovery.py": 10,
        "test_ci_skip_guard.py": 3,
    }
    for name, expected in suites.items():
        path = ROOT / "backend/tests" / name
        actual = _count_tests(path)
        ok = actual == expected
        print(f"  {'OK  ' if ok else 'FAIL'} {name:34} {actual} (doc says {expected})")
        if not ok:
            problems.append(f"{name}: {actual} tests, doc says {expected}")

    print("\n== branch / history discipline ==")
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    print(f"  branch: {branch}")
    if branch != "main":
        problems.append(f"not on main: {branch}")
    ahead = _run(["git", "rev-list", "--left-right", "--count", "origin/main...HEAD"])
    print(f"  ahead/behind vs origin/main: {ahead}")
    if ahead.strip() != "0\t0":
        problems.append(f"not in sync with origin/main: {ahead!r}")
    dirty = _run(["git", "status", "--porcelain"])
    print(f"  worktree clean: {not dirty}")
    if dirty:
        problems.append("worktree is dirty")

    print("\n" + ("VERDICT: PASS" if not problems else "VERDICT: FAIL"))
    for p in problems:
        print(f"  - {p}")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
