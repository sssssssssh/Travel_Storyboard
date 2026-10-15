"""Shared helper for tests that need a real FFmpeg toolchain.

Why this exists (TSB-311): the regular ``backend`` CI job ran the whole suite
without installing FFmpeg, so any test whose *fixture* built a real video errored
with ``Executable not found: ffmpeg`` instead of being reported as skipped. That
made a missing media toolchain look like a product failure.

The rules this helper encodes:

* a test that genuinely needs FFmpeg calls :func:`require_ffmpeg`, which **skips**
  when the toolchain is absent — unless ``TSB_REQUIRE_FFMPEG=1`` is set, in which
  case the absence is a hard failure (the mandatory-media job must never pass by
  silently skipping);
* skip reasons use the canonical wording in :data:`SKIP_REASON`, because
  ``conftest._EXPECTED_SKIP_REASONS`` matches those strings to distinguish
  "platform fact" from "a tool went missing".

Nothing here may be used to hide a real assertion: the helpers only decide
*whether the test can run at all*.
"""

from __future__ import annotations

import os

import pytest

from app.ffmpeg import ffmpeg_available, ffprobe_available

#: Canonical skip reason. Kept in one place so ``conftest`` can match it and so
#: a rename cannot silently turn a legitimate skip into a CI failure.
SKIP_REASON = "ffmpeg/ffprobe not available"


def have_ffmpeg() -> bool:
    """True when a *usable* ffmpeg **and** ffprobe are present.

    ``locate_ffmpeg()`` falls back to the bare string ``"ffmpeg"`` so callers can
    build a command line before probing; that is not proof the binary exists, so
    availability is decided by the same probe the health endpoint uses.
    """
    return bool(ffmpeg_available()) and bool(ffprobe_available())


def require_ffmpeg() -> str:
    """Return a usable ffmpeg path, or skip/fail depending on the CI mode."""
    exe = ffmpeg_available()
    if not exe or not ffprobe_available():
        if os.environ.get("TSB_REQUIRE_FFMPEG") == "1":
            pytest.fail("mandatory FFmpeg integration requires ffmpeg AND ffprobe")
        pytest.skip(SKIP_REASON)
    return exe


#: Marks a test as requiring a real media toolchain. Applied at module level via
#: ``pytestmark`` in suites that are *entirely* media-dependent; per-test use is
#: preferred when a file mixes pure logic with real encoding (TSB-311 requires
#: that split so future cases cannot drift into a tool-less runner by accident).
needs_ffmpeg = pytest.mark.needs_ffmpeg
