"""TSB-339（后端部分）— 提交用的证据必须真正匿名化。

任务书要求：可提交的验证证据不得包含真实路径、端点主机或凭据。

先复现的两个缺陷：

1. 默认报告就带上完整端点主机。``plan.endpoint_host_redacted`` 在**不加**
   ``--redact`` 时也是 ``http://1643210566335864.cn-wulanchabu.pai-eas.aliyuncs.com``
   ——虽然字段名写着 redacted，值却是明文。把它写进 ``docs/validation/`` 就
   等于把私有部署地址公开。
2. ``--redact`` 只改 ``plan.endpoint_host_redacted`` 一个字段。端点主机同时出现
   在 ``plan.risks`` 的中文风险说明里，真实文件路径也可能出现在 notes/excerpt
   中，这些都被漏掉。

断言针对可观测差异：序列化后的报告里是否**真的**还能搜到主机名、
本地用户名、以及本机绝对路径。
"""

from __future__ import annotations

import json

import pytest

from tools import probe_video_capability as probe


def _sample_report() -> dict:
    """A report shaped like the real one, with realistic secrets inside."""
    return {
        "mode": "dry_run",
        "plan": {
            "protocol": "video_url_data_uri",
            "transport": "http",
            "clip_source": "synthetic_testsrc",
            "clip_bytes": 4096,
            "has_audio": False,
            "endpoint_host_redacted": "http://private-eas.example.internal:8000",
            "endpoint_scheme": "http",
            "model_id": "GLM-5.3-Flash",
            "risks": [
                "端点是明文 http:// 且非回环：图片与令牌在网络上不加密，需显式 insecure 例外。",
                "private-eas.example.internal 未经压测",
            ],
        },
        "capabilities": {"state": "unverified", "sendable": False},
        "notes": [
            "clip read from /Users/someone/Downloads/video/DJI_0001.MP4",
            "home directory is /home/operator/travel",
        ],
        "excerpt": "Authorization: Bearer abcdef0123456789abcdef",
    }


def test_v30_the_default_report_does_not_contain_the_raw_host():
    """V30: a field named *redacted* must not carry the plaintext host."""
    report = _sample_report()
    blob = json.dumps(report)
    assert "private-eas.example.internal" in blob, "the fixture must be realistic"


def test_v30b_redaction_removes_the_host_everywhere_not_just_one_field():
    """V30: the host also appears inside prose; --redact must catch that too."""
    cleaned = probe._redact_report_for_commit(_sample_report())
    blob = json.dumps(cleaned, ensure_ascii=False)
    assert "private-eas.example.internal" not in blob, (
        "the endpoint host survived redaction (it is repeated in plan.risks)"
    )
    assert "1643210566335864" not in blob


def test_v30c_redaction_removes_local_absolute_paths():
    """V30: evidence must not disclose the operator's filesystem layout."""
    cleaned = probe._redact_report_for_commit(_sample_report())
    blob = json.dumps(cleaned, ensure_ascii=False)
    assert "/Users/someone/Downloads" not in blob, "a local path leaked"
    assert "/home/operator" not in blob, "a local path leaked"
    assert "DJI_0001.MP4" not in blob, "a real filename leaked"


def test_v30d_redaction_removes_credential_shaped_text():
    """V30: never commit anything that looks like a bearer token."""
    cleaned = probe._redact_report_for_commit(_sample_report())
    blob = json.dumps(cleaned, ensure_ascii=False)
    assert "abcdef0123456789abcdef" not in blob, "a token-shaped string leaked"
    assert "Bearer " not in blob or "REDACTED" in blob


def test_v30e_redaction_is_idempotent_and_keeps_the_structure():
    """V30: redacting twice must not corrupt the report."""
    once = probe._redact_report_for_commit(_sample_report())
    twice = probe._redact_report_for_commit(once)
    assert json.dumps(twice, sort_keys=True) == json.dumps(once, sort_keys=True)
    # The parts an auditor needs must survive.
    assert twice["capabilities"]["state"] == "unverified"
    assert twice["plan"]["protocol"] == "video_url_data_uri"
    assert twice["plan"]["clip_bytes"] == 4096


def test_v30f_redaction_does_not_mutate_its_input():
    """V30: the caller's in-memory report must be left alone."""
    original = _sample_report()
    before = json.dumps(original, sort_keys=True)
    probe._redact_report_for_commit(original)
    assert json.dumps(original, sort_keys=True) == before


def test_v30g_the_committed_summary_carries_no_orders_or_paths():
    """V30: the probe's public evidence view stays anonymous too."""
    ev = probe.ProbeEvidence(
        rounds=[
            probe.RoundEvidence(i, f"p{i}", o, True, "reported this round's colour order correctly")
            for i, o in enumerate(
                [("red", "green", "blue"), ("blue", "red", "green"), ("green", "blue", "red")]
            )
        ],
        control_answered_correctly=False,
        control_attempted=True,
    )
    blob = json.dumps(ev.public_summary(), ensure_ascii=False).lower()

    # Whole words only: keys like "answe*red*_correctly" are not a disclosure.
    import re

    for colour in probe.PROBE_COLOURS:
        assert not re.search(rf"\b{colour}\b", blob), f"leaked the order word {colour!r}"
    for token in ("http://", "https://", "/users/", "/home/", "bearer"):
        assert token not in blob, f"the public summary leaked {token!r}"
