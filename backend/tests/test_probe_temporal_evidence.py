"""TSB-342 — video temporal capability evidence must not be forgeable.

The defects (both reproduced before the fix)
--------------------------------------------
1. **Prompt leak.** ``build_probe_prompt()`` embedded the clip's real order as the
   JSON example::

       ... for example: {"order": ["red", "green", "blue"]}

   The clip's ground truth *was* ``red, green, blue``. A model that never watched
   anything could copy the example out of the prompt, and
   ``evaluate_probe_answer()`` scored that as "reported the clip's colour order
   correctly" -- which ``classify_probe_outcome()`` then upgraded to
   ``verified_native_video``, unlocking uploads.

2. **Motion-only false positive.** When the full colour set was absent,
   ``evaluate_probe_answer()`` returned ``True`` for any text containing
   moving/motion/changes. "There is motion in this clip." therefore counted as
   proof of temporal understanding.

What the fix must guarantee
---------------------------
* The order is chosen per round from a secure random source, so it cannot be
  predicted, and the prompt never states it or shows a filled example.
* A round passes only if the answer is valid JSON, names each of the three
  colours exactly once, and matches **that round's** hidden ground truth.
* A prompt-only negative control runs the same question without the video. If the
  model answers it correctly anyway, the endpoint cannot be distinguishing the
  video from the prompt, so no round may be credited.
* All rounds must pass independently (>= 3/3). Any single failure, 5xx, timeout
  or control pass keeps the channel closed.
"""

from __future__ import annotations

import json

import pytest

from tools import probe_video_capability as probe


# --- V01: the prompt must not contain the answer ----------------------------


def test_v01_the_prompt_never_contains_a_colour_order():
    """No filled order may appear anywhere in the prompt."""
    prompt = probe.build_probe_prompt()
    lowered = prompt.lower()

    # The model must still be told which colours exist -- that is not the secret.
    for colour in probe.PROBE_COLOURS:
        assert colour in lowered, f"the prompt must name the available colour {colour}"

    # ...but it must not show any concrete 3-colour sequence.
    import itertools
    import re

    for perm in itertools.permutations(probe.PROBE_COLOURS, 3):
        pattern = r"[\[\"' ]*".join(perm) + r"[\"'\] ]*"
        assert not re.search(pattern, lowered, re.IGNORECASE), (
            f"the prompt leaks the order {perm}; a model could copy it without "
            "watching the clip"
        )


def test_v01b_the_prompt_does_not_use_a_real_round_as_its_example():
    """A concrete example must not double as a passable answer.

    Naming the colour *set* is fine and necessary ("the colours used are exactly
    red, green, blue"). What must never appear is any claim about the *order*: no
    filled JSON example, and no "in the order red, green, blue" phrasing.
    """
    prompt = probe.build_probe_prompt().lower()

    # No JSON array of colours at all -- that was the original leak.
    import re

    assert not re.search(r"\[\s*\"?(red|green|blue)\"?", prompt), (
        "the prompt must not contain a colour array; a model can copy it"
    )

    # And no ordinal/sequence wording that states the answer.
    leak_phrases = [
        "in the order red", "in the order blue", "in the order green",
        "order: red", "order: blue", "order: green",
        "first red", "first green", "first blue",
        "red first", "green first", "blue first",
    ]
    for phrase in leak_phrases:
        assert phrase not in prompt, f"the prompt leaks the order via {phrase!r}"

    # Sanity: whatever order a round picks, it is not asserted in the prompt.
    for _ in range(30):
        order = probe.choose_probe_order()
        assert f"order is {order[0]}" not in prompt
        assert f"then {order[1]}" not in prompt


def test_v01c_orders_are_randomised_across_rounds():
    """Consecutive rounds must not reuse a fixed order.

    A constant order is guessable and, worse, would make the "3 different
    permutations" requirement vacuous.
    """
    seen = {probe.choose_probe_order() for _ in range(60)}
    assert len(seen) >= 4, f"the order must vary between calls, saw only {seen}"


