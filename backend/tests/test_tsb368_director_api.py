"""TSB-368-B — director API contracts: filtering, paging, isolation, read-only.

The endpoints must be usable as a browse surface AND as a stable contract for
TSB-369. That means: strict validation, stable ordering across identical calls,
coverage that ignores the current filter, no cross-project leakage, and — most
importantly — no side effects.
"""

from __future__ import annotations

import uuid

import pytest

from app.database import session_scope
from app.models import (
    Asset,
    EditPlanShot,
    Job,
    Project,
    Segment,
    SegmentSemanticResult,
    SegmentVideoObservation,
    StoryboardVersion,
    UserAnnotation,
)
from app.services import director_query as dq


def _uid() -> str:
    return str(uuid.uuid4())


@pytest.fixture()
def seeded(client):
    """A project with 12 shots across 3 assets and mixed analysis states."""
    pid = client.post("/api/projects", json={"name": "director-api", "source_roots": []}).json()["id"]
    ids: dict[str, list[str]] = {"assets": [], "segments": []}
    with session_scope() as s:
        p = s.get(Project, pid)
        assets = []
        for i in range(3):
            a = Asset(
                id=_uid(), project_id=pid, source_path=f"/x/{i}.mp4", display_name=f"clip{i}.mp4",
                file_size_bytes=1, mtime_ns=1, quick_fingerprint="f", duration_sec=600.0,
                fps_num=30, fps_den=1, vfr_flag=False, width=640, height=360, rotation=0,
                codec="h264", pixel_format="yuv420p", color_primaries="", color_transfer="",
                color_space="", has_audio=False, audio_codec="", scan_status="succeeded",
                analysis_version="", analyzed_fingerprint="", analyzed_source_signature="",
                analyzed_detect_config="", analyzed_at=None, scanned_at=None,
            )
            s.add(a)
            assets.append(a)
            ids["assets"].append(a.id)
        s.flush()
        for i in range(12):
            a = assets[i % 3]
            start = float(i * 10)
            g = Segment(
                id=_uid(), asset_id=a.id, project_id=pid,
                detected_start_sec=start, detected_end_sec=start + 8,
                start_sec=start, end_sec=start + 8, is_trimmed=False, boundary_reason="",
                quality_metrics={"blur_avg": 100.0 + i, "frame_count": 2},
                content_tags={}, story_role="", user_rating="pending", status="candidate",
                frame_refs=[], proxy_ref="", parent_segment_id="", algorithm_version="1",
                created_at=None,
            )
            s.add(g)
            ids["segments"].append(g.id)
        s.flush()
        # Segment 0: verified-ish static reading (unverified freshness, but a reading).
        s.add(SegmentSemanticResult(
            id=_uid(), project_id=pid, asset_id=s.get(Segment, ids["segments"][0]).asset_id,
            segment_id=ids["segments"][0], status="succeeded", model_provider="mock",
            model_id="m", prompt_version="p", schema_version="1.0",
            source_signature="sig", frame_manifest_hash="h",
            analysis_json={"visual": {"scene_tags": ["nature"], "subjects": ["a lake"],
                                      "visible_actions": ["rippling"], "lighting": "daylight",
                                      "composition": "wide", "mood": ["calm"]}},
            evidence_json={"state": "observed", "frame_ids": ["f1", "f2"],
                           "observed_confidence": 0.9},
            request_usage={}, is_current=True, error_code="", error_message="",
            created_at=None, updated_at=None,
        ))
        # Segment 1: native video reading.
        seg1 = s.get(Segment, ids["segments"][1])
        s.add(SegmentVideoObservation(
            id=_uid(), project_id=pid, asset_id=seg1.asset_id, segment_id=seg1.id,
            status="succeeded", method="video_native", model_provider="mock", model_id="m",
            prompt_version="p", schema_version="1.0", protocol="native",
            source_signature="v", clip_sha256="c", version_key="k",
            clip_start_sec=seg1.start_sec, clip_end_sec=seg1.end_sec,
            clip_duration_sec=8.0, clip_size_bytes=1,
            segment_start_sec=seg1.start_sec, segment_end_sec=seg1.end_sec,
            analysis_json={"summary": "a boat crosses the lake"},
            events_json=[{"start_sec": seg1.start_sec + 0.5, "end_sec": seg1.start_sec + 2.0,
                          "label": "boat_moving", "confidence": 0.7}],
            request_usage={}, audio_analyzed=False, color_unverified=True,
            observed_temporal_quality="partial", is_current=True,
            error_code="", error_message="", created_at=None, updated_at=None,
        ))
        # Segment 2: human favourite.
        seg2 = s.get(Segment, ids["segments"][2])
        seg2.user_rating = "favorite"
        seg2.story_role = "开场"
        # Segment 3: needs_review status.
        s.get(Segment, ids["segments"][3]).status = "needs_review"
        # Segment 4: excluded by a human.
        s.get(Segment, ids["segments"][4]).user_rating = "exclude"
        # Segment 5: an annotation note (searchable text).
        s.add(UserAnnotation(
            id=_uid(), segment_id=ids["segments"][5], rating="like",
            note="朋友在桥上看日落", in_sec=None, out_sec=None,
            created_at=None, updated_at=None,
        ))
        s.flush()
    return {"project_id": pid, **ids}


