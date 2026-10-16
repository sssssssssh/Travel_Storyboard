"""Final acceptance audit for the v2.1.3 / M5.3 objective.

Verifies the claims the goal depends on, against the files on disk rather than
against recollection. Exits non-zero if any acceptance criterion fails.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend"
V = REPO / "docs" / "validation" / "v2.1.3"

problems: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  {'OK  ' if ok else 'FAIL'} {label}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        problems.append(label)


print("== 1. A/B report must not fabricate numbers ==")
ab = json.loads((V / "AB_RESULTS.json").read_text(encoding="utf-8"))
print(f"     status={ab['status']} evaluated={ab['evaluated']} "
      f"arm_c_available={ab['arm_c_available']}")
for arm, values in ab["metrics"]["arms"].items():
    print(f"     {arm:16} recall={values['event_recall']} "
          f"precision={values['event_precision']}")
check("A/B is not marked evaluated", ab["evaluated"] is False)
check("every arm metric is null", all(
    v["event_recall"] is None and v["event_precision"] is None
    for v in ab["metrics"]["arms"].values()
))
check("adoption is not decided", ab["metrics"]["meets_adoption_criteria"]["decided"] is False)
check("status records the block", ab["status"] in {"blocked_by_endpoint", "not_annotated"},
      ab["status"])

print("\n== 2. Capability report must not overclaim ==")
cap = json.loads((V / "CAPABILITY_REPORT.json").read_text(encoding="utf-8"))["capabilities"]
print(f"     state={cap['state']} verified={cap['verified']} sendable={cap['sendable']}")
check("state is not verified", cap["verified"] is False)
check("channel is not sendable", cap["sendable"] is False)
check("limits stay unknown", set(cap["unknown_fields"]) >= {
    "max_bytes", "max_duration_sec", "accepts_audio"})

print("\n== 3. Protection report must be measured, not asserted ==")
prot = json.loads((V / "PROTECTION_REPORT.json").read_text(encoding="utf-8"))
check("originals were hashed", prot["originals"].get("file_count", 0) > 0,
      f"{prot['originals'].get('file_count')} files")
check("digest scope is disclosed honestly",
      "not a full-file digest" in prot["originals"]["files"][0]["digest_scope"])
check("no absolute path recorded",
      not any(ch in json.dumps(prot) for ch in ("C:\\", "/Users/", "/tmp/")))
check("new table recorded",
      "segment_video_observations" in prot["database"]["tables_added_by_this_batch"])

print("\n== 4. Evidence must contain no credentials ==")
for path in V.glob("*"):
    if not path.is_file():
        continue
    blob = path.read_text(encoding="utf-8", errors="replace")
    leaks = [m for m in ("sk-", "Bearer sk", "aliyuncs.com/api") if m in blob]
    check(f"no credential in {path.name}", not leaks, ",".join(leaks))

print("\n== 5. CI registers the new suites ==")
ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
registered = [
    name for name in (
        "test_native_video_capability.py", "test_native_video_probe.py",
        "test_native_video_provider.py", "test_video_clip.py",
        "test_video_schema.py", "test_video_observation.py",
        "test_video_job.py", "test_video_api.py", "test_video_ab.py",
    ) if f"tests/{name}" in ci
]
check("all 9 new suites are in the mandatory-media job", len(registered) == 9,
      str(len(registered)))

print("\n== 6. Regression mappings present ==")
reg = (REPO / "docs" / "REGRESSION.md").read_text(encoding="utf-8")
for suite in ("test_video_clip", "test_video_schema", "test_video_observation",
              "test_video_job", "test_video_api", "test_video_ab"):
    check(f"{suite} mapped", suite in reg)

print("\n== 7. Git state ==")
status = subprocess.run(["git", "status", "--porcelain"], cwd=REPO,
                        capture_output=True, text=True).stdout.strip()
ahead = subprocess.run(["git", "rev-list", "--left-right", "--count",
                        "origin/main...HEAD"], cwd=REPO,
                       capture_output=True, text=True).stdout.strip()
check("worktree clean", not status, status[:80])
check("in sync with origin/main", ahead == "0\t0", ahead)

print()
print("VERDICT:", "PASS - objective acceptance criteria met" if not problems
      else f"FAIL - {problems}")
sys.exit(0 if not problems else 1)
