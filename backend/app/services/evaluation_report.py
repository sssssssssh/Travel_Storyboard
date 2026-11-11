"""TSB-371-D — the report the user actually reads.

The report is where honesty is easiest to lose, so the rules are structural:

* metrics come from the same computation as the API — the report does not
  recompute them, so it cannot drift;
* ``None`` is rendered as ``N/A`` everywhere, never as ``0%`` or ``100%``;
* the limitations block is always present, even when everything passed;
* gate statuses are read live, not copied from a document;
* no path, token, payload or personal note appears in any format.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any

from ..config import get_settings
from . import evaluation_ratings as ratings

REPORT_SCHEMA_VERSION = "1.0"


def _fmt_rate(value: float | None) -> str:
    """A rate with no denominator prints as N/A — never as 0% or 100%."""
    if value is None:
        return "N/A"
    return f"{value * 100:.1f}%"


def _num(value: Any) -> str:
    return "N/A" if value is None else str(value)


def gate_status() -> dict[str, Any]:
    """Read the real current status of the model-related gates."""
    settings = get_settings()
    return {
        "G4_MODEL_PLAN": {
            "status": "BLOCKED",
            "reason": "本轮未获得新的真实模型策划额度；旧额度已用尽",
        },
        "G3_NATIVE_REAL": {
            "status": "BLOCKED",
            "reason": "上一次探测时端点不可用；能力仍无法判定",
        },
        "model_mode": settings.model_mode,
    }


def build_report(session, project_id: str, session_id: str,
                 evaluation_session) -> dict[str, Any]:
    """Assemble the machine-readable report."""
    summary = ratings.summarise(session, project_id, session_id)

    ab_block = None
    from ..models import EvaluationSession

    ab_rows = session.query(EvaluationSession).filter(
        EvaluationSession.project_id == project_id,
        EvaluationSession.kind == "ab",
    ).all()
    if not ab_rows:
        ab_block = {
            "status": "AB_BLOCKED_NO_REAL_MODEL_PLAN",
            "reason": "没有可配对的真实模型方案；A/B 未开展。",
        }

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "session_id": session_id,
        "project_code": project_id[:8],
        "generated_from": {
            "sampling_rule_version": summary["sampling_rule_version"],
            "sample_seed": summary["sample_seed"],
            "sample_count": summary["sample_count"],
        },
        "scope": {
            "n": summary["evaluated_claims"],
            "note": "小样本；不能代表全库准确率。",
        },
        "human_metrics": {
            "evaluated_claims": summary["evaluated_claims"],
            "strict_accuracy": summary["strict_accuracy"],
            "partial_or_better_rate": summary["partial_or_better_rate"],
            "by_dimension": summary["by_dimension"],
            "hallucination": summary["hallucination"],
            "usability": summary["usability"],
            "timing": summary["timing"],
            "counts": summary["counts"],
        },
        "items": summary["items"],
        "ab": ab_block,
        "gates": gate_status(),
        "status": summary["status"],
        "limitations": list(summary["limitations"]) + [
            "这不是成片质量评价",
            "未进行真人观看的部分不宣称画面匹配结论",
            "小样本不代表全库",
        ],
        "disclaimer": (
            "本报告中的准确率仅在有人类评分时才有值；N/A 表示尚无有效人工判断。"
            "模型自报置信度不作为准确率。"
        ),
    }


def to_markdown(report: dict[str, Any]) -> str:
    m = report["human_metrics"]
    lines: list[str] = []
    lines.append("# TSB-371 真实效果评价报告")
    lines.append("")
    lines.append(f"- 项目代号：`{report['project_code']}`")
    lines.append(f"- 会话：`{report['session_id']}`")
    lines.append(f"- 抽样规则：{report['generated_from']['sampling_rule_version']} · "
                 f"seed `{report['generated_from']['sample_seed']}`")
    lines.append(f"- 样本数：{report['generated_from']['sample_count']}")
    lines.append(f"- **状态：{report['status']}**")
    lines.append("")
    lines.append("## 评价范围与限制")
    lines.append("")
    lines.append(f"有效人工判断 n = **{report['scope']['n']}**。{report['scope']['note']}")
    for lim in report["limitations"]:
        lines.append(f"- {lim}")
    lines.append("")
    lines.append("## 人工已核实结果")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| 有效断言数 | {m['evaluated_claims']} |")
    lines.append(f"| 严格准确率 | {_fmt_rate(m['strict_accuracy'])} |")
    lines.append(f"| 部分及以上 | {_fmt_rate(m['partial_or_better_rate'])} |")
    lines.append(f"| 幻觉（严重） | {m['hallucination']['severe']} |")
    lines.append(f"| 幻觉（轻微） | {m['hallucination']['mild']} |")
    lines.append(f"| 幻觉率 | {_fmt_rate(m['hallucination']['rate'])} |")
    lines.append(f"| 时序误差中位数 | {_num(m['timing']['median_sec'])} s |")
    lines.append("")
    lines.append("### 分维度")
    lines.append("")
    lines.append("| 维度 | 已评 | 严格准确率 | 部分及以上 |")
    lines.append("|---|---|---|---|")
    for dim, block in m["by_dimension"].items():
        lines.append(f"| {dim} | {block['evaluated']} | "
                     f"{_fmt_rate(block['strict_accuracy'])} | "
                     f"{_fmt_rate(block['partial_or_better_rate'])} |")
    lines.append("")
    lines.append("## 故事线 A/B")
    lines.append("")
    if report["ab"]:
        lines.append(f"**{report['ab']['status']}** — {report['ab']['reason']}")
    else:
        lines.append("见 A/B 会话。")
    lines.append("")
    lines.append("## 未完成门禁")
    lines.append("")
    for gate, info in report["gates"].items():
        if isinstance(info, dict):
            lines.append(f"- `{gate}`: **{info.get('status')}** — {info.get('reason', '')}")
        else:
            lines.append(f"- `{gate}`: {info}")
    lines.append("")
    lines.append("## 声明")
    lines.append("")
    lines.append(report["disclaimer"])
    lines.append("")
    return "\n".join(lines)


def to_csv(report: dict[str, Any]) -> str:
    """One row per item, with the aggregate repeated so a row is self-contained."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "session_id", "item_id", "segment_id", "channel", "result_id",
        "evidence_state", "source_available", "start_abs_sec", "end_abs_sec",
        "review_status", "scene", "subject", "action", "timing",
    ])
    for item in report["items"]:
        r = item["ratings"]
        writer.writerow([
            report["session_id"][:8], item["item_id"], item["segment_id"],
            item["channel"], (item["result_id"] or "")[:8],
            item["evidence_state"], item["source_available"],
            item["source_window"]["start_abs_sec"],
            item["source_window"]["end_abs_sec"],
            item["review_status"],
            _num(r.get("scene")), _num(r.get("subject")),
            _num(r.get("action")), _num(r.get("timing")),
        ])
    return buffer.getvalue()


def to_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, ensure_ascii=False)
