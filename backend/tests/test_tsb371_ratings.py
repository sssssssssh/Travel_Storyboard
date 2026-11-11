"""TSB-371-B — human ratings: storage, recovery, and honest metrics.

The single most important property here is stated in the task book (§3.3): a
metric with **no human ratings must be ``null``, never 100%**. Everything else in
this file exists to keep that true — denominators exclude skipped and un-judged
items, a re-rating recomputes, and stale evidence is not folded into a new result.

The ratings live in their own tables. They must never appear in
``UserAnnotation`` or a storyboard version: a reviewer's opinion of a model's
output is not the user's opinion of the footage.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.database import session_scope
from app.models import (
    Asset,
    Project,
    Segment,
    SegmentSemanticResult,
    Storyboard,
    UserAnnotation,
)

SESSIONS = "/api/projects/{p}/evaluation/sessions"


def _tmp_root() -> Path:
    import os

    root = Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".") / "tsb371-rating-src"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _uid() -> str:
    return str(uuid.uuid4())


def _seed(client, name="t371b", *, n=3, analysed=None, tmp_root=None):
    pid = client.post("/api/projects", json={"name": name}).json()["id"]
    analysed = analysed if analysed is not None else n
    # A real, present file: a shot whose source is missing cannot be visually
    # verified, and the metrics deliberately exclude those (E20).
    src = Path(tmp_root or _tmp_root()) / f"{pid[:8]}.mp4"
    src.parent.mkdir(parents=True, exist_ok=True)
    if not src.exists():
        src.write_bytes(b"\x00" * 64)
    with session_scope() as s:
        a = Asset(
            id=_uid(), project_id=pid, source_path=str(src),
            display_name="c.mp4", file_size_bytes=src.stat().st_size,
            mtime_ns=src.stat().st_mtime_ns, quick_fingerprint="f",
            duration_sec=60.0, fps_num=25, fps_den=1, vfr_flag=False, width=320,
            height=180, rotation=0, codec="h264", pixel_format="yuv420p",
            color_primaries="", color_transfer="", color_space="", has_audio=False,
            audio_codec="", scan_status="succeeded", analysis_version="",
            analyzed_fingerprint="", analyzed_source_signature="",
            analyzed_detect_config="", analyzed_at=None, scanned_at=None,
        )
        s.add(a)
        s.flush()
        segs, results = [], []
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
            segs.append(g.id)
            if i < analysed:
                r = SegmentSemanticResult(
                    id=_uid(), project_id=pid, asset_id=a.id, segment_id=g.id,
                    status="succeeded", model_provider="openai_compatible",
                    model_id="GLM", prompt_version="p", schema_version="1.0",
                    source_signature=_uid(), frame_manifest_hash="h",
                    analysis_json={"scene_tags": ["lake"], "subjects": ["pier"],
                                   "visible_actions": ["walking"],
                                   "lighting": "daylight", "composition": "wide",
                                   "mood": ["calm"]},
                    evidence_json={"state": "observed", "frame_ids": ["f1"]},
                    request_usage={}, is_current=True, error_code="",
                    error_message="", created_at=None, updated_at=None,
                )
                s.add(r)
                s.flush()
                results.append(r.id)
    return pid, {"segments": segs, "results": results}


def _create_session(client, pid) -> dict:
    r = client.post(SESSIONS.format(p=pid), json={"limit": 5})
    assert r.status_code in (200, 201), r.text
    return r.json()


def _rate(client, pid, sid, item_id, **body):
    return client.patch(f"{SESSIONS.format(p=pid)}/{sid}/ratings/{item_id}", json=body)


# ---------------------------------------------------------------------------
# Session creation and recovery
# ---------------------------------------------------------------------------

def test_a_session_freezes_the_items_it_will_ask_about(client):
    pid, ids = _seed(client)
    session = _create_session(client, pid)
    assert session["project_id"] == pid
    assert session["status"] == "IN_PROGRESS"
    assert session["sample_seed"]
    assert len(session["items"]) == 3
    for item in session["items"]:
        assert item["result_id"] in ids["results"]
        assert item["review_status"] == "PENDING"


def test_creating_a_session_writes_no_production_rows(client):
    pid, _ = _seed(client)

    def counts():
        with session_scope() as s:
            return (
                int(s.scalar(select(func.count()).select_from(UserAnnotation)) or 0),
                int(s.scalar(select(func.count()).select_from(Storyboard).where(
                    Storyboard.project_id == pid)) or 0),
            )

    before = counts()
    _create_session(client, pid)
    assert counts() == before


def test_a_session_can_be_resumed_after_a_restart(client):
    pid, _ = _seed(client)
    created = _create_session(client, pid)
    sid = created["session_id"]
    item = created["items"][0]
    _rate(client, pid, sid, item["item_id"], scene=2, subject=2, action=1, hallucination="none")

    # A fresh request models a page reload / process restart.
    again = client.get(f"{SESSIONS.format(p=pid)}/{sid}")
    assert again.status_code == 200
    body = again.json()
    assert body["session_id"] == sid
    rated = [i for i in body["items"] if i["review_status"] != "PENDING"]
    assert len(rated) == 1
    assert rated[0]["ratings"]["scene"] == 2


def test_the_frozen_items_do_not_change_between_reads(client):
    pid, _ = _seed(client)
    created = _create_session(client, pid)
    sid = created["session_id"]
    a = client.get(f"{SESSIONS.format(p=pid)}/{sid}").json()
    b = client.get(f"{SESSIONS.format(p=pid)}/{sid}").json()
    assert [i["item_id"] for i in a["items"]] == [i["item_id"] for i in b["items"]]
    assert [i["result_id"] for i in a["items"]] == [i["result_id"] for i in b["items"]]


# ---------------------------------------------------------------------------
# E17 / E18 — the metrics that must never lie
# ---------------------------------------------------------------------------

def test_no_ratings_means_null_accuracy_never_one_hundred_percent(client):
    pid, _ = _seed(client)
    created = _create_session(client, pid)
    summary = client.get(
        f"{SESSIONS.format(p=pid)}/{created['session_id']}/summary").json()
    # E17: the headline failure mode.
    assert summary["evaluated_claims"] == 0
    assert summary["strict_accuracy"] is None
    assert summary["partial_or_better_rate"] is None
    assert summary["status"] == "MANUAL_REVIEW_PENDING"


def test_two_one_zero_and_null_produce_the_documented_denominator(client):
    """E18: 2/1/0 scored, null skipped -> denominator 3, strict 1/3, partial 2/3."""
    pid, _ = _seed(client)
    created = _create_session(client, pid)
    sid = created["session_id"]
    items = created["items"]

    _rate(client, pid, sid, items[0]["item_id"], scene=2)
    _rate(client, pid, sid, items[1]["item_id"], scene=1)
    _rate(client, pid, sid, items[2]["item_id"], scene=0)

    summary = client.get(f"{SESSIONS.format(p=pid)}/{sid}/summary").json()
    assert summary["evaluated_claims"] == 3
    assert summary["strict_accuracy"] == pytest.approx(1 / 3)
    assert summary["partial_or_better_rate"] == pytest.approx(2 / 3)
    # All three items were judged, so this session is complete.
    assert summary["status"] == "COMPLETE"


def test_cannot_judge_is_excluded_from_the_denominator(client):
    pid, _ = _seed(client)
    created = _create_session(client, pid)
    sid = created["session_id"]
    items = created["items"]
    _rate(client, pid, sid, items[0]["item_id"], scene=2)
    _rate(client, pid, sid, items[1]["item_id"], scene=None)

    summary = client.get(f"{SESSIONS.format(p=pid)}/{sid}/summary").json()
    assert summary["evaluated_claims"] == 1
    assert summary["strict_accuracy"] == pytest.approx(1.0)
    assert summary["unjudged"] == 1


def test_re_rating_recomputes_immediately(client):
    """E19: changing 2 to 0 must move the metric, not be averaged in."""
    pid, _ = _seed(client)
    created = _create_session(client, pid)
    sid = created["session_id"]
    item = created["items"][0]
    _rate(client, pid, sid, item["item_id"], scene=2)
    assert client.get(f"{SESSIONS.format(p=pid)}/{sid}/summary").json()[
        "strict_accuracy"] == pytest.approx(1.0)

    _rate(client, pid, sid, item["item_id"], scene=0)
    summary = client.get(f"{SESSIONS.format(p=pid)}/{sid}/summary").json()
    assert summary["strict_accuracy"] == pytest.approx(0.0)
    assert summary["evaluated_claims"] == 1


# ---------------------------------------------------------------------------
# Hallucination and per-dimension breakdown
# ---------------------------------------------------------------------------

def test_hallucination_is_counted_separately_for_severe_and_mild(client):
    pid, _ = _seed(client)
    created = _create_session(client, pid)
    sid = created["session_id"]
    items = created["items"]
    _rate(client, pid, sid, items[0]["item_id"], hallucination="severe")
    _rate(client, pid, sid, items[1]["item_id"], hallucination="mild")
    _rate(client, pid, sid, items[2]["item_id"], hallucination="none")

    summary = client.get(f"{SESSIONS.format(p=pid)}/{sid}/summary").json()
    assert summary["hallucination"]["severe"] == 1
    assert summary["hallucination"]["mild"] == 1
    assert summary["hallucination"]["none"] == 1
    assert summary["hallucination"]["judged"] == 3
    assert summary["hallucination"]["rate"] == pytest.approx(2 / 3)


def test_dimensions_are_reported_independently(client):
    pid, _ = _seed(client)
    created = _create_session(client, pid)
    sid = created["session_id"]
    items = created["items"]
    _rate(client, pid, sid, items[0]["item_id"], scene=2, subject=0, action=1)
    summary = client.get(f"{SESSIONS.format(p=pid)}/{sid}/summary").json()
    by_dim = summary["by_dimension"]
    assert by_dim["scene"]["evaluated"] == 1
    assert by_dim["scene"]["strict_accuracy"] == pytest.approx(1.0)
    assert by_dim["subject"]["strict_accuracy"] == pytest.approx(0.0)
    assert by_dim["timing"]["evaluated"] == 0
    assert by_dim["timing"]["strict_accuracy"] is None


def test_an_invalid_rating_value_is_refused(client):
    pid, _ = _seed(client)
    created = _create_session(client, pid)
    sid = created["session_id"]
    item = created["items"][0]
    r = _rate(client, pid, sid, item["item_id"], scene=7)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# E13 — concurrency and idempotence
# ---------------------------------------------------------------------------

def test_saving_the_same_rating_twice_is_idempotent(client):
    pid, _ = _seed(client)
    created = _create_session(client, pid)
    sid = created["session_id"]
    item = created["items"][0]
    a = _rate(client, pid, sid, item["item_id"], scene=2)
    b = _rate(client, pid, sid, item["item_id"], scene=2)
    assert a.status_code == b.status_code == 200
    summary = client.get(f"{SESSIONS.format(p=pid)}/{sid}/summary").json()
    assert summary["evaluated_claims"] == 1


def test_a_stale_version_cannot_overwrite_a_newer_rating(client):
    """E13: two tabs must not silently clobber each other."""
    pid, _ = _seed(client)
    created = _create_session(client, pid)
    sid = created["session_id"]
    item = created["items"][0]

    first = _rate(client, pid, sid, item["item_id"], scene=2)
    version = first.json()["items"][0]["version"]

    # Tab B saves with the old version after tab A already moved on.
    _rate(client, pid, sid, item["item_id"], scene=0, expected_version=version)
    stale = _rate(client, pid, sid, item["item_id"], scene=1,
                  expected_version=version)
    assert stale.status_code == 409


# ---------------------------------------------------------------------------
# E08 / E12 / E14 — isolation, durability, staleness
# ---------------------------------------------------------------------------

def test_another_projects_session_is_404(client):
    pid_a, _ = _seed(client, name="sess-a")
    pid_b, _ = _seed(client, name="sess-b")
    created = _create_session(client, pid_a)
    r = client.get(f"{SESSIONS.format(p=pid_b)}/{created['session_id']}")
    assert r.status_code == 404


def test_an_unknown_item_cannot_be_rated(client):
    pid, _ = _seed(client)
    created = _create_session(client, pid)
    r = _rate(client, pid, created["session_id"], "not-an-item", scene=2)
    assert r.status_code == 404


def test_a_changed_result_is_marked_stale_and_excluded_from_metrics(client):
    """E14: an old rating must not be applied to a new model result."""
    pid, ids = _seed(client)
    created = _create_session(client, pid)
    sid = created["session_id"]
    item = created["items"][0]
    _rate(client, pid, sid, item["item_id"], scene=2)
    assert client.get(f"{SESSIONS.format(p=pid)}/{sid}/summary").json()[
        "evaluated_claims"] == 1

    # The model is re-run: the old row is superseded by a new one.
    with session_scope() as s:
        old = s.get(SegmentSemanticResult, item["result_id"])
        old.is_current = False
        asset_id = old.asset_id
        segment_id = old.segment_id
        s.add(SegmentSemanticResult(
            id=_uid(), project_id=pid, asset_id=asset_id, segment_id=segment_id,
            status="succeeded", model_provider="openai_compatible", model_id="GLM",
            prompt_version="p2", schema_version="1.0", source_signature=_uid(),
            frame_manifest_hash="h",
            analysis_json={"scene_tags": ["temple"]},
            evidence_json={"state": "observed", "frame_ids": ["f2"]},
            request_usage={}, is_current=True, error_code="", error_message="",
            created_at=None, updated_at=None))

    resumed = client.get(f"{SESSIONS.format(p=pid)}/{sid}").json()
    stale_item = next(i for i in resumed["items"] if i["item_id"] == item["item_id"])
    assert stale_item["review_status"] == "STALE_EVIDENCE"

    summary = client.get(f"{SESSIONS.format(p=pid)}/{sid}/summary").json()
    # The old rating is kept for audit but no longer counts.
    assert summary["evaluated_claims"] == 0
    assert summary["strict_accuracy"] is None


def test_the_session_records_what_it_measured_for_the_report(client):
    pid, ids = _seed(client)
    created = _create_session(client, pid)
    body = client.get(f"{SESSIONS.format(p=pid)}/{created['session_id']}").json()
    assert body["sampling_rule_version"]
    assert body["sample_seed"]
    for item in body["items"]:
        assert item["source_window"]["start_abs_sec"] is not None
        assert item["channel"] in ("static", "multiframe", "video_native")
