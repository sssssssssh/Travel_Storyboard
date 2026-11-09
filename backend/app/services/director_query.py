"""TSB-368-B — filtering, ordering, pagination and the planning context.

All ranking here is a **stated policy over observable facts**, not a predicted
audience score. There is deliberately no composite number: nothing in the data
calibrates one, and a fabricated decimal would be indistinguishable from a real
measurement in downstream use.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Literal, Sequence

from .director_candidates import (
    ELIGIBLE,
    EXCLUDED,
    NEEDS_REVIEW,
    STATE_CURRENT,
    STATE_UNVERIFIED,
    Candidate,
)

SortKey = Literal["editorial_priority", "timeline"]

#: Ordering of the discrete priority buckets, highest first.
PRIORITY_RANK = {"preferred": 0, "noted": 1, "unrated": 2, "excluded": 3}

#: Stable tie-break: a shot is fully identified by (asset, start, segment id).
_TIMELINE_KEY = lambda c: (c.asset_id, c.source_start, c.segment_id)  # noqa: E731


def _editorial_key(c: Candidate) -> tuple:
    return (
        PRIORITY_RANK.get(c.editorial["priority"], 9),
        # Human-set roles and notes outrank machine evidence inside a bucket.
        0 if c.human["story_role"] else 1,
        0 if c.human["note"] else 1,
        # Then "we have actually looked at it", then position.
        0 if any(
            ch.state == STATE_CURRENT
            for ch in (c.static, c.multiframe, c.video_native)
        ) else 1,
        *_TIMELINE_KEY(c),
    )


def _matches(c: Candidate, *, eligibility: str, rating: str | None,
             story_role: str | None, asset_id: str | None,
             scene_tag: str | None, q: str | None) -> bool:
    if eligibility != "all" and c.eligibility != eligibility:
        return False
    if rating and c.human["rating"] != rating:
        return False
    if story_role and c.human["story_role"] != story_role:
        return False
    if asset_id and c.asset_id != asset_id:
        return False
    if scene_tag:
        # A tag may match when a usable static reading exists. "Usable" includes
        # `freshness_unverified` — otherwise the filter would return nothing on
        # any machine that cannot re-read the source, which is the normal case.
        # A shot with NO reading must never match: that would invent content.
        if c.static.state not in (STATE_CURRENT, STATE_UNVERIFIED):
            return False
        if not c.static.result_id:
            return False
        tags = (c.static.payload.get("visual") or {}).get("scene_tags") or []
        if scene_tag not in tags:
            return False
    if q:
        needle = q.strip().lower()
        if not needle:
            return False
        haystack: list[str] = [c.human["note"], c.human["story_role"]]
        visual = (c.static.payload.get("visual") or {}) if c.static.state in (
            STATE_CURRENT, STATE_UNVERIFIED
        ) else {}
        for key in ("subjects", "visible_actions", "scene_tags"):
            haystack.extend(str(v) for v in (visual.get(key) or []))
        tags = c.technical.get("content_tags") or {}
        haystack.extend(str(v) for v in tags.values())
        if not any(needle in h.lower() for h in haystack if h):
            return False
    return True


def _round_robin(items: Sequence[Candidate]) -> list[Candidate]:
    """Interleave assets so one long clip cannot monopolise the first page.

    Deliberately does NOT filter on "has a frame": that is a property of the
    analysis queue, not of material the director may want.
    """
    buckets: dict[str, list[Candidate]] = {}
    order: list[str] = []
    for c in items:
        if c.asset_id not in buckets:
            buckets[c.asset_id] = []
            order.append(c.asset_id)
        buckets[c.asset_id].append(c)
    out: list[Candidate] = []
    while any(buckets[a] for a in order):
        for a in order:
            if buckets[a]:
                out.append(buckets[a].pop(0))
    return out


def select(
    candidates: Sequence[Candidate],
    *,
    eligibility: str = ELIGIBLE,
    rating: str | None = None,
    story_role: str | None = None,
    asset_id: str | None = None,
    scene_tag: str | None = None,
    q: str | None = None,
    sort: SortKey = "editorial_priority",
    diversify: bool = True,
    offset: int = 0,
    limit: int = 30,
) -> tuple[list[Candidate], int]:
    """Filter, order and page. Returns ``(page_items, total_before_paging)``."""
    kept = [
        c
        for c in candidates
        if _matches(
            c, eligibility=eligibility, rating=rating, story_role=story_role,
            asset_id=asset_id, scene_tag=scene_tag, q=q,
        )
    ]

    if sort == "timeline":
        kept.sort(key=_TIMELINE_KEY)
    else:
        kept.sort(key=_editorial_key)

    total = len(kept)
    if diversify and sort == "editorial_priority":
        # Interleave footage while preserving priority ORDER strictly.
        #
        # Run over the WHOLE filtered list, not just the current page: slicing
        # first would hand the interleaver only the assets that happen to sort
        # early, which is how a 3-asset library ends up with a 2-asset page.
        #
        # At each position: take the highest-priority bucket that still has
        # items, and inside it pick the shot whose asset has appeared least so
        # far. A favourite therefore always precedes an unrated shot, yet one
        # long clip cannot own the page.
        buckets: dict[int, list[Candidate]] = {}
        for c in kept:
            buckets.setdefault(PRIORITY_RANK.get(c.editorial["priority"], 9), []).append(c)
        diversified: list[Candidate] = []
        counts: dict[str, int] = {}
        remaining = sum(len(v) for v in buckets.values())
        while remaining:
            rank = min(r for r, v in buckets.items() if v)
            row = buckets[rank]
            pick = min(
                range(len(row)),
                key=lambda i: (counts.get(row[i].asset_id, 0),
                               row[i].source_start, row[i].segment_id),
            )
            chosen = row.pop(pick)
            diversified.append(chosen)
            counts[chosen.asset_id] = counts.get(chosen.asset_id, 0) + 1
            remaining -= 1
        kept = diversified
    return kept[offset: offset + limit], total


def input_digest(project_id: str, candidates: Iterable[Candidate], brief: dict[str, Any]) -> str:
    """Deterministic digest of the normalized planning input.

    Contains no timestamp and no randomness, so the same library and the same
    brief always produce the same value. It is a *change detector*, not proof
    that source files are unchanged — sources whose signatures cannot be
    recomputed stay ``freshness_unverified``.
    """
    normalized = {
        "project_id": project_id,
        "brief": {"theme": brief.get("theme", ""),
                  "target_duration_sec": brief.get("target_duration_sec")},
        "candidates": [
            {
                "segment_id": c.segment_id,
                "asset_id": c.asset_id,
                "start": round(c.source_start, 3),
                "end": round(c.source_end, 3),
                "rating": c.human["rating"],
                "story_role": c.human["story_role"],
                "static": c.static.state,
                "multiframe": c.multiframe.state,
                "video_native": c.video_native.state,
                "edit_plan": c.editorial["priority"],
            }
            for c in sorted(candidates, key=_TIMELINE_KEY)
        ],
    }
    blob = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def build_context(
    *,
    project_id: str,
    candidates: Sequence[Candidate],
    coverage: dict[str, Any],
    brief: dict[str, Any],
    max_candidates: int = 40,
) -> dict[str, Any]:
    """The read-only planning input for TSB-369. Generates no story."""
    # Only shots a planner may actually use; excluded ones are never offered.
    usable = [c for c in candidates if c.eligibility in (ELIGIBLE, NEEDS_REVIEW)]
    usable.sort(key=_editorial_key)
    chosen = usable[:max_candidates]

    summaries = []
    for c in chosen:
        readings = []
        for name, ch in (("static", c.static), ("multiframe", c.multiframe),
                         ("video_native", c.video_native)):
            if ch.result_id:
                readings.append({"channel": name, "state": ch.state,
                                 "result_id": ch.result_id})
        summaries.append({
            "segment_id": c.segment_id,
            "asset_id": c.asset_id,
            "source_window": {"start_sec": c.source_start, "end_sec": c.source_end},
            "eligibility": c.eligibility,
            "human": {"rating": c.human["rating"], "story_role": c.human["story_role"],
                      "note": c.human["note"][:200]},
            "editorial_priority": c.editorial["priority"],
            "priority_reasons": c.editorial["reasons"][:8],
            "readings": readings,
            "limitations": c.limitations[:8],
        })

    labelled = [
        {
            "segment_id": c.segment_id,
            "asset_id": c.asset_id,
            "rating": c.human["rating"],
            "story_role": c.human["story_role"],
            "note": c.human["note"][:200],
        }
        for c in usable
        if c.human["rating"] in ("favorite", "like")
        or c.human["story_role"]
        or c.human["note"]
    ][:20]

    uncovered = int(coverage.get("no_valid_model_reading", 0))
    limitations: list[str] = ["no_story_generated_this_round"]
    if uncovered:
        limitations.append("many_shots_not_analyzed")
    if not coverage.get("coverage_complete"):
        limitations.append("partial_analysis_coverage")
    if any(
        ch.state == STATE_UNVERIFIED
        for c in chosen
        for ch in (c.static, c.multiframe, c.video_native)
    ):
        limitations.append("freshness_unverified_for_some_readings")

    return {
        "schema_version": "1.0",
        "project_id": project_id,
        "input_digest": input_digest(project_id, chosen, brief),
        "brief": {
            "theme": brief.get("theme", ""),
            "target_duration_sec": brief.get("target_duration_sec"),
            "note": "client-supplied constraint text; not verified as video content",
        },
        "coverage": coverage,
        "candidate_segment_ids": [c.segment_id for c in chosen],
        "candidate_summaries": summaries,
        "important_human_labels": labelled,
        "limitations": limitations,
        "needs_more_analysis": uncovered > 0,
        "uncovered_segment_count": uncovered,
    }
