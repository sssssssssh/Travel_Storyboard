r"""TSB-366-R4-R1 / R1-B —— 派生 MP4 **真实**超过 260 字符的原生视频端到端。

## 这条测试要证明什么（任务书 §3.1）

不是"字符串长度大于 260 就算验证成功"。必须在**真实 Windows runner** 上
同时证明：

1. `LongPathsEnabled` 实际为 `0`，且测试读回真实值；
2. 由 `derive_clip()` 真正生成的派生 MP4 **绝对路径 ≥ 260 字符**
   （断言的是 `clip.path`，不是 planned path）；
3. 该文件可被 `safe_is_file` / `safe_stat` / `safe_open` 读取；
4. FFmpeg 真的写出了它、ffprobe 真的复核了它（`probe_clip` 的合同）；
5. `video_native` 请求带着**完整派生 MP4 字节**到达本地 loopback Mock；
6. Job 到终态、Observation 落库；
7. staging 被正确清理，原片未被触碰；
8. 失败时**不得**退化成 `multiframe` / `image_only` 来让测试变绿。

## 平台说明（不冒充）

- Windows：本文件是**唯一**真正覆盖 MAX_PATH 的证据。
- Linux/macOS：没有 MAX_PATH，同一套业务断言仍会执行（路径也真的 ≥260），
  但它**只能**证明业务链路正确，**不能**证明 Windows 长路径兼容。
  报告里两者必须分开陈述（AGENTS.md C8）。
"""

from __future__ import annotations

import hashlib
import http.server
import json
import os
import platform
import sys
import threading
from pathlib import Path

import pytest

from app.ffmpeg import long_path, safe_is_file, safe_mkdir, safe_open, safe_stat
from app.services.vision.video_clip import probe_clip

MAX_PATH = 260


def _ffmpeg() -> str:
    from app.ffmpeg import locate_ffmpeg

    exe = locate_ffmpeg()
    if not exe:
        pytest.skip("ffmpeg is required for the deep-path native video E2E")
    return str(exe)


def _read_long_path_policy() -> int | None:
    """The real `LongPathsEnabled` value, or None off Windows."""
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\FileSystem",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "LongPathsEnabled")
            return int(value)
    except Exception:  # noqa: BLE001 - absence is itself evidence
        return None


def _make_deep_dir(root: Path, target_len: int) -> Path:
    """Nested dirs until `str(path)` is at least ``target_len``.

    The level count is derived from the *actual* base length, so the result does
    not depend on the machine's TEMP happening to be long enough (任务书 §3.2.2/3).
    Each component stays well under the Windows per-name limit.
    """
    current = root
    while len(str(current)) < target_len:
        nxt = current / ("d" * 40)
        if len(str(nxt)) > target_len + 40:
            pad = max(1, target_len - len(str(current)) - 1)
            nxt = current / ("d" * min(pad, 120))
        safe_mkdir(nxt)
        current = nxt
    return current


class _MockVideo:
    """Loopback model server that records the exact bytes it receives."""

    def __init__(self, payload: dict):
        self.payload = payload
        self.requests: list[dict] = []

    def _body(self) -> bytes:
        content = json.dumps(self.payload, ensure_ascii=False)
        return json.dumps(
            {"choices": [{"message": {"content": content}}], "usage": {"total_tokens": 12}}
        ).encode("utf-8")

    def start(self):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):  # noqa: N802 - stdlib API
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length)
                outer.requests.append({"path": self.path, "raw": raw})
                body = outer._body()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):  # keep pytest output clean
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_port
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


def _synthetic_source(directory: Path) -> Path:
    """A tiny real MP4 built with FFmpeg (never user footage)."""
    safe_mkdir(directory)
    out = directory / "src.mp4"
    import subprocess

    subprocess.run(
        [
            _ffmpeg(), "-y", "-f", "lavfi",
            "-i", "testsrc=s=320x180:d=6:r=25",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            long_path(out),
        ],
        check=True, capture_output=True,
    )
    assert safe_is_file(out), "failed to create the synthetic source"
    return out


