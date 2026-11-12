"""TSB-371-R1 P1-04 — a verdict must name a claim that actually exists.

Without this, a client could post ``claim_verdicts`` for keys that never existed,
inflate the denominator, or smuggle verdicts belonging to another project's item.
The review table is the record a report cites, so its content has to be real.

The authority is ``EvaluationItem.readings_json`` — frozen when the session was
created. Validation is against *that*, not against a live re-read of the model
row, so a session stays internally consistent even if the model is re-run.

Duplicate keys matter: sending ``subjects:0`` twice must count once, or a single
sentence could be weighted arbitrarily.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.database import session_scope
from app.models import Asset, Project, Segment, SegmentSemanticResult

SESSIONS = "/api/projects/{p}/evaluation/sessions"
REPORTS = "/api/projects/{p}/evaluation/reports/{s}"


def _uid() -> str:
    return str(uuid.uuid4())


def _tmp_root() -> Path:
    root = Path(os.environ.get("TEMP") or ".") / "tsb371r1-p4"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _seed(client, name="r1p4"):
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
            detected_start_sec=4.0, detected_end_sec=11.0,
            start_sec=4.0, end_sec=11.0, is_trimmed=False, boundary_reason="",
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
            analysis_json={"scene_tags": ["lake", "sky"],
                           "subjects": ["boats", "pier"],
                           "visible_actions": ["floating"],
                           "lighting": "daylight", "composition": "wide",
                           "mood": ["calm"]},
            evidence_json={"state": "observed", "frame_ids": ["f1"]},
            request_usage={}, is_current=True, error_code="", error_message="",
            created_at=None, updated_at=None,
        ))
    return pid


def _session(client, pid):
    return client.post(SESSIONS.format(p=pid), json={"limit": 5}).json()


def _rate(client, pid, sid, item_id, verdicts):
    return client.patch(
        f"{SESSIONS.format(p=pid)}/{sid}/ratings/{item_id}",
        json={"claim_verdicts": verdicts})


# ---------------------------------------------------------------------------
# Only real claims are accepted
# ---------------------------------------------------------------------------

def test_a_known_claim_key_is_accepted(client):
    pid = _seed(client)
    created = _session(client, pid)
    item = created["items"][0]
    r = _rate(client, pid, created["session_id"], item["item_id"],
              [{"key": "subjects:0", "verdict": "right"}])
    assert r.status_code == 200, r.text


def test_an_unknown_claim_key_is_refused(client):
    """A key the frozen reading never had must not enter the record."""
    pid = _seed(client)
    created = _session(client, pid)
    item = created["items"][0]
    r = _rate(client, pid, created["session_id"], item["item_id"],
              [{"key": "subjects:99", "verdict": "right"}])
    assert r.status_code == 422, r.text
    assert "claim" in r.text.lower()


def test_an_unknown_group_is_refused(client):
    pid = _seed(client)
    created = _session(client, pid)
    item = created["items"][0]
    r = _rate(client, pid, created["session_id"], item["item_id"],
              [{"key": "camera_movement:0", "verdict": "right"}])
    assert r.status_code == 422


def test_a_verdict_from_another_projects_item_is_refused(client):
    pid_a = _seed(client, name="p4-a")
    pid_b = _seed(client, name="p4-b")
    created_a = _session(client, pid_a)
    created_b = _session(client, pid_b)
    item_b = created_b["items"][0]
    # Rating project A's session with project B's item id must not work.
    r = _rate(client, pid_a, created_a["session_id"], item_b["item_id"],
              [{"key": "subjects:0", "verdict": "wrong"}])
    assert r.status_code == 404


def test_a_rejected_key_leaves_the_stored_rating_untouched(client):
    """A partially-invalid payload must not half-apply."""
    pid = _seed(client)
    created = _session(client, pid)
    sid, item = created["session_id"], created["items"][0]

    ok = _rate(client, pid, sid, item["item_id"],
               [{"key": "subjects:0", "verdict": "right"}])
    assert ok.status_code == 200

    bad = _rate(client, pid, sid, item["item_id"],
                [{"key": "subjects:0", "verdict": "right"},
                 {"key": "subjects:99", "verdict": "wrong"}])
    assert bad.status_code == 422

    resumed = client.get(f"{SESSIONS.format(p=pid)}/{sid}").json()
    row = next(i for i in resumed["items"] if i["item_id"] == item["item_id"])
    assert row["claim_verdicts"] == [{"key": "subjects:0", "verdict": "right"}]


# ---------------------------------------------------------------------------
# Duplicates must not double-count
# ---------------------------------------------------------------------------

def test_a_duplicate_claim_key_counts_once(client):
    pid = _seed(client)
    created = _session(client, pid)
    sid, item = created["session_id"], created["items"][0]
    r = _rate(client, pid, sid, item["item_id"],
              [{"key": "subjects:0", "verdict": "right"},
               {"key": "subjects:0", "verdict": "right"}])
    assert r.status_code == 200
    resumed = client.get(f"{SESSIONS.format(p=pid)}/{sid}").json()
    row = next(i for i in resumed["items"] if i["item_id"] == item["item_id"])
    assert len(row["claim_verdicts"]) == 1

    summary = client.get(f"{SESSIONS.format(p=pid)}/{sid}/summary").json()
    assert summary["sentences"]["right"] == 1


def test_contradictory_duplicates_keep_the_last(client):
    pid = _seed(client)
    created = _session(client, pid)
    sid, item = created["session_id"], created["items"][0]
    _rate(client, pid, sid, item["item_id"],
          [{"key": "subjects:0", "verdict": "right"},
           {"key": "subjects:0", "verdict": "wrong"}])
    resumed = client.get(f"{SESSIONS.format(p=pid)}/{sid}").json()
    row = next(i for i in resumed["items"] if i["item_id"] == item["item_id"])
    assert row["claim_verdicts"] == [{"key": "subjects:0", "verdict": "wrong"}]


# ---------------------------------------------------------------------------
# The report can point at the sentence
# ---------------------------------------------------------------------------

def test_the_report_names_the_sentence_that_was_wrong(client):
    """A total accuracy is not actionable; the sentence is."""
    pid = _seed(client)
    created = _session(client, pid)
    sid, item = created["session_id"], created["items"][0]
    _rate(client, pid, sid, item["item_id"],
          [{"key": "subjects:0", "verdict": "right"},
           {"key": "subjects:1", "verdict": "wrong"}])

    body = client.get(f"{REPORTS.format(p=pid, s=sid)}").json()
    claims = body["claims"]
    assert any(c["claim_key"] == "subjects:1" and c["verdict"] == "wrong"
               for c in claims)
    wrong = next(c for c in claims if c["claim_key"] == "subjects:1")
    # The text itself, so a reader can quote it.
    assert wrong["text"] == "pier"
    assert wrong["channel"] == "static"
    assert wrong["counted"] is True


def test_the_report_marks_unsure_as_not_counted_in_error_rate(client):
    pid = _seed(client)
    created = _session(client, pid)
    sid, item = created["session_id"], created["items"][0]
    _rate(client, pid, sid, item["item_id"],
          [{"key": "subjects:0", "verdict": "unsure"}])
    body = client.get(f"{REPORTS.format(p=pid, s=sid)}").json()
    row = next(c for c in body["claims"] if c["claim_key"] == "subjects:0")
    assert row["verdict"] == "unsure"
    assert row["counted"] is False


def test_a_claim_never_judged_is_listed_without_a_verdict(client):
    pid = _seed(client)
    created = _session(client, pid)
    sid = created["session_id"]
    body = client.get(f"{REPORTS.format(p=pid, s=sid)}").json()
    keys = {c["claim_key"] for c in body["claims"]}
    # Every claim of the reading is listed, judged or not.
    assert "subjects:0" in keys
    row = next(c for c in body["claims"] if c["claim_key"] == "subjects:0")
    assert row["verdict"] is None
    assert row["counted"] is False


def test_the_markdown_report_lists_the_wrong_sentences(client):
    pid = _seed(client)
    created = _session(client, pid)
    sid, item = created["session_id"], created["items"][0]
    _rate(client, pid, sid, item["item_id"],
          [{"key": "subjects:1", "verdict": "wrong"}])
    md = client.get(f"{REPORTS.format(p=pid, s=sid)}?format=md").text
    assert "pier" in md
    assert "判错" in md or "wrong" in md.lower()


def test_the_csv_report_has_one_row_per_claim(client):
    pid = _seed(client)
    created = _session(client, pid)
    sid = created["session_id"]
    text = client.get(f"{REPORTS.format(p=pid, s=sid)}?format=csv").text
    lines = [ln for ln in text.strip().splitlines() if ln]
    # header + one row per claim. The fixture's reading has
    # 2 tags + 2 subjects + 1 action + lighting + composition + mood = 8 claims.
    assert len(lines) == 9
    assert "claim_key" in lines[0]
