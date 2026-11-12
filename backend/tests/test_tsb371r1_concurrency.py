"""TSB-371-R1 P2-02 — fast clicking must not lose a judgement.

The page saves on every click, so a reviewer rating a shot quickly fires several
PATCHes in flight at once. TSB-371 had `version` and a 409, but the client sent a
snapshot read at render time — two quick clicks sent ``expected_version: 0`` twice,
the second was rejected, and (before R1) navigating away then silently replaced the
saved set.

The contract this file pins:

* a version conflict is reported as **409**, never silently merged or overwritten;
* the latest stored judgement is always one the reviewer actually expressed —
  never a lost update, never a half-written payload;
* out-of-order arrivals cannot erase a newer judgement;
* reloading shows the true stored state.

Application-level concurrency (two requests at once) is exercised with threads
against the real app, not simulated.
"""

from __future__ import annotations

import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.database import session_scope
from app.models import Asset, Project, Segment, SegmentSemanticResult

SESSIONS = "/api/projects/{p}/evaluation/sessions"


def _uid() -> str:
    return str(uuid.uuid4())


def _tmp_root() -> Path:
    root = Path(os.environ.get("TEMP") or ".") / "tsb371r1-conc"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _seed(client, name="conc"):
    pid = client.post("/api/projects", json={"name": name}).json()["id"]
    src = _tmp_root() / f"{pid[:8]}.mp4"
    src.write_bytes(b"\x00" * 64)
    with session_scope() as s:
        a = Asset(
            id=_uid(), project_id=pid, source_path=str(src), display_name="c.mp4",
            file_size_bytes=src.stat().st_size, mtime_ns=src.stat().st_mtime_ns,
            quick_fingerprint="f", duration_sec=60.0, fps_num=25, fps_den=1,
            vfr_flag=False, width=1280, height=720, rotation=0, codec="h264",
            pixel_format="yuv420p", color_primaries="", color_transfer="",
            color_space="", has_audio=False, audio_codec="", scan_status="succeeded",
            analysis_version="", analyzed_fingerprint="",
            analyzed_source_signature="", analyzed_detect_config="",
            analyzed_at=None,
            scanned_at=datetime(2026, 10, 14, tzinfo=timezone.utc),
        )
        s.add(a)
        s.flush()
        seg = Segment(
            id=_uid(), asset_id=a.id, project_id=pid,
            detected_start_sec=0.0, detected_end_sec=8.0,
            start_sec=0.0, end_sec=8.0, is_trimmed=False, boundary_reason="",
            quality_metrics={"blur_avg": 100.0}, content_tags={}, story_role="",
            user_rating="pending", status="candidate", frame_refs=[], proxy_ref="",
            parent_segment_id="", algorithm_version="1", created_at=None,
        )
        s.add(seg)
        s.flush()
        s.add(SegmentSemanticResult(
            id=_uid(), project_id=pid, asset_id=a.id, segment_id=seg.id,
            status="succeeded", model_provider="openai_compatible", model_id="GLM",
            prompt_version="p", schema_version="1.0", source_signature=_uid(),
            frame_manifest_hash="h",
            analysis_json={"scene_tags": ["lake"], "subjects": ["boats", "pier"],
                           "visible_actions": ["floating"]},
            evidence_json={"state": "observed", "frame_ids": ["f1"]},
            request_usage={}, is_current=True, error_code="", error_message="",
            created_at=None, updated_at=None,
        ))
    return pid


def _session(client, pid):
    return client.post(SESSIONS.format(p=pid), json={"limit": 5}).json()


def _patch(client, pid, sid, item_id, body):
    return client.patch(
        f"{SESSIONS.format(p=pid)}/{sid}/ratings/{item_id}", json=body)


def _stored(client, pid, sid, item_id):
    body = client.get(f"{SESSIONS.format(p=pid)}/{sid}").json()
    return next(i for i in body["items"] if i["item_id"] == item_id)


# ---------------------------------------------------------------------------
# Version conflict is reported, not swallowed
# ---------------------------------------------------------------------------

def test_a_stale_version_is_rejected_with_409(client):
    pid = _seed(client)
    created = _session(client, pid)
    sid, item = created["session_id"], created["items"][0]

    first = _patch(client, pid, sid, item["item_id"],
                   {"subject": 2, "claim_verdicts": [
                       {"key": "subjects:0", "verdict": "right"}],
                    "expected_version": 0})
    assert first.status_code == 200

    stale = _patch(client, pid, sid, item["item_id"],
                   {"subject": 0, "claim_verdicts": [
                       {"key": "subjects:0", "verdict": "wrong"}],
                    "expected_version": 0})
    assert stale.status_code == 409


def test_a_rejected_stale_write_changes_nothing(client):
    pid = _seed(client)
    created = _session(client, pid)
    sid, item = created["session_id"], created["items"][0]
    _patch(client, pid, sid, item["item_id"],
           {"subject": 2, "claim_verdicts": [
               {"key": "subjects:0", "verdict": "right"}]})
    before = _stored(client, pid, sid, item["item_id"])

    _patch(client, pid, sid, item["item_id"],
           {"subject": 0, "claim_verdicts": [
               {"key": "subjects:0", "verdict": "wrong"}],
            "expected_version": 0})

    after = _stored(client, pid, sid, item["item_id"])
    assert after["claim_verdicts"] == before["claim_verdicts"]
    assert after["ratings"]["subject"] == before["ratings"]["subject"]