# --- V03/V04: the two forgeries must be rejected ----------------------------


def test_v04_motion_only_is_not_temporal_evidence():
    """V04: describing movement without an order proves nothing."""
    for text in (
        "There is motion in this clip.",
        "things are changing over time",
        "The clip shows movement and changing content.",
        "I see motion, changes, and moving objects.",
    ):
        ok, basis = probe.evaluate_probe_answer(text, expected_order=("red", "green", "blue"))
        assert ok is False, (
            f"motion-only text must NOT count as temporal evidence, got basis={basis!r}"
        )


def test_v03_a_prompt_echo_is_not_evidence():
    """V03: repeating the question back cannot be credited."""
    echo = probe.build_probe_prompt()
    ok, basis = probe.evaluate_probe_answer(echo, expected_order=("red", "green", "blue"))
    assert ok is False, f"echoing the prompt must not pass, got basis={basis!r}"


# --- V02/V05/V06: the order check itself ------------------------------------


def test_v02_a_correct_order_passes():
    ok, basis = probe.evaluate_probe_answer(
        '{"order": ["red", "green", "blue"], "motion": true}',
        expected_order=("red", "green", "blue"),
    )
    assert ok is True, f"a correct order must pass, got basis={basis!r}"


def test_v05_a_wrong_order_fails():
    ok, basis = probe.evaluate_probe_answer(
        '{"order": ["blue", "green", "red"]}', expected_order=("red", "green", "blue")
    )
    assert ok is False, f"a reversed order must fail, got basis={basis!r}"


@pytest.mark.parametrize(
    "answer",
    [
        '{"order": ["red", "green"]}',                      # missing a colour
        '{"order": ["red", "red", "green"]}',               # duplicated
        '{"order": ["red", "green", "blue", "red"]}',       # extra
        '{"order": ["red", "green", "yellow"]}',            # invalid colour
        '{"order": "red green blue"}',                      # wrong type
        '{"order": []}',                                    # empty
        "not json at all",
        "",
        '{"motion": true}',                                 # no order key
    ],
)
def test_v06_invalid_answers_are_never_evidence(answer):
    ok, basis = probe.evaluate_probe_answer(answer, expected_order=("red", "green", "blue"))
    assert ok is False, f"{answer!r} must not pass, got basis={basis!r}"


def test_v06b_the_check_is_per_round_not_global():
    """The same answer must pass only for its own round."""
    answer = '{"order": ["red", "green", "blue"]}'
    ok_own, _ = probe.evaluate_probe_answer(answer, expected_order=("red", "green", "blue"))
    ok_other, _ = probe.evaluate_probe_answer(answer, expected_order=("blue", "red", "green"))
    assert ok_own is True
    assert ok_other is False, "an answer must be judged against its own round's order"


# --- V07/V08: classification ------------------------------------------------


def test_v07_http_200_without_a_valid_order_is_not_verified():
    """V07: a 200 that shows no attributable temporal content stays unknown."""
    from tools.probe_video_capability import ProbeOutcome, classify_probe_outcome

    out = ProbeOutcome(attempted=True, http_status=200)
    out.temporal_signal = False
    state, _ = classify_probe_outcome(out)
    assert state != "verified_native_video"
    assert state == "video_accepted_sampling_unknown"


def test_v07b_a_single_frame_control_blocks_verification():
    """V07: if a still frame also "passes", the endpoint is not reading time."""
    from tools.probe_video_capability import ProbeOutcome, classify_probe_outcome

    out = ProbeOutcome(attempted=True, http_status=200)
    out.temporal_signal = True
    out.temporal_signal_basis = "reported the clip's colour order correctly"
    # A still-frame control that also answered correctly means the result is not
    # attributable to the video. `attempted` must be set too: a control that was
    # never run cannot be credited as having failed.
    out.frame_control_attempted = True
    out.frame_control_signal = True
    state, reason = classify_probe_outcome(out)
    assert state != "verified_native_video", (
        "a still frame that answers the same question means the probe cannot tell "
        "video from a thumbnail"
    )
    assert state == "video_accepted_sampling_unknown"


