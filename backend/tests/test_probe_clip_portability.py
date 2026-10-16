"""TSB-341 — the capability probe must run on any FFmpeg the project accepts.

The defect
----------
``tools/probe_video_capability.py`` built its synthetic clip with a ``drawtext``
filter::

    drawtext=text='%{eif\\:n\\:d}':x=10:y=10:fontsize=48:fontcolor=white

``drawtext`` is not part of a minimal FFmpeg build -- it needs a compile-time
freetype dependency. Measured on this Mac (Homebrew ffmpeg 9.0.2)::

    [AVFilterGraph] No such filter: 'drawtext'
    Error opening output files: Filter not found

The consequence is out of proportion to the bug. This tool is the *only* way to
produce the capability record TSB-335 reads, so on a machine whose FFmpeg lacks
freetype the entire native-video closure is unreachable: the probe crashes with
an ``FFmpegError`` traceback before it can even say "unavailable", and the video
channel can never be enabled no matter how capable the endpoint is.

It also broke the test suite on such machines (12 ``test_video_clip.py``
failures) and, worse, it made the tool unusable *precisely* when an operator is
trying to find out whether their endpoint works.

What is required
----------------
A probe clip that still makes temporal order *checkable* using only filters that
are always present. The replacement uses colour phases -- the frame is a single
distinct colour in each third of the clip, in a fixed order -- and asks the model
for the order it saw. That is stronger evidence than a keyword scan: either the
model reports the actual sequence, or it does not.

Two rules are preserved:

* the clip still changes over time, so a server that decoded one frame cannot
  answer correctly;
* the probe still refuses to treat a bare 2xx as proof (TSB-335).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app.ffmpeg import locate_ffmpeg

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None and not Path(locate_ffmpeg()).exists(),
    reason="ffmpeg not available",
)


def _has_filter(name: str) -> bool:
    """Does the FFmpeg on this machine provide ``name``?"""
    try:
        out = subprocess.run(
            [str(locate_ffmpeg()), "-hide_banner", "-filters"],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except Exception:  # pragma: no cover - environment probe
        return False
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == name:
            return True
    return False


def test_ffmpeg_facts_are_what_the_fix_assumes():
    """Pin the environment fact this fix is built on, without requiring it."""
    # drawtext may or may not exist here; the point is that the probe must not
    # depend on it. This test merely records which world we are in.
    has_drawtext = _has_filter("drawtext")
    assert isinstance(has_drawtext, bool)


def test_the_probe_clip_builds_without_drawtext(tmp_path, monkeypatch):
    """The probe clip must be derived on a freetype-less FFmpeg.

    RED before the fix: ``build_synthetic_clip()`` raised
    ``FFmpegError: No such filter: 'drawtext'``.
    """
    from tools import probe_video_capability as probe

    clip = probe.build_synthetic_clip(duration_sec=3.0)
    assert clip.exists(), "the probe clip must be produced"
    assert clip.stat().st_size > 1000, "the probe clip must have real content"
    Path(clip).unlink(missing_ok=True)


def test_the_probe_clip_carries_a_checkable_colour_sequence(tmp_path):
    """Temporal order must be verifiable by sampling the clip itself.

    A clip that merely "moves" cannot distinguish a real video read from a
    single-frame or prompt-only guess. Sampling the middle of each third must
    yield distinct, ordered colours.
    """
    from tools import probe_video_capability as probe

    clip = probe.build_synthetic_clip(duration_sec=3.0)

    def _sample(at: float) -> tuple[int, int, int]:
        out = subprocess.run(
            [
                str(locate_ffmpeg()), "-hide_banner", "-loglevel", "error",
                "-ss", f"{at}", "-i", str(clip), "-frames:v", "1",
                "-vf", "scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
            ],
            capture_output=True, timeout=60, check=True,
        ).stdout
        assert len(out) >= 3, f"could not sample the clip at {at}s"
        return out[0], out[1], out[2]

    early, middle, late = _sample(0.4), _sample(1.5), _sample(2.6)
    assert early != middle != late, (
        f"the three phases must be visually distinct, saw {early}, {middle}, {late}"
    )

    # The expected order is what the probe tells the model to look for.
    assert probe.PROBE_PHASE_ORDER == ("red", "green", "blue"), (
        "the probe's declared phase order must match the clip it builds"
    )

    def _dominant(rgb: tuple[int, int, int]) -> str:
        names = ("red", "green", "blue")
        return names[max(range(3), key=lambda i: rgb[i])]

    assert _dominant(early) == "red"
    assert _dominant(middle) == "green"
    assert _dominant(late) == "blue"

    Path(clip).unlink(missing_ok=True)


def test_the_probe_asks_for_the_observed_order():
    """The prompt must ask for the order, so the answer can be checked."""
    from tools import probe_video_capability as probe

    prompt = probe.build_probe_prompt()
    lowered = prompt.lower()
    for colour in probe.PROBE_PHASE_ORDER:
        assert colour in lowered, f"the prompt must name the expected colour {colour}"
    assert "order" in lowered or "sequence" in lowered, (
        "the prompt must ask for the order, otherwise a keyword hit proves nothing"
    )


def test_a_correct_order_is_recognised_and_a_wrong_one_is_not():
    """Answer checking must be able to *fail* -- that is the whole point."""
    from tools import probe_video_capability as probe

    good, basis = probe.evaluate_probe_answer("The order is red, then green, then blue.")
    assert good is True, f"a correct order must be recognised (basis={basis})"

    bad, basis_bad = probe.evaluate_probe_answer("The order is blue, then green, then red.")
    assert bad is False, (
        "a wrong order must NOT count as temporal evidence; "
        f"otherwise the check is decorative (basis={basis_bad})"
    )

    unrelated, _ = probe.evaluate_probe_answer("I cannot view videos.")
    assert unrelated is False, "an unhelpful answer is not evidence"


def test_the_old_keyword_heuristic_could_not_fail_this_way():
    """Regression guard on the evidence standard.

    The previous ``_looks_temporal`` returned True for any answer containing the
    word "then" or "from" -- including a wrong order. The new evaluation must be
    able to reject a confident but incorrect answer.
    """
    from tools import probe_video_capability as probe

    wrong_but_fluent = "First the blue phase, then the green, then the red one."
    ok, basis = probe.evaluate_probe_answer(wrong_but_fluent)
    assert ok is False, (
        "a fluent string that names the phases in the wrong order must not pass; "
        f"basis={basis}"
    )
