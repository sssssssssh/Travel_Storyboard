"""TSB-370 driver: real multiframe motion analysis on the isolated DB.

Selects analyse-able shots that already have frames, asks the production motion
endpoint, and reports exactly what the model said. Nothing here bypasses the
authorisation or budget gates.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")

import tools.tsb370_drive as drv  # noqa: E402

RUN = Path(os.environ["LOCALAPPDATA"]) / "Temp" / "tsb370-run"


def motion_targets(limit: int) -> list[str]:
    """Shots worth motion analysis: have frames, not yet reviewed."""
    con = sqlite3.connect(RUN / "travel_storyboard.db")
    try:
        pid = drv._project()
        done = {r[0] for r in con.execute(
            "SELECT DISTINCT segment_id FROM segment_motion_reviews WHERE project_id=?",
            (pid,))}
        rows = con.execute(
            """SELECT s.id,
                      (SELECT COUNT(*) FROM frames f
                        WHERE f.asset_id = s.asset_id
                          AND f.segment_id = s.id) AS nframes
               FROM segments s
               WHERE s.project_id=? AND s.status='candidate'
                 AND s.user_rating != 'exclude'
               ORDER BY nframes DESC, s.start_sec""", (pid,)).fetchall()
    finally:
        con.close()
    out = [r[0] for r in rows if r[0] not in done and r[1] >= 2]
    return out[:limit]


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    segs = motion_targets(n)
    pid = drv._project()
    print(f"motion targets: {len(segs)}")
    if not segs:
        print("no segment has >=2 frames; motion analysis cannot run")
        raise SystemExit(1)

    with drv._client() as c:
        r = c.post(f"/api/projects/{pid}/jobs/motion_review",
                   json={"segment_ids": segs, "frame_count": 3,
                         "limit": n, "upload_acknowledged": True})
    print("enqueue:", r.status_code)
    print("body:", json.dumps(r.json(), ensure_ascii=False)[:300])
    if r.status_code not in (200, 202):
        raise SystemExit(1)

    job_id = r.json()["id"]
    deadline = time.time() + 1200
    j: dict = {}
    while time.time() < deadline:
        with drv._client() as c:
            j = c.get(f"/api/jobs/{job_id}").json()
        if j.get("status") in ("succeeded", "failed", "partial_failed", "cancelled"):
            break
        time.sleep(4)
    print("\n=== motion job ===")
    print(json.dumps({k: j.get(k) for k in ("status", "stats", "failures", "error")},
                     indent=2, ensure_ascii=False)[:1800])


if __name__ == "__main__":
    main()