def test_v03b_a_prompt_only_control_that_passes_blocks_verification():
    """V03: the decisive control -- no video, same question."""
    from tools.probe_video_capability import ProbeOutcome, classify_probe_outcome

    out = ProbeOutcome(attempted=True, http_status=200)
    out.temporal_signal = True
    out.temporal_signal_basis = "reported the clip's colour order correctly"
    out.control_attempted = True  # the control must actually have been run
    out.control_signal = True     # ...and it guessed the order right
    state, reason = classify_probe_outcome(out)
    assert state != "verified_native_video", (
        "if the model answers the same question without the video, the answer "
        "cannot be attributed to the video"
    )
    assert "control" in reason.lower() or "prompt" in reason.lower(), (
        f"the reason must name the control, got {reason!r}"
    )


@pytest.mark.parametrize("status", [401, 403, 413, 429, 500, 503])
def test_v08_error_statuses_are_never_verified(status):
    from tools.probe_video_capability import ProbeOutcome, classify_probe_outcome

    out = ProbeOutcome(attempted=True, http_status=status)
    out.temporal_signal = True  # even a "temporal" body cannot save an error
    state, _ = classify_probe_outcome(out)
    assert state != "verified_native_video"


def test_v08b_a_timeout_is_never_verified():
    from tools.probe_video_capability import ProbeOutcome, classify_probe_outcome

    out = ProbeOutcome(attempted=True, http_status=None, error_code="MODEL_TIMEOUT")
    out.temporal_signal = True
    state, _ = classify_probe_outcome(out)
    assert state != "verified_native_video"


# --- V02 aggregate: the multi-round rule ------------------------------------


def test_a3_a_three_round_pass_is_required_to_verify():
    """V02: >= 3/3 independent rounds, each with its own order."""
    orders = [("red", "green", "blue"), ("blue", "red", "green"), ("green", "blue", "red")]
    rounds = [
        probe.RoundEvidence(
            round_index=r,
            probe_id=f"probe-{r}",
            order=o,
            passed=True,
            basis="reported the clip's colour order correctly",
        )
        for r, o in enumerate(orders)
    ]
    ev = probe.ProbeEvidence(rounds=rounds, control_answered_correctly=False,
                             control_attempted=True)
    assert probe.evidence_supports_verification(ev) is True
    assert ev.passed_rounds == 3


def test_a3b_two_of_three_is_not_enough():
    ev = probe.ProbeEvidence(
        rounds=[
            probe.RoundEvidence(0, "p0", ("red", "green", "blue"), True, "correct order"),
            probe.RoundEvidence(1, "p1", ("blue", "red", "green"), True, "correct order"),
            probe.RoundEvidence(2, "p2", ("green", "blue", "red"), False, "order mismatch"),
        ],
        control_answered_correctly=False, control_attempted=True,
    )
    assert probe.evidence_supports_verification(ev) is False, (
        "2/3 must not verify: one failure means the reading is not reproducible"
    )


def test_a3c_a_passing_control_blocks_even_three_correct_rounds():
    ev = probe.ProbeEvidence(
        rounds=[
            probe.RoundEvidence(i, f"p{i}", o, True, "correct order")
            for i, o in enumerate(
                [("red", "green", "blue"), ("blue", "red", "green"), ("green", "blue", "red")]
            )
        ],
        control_answered_correctly=True, control_attempted=True,
    )
    assert probe.evidence_supports_verification(ev) is False, (
        "a prompt-only control that answers correctly means the endpoint may be "
        "reading the prompt, not the video"
    )


