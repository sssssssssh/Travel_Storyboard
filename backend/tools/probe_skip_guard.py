"""Probe: would a *legitimate Linux skip* still fail the mandatory-media job?

The Ubuntu media job failed because two newly-added suites skip on Linux with a
reason string that ``conftest._EXPECTED_SKIP_REASONS`` did not recognise, so
``pytest_sessionfinish`` treated a platform fact as an unexpected skip and set a
non-zero exit status.

This probe replays that decision for every skip reason the suite can emit on a
non-Windows host, without needing a Linux machine: it feeds synthetic skip
reports through the same predicate ``conftest`` uses.

Run:  python tools/probe_skip_guard.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import conftest  # noqa: E402

#: (suite, reason emitted on a POSIX host when the platform gate applies)
LINUX_SKIPS = [
    ("test_deep_media_http.py", "MAX_PATH only constrains Windows"),
    ("test_deep_scan_api.py", "MAX_PATH only constrains Windows"),
    ("test_deep_path_cleanup.py", "long-path behaviour is Windows-specific"),
    ("test_long_paths.py", "long-path behaviour is Windows-specific"),
    ("test_cancel_cleanup.py", "long-path behaviour is Windows-specific"),
    ("test_cancel_cleanup.py", "link creation is a platform fact"),
]

#: Skips that must STILL fail the job: a missing tool is not a platform fact.
MUST_FAIL = [
    ("test_ffmpeg_integration.py", "ffmpeg/ffprobe not available"),
    ("test_long_paths.py", "ffmpeg/ffprobe not available"),
    ("test_deep_scan_api.py", "ffmpeg not available"),
    ("test_job_cancel_kills_process.py", "ffmpeg not available"),
]


class _FakeReport:
    def __init__(self, nodeid: str, reason: str) -> None:
        self.nodeid = nodeid
        self.longrepr = f"Skipped: {reason}"


def main() -> int:
    failures = 0

    print("legitimate platform skips (must be tolerated):")
    for suite, reason in LINUX_SKIPS:
        ok = conftest._is_expected_skip(_FakeReport(suite, reason))
        print(f"  {'OK  ' if ok else 'FAIL'} {suite:32} {reason}")
        failures += 0 if ok else 1

    print("tool-missing skips (must still fail the job):")
    for suite, reason in MUST_FAIL:
        recognized = conftest._is_expected_skip(_FakeReport(suite, reason))
        print(f"  {'OK  ' if not recognized else 'FAIL'} {suite:32} {reason}")
        failures += 0 if not recognized else 1

    print(f"\nwhitelist = {conftest._EXPECTED_SKIP_REASONS}")
    print("VERDICT:", "PASS - Linux platform skips cannot fail the job"
          if failures == 0 else f"FAIL - {failures} case(s) wrong")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
