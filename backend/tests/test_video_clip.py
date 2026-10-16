"""TSB-327 — clip derivation, time mapping and source protection.

The pure-logic cases (time mapping, duration planning, source verification) run
without a toolchain because they are where the *correctness* rules live: they
decide whether a model's event window is allowed to be attached to a person's
edited shot. The real-encoding cases build actual media and are marked
``needs_ffmpeg`` so the mandatory-media job owns them.

The task book's V07–V11, V14 and V17 rows map onto this file.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from app.ffmpeg import probe_media, run, locate_ffmpeg
from app.services.vision.video_clip import (
    CLIP_UNREADABLE,
    COLOR_UNVERIFIED,
    NO_VIDEO_STREAM,
    SEGMENT_RANGE_INVALID,
    SOURCE_CHANGED,
    ClipArtifact,
    ClipSpec,
    VideoClipError,
    derive_clip,
    map_event_window,
    plan_clip_duration,
    probe_clip,
    strip_container_metadata_report,
    verify_source_unchanged,
)
from tests._media import needs_ffmpeg, require_ffmpeg


def _artifact(
    *,
    start: float = 10.0,
    duration: float = 5.0,
    **kwargs,
) -> ClipArtifact:
    base = dict(
        path=Path("clip.mp4"),
        segment_id="seg-1",
        asset_id="asset-1",
        source_clip_start_sec=start,
        source_clip_end_sec=start + duration,
        duration_sec=duration,
        width=1280,
        height=720,
        rotation=0,
        vfr=False,
        has_audio=False,
        size_bytes=1000,
        sha256="deadbeef",
        container="mov,mp4",
        color_unverified=False,
    )
    base.update(kwargs)
    return ClipArtifact(**base)


# ---------------------------------------------------------------------------
# time mapping: relative -> absolute
# ---------------------------------------------------------------------------

def test_a_window_maps_to_absolute_source_seconds():
    """V11: absolute = clip start + relative, never a re-based origin."""
    clip = _artifact(start=10.0, duration=5.0)
    window = map_event_window(
        clip=clip,
        segment_start_sec=10.0,
        segment_end_sec=15.0,
        start_rel_sec=1.5,
        end_rel_sec=3.0,
    )
    assert window.start_abs_sec == pytest.approx(11.5)
    assert window.end_abs_sec == pytest.approx(13.0)


def test_mapping_uses_the_clip_offset_not_the_segment_start():
    """A clip may start *after* the segment start (budget trim); the offset wins."""
    clip = _artifact(start=12.0, duration=3.0)
    window = map_event_window(
        clip=clip,
        segment_start_sec=10.0,
        segment_end_sec=20.0,
        start_rel_sec=0.0,
        end_rel_sec=1.0,
    )
    assert window.start_abs_sec == pytest.approx(12.0)


def test_a_negative_relative_time_is_rejected():
    clip = _artifact()
    with pytest.raises(VideoClipError) as exc:
        map_event_window(
            clip=clip, segment_start_sec=10.0, segment_end_sec=15.0,
            start_rel_sec=-1.0, end_rel_sec=2.0,
        )
    assert exc.value.code == SEGMENT_RANGE_INVALID


def test_negative_times_are_refused_for_their_own_reason():
    """Isolate the negativity check from the boundary check.

    With a wide segment the boundary comparison would accept the window, so the
    only thing left protecting the invariant is the explicit negativity guard.
    Without this case the guard could be deleted and the suite would stay green --
    which the reverse-verification script demonstrated before this test existed.
    """
    clip = _artifact(start=10.0, duration=5.0)
    with pytest.raises(VideoClipError) as exc:
        map_event_window(
            clip=clip,
            segment_start_sec=-100.0,
            segment_end_sec=100.0,
            start_rel_sec=-1.0,
            end_rel_sec=2.0,
        )
    assert exc.value.code == SEGMENT_RANGE_INVALID
    assert "must not be negative" in str(exc.value)


def test_a_negative_end_time_is_also_refused():
    clip = _artifact(start=10.0, duration=5.0)
    with pytest.raises(VideoClipError) as exc:
        map_event_window(
            clip=clip, segment_start_sec=-100.0, segment_end_sec=100.0,
            start_rel_sec=-5.0, end_rel_sec=-1.0,
        )
    assert exc.value.code == SEGMENT_RANGE_INVALID


def test_an_inverted_window_is_rejected():
    """V12: start >= end must not become a silently 'empty' observation."""
    clip = _artifact()
    with pytest.raises(VideoClipError) as exc:
        map_event_window(
            clip=clip, segment_start_sec=10.0, segment_end_sec=15.0,
            start_rel_sec=3.0, end_rel_sec=3.0,
        )
    assert exc.value.code == SEGMENT_RANGE_INVALID
    with pytest.raises(VideoClipError):
        map_event_window(
            clip=clip, segment_start_sec=10.0, segment_end_sec=15.0,
            start_rel_sec=3.0, end_rel_sec=1.0,
        )


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_times_are_rejected(bad):
    """V12: NaN/inf must never reach the database as a candidate window."""
    clip = _artifact()
    with pytest.raises(VideoClipError) as exc:
        map_event_window(
            clip=clip, segment_start_sec=10.0, segment_end_sec=15.0,
            start_rel_sec=bad, end_rel_sec=2.0,
        )
    assert exc.value.code == SEGMENT_RANGE_INVALID


def test_a_string_time_is_rejected():
    clip = _artifact()
    with pytest.raises(VideoClipError) as exc:
        map_event_window(
            clip=clip, segment_start_sec=10.0, segment_end_sec=15.0,
            start_rel_sec="1.0", end_rel_sec=2.0,
        )
    assert exc.value.code == SEGMENT_RANGE_INVALID


def test_a_window_beyond_the_clip_duration_is_rejected():
    clip = _artifact(duration=5.0)
    with pytest.raises(VideoClipError) as exc:
        map_event_window(
            clip=clip, segment_start_sec=10.0, segment_end_sec=15.0,
            start_rel_sec=4.0, end_rel_sec=9.0,
        )
    assert exc.value.code == SEGMENT_RANGE_INVALID
    assert "exceeds the clip duration" in str(exc.value)


def test_a_window_crossing_the_edited_boundary_is_rejected_not_clamped():
    """V11/C2: clamping would move a person's edit boundary. Refuse instead."""
    clip = _artifact(start=10.0, duration=5.0)
    with pytest.raises(VideoClipError) as exc:
        map_event_window(
            clip=clip,
            segment_start_sec=11.0,   # the person trimmed away the first second
            segment_end_sec=15.0,
            start_rel_sec=0.0,
            end_rel_sec=1.0,
        )
    assert exc.value.code == SEGMENT_RANGE_INVALID, (
        "an event reaching outside the edited interval must be refused, "
        "not silently moved inward"
    )


