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

def pytest_runtest_logreport(report):
    if report.skipped:
        _SKIPPED_REPORTS.append(report.nodeid)


def pytest_sessionfinish(session, exitstatus) -> None:  # noqa: ARG001
    """Drop the throwaway data directory once the run is over."""
    shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)
    if os.environ.get("TSB_REQUIRE_FFMPEG") == "1" and _SKIPPED_REPORTS:
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
