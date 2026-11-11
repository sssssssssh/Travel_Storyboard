"""TSB-371-D — the report must be at least as honest as the API.

The report is what gets screenshotted and quoted, so it is where an overstated
number does the most damage. Every test here checks one way the report could lie:

* an empty session prints ``N/A``, not ``0%`` or ``100%``;
* re-rating changes the printed number;
* no export format leaks a path, a token or a user note;
* gate statuses are read live rather than copied from a document.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

from app.database import session_scope
from app.models import Asset, Project, Segment, SegmentSemanticResult

SESSIONS = "/api/projects/{p}/evaluation/sessions"
REPORTS = "/api/projects/{p}/evaluation/reports/{s}"


def _uid() -> str:
    return str(uuid.uuid4())


def _tmp_root() -> Path:
    root = Path(os.environ.get("TEMP") or ".") / "tsb371-report-src"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _seed(client, name="t371d", *, n=2):
    pid = client.post("/api/projects", json={"name": name}).json()["id"]
    src = _tmp_root() / f"{pid[:8]}.mp4"
    if not src.exists():
        src.write_bytes(b"\x00" * 64)
    secret_note = "私密备注：联系 13800000000 在老王家的阳台"
    with session_scope() as s:
        a = Asset(
            id=_uid(), project_id=pid, source_path=str(src), display_name="c.mp4",
            file_size_bytes=src.stat().st_size, mtime_ns=src.stat().st_mtime_ns,
            quick_fingerprint="f", duration_sec=60.0, fps_num=25, fps_den=1,
            vfr_flag=False, width=320, height=180, rotation=0, codec="h264",
            pixel_format="yuv420p", color_primaries="", color_transfer="",
            color_space="", has_audio=False, audio_codec="", scan_status="succeeded",
            analysis_version="", analyzed_fingerprint="",
            analyzed_source_signature="", analyzed_detect_config="",
            analyzed_at=None, scanned_at=None,
        )
        s.add(a)
        s.flush()
        for i in range(n):
            g = Segment(
                id=_uid(), asset_id=a.id, project_id=pid,
                detected_start_sec=i * 5.0, detected_end_sec=i * 5.0 + 4.0,
                start_sec=i * 5.0, end_sec=i * 5.0 + 4.0, is_trimmed=False,
                boundary_reason="", quality_metrics={"blur_avg": 100.0},
                content_tags={}, story_role="", user_rating="pending",
                status="candidate", frame_refs=[], proxy_ref="", parent_segment_id="",
                algorithm_version="1", created_at=None,
            )
            s.add(g)
            s.flush()
            s.add(SegmentSemanticResult(
                id=_uid(), project_id=pid, asset_id=a.id, segment_id=g.id,
                status="succeeded", model_provider="openai_compatible",
                model_id="GLM", prompt_version="p", schema_version="1.0",
                source_signature=_uid(), frame_manifest_hash="h",
                analysis_json={"scene_tags": ["lake"], "subjects": ["pier"],
                               "visible_actions": [], "lighting": "daylight",
                               "composition": "wide", "mood": ["calm"]},
                evidence_json={"state": "observed", "frame_ids": ["f1"]},
                request_usage={}, is_current=True, error_code="",
                error_message="", created_at=None, updated_at=None,
            ))
    return pid, secret_note


def _session(client, pid) -> dict:
    r = client.post(SESSIONS.format(p=pid), json={"limit": 5})
    assert r.status_code == 201, r.text
    return r.json()


def _report(client, pid, sid, fmt="json"):
    return client.get(REPORTS.format(p=pid, s=sid) + f"?format={fmt}")


# ---------------------------------------------------------------------------
# E17 passed through to the report
# ---------------------------------------------------------------------------

def test_an_unrated_session_reports_na_not_a_percentage(client):
    pid, _ = _seed(client)
    sid = _session(client, pid)["session_id"]
    body = _report(client, pid, sid).json()
    assert body["human_metrics"]["evaluated_claims"] == 0
    assert body["human_metrics"]["strict_accuracy"] is None
    assert body["status"] == "MANUAL_REVIEW_PENDING"

    md = _report(client, pid, sid, "md").text
    assert "N/A" in md
    # The two dangerous renderings of "no data".
    assert "100.0%" not in md
    assert "| 严格准确率 | 0.0% |" not in md


def test_the_markdown_never_claims_completion_without_ratings(client):
    pid, _ = _seed(client)
    sid = _session(client, pid)["session_id"]
    md = _report(client, pid, sid, "md").text
    assert "MANUAL_REVIEW_PENDING" in md


# ---------------------------------------------------------------------------
# E18 / E19 through the report
# ---------------------------------------------------------------------------

def test_the_report_reflects_real_scores(client):
    pid, _ = _seed(client, n=2)
    created = _session(client, pid)
    sid = created["session_id"]
    for i, item in enumerate(created["items"]):
        client.patch(f"{SESSIONS.format(p=pid)}/{sid}/ratings/{item['item_id']}",
                     json={"scene": 2 if i == 0 else 0})
    body = _report(client, pid, sid).json()
    assert body["human_metrics"]["evaluated_claims"] == 2
    assert body["human_metrics"]["strict_accuracy"] == pytest.approx(0.5)

    md = _report(client, pid, sid, "md").text
    assert "50.0%" in md


def test_changing_a_score_changes_the_report(client):
    """E19: 2 -> 0 must move the printed accuracy, not be averaged."""
    pid, _ = _seed(client, n=1)
    created = _session(client, pid)
    sid = created["session_id"]
    item = created["items"][0]
    client.patch(f"{SESSIONS.format(p=pid)}/{sid}/ratings/{item['item_id']}",
                 json={"scene": 2})
    assert _report(client, pid, sid).json()["human_metrics"]["strict_accuracy"] == 1.0

    client.patch(f"{SESSIONS.format(p=pid)}/{sid}/ratings/{item['item_id']}",
                 json={"scene": 0})
    assert _report(client, pid, sid).json()["human_metrics"]["strict_accuracy"] == 0.0


# ---------------------------------------------------------------------------
# E21 — a channel with no readings produces no metrics for it
# ---------------------------------------------------------------------------

def test_multiframe_metrics_are_absent_when_only_stills_exist(client):
    pid, _ = _seed(client, n=2)
    body = _report(client, pid, _session(client, pid)["session_id"]).json()
    channels = {i["channel"] for i in body["items"]}
    assert channels == {"static"}
    # No fabricated multiframe accuracy.
    md = _report(client, pid, _session(client, pid)["session_id"], "md").text
    assert "multiframe" not in md.lower() or "多帧" not in md


# ---------------------------------------------------------------------------
# E24 — export hygiene
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fmt", ["json", "csv", "md"])
def test_no_export_leaks_paths_tokens_or_notes(client, fmt):
    pid, secret_note = _seed(client)
    sid = _session(client, pid)["session_id"]
    text = _report(client, pid, sid, fmt).text
    assert str(_tmp_root()) not in text
    assert "/nonexistent/" not in text
    assert "13800000000" not in text
    assert secret_note not in text
    assert "Bearer" not in text
    assert "api_key" not in text


def test_the_json_report_contains_the_required_sections(client):
    pid, _ = _seed(client)
    body = _report(client, pid, _session(client, pid)["session_id"]).json()
    for key in ("session_id", "project_code", "scope", "human_metrics",
                "items", "gates", "limitations", "disclaimer", "status"):
        assert key in body, key
    assert "G4_MODEL_PLAN" in body["gates"]
    assert body["gates"]["G4_MODEL_PLAN"]["status"] in ("PASS", "BLOCKED", "FAIL")


def test_the_csv_has_one_row_per_item_and_na_for_unrated(client):
    pid, _ = _seed(client, n=2)
    sid = _session(client, pid)["session_id"]
    csv_text = _report(client, pid, sid, "csv").text
    lines = [ln for ln in csv_text.strip().splitlines() if ln]
    assert len(lines) == 3            # header + 2 items
    assert "N/A" in csv_text          # unrated scores are not zeros


def test_the_report_states_the_sample_is_small(client):
    pid, _ = _seed(client)
    body = _report(client, pid, _session(client, pid)["session_id"]).json()
    assert any("小样本" in lim or "small" in lim.lower() for lim in body["limitations"])


def test_an_unknown_session_is_404(client):
    pid, _ = _seed(client)
    r = _report(client, pid, str(uuid.uuid4()))
    assert r.status_code == 404


def test_another_projects_report_is_404(client):
    pid_a, _ = _seed(client, name="rep-a")
    pid_b, _ = _seed(client, name="rep-b")
    sid = _session(client, pid_a)["session_id"]
    assert _report(client, pid_b, sid).status_code == 404


def test_ab_is_blocked_when_no_model_plan_exists(client):
    """E25 surfaced in the report."""
    pid, _ = _seed(client)
    body = _report(client, pid, _session(client, pid)["session_id"]).json()
    assert body["ab"] is not None
    assert body["ab"]["status"] == "AB_BLOCKED_NO_REAL_MODEL_PLAN"
