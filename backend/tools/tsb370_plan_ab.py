"""TSB-370-D driver: real model series planning + offline A/B on the same input.

Runs the offline baselines and then the real model planner through the production
API, on the same isolated project, same candidates, same theme and target. The
ledger charges the model call; nothing here bypasses validation.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, ".")

import tools.tsb370_drive as drv  # noqa: E402

RUN = Path(os.environ["LOCALAPPDATA"]) / "Temp" / "tsb370-run"
THEME = "从出发到抵达：城市街景与湖畔的过渡"
TARGET = 60.0


def _preview(body: dict) -> dict:
    pid = drv._project()
    with drv._client() as c:
        r = c.post(f"/api/projects/{pid}/director/plans/preview", json=body)
    return {"status": r.status_code, "body": r.json()}


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    pid = drv._project()

    # The digest must come from the same source the planner uses.
    with drv._client() as c:
        st = c.get(f"/api/projects/{pid}/director/plans/input-status"
                   "?max_candidates=12").json()
    digest = st["input_digest"]
    print(f"input digest: {digest[:16]}  segments={st['segment_count']} "
          f"with_reading={st['segments_with_any_valid_reading']} "
          f"channels={st['evidence_channels']}")

    out: dict = {"input_digest": digest, "status": st}

    if mode in ("all", "offline"):
        for style in ("chronological", "highlights_first", "atmospheric"):
            res = _preview({
                "expected_input_digest": digest, "theme": THEME,
                "target_duration_sec": TARGET, "style": style,
                "variant_count": 1, "max_candidates": 40,
                "planning_mode": "offline_rules",
            })
            body = res["body"]
            summary = {
                "status": res["status"],
                "mode": body.get("planning_mode"),
                "variants": len(body.get("variants") or []),
                "shots": sum(len(v["shots"]) for v in (body.get("variants") or [])),
                "total_sec": sum(v["total_duration_sec"] for v in
                                 (body.get("variants") or [])),
                "claims": sum(len(s["claims"]) for v in (body.get("variants") or [])
                              for s in v["shots"]),
            }
            out[f"offline_{style}"] = summary
            print(f"offline/{style:16s} status={summary['status']} "
                  f"shots={summary['shots']} claims={summary['claims']}")

    if mode in ("all", "model"):
        res = _preview({
            "expected_input_digest": digest, "theme": THEME,
            "target_duration_sec": TARGET, "variant_count": 1,
            "max_candidates": 12, "planning_mode": "model",
            "upload_acknowledged": True,
        })
        body = res["body"]
        out["model"] = {"status": res["status"],
                        "detail": body.get("detail") if res["status"] != 200 else None}
        if res["status"] == 200:
            out["model"].update({
                "mode": body.get("planning_mode"),
                "variants": len(body.get("variants") or []),
                "shots": sum(len(v["shots"]) for v in body["variants"]),
                "claims": sum(len(s["claims"]) for v in body["variants"]
                              for s in v["shots"]),
                "observed_claims": sum(
                    1 for v in body["variants"] for s in v["shots"]
                    for c in s["claims"] if c["kind"] == "observed_signal"),
                "request_summary": body.get("request_summary"),
                "warnings": body.get("warnings"),
            })
            print(f"model            status=200 variants={out['model']['variants']} "
                  f"shots={out['model']['shots']} "
                  f"observed_claims={out['model']['observed_claims']}")
            (RUN / "model_plan.json").write_text(
                json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")
        else:
            print(f"model            status={res['status']} detail={body.get('detail')}")

    (RUN / "ab_result.json").write_text(json.dumps(out, indent=2, ensure_ascii=False),
                                        encoding="utf-8")
    print("\nsaved ab_result.json")


if __name__ == "__main__":
    main()
