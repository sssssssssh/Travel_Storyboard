r"""TSB-366-R4 —— 视频派生链路必须走长路径兼容层。

## 为什么不是"构造 260 字符路径"再断言

Windows 的 MAX_PATH 在 macOS/Ubuntu 上不存在，所以"跑一个超长路径看它红不红"
在这两个平台**恒为绿**，无论代码有没有缺陷 —— 这正是任务书 §八 批评的
"依赖 TEMP 偶然长度"。等 CI 在 Windows 上偶然变红再定位，等于把验收交给运气。

## 本轮采用的两层证据

1. **契约层（平台无关、确定性）**：断言生产代码对派生文件的每一次访问
   **确实经过** `safe_is_file` / `safe_stat` / `safe_open` / `long_path`。
   这些 helper 内部对 `\\?\` 的回退是既有实现、已被
   `test_long_paths.py` 覆盖；这里保证的是**视频链路接上了它们**。
   AGENTS.md 3.2 的教训正是"底层支持了，但生产调用点没传"。
2. **行为层（真跑）**：在正常路径上确认读到的字节数/内容与真实文件一致，
   且文件真缺失时**仍然抛错** —— 长路径兼容不得把真实失败吞成"文件不存在"。

真正的 Windows 260 字符端到端验证由 CI 的
`Backend (Python 3.x) — windows-latest` + `Required FFmpeg (windows-latest)`
承担；本机矩阵（35/58/76）在 macOS 上只能证明"没有引入回归"。
两者在报告中分开陈述，不互相冒充。
"""

from __future__ import annotations

import hashlib
import inspect
import os
from pathlib import Path

import pytest

from app.ffmpeg import long_path


def _make_clip(tmp_path: Path, payload: bytes = b"MP4DATA" * 500) -> Path:
    target = tmp_path / "derived-clip.mp4"
    target.write_bytes(payload)
    return target


# ---------------------------------------------------------------------------
# D1 — probe_clip(): 存在性与大小
# ---------------------------------------------------------------------------


def test_probe_clip_resolves_the_clip_through_the_compat_layer():
    """D1 契约: probe_clip 不得用普通 is_file()/stat() 判断派生文件。

    修复前这两处是 ``path.is_file()`` 与 ``path.stat()``。在
    ``LongPathsEnabled=0`` 的 Windows 上，一个**真实存在、ffprobe 也读得到**
    的派生 MP4 会被判成"未创建"，抛 ``CLIP_UNREADABLE: derived clip was not
    created``。该异常发生在 ``complete_video()`` 之前，
    于是 spy 记录到 ``used == []`` —— 与 R07/V31 的现象一致。
    """
    from app.services.vision import video_clip as vc

    src = inspect.getsource(vc.probe_clip)
    assert "safe_is_file(path)" in src, (
        "probe_clip 必须用 safe_is_file 判断派生文件存在性；"
        "普通 is_file() 会让超长路径下的真实文件看起来不存在"
    )
    assert "safe_stat(path)" in src, (
        "probe_clip 必须用 safe_stat 读取文件大小，否则超长路径读不到真实字节数"
    )
    # The plain forms must be gone, or they would still short-circuit first.
    assert "path.is_file()" not in src, "不得保留普通 path.is_file()"
    assert "path.stat()" not in src, "不得保留普通 path.stat()"


def test_probe_clip_reports_real_size_and_digest(tmp_path, monkeypatch):
    """D1 行为: 正常路径上大小与 SHA-256 必须等于真实字节。"""
    from app.services.vision import video_clip as vc

    clip = _make_clip(tmp_path)
    payload = clip.read_bytes()

    class _Info:
        has_audio = False
        duration_sec = 2.0
        container = "mp4"

        class video:
            width, height, rotation, vfr = 320, 180, 0, False

    monkeypatch.setattr(vc, "probe_media", lambda p: _Info())
    art = vc.probe_clip(
        path=clip, segment_id="s1", asset_id="a1",
        source_clip_start_sec=0.0, source_clip_end_sec=2.0,
    )
    assert art.size_bytes == len(payload)
    assert art.sha256 == hashlib.sha256(payload).hexdigest()


def test_probe_clip_still_rejects_a_clip_that_really_is_missing(tmp_path, monkeypatch):
    """D1 反向: 真缺失仍必须报 CLIP_UNREADABLE，兼容层不得吞掉真实失败。"""
    from app.services.vision import video_clip as vc

    class _Info:
        has_audio = False
        duration_sec = 1.0
        container = "mp4"

        class video:
            width, height, rotation, vfr = 320, 180, 0, False

    monkeypatch.setattr(vc, "probe_media", lambda p: _Info())
    with pytest.raises(vc.VideoClipError) as exc:
        vc.probe_clip(
            path=tmp_path / "never-created.mp4", segment_id="s1", asset_id="a1",
            source_clip_start_sec=0.0, source_clip_end_sec=1.0,
        )
    assert exc.value.code == vc.CLIP_UNREADABLE


# ---------------------------------------------------------------------------
# D2 — _analyze_one(): 预算计量与 VideoPart 字节
# ---------------------------------------------------------------------------


