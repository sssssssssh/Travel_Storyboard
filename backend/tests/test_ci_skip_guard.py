"""TSB-266 — the mandatory-media skip guard must not misjudge platform skips.

The Ubuntu required-FFmpeg CI job failed after ``test_deep_media_http.py`` and
``test_deep_scan_api.py`` were added to it: those suites skip on Linux with the
reason "MAX_PATH only constrains Windows", which ``_EXPECTED_SKIP_REASONS`` did
not list. ``pytest_sessionfinish`` therefore treated a legitimate platform fact
as an unexpected skip and failed the whole job.

The lesson generalises: adding a platform-marked suite to the mandatory job
requires its skip reason to be recognised. These tests pin both directions —
platform skips are tolerated, **tool-missing skips still fail** — so the guard
cannot be loosened into uselessness to make CI pass.
"""

from __future__ import annotations

import conftest


class _Report:
    """Stand-in for a pytest skip report (only ``nodeid``/``longrepr`` are read)."""

    def __init__(self, nodeid: str, reason: str) -> None:
        self.nodeid = nodeid
        self.longrepr = f"Skipped: {reason}"


#: Every reason a non-Windows host legitimately emits, across all suites.
PLATFORM_SKIPS = [
    ("test_deep_media_http.py", "MAX_PATH only constrains Windows"),
    ("test_deep_scan_api.py", "MAX_PATH only constrains Windows"),
    ("test_deep_path_cleanup.py", "long-path behaviour is Windows-specific"),
    ("test_long_paths.py", "long-path behaviour is Windows-specific"),
    ("test_cancel_cleanup.py", "long-path behaviour is Windows-specific"),
    ("test_cancel_cleanup.py", "link creation is a platform fact"),
    ("test_any.py", "requires Windows"),
]

#: A missing tool is NOT a platform fact: these must keep failing the job.
TOOL_MISSING_SKIPS = [
    ("test_ffmpeg_integration.py", "ffmpeg/ffprobe not available"),
    ("test_long_paths.py", "ffmpeg/ffprobe not available"),
    ("test_deep_scan_api.py", "ffmpeg not available"),
    ("test_job_cancel_kills_process.py", "ffmpeg not available"),
]


def test_platform_skips_are_tolerated():
    """A Windows-only suite skipping on Linux must not fail the media job."""
    for suite, reason in PLATFORM_SKIPS:
        assert conftest._is_expected_skip(_Report(suite, reason)), (
            f"{suite} skips with {reason!r}, which the mandatory-media guard does "
            "not recognise — its Linux skip would fail the whole CI job. Add the "
            "reason to _EXPECTED_SKIP_REASONS."
        )


def test_tool_missing_skips_still_fail_the_job():
    """Loosening the guard must never turn a missing FFmpeg into a pass."""
    for suite, reason in TOOL_MISSING_SKIPS:
        assert not conftest._is_expected_skip(_Report(suite, reason)), (
            f"{suite} skips with {reason!r}; that is a missing dependency, not a "
            "platform fact, and must keep failing TSB_REQUIRE_FFMPEG=1"
        )


def test_every_skip_reason_in_the_suite_is_recognised():
    """Catch the real regression shape: a new reason nobody whitelisted.

    Scans the test sources for ``skipif(..., reason=...)`` strings and requires
    each to be either recognised as a platform fact or explicitly listed here as
    a reason that should fail. This is what would have caught the Ubuntu failure
    before pushing.
    """
    import re
    from pathlib import Path

    tests_dir = Path(__file__).resolve().parent
    reasons: set[str] = set()
    pattern = re.compile(r'skipif\([^)]*reason="([^"]+)"', re.S)
    for path in tests_dir.glob("test_*.py"):
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            reasons.add(match.group(1))

    assert reasons, "no skipif reasons found — the scan itself is broken"

    # Reasons that are deliberately NOT platform facts (would be a real problem).
    must_fail = set()
    unrecognised = {
        r for r in reasons
        if not conftest._is_expected_skip(_Report("x", r)) and r not in must_fail
    }
    assert not unrecognised, (
        "these skipif reasons are neither whitelisted as platform facts nor "
        f"declared as must-fail: {sorted(unrecognised)}. A suite carrying one of "
        "them will fail the mandatory-media job on the platform where it skips."
    )
