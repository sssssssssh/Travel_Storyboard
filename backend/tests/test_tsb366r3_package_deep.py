"""TSB-366-R3 — a package built in a deep workspace must keep its frames.

The unit tests prove ``_safe_save`` works past MAX_PATH. This one proves the
*product* consequence: representative frames really appear in ``segments.json``,
in the zip, and on disk — not merely that the job says "succeeded".

It builds its own deep workspace so coverage does not depend on where the
configured temp directory happens to live; that dependence is what made the
original defect look intermittent.
"""

from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select

from app.database import session_scope
from app.ffmpeg import long_path, safe_is_file, safe_mkdir, safe_open
from app.models import Asset, Frame, Segment, new_id
from app.paths import frames_dir, resolve_in_project
from app.services import review_package as rp
from app.services.analyzer import _analyze_asset
from app.services.indexer import index_one

MAX_PATH = 260


def _ffmpeg() -> str:
    from app.ffmpeg import locate_ffmpeg

    exe = locate_ffmpeg()
    if not exe:
        pytest.skip("ffmpeg is required for the package integration test")
    return str(exe)


@pytest.fixture()
def project(client):
    """A fresh project (same shape as the ffmpeg-integration fixture)."""
    res = client.post("/api/projects", json={"name": "r3-deep", "source_roots": []})
    assert res.status_code in (200, 201), res.text
    return res.json()


@pytest.fixture()
def deep_output_dir(tmp_path: Path) -> Path:
    """An ``output_dir`` deep enough that package files cross MAX_PATH."""
    root = tmp_path / ("deep" + "w" * 24)
    safe_mkdir(root)
    cur = root
    while len(str(cur)) < 250:
        nxt = cur / ("p" * 32)
        safe_mkdir(nxt)
        cur = nxt
    return cur


def _analyzed_asset(project_id: str, tmp_path: Path) -> str:
    """Index + analyse a real clip so the project has segments and frames."""
    source = tmp_path / "r3-clip.mp4"
    subprocess.run(
        [_ffmpeg(), "-y", "-f", "lavfi",
         "-i", "testsrc=s=320x180:d=2:r=25",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source)],
        check=True, capture_output=True,
    )
    aid, _ = index_one(project_id, source)
    with session_scope() as s:
        asset = s.get(Asset, aid)
        s.expunge(asset)
    _analyze_asset(project_id, asset)
    return aid


def _ensure_frame(project_id: str, asset_id: str, *, count: int = 1) -> str:
    """Guarantee exactly one usable Frame row per segment, with its file on disk.

    Analysis writes frame rows and files; some of those files can be absent (the
    asset may have been analysed before the project's ``output_dir`` existed).
    A test about *saving* must not be derailed by *pre-existing* missing files, so
    every stale row is removed here and replaced by a known-good one.
    """
    with session_scope() as s:
        seg = s.scalars(select(Segment).where(Segment.asset_id == asset_id)).first()
        assert seg is not None, "analysis produced no segments"
        seg_id, start, end = seg.id, seg.start_sec, seg.end_sec

        # Purge every unusable frame row across ALL segments of this asset.
        for other in s.scalars(select(Segment).where(Segment.asset_id == asset_id)).all():
            for row in s.scalars(select(Frame).where(Frame.segment_id == other.id)).all():
                try:
                    resolved = resolve_in_project(project_id, row.image_path)
                except ValueError:
                    resolved = None
                if resolved is None or not safe_is_file(resolved):
                    s.delete(row)
        s.flush()
        existing = (
            s.scalars(select(Frame).where(Frame.segment_id == seg_id)).first()
            if count == 1 else None
        )
        if existing is not None:
            # A frame row is only useful if its file is really there; analysis
            # normally creates both, but a stale row must not silently satisfy
            # the precondition of this test.
            try:
                candidate = resolve_in_project(project_id, existing.image_path)
            except ValueError:
                candidate = None
            if candidate is not None and safe_is_file(candidate):
                return seg_id

        target_dir = frames_dir(project_id, asset_id)
        safe_mkdir(target_dir)
        span = max(end - start, 0.001)
        for i in range(count):
            image_path = target_dir / f"{new_id()}.jpg"
            Image.new("RGB", (160, 90), (10, 200, 30)).save(image_path, format="JPEG")
            # Spread the times inside the shot so each frame is valid.
            t = start + span * (i + 0.5) / count
            s.add(Frame(
                id=new_id(), asset_id=asset_id, segment_id=seg_id,
                time_sec=t,
                image_path=str(image_path),
                file_size_bytes=image_path.stat().st_size,
            ))
        s.commit()
    return seg_id