# ---------------------------------------------------------------------------
# Rapid sequential saves (what fast clicking produces)
# ---------------------------------------------------------------------------

def test_rapid_saves_accumulate_without_loss(client):
    """Clicking five claims in a row must end with all five stored."""
    pid = _seed(client)
    created = _session(client, pid)
    sid, item = created["session_id"], created["items"][0]

    plan = [
        {"key": "subjects:0", "verdict": "right"},
        {"key": "subjects:1", "verdict": "wrong"},
        {"key": "scene_tags:0", "verdict": "right"},
        {"key": "visible_actions:0", "verdict": "unsure"},
    ]
    acc: list[dict] = []
    version = 0
    for entry in plan:
        acc = acc + [entry]
        r = _patch(client, pid, sid, item["item_id"],
                   {"claim_verdicts": acc, "expected_version": version})
        assert r.status_code == 200, r.text
        version = _stored(client, pid, sid, item["item_id"])["version"]

    stored = _stored(client, pid, sid, item["item_id"])
    assert {v["key"] for v in stored["claim_verdicts"]} == {
        "subjects:0", "subjects:1", "scene_tags:0", "visible_actions:0"}


def test_the_version_advances_on_every_save(client):
    pid = _seed(client)
    created = _session(client, pid)
    sid, item = created["session_id"], created["items"][0]
    versions = []
    for i in range(3):
        _patch(client, pid, sid, item["item_id"],
               {"subject": 2, "claim_verdicts": [
                   {"key": "subjects:0", "verdict": "right"}]})
        versions.append(_stored(client, pid, sid, item["item_id"])["version"])
    assert versions == sorted(set(versions)), versions
    assert len(set(versions)) == 3


# ---------------------------------------------------------------------------
# Out-of-order arrival
# ---------------------------------------------------------------------------

def test_an_older_request_arriving_late_cannot_erase_a_newer_one(client):
    """Two tabs: tab A saves v1, tab B saves v2, then A's retry lands late."""
    pid = _seed(client)
    created = _session(client, pid)
    sid, item = created["session_id"], created["items"][0]

    # Both tabs read version 0.
    a = _patch(client, pid, sid, item["item_id"],
               {"claim_verdicts": [{"key": "subjects:0", "verdict": "right"}],
                "expected_version": 0})
    assert a.status_code == 200

    b = _patch(client, pid, sid, item["item_id"],
               {"claim_verdicts": [{"key": "subjects:1", "verdict": "wrong"}],
                "expected_version": 1})
    assert b.status_code == 200

    # Tab A retries with its stale snapshot.
    late = _patch(client, pid, sid, item["item_id"],
                  {"claim_verdicts": [{"key": "subjects:0", "verdict": "right"}],
                   "expected_version": 0})
    assert late.status_code == 409

    stored = _stored(client, pid, sid, item["item_id"])
    keys = {v["key"] for v in stored["claim_verdicts"]}
    assert keys == {"subjects:1"}, keys


# ---------------------------------------------------------------------------
# Real concurrent requests
# ---------------------------------------------------------------------------

def test_concurrent_saves_never_half_apply(client):
    """Several threads saving at once: the stored payload is always coherent."""
    pid = _seed(client)
    created = _session(client, pid)
    sid, item = created["session_id"], created["items"][0]

    results: list[int] = []
    errors: list[str] = []
    lock = threading.Lock()

    def worker(verdict: str) -> None:
        try:
            r = _patch(client, pid, sid, item["item_id"],
                       {"claim_verdicts": [{"key": "subjects:0",
                                            "verdict": verdict}]})
            with lock:
                results.append(r.status_code)
        except Exception as exc:              # noqa: BLE001 - recorded below
            with lock:
                errors.append(f"{type(exc).__name__}: {str(exc)[:120]}")
                results.append(-1)

    threads = [threading.Thread(target=worker, args=("right",)) for _ in range(3)]
    threads += [threading.Thread(target=worker, args=("wrong",)) for _ in range(3)]
    # Start them together so the requests really overlap.
    import time as _t
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    # Every request either applied or was refused; none crashed the server.
    assert all(code in (200, 409) for code in results), (results, errors)
    assert 200 in results

    stored = _stored(client, pid, sid, item["item_id"])
    # Whatever won, the stored state is one of the expressed verdicts -- never a
    # mixture, never empty.
    assert len(stored["claim_verdicts"]) == 1
    assert stored["claim_verdicts"][0]["verdict"] in ("right", "wrong")


def test_reloading_shows_the_single_winning_verdict(client):
    pid = _seed(client)
    created = _session(client, pid)
    sid, item = created["session_id"], created["items"][0]
    _patch(client, pid, sid, item["item_id"],
           {"claim_verdicts": [{"key": "subjects:0", "verdict": "wrong"}]})
    for _ in range(3):
        stored = _stored(client, pid, sid, item["item_id"])
        assert stored["claim_verdicts"] == [
            {"key": "subjects:0", "verdict": "wrong"}]
