"""TSB-325 — native-video capability probe (default: dry-run).

Answers the task book's P0 question: *does this endpoint actually accept video, and
does it tell us anything temporal about it?* Until that is answered with evidence,
:data:`model_supports_native_video` must stay ``false`` and no video may be sent.

Safety design — four independent gates before a single byte leaves the machine:

1. **dry-run is the default.** Without ``--execute`` the tool prints the protocol
   it *would* use, the byte estimate and the risks, then stops. This is the mode
   CI and casual inspection run in.
2. **``--execute`` alone is not enough.** It also demands
   ``--i-authorize-real-upload``, so an operator cannot turn a real upload on by
   half-remembering one flag.
3. **A synthetic clip by default.** ``--execute`` sends a generated, silent,
   element-changing test pattern. Pointing it at real user footage requires
   ``--clip <path>`` and is refused unless the operator passes
   ``--i-authorize-real-upload`` *and* the file is not under a project workspace.
4. **No credentials in output.** The report carries a model id and capability
   facts only. Keys, base URLs and file paths are never written; the endpoint
   host is redacted to its scheme+host form only when explicitly requested.

Nothing here is a capability *claim*. The tool reports what it observed, and
:func:`classify_probe_outcome` maps that onto one of the four honest states. In
particular a 2xx with no usable temporal content is recorded as
``video_accepted_sampling_unknown`` — never as verified.

Usage::

    python tools/probe_video_capability.py                     # dry-run
    python tools/probe_video_capability.py --json out.json      # dry-run + report
    python tools/probe_video_capability.py --execute --i-authorize-real-upload
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.services.vision import video_capabilities as vc  # noqa: E402
from app.services.vision.errors import redact  # noqa: E402

#: Reported when the operator supplied a real clip instead of the synthetic one.
REAL_CLIP_SENTINEL = "operator_supplied_clip"

#: Prefix of the temp directory :func:`build_synthetic_clip` writes into. Used to
#: tell our own generated clip apart from a clip the operator pointed at, so the
#: report never labels generated content as the operator's footage (or worse, the
#: reverse).
SYNTHETIC_DIR_PREFIX = "tsb-video-probe-"


def clip_source_is_real(clip: Path | None) -> bool:
    """True when ``clip`` is operator-supplied rather than our synthetic pattern."""
    if clip is None:
        return False
    try:
        return SYNTHETIC_DIR_PREFIX not in str(clip.parent.name) and not clip.parent.name.startswith(
            SYNTHETIC_DIR_PREFIX
        )
    except OSError:  # pragma: no cover - defensive
        return True

#: Files larger than this are never sent, matching the adapter's inline ceiling.
#: Reported as an observed limit only if the server itself rejected something;
#: otherwise it stays a local guard rail.
LOCAL_MAX_BYTES = 12_000_000

#: The colour each third of the probe clip is painted, in order. A model that
#: actually decoded the clip can report this sequence; one that saw a single
#: frame (or only read the prompt) cannot. Kept as data so the prompt, the clip
#: builder and the checker cannot drift apart.
PROBE_PHASE_ORDER: tuple[str, str, str] = ("red", "green", "blue")

#: The colours a probe round may use. The *set* is public -- the prompt names it --
#: but the per-round **order** is the secret and must never appear in a prompt or
#: in a committed report (TSB-342).
PROBE_COLOURS: tuple[str, str, str] = PROBE_PHASE_ORDER


# ---------------------------------------------------------------------------
# synthetic test clip
# ---------------------------------------------------------------------------

def build_synthetic_clip(
    duration_sec: float = 3.0,
    order: tuple[str, ...] | None = None,
) -> Path:
    """Generate a silent, element-changing MP4 for probing.

    The content changes *over time* on purpose: a static clip could be answered
    correctly by a server that decoded exactly one frame, which would make the
    probe unable to distinguish "read the video" from "read a thumbnail".

    TSB-341: this used to burn in a frame counter with ``drawtext``, which is
    absent from FFmpeg builds without a compile-time freetype dependency. Since
    this tool is the only way to produce the capability record TSB-335 consults,
    such a build made the whole native-video closure unreachable -- the probe died
    with an FFmpegError before it could even report "unavailable".

    The replacement paints the clip in three distinct colour phases and asks the
    model for the order. Only ``color`` and ``drawbox`` are used, both of which are
    present in every build the project accepts. This is also *stronger* evidence
    than the old keyword scan, because a wrong order can now be detected rather
    than merely un-matched.
    """
    from app.ffmpeg import locate_ffmpeg, run

    out = Path(tempfile.mkdtemp(prefix="tsb-video-probe-")) / "probe.mp4"
    third = max(duration_sec, 0.3) / 3.0
    # TSB-342: the order is chosen per round and kept only in local memory. It is
    # never written into the prompt, and never committed.
    first, second, third_c = order or PROBE_PHASE_ORDER
    # A moving box keeps the clip visibly dynamic; the colour phases give an
    # orderable, checkable signal. `enable` windows select each phase by time.
    vf = (
        f"drawbox=x=0:y=0:w=320:h=240:color={first}@1:t=fill:enable='lt(t,{third:.3f})',"
        f"drawbox=x=0:y=0:w=320:h=240:color={second}@1:t=fill"
        f":enable='between(t,{third:.3f},{2 * third:.3f})',"
        f"drawbox=x=0:y=0:w=320:h=240:color={third_c}@1:t=fill"
        f":enable='gte(t,{2 * third:.3f})',"
        "drawbox=x='mod(t*120,320)':y=100:w=80:h=80:color=white@1:t=fill"
    )
    cmd = [
        locate_ffmpeg(),
        "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi",
        "-i", f"color=c=black:size=320x240:rate=10:duration={duration_sec}",
        "-vf", vf,
        "-an",                      # no audio track at all
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(out),
    ]
    run(cmd, expect_output=out)
    return out


def choose_probe_order() -> tuple[str, str, str]:
    """Pick this round's colour order from a cryptographically secure source.

    TSB-342: the order must not be predictable and must differ between rounds. A
    constant order (or one leaked in the prompt) lets a model answer correctly
    without decoding anything.
    """
    import secrets

    colours = list(PROBE_COLOURS)
    secrets.SystemRandom().shuffle(colours)
    return (colours[0], colours[1], colours[2])


def new_probe_id() -> str:
    """A fresh identifier so each round's request and answer pair up unambiguously."""
    import uuid

    return uuid.uuid4().hex


