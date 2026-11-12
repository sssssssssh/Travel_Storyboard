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

from sqlalchemy import select

from ..config import get_settings
from . import evaluation_ratings as ratings

REPORT_SCHEMA_VERSION = "1.0"

#: Default project scope for gate evaluation. Tests override it to keep
#: records from leaking between cases; production always passes one explicitly.
_CURRENT_PROJECT = ""


def _fmt_rate(value: float | None) -> str:
    """A rate with no denominator prints as N/A — never as 0% or 100%."""
    if value is None:
        return "N/A"
    return f"{value * 100:.1f}%"


def _num(value: Any) -> str:
    return "N/A" if value is None else str(value)


#: What each gate needs in order to be considered exercised.
GATE_PURPOSES = {
    "G4_MODEL_PLAN": ("director_plan", "plan_text"),
    "G3_NATIVE_REAL": ("video_probe", "native_video"),
}


def _gate_from_records(session, project_id: str, purposes: tuple[str, str]
                       ) -> dict[str, Any]:
    """Decide one gate from recorded calls, never from a literal.

    TSB-371 hard-coded ``BLOCKED`` with a sentence about "this round's budget".
    True when written, and unchangeable afterwards — a report that cannot change
    its mind is a decoration. Three outcomes, in order:

    * ``PASS``    — a successful call of the gate's purpose for this project;
    * ``BLOCKED`` — an attempt was made and failed; the reason is the error code;
    * ``NOT_RUN`` — nothing was attempted. Never ``PASS``.
    """
    from ..models import ModelCallRecord

    purpose, channel = purposes
    rows = session.scalars(
        select(ModelCallRecord).where(
            ModelCallRecord.project_id == project_id,
            ModelCallRecord.purpose == purpose,
            ModelCallRecord.channel == channel,
        ).order_by(ModelCallRecord.created_at.desc())
    ).all()

    if not rows:
        return {
            "status": "NOT_RUN",
            "reason": "本轮未发起该类真实模型调用，无证据可判定",
            "evidence": [],
        }

    succeeded = [r for r in rows if r.succeeded]
    if succeeded:
        first = succeeded[0]
        return {
            "status": "PASS",
            "reason": "存在成功记录的真实模型调用",
            "evidence": [r.id for r in succeeded[:3]],
            "at": first.created_at.isoformat() if first.created_at else None,
        }

    latest = rows[0]
    return {
        "status": "BLOCKED",
        "reason": f"调用尝试失败：{latest.error_code or 'UNKNOWN'}",
        "evidence": [latest.id],
        "at": latest.created_at.isoformat() if latest.created_at else None,
    }


def gate_status(session=None, project_id: str = "") -> dict[str, Any]:
    """The real current status of the model-related gates, from evidence.

    Without a session or project there is nothing to read, so every gate reports
    ``NOT_RUN`` — the safe answer. It is never ``PASS`` by default.
    """
    settings = get_settings()
    out: dict[str, Any] = {}
    for gate, purposes in GATE_PURPOSES.items():
        if session is None or not project_id:
            out[gate] = {
                "status": "NOT_RUN",
                "reason": "无可用证据（未提供会话或项目）",
                "evidence": [],
            }
        else:
            out[gate] = _gate_from_records(session, project_id, purposes)
    out["model_mode"] = settings.model_mode
    return out


def _claims_table(session, project_id: str, session_id: str) -> list[dict[str, Any]]:
    """One row per claim the reviewer was shown, judged or not.

    A total accuracy is not actionable; the sentence is. This is what lets a
    reader quote the model's exact words and see how a human judged them.
    """
    from ..models import EvaluationItem, EvaluationRating
    from ..services.evaluation_feedback import claim_catalogue

    rows: list[dict[str, Any]] = []
    items = session.scalars(
        select(EvaluationItem)
        .where(EvaluationItem.session_id == session_id)
        .order_by(EvaluationItem.position)
    ).all()
    ratings_by_item = {
        r.item_id: r for r in session.scalars(
            select(EvaluationRating).where(EvaluationRating.session_id == session_id)
        ).all()
    }
    for item in items:
        catalogue = claim_catalogue(item.readings_json or {})
        rating = ratings_by_item.get(item.id)
        verdicts = {}
        if rating is not None:
            for entry in (rating.claim_verdicts or []):
                if isinstance(entry, dict) and entry.get("key"):
                    verdicts[entry["key"]] = entry.get("verdict")
        for key, text in catalogue.items():
            verdict = verdicts.get(key)
            # "not sure" is a statement about the reviewer's confidence, not the
            # model being wrong, so it never enters the error rate.
            counted = verdict in ("right", "wrong")
            rows.append({
                "item_id": item.id[:8],
                "segment_id": item.segment_id[:8],
                "channel": item.channel,
                "result_id": (item.result_id or "")[:8],
                "claim_key": key,
                "text": text,
                "verdict": verdict,
                "counted": counted,
            })
    return rows


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
        "metric_basis": summary["metric_basis"],
        "scope": {
            "n": summary["evaluated_claims"],
            "unit": ("句" if summary["metric_basis"] == "sentences" else "条目"),
            "note": "小样本；不能代表全库准确率。",
        },
        "sentences": summary["sentences"],
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
        # Which sentence was wrong, in the model's own words.
        "claims": _claims_table(session, project_id, session_id),
        "ab": ab_block,
        "gates": gate_status(session=session, project_id=project_id),
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
    unit = report["scope"].get("unit", "条目")
    lines.append(f"有效人工判断 n = **{report['scope']['n']}** {unit}。{report['scope']['note']}")
    sent = report.get("sentences") or {}
    if sent.get("judged"):
        lines.append("")
        lines.append(f"逐句判断：判对 {sent['right']} · 判错 {sent['wrong']} · "
                     f"不确定 {sent['unsure']}（不确定不计入分母）")
    for lim in report["limitations"]:
        lines.append(f"- {lim}")
    lines.append("")
    lines.append("## 人工已核实结果")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| 有效判断数 | {m['evaluated_claims']} {unit} |")
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
    claims = report.get("claims") or []
    wrong = [c for c in claims if c["verdict"] == "wrong"]
    if wrong:
        lines.append("### 判错的句子")
        lines.append("")
        lines.append("| 渠道 | 断言 | 模型原话 |")
        lines.append("|---|---|---|")
        for c in wrong[:40]:
            text = str(c["text"]).replace("|", "\\|")[:120]
            lines.append(f"| {c['channel']} | `{c['claim_key']}` | {text} |")
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
    """One row per claim: the sentence is the unit a reader acts on."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        "session_id", "item_id", "segment_id", "channel", "result_id",
        "claim_key", "text", "verdict", "counted",
    ])
    for claim in report.get("claims") or []:
        writer.writerow([
            report["session_id"][:8], claim["item_id"], claim["segment_id"],
            claim["channel"], claim["result_id"], claim["claim_key"],
            claim["text"], _num(claim["verdict"]),
            "yes" if claim["counted"] else "no",
        ])
    return buffer.getvalue()


def to_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, ensure_ascii=False)
