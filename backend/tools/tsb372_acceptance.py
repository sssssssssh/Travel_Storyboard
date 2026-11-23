"""Run TSB-372 planning through its APIs against a private SQLite copy."""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit


def _write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _pair(client, base, scratch, evidence, offline, model):
    pair = client.post(f"{base}/evaluation/ab-sessions",
                       json={"offline_plan": offline, "model_plan": model})
    evidence["pair_status"] = pair.status_code
    if pair.is_success:
        blind = pair.json()
        assert blind["revealed"] is False and "mapping" not in blind
        evidence["human_judgement"] = "pending"
        _write_json(scratch / "blind-session.json", blind)
    else:
        detail = pair.json().get("detail")
        evidence["pair_error_code"] = detail.get("code") if isinstance(detail, dict) else None


def _safe_outbound_text(prompt, entries):
    """Stop the real run if the evidence projection appears to contain private data."""
    if any(entry.human.note.strip() and entry.human.note.strip() in prompt for entry in entries):
        raise SystemExit("outbound check found private note text")
    if re.search(r"(?i)(?:\b[a-z]:[\\/]|\\\\[\w.-]+\\|/(?:home|users|mnt|volumes)/|"
                 r"\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b|(?<!\d)1[3-9]\d{9}(?!\d))", prompt):
        raise SystemExit("outbound check found an absolute path or contact detail")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="M5-real-acceptance")
    parser.add_argument("--expected-candidates", type=int, default=61)
    parser.add_argument("--ledger", type=Path,
                        help="Existing attempt ledger. Supplying it allows one text call.")
    phase = parser.add_mutually_exclusive_group()
    phase.add_argument("--defer-ab", action="store_true",
                       help="Save the validated pair inputs locally for later pairing.")
    phase.add_argument("--pair-from", type=Path,
                       help="Create a blind review from an earlier isolated run, without a model call.")
    args = parser.parse_args()

    from app.config import get_settings

    settings = get_settings()
    if settings.database_url:
        parser.error("this runner requires the default local SQLite source")
    source = (settings.data_path / "travel_storyboard.db").resolve()
    if not source.is_file():
        parser.error("source database not found")
    if args.pair_from and args.ledger:
        parser.error("pairing does not use a model ledger or send a model request")
    if args.ledger:
        from app.services.model_call_ledger import ModelCallLedger

        endpoint = urlsplit(settings.model_base_url)
        if (endpoint.scheme != "https" or not (endpoint.hostname or "").endswith(".pai-eas.aliyuncs.com")
                or not settings.model_name or not settings.model_api_key or settings.model_mode != "on"):
            parser.error("active HTTPS PAI-EAS endpoint and model credentials required")
        if not args.ledger.is_file():
            parser.error("existing ledger file required")
        slots = ModelCallLedger(args.ledger).remaining()
        if slots["per_channel"]["plan_text"]["left"] < 1:
            parser.error("no text attempts remain in the existing ledger")
        os.environ["TRAVELSB_MODEL_LEDGER"] = str(args.ledger.resolve())

    if args.pair_from:
        scratch = args.pair_from.resolve(strict=True)
        if (scratch / source.name).resolve() == source or not (scratch / "pair-inputs.json").is_file():
            parser.error("pairing requires an earlier isolated acceptance run")
    else:
        scratch = Path(tempfile.mkdtemp(prefix="tsb372-acceptance-"))
    db_file = scratch / source.name
    with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as src:
        matches = src.execute("SELECT id FROM projects WHERE name = ?", (args.project,)).fetchall()
        if len(matches) != 1:
            parser.error("expected exactly one named project")
        pid = matches[0][0]
        if not args.pair_from:
            with closing(sqlite3.connect(db_file)) as dst:
                src.backup(dst)
                # Derived review clips must use the independent acceptance workspace.
                dst.execute("UPDATE projects SET output_dir = '' WHERE id = ?", (pid,))
                dst.commit()

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
    if args.pair_from:
        inputs = json.loads((scratch / "pair-inputs.json").read_text(encoding="utf-8"))
        evidence_path = scratch / "evidence.json"
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        if evidence.get("pair_status") == 201:
            parser.error("this isolated run already has a blind session")
        _pair(client, base, scratch, evidence, inputs["offline"], inputs["model"])
        _write_json(evidence_path, evidence)
        print(json.dumps({"evidence_dir": str(scratch), "pair_status": evidence["pair_status"]}))
        return 0 if evidence["pair_status"] == 201 else 1

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
                "offline_shots": len(offline["variants"][0]["shots"]),
                "offline_duration_sec": offline["variants"][0]["total_duration_sec"]}
    from app.services.director_plan_input import build_planning_input
    from app.services.director_plan_model import build_evidence_text
    from app.services.director_plan_schema import PlanningBrief

    pi = build_planning_input(pid, max_candidates=100)
    prompt, planned = build_evidence_text(pi, PlanningBrief(
        theme=body["theme"], target_duration_sec=60, variant_count=1))
    assert planned["input_snapshot"] == snapshot
    _safe_outbound_text(prompt, pi.entries)
    evidence["preflight"] = {key: planned[key] for key in (
        "prompt_version", "prompt_digest", "sent_segment_ids", "shots_with_static",
        "shots_with_motion", "shots_with_video", "sends_images", "sends_user_notes", "sends_paths",
    )}
    evidence["preflight"]["prompt_bytes"] = len(prompt.encode("utf-8"))
    _write_json(scratch / "evidence.json", evidence)
    if args.ledger:
        from sqlalchemy import select

        model_calls = select(ModelCallRecord).where(
            ModelCallRecord.project_id == pid, ModelCallRecord.channel == "plan_text")
        with session_scope() as session:
            prior_ids = {row.id for row in session.scalars(model_calls)}
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
            assert summary["prompt_digest"] == planned["prompt_digest"]
            assert summary["receipt"]["attempts"] == 1
            evidence["attempts"] = summary["receipt"]["attempts"]
            evidence["model_shots"] = len(model["variants"][0]["shots"])
            evidence["model_duration_sec"] = model["variants"][0]["total_duration_sec"]
            evidence["offline_duration_sec"] = offline["variants"][0]["total_duration_sec"]
            if args.defer_ab:
                _write_json(scratch / "pair-inputs.json", {"offline": offline, "model": model})
            else:
                _pair(client, base, scratch, evidence, offline, model)
        else:
            detail = response.json().get("detail", {})
            evidence["model_error_code"] = detail.get("code")
            evidence["validation_codes"] = [p.get("code") for p in detail.get("problems", [])]
            with session_scope() as session:
                row = session.scalars(model_calls.where(ModelCallRecord.id.not_in(prior_ids))
                                      .order_by(ModelCallRecord.created_at.desc())).first()
                if row:
                    evidence["failed_model_call"] = {key: getattr(row, key) for key in (
                        "id", "model_id", "error_code", "succeeded", "request_bytes", "response_bytes",
                        "elapsed_ms",
                    )}
    _write_json(scratch / "evidence.json", evidence)
    print(json.dumps({"evidence_dir": str(scratch), "candidates": snapshot["candidate_count"],
                      "evidence_channels": evidence["evidence_channels"],
                      "model_status": evidence.get("model_status"),
                      "pair_status": evidence.get("pair_status"),
                      "model_error_code": evidence.get("model_error_code")}))
    if args.ledger and (evidence.get("model_status") != 200
                        or (not args.defer_ab and evidence.get("pair_status") != 201)):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