def build_probe_prompt() -> str:
    """The question asked about the probe clip.

    TSB-342: this deliberately does **not** state the order and does **not** show a
    filled example. The previous version ended with
    ``for example: {"order": ["red", "green", "blue"]}`` while the clip's real
    order was exactly that, so a model could copy the answer out of the prompt and
    be scored as having understood the video. Only the *set* of colours is named
    here -- that is not the secret, the order is.
    """
    colours = ", ".join(PROBE_COLOURS)
    return (
        "This is a short silent test clip. It is divided into three equal parts, "
        f"each filled with one solid colour. The three colours used are exactly "
        f"{colours}, one per part, each appearing once, in an order you must "
        "determine by watching. Report the order you actually observed, from the "
        "first part to the last. Reply with JSON only, with a key \"order\" whose "
        "value is a list of the three colour names in the order you saw them (do "
        "not copy an example -- there is none, and the order is not stated "
        "anywhere in this message). Also include a key \"motion\" (true/false) "
        "for whether anything moved. If you cannot watch video, reply exactly "
        "NO_VIDEO."
    )


def parse_probe_order(text: str) -> tuple[list[str] | None, str]:
    """Strictly parse ``{"order": [...]}`` from an answer.

    Returns ``(order, reason)``. ``order`` is ``None`` unless the reply is valid
    JSON, has an ``order`` list of exactly the three distinct probe colours.
    Loose prose is never accepted: the task book requires strict JSON parsing.
    """
    raw = (text or "").strip()
    if not raw:
        return None, "empty response"
    if "no_video" in raw.lower():
        return None, "model reported NO_VIDEO"

    # Accept a fenced or embedded JSON object, but nothing looser than JSON.
    candidate = raw
    if "```" in candidate:
        import re as _re

        block = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, _re.S)
        if block:
            candidate = block.group(1)
    if not candidate.lstrip().startswith("{"):
        import re as _re

        obj = _re.search(r"\{.*\}", candidate, _re.S)
        if not obj:
            return None, "response is not JSON"
        candidate = obj.group(0)

    try:
        parsed = json.loads(candidate)
    except (ValueError, TypeError):
        return None, "response is not valid JSON"
    if not isinstance(parsed, dict):
        return None, "response is not a JSON object"

    order = parsed.get("order")
    if not isinstance(order, list):
        return None, "response has no \"order\" list"
    if len(order) != 3:
        return None, f"expected 3 colours, got {len(order)}"

    normalised = [str(item).strip().lower() for item in order]
    allowed = set(PROBE_COLOURS)
    if any(item not in allowed for item in normalised):
        return None, f"unknown colour in order {normalised}"
    if len(set(normalised)) != 3:
        return None, f"colours must each appear once, got {normalised}"
    return normalised, ""


def evaluate_probe_answer(
    text: str, *, expected_order: tuple[str, ...] | None = None
) -> tuple[bool, str]:
    """Did the answer report **this round's** colour order?

    TSB-342 removed two ways this used to be forgeable:

    * a filled example in the prompt let a non-watching model echo the answer;
    * when the colour set was incomplete, any text mentioning motion returned
      ``True``, so "there is motion in this clip" counted as temporal evidence.

    An answer now passes only if it is strict JSON naming each probe colour once in
    the order this round actually encoded.
    """
    order, reason = parse_probe_order(text)
    if order is None:
        return False, reason

    expected = list(expected_order if expected_order is not None else PROBE_PHASE_ORDER)
    if order == expected:
        return True, "reported this round's colour order correctly"
    return False, f"reported order {order} but this round is {expected}"


@dataclass
class RoundEvidence:
    """One probe round: what was encoded, what came back, whether it matched."""

    round_index: int
    probe_id: str
    order: tuple[str, ...]
    passed: bool
    basis: str = ""
    http_status: int | None = None
    error_code: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        data["order"] = list(self.order)
        return data

    def public_dict(self) -> dict:
        """Anonymous view for a committed report: no order, no endpoint."""
        return {
            "round": self.round_index,
            "probe_id": self.probe_id,
            "passed": self.passed,
            "basis": self.basis,
            "http_status": self.http_status,
            "error_code": self.error_code,
        }


@dataclass
class ProbeEvidence:
    """The whole probe attempt, including the negative controls.

    TSB-342 requires more than "the model answered correctly": correctness has to
    be attributable to the video. Two controls establish that -- the same question
    with no video, and the same question against a single still frame. If either
    also answers correctly, the endpoint is not demonstrably reading time.
    """

    rounds: list[RoundEvidence] = field(default_factory=list)
    control_attempted: bool = False
    control_answered_correctly: bool = False
    frame_control_attempted: bool = False
    frame_control_answered_correctly: bool = False

    @property
    def rounds_total(self) -> int:
        return len(self.rounds)

    @property
    def passed_rounds(self) -> int:
        return sum(1 for r in self.rounds if r.passed)

    def to_dict(self) -> dict:
        return {
            "rounds": [r.to_dict() for r in self.rounds],
            "rounds_total": self.rounds_total,
            "rounds_passed": self.passed_rounds,
            "control_attempted": self.control_attempted,
            "control_answered_correctly": self.control_answered_correctly,
            "frame_control_attempted": self.frame_control_attempted,
            "frame_control_answered_correctly": self.frame_control_answered_correctly,
        }

    def public_summary(self) -> dict:
        """Anonymous summary safe to commit.

        Deliberately omits the round orders: publishing them would tell a future
        run's model what to say (and the committed report should not carry the
        answers to its own question).
        """
        return {
            "rounds_total": self.rounds_total,
            "rounds_passed": self.passed_rounds,
            "control_attempted": self.control_attempted,
            "control_answered_correctly": self.control_answered_correctly,
            "frame_control_attempted": self.frame_control_attempted,
            "frame_control_answered_correctly": self.frame_control_answered_correctly,
            "rounds": [
                {
                    "round": r.round_index,
                    "passed": r.passed,
                    "basis": r.basis,
                    "http_status": r.http_status,
                    "error_code": r.error_code,
                }
                for r in self.rounds
            ],
        }


