"""Turn a pytest junit XML into GitHub Actions annotations.

Why this exists
---------------
A red CI job whose failing test id only lives inside the job log is not
evidence: reading that log needs an authenticated GitHub session, so the run
page (and anyone reviewing a release record) sees nothing but
"Process completed with exit code 1". TSB-361 required proof that two Windows
failures were gone, and one CI run failed exactly that way — 6/7 with no way
to say which test broke. This script closes that gap: every ``failure`` /
``error`` becomes an ``::error`` annotation carrying the node id and the first
line of the assertion, every ``skip`` becomes a ``::warning`` with its reason,
and the same text is appended to ``GITHUB_STEP_SUMMARY``.

It reports only. The pytest step still owns the job's exit status, so this
script never fails a run and never hides a failure.

Usage
-----
``python scripts/ci_annotate.py backend-results.xml --label "backend (py3.11)"``

Unknown or missing files are not an error: when pytest dies before reporting
(collection error, interpreter crash) there is no XML, and the job's own exit
code already says so.
"""

from __future__ import annotations

import argparse
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

#: GitHub truncates a single annotation message; stay well inside it.
MAX_MESSAGE = 900


def _suite(root: ET.Element) -> ET.Element:
    """Return the element that carries the tests/failures counters."""
    if root.tag == "testsuite":
        return root
    inner = root.find("testsuite")
    return inner if inner is not None else root


def _first_line(text: str | None) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def collect(path: Path) -> tuple[dict[str, int], list[tuple[str, str, str]], list[tuple[str, str]]]:
    """Read *path* and return (counters, failures, skips)."""
    root = ET.parse(path).getroot()
    suite = _suite(root)
    counters = {
        key: int(suite.get(key, 0) or 0) for key in ("tests", "failures", "errors", "skipped")
    }
    failures: list[tuple[str, str, str]] = []
    skips: list[tuple[str, str]] = []
    for case in root.iter("testcase"):
        nodeid = f"{case.get('classname', '?')}::{case.get('name', '?')}"
        for tag in ("failure", "error"):
            node = case.find(tag)
            if node is not None:
                failures.append((tag, nodeid, _first_line(node.get("message") or node.text)))
        skip = case.find("skipped")
        if skip is not None:
            skips.append((nodeid, _first_line(skip.get("message") or skip.text)))
    return counters, failures, skips


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Annotate a pytest junit XML for GitHub Actions")
    parser.add_argument("xml", help="path to the junit XML produced by pytest")
    parser.add_argument("--label", default="pytest", help="job label used in the summary heading")
    args = parser.parse_args(argv)

    path = Path(args.xml)
    if not path.exists():
        print(f"{args.label}: no junit xml at {path}; pytest did not reach reporting")
        return 0

    counters, failures, skips = collect(path)
    passed = (
        counters["tests"] - counters["failures"] - counters["errors"] - counters["skipped"]
    )
    summary = (
        f"{args.label}: collected={counters['tests']} passed={passed} "
        f"failures={counters['failures']} errors={counters['errors']} "
        f"skipped={counters['skipped']}"
    )
    print(summary)

    for tag, nodeid, message in failures:
        print(f"::error title={tag}::{nodeid} {message}"[:MAX_MESSAGE])
    for nodeid, message in skips:
        print(f"::warning title=skipped::{nodeid} {message}"[:MAX_MESSAGE])

    lines = [summary]
    lines += [f"{tag.upper()}: {nodeid} {message}" for tag, nodeid, message in failures]
    lines += [f"SKIP: {nodeid} | {message}" for nodeid, message in skips]
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(f"### {args.label}\n\n```\n" + "\n".join(lines) + "\n```\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess in tests
    sys.exit(main())
