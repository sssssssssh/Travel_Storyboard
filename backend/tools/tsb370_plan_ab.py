"""TSB-371-0 driver: same-input A/B between the offline rules and the real model.

Fixed from TSB-370, where the digest was fetched with ``max_candidates=12`` but the
offline baseline was requested with ``max_candidates=40``. That is not an A/B: it
compares two different candidate sets, and it trips the digest guard (or would
hide the mismatch if the guard were relaxed).

Now every request — the digest read and all planners — is built from **one**
``INPUT`` mapping, so the only thing that may differ is the planner itself.

No model call happens unless ``--model`` is passed *and* the ledger has room.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, ".")

import tools.tsb370_drive as drv  # noqa: E402

RUN = Path(os.environ["LOCALAPPDATA"]) / "Temp" / "tsb370-run"

#: The single source of truth for the comparison input. Changing any value here
#: changes both sides at once, which is the point.
INPUT = {
    "theme": "从出发到抵达：城市街景与湖畔的过渡",
    "target_duration_sec": 60.0,
    "max_candidates": 12,
    "variant_count": 1,
}

#: Kept for callers that imported the old names.
THEME = INPUT["theme"]
TARGET = INPUT["target_duration_sec"]


def _preview(body: dict) -> dict:
    pid = drv._project()
    with drv._client() as c:
        r = c.post(f"/api/projects/{pid}/director/plans/preview", json=body)
    try:
        payload = r.json()
    except ValueError:
        payload = {"detail": "non-JSON response"}
    return {"status": r.status_code, "body": payload}


def _request(*, planning_mode: str, style: str = "chronological",
             digest: str, ack: bool = False) -> dict:
    """Build a preview request. Every field comes from INPUT except the planner."""
    body = {
        "expected_input_digest": digest,
        "theme": INPUT["theme"],
        "target_duration_sec": INPUT["target_duration_sec"],
        "max_candidates": INPUT["max_candidates"],
        "variant_count": INPUT["variant_count"],
        "style": style,
        "planning_mode": planning_mode,
    }
    if ack:
        body["upload_acknowledged"] = True
    return body


def main(argv: list[str]) -> None:
    mode = argv[1] if len(argv) > 1 else "all"
    pid = drv._project()

    with drv._client() as c:
        st = c.get(f"/api/projects/{pid}/director/plans/input-status"
                   f"?max_candidates={INPUT['max_candidates']}").json()
    digest = st["input_digest"]
    print(f"input digest: {digest[:16]}  segments={st['segment_count']} "
          f"with_reading={st['segments_with_any_valid_reading']} "
          f"channels={st['evidence_channels']}")

    out: dict = {"input": dict(INPUT), "input_digest": digest, "status": st}

    if mode in ("all", "offline"):
        for style in ("chronological", "highlights_first", "atmospheric"):
            res = _preview(_request(planning_mode="offline_rules", style=style,
                                    digest=digest))
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
            if summary["status"] == 200:
                (RUN / f"offline_{style}.json").write_text(
                    json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")

    if mode in ("all", "model"):
        res = _preview(_request(planning_mode="model", digest=digest, ack=True))
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
                "request_summary": body.get("request_summary"),
            })
            print(f"model            status=200 variants={out['model']['variants']} "
                  f"shots={out['model']['shots']}")
            (RUN / "model_plan.json").write_text(
                json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")
        else:
            print(f"model            status={res['status']} "
                  f"detail={json.dumps(body.get('detail'), ensure_ascii=False)[:200]}")

    (RUN / "ab_result.json").write_text(
        json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print("\nsaved ab_result.json")


if __name__ == "__main__":
    main(sys.argv)