#: How many independent rounds must pass. The task book requires >= 3.
REQUIRED_PROBE_ROUNDS = 3


def evidence_supports_verification(evidence: ProbeEvidence) -> bool:
    """Does this evidence justify ``verified_native_video``?

    Every condition must hold, and each one exists because omitting it was a way
    to be fooled:

    * at least ``REQUIRED_PROBE_ROUNDS`` rounds, **all** passing -- a single
      failure, 5xx or timeout means the reading is not reproducible;
    * the rounds used **distinct** permutations -- otherwise they are one round
      repeated, and a guessed order would pass all of them;
    * the prompt-only control **was run** and did **not** answer correctly, so the
      result is attributable to the video rather than to the question;
    * the still-frame control, if run, did not answer correctly either.
    """
    if len(self_rounds := evidence.rounds) < REQUIRED_PROBE_ROUNDS:
        return False
    if any(not r.passed for r in self_rounds):
        return False
    orders = [tuple(r.order) for r in self_rounds]
    if len(set(orders)) < len(orders):
        return False
    # A control that was never performed cannot be treated as having failed.
    if not evidence.control_attempted or evidence.control_answered_correctly:
        return False
    if evidence.frame_control_attempted and evidence.frame_control_answered_correctly:
        return False
    return True


# ---------------------------------------------------------------------------
# outcome classification
# ---------------------------------------------------------------------------

@dataclass
class ProbeOutcome:
    """What a single probe attempt observed. No credentials, no paths."""

    attempted: bool = False
    http_status: int | None = None
    error_code: str = ""
    #: The response text, truncated and credential-redacted.
    response_excerpt: str = ""
    #: Did the answer appear to be *about* the clip's temporal content? Set only
    #: by inspecting the response, never assumed from the status code.
    temporal_signal: bool = False
    temporal_signal_basis: str = ""
    usage_present: bool = False
    latency_ms: int = 0
    notes: list[str] = field(default_factory=list)
    #: Control probe: a text-only request to the same endpoint. Without this, an
    #: infrastructure outage (503 from a dead gateway) is indistinguishable from
    #: a genuine video rejection, and the report would imply the endpoint
    #: evaluated our video when it never did.
    control_http_status: int | None = None
    control_excerpt: str = ""

    # --- TSB-342 negative controls -------------------------------------------
    #: Which round this observation belongs to, so the answer is judged against
    #: *its own* hidden order rather than a global constant.
    probe_id: str = ""
    round_order: tuple[str, ...] = ()
    #: The prompt-only control: the same question with the video removed. If it
    #: answers correctly, the model was never reading the video.
    control_signal: bool = False
    control_attempted: bool = False
    #: The still-frame control: the same question against one frame. A pass here
    #: means a thumbnail was enough, so the endpoint cannot be shown to read time.
    frame_control_signal: bool = False
    frame_control_attempted: bool = False


def control_probe(*, model_id: str, timeout_sec: float = 60.0) -> tuple[int | None, str]:
    """A text-only request, to tell "endpoint is down" from "video unsupported".

    Returns ``(status, redacted_excerpt)``; ``(None, reason)`` when the request
    could not be made at all.
    """
    import httpx

    settings = get_settings()
    url = f"{settings.model_base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.model_api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model_id,
        "messages": [{"role": "user", "content": "reply with the single word: ok"}],
        "max_tokens": 10,
    }
    try:
        resp = httpx.post(url, json=body, headers=headers, timeout=timeout_sec)
    except httpx.TimeoutException:
        return None, "control request timed out"
    except httpx.HTTPError as exc:
        return None, redact(str(exc), settings.model_api_key)
    return resp.status_code, redact(resp.text[:300], settings.model_api_key)


def _classify_video_rejection(body: str) -> str:
    """Turn a rejection body into a precise, actionable reason.

    "HTTP 400" is not a useful verdict on its own. The vLLM family answers with a
    message that says *why*, and the difference between "this server cannot do
    video at all" and "this server can, but the deployment's per-prompt video
    limit is 0" changes the remedy completely: the first needs a different model,
    the second needs a serving flag. So the body is inspected for that.
    """
    lowered = (body or "").lower()
    if "limit-mm-per-prompt" in lowered or "may be provided in one prompt" in lowered:
        return (
            "vLLM recognises the video content part, but this deployment's "
            "per-prompt video limit is 0 — the serving configuration needs "
            "`--limit-mm-per-prompt video=1` (a server-side change, not client-side)"
        )
    if "unsupported chat content part type" in lowered:
        return "server does not recognise this content part type at all"
    if "not support" in lowered:
        return "server states the model does not support this input"
    return ""


