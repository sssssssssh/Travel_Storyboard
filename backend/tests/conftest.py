"""Shared pytest fixtures.

Every test runs against a throwaway data directory + SQLite file so the real
``backend/data`` workspace is never touched. Settings are cached by
``get_settings``, so the env vars are set before importing the app.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# The data directory must be redirected BEFORE anything imports
# `app.database`/`app.config`, because `get_settings()` is cached at import time.
# Doing this at module scope (not in a fixture) is what keeps tests off the real
# `backend/data` workspace.
_TEST_DATA_DIR = Path(tempfile.mkdtemp(prefix="tsb-tests-"))
os.environ["TRAVELSB_DATA_DIR"] = str(_TEST_DATA_DIR)
os.environ["TRAVELSB_BIND_HOST"] = "127.0.0.1"
os.environ["TRAVELSB_BIND_PORT"] = "8737"
os.environ.pop("TRAVELSB_MODEL_BASE_URL", None)
os.environ.pop("TRAVELSB_MODEL_API_KEY", None)


_SKIPPED_REPORTS = []

#: Skip reasons that mean "this platform genuinely cannot run the test", as
#: opposed to "a required tool was missing". The media CI job sets
#: TSB_REQUIRE_FFMPEG=1 to turn *silent* skips into failures; a test that is
#: Windows-only (or OTIO-only) is legitimately skipped on other platforms and
#: must not fail the run.
#:
#: Matched as substrings against the skip's longrepr. Keep this list to reasons
#: that describe a *platform fact*: anything else skipping under
#: TSB_REQUIRE_FFMPEG=1 is a real problem and should fail the job.
#:
#: Note this list must cover every reason the platform-marked suites use.
#: `test_deep_media_http.py` and `test_deep_scan_api.py` say "MAX_PATH only
#: constrains Windows" while the others say "long-path behaviour is
#: Windows-specific"; when those two files were added to the mandatory-media
#: job their Linux skips were counted as unexpected and failed the run.
_EXPECTED_SKIP_REASONS = (
    "long-path behaviour is Windows-specific",
    "MAX_PATH only constrains Windows",
    "requires Windows",
    "link creation is a platform fact",
)


def _is_expected_skip(report) -> bool:
    """True when the skip is a platform fact rather than a missing dependency."""
    longrepr = getattr(report, "longrepr", None)
    if longrepr is None:
        return False
    text = str(longrepr)
    return any(reason in text for reason in _EXPECTED_SKIP_REASONS)


def pytest_runtest_logreport(report):
    if report.skipped and not _is_expected_skip(report):
        _SKIPPED_REPORTS.append(report.nodeid)


def pytest_sessionfinish(session, exitstatus) -> None:  # noqa: ARG001
    """Drop the throwaway data directory once the run is over."""
    shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)
    if os.environ.get("TSB_REQUIRE_FFMPEG") == "1" and _SKIPPED_REPORTS:
        # A missing FFmpeg (or a silently skipped integration test) is a real
        # problem in the mandatory-media job; a platform-limited test is not.
        print(
            "TSB_REQUIRE_FFMPEG=1 but these tests skipped unexpectedly: "
            + ", ".join(_SKIPPED_REPORTS)
        )
        session.exitstatus = 1


@pytest.fixture(scope="session", autouse=True)
def _isolated_data_dir() -> Path:
    """Expose the session data directory (env vars are set at import time)."""
    assert os.environ["TRAVELSB_DATA_DIR"] == str(_TEST_DATA_DIR)
    return _TEST_DATA_DIR


@pytest.fixture(scope="session", autouse=True)
def _app_ready(_isolated_data_dir: Path) -> None:
    """Create the schema and import the app once, so TASK_REGISTRY is populated."""
    from app.database import init_db
    import app.main  # noqa: F401 - importing registers every task

    init_db()


@pytest.fixture()
def client(_app_ready) :
    """A TestClient whose requests carry the local access token.

    ``base_url`` is loopback so the Host/origin guard (audit P0-01) accepts the
    request — the default ``testserver`` host is intentionally rejected.
    """
    from fastapi.testclient import TestClient

    from app.main import app
    from app.security import get_token

    token = get_token()
    with TestClient(
        app,
        base_url="http://127.0.0.1:8737",
        headers={"X-Access-Token": token},
    ) as c:
        c.app_token = token  # type: ignore[attr-defined]
        yield c


@pytest.fixture()
def db_session(_app_ready):
    from app.database import session_scope

    with session_scope() as s:
        yield s
