"""Run exactly N real static-frame analyses through the production job, with the
ledger charging every HTTP attempt and zero retries enforced at the transport.

Nothing here is a shortcut around the app: it drives the same HTTP API a browser
would, and the ledger hook is installed on the production send seam.
"""
import json, os, sys, time
sys.path.insert(0, ".")
from pathlib import Path
import httpx

from app.config import get_settings
from app.services.model_call_ledger import ModelCallLedger, BudgetExhausted
from app.services.vision.providers import openai_compatible as oc
import tools.tsb370_drive as drv

RUN = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("TMPDIR")
           or os.environ.get("TEMP") or "/tmp") / "tsb370-run"
ledger = ModelCallLedger(RUN / "ledger.json")

N = int(sys.argv[1]) if len(sys.argv) > 1 else 3
segs = drv.unanalysed_segments(N)
pid = drv._project()
print(f"segments: {len(segs)}  project={pid[:8]}")
print("ledger before:", json.dumps(ledger.remaining()["per_channel"]["static"]))

# Every real POST must pass the ledger. This is the same seam the providers use.
def hook(size: int) -> None:
    ledger.charge("static", "batch", size, note="static analyze")

oc.set_ledger_hook(hook)
try:
    with drv._client() as c:
        # The user authorised this round explicitly. Consent is passed through the
        # production field (not by disabling the gate), so the same
        # authorize_upload() path runs and records it.
        r = c.post(f"/api/projects/{pid}/jobs/vision_analyze",
                   json={"segment_ids": segs, "limit": N, "force": False,
                         "upload_acknowledged": True})
    print("enqueue status:", r.status_code)
    print("body:", json.dumps(r.json(), ensure_ascii=False)[:400])
    if r.status_code != 202:
        raise SystemExit(1)
    job_id = r.json()["id"]

    deadline = time.time() + 900
    while time.time() < deadline:
        with drv._client() as c:
            j = c.get(f"/api/jobs/{job_id}").json()
        st = j.get("status")
        if st in ("succeeded", "failed", "partial_failed", "cancelled"):
            break
        time.sleep(4)
    print("\n=== Job 终态 ===")
    print(json.dumps({k: j.get(k) for k in
                      ("status", "job_type", "stats", "failures", "error")},
                     indent=2, ensure_ascii=False)[:1500])
finally:
    oc.set_ledger_hook(None)

print("\n=== 账本 ===")
print(json.dumps(ledger.summary(), indent=2, ensure_ascii=False))