def classify_probe_outcome(outcome: ProbeOutcome) -> tuple[str, str]:
    """Map an observation onto ``(state, verification_scope)``.

    Returns one of the four honest states. The rule that matters: a successful
    call with no demonstrable temporal reading is
    ``video_accepted_sampling_unknown``, because the task book explicitly refuses
    to call that "verified" (a server may have taken one frame, or ignored the
    file and answered from the prompt).
    """
    if not outcome.attempted:
        return vc.UNVERIFIED, ""

    if outcome.http_status is None:
        return vc.UNVERIFIED, f"probe did not complete: {outcome.error_code or 'unknown'}"

    # A failing *control* request proves the endpoint is unhealthy, which makes
    # the video attempt uninformative: we cannot claim the endpoint rejected
    # video, and we certainly cannot claim it accepted it.
    if outcome.control_http_status is not None and outcome.control_http_status >= 500:
        return (
            vc.UNVERIFIED,
            f"endpoint unhealthy: text-only control also returned HTTP "
            f"{outcome.control_http_status}; the video attempt is inconclusive",
        )

    if outcome.http_status in (415, 422, 400):
        precise = _classify_video_rejection(outcome.response_excerpt)
        base = f"endpoint refused the video payload (HTTP {outcome.http_status})"
        return vc.UNSUPPORTED, f"{base}: {precise}" if precise else base
    if outcome.http_status in (401, 403):
        return vc.UNVERIFIED, f"probe was not accepted for auth reasons (HTTP {outcome.http_status})"
    if outcome.http_status == 413:
        return vc.UNSUPPORTED, "endpoint rejected the payload as too large (HTTP 413)"
    if outcome.http_status >= 500:
        return vc.UNVERIFIED, f"endpoint error (HTTP {outcome.http_status}); inconclusive"
    if outcome.http_status != 200:
        return vc.UNVERIFIED, f"unexpected status HTTP {outcome.http_status}"

    if not outcome.temporal_signal:
        return (
            vc.VIDEO_ACCEPTED_SAMPLING_UNKNOWN,
            "request succeeded but the response showed no usable temporal "
            "information attributable to the video; server-side sampling unknown",
        )

    # TSB-342: a correct answer only counts if it is attributable to the video.
    # If the same question is answered correctly without the video, or from a
    # single still frame, the endpoint has not been shown to read time -- the
    # answer may come from the prompt or from one decoded frame.
    if outcome.control_attempted and outcome.control_signal:
        return (
            vc.VIDEO_ACCEPTED_SAMPLING_UNKNOWN,
            "a prompt-only control answered the same question correctly, so the "
            "result cannot be attributed to the video; server-side sampling unknown",
        )
    if outcome.frame_control_attempted and outcome.frame_control_signal:
        return (
            vc.VIDEO_ACCEPTED_SAMPLING_UNKNOWN,
            "a single-frame control answered the same question correctly, so a "
            "thumbnail suffices and temporal reading is unproven",
        )

    return (
        vc.VERIFIED_NATIVE_VIDEO,
        f"2xx with temporal content ({outcome.temporal_signal_basis or 'unstated'})",
    )


# TSB-341 removed ``_looks_temporal``: it accepted any answer containing "then",
# "first" or "from", so a model naming the clip's phases in the wrong order still
# scored as proof of temporal understanding. ``evaluate_probe_answer`` checks the
# reported order against the clip that was actually sent and *can* return False.
# Do not reintroduce a keyword-only check here.


# ---------------------------------------------------------------------------
# dry-run planning
# ---------------------------------------------------------------------------

@dataclass
class ProbePlan:
    """What a real probe *would* do. Safe to print and to commit as evidence."""

    protocol: str
    transport: str
    mime: str
    clip_source: str
    clip_bytes: int
    clip_duration_sec: float
    has_audio: bool
    endpoint_scheme: str
    endpoint_host_redacted: str
    model_id: str
    model_mode: str
    risks: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def plan_probe(
    *,
    clip: Path | None,
    model_id: str,
    endpoint: str,
    model_mode: str,
    protocol_name: str = "video_url_data_uri",
) -> ProbePlan:
    """Build the dry-run plan, including the risks a human should weigh."""
    protocol = vc.get_protocol(protocol_name)
    from urllib.parse import urlparse

    parsed = urlparse(endpoint or "")
    host = parsed.hostname or ""
    # Keep the host only as an explicit "this is where it goes" line; never the
    # full URL, which can carry a path or query with a token in it.
    redacted = f"{parsed.scheme}://{host}" if host else "(unconfigured)"

    clip_bytes = clip.stat().st_size if clip and clip.exists() else 0
    using_real_clip = clip is not None and clip_source_is_real(clip)
    risks: list[str] = [
        "真实执行会把派生短片发送到远端模型服务；dry-run 不会发送任何字节。",
        "本工具只做能力探测，不会把结果写成对模型的信任；未通过时 video 通道保持禁用。",
        "探测结论只覆盖本次所用协议与模型 ID，不推断其他部署。",
    ]
    if parsed.scheme == "http" and host not in {"127.0.0.1", "::1", "localhost"}:
        risks.append(
            "端点是明文 http:// 且非回环：图片与令牌在网络上不加密，需显式 insecure 例外。"
        )
    if model_mode != "on":
        risks.append(
            f"TRAVELSB_MODEL_MODE={model_mode!r}（非 on）：真实执行会被授权边界拒绝，这是预期行为。"
        )
    else:
        risks.append(
            "TRAVELSB_MODEL_MODE=on：授权边界允许本次外发，请求会真正离开本机。"
        )
    if using_real_clip:
        risks.append(
            "使用运行者提供的真实短片：请确认画面不含需要遮蔽的人脸/车牌；"
            "去元数据不等于画面脱敏。"
        )
    else:
        risks.append("默认使用合成的静音测试图样，不含任何用户素材。")

    return ProbePlan(
        protocol=protocol.name,
        transport=protocol.transport,
        mime=sorted(protocol.allowed_mimes)[0],
        clip_source=REAL_CLIP_SENTINEL if using_real_clip else "synthetic_testsrc",
        clip_bytes=clip_bytes,
        clip_duration_sec=0.0 if using_real_clip else 3.0,
        has_audio=False,
        endpoint_scheme=parsed.scheme or "(none)",
        endpoint_host_redacted=redacted,
        model_id=model_id,
        model_mode=model_mode,
        risks=risks,
    )


