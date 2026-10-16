"""Reverse verification for TSB-326: neuter a guard, confirm tests go red.

Run from ``backend/``. Restores the original file afterwards. Each mutation
targets exactly one guard so the failure names the guard it proves.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

TARGET = pathlib.Path("app/services/vision/providers/native_video.py")
ORIGINAL = TARGET.read_text(encoding="utf-8")

MUTATIONS = {
    "protocol-registry gate (accept any protocol name)": (
        """        try:
            protocol = get_protocol(request.protocol)
        except KeyError as exc:
            raise ModelError(MODEL_CAPABILITY_UNSUPPORTED, str(exc)) from None""",
        """        from ..video_capabilities import VideoProtocol
        protocol = VideoProtocol(
            name=request.protocol or "x",
            allowed_mimes=frozenset({"video/mp4"}),
            transport="x",
        )""",
    ),
    "payload validation (skip content sniffing)": (
        """        mime = validate_video_payload(
            video.data, video.mime_type, max_bytes=self.max_bytes
        )""",
        """        mime = "video/mp4" """,
    ),
    "authorization chokepoint": (
        "        require_context(request.authorization)",
        "        _ = request.authorization",
    ),
    "capability_gate body": (
        """        raise ModelError(
            MODEL_CAPABILITY_UNSUPPORTED,
            f"VIDEO_CAPABILITY_{str(state).upper()}: the video channel is not "
            "allowed to send (verified capability required)",
        )""",
        "        return",
    ),
}


def run_suite() -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/test_native_video_provider.py"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
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
            print(f"  SKIP {label}: anchor not found (file drifted from the script)")
            problems.append(label)
            continue
        TARGET.write_text(ORIGINAL.replace(needle, replacement, 1), encoding="utf-8")
        try:
            code, out = run_suite()
        finally:
            TARGET.write_text(ORIGINAL, encoding="utf-8")

        last = out.strip().splitlines()[-1] if out.strip() else "(no output)"
        if code != 0:
            print(f"  OK   {label}")
            print(f"         -> {last}")
        else:
            print(f"  FAIL {label}: suite stayed green, so the tests do not cover it")
            problems.append(label)

    TARGET.write_text(ORIGINAL, encoding="utf-8")
    print()
    print("VERDICT:", "PASS - every guard is covered by a failing test" if not problems
          else f"FAIL - uncovered guards: {problems}")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