def _get(client, path, **params):
    return client.get(path, params=params)


CAND = "/api/projects/{p}/director/candidates"
CTX = "/api/projects/{p}/director/context"


# ---------------------------------------------------------------- basics
def test_candidates_endpoint_returns_one_page_and_project_coverage(client, seeded):
    r = _get(client, CAND.format(p=seeded["project_id"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["project_id"] == seeded["project_id"]
    assert body["query_version"] == "1.0"
    # 12 shots, minus needs_review(1) and exclude(1) = 10 eligible by default.
    assert body["coverage"]["library_segments"] == 12
    assert body["coverage"]["needs_review_segments"] == 1
    assert body["coverage"]["excluded_segments"] == 1
    assert body["coverage"]["eligible_segments"] == 10
    assert body["total"] == 10
    assert len(body["items"]) == 10


def test_coverage_ignores_the_current_filter(client, seeded):
    """The denominator is the project, not the page."""
    r_all = _get(client, CAND.format(p=seeded["project_id"]), eligibility="all")
    r_one = _get(client, CAND.format(p=seeded["project_id"]), eligibility="eligible", limit=1)
    assert r_all.json()["coverage"] == r_one.json()["coverage"]
    assert r_one.json()["total"] == 10
    assert len(r_one.json()["items"]) == 1


def test_channels_are_reported_separately(client, seeded):
    r = _get(client, CAND.format(p=seeded["project_id"]), eligibility="all", limit=100)
    items = {i["segment_id"]: i for i in r.json()["items"]}
    s0 = items[seeded["segments"][0]]
    assert s0["observations"]["static"]["result_id"] is not None
    assert s0["observations"]["video_native"]["state"] == "not_analyzed"

    s1 = items[seeded["segments"][1]]
    assert s1["observations"]["video_native"]["result_id"] is not None
    assert s1["observations"]["static"]["state"] == "not_analyzed"


def test_unanalyzed_shots_are_returned_and_flagged(client, seeded):
    r = _get(client, CAND.format(p=seeded["project_id"]), eligibility="all", limit=100)
    unanalysed = [
        i for i in r.json()["items"]
        if i["observations"]["static"]["state"] == "not_analyzed"
        and i["observations"]["video_native"]["state"] == "not_analyzed"
        and i["observations"]["multiframe"]["state"] == "not_analyzed"
    ]
    assert len(unanalysed) == 10, "shots with no reading must still be candidates"
    for i in unanalysed:
        assert "no_valid_model_reading" in i["limitations"]


# ---------------------------------------------------------------- filtering
def test_rating_filter(client, seeded):
    r = _get(client, CAND.format(p=seeded["project_id"]), rating="favorite", eligibility="all")
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["segment_id"] == seeded["segments"][2]


def test_story_role_filter(client, seeded):
    r = _get(client, CAND.format(p=seeded["project_id"]), story_role="开场", eligibility="all")
    assert r.json()["total"] == 1


def test_asset_filter(client, seeded):
    r = _get(client, CAND.format(p=seeded["project_id"]), asset_id=seeded["assets"][0],
             eligibility="all", limit=100)
    assert r.json()["total"] == 4


def test_scene_tag_only_matches_current_static_readings(client, seeded):
    r = _get(client, CAND.format(p=seeded["project_id"]), scene_tag="nature", eligibility="all")
    body = r.json()
    # Only segment 0 has a static reading; unanalysed shots must not match.
    assert body["total"] == 1
    assert body["items"][0]["segment_id"] == seeded["segments"][0]

    none = _get(client, CAND.format(p=seeded["project_id"]), scene_tag="beach", eligibility="all")
    assert none.json()["total"] == 0


def test_q_searches_human_notes(client, seeded):
    r = _get(client, CAND.format(p=seeded["project_id"]), q="日落", eligibility="all")
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["segment_id"] == seeded["segments"][5]


def test_q_searches_observed_subjects(client, seeded):
    r = _get(client, CAND.format(p=seeded["project_id"]), q="lake", eligibility="all")
    assert r.json()["total"] >= 1
    assert any(i["segment_id"] == seeded["segments"][0] for i in r.json()["items"])


def test_excluded_view_shows_reasons(client, seeded):
    r = _get(client, CAND.format(p=seeded["project_id"]), eligibility="excluded")
    body = r.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["eligibility"] == "excluded"
    assert "human_excluded" in item["exclusion_reasons"]


def test_needs_review_view_is_separate_from_eligible(client, seeded):
    r = _get(client, CAND.format(p=seeded["project_id"]), eligibility="needs_review")
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["eligibility"] == "needs_review"


# ---------------------------------------------------------------- validation
@pytest.mark.parametrize(
    "params",
    [
        {"limit": 0},
        {"limit": 101},
        {"offset": -1},
        {"eligibility": "bogus"},
        {"sort": "random"},
        {"q": "x" * 500},
        {"scene_tag": "y" * 100},
    ],
)
def test_invalid_parameters_are_rejected(client, seeded, params):
    r = _get(client, CAND.format(p=seeded["project_id"]), **params)
    assert r.status_code == 422, f"{params} should be rejected, got {r.status_code}"


def test_unknown_project_returns_404(client):
    bogus = str(uuid.uuid4())
    assert _get(client, CAND.format(p=bogus)).status_code == 404
    assert _get(client, CTX.format(p=bogus)).status_code == 404


def test_requests_without_a_token_are_refused(client, seeded):
    # The client fixture carries the token; a raw request must not.
    r = client.get(CAND.format(p=seeded["project_id"]), headers={"X-Access-Token": "wrong"})
    assert r.status_code in (401, 403)


# ---------------------------------------------------------------- paging
def test_pagination_is_stable_and_complete(client, seeded):
    seen: list[str] = []
    for offset in (0, 4, 8):
        r = _get(client, CAND.format(p=seeded["project_id"]), eligibility="all",
                 sort="timeline", diversify=False, offset=offset, limit=4)
        seen.extend(i["segment_id"] for i in r.json()["items"])
    assert len(seen) == 12
    assert len(set(seen)) == 12, "paging must not repeat or drop rows"


def test_repeated_calls_return_identical_order(client, seeded):
    a = _get(client, CAND.format(p=seeded["project_id"]), eligibility="all", limit=100).json()
    b = _get(client, CAND.format(p=seeded["project_id"]), eligibility="all", limit=100).json()
    assert [i["segment_id"] for i in a["items"]] == [i["segment_id"] for i in b["items"]]


def test_timeline_sort_is_ordered_by_asset_then_start(client, seeded):
    r = _get(client, CAND.format(p=seeded["project_id"]), eligibility="all",
             sort="timeline", diversify=False, limit=100)
    keys = [(i["asset_id"], i["source_window"]["start_sec"]) for i in r.json()["items"]]
    assert keys == sorted(keys)


def test_editorial_sort_puts_human_favourites_first(client, seeded):
    r = _get(client, CAND.format(p=seeded["project_id"]), eligibility="all",
             diversify=False, limit=100)
    first = r.json()["items"][0]
    assert first["segment_id"] == seeded["segments"][2]
    assert first["editorial_hints"]["priority"] == "preferred"


def test_diversity_spreads_across_assets(client, seeded):
    r = _get(client, CAND.format(p=seeded["project_id"]), eligibility="all",
             diversify=True, limit=6)
    assets = [i["asset_id"] for i in r.json()["items"]]
    # Six items over three assets must not all come from one clip.
    assert len(set(assets)) >= 3, assets


# ---------------------------------------------------------------- read-only
def test_browsing_creates_no_jobs_versions_or_edits(client, seeded):
    with session_scope() as s:
        before = (
            s.query(Job).filter(Job.project_id == seeded["project_id"]).count(),
            s.query(StoryboardVersion).count(),
            s.query(EditPlanShot).filter(EditPlanShot.project_id == seeded["project_id"]).count(),
        )
    for _ in range(3):
        _get(client, CAND.format(p=seeded["project_id"]), eligibility="all", limit=100)
        _get(client, CTX.format(p=seeded["project_id"]))
    with session_scope() as s:
        after = (
            s.query(Job).filter(Job.project_id == seeded["project_id"]).count(),
            s.query(StoryboardVersion).count(),
            s.query(EditPlanShot).filter(EditPlanShot.project_id == seeded["project_id"]).count(),
        )
    # Compare deltas: the shared test database may already hold other projects.
    assert before == after, "browsing must not create jobs, versions or edit rows"


def test_reading_does_not_mutate_human_data(client, seeded):
    with session_scope() as s:
        s.get(Segment, seeded["segments"][2]).user_rating  # noqa: B018 - prime session
    _get(client, CAND.format(p=seeded["project_id"]), eligibility="all", limit=100)
    with session_scope() as s:
        seg = s.get(Segment, seeded["segments"][2])
        assert seg.user_rating == "favorite"
        assert seg.story_role == "开场"


# ---------------------------------------------------------------- context
def test_context_returns_planning_input_not_a_story(client, seeded):
    r = _get(client, CTX.format(p=seeded["project_id"]), max_candidates=5)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["schema_version"] == "1.0"
    assert body["project_id"] == seeded["project_id"]
    assert body["input_digest"]
    assert len(body["candidate_segment_ids"]) <= 5
    assert body["needs_more_analysis"] is True
    assert body["uncovered_segment_count"] > 0
    # No story artefacts.
    assert "shots" not in body
    assert "storyline" not in body
    assert "sections" not in body


def test_context_excludes_human_excluded_shots(client, seeded):
    body = _get(client, CTX.format(p=seeded["project_id"]), max_candidates=50).json()
    assert seeded["segments"][4] not in body["candidate_segment_ids"]


def test_context_digest_is_stable_and_changes_with_brief(client, seeded):
    a = _get(client, CTX.format(p=seeded["project_id"])).json()
    b = _get(client, CTX.format(p=seeded["project_id"])).json()
    assert a["input_digest"] == b["input_digest"], "deterministic across identical calls"

    c = _get(client, CTX.format(p=seeded["project_id"]), theme="海边日落").json()
    assert c["input_digest"] != a["input_digest"], "a changed brief must change the digest"


def test_context_digest_changes_when_candidates_change(client, seeded):
    before = _get(client, CTX.format(p=seeded["project_id"])).json()["input_digest"]
    with session_scope() as s:
        seg = s.get(Segment, seeded["segments"][0])
        seg.user_rating = "favorite"
    after = _get(client, CTX.format(p=seeded["project_id"])).json()["input_digest"]
    assert before != after


def test_context_does_not_leak_paths_or_tokens(client, seeded):
    body = _get(client, CTX.format(p=seeded["project_id"])).json()
    blob = str(body)
    assert "/x/" not in blob
    assert "X-Access-Token" not in blob
    assert "request_usage" not in blob


def test_candidates_do_not_leak_paths_or_raw_model_payloads(client, seeded):
    body = _get(client, CAND.format(p=seeded["project_id"]), eligibility="all", limit=100).json()
    blob = str(body)
    assert "/x/" not in blob
    assert "source_path" not in blob
    assert "request_usage" not in blob
    assert "total_tokens" not in blob


def test_brief_text_is_not_treated_as_video_content(client, seeded):
    """A theme the user types must not turn into a factual claim."""
    body = _get(client, CTX.format(p=seeded["project_id"]), theme="极光下的雪山").json()
    blob = str(body["candidate_summaries"])
    assert "极光" not in blob
    assert "雪山" not in blob
    assert "not verified as video content" in str(body["brief"]["note"])
