"""TSB-361 — the junit→annotation reporter that makes a red CI job readable.

``scripts/ci_annotate.py`` exists because a failing job whose test id lives only
in the (login-only) job log cannot serve as release evidence. These tests pin
the three things that would silently regress:

* a failure must produce an ``::error`` naming the node id **and** the first
  assertion line — a bare "job failed" is what we were trying to get rid of;
* a skip must keep its reason, so a silently skipped media test stays visible;
* both the backend and the mandatory-media job must actually call it, and each
  must point at its own junit file.

The reporter is not allowed to fail a run: exit status belongs to pytest.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "ci_annotate.py"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _module():
    spec = importlib.util.spec_from_file_location("ci_annotate", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _junit(tmp_path: Path, body: str, name: str = "results.xml") -> Path:
    path = tmp_path / name
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<testsuites>\n'
        '<testsuite name="pytest" tests="3" failures="1" errors="0" skipped="1">\n'
        f"{body}"
        "</testsuite>\n</testsuites>\n",
        encoding="utf-8",
    )
    return path


PASSING = '<testcase classname="tests.test_x" name="test_ok" time="0.01" />\n'
FAILING = (
    '<testcase classname="tests.test_x" name="test_bad" time="0.02">\n'
    '  <failure message="assert 2 == 1&#10;assert [1, 1] == [1]">assert 2 == 1</failure>\n'
    "</testcase>\n"
)
SKIPPED = (
    '<testcase classname="tests.test_y" name="test_link" time="0.00">\n'
    '  <skipped message="link creation is a platform fact" />\n'
    "</testcase>\n"
)


def test_a_failure_becomes_an_error_annotation_with_the_first_assertion_line(tmp_path, capsys):
    path = _junit(tmp_path, PASSING + FAILING + SKIPPED)

    assert _module().main([str(path), "--label", "backend (py3.11)"]) == 0

    out = capsys.readouterr().out
    line = next(text for text in out.splitlines() if text.startswith("::error"))
    assert "tests.test_x::test_bad" in line, line
    assert "assert 2 == 1" in line, line
    assert "[1, 1]" not in line, "only the first line is quoted; a full traceback is not an annotation"


def test_a_skip_keeps_its_reason_so_a_silent_skip_stays_visible(tmp_path, capsys):
    path = _junit(tmp_path, PASSING + FAILING + SKIPPED)

    _module().main([str(path), "--label", "media (windows-latest)"])

    out = capsys.readouterr().out
    line = next(text for text in out.splitlines() if text.startswith("::warning"))
    assert "tests.test_y::test_link" in line, line
    assert "link creation is a platform fact" in line, line


def test_the_counters_line_reports_what_junit_recorded(tmp_path, capsys):
    path = _junit(tmp_path, PASSING + FAILING + SKIPPED)

    _module().main([str(path), "--label", "backend (py3.13)"])

    out = capsys.readouterr().out
    assert "collected=3 passed=1 failures=1 errors=0 skipped=1" in out, out


def test_a_missing_junit_file_is_not_a_failure(tmp_path, capsys):
    """A collection error means no XML; the pytest step already failed the job."""
    missing = tmp_path / "never-written.xml"

    assert _module().main([str(missing), "--label", "media (ubuntu-latest)"]) == 0
    assert "did not reach reporting" in capsys.readouterr().out


def test_the_job_summary_carries_the_same_lines(tmp_path, monkeypatch, capsys):
    path = _junit(tmp_path, PASSING + FAILING + SKIPPED)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    _module().main([str(path), "--label", "media (windows-latest)"])

    capsys.readouterr()
    written = summary.read_text(encoding="utf-8")
    assert "### media (windows-latest)" in written
    assert "FAILURE: tests.test_x::test_bad assert 2 == 1" in written
    assert "SKIP: tests.test_y::test_link | link creation is a platform fact" in written


def test_a_long_assertion_is_truncated_to_the_annotation_limit(tmp_path, capsys):
    long_message = "x" * 4000
    body = (
        '<testcase classname="tests.test_x" name="test_huge" time="0.1">\n'
        f'  <failure message="{long_message}">traceback</failure>\n'
        "</testcase>\n"
    )
    path = _junit(tmp_path, body, name="huge.xml")

    _module().main([str(path), "--label", "backend (py3.11)"])

    line = next(
        text for text in capsys.readouterr().out.splitlines() if text.startswith("::error")
    )
    assert len(line) <= _module().MAX_MESSAGE


@pytest.mark.parametrize(
    "job, junit",
    [("backend", "backend-results.xml"), ("media", "media-results.xml")],
)
def test_both_ci_jobs_call_the_reporter_with_their_own_junit_file(job, junit):
    """Pinning the wiring: a job that stops annotating goes back to being opaque."""
    text = WORKFLOW.read_text(encoding="utf-8")
    start = text.index(f"\n  {job}:\n")
    tail = text[start + 1 :]
    following = [
        tail.index(f"\n  {other}:\n")
        for other in ("backend", "media", "frontend")
        if f"\n  {other}:\n" in tail
    ]
    block = tail[: min(following)] if following else tail
    lines = block.splitlines()

    call = next(
        (index for index, line in enumerate(lines) if "python ../scripts/ci_annotate.py" in line),
        None,
    )
    assert call is not None, f"{job} no longer runs scripts/ci_annotate.py: a red job would be unexplained"
    call_text = " ".join(lines[call : call + 2])
    assert junit in call_text, call_text
    assert any(
        "if: always()" in line for line in lines[max(0, call - 4) : call]
    ), "the reporter must run even when pytest failed (if: always())"
