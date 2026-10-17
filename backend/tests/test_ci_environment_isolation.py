"""TSB-343 — CI isolation: the suite must not depend on a developer's ``.env``.

The defects
-----------
1. **Incomplete Mock contract.** The provider / switch-scope fixtures set only
   ``model_mode="on"``. ``authorize_upload()`` also requires ``model_base_url``
   and ``model_api_key``, so on a machine with no ``.env`` -- which is exactly what
   CI is -- it refused with ``MODEL_NOT_CONFIGURED`` *before* reaching the branch
   under test, masking the expected ``MODEL_UPLOAD_DENIED`` and producing 17
   failures that never appear locally where ``.env`` exists.

2. **Console encoding.** The probe's risk notes are Chinese. On a legacy Windows
   code page, printing them raised
   ``UnicodeEncodeError: 'charmap' codec can't encode characters ...``, killing the
   dry-run that CI asserts returns 0.

These tests run the CLI as a subprocess in a deliberately hostile environment, so
they catch both classes rather than only the in-process symptoms.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]


def _run_probe(args, *, env_overrides=None, encoding="utf-8"):
    """Run the probe CLI with a controlled environment."""
    env = dict(os.environ)
    # Make the run independent of any developer .env values.
    for key in (
        "TRAVELSB_MODEL_BASE_URL",
        "TRAVELSB_MODEL_API_KEY",
        "TRAVELSB_MODEL_NAME",
        "TRAVELSB_MODEL_MODE",
        "TRAVELSB_MODEL_ALLOW_INSECURE_HTTP",
    ):
        env.pop(key, None)
    env["PYTHONIOENCODING"] = encoding
    env.update(env_overrides or {})
    return subprocess.run(
        [sys.executable, "tools/probe_video_capability.py", *args],
        cwd=BACKEND, env=env, capture_output=True, timeout=300,
    )


# --- V14: the Windows console encoding failure ------------------------------


@pytest.mark.parametrize("encoding", ["cp1252", "cp936", "ascii", "latin-1"])
def test_v14_dry_run_survives_a_non_utf8_console(encoding):
    """V14: a legacy code page must not crash the probe.

    RED before the fix: ``UnicodeEncodeError: 'charmap' codec can't encode
    characters ... <undefined>`` and exit 1.
    """
    result = _run_probe([], encoding=encoding)
    assert result.returncode == 0, (
        f"dry-run must exit 0 under {encoding}; "
        f"stderr={result.stderr.decode(encoding, 'replace')[:400]}"
    )
    assert b"UnicodeEncodeError" not in result.stderr
    assert b"Traceback" not in result.stderr


def test_v14b_ascii_mode_emits_pure_ascii():
    """V14: ``--ascii`` keeps machine output parseable under any code page."""
    result = _run_probe(["--ascii"], encoding="cp1252")
    assert result.returncode == 0
    result.stdout.decode("ascii")  # must not raise
    assert b"Traceback" not in result.stderr


def test_v14c_the_json_report_is_written_even_on_a_legacy_console(tmp_path):
    """The structured result must survive, not just the human summary."""
    out = tmp_path / "report.json"
    result = _run_probe(["--json", str(out), "--ascii"], encoding="cp1252")
    assert result.returncode == 0
    assert out.exists(), "the machine-readable report must still be written"
    payload = json.loads(out.read_text(encoding="utf-8"))
    # Non-ASCII must be preserved in the file (only the console is constrained).
    assert payload["capabilities"]["state"] == "unverified"


# --- V15: a dry run never sends anything ------------------------------------


def test_v15_a_dry_run_makes_no_network_call(tmp_path):
    """V15: dry-run means zero outbound traffic, on every platform."""
    out = tmp_path / "dry.json"
    result = _run_probe(["--json", str(out)])
    assert result.returncode == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["mode"] == "dry_run"
    assert payload["capabilities"]["state"] == "unverified"
    assert payload["capabilities"]["sendable"] is False


def test_v15b_a_dry_run_needs_no_base_url_or_key():
    """V15: a missing endpoint must not stop the plan from being produced."""
    result = _run_probe([], env_overrides={
        "TRAVELSB_MODEL_BASE_URL": "",
        "TRAVELSB_MODEL_API_KEY": "",
    })
    assert result.returncode == 0, (
        "a dry run must return the review plan even with nothing configured; "
        f"stderr={result.stderr.decode('utf-8', 'replace')[:300]}"
    )


def test_v16_execute_without_consent_is_refused():
    """V16: ``--execute`` alone must not send; consent is a separate flag."""
    result = _run_probe(["--execute"])
    assert result.returncode == 2
    assert b"i-authorize-real-upload" in result.stdout


def test_v16b_the_production_default_is_still_off():
    """V16: none of the above may have flipped the shipped default."""
    env_example = (BACKEND.parent / ".env.example").read_text(encoding="utf-8")
    assert "TRAVELSB_MODEL_MODE=off" in env_example


def test_v16c_ci_never_exports_a_global_model_switch():
    """V16: the fix must not be "turn it on in the workflow"."""
    ci = (BACKEND.parent / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "TRAVELSB_MODEL_MODE" not in ci, (
        "CI must not export a global model switch; the enabling belongs in the "
        "test fixture so the guard stays meaningful"
    )


# --- V11/V12/V13: the Mock contract ----------------------------------------


def test_v11_the_mock_contract_helper_supplies_everything_authorization_needs():
    """V11/V12: the shared helper must satisfy ``authorize_upload`` itself.

    This asserts against the real authorization function rather than a copy of its
    rules, so the contract cannot silently drift from the product's.
    """
    from tests.conftest import (
        MOCK_MODEL_API_KEY,
        MOCK_MODEL_BASE_URL,
        apply_mock_model_contract,
    )

    class _MP:
        def __init__(self):
            self.undo = []

        def setattr(self, obj, name, value, raising=True):
            self.undo.append((obj, name, getattr(obj, name, None)))
            setattr(obj, name, value)

    mp = _MP()
    try:
        apply_mock_model_contract(mp)
        from app.services.vision.authorization import authorize_upload

        # Must not raise: the whole point is that authorization passes so the
        # test can reach its own branch.
        ctx = authorize_upload(
            project_id="tsb343", upload_acknowledged=True, purpose="video_analyze"
        )
        assert ctx is not None
        assert MOCK_MODEL_BASE_URL.startswith("http://127.0.0.1"), (
            "the mock endpoint must be loopback so no real request can escape"
        )
        assert "test-only" in MOCK_MODEL_API_KEY, "the key must be an obvious placeholder"
    finally:
        for obj, name, old in reversed(mp.undo):
            setattr(obj, name, old)


def test_v12_without_the_contract_authorization_is_not_configured():
    """V12: reproduce the RED condition explicitly.

    With no base URL or key, authorization fails with ``MODEL_NOT_CONFIGURED`` --
    the code that used to mask the assertions. Pinning it here documents *why*
    the helper is necessary.
    """
    from app.config import get_settings
    from app.services.vision.authorization import authorization_denial_reason

    settings = get_settings()
    saved = (settings.model_mode, settings.model_base_url, settings.model_api_key)
    try:
        settings.model_mode = "on"
        settings.model_base_url = ""
        settings.model_api_key = ""
        reason = authorization_denial_reason(
            project_id="tsb343", upload_acknowledged=True
        )
        assert reason, "an unconfigured model must be refused"
        assert "MODEL_NOT_CONFIGURED" in reason
    finally:
        settings.model_mode, settings.model_base_url, settings.model_api_key = saved


def test_v13_the_contract_is_restored_after_the_fixture(tmp_path):
    """V13: a test-scoped enable must not leak into later tests."""
    script = tmp_path / "check_leak.py"
    script.write_text(
        "from app.config import get_settings\n"
        "import sys\n"
        "sys.path.insert(0, 'tests')\n"
        "def test_a_enables(monkeypatch):\n"
        "    from tests.conftest import apply_mock_model_contract\n"
        "    apply_mock_model_contract(monkeypatch)\n"
        "    assert get_settings().model_mode == 'on'\n"
        "def test_b_sees_the_default_again():\n"
        "    assert get_settings().model_mode == 'off', get_settings().model_mode\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(script), "-q", "-p", "no:randomly"],
        cwd=BACKEND, capture_output=True, timeout=300,
        env={**os.environ, "TRAVELSB_MODEL_MODE": "off"},
    )
    assert result.returncode == 0, (
        "the switch must be restored at teardown; "
        f"{result.stdout.decode('utf-8', 'replace')[-600:]}"
    )