# ---------------------------------------------------------------------------
# the probe itself
# ---------------------------------------------------------------------------

def run_probe(
    *,
    clip: Path,
    model_id: str,
    authorization,
    timeout_sec: float = 120.0,
    max_bytes: int = LOCAL_MAX_BYTES,
    round_order: tuple[str, ...] | None = None,
    probe_id: str = "",
    include_video: bool = True,
    frame_only: bool = False,
) -> ProbeOutcome:
    """Send one video request and report what happened.

    Requires an explicit ``authorization`` context: this function will not build
    one, so the caller cannot probe by accident. All failure modes are classified
    rather than raised, because "the probe failed" is itself a result the report
    must carry.
    """
    import base64

    import httpx

    from app.services.vision.authorization import require_context

    require_context(authorization)

    settings = get_settings()
    outcome = ProbeOutcome(attempted=True)
    outcome.probe_id = probe_id
    outcome.round_order = tuple(round_order or PROBE_PHASE_ORDER)

    data = clip.read_bytes()
    if len(data) > max_bytes:
        outcome.notes.append(f"clip {len(data)}B exceeds local guard {max_bytes}B; not sent")
        outcome.error_code = "LOCAL_SIZE_GUARD"
        return outcome

    payload = {
        "model": model_id,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        # TSB-341: ask for the clip's actual colour order, so the
                        # answer can be checked against ground truth instead of
                        # scanned for time-ish words.
                        "text": build_probe_prompt(),
                    },
                    *(
                        [
                            {
                                "type": "video_url",
                                "video_url": {
                                    "url": "data:video/mp4;base64,"
                                    + base64.b64encode(data).decode("ascii")
                                },
                            }
                        ]
                        if include_video
                        else []
                    ),
                ],
            }
        ],
        "max_tokens": 400,
    }
    url = f"{settings.model_base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.model_api_key}",
        "Content-Type": "application/json",
    }

    started = time.monotonic()
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=timeout_sec)
    except httpx.TimeoutException as exc:
        outcome.error_code = "MODEL_TIMEOUT"
        outcome.notes.append(redact(str(exc), settings.model_api_key))
    except httpx.HTTPError as exc:
        outcome.error_code = "MODEL_NETWORK_ERROR"
        outcome.notes.append(redact(str(exc), settings.model_api_key))
    else:
        outcome.http_status = resp.status_code
        outcome.latency_ms = int((time.monotonic() - started) * 1000)
        body = redact(resp.text[:2000], settings.model_api_key)
        outcome.response_excerpt = body
        try:
            parsed = resp.json()
        except ValueError:
            parsed = {}
        text = ""
        try:
            text = parsed["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            text = ""
        outcome.usage_present = isinstance(parsed.get("usage"), dict) and bool(parsed.get("usage"))
        if "no_video" in (text or "").lower():
            # The model explicitly said it saw no video. That is a *negative*
            # result and must not be softened into "accepted".
            outcome.temporal_signal = False
            outcome.temporal_signal_basis = "model reported NO_VIDEO"
            outcome.notes.append("model explicitly reported no video content")
        else:
            # TSB-341: check the reported colour order against the clip that was
            # actually sent. This can return False for a fluent wrong answer, which
            # the previous keyword scan could not.
            found, basis = evaluate_probe_answer(
                text, expected_order=outcome.round_order
            )
            outcome.temporal_signal = found
            outcome.temporal_signal_basis = basis

    return outcome


def run_multi_round_probe(
    *,
    model_id: str,
    authorization,
    rounds: int = REQUIRED_PROBE_ROUNDS,
    duration_sec: float = 3.0,
    timeout_sec: float = 120.0,
    run_frame_control: bool = True,
    workdir: Path | None = None,
    _probe=run_probe,
) -> ProbeEvidence:
    """Run the full TSB-342 battery: N randomised rounds plus negative controls.

    Each round encrypts a **fresh random order** into its own clip and asks the
    same video-free question. The order lives only in this process: it is not in
    the prompt and not written to disk. A round is credited only if the reply is
    strict JSON matching *that round's* order.

    Two controls then test attribution, because "the model answered correctly" is
    not by itself evidence that it watched anything:

    * **prompt-only** -- the identical question with no video. A correct answer
      here means the model produced the order from the question alone.
    * **single frame** -- the same question against one still frame. A correct
      answer means a thumbnail suffices.
    """
    evidence = ProbeEvidence()
    # Cache one clip per distinct order; three rounds is small and the encode is
    # the slow part of a probe.
    clips: dict[tuple[str, ...], Path] = {}

    def _clip_for(order: tuple[str, ...]) -> Path:
        # One clip per distinct order: the encode dominates a probe's runtime.
        if order not in clips:
            clips[order] = build_synthetic_clip(duration_sec=duration_sec, order=order)
        return clips[order]

    # --- the timed rounds ---------------------------------------------------
    for index in range(rounds):
        order = choose_probe_order()
        # Distinct permutations only: three identical rounds are one round
        # repeated, and a guessed order would pass all of them.
        attempts = 0
        while order in clips and attempts < 10:
            order = choose_probe_order()
            attempts += 1
        probe_id = new_probe_id()

        clip = _clip_for(order)
        out = _probe(
            clip=clip, model_id=model_id, authorization=authorization,
            timeout_sec=timeout_sec, round_order=order, probe_id=probe_id,
        )
        evidence.rounds.append(
            RoundEvidence(
                round_index=index,
                probe_id=probe_id,
                order=order,
                passed=bool(out.temporal_signal),
                basis=out.temporal_signal_basis,
                http_status=out.http_status,
                error_code=out.error_code,
            )
        )

    # --- control 1: prompt only, no video -----------------------------------
    control_order = choose_probe_order()
    control_id = new_probe_id()
    control_out = _probe(
        clip=_clip_for(control_order), model_id=model_id, authorization=authorization,
        timeout_sec=timeout_sec, round_order=control_order, probe_id=control_id,
        include_video=False,
    )
    evidence.control_attempted = True
    evidence.control_answered_correctly = bool(control_out.temporal_signal)

    # --- control 2: a single still frame ------------------------------------
    if run_frame_control:
        frame_order = choose_probe_order()
        frame_out = _probe(
            clip=_clip_for(frame_order), model_id=model_id, authorization=authorization,
            timeout_sec=timeout_sec, round_order=frame_order,
            probe_id=new_probe_id(), frame_only=True,
        )
        evidence.frame_control_attempted = True
        evidence.frame_control_answered_correctly = bool(frame_out.temporal_signal)

    return evidence


# ---------------------------------------------------------------------------
# console output
# ---------------------------------------------------------------------------

def configure_console_output() -> None:
    """Make console writes survive a non-UTF-8 code page.

    TSB-343: the risk list and several notes are Chinese, and a Windows console
    defaults to a legacy code page (cp1252/cp936). Printing them raised::

        UnicodeEncodeError: 'charmap' codec can't encode characters ... <undefined>

    which killed the probe before it could report anything -- including the
    dry-run result CI asserts on. Two changes fix this without pretending the
    console is UTF-8:

    * ``errors="backslashreplace"`` on the std streams, so an unencodable glyph
      degrades to an escape instead of an exception;
    * ``reconfigure`` is attempted first (Python 3.7+), which is a no-op where the
      stream is already UTF-8.

    This is output robustness only: it does not touch how anything is computed.
    """
    import sys

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError, OSError):
            # Not a TextIOWrapper (or detached); the safe_print fallback covers it.
            pass


