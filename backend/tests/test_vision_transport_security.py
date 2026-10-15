"""TSB-317: loopback identity, TLS classification and public diagnostic privacy."""
import json
import logging
import ssl

import httpx
import pytest

from app.config import get_settings
from app.services.vision.errors import ModelError
from app.services.vision.providers.openai_compatible import OpenAICompatibleVisionProvider, validate_endpoint
from test_vision_provider import jpg, mock_server, _configure, _request  # noqa: F401


@pytest.mark.parametrize('host', ['127.evil.invalid', '127.0.0.1.evil.invalid', 'localhost.evil.invalid'])
def test_loopback_looking_host_is_not_a_loopback(host):
    with pytest.raises(ModelError):
        validate_endpoint('http://' + host + '/v1', allow_insecure=False)


@pytest.mark.parametrize('url', ['https:///v1', 'https://user:password@model.invalid/v1',
                               'https://model.invalid/v1?key=secret', 'https://model.invalid:bad/v1'])
def test_malformed_or_credential_bearing_endpoint_is_refused(url):
    with pytest.raises(ModelError):
        validate_endpoint(url, allow_insecure=False)


def test_tls_verification_failure_is_permanent_classified_and_private(jpg, monkeypatch):
    _configure(monkeypatch, 'https://private-gateway.invalid/v1', insecure=False, retries=3)
    monkeypatch.setattr(get_settings(), 'model_mode', 'on')
    calls = []
    def broken(*a, **kw):
        calls.append(kw)
        assert kw.get('verify', True) is not False
        try:
            raise ssl.SSLCertVerificationError('certificate expired')
        except ssl.SSLCertVerificationError as cause:
            raise httpx.ConnectError('certificate failed at https://private-gateway.invalid/v1') from cause
    monkeypatch.setattr(httpx, 'post', broken)
    with pytest.raises(ModelError) as ei:
        OpenAICompatibleVisionProvider().complete(_request(jpg))
    assert ei.value.code == 'MODEL_TLS_FAILED'
    assert not ei.value.retryable and len(calls) == 1
    assert 'private-gateway' not in str(ei.value)


@pytest.mark.parametrize('failure', ['timeout', 'network', 'body'])
def test_untrusted_errors_cannot_echo_endpoint_image_path_or_key(failure, jpg, monkeypatch):
    _configure(monkeypatch, 'https://private-gateway.invalid/v1', insecure=False, retries=0)
    monkeypatch.setattr(get_settings(), 'model_mode', 'on')
    secret = 'Bearer test-key-1234567890 https://private-gateway.invalid/v1 data:image/jpeg;base64,AAABBB /Users/private/source.mp4'
    def broken(*a, **kw):
        if failure == 'timeout': raise httpx.ReadTimeout(secret)
        if failure == 'network': raise httpx.ConnectError(secret)
        return httpx.Response(401, json={'error': secret})
    monkeypatch.setattr(httpx, 'post', broken)
    with pytest.raises(ModelError) as ei:
        OpenAICompatibleVisionProvider().complete(_request(jpg))
    public = json.dumps(ei.value.to_dict())
    for value in ['test-key-1234567890', 'private-gateway', 'AAABBB', '/Users/private', 'Bearer']:
        assert value not in public


def test_mode_off_refuses_provider_even_with_an_old_authorized_context(jpg, monkeypatch):
    _configure(monkeypatch, 'https://private-gateway.invalid/v1', insecure=False)
    monkeypatch.setattr(get_settings(), 'model_mode', 'off')
    calls = []
    monkeypatch.setattr(httpx, 'post', lambda *a, **kw: calls.append(a))
    with pytest.raises(ModelError) as ei:
        OpenAICompatibleVisionProvider().complete(_request(jpg))
    assert ei.value.code == 'MODEL_UPLOAD_DENIED'
    assert calls == []


def test_httpx_logging_does_not_disclose_configured_endpoint(mock_server, jpg, monkeypatch, caplog):
    _configure(monkeypatch, mock_server.url)
    monkeypatch.setattr(get_settings(), 'model_mode', 'on')
    mock_server.ok('{}')
    caplog.set_level(logging.INFO, logger='httpx')
    OpenAICompatibleVisionProvider().complete(_request(jpg))
    assert mock_server.url not in caplog.text


@pytest.mark.parametrize('probe', [False, True])
def test_user_migration_checker_never_uploads_without_explicit_ack(probe, monkeypatch, capsys):
    import sys
    from tools import verify_secure_model as checker
    _configure(monkeypatch, 'https://private-gateway.invalid/v1', insecure=False)
    monkeypatch.setattr(get_settings(), 'model_mode', 'on')
    called = []
    monkeypatch.setattr(OpenAICompatibleVisionProvider, 'complete', lambda *a, **kw: called.append(a))
    monkeypatch.setattr(sys, 'argv', ['verify_secure_model.py'] + (['--probe'] if probe else []))
    assert checker.main() == (1 if probe else 0)
    out = capsys.readouterr().out
    assert 'private-gateway' not in out and 'test-key' not in out
    assert called == []


@pytest.mark.parametrize('reason', ['development', 'tunnel'])
def test_user_checker_does_not_accept_unsafe_production_configuration(reason, monkeypatch):
    from tools.verify_secure_model import check_config
    _configure(monkeypatch, 'http://127.0.0.1:12345/v1' if reason == 'tunnel' else 'https://model.invalid/v1',
               insecure=reason == 'development')
    with pytest.raises(ModelError):
        check_config()