@pytest.mark.usefixtures("_app_ready")
def test_package_in_a_deep_workspace_contains_its_frames(client, tmp_path, deep_output_dir):
    """The regression end to end: frames survive a >MAX_PATH package directory.

    TSB-366-R4 (R4-F02): this used to ignore the ``deep_output_dir`` fixture and
    build at ``tmp_path / ("out" + "z" * 24)`` -- an ``output_dir`` of ~43 chars,
    which puts the packaged frame at ~128 chars. That never crossed MAX_PATH, so
    the test could not have caught the defect it is named after. It now uses the
    fixture and *asserts* the real target lengths, so it cannot silently regress
    to a shallow path again.
    """
    _ffmpeg()
    res = client.post("/api/projects", json={
        "name": "r3-deep-e2e",
        "source_roots": [],
        "output_dir": str(deep_output_dir),
    })
    assert res.status_code in (200, 201), res.text
    pid = res.json()["id"]

    aid = _analyzed_asset(pid, tmp_path)
    _ensure_frame(pid, aid)

    result = rp._build_package(pid, {"max_package_mb": 200, "frame_quality": 82})

    assert result["reencode_failures"] == 0, (
        f"frames failed to re-encode: {result.get('first_reencode_error')}"
    )
    assert result["frames"] >= 1, "no representative frame reached the package"
    assert result["segments"] >= 1

    zip_path = resolve_in_project(pid, result["zip"])
    # Layout: packages/<version>/<files> and packages/<version>.zip, so the
    # package directory is the zip's stem, not its parent.
    pkg_dir = zip_path.with_suffix("")
    assert pkg_dir.is_dir(), f"package directory missing next to {zip_path.name}"

    # --- the point of this test: the targets really are past MAX_PATH ---------
    segments_path = pkg_dir / "segments.json"
    assert safe_is_file(segments_path), (
        f"segments.json missing; package contains: "
        f"{sorted(p.name for p in pkg_dir.iterdir())[:10]}"
    )
    assert len(str(segments_path)) >= MAX_PATH, (
        f"segments.json 路径只有 {len(str(segments_path))} 字符，未跨过 MAX_PATH"
    )
    assert len(str(zip_path)) >= MAX_PATH, (
        f"ZIP 路径只有 {len(str(zip_path))} 字符，未跨过 MAX_PATH"
    )

    segments_doc = json.loads(safe_open(segments_path, "rb").read().decode("utf-8"))
    entries = segments_doc["segments"] if isinstance(segments_doc, dict) else segments_doc
    frames = [f for seg in entries for f in seg["frames"]]
    assert frames, "segments.json lists no frames"

    for entry in frames:
        frame_file = pkg_dir / entry["file"]
        assert len(str(frame_file)) >= MAX_PATH, (
            f"代表帧目标路径只有 {len(str(frame_file))} 字符，未跨过 MAX_PATH：{frame_file}"
        )

    for entry in frames:
        frame_file = pkg_dir / entry["file"]
        assert safe_is_file(frame_file), f"missing packaged frame: {entry['file']}"
        # Pillow's own open() cannot see past MAX_PATH; read through long_path.
        with Image.open(long_path(frame_file)) as img:
            img.load()
            assert img.format == "JPEG"

    # The zip itself must open through the extended-length form.
    with zipfile.ZipFile(long_path(zip_path)) as zf:
        names = set(zf.namelist())
    # Entries are prefixed with the version directory, so match the segment.
    assert any("/frames/" in name for name in names), sorted(names)[:10]
    assert any(name.endswith("segments.json") for name in names), sorted(names)[:10]


@pytest.mark.usefixtures("_app_ready")
def test_partial_reencode_failure_keeps_good_frames_and_separates_counters(
    client, tmp_path, monkeypatch
):
    """A failing encode must be reported as an encode failure — never as budget.

    ``_build_package`` fails the whole build when nothing survives, so a partial
    failure is created by letting the first frame through and failing the rest.
    The assertion is on the counters and the message, which are what a user and a
    future debugger actually read.
    """
    _ffmpeg()
    res = client.post("/api/projects", json={"name": "r3-counters", "source_roots": []})
    pid = res.json()["id"]
    aid = _analyzed_asset(pid, tmp_path)
    _ensure_frame(pid, aid, count=3)

    real = rp._sanitized_copy
    state = {"n": 0}

    def first_ok_rest_fail(src, dst, quality):
        state["n"] += 1
        if state["n"] == 1:
            return real(src, dst, quality)
        raise ValueError("unknown file extension: ")

    monkeypatch.setattr(rp, "_sanitized_copy", first_ok_rest_fail)

    try:
        result = rp._build_package(pid, {"max_package_mb": 200, "frame_quality": 82})
    except ValueError as exc:
        # All frames failed: the message must name the encode failure and must
        # not blame the budget.
        message = str(exc)
        assert "重编码失败" in message
        assert "预算" not in message, "encode failure was reported as budget pressure"
        return

    # At least one frame survived: the counters must still be separated.
    assert result["reencode_failures"] >= 1, (
        f"encoder called {state['n']} time(s); frames={result['frames']}"
    )
    assert result["first_reencode_error"]["error_type"] == "ValueError"

    # The per-reason counters live in manifest.json, not in the compact result.
    zip_path = resolve_in_project(pid, result["zip"])
    pkg_dir = zip_path.with_suffix("")
    manifest = json.loads((pkg_dir / "manifest.json").read_text(encoding="utf-8"))
    selection = manifest["selection"]
    assert selection["reencode_failures"] >= 1
    assert selection["excluded_by_budget"] == 0, (
        "a re-encode failure was counted as a budget exclusion"
    )
    assert selection["first_reencode_error"]["error_type"] == "ValueError"