def test_a3d_an_unrun_control_blocks_verification():
    """The control must actually have been performed to count as absent."""
    ev = probe.ProbeEvidence(
        rounds=[
            probe.RoundEvidence(i, f"p{i}", o, True, "correct order")
            for i, o in enumerate(
                [("red", "green", "blue"), ("blue", "red", "green"), ("green", "blue", "red")]
            )
        ],
        control_answered_correctly=False, control_attempted=False,
    )
    assert probe.evidence_supports_verification(ev) is False, (
        "a control that was never run cannot be treated as having failed"
    )


def test_a3e_rounds_are_distinct_permutations():
    ev = probe.ProbeEvidence(
        rounds=[
            probe.RoundEvidence(i, f"p{i}", ("red", "green", "blue"), True, "correct order")
            for i in range(3)
        ],
        control_answered_correctly=False, control_attempted=True,
    )
    assert probe.evidence_supports_verification(ev) is False, (
        "three identical rounds are one round repeated; the orders must differ"
    )


# --- evidence / report shape -------------------------------------------------


def test_the_evidence_record_is_structured_and_serialisable():
    ev = probe.ProbeEvidence(
        rounds=[probe.RoundEvidence(0, "p0", ("red", "green", "blue"), True, "correct order")],
        control_answered_correctly=False, control_attempted=True,
    )
    data = ev.to_dict()
    assert data["rounds_total"] == 1
    assert data["rounds_passed"] == 1
    assert data["control_attempted"] is True
    assert json.loads(json.dumps(data))  # serialisable
    # The hidden ground truth belongs in the local record, but the round order must
    # be reported so an auditor can see the rounds differed.
    assert data["rounds"][0]["order"] == ["red", "green", "blue"]
    assert data["rounds"][0]["probe_id"] == "p0"


def test_the_public_summary_carries_no_order_or_endpoint():
    """The committed report must not contain the answers or the private endpoint."""
    ev = probe.ProbeEvidence(
        rounds=[
            probe.RoundEvidence(i, f"p{i}", o, True, "correct order")
            for i, o in enumerate(
                [("red", "green", "blue"), ("blue", "red", "green"), ("green", "blue", "red")]
            )
        ],
        control_answered_correctly=False, control_attempted=True,
    )
    public = ev.public_summary()
    blob = json.dumps(public).lower()

    # Match whole colour words: a naive substring test is fooled by keys like
    # "answe*red*_correctly", which disclose nothing.
    import re

    for colour in probe.PROBE_COLOURS:
        assert not re.search(rf"\b{colour}\b", blob), (
            f"a committed summary must not disclose the round orders (found {colour!r})"
        )
    for key in ("http://", "https://", "api_key", "bearer", "/users/"):
        assert key not in blob, f"public summary must not contain {key!r}"
    assert public["rounds_total"] == 3 and public["rounds_passed"] == 3
    # Per-round detail must stay auditable without carrying the answers.
    assert len(public["rounds"]) == 3
    for entry in public["rounds"]:
        assert "order" not in entry, "the per-round order must not be committed"
        assert "probe_id" not in entry, "the probe id is correlatable; keep it local"


# --- wiring: the driver must actually use the rounds and the controls --------


def _fake_probe_factory(behaviour):
    """Build a stand-in for ``run_probe`` that answers from the round order."""
    calls = []

    def _probe(*, clip, model_id, authorization, timeout_sec=120.0, max_bytes=0,
               round_order=None, probe_id="", include_video=True, frame_only=False):
        from tools.probe_video_capability import ProbeOutcome

        calls.append({
            "round_order": tuple(round_order or ()),
            "probe_id": probe_id,
            "include_video": include_video,
            "frame_only": frame_only,
        })
        out = ProbeOutcome(attempted=True, http_status=200, probe_id=probe_id)
        out.round_order = tuple(round_order or ())
        ok, basis = behaviour(round_order, include_video, frame_only)
        out.temporal_signal = ok
        out.temporal_signal_basis = basis
        return out

    _probe.calls = calls
    return _probe