def test_float_noise_within_tolerance_is_accepted():
    """Models emit 3 decimals; a hair over the edge is not a real violation."""
    clip = _artifact(start=10.0, duration=5.0)
    window = map_event_window(
        clip=clip, segment_start_sec=10.0, segment_end_sec=15.0,
        start_rel_sec=0.0, end_rel_sec=5.001,
    )
    assert window.end_abs_sec == pytest.approx(15.001)


# ---------------------------------------------------------------------------
# duration planning
# ---------------------------------------------------------------------------

def test_a_long_shot_is_trimmed_to_the_budget_and_says_so():
    start, end, notes = plan_clip_duration(
        segment_start_sec=100.0, segment_end_sec=140.0, max_duration_sec=15.0
    )
    assert start == 100.0
    assert end == pytest.approx(115.0)
    assert notes and "15.0s" in notes[0]


def test_a_short_shot_is_used_whole():
    start, end, notes = plan_clip_duration(
        segment_start_sec=5.0, segment_end_sec=12.0, max_duration_sec=15.0
    )
    assert (start, end) == (5.0, 12.0)
    assert notes == []


def test_an_empty_segment_interval_is_refused():
    with pytest.raises(VideoClipError) as exc:
        plan_clip_duration(segment_start_sec=5.0, segment_end_sec=5.0)
    assert exc.value.code == SEGMENT_RANGE_INVALID


def test_the_window_never_leaves_the_segment():
    """V11: derived clip must stay inside the editable interval."""
    start, end, _ = plan_clip_duration(
        segment_start_sec=7.0, segment_end_sec=9.0, max_duration_sec=15.0
    )
    assert start >= 7.0 and end <= 9.0


# ---------------------------------------------------------------------------
# source protection (no FFmpeg needed for these)
# ---------------------------------------------------------------------------

class _Asset:
    def __init__(self, *, size=None, mtime_ns=None):
        self.file_size_bytes = size
        self.mtime_ns = mtime_ns


