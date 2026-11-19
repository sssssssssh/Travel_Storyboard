"""Run TSB-372 planning through its APIs against a private SQLite copy."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="M5-real-acceptance")
    parser.add_argument("--expected-candidates", type=int, default=61)
    parser.add_argument("--ledger", type=Path,
                        help="Existing attempt ledger. Supplying it allows one text call.")
    args = parser.parse_args()

    from app.config import get_settings

    settings = get_settings()
    if settings.database_url:
        parser.error("this runner requires the default local SQLite source")
    source = settings.data_path / "travel_storyboard.db"
    if not source.is_file():
        parser.error("source database not found")
    if args.ledger:
        from app.services.model_call_ledger import ModelCallLedger

        if not args.ledger.is_file():
            parser.error("existing ledger file required")
        slots = ModelCallLedger(args.ledger).remaining()
        if slots["per_channel"]["plan_text"]["left"] < 1:
            parser.error("no text attempts remain in the existing ledger")
        os.environ["TRAVELSB_MODEL_LEDGER"] = str(args.ledger.resolve())

    scratch = Path(tempfile.mkdtemp(prefix="tsb372-acceptance-"))
    db_file = scratch / source.name
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(db_file)) as dst:
            src.backup(dst)
        matches = src.execute("SELECT id FROM projects WHERE name = ?", (args.project,)).fetchall()
        if len(matches) != 1:
            parser.error("expected exactly one named project")
        pid = matches[0][0]

    os.environ["TRAVELSB_DATA_DIR"] = str(scratch)
    os.environ["TRAVELSB_MODEL_MAX_RETRIES"] = "0"
    get_settings.cache_clear()

    from fastapi.testclient import TestClient
    from app.database import init_db, session_scope
    from app.main import app
    from app.models import ModelCallRecord
    from app.security import TOKEN_HEADER, get_token
    from app.services.evaluation_ab import has_model_receipt
    from app.services.director_plan_schema import PlanDraft

    init_db()
    base = f"/api/projects/{pid}"
    # Do not run startup recovery or schedule any media jobs from the copy.
    client = TestClient(app, base_url="http://127.0.0.1",
                        headers={TOKEN_HEADER: get_token()})
    status = client.get(f"{base}/director/plans/input-status?max_candidates=100")
    status.raise_for_status()
    info = status.json()
    if info["segment_count"] != args.expected_candidates:
        raise SystemExit(f"candidate count differs: found {info['segment_count']}")
    body = {
        "max_candidates": 100, "expected_input_digest": info["input_digest"],
        "target_duration_sec": 60, "theme": "从出发到抵达：城市街景与湖畔的过渡",
        "variant_count": 1,
    }
    preview = f"{base}/director/plans/preview"
    response = client.post(preview, json=body)
    response.raise_for_status()
    offline = response.json()
    snapshot = offline["request_summary"]["input_snapshot"]
    evidence = {"library_segments": info["coverage"]["library_segments"],
                "input": snapshot, "evidence_channels": info["evidence_channels"],
                "offline_shots": len(offline["variants"][0]["shots"])}
    if args.ledger:
        response = client.post(preview, json={**body, "planning_mode": "model",
                                              "upload_acknowledged": True})
        evidence["model_status"] = response.status_code
        if response.is_success:
            model = response.json()
            summary = model["request_summary"]
            assert summary["input_snapshot"] == snapshot
            assert summary["sent_segment_ids"] == [c["segment_id"] for c in snapshot["candidates"]]
            with session_scope() as session:
                assert has_model_receipt(PlanDraft.model_validate(model), session=session)
                row = session.get(ModelCallRecord, summary["receipt"]["call_id"])
                evidence["model_receipt"] = {
                    "call_id": row.id, "result_digest": row.result_digest,
                    "request_bytes": row.request_bytes, "response_bytes": row.response_bytes,
                    "elapsed_ms": row.elapsed_ms, "model_id": row.model_id,
                    "response_digest": summary["receipt"]["response_digest"],
                    "prompt_version": summary["prompt_version"],
                    "prompt_digest": summary["prompt_digest"],
                }
            evidence["model_sent_segment_ids"] = summary["sent_segment_ids"]
            evidence["model_shots"] = len(model["variants"][0]["shots"])
            evidence["model_duration_sec"] = model["variants"][0]["total_duration_sec"]
            evidence["offline_duration_sec"] = offline["variants"][0]["total_duration_sec"]
            pair = client.post(f"{base}/evaluation/ab-sessions",
                               json={"offline_plan": offline, "model_plan": model})
            evidence["pair_status"] = pair.status_code
            if pair.is_success:
                blind = pair.json()
                assert blind["revealed"] is False and "mapping" not in blind
                evidence["human_judgement"] = "pending"
                (scratch / "blind-session.json").write_text(
                    json.dumps(blind, ensure_ascii=False, indent=2), encoding="utf-8")
            else:
                evidence["pair_error_code"] = pair.json().get("detail", {}).get("code")
        else:
            detail = response.json().get("detail", {})
            evidence["model_error_code"] = detail.get("code")
            evidence["validation_codes"] = [p.get("code") for p in detail.get("problems", [])]
    (scratch / "evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"evidence_dir": str(scratch), "candidates": snapshot["candidate_count"],
                      "evidence_channels": evidence["evidence_channels"],
                      "model_status": evidence.get("model_status"),
                      "pair_status": evidence.get("pair_status"),
                      "model_error_code": evidence.get("model_error_code")}))
    if args.ledger and evidence.get("pair_status") != 201:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
