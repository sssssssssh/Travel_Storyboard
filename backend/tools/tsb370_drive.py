"""TSB-370 driver: talk to the isolated local server with correct auth.

Run from ``backend/``; the server must already be listening on the isolated DB.

Usage:
  python tools/tsb370_drive.py preview <n>
  python tools/tsb370_drive.py analyze <n>
  python tools/tsb370_drive.py job <job_id>
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import httpx

RUN = Path(os.environ["LOCALAPPDATA"]) / "Temp" / "tsb370-run"
BASE = "http://127.0.0.1:9412"


def _token() -> str:
    return (RUN / "token.txt").read_text(encoding="utf-8").strip()


def _project() -> str:
    con = sqlite3.connect(RUN / "travel_storyboard.db")
    try:
        return con.execute("SELECT id FROM projects").fetchone()[0]
    finally:
        con.close()


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=BASE, timeout=300, trust_env=False,
        headers={"X-Access-Token": _token()},
    )


def unanalysed_segments(limit: int) -> list[str]:
    con = sqlite3.connect(RUN / "travel_storyboard.db")
    try:
        pid = _project()
        done = {r[0] for r in con.execute(
            "SELECT DISTINCT segment_id FROM segment_semantic_results "
            "WHERE project_id=?", (pid,))}
        rows = con.execute(
            """SELECT s.id FROM segments s JOIN assets a ON a.id=s.asset_id
               WHERE s.project_id=? AND s.status='candidate'
                 AND s.user_rating != 'exclude'
                 AND (s.end_sec - s.start_sec) BETWEEN 2 AND 12
               ORDER BY s.asset_id, s.start_sec""", (pid,)).fetchall()
    finally:
        con.close()
    out = [r[0] for r in rows if r[0] not in done]
    return out[:limit]


def cmd_preview(n: int) -> None:
    segs = unanalysed_segments(n)
    pid = _project()
    with _client() as c:
        r = c.post(f"/api/projects/{pid}/vision/preview",
                   json={"segment_ids": segs, "limit": n})
    print(json.dumps({"status": r.status_code, "body": r.json()}, indent=2,
                     ensure_ascii=False))
    if r.status_code == 200:
        (RUN / "selected_segments.json").write_text(
            json.dumps(segs, indent=2), encoding="utf-8")


def cmd_analyze(n: int) -> None:
    segs = unanalysed_segments(n)
    pid = _project()
    with _client() as c:
        r = c.post(f"/api/projects/{pid}/jobs/vision_analyze",
                   json={"segment_ids": segs, "limit": n, "force": False})
    print(json.dumps({"status": r.status_code, "body": r.json()}, indent=2,
                     ensure_ascii=False))
    if r.status_code == 202:
        job_id = r.json().get("id")
        print(f"JOB_ID={job_id}")
        (RUN / "last_job.txt").write_text(job_id or "", encoding="utf-8")


def cmd_job(job_id: str) -> None:
    pid = _project()
    deadline = time.time() + 600
    last = None
    while time.time() < deadline:
        with _client() as c:
            r = c.get(f"/api/jobs/{job_id}")
        if r.status_code != 200:
            print("job fetch failed", r.status_code, r.text[:200])
            return
        last = r.json()
        if last.get("status") in ("succeeded", "failed", "partial_failed",
                                  "cancelled"):
            break
        time.sleep(3)
    print(json.dumps(last, indent=2, ensure_ascii=False))
    with _client() as c:
        rr = c.get(f"/api/projects/{pid}/vision-results")
    print("VISION_RESULTS=", json.dumps(rr.json(), ensure_ascii=False)[:2000])


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    cmd = sys.argv[1]
    if cmd == "preview":
        cmd_preview(int(sys.argv[2]) if len(sys.argv) > 2 else 3)
    elif cmd == "analyze":
        cmd_analyze(int(sys.argv[2]) if len(sys.argv) > 2 else 3)
    elif cmd == "job":
        cmd_job(sys.argv[2])
    elif cmd == "segments":
        print(json.dumps(unanalysed_segments(int(sys.argv[2])), indent=2))
    else:
        print("unknown command", cmd)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
