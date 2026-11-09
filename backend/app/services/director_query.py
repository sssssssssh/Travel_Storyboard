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

    P1-03: this must identify *everything the next stage reads*, not just the
    shot skeleton. A digest over identity and rating alone stayed constant while
    the human note changed from "普通海景" to "旅程情绪高潮", and while a shot was
    re-analysed into different evidence — so a downstream plan could be cached
    against input it never saw.

    Covered (and nothing else):
      * identity and window            — what the shot *is*
      * normalized human rating/role/note — what the user decided
      * eligibility                    — whether it may be planned at all
      * per-channel result id + state + a digest of the *content the planner will
        read* (scene tags, subjects, actions, motion summary, video summary)
      * limitations that change meaning — the honest caveats travel with it

    Deliberately absent: filesystem paths, tokens, raw model responses, request
    usage, timestamps, random ids. The digest is a change detector over public,
    normalized fields — not proof that source bytes are unchanged; readings whose
    signatures cannot be recomputed stay ``freshness_unverified``.
    """
    normalized = {
        "project_id": project_id,
        "brief": {
            "theme": brief.get("theme", ""),
            "target_duration_sec": brief.get("target_duration_sec"),
        },
        "candidates": [_digest_entry(c) for c in sorted(candidates, key=_TIMELINE_KEY)],
    }
    blob = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _content_fingerprint(channel) -> str | None:
    """A stable digest of the content a planner would read from this channel.

    Hashing the projected content (rather than the row id) is what makes a
    re-analysis detectable: a new row with identical content is genuinely the
    same input, while an edited reading over the same row is not.
    """
    if not channel.result_id:
        return None
    payload = channel.payload or {}
    visual = payload.get("visual") or {}
    material = {
        "visual": {k: visual.get(k) for k in sorted(visual)} if visual else {},
        "evidence_state": payload.get("evidence_state", ""),
        "motion_type": payload.get("motion_type", ""),
        "action_summary": payload.get("action_summary", ""),
        "summary": payload.get("summary", ""),
        "events": [
            {"start": e.get("start_sec"), "end": e.get("end_sec"), "label": e.get("label")}
            for e in (payload.get("events") or [])
        ],
    }
    blob = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _digest_entry(c: Candidate) -> dict[str, Any]:
    return {
        "segment_id": c.segment_id,
        "asset_id": c.asset_id,
        "start": round(c.source_start, 3),
        "end": round(c.source_end, 3),
        "eligibility": c.eligibility,
        # Human decisions, verbatim — the note is part of the input the planner reads.
        "rating": c.human["rating"],
        "story_role": c.human["story_role"],
        "note": c.human["note"],
        # Each channel: is it usable, which version, and what does it say.
        "static": {
            "state": c.static.state,
            "result_id": c.static.result_id,
            "source_version": c.static.source_version,
            "content": _content_fingerprint(c.static),
        },
        "multiframe": {
            "state": c.multiframe.state,
            "result_id": c.multiframe.result_id,
            "source_version": c.multiframe.source_version,
            "content": _content_fingerprint(c.multiframe),


        },
        "video_native": {
            "state": c.video_native.state,
            "result_id": c.video_native.result_id,
            "source_version": c.video_native.source_version,
            "content": _content_fingerprint(c.video_native),


        },
        "priority": c.editorial["priority"],
        "limitations": list(c.limitations),
    }


def build_context(
    *,
    project_id: str,
    candidates: Sequence[Candidate],
    coverage: dict[str, Any],
    brief: dict[str, Any],
    max_candidates: int = 40,
) -> dict[str, Any]:
    """The read-only planning input for TSB-369. Generates no story."""
    # P1-02: ONLY `eligible` shots are offered to the planner.
    #
    # `needs_review` means a human has not confirmed the source yet. It stays
    # visible in /candidates?eligibility=all and is counted in coverage, but it
    # must not be silently ordered into a story — a plan that reaches for
    # unconfirmed footage is exactly the failure the review gate exists to stop.
    # If a future product decision wants "force use anyway", that needs its own
    # explicit acknowledgement contract rather than a quiet widening here.
    usable = [c for c in candidates if c.eligibility == ELIGIBLE]
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
    withheld_review = int(coverage.get("needs_review_segments", 0))
    withheld_excluded = int(coverage.get("excluded_segments", 0))
    limitations: list[str] = ["no_story_generated_this_round"]
    if withheld_review:
        limitations.append("shots_awaiting_review_withheld_from_planning")
    if withheld_excluded:
        limitations.append("human_excluded_shots_withheld_from_planning")
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
        # The planner is told what it did NOT get, so a later stage cannot
        # mistake silence for "there was nothing there".
        "withheld": {
            "needs_review_segments": withheld_review,
            "excluded_segments": withheld_excluded,
        },
    }
