"""Stable error codes and failure classification for vision model calls.

Every failure surfaced to a job, the API or the UI carries one of these codes
(C6: no silent skipping, no ambiguous "empty success"). Classification also
decides retryability, which is what keeps a 401 from being retried forever and
a 429 from being abandoned immediately.
"""

from __future__ import annotations

# --- Configuration / capability -------------------------------------------
MODEL_NOT_CONFIGURED = "MODEL_NOT_CONFIGURED"
MODEL_CAPABILITY_UNSUPPORTED = "MODEL_CAPABILITY_UNSUPPORTED"

# --- Auth / request --------------------------------------------------------
MODEL_AUTH_FAILED = "MODEL_AUTH_FAILED"
MODEL_BAD_REQUEST = "MODEL_BAD_REQUEST"
MODEL_NOT_FOUND = "MODEL_NOT_FOUND"

# --- Transient -------------------------------------------------------------
MODEL_RATE_LIMITED = "MODEL_RATE_LIMITED"
MODEL_SERVER_ERROR = "MODEL_SERVER_ERROR"
MODEL_TIMEOUT = "MODEL_TIMEOUT"
MODEL_NETWORK_ERROR = "MODEL_NETWORK_ERROR"

# --- Payload / result ------------------------------------------------------
MODEL_INVALID_OUTPUT = "MODEL_INVALID_OUTPUT"
MODEL_BUDGET_EXCEEDED = "MODEL_BUDGET_EXCEEDED"
MODEL_UPLOAD_DENIED = "MODEL_UPLOAD_DENIED"
INSUFFICIENT_FRAMES = "INSUFFICIENT_FRAMES"
SOURCE_CHANGED = "SOURCE_CHANGED"
CANCELLED = "CANCELLED"

#: Codes where retrying the *same* request can plausibly succeed. 401/403, an
#: unsupported image, or a malformed 400 will not become correct by repetition,
#: so they are deliberately excluded.
RETRYABLE_CODES = frozenset(
    {
        MODEL_RATE_LIMITED,
        MODEL_SERVER_ERROR,
        MODEL_TIMEOUT,
        MODEL_NETWORK_ERROR,
    }
)


class ModelError(RuntimeError):
    """A classified vision-model failure.

    ``code`` is a stable machine-readable identifier; ``status``/``retry_after``
    are recorded for diagnostics but never contain credentials.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retry_after = retry_after
        self.request_id = request_id

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE_CODES

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "status": self.status,
            "retryable": self.retryable,
            "request_id": self.request_id,
        }


def classify_status(status: int, body: str = "") -> str:
    """Map an HTTP status onto a stable code.

    A wrong model id on some gateways is a 400 rather than a 404, so the body is
    inspected (case-insensitively) to keep the distinction useful.
    """
    if status in (401, 403):
        return MODEL_AUTH_FAILED
    if status == 404:
        return MODEL_NOT_FOUND
    if status == 408:
        return MODEL_TIMEOUT
    if status == 429:
        return MODEL_RATE_LIMITED
    if status >= 500:
        return MODEL_SERVER_ERROR
    if status == 400:
        lowered = body.lower()
        if "does not exist" in lowered or "not found" in lowered:
            return MODEL_NOT_FOUND
        if "image" in lowered and ("support" in lowered or "invalid" in lowered):
            return MODEL_CAPABILITY_UNSUPPORTED
        return MODEL_BAD_REQUEST
    return MODEL_BAD_REQUEST


def redact(text: str, *secrets: str) -> str:
    """Remove credentials from any text bound for logs or error messages."""
    out = text
    for secret in secrets:
        if secret and len(secret) >= 8:
            out = out.replace(secret, "***REDACTED***")
    return out
