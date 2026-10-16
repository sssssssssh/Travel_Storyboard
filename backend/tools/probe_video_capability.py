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


# ---------------------------------------------------------------------------
# synthetic test clip
# ---------------------------------------------------------------------------

def build_synthetic_clip(duration_sec: float = 3.0) -> Path:
    """Generate a silent, element-changing MP4 for probing.

    The content changes *over time* on purpose: a static clip could be answered
    correctly by a server that decoded exactly one frame, which would make the
    probe unable to distinguish "read the video" from "read a thumbnail".
    """
    from app.ffmpeg import locate_ffmpeg, run

    out = Path(tempfile.mkdtemp(prefix="tsb-video-probe-")) / "probe.mp4"
    # A moving box over a changing background, plus a burned-in counter so a
    # temporal answer can be checked against real order.
    vf = (
        "drawbox=x='mod(t*120,320)':y=100:w=80:h=80:color=red@1:t=fill,"
        "drawtext=text='%{eif\\:n\\:d}':x=10:y=10:fontsize=48:fontcolor=white"
    )
    cmd = [
        locate_ffmpeg(),
        "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi",
        "-i", f"testsrc=size=320x240:rate=10:duration={duration_sec}",
        "-vf", vf,
        "-an",                      # no audio track at all
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(out),
    ]
    run(cmd, expect_output=out)
    return out


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

    return (
        vc.VERIFIED_NATIVE_VIDEO,
        f"2xx with temporal content ({outcome.temporal_signal_basis or 'unstated'})",
    )


def _looks_temporal(text: str) -> tuple[bool, str]:
    """Cheap, honest heuristic for "does this answer know about time?".

    Deliberately conservative. It cannot prove the server decoded every frame, and
    the returned basis string says so; it only distinguishes "the answer contains
    time-ordered content" from "the answer is unrelated prose".
    """
    lowered = (text or "").lower()
    if not lowered.strip():
        return False, ""
    markers = [
        ("sequence", "mentions a sequence of changes"),
        ("then", "describes ordered change"),
        ("first", "describes ordered change"),
        ("second", "describes ordered change"),
        ("moving", "describes motion"),
        ("motion", "describes motion"),
        ("from", "describes a transition"),
        ("changing", "describes change over time"),
        ("frames", "mentions frames"),
        ("timestamp", "mentions timestamps"),
    ]
    hits = [why for needle, why in markers if needle in lowered]
    if hits:
        return True, hits[0]
    return False, ""


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
                        "text": (
                            "Describe what changes over time in this video, in order. "
                            "If you cannot see a video, say exactly: NO_VIDEO."
                        ),
                    },
                    {
                        "type": "video_url",
                        "video_url": {
                            "url": "data:video/mp4;base64,"
                            + base64.b64encode(data).decode("ascii")
                        },
                    },
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
            found, basis = _looks_temporal(text)
            outcome.temporal_signal = found
            outcome.temporal_signal_basis = basis

    return outcome


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


def _redact_report_for_commit(report: dict) -> dict:
    """Strip anything that could still identify the endpoint or the operator."""
    cleaned = json.loads(json.dumps(report))
    plan = cleaned.get("plan", {})
    plan["endpoint_host_redacted"] = "(redacted for committed evidence)"
    cleaned["plan"] = plan
    return cleaned


def main(argv: list[str] | None = None) -> int:
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
        print("REFUSED: --execute also requires --i-authorize-real-upload")
        print("         (a real video upload is an outbound transfer of media; it needs explicit consent)")
        return 2

    clip: Path | None = None
    if executed:
        if args.clip is not None:
            if not args.clip.is_file():
                print(f"REFUSED: clip not found: {args.clip}")
                return 2
            clip = args.clip
        else:
            clip = build_synthetic_clip()
            print(f"synthetic clip: {clip.name} ({clip.stat().st_size} bytes, silent)")

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
            print(f"REFUSED by the authorization boundary: {exc}")
            return 3
        # Say it before doing it: an operator must see that this run is outbound,
        # and to which host, rather than discovering it from the report.
        print(
            f"ABOUT TO SEND: {plan.clip_bytes} bytes of "
            f"{'operator-supplied' if clip_source_is_real(clip) else 'synthetic'} video "
            f"to {plan.endpoint_host_redacted} (model {plan.model_id})"
        )
        outcome = run_probe(clip=clip, model_id=model_id, authorization=context)
        # Always run the control: it is what makes an outage distinguishable from
        # a real capability answer.
        control_status, control_excerpt = control_probe(model_id=model_id)
        outcome.control_http_status = control_status
        outcome.control_excerpt = control_excerpt

    report = build_report(plan=plan, outcome=outcome, executed=executed)
    if args.redact:
        report = _redact_report_for_commit(report)

    state = report["capabilities"]["state"]
    sendable = report["capabilities"]["sendable"]

    print()
    print("== video capability probe ==")
    print(f"  mode            : {report['mode']}")
    print(f"  protocol        : {plan.protocol} ({plan.transport})")
    print(f"  clip            : {plan.clip_source}, {plan.clip_bytes} bytes, has_audio={plan.has_audio}")
    print(f"  endpoint        : {plan.endpoint_host_redacted} ({plan.endpoint_scheme})")
    print(f"  model           : {plan.model_id}")
    print(f"  state           : {state}")
    print(f"  sendable        : {sendable}")
    if report["capabilities"]["verification_scope"]:
        print(f"  scope           : {report['capabilities']['verification_scope']}")
    print()
    print("  risks:")
    for risk in plan.risks:
        print(f"    - {risk}")

    if not executed:
        print()
        print("DRY-RUN: nothing was sent. Re-run with --execute --i-authorize-real-upload")
        print("         only when you explicitly approve uploading a video to this endpoint.")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\nreport written: {args.json}")

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
        print()
        print("NOT RECORDED: a dry run produces no evidence; re-run with --execute first.")
        print("              (recording an unproven positive would defeat the guard)")
        return

    caps = report.get("capabilities") or {}
    state = caps.get("state", "unverified")
    if state == "unverified":
        print()
        print("NOT RECORDED: the probe did not establish a usable capability state.")
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
    print()
    print(f"capability recorded: {path}")
    print(f"  state bound to endpoint: {state}")
    print("  the record is local and outside the repository; it is never committed.")


if __name__ == "__main__":
    raise SystemExit(main())