def test_all_frames_failing_produces_an_actionable_error(client, tmp_path, monkeypatch):
    """When nothing can be encoded the message must name the real cause."""
    _ffmpeg()
    res = client.post("/api/projects", json={"name": "r3-allfail", "source_roots": []})
    pid = res.json()["id"]
    aid = _analyzed_asset(pid, tmp_path)
    _ensure_frame(pid, aid)

    monkeypatch.setattr(
        rp, "_sanitized_copy",
        lambda src, dst, quality: (_ for _ in ()).throw(
            ValueError("unknown file extension: ")
        ),
    )

    with pytest.raises(ValueError) as excinfo:
        rp._build_package(pid, {"max_package_mb": 200, "frame_quality": 82})

    message = str(excinfo.value)
    assert "重编码失败" in message
    assert "unknown file extension" in message
    assert "预算" not in message, "must not blame the budget for an encode failure"


# ---------------------------------------------------------------------------
# TSB-366-R4 / R4-F01 — README.txt 的实际生成文本
# ---------------------------------------------------------------------------


def _package_readme(client, tmp_path: Path) -> str:
    """Build a package and return the README *file* contents."""
    _ffmpeg()
    res = client.post("/api/projects", json={
        "name": "r4-readme", "source_roots": [], "output_dir": str(tmp_path / "out"),
    })
    assert res.status_code in (200, 201), res.text
    pid = res.json()["id"]
    aid = _analyzed_asset(pid, tmp_path)
    _ensure_frame(pid, aid)

    result = rp._build_package(pid, {"max_package_mb": 200, "frame_quality": 82})
    zip_path = resolve_in_project(pid, result["zip"])
    pkg_dir = zip_path.with_suffix("")
    readme = pkg_dir / "README.txt"
    assert safe_is_file(readme), "README.txt was not written"
    # Read the real file, through the compat layer, not an in-memory string.
    with open(long_path(readme), "r", encoding="utf-8") as fh:
        return fh.read()


@pytest.mark.usefixtures("_app_ready")
def test_the_readme_contains_a_real_rule_not_a_literal(client, tmp_path):
    """R4-F01: 分隔线必须是 50 个等号，不是字面量 '=" * 5'。

    修复前生成的是 7 个字符 ``=" * 5``（普通字符串，没被求值）。
    只断言"函数没抛异常"永远发现不了它 —— 必须读实际文件。
    """
    text = _package_readme(client, tmp_path)
    assert "=" * 50 in text, "README 必须有 50 个等号的分隔线"
    assert '=" * 5' not in text, (
        "README 不得出现字面量 '=\" * 5'（修复前的缺陷形态）"
    )


@pytest.mark.usefixtures("_app_ready")
def test_the_readme_json_sample_has_clean_double_quotes(client, tmp_path):
    """R4-F01: JSON 示例必须显示正常的双引号，不能有多余反斜杠。"""
    text = _package_readme(client, tmp_path)
    assert '"schema_version": "2.0"' in text, "JSON 示例必须是正常双引号"
    assert '"asset_id": "..."' in text
    assert '\\"' not in text, (
        "README 不得出现转义残留的 \\\" （修复前多重转义导致的缺陷形态）"
    )


@pytest.mark.usefixtures("_app_ready")
def test_the_readme_json_sample_is_valid_json(client, tmp_path):
    """R4-F01: 示例抠出来必须是可解析的 JSON，而不只是"看着像"。"""
    text = _package_readme(client, tmp_path)
    start = text.index("return a JSON object:")
    brace = text.index("{", start)
    # The block ends at the matching close brace on its own line.
    end = text.index("\n  }", brace) + len("\n  }")
    sample = text[brace:end]
    doc = json.loads(sample)
    assert doc["schema_version"] == "2.0"
    assert isinstance(doc["requests"], list)
    req = doc["requests"][0]
    for key in ("asset_id", "segment_id", "start_sec", "end_sec", "priority", "reason"):
        assert key in req, f"README 示例缺少字段 {key}"