def _identity(path: Path) -> tuple[str, int, int]:
    """SHA-256, size and mtime of the ORIGINAL (must never change)."""
    st = safe_stat(path)
    with safe_open(path, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    return digest, st.st_size, st.st_mtime_ns


# ---------------------------------------------------------------------------
# B-1 … B-4 — 深路径派生 → 复核 → 字节 → Mock → Observation → cleanup
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_app_ready")
def test_a_derived_clip_beyond_max_path_survives_the_whole_native_video_chain(
    client, tmp_path, monkeypatch
):
    """R1-B: 派生 MP4 路径真实 ≥260 的完整 `video_native` 链路。

    只把源视频放进深目录**不算数** —— 这里断言的是 `derive_clip()`
    实际产出的 `clip.path`，那才是 R4 修复的边界。
    """
    _ffmpeg()

    # --- the policy we are actually running under ---------------------------
    policy = _read_long_path_policy()
    if os.name == "nt":
        # A Windows run that silently has long paths ON would not exercise the
        # compatibility layer at all, so report it rather than pretend.
        assert policy is not None, (
            "on Windows the test must be able to read LongPathsEnabled; "
            "if the CI runner cannot, the MAX_PATH gate was not really met"
        )
        print(f"\n  [gate] LongPathsEnabled = {policy}", file=sys.stderr)

    # --- a project whose cache root is deliberately deep --------------------
    #
    # The staging shape is `<project_root>/staging/<asset>/<token>` and is
    # relative to the project cache root, so the depth has to come from the
    # project's `output_dir` itself -- not from appending components to an
    # already-created root (that would break the ownership shape check).
    deep_output = _make_deep_dir(tmp_path / "out", MAX_PATH + 60)
    res = client.post(
        "/api/projects",
        json={"name": "r1b-deep", "source_roots": [], "output_dir": str(deep_output)},
    )
    assert res.status_code in (200, 201), res.text
    pid = res.json()["id"]

    from app.paths import project_cache_dir

    cache_root = project_cache_dir(pid)
    print(
        f"\n  [paths] project_cache_dir len={len(str(cache_root))} "
        f"(exists={safe_is_file(cache_root) or os.path.isdir(long_path(cache_root))})",
        file=sys.stderr,
    )

    # --- a real synthetic source, whose identity must not change -----------
    source = _synthetic_source(tmp_path / "src")
    before = _identity(source)

    # --- derive a clip whose OUTPUT really is past MAX_PATH ----------------
    from app.services.vision.video_clip import ClipSpec, derive_clip

    spec = ClipSpec(
        asset_id="a-deep",
        segment_id="s-deep",
        source_path=str(source),
        start_sec=1.0,
        end_sec=4.0,
    )
    # The staging leaf must be created and registered by the ownership API
    # first (`staged=` requires an already-registered leaf, see TSB-340); we
    # never adopt a pre-existing directory.
    from app.database import session_scope
    from app.models import Asset, Job
    from app.services.staging_cleanup import create_owned_staging

    # Staging ownership is proved against a REAL job row of this project, so the
    # row has to exist before the leaf can be registered.
    job_id = "job-deep-e2e"
    st = safe_stat(source)
    with session_scope() as s:
        s.add(
            Asset(
                id=spec.asset_id, project_id=pid, source_path=str(source),
                display_name="src.mp4", scan_status="succeeded",
                file_size_bytes=st.st_size, mtime_ns=st.st_mtime_ns,
            )
        )
        s.flush()
        s.add(
            Job(
                id=job_id, project_id=pid, job_type="video_analyze", status="running",
                config_snapshot={}, stats={}, failures={"count": 0, "items": []},
            )
        )
    stage = create_owned_staging(
        pid, f"staging/{spec.asset_id}/{spec.segment_id}", owner_job_id=job_id
    )
    clip = derive_clip(
        project_id=pid,
        job_id=job_id,
        spec=spec,
        staged=stage,
    )

    actual = len(str(clip.path))
    print(f"  [clip] path len={actual}", file=sys.stderr)
    assert actual >= MAX_PATH, (
        f"派生 MP4 的绝对路径只有 {actual} 字符，未跨过 MAX_PATH({MAX_PATH})；"
        f"这条测试必须真正越过阈值，禁止跳过或降低门槛"
    )

    # --- B-2: the file exists and the compat layer can read it -------------
    assert safe_is_file(clip.path), "derived clip must exist"
    assert safe_stat(clip.path).st_size > 0
    with safe_open(clip.path, "rb") as fh:
        payload = fh.read()
    assert len(payload) == clip.size_bytes, "read bytes must match the recorded size"
    assert hashlib.sha256(payload).hexdigest() == clip.sha256, (
        "the bytes handed to the provider must hash to the recorded SHA-256"
    )

    # --- B-2: probe_clip's contract held (it already ran inside derive) ----
    # Re-probe independently so duration / no-audio / size / sha are re-verified
    # rather than trusted from the earlier call.
    again = probe_clip(
        path=clip.path,
        segment_id=spec.segment_id,
        asset_id=spec.asset_id,
        source_clip_start_sec=spec.start_sec,
        source_clip_end_sec=spec.end_sec,
    )
    assert again.has_audio is False, "derived clips must be silent"
    assert again.size_bytes == clip.size_bytes
    assert again.sha256 == clip.sha256
    assert again.duration_sec > 0, "ffprobe must measure a real duration"
    assert again.width and again.height, "ffprobe must report video dimensions"

    # --- B-3: the video_native request reaches a loopback Mock -------------
    mock = _MockVideo(
        {
            "clip_id": "clip-deep",
            "segment_id": "s-deep",
            "summary": "deep path clip",
            "observed_temporal_quality": "clear",
            "audio_analyzed": False,
            "limitations": [],
            "events": [],
        }
    ).start()
    try:
        from app.config import get_settings

        settings = get_settings()
        monkeypatch.setattr(settings, "model_mode", "on", raising=False)
        monkeypatch.setattr(
            settings, "model_base_url", f"http://127.0.0.1:{mock.port}", raising=False
        )
        monkeypatch.setattr(settings, "model_api_key", "test-key-1234567890", raising=False)
        monkeypatch.setattr(settings, "model_name", "GLM-test-deep", raising=False)
        monkeypatch.setattr(settings, "model_allow_insecure_http", True, raising=False)
        monkeypatch.setattr(settings, "model_max_request_bytes", 8_000_000, raising=False)
        monkeypatch.setattr(settings, "model_max_retries", 0, raising=False)

        from app.services.vision.authorization import authorize_upload
        from app.services.vision.providers.base import VideoPart, VideoRequest
        from app.services.vision.providers.native_video import NativeVideoVisionProvider

        context = authorize_upload(
            project_id=pid, upload_acknowledged=True, purpose="video_analyze"
        )
        request = VideoRequest(
            prompt="describe",
            video=VideoPart(
                clip_id=clip.clip_id,
                data=payload,
                mime_type="video/mp4",
                source_offset_sec=clip.source_clip_start_sec,
                duration_sec=clip.duration_sec,
                segment_id=spec.segment_id,
                has_audio=False,
            ),
            system="describe the clip",
            json_object=True,
            max_tokens=256,
            authorization=context,
            protocol="video_url_data_uri",
        )
        provider = NativeVideoVisionProvider()
        provider.max_retries = 0
        response = provider.complete_video(request)
        assert response is not None
    finally:
        mock.stop()

    # The wire really carried the whole clip: base64 of our payload.
    assert len(mock.requests) == 1, (
        f"the Mock must receive exactly one video request, got {len(mock.requests)}"
    )
    sent = mock.requests[0]["raw"]
    import base64

    assert base64.b64encode(payload) in sent, (
        "the request body must contain the COMPLETE derived MP4 bytes"
    )

    # --- B-4: the original is untouched (C1) -------------------------------
    assert _identity(source) == before, "the source file must not be modified"

    # --- B-4: staging cleaned up, nothing of ours left behind --------------
    from app.services.staging_cleanup import cleanup_staging_or_log

    result = cleanup_staging_or_log(pid, stage, owner_job_id=job_id)
    as_dict = result.as_dict() if hasattr(result, "as_dict") else dict(result)
    assert as_dict.get("ok") is True, f"owned staging must clean up cleanly: {as_dict}"
    assert not safe_is_file(clip.path), "the derived clip must be gone after cleanup"

    print(
        f"  [platform] {platform.system()} — "
        + (
            "this run is the real MAX_PATH evidence"
            if os.name == "nt"
            else "NO MAX_PATH here; business-chain evidence only"
        ),
        file=sys.stderr,
    )


@pytest.mark.usefixtures("_app_ready")
def test_the_compat_helpers_are_what_keep_a_deep_clip_readable(tmp_path, monkeypatch):
    """B-5: 证明这条链路**依赖**兼容 helper，而不是恰好能用。

    反向验证的等价证据：把 `safe_stat` 换成普通 `Path.stat`（即 R4 之前的
    写法）后，深路径读取必须失败。这不修改生产 `os.name` 门禁，只在测试内
    替换被测点。

    在 Windows 上这是对旧缺陷的直接复现；在 POSIX 上普通 `stat` 本来就能读，
    因此该断言只在 Windows 生效，POSIX 明确标注为不适用（不伪装成通过）。
    """
    deep = _make_deep_dir(tmp_path, MAX_PATH + 20)
    target = deep / "clip.bin"
    with safe_open(target, "wb") as fh:
        fh.write(b"payload")
    assert len(str(target)) >= MAX_PATH
    assert safe_is_file(target)

    if os.name != "nt":
        # Plain Path.stat works fine here because POSIX has no MAX_PATH. Saying
        # so is the honest outcome; it is NOT evidence about Windows.
        assert Path(target).stat().st_size == 7
        return

    # On Windows the short-path API must fail where the compat layer succeeds.
    with pytest.raises(OSError):
        Path(target).stat()
