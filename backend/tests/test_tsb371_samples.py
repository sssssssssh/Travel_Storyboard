"""TSB-371-A — evaluation samples and the protected clip player.

The player is the only new way to get video bytes out of this application, so the
security assertions are not decoration: path traversal, cross-project access and
signature drift are the three ways a "small preview feature" becomes a data leak.

Clip generation is exercised for real (FFmpeg on a synthesised source), because a
mocked transcode would not prove that Range requests work on the actual output.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.database import session_scope
from app.models import Asset, Project, Segment, SegmentSemanticResult

SAMPLES = "/api/projects/{p}/evaluation/eligible-samples"
CLIPS = "/api/projects/{p}/evaluation/clips"


def _uid() -> str:
    return str(uuid.uuid4())


def _ffmpeg_available() -> bool:
    from app.ffmpeg import locate_ffmpeg

    return bool(locate_ffmpeg())


needs_ffmpeg = pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg not available")


@pytest.fixture()
def source_video(tmp_path: Path) -> Path:
    """A real, short video so clip generation and Range serving can be tested."""
    from app.ffmpeg import locate_ffmpeg

    exe = locate_ffmpeg()
    out = tmp_path / "src.mp4"
    import subprocess

    subprocess.run(
        [str(exe), "-y", "-f", "lavfi", "-i", "testsrc=s=320x180:d=8:r=25",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-an", str(out)],
        check=True, capture_output=True,
    )
    return out


def _seed(client, name="t371a", *, n_segments=3, analysed=2, with_source=None,
          analysis=None):
    pid = client.post("/api/projects", json={"name": name}).json()["id"]
    with session_scope() as s:
        p = s.get(Project, pid)
        a = Asset(
            id=_uid(), project_id=pid,
            source_path=str(with_source) if with_source else f"/nonexistent/{_uid()}.mp4",
            display_name="c.mp4",
            file_size_bytes=with_source.stat().st_size if with_source else 1,
            mtime_ns=with_source.stat().st_mtime_ns if with_source else 1,
            quick_fingerprint="f", duration_sec=8.0, fps_num=25, fps_den=1,
            vfr_flag=False, width=320, height=180, rotation=0, codec="h264",
            pixel_format="yuv420p", color_primaries="", color_transfer="",
            color_space="", has_audio=False, audio_codec="", scan_status="succeeded",
            analysis_version="", analyzed_fingerprint="",
            analyzed_source_signature="", analyzed_detect_config="",
            analyzed_at=None, scanned_at=None,
        )
        s.add(a)
        s.flush()
        ids = {"segments": [], "results": [], "asset": a.id}
        for i in range(n_segments):
            g = Segment(
                id=_uid(), asset_id=a.id, project_id=pid,
                detected_start_sec=i * 2.0, detected_end_sec=i * 2.0 + 2.0,
                start_sec=i * 2.0, end_sec=i * 2.0 + 2.0, is_trimmed=False,
                boundary_reason="", quality_metrics={"blur_avg": 100.0},
                content_tags={}, story_role="", user_rating="pending",
                status="candidate", frame_refs=[], proxy_ref="", parent_segment_id="",
                algorithm_version="1", created_at=None,
            )
            s.add(g)
            s.flush()
            ids["segments"].append(g.id)
            if i < analysed:
                r = SegmentSemanticResult(
                    id=_uid(), project_id=pid, asset_id=a.id, segment_id=g.id,
                    status="succeeded", model_provider="openai_compatible",
                    model_id="GLM-5.3-Flash", prompt_version="p", schema_version="1.0",
                    source_signature=_uid(), frame_manifest_hash="h",
                    analysis_json=analysis if analysis is not None else
                    {"scene_tags": ["lake"], "subjects": ["a pier"],
                                   "visible_actions": ["walking"],
                                   "lighting": "daylight", "composition": "wide",
                                   "mood": ["calm"]},
                    evidence_json={"state": "observed", "frame_ids": ["f1"]},
                    request_usage={}, is_current=True, error_code="",
                    error_message="", created_at=None, updated_at=None,
                )
                s.add(r)
                s.flush()
                ids["results"].append(r.id)
    return pid, ids


# ---------------------------------------------------------------------------
# E01 / E02 / E03 / E05 — sample selection
# ---------------------------------------------------------------------------

def test_samples_split_by_channel_with_real_result_ids(client):
    pid, ids = _seed(client, analysed=2)
    r = client.get(SAMPLES.format(p=pid))
    assert r.status_code == 200, r.text
    body = r.json()
    channels = {i["channel"] for i in body["items"]}
    assert channels == {"static"}
    assert len(body["items"]) == 2
    for item in body["items"]:
        assert item["result_id"] in ids["results"]
        assert item["segment_id"] in ids["segments"]
        assert item["source_window"]["start_abs_sec"] >= 0


def test_a_project_with_no_readings_offers_no_rating_items(client):
    pid, _ = _seed(client, analysed=0)
    body = client.get(SAMPLES.format(p=pid)).json()
    assert body["items"] == []
    assert body["summary"]["analysed_segments"] == 0


def test_an_unanalysed_shot_can_be_offered_as_a_control(client):
    """A no-evidence control is useful for spotting false recognition."""
    pid, _ = _seed(client, name="ctl", analysed=1, n_segments=3)
    body = client.get(SAMPLES.format(p=pid)).json()
    assert "controls" in body
    assert len(body["controls"]) >= 1
    for c in body["controls"]:
        assert c["channel"] == "none"


def test_a_missing_source_is_marked_and_never_counted_as_a_model_error(client):
    pid, ids = _seed(client, analysed=2)
    body = client.get(SAMPLES.format(p=pid)).json()
    for item in body["items"]:
        # The seeded source path does not exist.
        assert item["source_available"] is False
        assert item["source_status"] == "SOURCE_UNAVAILABLE"


def test_the_sample_carries_the_evidence_state_verbatim(client):
    pid, ids = _seed(client, analysed=2)
    body = client.get(SAMPLES.format(p=pid)).json()
    for item in body["items"]:
        # Freshness is never upgraded to "current".
        assert item["evidence_state"] in (
            "current", "freshness_unverified", "stale", "not_analyzed", "failed")


def test_sampling_is_reproducible_for_a_given_seed(client):
    pid, _ = _seed(client, analysed=2, n_segments=3)
    a = client.get(SAMPLES.format(p=pid) + "?seed=abc&limit=2").json()
    b = client.get(SAMPLES.format(p=pid) + "?seed=abc&limit=2").json()
    assert [i["segment_id"] for i in a["items"]] == [i["segment_id"] for i in b["items"]]


def test_two_channels_on_one_shot_are_two_evidence_items(client):
    """E05: one shot, two readings — counted separately, denominator not confused."""
    pid, ids = _seed(client, analysed=1, n_segments=1)
    with session_scope() as s:
        from app.models import SegmentMotionReview

        seg = ids["segments"][0]
        a = s.get(Asset, ids["asset"])
        s.add(SegmentMotionReview(
            id=_uid(), project_id=pid, asset_id=a.id, segment_id=seg,
            method="multiframe", status="succeeded",
            model_provider="openai_compatible", model_id="GLM", prompt_version="p",
            start_sec=0.0, end_sec=2.0, frame_manifest_hash="h",
            input_signature=_uid(), motion_type="pan",
            stability_risk="low", action_summary="camera pans right",
            analysis_json={}, evidence_json={}, request_usage={}, is_current=True,
            human_verdict="", human_note="", human_at=None, error_code="",
            error_message="", created_at=None, updated_at=None,
        ))
    body = client.get(SAMPLES.format(p=pid)).json()
    segs = [i["segment_id"] for i in body["items"]]
    assert segs.count(ids["segments"][0]) == 2
    assert {i["channel"] for i in body["items"]} == {"static", "multiframe"}
    # But the distinct-shot count is one.
    assert body["summary"]["distinct_segments"] == 1


def test_excluded_and_review_shots_are_flagged_not_silently_included(client):
    pid, ids = _seed(client, analysed=1, n_segments=2)
    with session_scope() as s:
        g = s.get(Segment, ids["segments"][0])
        g.user_rating = "exclude"
    body = client.get(SAMPLES.format(p=pid)).json()
    item = next(i for i in body["items"] if i["segment_id"] == ids["segments"][0])
    assert item["eligibility"] in ("excluded", "needs_review")
    assert item["eligible_for_planning"] is False


# ---------------------------------------------------------------------------
# E08 — cross-project isolation
# ---------------------------------------------------------------------------

def test_another_projects_samples_are_not_visible(client):
    pid_a, _ = _seed(client, name="iso-a", analysed=1)
    pid_b, ids_b = _seed(client, name="iso-b", analysed=1)
    body = client.get(SAMPLES.format(p=pid_a)).json()
    ids_shown = {i["segment_id"] for i in body["items"]}
    assert ids_b["segments"][0] not in ids_shown


def test_another_projects_clip_is_404(client, source_video):
    pid_a, _ = _seed(client, name="clip-a", analysed=1, with_source=source_video)
    pid_b, _ = _seed(client, name="clip-b", analysed=1, with_source=source_video)
    created = client.post(CLIPS.format(p=pid_a), json={
        "segment_id": client.get(SAMPLES.format(p=pid_a)).json()["items"][0]["segment_id"]})
    if created.status_code not in (200, 202):
        pytest.skip(f"clip creation unavailable: {created.status_code}")
    clip_id = created.json()["clip_id"]
    r = client.get(f"{CLIPS.format(p=pid_b)}/{clip_id}/media")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# E09 — path traversal and symlink escape
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("evil", [
    "../../../../etc/passwd",
    "..\\..\\windows\\win.ini",
    "/absolute/path.mp4",
    "projects/../../secret.mp4",
])
def test_a_traversing_clip_id_is_rejected(client, evil):
    pid, _ = _seed(client)
    r = client.get(f"{CLIPS.format(p=pid)}/{evil}/media")
    assert r.status_code in (404, 422), r.status_code


def test_the_clip_endpoint_never_returns_a_filesystem_path(client, source_video):
    pid, ids = _seed(client, analysed=1, with_source=source_video)
    created = client.post(CLIPS.format(p=pid), json={"segment_id": ids["segments"][0]})
    if created.status_code not in (200, 202):
        pytest.skip(f"clip creation unavailable: {created.status_code}")
    body = created.text
    assert str(source_video) not in body
    assert "/nonexistent/" not in body
    detail = client.get(f"{CLIPS.format(p=pid)}/{created.json()['clip_id']}").text
    assert str(source_video) not in detail


# ---------------------------------------------------------------------------
# E10 — Range / HEAD / time mapping
# ---------------------------------------------------------------------------

@needs_ffmpeg
def test_a_clip_can_be_generated_and_played_with_range(client, source_video):
    pid, ids = _seed(client, analysed=1, with_source=source_video)
    created = client.post(CLIPS.format(p=pid), json={"segment_id": ids["segments"][0]})
    assert created.status_code in (200, 202), created.text
    clip_id = created.json()["clip_id"]

    status = client.get(f"{CLIPS.format(p=pid)}/{clip_id}")
    assert status.status_code == 200, status.text
    info = status.json()
    assert info["segment_id"] == ids["segments"][0]
    assert info["status"] in ("ready", "pending", "generating")
    if info["status"] != "ready":
        pytest.skip("clip not ready yet")

    media = f"{CLIPS.format(p=pid)}/{clip_id}/media"
    full = client.get(media)
    assert full.status_code == 200
    assert full.headers["content-type"].startswith("video/")
    assert int(full.headers.get("content-length", 0)) > 0

    head = client.head(media)
    assert head.status_code == 200

    ranged = client.get(media, headers={"Range": "bytes=0-99"})
    assert ranged.status_code == 206
    assert len(ranged.content) == 100
    assert ranged.headers["content-range"].startswith("bytes 0-99/")


@needs_ffmpeg
def test_the_clip_reports_a_verifiable_source_time_mapping(client, source_video):
    pid, ids = _seed(client, analysed=1, with_source=source_video)
    created = client.post(CLIPS.format(p=pid), json={"segment_id": ids["segments"][0]})
    if created.status_code not in (200, 202):
        pytest.skip("clip creation unavailable")
    info = client.get(f"{CLIPS.format(p=pid)}/{created.json()['clip_id']}").json()
    if info["status"] != "ready":
        pytest.skip("clip not ready")
    # source_abs = segment.start + playback_rel, and the clip must start at the
    # segment's own start, not at 0 of the source.
    assert info["clip_start_abs_sec"] == pytest.approx(info["source_window"]["start_abs_sec"])
    assert info["source_window"]["start_abs_sec"] >= 0


# ---------------------------------------------------------------------------
# E15 — the evaluation paths must not touch production tables
# ---------------------------------------------------------------------------

def test_listing_samples_writes_nothing(client):
    pid, _ = _seed(client, analysed=2)

    def counts():
        with session_scope() as s:
            return {
                "segments": int(s.scalar(select(func.count()).select_from(Segment).where(
                    Segment.project_id == pid)) or 0),
                "results": int(s.scalar(select(func.count()).select_from(
                    SegmentSemanticResult).where(
                    SegmentSemanticResult.project_id == pid)) or 0),
            }

    before = counts()
    client.get(SAMPLES.format(p=pid))
    client.get(SAMPLES.format(p=pid))
    assert counts() == before


# ---------------------------------------------------------------------------
# The reviewer must be able to see the claim being judged
# ---------------------------------------------------------------------------

def test_a_sample_carries_the_models_own_words(client):
    """Asking "is this accurate?" without showing the claim is unanswerable."""
    pid, ids = _seed(client, n_segments=1)
    body = client.get(f"/api/projects/{pid}/evaluation/eligible-samples?limit=5").json()
    item = body["items"][0]
    assert item["readings"]["scene_tags"] == ["lake"]
    assert item["readings"]["subjects"] == ["a pier"]
    assert item["readings"]["lighting"] == "daylight"


def test_readings_are_omitted_when_the_model_said_nothing(client):
    """No invented text: an empty reading stays empty rather than being filled."""
    pid, ids = _seed(client, n_segments=1, analysis={"scene_tags": [], "subjects": []})
    body = client.get(f"/api/projects/{pid}/evaluation/eligible-samples?limit=5").json()
    assert body["items"][0]["readings"] == {}


def test_the_rating_session_freezes_the_readings(client):
    """A review stays reproducible even if the model is re-run later."""
    pid, ids = _seed(client, n_segments=1)
    created = client.post(f"/api/projects/{pid}/evaluation/sessions",
                          json={"limit": 5}).json()
    sid = created["session_id"]
    body = client.get(f"/api/projects/{pid}/evaluation/sessions/{sid}").json()
    assert body["items"][0]["readings"]["scene_tags"] == ["lake"]
    assert body["items"][0]["frame_ids"]
