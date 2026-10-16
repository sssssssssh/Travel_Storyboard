"""TSB-330 — A/B evaluation driver (read-only unless told otherwise).

Collects the three arms for a project, optionally applies human labels, and writes
a committable report. It never performs a model call itself: arm C only exists if a
``video_analyze`` job already produced observations, which keeps the "who spent the
money" question answerable.

Usage::

    python tools/evaluate_video_ab.py --project <id>
    python tools/evaluate_video_ab.py --project <id> --labels labels.json \\
        --json ../docs/validation/v2.1.3/AB_RESULTS.json \\
        --markdown ../docs/validation/v2.1.3/AB_RESULTS.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.services.vision import video_ab as ab  # noqa: E402
from app.services.vision.video_capabilities import (  # noqa: E402
    VideoCapabilities,
    unverified_capabilities,
)


def load_capability_report(path: Path | None) -> VideoCapabilities:
    """Read a capability report produced by ``probe_video_capability.py``.

    Reading the recorded probe result rather than re-probing keeps this tool
    read-only, and means the A/B report cites the same evidence as the capability
    report instead of a second, possibly different, observation.
    """
    if path is None or not Path(path).is_file():
        return unverified_capabilities(reason="no capability report supplied")
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return unverified_capabilities(reason="capability report could not be parsed")
    caps = data.get("capabilities") or {}
    return VideoCapabilities(
        state=caps.get("state") or "unverified",
        protocol=caps.get("protocol") or "",
        model_id=caps.get("model_id") or "",
        probe_time=caps.get("probe_time") or "",
        verification_scope=caps.get("verification_scope") or "",
        max_duration_sec=caps.get("max_duration_sec"),
        max_bytes=caps.get("max_bytes"),
        accepts_audio=caps.get("accepts_audio"),
        temporal_sampling_disclosed=bool(caps.get("temporal_sampling_disclosed")),
    )


def render_markdown(report: dict) -> str:
    """A human-readable view of the same numbers, for the release record."""
    metrics = report.get("metrics") or {}
    lines = [
        "# TSB-330 A/B 评价结果",
        "",
        f"- 状态: `{report.get('status')}`",
        f"- 是否已评价: **{report.get('evaluated')}**",
        f"- 镜头数: {report.get('segments_in_run')}，"
        f"来源视频数: {report.get('distinct_assets')}",
        f"- arm C 可用数: {report.get('arm_c_available')}",
        f"- 能力状态: `{report.get('capability_state')}`",
        "",
    ]
    if report.get("arm_c_blocked_reason"):
        lines += [f"> arm C 被阻塞：{report['arm_c_blocked_reason']}", ""]

    lines += [
        "## 各臂指标",
        "",
        "| 臂 | 已评价 | event_recall | event_precision | IoU≥0.5 比例 | 备注 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for arm, values in (metrics.get("arms") or {}).items():
        lines.append(
            f"| {arm} | {values.get('evaluated')} | {values.get('event_recall')} | "
            f"{values.get('event_precision')} | "
            f"{values.get('temporal_iou_ge_threshold')} | "
            f"{values.get('reason') or ''} |"
        )

    lines += [
        "",
        f"召回增益（C 相对 A）: {metrics.get('recall_gain_points_C_over_A')} 个百分点",
        "",
        "## 采纳判定",
        "",
        f"- 已判定: {metrics.get('meets_adoption_criteria', {}).get('decided')}",
        f"- 采纳视频为首选: "
        f"{metrics.get('meets_adoption_criteria', {}).get('adopt_video_as_preferred')}",
        "",
        "## 诚实声明",
        "",
    ]
    lines += [f"- {item}" for item in report.get("honesty", [])]
    lines += ["", f"> {report.get('cost_note', '')}", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the TSB-330 A/B report")
    parser.add_argument("--project", required=True, help="project id")
    parser.add_argument("--labels", type=Path, default=None,
                        help="JSON of human annotations: {segment_id: [{start_abs_sec,...}]}")
    parser.add_argument("--segments", default="", help="comma-separated segment ids")
    parser.add_argument("--capability-report", type=Path,
                        default=Path("../docs/validation/v2.1.3/CAPABILITY_REPORT.json"))
    parser.add_argument("--json", type=Path, default=None)
    parser.add_argument("--markdown", type=Path, default=None)
    args = parser.parse_args(argv)

    settings = get_settings()
    caps = load_capability_report(args.capability_report)
    segment_ids = [item.strip() for item in args.segments.split(",") if item.strip()]

    labels = ab.load_labels(args.labels) if args.labels else {}
    arms = ab.collect_arms(args.project, segment_ids=segment_ids or None, capabilities=caps)

    report = ab.build_report(
        project_id=args.project,
        arms=arms,
        labels_by_segment=labels,
        capabilities=caps,
        endpoint_note=(
            f"model={settings.model_name or '(unconfigured)'}; "
            f"capability={caps.state}; scope={caps.verification_scope or '(none)'}"
        ),
    )

    metrics = report["metrics"]
    print("== TSB-330 A/B ==")
    print(f"  status      : {report['status']}")
    print(f"  evaluated   : {report['evaluated']}")
    print(f"  segments    : {report['segments_in_run']} "
          f"(from {report['distinct_assets']} source videos)")
    print(f"  arm C ready : {report['arm_c_available']}")
    print(f"  capability  : {caps.state}")
    for arm, values in (metrics.get("arms") or {}).items():
        print(f"  {arm:16} evaluated={values.get('evaluated')} "
              f"recall={values.get('event_recall')} "
              f"precision={values.get('event_precision')}")
    if not report["evaluated"]:
        print()
        print("NOT EVALUATED: no accuracy figure is produced.")
        if report["status"] == ab.BLOCKED_BY_ENDPOINT:
            print("  arm C is blocked by the endpoint; see CAPABILITY_REPORT.json")
        if report["status"] == ab.NOT_ANNOTATED:
            print("  no human labels supplied; pass --labels to score the arms")

    if args.json:
        ab.write_report(report, args.json)
        print(f"\nreport written: {args.json}")
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(render_markdown(report), encoding="utf-8")
        print(f"markdown written: {args.markdown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
