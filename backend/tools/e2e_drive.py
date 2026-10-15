"""Drive the full Travel Storyboard pipeline over the real REST API.

This is a *test harness*, not part of the product: it exists so an end-to-end
run against real footage is reproducible and produces machine-checkable
evidence (JSON) instead of a hand-typed claim.

It exercises the documented chain:

    create project -> scan -> detect -> package -> proxies -> roughcut

and enforces the two things that are easy to fake accidentally:

* **C1 (originals are read-only)** — every source file's size/mtime/SHA-256 is
  captured before and after; any change is a hard failure.
* **C6 (nothing fails silently)** — a job that ends `failed`, or that reports
  entries in its `failures` list, fails the run instead of passing quietly.

Usage::

    python tools/e2e_drive.py --source /path/to/footage --work /path/to/workdir

The workspace (``--work``) must NOT overlap the footage directory: the product
itself refuses overlapping output, and so does this script.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import httpx

# Job statuses that mean "stop waiting". Mirrors runner.TERMINAL_STATUSES.
# `succeeded` is the success value (NOT `completed`), and `partial_failed` is
# how a run with per-file failures ends — it must not be treated as success.
TERMINAL = {"succeeded", "partial_failed", "failed", "cancelled"}

# Stages that count as an acceptable outcome only if they are `succeeded`.
OK_STATUS = {"succeeded"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".mts", ".m2ts", ".mpg", ".mpeg", ".webm"}


def sha256(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def snapshot(root: Path) -> dict:
    """Record the observable identity of every file under *root*."""
    out = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name.startswith("."):
            continue
        st = path.stat()
        out[str(path.relative_to(root))] = {
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
            "sha256": sha256(path),
        }
    return out


def diff_snapshot(before: dict, after: dict) -> list[str]:
    problems = []
    for name, meta in before.items():
        now = after.get(name)
        if now is None:
            problems.append(f"DELETED: {name}")
            continue
        if now["sha256"] != meta["sha256"]:
            problems.append(f"CONTENT CHANGED: {name}")
        if now["size"] != meta["size"]:
            problems.append(f"SIZE CHANGED: {name} {meta['size']} -> {now['size']}")
        if now["mtime_ns"] != meta["mtime_ns"]:
            problems.append(f"MTIME CHANGED: {name}")
    for name in after:
        if name not in before:
            problems.append(f"CREATED INSIDE SOURCE: {name}")
    return problems


class Client:
    def __init__(self, base: str, token: str) -> None:
        self.http = httpx.Client(
            base_url=base,
            headers={"X-Access-Token": token},
            timeout=httpx.Timeout(60.0, read=600.0),
        )

    # -- jobs -------------------------------------------------------------
    def start(self, project_id: str, job_type: str, options: dict | None = None,
              force: bool = False) -> dict:
        r = self.http.post(
            f"/api/projects/{project_id}/jobs/{job_type}",
            json={"options": options or {}, "force": force},
        )
        if r.status_code != 202:
            raise SystemExit(f"start {job_type} -> HTTP {r.status_code}: {r.text}")
        return r.json()

    def wait(self, job_id: str, label: str, timeout: float = 3600.0) -> dict:
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            job = self.http.get(f"/api/jobs/{job_id}").json()
            status = job["status"]
            if status != last:
                stats = job.get("stats") or {}
                print(f"    [{label}] status={status} stats={json.dumps(stats, ensure_ascii=False)}")
                last = status
            if status in TERMINAL:
                return job
            time.sleep(1.0)
        raise SystemExit(f"job {label} ({job_id}) did not finish within {timeout}s")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, help="directory of real footage (read-only)")
    ap.add_argument("--work", required=True, help="workspace for outputs (must not overlap source)")
    ap.add_argument("--base", default="http://127.0.0.1:8737")
    ap.add_argument("--token-file", default=None)
    ap.add_argument("--name", default="E2E 真实素材验收")
    ap.add_argument("--evidence", default=None, help="write JSON evidence here")
    ap.add_argument("--limit", type=int, default=0, help="only scan the first N clips (0 = all)")
    ap.add_argument("--project", default=None,
                    help="reuse an existing project id and skip project creation")
    args = ap.parse_args()

    source = Path(os.path.expanduser(args.source)).resolve()
    work = Path(os.path.expanduser(args.work)).resolve()
    if not source.is_dir():
        raise SystemExit(f"source is not a directory: {source}")
    if source == work or source in work.parents or work in source.parents:
        raise SystemExit(f"source and work must not overlap: {source} / {work}")
    work.mkdir(parents=True, exist_ok=True)

    if args.token_file:
        token = Path(args.token_file).read_text(encoding="utf-8").strip()
    else:
        token = (Path("data") / "token.txt").read_text(encoding="utf-8").strip()

    clips = sorted(p for p in source.rglob("*")
                   if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES)
    if not clips:
        raise SystemExit(f"no video files under {source}")
    print(f"== footage ==")
    print(f"  source : {source}")
    print(f"  clips  : {len(clips)}")
    for p in clips[:args.limit or len(clips)]:
        print(f"    {p.stat().st_size / 1e6:8.1f} MB  {p.name}")

    print("\n== hashing originals (before) ==")
    before = snapshot(source)
    print(f"  {len(before)} files hashed, C1 baseline captured")

    api = Client(args.base, token)
    health = api.http.get("/api/system/health").json()
    print(f"\n== system ==\n  version={health.get('version')} ffmpeg={health.get('ffmpeg')} "
          f"ffprobe={health.get('ffprobe')}")
    if not health.get("ffmpeg") or not health.get("ffprobe"):
        raise SystemExit("ffmpeg/ffprobe missing per /api/system/health — refusing a silent-skip run (C6)")

    results: dict = {"source": str(source), "work": str(work), "clips": len(clips),
                     "health": health, "stages": []}

    # -- 1. project -------------------------------------------------------
    if args.project:
        pid = args.project
        print(f"\n== 1. reuse project {pid} ==")
    else:
        print("\n== 1. create project ==")
        r = api.http.post("/api/projects", json={
            "name": args.name,
            "source_roots": [str(source)],
            "output_dir": str(work),
        })
        if r.status_code != 201:
            raise SystemExit(f"create project -> HTTP {r.status_code}: {r.text}")
        project = r.json()
        pid = project["id"]
        print(f"  project {pid}  workspace={project.get('output_dir')}")

    only = {str(p) for p in clips[:args.limit]} if args.limit else None

    def get_items(path: str) -> list[dict]:
        """List endpoints return {items,total,offset,limit}; page through them."""
        items: list[dict] = []
        offset = 0
        while True:
            page = api.http.get(path, params={"offset": offset, "limit": 200}).json()
            batch = page.get("items", [])
            items.extend(batch)
            if len(items) >= page.get("total", 0) or not batch:
                return items
            offset += len(batch)

    # -- 2. scan ----------------------------------------------------------
    print("\n== 2. scan ==")
    job = api.start(pid, "scan")
    job = api.wait(job["id"], "scan")
    assets = get_items(f"/api/projects/{pid}/assets")
    if only:
        assets = [a for a in assets if a["source_path"] in only] or assets
    print(f"  indexed {len(assets)} assets")
    results["stages"].append({"stage": "scan", "status": job["status"],
                              "stats": job.get("stats"), "failures": job.get("failures"),
                              "assets": len(assets)})

    # -- 3. detect --------------------------------------------------------
    print("\n== 3. detect ==")
    job = api.start(pid, "detect")
    job = api.wait(job["id"], "detect")
    segs = get_items(f"/api/projects/{pid}/segments")
    if only:
        asset_ids = {a["id"] for a in assets}
        segs = [s for s in segs if s["asset_id"] in asset_ids] or segs
    print(f"  detected {len(segs)} segments")
    results["stages"].append({"stage": "detect", "status": job["status"],
                              "stats": job.get("stats"), "failures": job.get("failures"),
                              "segments": len(segs)})

    # -- 4. review package ------------------------------------------------
    print("\n== 4. review package ==")
    job = api.start(pid, "package")
    job = api.wait(job["id"], "package")
    pkgs = api.http.get(f"/api/projects/{pid}/packages").json()
    print(f"  {len(pkgs)} package(s)")
    results["stages"].append({"stage": "package", "status": job["status"],
                              "stats": job.get("stats"), "failures": job.get("failures"),
                              "packages": len(pkgs)})

    # -- 5. proxies -------------------------------------------------------
    print("\n== 5. proxies ==")
    job = api.start(pid, "proxies")
    job = api.wait(job["id"], "proxies")
    results["stages"].append({"stage": "proxies", "status": job["status"],
                              "stats": job.get("stats"), "failures": job.get("failures")})
    print(f"  proxies {json.dumps(job.get('stats') or {}, ensure_ascii=False)}")

    # -- 6. edit plan + roughcut -----------------------------------------
    # roughcut reads EditPlanShot rows, so a plan must exist first or the job
    # legitimately produces nothing. Take the best-rated segments as shots.
    print("\n== 6. edit plan + roughcut ==")
    selected = [s for s in segs if s.get("user_rating") in ("like", "favorite")] or segs
    selected = selected[:12]
    if not selected:
        raise SystemExit("no segments to plan from — detect produced nothing")
    shots = [{
        "asset_id": s["asset_id"],
        "segment_id": s["id"],
        "source_in_sec": s["start_sec"],
        "source_out_sec": s["end_sec"],
        "target_duration_sec": min(s["duration_sec"], 6.0),
        "story_role": "highlight",
        "review_status": "approved",
    } for s in selected]
    r = api.http.post(f"/api/projects/{pid}/edit-plan", json={"shots": shots})
    if r.status_code not in (200, 201):
        raise SystemExit(f"edit-plan -> HTTP {r.status_code}: {r.text}")
    plan = r.json()
    print(f"  plan built with {len(plan.get('shots', shots))} shot(s)")

    job = api.start(pid, "roughcut")
    job = api.wait(job["id"], "roughcut", timeout=1800)
    results["stages"].append({"stage": "roughcut", "status": job["status"],
                              "stats": job.get("stats"), "failures": job.get("failures")})
    print(f"  roughcut {json.dumps(job.get('stats') or {}, ensure_ascii=False)}")

    # Confirm the rough cut is actually downloadable (a file that "exists" but
    # cannot be served is exactly the 404-becoming-500 trap in AGENTS.md §5.1).
    rc = api.http.get(f"/api/projects/{pid}/roughcut")
    results["roughcut_download"] = {"status": rc.status_code, "bytes": len(rc.content)}
    print(f"  roughcut download -> HTTP {rc.status_code}, {len(rc.content)} bytes")

    # -- 7. C1 verification ----------------------------------------------
    print("\n== 7. verify originals unchanged (C1) ==")
    after = snapshot(source)
    problems = diff_snapshot(before, after)
    if problems:
        print("  FAIL — originals were modified:")
        for p in problems:
            print(f"    {p}")
    else:
        print(f"  OK — {len(before)} files byte-identical, no additions/deletions")
    results["c1_originals_unchanged"] = not problems
    results["c1_problems"] = problems

    # -- 8. honest verdict (C6/C8) ---------------------------------------
    failed_stages = [s for s in results["stages"]
                     if s["status"] not in OK_STATUS or (s.get("failures") or {}).get("count")]
    results["failed_stages"] = [s["stage"] for s in failed_stages]
    verdict = "PASS" if (not problems and not failed_stages) else "FAIL"
    results["verdict"] = verdict

    print(f"\n{'=' * 60}\nVERDICT: {verdict}")
    if failed_stages:
        for s in failed_stages:
            print(f"  stage {s['stage']}: status={s['status']} failures={s.get('failures')}")
    print("=" * 60)

    if args.evidence:
        Path(args.evidence).parent.mkdir(parents=True, exist_ok=True)
        Path(args.evidence).write_text(
            json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"evidence written to {args.evidence}")

    print(f"\nWeb UI: {args.base}/?token=<see {args.token_file or 'backend/data/token.txt'}>")
    print(f"project id: {pid}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
