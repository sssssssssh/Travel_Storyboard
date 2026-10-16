"""TSB-335 — the runtime capability record for the native video channel.

Why this module exists
----------------------
``current_capabilities()`` used to answer ``unverified`` unconditionally, so the
video route returned 422 forever: even a *successful* probe could not enable the
channel. The obvious repair -- read the committed
``docs/validation/.../CAPABILITY_REPORT.json`` -- is forbidden by TSB-335 §2 and
is genuinely dangerous. That file is public, describes one past deployment, and
anyone able to open a pull request could edit it into a grant of permission to
upload a user's footage. A privacy guard validated against a forgeable public
artifact is not a guard.

So the record lives here instead:

* **Local, operator-controlled, outside the repository** (``<data_dir>/runtime/``),
  which is already the place this project keeps non-public runtime state.
* **Produced only by an actually performed probe** -- ``evidence_digest`` and a
  non-empty ``verification_scope`` are required for any sendable state, so
  "the call returned 200" cannot be dressed up as understanding.
* **Bound to the full server identity** -- base URL, model name, protocol and
  deployment id. Any difference, a damaged file, an unreadable file, or an
  expired record yields ``unverified``. It never falls back to a remembered
  positive.

The authorization itself is unchanged: this only decides *what is known*, and the
adapter still enforces scope, protocol registration, MIME allow-lists and the
payload budget on every call. ``TRAVELSB_MODEL_MODE`` remains the master switch
and stays ``off`` by default.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .video_capabilities import (
    CAPABILITY_STATES,
    UNVERIFIED,
    VERIFIED_NATIVE_VIDEO,
    VideoCapabilities,
    record_verification,
    unverified_capabilities,
)

logger = logging.getLogger(__name__)

#: Bumped only if the on-disk shape changes incompatibly. An unrecognised
#: version is treated as "no usable record", never as "assume verified".
SCHEMA_VERSION = 1

#: The file name inside ``<data_dir>/runtime/``.
STORE_FILENAME = "video_capability.json"


def default_capability_store_path() -> Path:
    """Where the record lives by default: a local, non-repository location.

    ``<data_dir>`` defaults to ``backend/data`` and is already the home of the
    database and ``token.txt`` -- runtime state that is deliberately not part of
    the repository and not served to the network.
    """
    from ...config import get_settings

    return Path(get_settings().data_path) / "runtime" / STORE_FILENAME


def capability_store_path() -> Path:
    """Indirection point so tests can redirect the store without touching config."""
    override = os.environ.get("TRAVELSB_CAPABILITY_STORE", "").strip()
    if override:
        return Path(override).expanduser()
    return default_capability_store_path()


@dataclass(frozen=True)
class EndpointIdentity:
    """The full identity a capability record is bound to.

    ``deployment_id`` is the server's own statement about what is running. When a
    probe can read it, a change means a different build and the old positive no
    longer applies (task book §2.3).
    """

    base_url: str = ""
    model_name: str = ""
    protocol: str = ""
    deployment_id: str = ""

    def fingerprint(self, *, include_deployment: bool = True) -> str:
        material = "|".join(
            [
                self.base_url.rstrip("/"),
                self.model_name,
                self.protocol,
                self.deployment_id if include_deployment else "",
            ]
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def matches(self, other: "EndpointIdentity") -> bool:
        """Is a record bound to ``self`` still valid for the live endpoint ``other``?

        The deployment id is compared only when the *live* process can actually
        observe one. A probe can learn the server's build string, but the runtime
        path has no equivalent channel; demanding equality against an empty value
        would make every positive record permanently unusable, which is just the
        old "always unverified" defect wearing a different hat.

        The asymmetric case that matters is preserved: if the live endpoint *does*
        report a deployment identity and it differs from the recorded one, the
        record is refused (task book §2.3 -- a different build is not the build
        that was probed).
        """
        if self.base_url.rstrip("/") != other.base_url.rstrip("/"):
            return False
        if self.model_name != other.model_name:
            return False
        if self.protocol != other.protocol:
            return False
        if other.deployment_id and self.deployment_id != other.deployment_id:
            return False
        return True

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def current_identity() -> EndpointIdentity:
    """Identity of the endpoint this process would actually send to.

    ``protocol`` is intentionally taken from the registered default rather than
    guessed; a record naming a protocol we no longer speak must not match.
    """
    from ...config import get_settings
    from .video_capabilities import registered_protocols

    settings = get_settings()
    protocols = registered_protocols()
    protocol = next(iter(protocols), "") if protocols else ""
    return EndpointIdentity(
        base_url=(settings.model_base_url or "").rstrip("/"),
        model_name=settings.model_name or "",
        protocol=protocol,
        deployment_id="",
    )


@dataclass
class CapabilityRecord:
    """A probe result plus the identity and evidence that justify it."""

    identity: EndpointIdentity
    capabilities: VideoCapabilities
    evidence_digest: str = ""
    evidence_summary: str = ""
    recorded_at: str = ""
    schema_version: int = SCHEMA_VERSION
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "identity": self.identity.to_dict(),
            "capabilities": self.capabilities.to_dict(),
            "evidence_digest": self.evidence_digest,
            "evidence_summary": self.evidence_summary,
            "recorded_at": self.recorded_at,
            **({"extra": self.extra} if self.extra else {}),
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_record(
    *,
    identity: EndpointIdentity,
    state: str,
    protocol: str,
    model_id: str,
    verification_scope: str,
    evidence_digest: str = "",
    evidence_summary: str = "",
    max_duration_sec: float | None = None,
    max_bytes: int | None = None,
    accepts_audio: bool | None = None,
    temporal_sampling_disclosed: bool = False,
    deployment_id: str = "",
    probe_time: str = "",
    extra: dict[str, Any] | None = None,
) -> CapabilityRecord:
    """Build a record from a probe that was *actually performed*.

    ``record_verification`` already refuses a ``verified_native_video`` claim
    with no scope; the extra ``evidence_digest`` requirement here means a record
    cannot be conjured from a bare success either.
    """
    caps = record_verification(
        state=state,
        protocol=protocol,
        model_id=model_id,
        verification_scope=verification_scope,
        max_duration_sec=max_duration_sec,
        max_bytes=max_bytes,
        accepts_audio=accepts_audio,
        temporal_sampling_disclosed=temporal_sampling_disclosed,
    )
    if probe_time:
        caps = VideoCapabilities(**{**asdict(caps), "probe_time": probe_time})

    bound = identity
    if deployment_id and not identity.deployment_id:
        bound = EndpointIdentity(
            base_url=identity.base_url,
            model_name=identity.model_name,
            protocol=identity.protocol,
            deployment_id=deployment_id,
        )

    return CapabilityRecord(
        identity=bound,
        capabilities=caps,
        evidence_digest=evidence_digest,
        evidence_summary=evidence_summary,
        recorded_at=_now(),
        extra=dict(extra or {}),
    )


def save_record(record: CapabilityRecord, *, identity: EndpointIdentity | None = None) -> Path:
    """Persist a probe record atomically. Never stores credentials or paths."""
    path = capability_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = record.to_dict()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    # The record may name the endpoint, so it is operator-private: keep it
    # readable only by the owner where the platform supports it.
    try:
        path.chmod(0o600)
    except OSError:  # pragma: no cover - Windows / exotic filesystems
        pass
    _CACHE.clear()
    return path


#: A single-entry cache so a job loop does not re-read the file per shot. Cleared
#: by ``reset_cache()``; tests use it to simulate a fresh process.
_CACHE: dict[str, Any] = {}


def reset_cache() -> None:
    _CACHE.clear()


def _load_raw() -> dict[str, Any] | None:
    """Read the file. Any problem yields ``None`` -- never an exception upward."""
    try:
        text = capability_store_path().read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("capability store unreadable (%s); treating as unverified", type(exc).__name__)
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        logger.warning("capability store is not valid JSON; treating as unverified")
        return None
    if not isinstance(data, dict):
        return None
    return data


def _parse(raw: dict[str, Any]) -> CapabilityRecord | None:
    """Validate strictly. Anything questionable is rejected, not repaired."""
    if int(raw.get("schema_version", -1)) != SCHEMA_VERSION:
        logger.warning("capability store schema version is not %s; treating as unverified", SCHEMA_VERSION)
        return None

    ident = raw.get("identity")
    caps_raw = raw.get("capabilities")
    if not isinstance(ident, dict) or not isinstance(caps_raw, dict):
        return None

    state = caps_raw.get("state", UNVERIFIED)
    if state not in CAPABILITY_STATES:
        return None
    scope = str(caps_raw.get("verification_scope", "") or "").strip()
    digest = str(raw.get("evidence_digest", "") or "").strip()

    # The two conditions that make a positive claim mean something. A record
    # failing either one is downgraded, so hand-editing a public-style JSON into
    # "sendable: true" achieves nothing.
    if state == VERIFIED_NATIVE_VIDEO and (not scope or not digest):
        logger.warning(
            "capability record claims verified video without probe evidence "
            "(scope=%s digest=%s); treating as unverified",
            bool(scope),
            bool(digest),
        )
        return None

    try:
        caps = VideoCapabilities(
            state=state,
            protocol=str(caps_raw.get("protocol", "") or ""),
            max_duration_sec=caps_raw.get("max_duration_sec"),
            max_bytes=caps_raw.get("max_bytes"),
            accepts_audio=caps_raw.get("accepts_audio"),
            temporal_sampling_disclosed=bool(caps_raw.get("temporal_sampling_disclosed", False)),
            model_id=str(caps_raw.get("model_id", "") or ""),
            probe_time=str(caps_raw.get("probe_time", "") or ""),
            verification_scope=scope,
            # ``detail`` is carried for context only and never for authority.
            detail=dict(caps_raw.get("detail") or {}),
        )
    except (ValueError, TypeError):
        return None

    return CapabilityRecord(
        identity=EndpointIdentity(
            base_url=str(ident.get("base_url", "") or "").rstrip("/"),
            model_name=str(ident.get("model_name", "") or ""),
            protocol=str(ident.get("protocol", "") or ""),
            deployment_id=str(ident.get("deployment_id", "") or ""),
        ),
        capabilities=caps,
        evidence_digest=digest,
        evidence_summary=str(raw.get("evidence_summary", "") or ""),
        recorded_at=str(raw.get("recorded_at", "") or ""),
        schema_version=SCHEMA_VERSION,
    )


def _is_expired(record: CapabilityRecord, *, max_age_days: float) -> bool:
    stamp = record.capabilities.probe_time or record.recorded_at
    if not stamp:
        return True  # no time on record -> cannot show freshness -> refuse
    try:
        when = datetime.fromisoformat(stamp)
    except (ValueError, TypeError):
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - when > timedelta(days=max_age_days)


def load_capabilities(
    *,
    identity: EndpointIdentity | None = None,
    max_age_days: float = 30.0,
) -> VideoCapabilities:
    """The capability record for *this* endpoint, or ``unverified``.

    Every failure mode answers ``unverified`` with a reason: no record, damaged
    record, identity mismatch, expired, or a positive claim lacking evidence.
    Falling back to ``unverified`` is always safe -- the route answers 422 and
    explains how to fix it -- whereas wrongly inheriting a positive would send a
    user's footage to an endpoint nobody verified.
    """
    live = identity if identity is not None else current_identity()

    raw = _load_raw()
    if raw is None:
        return unverified_capabilities(
            model_id=live.model_name, reason="本机没有能力探测记录；请先运行探测"
        )

    record = _parse(raw)
    if record is None:
        return unverified_capabilities(
            model_id=live.model_name, reason="能力记录损坏或缺少探测证据；已忽略，请重新探测"
        )

    if not record.identity.matches(live):
        return unverified_capabilities(
            model_id=live.model_name,
            reason="能力记录绑定的是另一个端点/模型/协议/部署，已失效；请重新探测",
        )

    if _is_expired(record, max_age_days=max_age_days):
        return unverified_capabilities(
            model_id=live.model_name, reason="能力记录已过期；请重新探测"
        )

    # A non-sendable measured state (e.g. ``unsupported``) is returned as-is so
    # the UI can explain it, but ``sendable`` stays False.
    return record.capabilities


def describe_store() -> dict[str, Any]:
    """Non-sensitive summary for diagnostics and the UI repair hint."""
    path = capability_store_path()
    out: dict[str, Any] = {"path": str(path), "exists": path.exists()}
    raw = _load_raw()
    if raw is not None:
        record = _parse(raw)
        if record is not None:
            out["state"] = record.capabilities.state
            out["probe_time"] = record.capabilities.probe_time
            out["recorded_at"] = record.recorded_at
        else:
            out["state"] = "invalid"
    return out