def test_the_budget_and_the_payload_use_the_compat_helpers():
    """D2 契约: _analyze_one 不得用普通 .stat()/.read_bytes() 访问派生文件。

    修复前是 ``clip.path.stat().st_size``（预算计量）与
    ``clip.path.read_bytes()``（构造 VideoPart）。两者都在
    ``complete_video()`` **之前**；超长路径上抛 ``FileNotFoundError``，
    而它不是 ``ModelError``，所以任务在上传前直接中断，provider 永不被调用。
    """
    from app.services.vision import video_job as vj

    src = inspect.getsource(vj._analyze_one)
    assert "_clip_size_bytes(clip.path)" in src, (
        "预算计量必须经过 _clip_size_bytes（内部 safe_stat）"
    )
    assert "_clip_read_bytes(clip.path)" in src, (
        "VideoPart 必须经过 _clip_read_bytes（内部 safe_open）"
    )
    assert "clip.path.stat()" not in src, "不得保留普通 clip.path.stat()"
    assert "clip.path.read_bytes()" not in src, "不得保留普通 clip.path.read_bytes()"


def test_clip_size_helper_reads_the_real_size(tmp_path):
    """D2 行为: 辅助函数必须返回真实大小。"""
    from app.services.vision.video_job import _clip_size_bytes

    clip = _make_clip(tmp_path)
    assert _clip_size_bytes(clip) == clip.stat().st_size


def test_clip_read_helper_returns_the_whole_file(tmp_path):
    """D2 行为: 必须返回**完整**字节，不得截断（provider 要收到整个 MP4）。"""
    from app.services.vision.video_job import _clip_read_bytes

    payload = b"MP4DATA" * 700
    clip = _make_clip(tmp_path, payload)
    got = _clip_read_bytes(clip)
    assert got == payload
    assert len(got) == len(payload)


def test_the_clip_helpers_raise_on_a_genuinely_missing_file(tmp_path):
    """D2 反向: 真缺失必须抛错，不得伪造 0 字节或空 bytes（禁止假成功）。"""
    from app.services.vision.video_job import _clip_read_bytes, _clip_size_bytes

    absent = tmp_path / "missing.mp4"
    with pytest.raises(FileNotFoundError):
        _clip_size_bytes(absent)
    with pytest.raises(FileNotFoundError):
        _clip_read_bytes(absent)


# ---------------------------------------------------------------------------
# D3 — path_occupies_disk(): 不得把"路径过长"当成"存在"
# ---------------------------------------------------------------------------


def test_path_occupies_disk_resolves_overlong_paths_before_deciding():
    """D3 契约: 必须先经 long_path 解析，再判断路径是否占用。

    修复前只有 ``os.lstat(stage)``，超长路径抛 OSError 后被
    ``except OSError`` 归为"存在"。答案方向凑巧对了，但**理由错误**：
    它把"路径过长"伪装成"存在但不可读"，掩盖真实错误（C6）。
    修复后要求：异常分支里也先用 long_path 复核，只有 long_path 也说
    不存在时才返回 False。
    """
    from app.services.vision import video_job as vj

    src = inspect.getsource(vj.path_occupies_disk)
    # Count *executable* uses only: the docstring legitimately mentions long_path.
    code = "\n".join(
        line for line in src.splitlines()
        if not line.strip().startswith("#")
    )
    body = code.split('"""')
    body = body[-1] if len(body) > 1 else code  # drop the docstring
    uses = body.count("long_path(")
    assert uses >= 2, (
        f"普通 lstat 失败与 OSError 两条分支都要经 long_path 复核，实际仅 {uses} 处"
    )
    # lexists semantics must survive: no follow-the-link helper may replace lstat.
    assert "os.lstat" in src, "必须保持 lstat（不跟随链接）的语义"


def test_path_occupies_disk_is_true_for_a_real_directory(tmp_path):
    """D3 行为: 真实目录 -> True。"""
    from app.services.vision.video_job import path_occupies_disk

    d = tmp_path / "stage"
    d.mkdir()
    assert path_occupies_disk(d) is True


def test_path_occupies_disk_is_false_for_an_absent_path(tmp_path):
    """D3 行为: 真不存在 -> False（否则 cleanup 会对空路径动手）。"""
    from app.services.vision.video_job import path_occupies_disk

    assert path_occupies_disk(tmp_path / "nope") is False


def test_path_occupies_disk_still_sees_a_dangling_link(tmp_path):
    """D3 回归（TSB-346）: 悬空链接仍算"占用"，清理不得静默跳过。"""
    from app.services.vision.video_job import path_occupies_disk

    link = tmp_path / "dangling"
    try:
        os.symlink(str(tmp_path / "target-that-does-not-exist"), str(link))
    except (OSError, NotImplementedError):  # pragma: no cover - platform fact
        pytest.skip("link creation is a platform fact")
    assert path_occupies_disk(link) is True


# ---------------------------------------------------------------------------
# 反向验证：把修复逐条去掉，对应用例必须转红
# ---------------------------------------------------------------------------


def test_the_video_chain_no_longer_uses_plain_path_io():
    """总闸: 视频链路的派生文件访问不得残留普通 Path I/O。

    这条是上三条的汇总锁：任何人日后把 safe_* 改回普通 API，
    这里会立刻失败，而不必等某个 Windows 机器的 TEMP 恰好够长。
    """
    from app.services.vision import video_clip as vc
    from app.services.vision import video_job as vj

    for mod, fn in [(vc, vc.probe_clip), (vj, vj._analyze_one)]:
        src = inspect.getsource(fn)
        for bad in ("path.is_file()", "path.stat()", "clip.path.stat()",
                    "clip.path.read_bytes()"):
            assert bad not in src, f"{fn.__name__} 仍残留普通路径访问: {bad}"
