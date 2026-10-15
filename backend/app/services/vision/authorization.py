"""TSB-312 — the single outbound authorization boundary for vision models.

Every path that could send frames to a third party must pass through here first.
Before this module existed the rules lived in two places and disagreed:

* the new ``vision_analyze`` / ``motion_review`` jobs required
  ``upload_acknowledged`` **and** ``TRAVELSB_MODEL_MODE=on``;
* the legacy ``POST /story/analyze`` route required **neither** — it only checked
  that a base URL and key were configured, so configuring a key silently
  authorised uploads from the old endpoint.

C5 says an outbound request needs a global switch **and** a per-call consent, so
all three conditions are now evaluated here, for every caller:

1. ``TRAVELSB_MODEL_MODE=on`` — the operator's global switch (default ``off``);
2. ``upload_acknowledged=True`` for *this* call — the user's consent for this
   upload, never a process-wide boolean that could leak across requests;
3. a resolvable, non-empty project scope.

The check must run **before** any image is decoded, rendered to a temp file or
sent: a denied call has to leave zero bytes behind as well as zero requests.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ...config import get_settings
from .errors import MODEL_NOT_CONFIGURED, MODEL_UPLOAD_DENIED, ModelError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AuthorizedModelContext:
    """Proof that one specific call is allowed to talk to the model.

    This object is the *token* of authorization: the provider layer accepts it
    instead of re-deriving permission from global state, so a caller cannot
    bypass the business entry point by invoking the client directly. It is
    per-call by construction — there is no process-level "we are authorised now"
    flag anywhere in the vision package.
    """

    project_id: str
    purpose: str
    acknowledged_at: str = ""
    #: Free-form, non-sensitive audit detail (never a key, URL or image).
    detail: dict = field(default_factory=dict)

    def for_log(self) -> dict:
        return {
            "project_id": self.project_id,
            "purpose": self.purpose,
            "acknowledged_at": self.acknowledged_at,
            **self.detail,
        }


def authorization_denial_reason(
    *,
    upload_acknowledged: bool,
    project_id: str,
) -> str | None:
    """Return a stable error string when the call may not proceed, else None.

    Split out from :func:`authorize_upload` so callers that want to *explain* a
    refusal (the API layer, the UI) can ask the same question without raising.
    """
    settings = get_settings()

    if settings.model_mode != "on":
        return (
            f"{MODEL_UPLOAD_DENIED}: 全局开关未开启（TRAVELSB_MODEL_MODE=off）。"
            "出于隐私默认不上传任何画面，请显式设为 on。"
        )
    if not upload_acknowledged:
        return (
            f"{MODEL_UPLOAD_DENIED}: 本次调用未获得上传同意"
            "（需要 upload_acknowledged=true）。"
        )
    if not project_id:
        return f"{MODEL_UPLOAD_DENIED}: 缺少有效的项目范围，无法确认上传对象。"
    if not (settings.model_base_url and settings.model_api_key):
        return f"{MODEL_NOT_CONFIGURED}: 视觉模型未配置（缺少 base URL 或 API Key）。"
    return None


def authorize_upload(
    *,
    project_id: str,
    upload_acknowledged: bool,
    purpose: str,
) -> AuthorizedModelContext:
    """Raise :class:`ModelError` unless this exact call may send images.

    Callers must invoke this **before** rendering or encoding any image.
    """
    reason = authorization_denial_reason(
        upload_acknowledged=upload_acknowledged, project_id=project_id
    )
    if reason is not None:
        code = reason.split(":", 1)[0]
        logger.info(
            "vision upload denied (%s) project=%s purpose=%s",
            code,
            project_id or "<none>",
            purpose,
        )
        raise ModelError(code, reason)

    from datetime import datetime, timezone

    return AuthorizedModelContext(
        project_id=project_id,
        purpose=purpose,
        acknowledged_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )


def require_context(context: AuthorizedModelContext | None) -> AuthorizedModelContext:
    """Guard for the provider layer.

    Any code path that actually performs an HTTP call must present the context
    produced by :func:`authorize_upload`. Passing ``None`` is a programming
    error and is refused rather than silently allowed, so a future refactor
    cannot reintroduce an unguarded outbound path.
    """
    if context is None:
        raise ModelError(
            MODEL_UPLOAD_DENIED,
            f"{MODEL_UPLOAD_DENIED}: 缺少本次调用的上传授权上下文"
            "（必须通过 authorize_upload() 获得）。",
        )
    return context