def test_verify_source_accepts_an_unchanged_file(tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x" * 100)
    stat = f.stat()
    asset = _Asset(size=stat.st_size, mtime_ns=stat.st_mtime_ns)
    info = verify_source_unchanged(asset, f)
    assert info["size"] == 100


def test_verify_source_detects_a_size_change(tmp_path):
    """V14: a swapped file must fail loudly, not produce a clip of other footage."""
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x" * 100)
    asset = _Asset(size=999, mtime_ns=f.stat().st_mtime_ns)
    with pytest.raises(VideoClipError) as exc:
        verify_source_unchanged(asset, f)
    assert exc.value.code == SOURCE_CHANGED


def test_verify_source_detects_an_mtime_change(tmp_path):
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x" * 100)
    asset = _Asset(size=100, mtime_ns=12345)
    with pytest.raises(VideoClipError) as exc:
        verify_source_unchanged(asset, f)
    assert exc.value.code == SOURCE_CHANGED


def test_verify_source_reports_a_missing_file_as_source_changed(tmp_path):
    asset = _Asset(size=100, mtime_ns=1)
    with pytest.raises(VideoClipError) as exc:
        verify_source_unchanged(asset, tmp_path / "gone.mp4")
    assert exc.value.code == SOURCE_CHANGED


def test_verify_source_tolerates_an_asset_without_recorded_facts(tmp_path):
    """Legacy rows may lack size/mtime; that is not a change."""
    f = tmp_path / "a.mp4"
    f.write_bytes(b"x" * 10)
    assert verify_source_unchanged(_Asset(), f)["size"] == 10


# ---------------------------------------------------------------------------
# real encoding
# ---------------------------------------------------------------------------

@pytest.fixture()
def clip_env(client):
    """A real project + asset + vision job row.

    ``create_owned_staging`` deliberately refuses to hand out a staging directory
    unless the project, the job (of the right type) and the asset all exist, so a
    test that passed fake ids would be testing a code path the product never uses.
    """
    import uuid

    from app.database import session_scope
    from app.models import Asset, Job

    res = client.post("/api/projects", json={"name": "clip-test", "source_roots": []})
    assert res.status_code in (200, 201), res.text
    project_id = res.json()["id"]

    asset_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    with session_scope() as s:
        s.add(Asset(id=asset_id, project_id=project_id, source_path="/tmp/src.mp4",
                    display_name="src.mp4"))
        s.add(Job(id=job_id, project_id=project_id, job_type="vision_analyze",
                  status="running", config_snapshot={}, stats={},
                  failures={"count": 0, "items": []}))
    return {"project_id": project_id, "asset_id": asset_id, "job_id": job_id}


def _cleanup(env, segment_id: str) -> None:
    """Remove the owned staging tree this test created."""
    import shutil

    from app.paths import project_cache_dir

    stage = project_cache_dir(env["project_id"]) / "staging" / env["asset_id"]
    shutil.rmtree(stage, ignore_errors=True)


def _derive(env, source: Path, *, start: float, end: float, segment_id: str = "seg-1",
            max_duration_sec: float = 15.0, asset=None, cancel_check=None):
    asset_id = env["asset_id"]
    return derive_clip(
        project_id=env["project_id"],
        job_id=env["job_id"],
        asset=asset,
        cancel_check=cancel_check,
        spec=ClipSpec(
            segment_id=segment_id,
            asset_id=asset_id,
            source_path=source,
            start_sec=start,
            end_sec=end,
            max_duration_sec=max_duration_sec,
        ),
    )


def _make_source(
    path: Path,
    *,
    duration: float = 20.0,
    size: str = "1280x720",
    fps: int = 30,
    audio: bool = False,
    rotate: bool = False,
    codec: str = "libx264",
    hdr: bool = False,
) -> Path:
    """Build a real test source. Colour changes over time so a clip of the wrong
    window would be visibly different, not merely shorter."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [locate_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", f"testsrc=size={size}:rate={fps}:duration={duration}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}"]
    vf = "drawtext=text='%{eif\\:t\\:d}':x=20:y=20:fontsize=64:fontcolor=white"
    if rotate:
        vf += ",scale=1280:720"
    cmd += ["-vf", vf]
    if rotate:
        cmd += ["-metadata:s:v:0", "rotate=90"]
    if hdr:
        cmd += ["-color_primaries", "bt2020", "-color_trc", "smpte2084",
                "-colorspace", "bt2020nc"]
    cmd += ["-c:v", codec, "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "aac", "-shortest"] if audio else ["-an"]
    cmd += [str(path)]
    run(cmd, expect_output=path)
    return path


@needs_ffmpeg
def test_a_derived_clip_is_readable_and_silent(tmp_path, clip_env):
    require_ffmpeg()
    source = _make_source(tmp_path / "src.mp4", duration=20.0, audio=True)
    artifact = _derive(clip_env, source, start=2.0, end=8.0)
    try:
        assert artifact.path.is_file()
        assert artifact.duration_sec == pytest.approx(6.0, abs=0.25)
        assert artifact.has_audio is False, "derived clips must be silent"
        assert artifact.height <= 720
        assert artifact.sha256
    finally:
        _cleanup(clip_env, "seg-1")


@needs_ffmpeg
def test_the_source_file_is_never_modified(tmp_path, clip_env):
    """C1: byte-for-byte and mtime unchanged after derivation."""
    require_ffmpeg()
    source = _make_source(tmp_path / "src.mp4", duration=10.0)
    before_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    before_mtime = source.stat().st_mtime_ns

    artifact = _derive(clip_env, source, start=1.0, end=5.0)
    try:
        assert hashlib.sha256(source.read_bytes()).hexdigest() == before_hash
        assert source.stat().st_mtime_ns == before_mtime
    finally:
        _cleanup(clip_env, "seg-1")


@needs_ffmpeg
def test_personal_container_metadata_does_not_survive_derivation(tmp_path, clip_env):
    """V07/C5: creation_time, device tags and GPS must not travel with the clip.

    The assertion is deliberately split. *Personal* tags must be gone; the
    ``encoder=Lavf<version>`` tag is reported separately because FFmpeg's MP4
    muxer re-stamps it on every write and no flag combination removes it
    (measured on FFmpeg 9.0.2 -- ``-map_metadata -1``, ``+bitexact`` and
    ``-metadata encoder=`` were all tried). It names the encoder version, not the
    user, so calling it a privacy leak would be an inaccurate finding.
    """
    require_ffmpeg()
    source = _make_source(tmp_path / "src.mp4", duration=8.0)
    tagged = tmp_path / "tagged.mp4"
    run([locate_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
         "-c", "copy", "-metadata", "creation_time=2026-01-02T03:04:05Z",
         "-metadata", "make=SecretPhone", "-metadata", "model=ModelX",
         "-metadata", "location=+41.0000+029.0000/", str(tagged)], expect_output=tagged)
    # Sanity check: the source really does carry the tags we expect to lose, so
    # this test cannot pass merely by having had nothing to strip.
    planted = strip_container_metadata_report(tagged)["personal_tags"]
    assert planted, "fixture did not actually plant metadata"

    artifact = _derive(clip_env, tagged, start=0.0, end=4.0)
    try:
        report = strip_container_metadata_report(artifact.path)
        assert report["readable"] is True
        assert report["personal_tags"] == {}, (
            f"personal metadata survived derivation: {report['personal_tags']}"
        )
    finally:
        _cleanup(clip_env, "seg-1")


@needs_ffmpeg
def test_probe_clip_refuses_an_audio_bearing_artifact(tmp_path):
    """The silent-clip assumption is enforced on the artifact, not just requested.

    ``-an`` normally guarantees this, so the guard is unreachable through
    :func:`derive_clip` -- which is exactly why it needs a direct test. If a
    future change dropped ``-an`` (or FFmpeg changed behaviour), this is the check
    that turns a silent privacy failure into a loud error.
    """
    require_ffmpeg()
    clip = tmp_path / "with_audio.mp4"
    run([locate_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=1",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(clip)], expect_output=clip)

    with pytest.raises(VideoClipError) as exc:
        probe_clip(
            path=clip, segment_id="s", asset_id="a",
            source_clip_start_sec=0.0, source_clip_end_sec=1.0,
        )
    assert exc.value.code == CLIP_UNREADABLE
    assert "audio" in str(exc.value)


def test_probe_clip_refuses_a_missing_artifact(tmp_path):
    with pytest.raises(VideoClipError) as exc:
        probe_clip(
            path=tmp_path / "never_written.mp4", segment_id="s", asset_id="a",
            source_clip_start_sec=0.0, source_clip_end_sec=1.0,
        )
    assert exc.value.code == CLIP_UNREADABLE
    assert "not created" in str(exc.value)


@needs_ffmpeg
def test_the_derived_clip_has_only_a_video_stream(tmp_path, clip_env):
    """V07: silent by construction, with no side-channel streams."""
    require_ffmpeg()
    from app.ffmpeg import probe

    source = _make_source(tmp_path / "src.mp4", duration=8.0, audio=True)
    artifact = _derive(clip_env, source, start=0.0, end=4.0)
    try:
        streams = probe(artifact.path).get("streams") or []
        kinds = {s.get("codec_type") for s in streams}
        assert "audio" not in kinds
        assert "subtitle" not in kinds
        assert "data" not in kinds
        assert kinds == {"video"}
    finally:
        _cleanup(clip_env, "seg-1")


@needs_ffmpeg
def test_a_custom_handler_name_does_not_travel_into_the_clip(tmp_path, clip_env):
    """A stream's ``handler_name`` can hold a camera-supplied user string.

    Measured: FFmpeg writes its own constant ("VideoHandler") on output, so a
    custom source value is replaced rather than carried over. This test pins that
    behaviour, because if a future FFmpeg propagated the tag instead, the clip
    would silently start leaking it.

    The value -- not the key -- decides classification: the muxer's own constant
    is not user data, and reporting it as such would create a false positive that
    buries real findings.
    """
    require_ffmpeg()
    source = _make_source(tmp_path / "src.mp4", duration=8.0)
    tagged = tmp_path / "tagged_handler.mp4"
    run([locate_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
         "-c", "copy", "-metadata:s:v:0", "handler_name=SecretPerson",
         "-metadata", "title=MyHoliday", str(tagged)], expect_output=tagged)
    planted = strip_container_metadata_report(tagged)["personal_tags"]
    assert "handler_name" in planted, "fixture did not plant a custom handler_name"

    artifact = _derive(clip_env, tagged, start=0.0, end=4.0)
    try:
        report = strip_container_metadata_report(artifact.path)
        assert "handler_name" not in report["personal_tags"], (
            "a custom handler_name reached the derived clip"
        )
        assert "title" not in report["personal_tags"]
    finally:
        _cleanup(clip_env, "seg-1")


@needs_ffmpeg
def test_a_clip_shorter_than_its_shot_uses_the_first_seconds(tmp_path, clip_env):
    """The derived window is the head of the shot, and the offset records it."""
    require_ffmpeg()
    source = _make_source(tmp_path / "src.mp4", duration=60.0)
    artifact = _derive(clip_env, source, start=10.0, end=50.0, max_duration_sec=5.0)
    try:
        assert artifact.source_clip_start_sec == pytest.approx(10.0)
        assert artifact.duration_sec == pytest.approx(5.0, abs=0.25)
        assert any("first 5.0s" in n for n in artifact.notes)
    finally:
        _cleanup(clip_env, "seg-1")


@needs_ffmpeg
def test_an_hdr_transfer_characteristic_is_flagged_color_unverified(tmp_path, clip_env, monkeypatch):
    """V10: never present a tone-mapped guess as accurate colour.

    The flag is driven by the source's transfer characteristic, so the decision is
    asserted directly by feeding the classifier the characteristics it must react
    to. An earlier version of this test skipped when the local FFmpeg did not
    preserve an HDR tag -- which the skip guard correctly rejected as an
    *unregistered* skip, because "this build tags HDR differently" is not a
    platform fact and must not be allowed to silently remove the only test of the
    COLOR_UNVERIFIED path.
    """
    require_ffmpeg()
    from app.services.vision import video_clip as vc

    class _Info:
        def __init__(self, transfer):
            self.video = type("V", (), {"color_transfer": transfer})()

    for transfer in ("smpte2084", "arib-std-b67", "SMPTE2084"):
        assert vc._is_hdr(_Info(transfer)) is True, transfer
    for transfer in ("bt709", "", "smpte170m"):
        assert vc._is_hdr(_Info(transfer)) is False, transfer


@needs_ffmpeg
def test_an_hdr_flagged_clip_carries_the_note_and_the_flag(tmp_path, clip_env, monkeypatch):
    """The flag must also reach the artifact, not only the classifier.

    ``_is_hdr`` is forced True here because whether a given FFmpeg build preserves
    the transfer tag is an environment detail; the behaviour under test -- that an
    HDR source produces ``color_unverified`` plus an explanatory note -- must hold
    regardless.
    """
    require_ffmpeg()
    from app.services.vision import video_clip as vc

    monkeypatch.setattr(vc, "_is_hdr", lambda info: True)
    source = _make_source(tmp_path / "hdr.mp4", duration=8.0, hdr=True)
    artifact = _derive(clip_env, source, start=0.0, end=4.0)
    try:
        assert artifact.color_unverified is True
        assert any(COLOR_UNVERIFIED in note for note in artifact.notes)
    finally:
        _cleanup(clip_env, "seg-1")


@needs_ffmpeg
def test_an_sdr_clip_is_not_flagged_color_unverified(tmp_path, clip_env, monkeypatch):
    """The flag must not be permanently on, or it would stop meaning anything."""
    require_ffmpeg()
    from app.services.vision import video_clip as vc

    monkeypatch.setattr(vc, "_is_hdr", lambda info: False)
    source = _make_source(tmp_path / "sdr.mp4", duration=8.0)
    artifact = _derive(clip_env, source, start=0.0, end=4.0)
    try:
        assert artifact.color_unverified is False
        assert not any(COLOR_UNVERIFIED in note for note in artifact.notes)
    finally:
        _cleanup(clip_env, "seg-1")


@needs_ffmpeg
def test_a_rotated_source_is_reported_and_normalised(tmp_path, clip_env):
    """V08: rotation must be surfaced, not silently ignored."""
    require_ffmpeg()
    source = _make_source(tmp_path / "rot.mp4", duration=8.0, rotate=True)
    artifact = _derive(clip_env, source, start=0.0, end=4.0)
    try:
        assert artifact.width > 0 and artifact.height > 0
        # Either the rotation was baked into pixels (rotation now 0) or it is
        # reported; silently dropping it is what is forbidden.
        assert artifact.rotation in (0, 90, 180, 270)
    finally:
        _cleanup(clip_env, "seg-1")


@needs_ffmpeg
def test_a_source_that_changed_on_disk_is_refused(tmp_path, clip_env):
    """V14: the clip must not be derived from footage we did not analyse."""
    require_ffmpeg()
    source = _make_source(tmp_path / "src.mp4", duration=8.0)
    asset = _Asset(size=source.stat().st_size, mtime_ns=999999)  # wrong mtime
    with pytest.raises(VideoClipError) as exc:
        _derive(clip_env, source, start=0.0, end=4.0, asset=asset)
    assert exc.value.code == SOURCE_CHANGED
    _cleanup(clip_env, "seg-1")


@needs_ffmpeg
def test_a_non_video_file_is_refused(tmp_path, clip_env):
    require_ffmpeg()
    junk = tmp_path / "notvideo.mp4"
    junk.write_bytes(b"this is not a video" * 50)
    with pytest.raises(VideoClipError) as exc:
        _derive(clip_env, junk, start=0.0, end=1.0)
    assert exc.value.code in (CLIP_UNREADABLE, NO_VIDEO_STREAM)
    _cleanup(clip_env, "seg-1")


@needs_ffmpeg
def test_probe_clip_reports_the_measured_duration_not_the_requested_one(tmp_path, clip_env):
    """The artifact, not the encoder's intent, is the evidence."""
    require_ffmpeg()
    source = _make_source(tmp_path / "src.mp4", duration=20.0)
    artifact = _derive(clip_env, source, start=5.0, end=9.0)
    try:
        measured = probe_clip(
            path=artifact.path, segment_id="seg-1", asset_id=clip_env["asset_id"],
            source_clip_start_sec=5.0, source_clip_end_sec=9.0,
        )
        assert measured.duration_sec == pytest.approx(artifact.duration_sec, abs=0.01)
        assert measured.sha256 == artifact.sha256
        assert measured.size_bytes == artifact.path.stat().st_size
    finally:
        _cleanup(clip_env, "seg-1")


@needs_ffmpeg
def test_a_cancelled_derivation_stops_before_encoding(tmp_path, clip_env):
    """V17/C7: cancellation is honoured and no clip is published."""
    require_ffmpeg()
    source = _make_source(tmp_path / "src.mp4", duration=30.0)
    from app.ffmpeg import FFmpegCancelled

    with pytest.raises(FFmpegCancelled):
        _derive(clip_env, source, start=0.0, end=10.0, cancel_check=lambda: True)
    _cleanup(clip_env, "seg-1")