def _ascii_if_requested(text: str, args) -> str:
    """ASCII-only rendering for stable machine parsing under any console.

    ``--ascii`` exists because a Windows job may run with a legacy code page; the
    escaped form keeps the output parseable instead of merely non-crashing.
    """
    if not getattr(args, "ascii_only", False):
        return text
    return str(text).encode("ascii", "backslashreplace").decode("ascii")


def safe_print(*parts, sep: str = " ", end: str = "\n") -> None:
    """Print without ever raising on an unencodable character."""
    import sys

    text = sep.join(str(p) for p in parts)
    try:
        print(text, end=end)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        sys.stdout.write(text.encode(enc, "backslashreplace").decode(enc, "replace") + end)


# ---------------------------------------------------------------------------
# report writing
# ---------------------------------------------------------------------------

def build_report(
    *,
    plan: ProbePlan,
    outcome: ProbeOutcome,
    executed: bool,
) -> dict:
    """Assemble the redacted capability report (task book §6.1)."""
    state, scope = classify_probe_outcome(outcome)
    caps = (
        vc.record_verification(
            state=state,
            protocol=plan.protocol,
            model_id=plan.model_id,
            verification_scope=scope,
            detail={"temporal_signal_basis": outcome.temporal_signal_basis},
        )
        if executed
        else vc.unverified_capabilities(
            model_id=plan.model_id, reason="dry-run only; no authorised probe performed"
        )
    )
    return {
        "task": "TSB-325",
        "executed": executed,
        "mode": "real_probe" if executed else "dry_run",
        "plan": plan.to_dict(),
        "observation": {
            "attempted": outcome.attempted,
            "http_status": outcome.http_status,
            "error_code": outcome.error_code,
            "temporal_signal": outcome.temporal_signal,
            "temporal_signal_basis": outcome.temporal_signal_basis,
            "usage_present": outcome.usage_present,
            "latency_ms": outcome.latency_ms,
            "notes": outcome.notes,
            # The control request is the evidence that separates "the endpoint
            # never evaluated our video" from "the endpoint rejected video".
            "control_http_status": outcome.control_http_status,
            "control_excerpt": outcome.control_excerpt[:200],
            # Excerpt is already redacted and truncated; kept for the report but
            # it is model prose about a synthetic clip, not user content.
            "response_excerpt": outcome.response_excerpt[:800],
        },
        "capabilities": caps.to_dict(),
        "not_verified": [
            "不得由本报告推断其他模型/部署的视频能力",
            "服务端抽帧 fps/上限未确认时保持 temporal_sampling_disclosed=false",
            "未做边界压测，故 max_bytes / max_duration_sec 保持 unknown",
        ],
    }


#: Patterns that must never reach a committed evidence file.
_REDACTION_PATTERNS: tuple[tuple[str, str], ...] = (
    # Absolute POSIX paths (macOS/Linux), including a trailing filename.
    (r"/(?:Users|home|private|var|tmp|mnt|Volumes)/[^\s\"',;)\]}]+", "<path>"),
    # Windows drive paths.
    (r"[A-Za-z]:\\[^\s\"',;)\]}]+", "<path>"),
    # Any absolute path segment that looks user-specific.
    (r"/(?:Users|home)/[^/\s]+", "<home>"),
    # Bearer / token material. Run this before the generic blob rule so the
    # credential itself is replaced, not just its label.
    (r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}", r"\1 <redacted>"),
    (r"(?i)\b(token|api[_-]?key|authorization|password|secret)\b\s*[:=]\s*\S+",
     r"\1=<redacted>"),
    # Any remaining long opaque hex/base64 blob that looks like a credential.
    (r"\b[A-Za-z0-9+/_-]{24,}={0,2}\b", "<redacted>"),
    # Bare hostnames / URLs left in prose.
    (r"https?://[^\s\"',;)\]}]+", "<endpoint>"),
    (r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)+\.(?:com|cn|net|io|internal|local)(?::\d+)?\b", "<endpoint>"),
    # Real media filenames.
    (r"\b[A-Za-z0-9_-]{2,}\.(?:MP4|MOV|mp4|mov|avi|mkv)\b", "<clip>"),
)


