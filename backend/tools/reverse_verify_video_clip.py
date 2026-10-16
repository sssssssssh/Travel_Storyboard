"""Reverse verification for TSB-327: neuter a guard, confirm tests go red.

Run from ``backend/``. Restores the original file after each mutation.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

TARGET = pathlib.Path("app/services/vision/video_clip.py")
ORIGINAL = TARGET.read_text(encoding="utf-8")

MUTATIONS = {
    "source-change check (accept any file)": (
        """    if asset is not None:
        verify_source_unchanged(asset, source)""",
        """    if False:
        verify_source_unchanged(asset, source)""",
    ),
    "boundary check (clamp instead of refuse)": (
        """    if start_abs < segment_start_sec - tolerance or end_abs > segment_end_sec + tolerance:
        raise VideoClipError(
            SEGMENT_RANGE_INVALID,
            f"event window {start_abs:.3f}..{end_abs:.3f}s falls outside the edited "
            f"segment {segment_start_sec:.3f}..{segment_end_sec:.3f}s",
        )""",
        """    start_abs = max(start_abs, segment_start_sec)
    end_abs = min(end_abs, segment_end_sec)""",
    ),
    "negative-time rejection": (
        """    if start_rel_sec < 0 or end_rel_sec < 0:""",
        """    if False:""",
    ),
    "non-finite rejection": (
        """        if value != value or value in (float("inf"), float("-inf")):  # NaN / inf""",
        """        if False:""",
    ),
    "silent-clip enforcement on the artifact": (
        """    if info.has_audio:""",
        """    if False:""",
    ),
}


def run_suite() -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_video_clip.py"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode, proc.stdout


def main() -> int:
    _, baseline = run_suite()
    print("baseline (all guards present):")
    print("   " + baseline.strip().splitlines()[-1])
    print()

    problems = []
    for label, (needle, replacement) in MUTATIONS.items():
        if needle not in ORIGINAL:
            print(f"  SKIP {label}: anchor not found (file drifted)")
            problems.append(label)
            continue
        TARGET.write_text(ORIGINAL.replace(needle, replacement, 1), encoding="utf-8")
        try:
            code, out = run_suite()
        finally:
            TARGET.write_text(ORIGINAL, encoding="utf-8")

        lines = [l for l in out.strip().splitlines() if l.startswith("FAILED") or " passed" in l or " failed" in l]
        red = [l for l in out.splitlines() if l.startswith("FAILED")]
        if code != 0:
            print(f"  OK   {label}")
            for line in red[:3]:
                print(f"         -> {line.replace('FAILED ', '')}")
        else:
            print(f"  FAIL {label}: suite stayed green")
            problems.append(label)

    TARGET.write_text(ORIGINAL, encoding="utf-8")
    print()
    print("VERDICT:", "PASS - every guard is covered by a failing test" if not problems
          else f"FAIL - uncovered guards: {problems}")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