def test_w1_the_driver_runs_three_distinct_rounds_and_both_controls(tmp_path, monkeypatch):
    """The production driver must perform the rounds *and* the controls."""
    # Avoid real encoding: the fake clip is never inspected by the fake probe.
    clip = tmp_path / "fake.mp4"
    clip.write_bytes(b"\x00" * 2048)
    monkeypatch.setattr(probe, "build_synthetic_clip", lambda **kw: clip)

    honest = _fake_probe_factory(
        lambda order, has_video, frame_only: (
            (True, "reported this round's colour order correctly")
            if (has_video and not frame_only)
            else (False, "no video in the request")
        )
    )
    ev = probe.run_multi_round_probe(
        model_id="test-model", authorization=object(), workdir=tmp_path, _probe=honest
    )

    assert ev.rounds_total == 3, "three rounds are required"
    assert ev.passed_rounds == 3
    assert len({tuple(r.order) for r in ev.rounds}) == 3, "orders must differ"
    assert ev.control_attempted is True, "the prompt-only control must run"
    assert ev.control_answered_correctly is False
    assert ev.frame_control_attempted is True, "the still-frame control must run"

    # The control request really had no video part.
    assert any(c["include_video"] is False for c in honest.calls)
    assert probe.evidence_supports_verification(ev) is True


def test_w2_the_driver_does_not_verify_when_the_control_answers(tmp_path, monkeypatch):
    """If the video-free control answers, the result is not attributable."""
    clip = tmp_path / "fake.mp4"
    clip.write_bytes(b"\x00" * 2048)
    monkeypatch.setattr(probe, "build_synthetic_clip", lambda **kw: clip)

    guessing = _fake_probe_factory(lambda order, has_video, frame_only: (True, "guessed"))
    ev = probe.run_multi_round_probe(
        model_id="test-model", authorization=object(), workdir=tmp_path, _probe=guessing
    )
    assert ev.control_answered_correctly is True
    assert probe.evidence_supports_verification(ev) is False, (
        "a model that can answer without the video must not unlock the channel"
    )


def test_w3_the_driver_passes_the_round_order_to_the_probe(tmp_path, monkeypatch):
    """Wiring: the hidden order must reach the checker per round."""
    clip = tmp_path / "fake.mp4"
    clip.write_bytes(b"\x00" * 2048)
    monkeypatch.setattr(probe, "build_synthetic_clip", lambda **kw: clip)

    seen = _fake_probe_factory(lambda order, has_video, frame_only: (False, "n/a"))
    probe.run_multi_round_probe(
        model_id="m", authorization=object(), workdir=tmp_path, _probe=seen
    )
    round_calls = [c for c in seen.calls if c["include_video"] and not c["frame_only"]]
    assert len(round_calls) == 3
    for call in round_calls:
        assert len(call["round_order"]) == 3, "each round must carry its own order"
        assert call["probe_id"], "each round must carry a probe id"


def test_w4_a_failing_round_blocks_verification(tmp_path, monkeypatch):
    """One bad round among three is enough to refuse."""
    clip = tmp_path / "fake.mp4"
    clip.write_bytes(b"\x00" * 2048)
    monkeypatch.setattr(probe, "build_synthetic_clip", lambda **kw: clip)

    state = {"n": 0}

    def behaviour(order, has_video, frame_only):
        if has_video and not frame_only:
            state["n"] += 1
            if state["n"] == 2:
                return False, "reported the wrong order"
            return True, "reported this round's colour order correctly"
        return False, "no video"

    ev = probe.run_multi_round_probe(
        model_id="m", authorization=object(), workdir=tmp_path,
        _probe=_fake_probe_factory(behaviour),
    )
    assert ev.passed_rounds == 2
    assert probe.evidence_supports_verification(ev) is False