def _redact_text_for_commit(text: str) -> str:
    """Scrub paths, endpoints and credentials from one string."""
    import re as _re

    out = text
    for pattern, replacement in _REDACTION_PATTERNS:
        out = _re.sub(pattern, replacement, out)
    return out


def _redact_report_for_commit(report: dict) -> dict:
    """Strip anything that could identify the endpoint, the operator or a file.

    TSB-339: this used to replace exactly one field
    (``plan.endpoint_host_redacted``). The endpoint host also appears inside the
    human-readable ``plan.risks`` prose, and notes/excerpts can carry local
    absolute paths and token-shaped strings -- all of which survived and would
    have been committed under ``docs/validation/``. Redaction is now a deep walk
    over every string in the report, so a new field cannot silently bypass it.

    The input is not mutated: the caller keeps its full-fidelity in-memory copy.
    """
    cleaned = json.loads(json.dumps(report))

    def scrub(value):
        if isinstance(value, dict):
            return {k: scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [scrub(v) for v in value]
        if isinstance(value, str):
            return _redact_text_for_commit(value)
        return value

    cleaned = scrub(cleaned)
    plan = cleaned.setdefault("plan", {})
    plan["endpoint_host_redacted"] = "(redacted for committed evidence)"
    plan["model_id"] = plan.get("model_id") or ""
    cleaned["redaction"] = (
        "paths, endpoint hosts and credential-shaped strings replaced; "
        "see _REDACTION_PATTERNS"
    )
    return cleaned


def main(argv: list[str] | None = None) -> int:
    configure_console_output()
    parser = argparse.ArgumentParser(description="Probe native-video support (dry-run by default)")
    parser.add_argument("--execute", action="store_true", help="actually send a request")
    parser.add_argument(
        "--i-authorize-real-upload",
        action="store_true",
        dest="authorize_upload",
        help="explicit per-run consent; required together with --execute",
    )
    parser.add_argument("--clip", type=Path, default=None, help="use this clip instead of the synthetic one")
    parser.add_argument("--protocol", default="video_url_data_uri")
    parser.add_argument("--json", type=Path, default=None, help="write the report here")
    parser.add_argument("--redact", action="store_true", help="strip endpoint host (for committed evidence)")
    parser.add_argument(
        "--ascii",
        action="store_true",
        dest="ascii_only",
        help=(
            "render free-form text as pure ASCII (escaped), so output stays "
            "parseable on a non-UTF-8 console such as a legacy Windows code page"
        ),
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=REQUIRED_PROBE_ROUNDS,
        help=(
            "how many randomised video rounds to require; each round encodes its own "
            "order and must be answered correctly for a positive result "
            f"(default {REQUIRED_PROBE_ROUNDS})"
        ),
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=3.0,
        help="seconds per synthetic probe clip",
    )
    parser.add_argument(
        "--record-capability",
        action="store_true",
        dest="record_capability",
        help=(
            "write the outcome to the local capability store so the video route can "
            "stop answering 422; requires --execute (a dry run has no evidence), and "
            "the record is never committed"
        ),
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    model_id = settings.model_name or "(unconfigured)"

    # Gate 2: refuse a half-specified real run, and say precisely why.
    executed = bool(args.execute)
    if executed and not args.authorize_upload:
        safe_print("REFUSED: --execute also requires --i-authorize-real-upload")
        safe_print("         (a real video upload is an outbound transfer of media; it needs explicit consent)")
        return 2

    clip: Path | None = None
    if executed:
        if args.clip is not None:
            if not args.clip.is_file():
                safe_print(f"REFUSED: clip not found: {args.clip}")
                return 2
            clip = args.clip
        else:
            clip = build_synthetic_clip()
            safe_print(f"synthetic clip: {clip.name} ({clip.stat().st_size} bytes, silent)")

    plan = plan_probe(
        clip=clip,
        model_id=model_id,
        endpoint=settings.model_base_url,
        model_mode=settings.model_mode,
        protocol_name=args.protocol,
    )

    outcome = ProbeOutcome()
    if executed:
        from app.services.vision.authorization import authorize_upload

        # Gate 3/4: build the context through the product's own boundary, so the
        # probe cannot bypass the same rules the product enforces.
        try:
            context = authorize_upload(
                project_id="capability-probe",
                upload_acknowledged=True,
                purpose="video_capability_probe",
            )
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            safe_print(f"REFUSED by the authorization boundary: {exc}")
            return 3
        # Say it before doing it: an operator must see that this run is outbound,
        # and to which host, rather than discovering it from the report.
        safe_print(
            f"ABOUT TO SEND: {plan.clip_bytes} bytes of "
            f"{'operator-supplied' if clip_source_is_real(clip) else 'synthetic'} video "
            f"to {plan.endpoint_host_redacted} (model {plan.model_id})"
        )
        # Always run the text-only control: it is what makes an outage
        # distinguishable from a real capability answer.
        control_status, control_excerpt = control_probe(model_id=model_id)

        evidence = ProbeEvidence()
        if args.rounds > 1:
            # TSB-342: the deciding battery. Several randomised rounds plus a
            # prompt-only control, because a single correct answer can come from
            # the prompt -- especially when the prompt used to contain it.
            safe_print(
                f"running {args.rounds} randomised rounds + prompt-only/frame "
                "controls (the order is never stated in the prompt)"
            )
            if clip is not None and clip_source_is_real(clip):
                outcome = run_probe(clip=clip, model_id=model_id, authorization=context)
            else:
                evidence = run_multi_round_probe(
                    model_id=model_id,
                    authorization=context,
                    rounds=args.rounds,
                    duration_sec=args.duration,
                )
                outcome = ProbeOutcome(attempted=True)
                outcome.probe_id = evidence.rounds[0].probe_id if evidence.rounds else ""
                outcome.temporal_signal = evidence_supports_verification(evidence)
                outcome.temporal_signal_basis = (
                    f"{evidence.passed_rounds}/{evidence.rounds_total} rounds correct"
                    + (
                        "; prompt-only control also answered correctly, so the "
                        "result is not attributable to the video"
                        if evidence.control_answered_correctly
                        else ""
                    )
                )
                outcome.control_attempted = evidence.control_attempted
                outcome.control_signal = evidence.control_answered_correctly
                outcome.frame_control_attempted = evidence.frame_control_attempted
                outcome.frame_control_signal = evidence.frame_control_answered_correctly
                # A round that did not pass must not look like a clean 200.
                failed = [r for r in evidence.rounds if not r.passed]
                if failed and failed[0].http_status is not None:
                    outcome.http_status = failed[0].http_status
                else:
                    outcome.http_status = 200
                if failed and failed[0].error_code:
                    outcome.error_code = failed[0].error_code
                for r in evidence.rounds:
                    outcome.notes.append(
                        f"round {r.round_index}: {'pass' if r.passed else 'FAIL'} "
                        f"({r.basis or r.error_code or 'no reason'})"
                    )
        else:
            outcome = run_probe(clip=clip, model_id=model_id, authorization=context)

        outcome.control_http_status = control_status
        outcome.control_excerpt = control_excerpt

    report = build_report(plan=plan, outcome=outcome, executed=executed)
    # The anonymous, committable view of the battery: rounds and pass counts, no
    # orders, no probe ids, no endpoint.
    report["evidence"] = evidence.public_summary() if executed else None
    if args.redact:
        report = _redact_report_for_commit(report)

    state = report["capabilities"]["state"]
    sendable = report["capabilities"]["sendable"]

    safe_print()
    safe_print("== video capability probe ==")
    safe_print(f"  mode            : {report['mode']}")
    safe_print(f"  protocol        : {plan.protocol} ({plan.transport})")
    safe_print(f"  clip            : {plan.clip_source}, {plan.clip_bytes} bytes, has_audio={plan.has_audio}")
    safe_print(f"  endpoint        : {plan.endpoint_host_redacted} ({plan.endpoint_scheme})")
    safe_print(f"  model           : {plan.model_id}")
    safe_print(f"  state           : {state}")
    safe_print(f"  sendable        : {sendable}")
    if report["capabilities"]["verification_scope"]:
        safe_print(f"  scope           : {report['capabilities']['verification_scope']}")
    safe_print()
    safe_print("  risks:")
    for risk in plan.risks:
        safe_print(f"    - {_ascii_if_requested(risk, args)}")

    if not executed:
        safe_print()
        safe_print("DRY-RUN: nothing was sent. Re-run with --execute --i-authorize-real-upload")
        safe_print("         only when you explicitly approve uploading a video to this endpoint.")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        safe_print(f"\nreport written: {args.json}")

    # TSB-335: close the probe -> load -> use loop. Without this the probe could
    # succeed while the route kept answering 422 forever, because nothing ever
    # wrote the runtime record that ``current_capabilities()`` reads.
    if args.record_capability:
        _record_capability(report, plan=plan, executed=executed)

    return 0


def _record_capability(report: dict, *, plan, executed: bool) -> None:
    """Persist the probe outcome as the runtime capability record.

    Only a probe that actually ran can be recorded: a dry run has no evidence, so
    writing one would be exactly the "assume it works" mistake this task exists to
    prevent. ``unsupported`` and ``video_accepted_sampling_unknown`` are recorded
    too, so the UI can explain *why* the channel is closed instead of showing a
    blank "unverified".
    """
    import hashlib

    from app.services.vision.capability_store import (
        EndpointIdentity,
        build_record,
        save_record,
    )

    if not executed:
        safe_print()
        safe_print("NOT RECORDED: a dry run produces no evidence; re-run with --execute first.")
        safe_print("              (recording an unproven positive would defeat the guard)")
        return

    caps = report.get("capabilities") or {}
    state = caps.get("state", "unverified")
    if state == "unverified":
        safe_print()
        safe_print("NOT RECORDED: the probe did not establish a usable capability state.")
        return

    scope = caps.get("verification_scope", "")
    # Evidence over the machine-readable outcome, so a hand-edited report cannot
    # masquerade as probe output.
    digest_material = json.dumps(
        {
            "state": state,
            "protocol": caps.get("protocol", ""),
            "model_id": caps.get("model_id", ""),
            "scope": scope,
            "temporal_sampling_disclosed": caps.get("temporal_sampling_disclosed", False),
        },
        sort_keys=True,
        ensure_ascii=False,
    )

    from app.config import get_settings

    identity = EndpointIdentity(
        base_url=(get_settings().model_base_url or "").rstrip("/"),
        model_name=caps.get("model_id", "") or plan.model_id,
        protocol=caps.get("protocol", "") or plan.protocol,
        deployment_id="",
    )

    record = build_record(
        identity=identity,
        state=state,
        protocol=caps.get("protocol", "") or plan.protocol,
        model_id=caps.get("model_id", "") or plan.model_id,
        verification_scope=scope,
        evidence_digest=hashlib.sha256(digest_material.encode("utf-8")).hexdigest(),
        evidence_summary=str(report.get("verification_scope", "") or scope)[:500],
        max_duration_sec=caps.get("max_duration_sec"),
        max_bytes=caps.get("max_bytes"),
        accepts_audio=caps.get("accepts_audio"),
        temporal_sampling_disclosed=bool(caps.get("temporal_sampling_disclosed", False)),
    )
    path = save_record(record, identity=identity)
    safe_print()
    safe_print(f"capability recorded: {path}")
    safe_print(f"  state bound to endpoint: {state}")
    safe_print("  the record is local and outside the repository; it is never committed.")


if __name__ == "__main__":
    raise SystemExit(main())
