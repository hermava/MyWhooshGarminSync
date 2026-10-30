"""Exercise the pinned vendor auth integration with all HTTP calls mocked."""

import json
import base64
import time
from types import SimpleNamespace

import pytest

from mywhoosh_garmin_sync import garmin
from mywhoosh_garmin_sync.errors import AuthError, NetworkError, RateLimitError, SessionError
from mywhoosh_garmin_sync.sessions import SavedSession


def response(data=None, status=200):
    content = json.dumps(data or {}).encode()
    return SimpleNamespace(status_code=status, content=content, ok=status < 400, json=lambda: data or {})


@pytest.fixture
def vendor(monkeypatch):
    pytest.importorskip("garminconnect")
    from curl_cffi import requests as curl_requests
    from garminconnect.client import sso, Client
    monkeypatch.setattr(sso.time, "sleep", lambda _: None)

    def forbidden(*args, **kwargs):
        pytest.fail("Manual auth must not load, save or serialize any tokens")

    for name in ("load", "loads", "dump", "dumps"):
        monkeypatch.setattr(Client, name, forbidden)
    monkeypatch.setenv("GARMINTOKENS", "/must-not-be-opened/session.json")
    return curl_requests


@pytest.mark.parametrize("mfa", [False, True])
def test_real_library_login_and_mfa_are_memory_only(vendor, monkeypatch, mfa, caplog):
    calls = []
    prompts = []

    def handle(session, method, url, **kwargs):
        calls.append((method, url))
        assert kwargs["verify"] is True
        assert kwargs["allow_redirects"] is False
        assert kwargs["timeout"] <= 15
        assert session.trust_env is False
        if method == "GET":
            return response()
        if url.endswith("/mobile/api/login"):
            assert kwargs["json"]["password"] == "synthetic-password"
            if mfa:
                return response({"responseStatus": {"type": "MFA_REQUIRED"}, "customerMfaInfo": {"mfaLastMethodUsed": "email"}})
            return response({"responseStatus": {"type": "SUCCESSFUL"}, "serviceTicketId": "synthetic-ticket"})
        if url.endswith("/mfa/verifyCode"):
            assert kwargs["json"]["mfaVerificationCode"] == "123456"
            return response({"responseStatus": {"type": "SUCCESSFUL"}, "serviceTicketId": "synthetic-ticket"})
        assert url == "https://diauth.garmin.com/di-oauth2-service/oauth/token"
        return response({"access_token": "synthetic-access-token", "refresh_token": "synthetic-refresh-token"})

    monkeypatch.setattr(vendor.Session, "request", handle)
    backend = garmin._create_backend()
    try:
        backend.login("example@example.invalid", "synthetic-password", lambda: prompts.append(True) or "123456")
        assert backend.headers()["Authorization"] == "Bearer synthetic-access-token"
        assert backend._tokenstore_path is None
        assert len(calls) == (4 if mfa else 3)
        assert len(prompts) == int(mfa)
        assert not any("socialProfile" in url for _, url in calls)
    finally:
        backend.close()
    assert backend.di_token is backend.di_refresh_token is None
    assert "synthetic-password" not in caplog.text
    assert "synthetic-ticket" not in caplog.text


@pytest.mark.parametrize("failure,expected", [("429", RateLimitError), ("network", NetworkError),
                                             ("wrong-password", AuthError), ("redirect", AuthError)])
def test_auth_error_does_not_retry_credentials(vendor, monkeypatch, failure, expected):
    calls = []

    def handle(session, method, url, **kwargs):
        calls.append((method, url))
        if failure == "429":
            return response(status=429)
        if failure == "redirect":
            return response(status=302)
        if failure == "network":
            raise RuntimeError("synthetic-private-password")
        if method == "GET":
            return response()
        return response({"responseStatus": {"type": "INVALID_USERNAME_PASSWORD"}}, status=401)

    monkeypatch.setattr(vendor.Session, "request", handle)
    backend = garmin._create_backend()
    try:
        with pytest.raises(expected) as error:
            backend.login("example@example.invalid", "synthetic-password", lambda: "123456")
        assert "synthetic-private-password" not in str(error.value)
        assert len(calls) == (2 if failure == "wrong-password" else 1)
    finally:
        backend.close()


def test_rate_limit_during_token_exchange_does_not_trigger_cookie_fallback(vendor, monkeypatch):
    calls = []

    def handle(session, method, url, **kwargs):
        calls.append((method, url))
        if method == "GET":
            return response()
        if "diauth" in url:
            return response(status=429)
        return response({"responseStatus": {"type": "SUCCESSFUL"}, "serviceTicketId": "synthetic-ticket"})

    monkeypatch.setattr(vendor.Session, "request", handle)
    backend = garmin._create_backend()
    try:
        with pytest.raises(RateLimitError):
            backend.login("example@example.invalid", "synthetic-password", lambda: "123456")
    finally:
        backend.close()
    assert len(calls) == 3


@pytest.mark.parametrize("error_type", [EOFError, KeyboardInterrupt, SessionError])
def test_mfa_interruption_survives_vendor_exception_handling(vendor, monkeypatch, error_type):
    calls = []

    def handle(session, method, url, **kwargs):
        calls.append(url)
        return response() if method == "GET" else response({"responseStatus": {"type": "MFA_REQUIRED"}})

    def abort():
        raise error_type("synthetic-safe-error")

    monkeypatch.setattr(vendor.Session, "request", handle)
    backend = garmin._create_backend()
    try:
        with pytest.raises(error_type):
            backend.login("example@example.invalid", "synthetic-password", abort)
    finally:
        backend.close()
    assert len(calls) == 2


def test_unsupported_library_version_fails_before_auth(monkeypatch):
    monkeypatch.setattr(garmin, "version", lambda name: "unexpected-version")
    with pytest.raises(SessionError, match="Abhängigkeiten"):
        garmin._create_backend()


def synthetic_jwt(expires):
    def encode(raw):
        return base64.urlsafe_b64encode(json.dumps(raw).encode()).decode().rstrip("=")
    return f"{encode({'alg': 'RS256'})}.{encode({'exp': expires, 'client_id': 'synthetic-client'})}.synthetic-signature"


def test_restore_valid_tokens_never_uses_vendor_file_storage_or_network(vendor, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Restoring a valid in-memory snapshot must not contact the auth server")

    monkeypatch.setattr(vendor.Session, "request", forbidden)
    backend = garmin._create_backend()
    snapshot = SavedSession("garmin", synthetic_jwt(time.time() + 3600),
                            refresh_token="synthetic-refresh", client_id="synthetic-client")
    try:
        assert backend.restore_session(snapshot) is False
        assert backend.export_session() == snapshot
        assert backend.headers()["Authorization"] == "Bearer " + snapshot.access_token
        assert backend._tokenstore_path is None
    finally:
        backend.close()


def test_expired_snapshot_refreshes_once_with_new_budget_and_rotated_tokens(vendor, monkeypatch, caplog):
    calls = []
    new_token = synthetic_jwt(time.time() + 3600)

    def handle(session, method, url, **kwargs):
        calls.append(url)
        assert method == "POST"
        assert url == "https://diauth.garmin.com/di-oauth2-service/oauth/token"
        assert kwargs["data"] == {"grant_type": "refresh_token", "client_id": "synthetic-client",
                                  "refresh_token": "synthetic-old-refresh"}
        assert kwargs["verify"] is True
        assert kwargs["allow_redirects"] is False
        assert kwargs["timeout"] <= 15
        return response({"access_token": new_token, "refresh_token": "synthetic-rotated"})

    monkeypatch.setattr(vendor.Session, "request", handle)
    backend = garmin._create_backend()
    try:
        snapshot = SavedSession("garmin", synthetic_jwt(time.time() - 60),
                                refresh_token="synthetic-old-refresh", client_id="synthetic-client")
        assert backend.restore_session(snapshot) is True
        assert backend.export_session().refresh_token == "synthetic-rotated"
        assert backend.headers()["Authorization"] == "Bearer " + new_token
        assert len(calls) == 1
    finally:
        backend.close()
    assert "synthetic-old-refresh" not in caplog.text
    assert "synthetic-rotated" not in caplog.text


@pytest.mark.parametrize("status,data,expected,code", [
    (400, {"error": "invalid_grant"}, AuthError, "session_expired"),
    (401, {}, AuthError, "session_expired"),
    (403, {}, AuthError, "http_403"),
    (429, {}, RateLimitError, None),
    (500, {}, NetworkError, None),
    (200, {"access_token": "contains spaces"}, garmin.ProtocolError, None),
    (200, {"access_token": "synthetic-new", "refresh_token": None}, garmin.ProtocolError, None),
])
def test_refresh_failure_is_bounded_and_does_not_reauthenticate(vendor, monkeypatch, status, data, expected, code):
    calls = []

    def handle(session, method, url, **kwargs):
        calls.append(url)
        return response(data, status=status)

    monkeypatch.setattr(vendor.Session, "request", handle)
    backend = garmin._create_backend()
    try:
        backend.restore_session(SavedSession("garmin", "synthetic-access", refresh_token="synthetic-refresh",
                                             client_id="synthetic-client"))
        with pytest.raises(expected) as error:
            backend.refresh_session()
        assert error.value.code == code
        assert len(calls) == 1
        assert "synthetic-refresh" not in str(error.value)
    finally:
        backend.close()


def test_refresh_without_refresh_token_requires_sign_in_without_network(vendor):
    backend = garmin._create_backend()
    try:
        backend.restore_session(SavedSession("garmin", "synthetic-access"))
        with pytest.raises(AuthError) as error:
            backend.refresh_session()
        assert error.value.code == "session_expired"
    finally:
        backend.close()


def test_account_cli_login_restart_and_401_renewal_use_real_pinned_adapter(vendor, monkeypatch, capsys):
    import sys
    import httpx
    from mywhoosh_garmin_sync import main as cli, windows_credentials
    from test_accounts import Store

    store = Store()
    monkeypatch.setattr(windows_credentials, "WindowsSessionStore", lambda: store)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "example@example.invalid")
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "synthetic-password")
    auth_calls = []
    reads = []
    access = synthetic_jwt(time.time() + 7200)

    def auth(session, method, url, **kwargs):
        auth_calls.append(url)
        if method == "GET":
            return response()
        if url.endswith("/mobile/api/login"):
            return response({"responseStatus": {"type": "SUCCESSFUL"}, "serviceTicketId": "synthetic-ticket"})
        refresh = kwargs["data"].get("grant_type") == "refresh_token"
        if refresh:
            assert kwargs["data"]["refresh_token"] == "synthetic-original-refresh"
        return response({"access_token": access,
                         "refresh_token": "synthetic-rotated" if refresh else "synthetic-original-refresh"})

    def activities(request):
        reads.append(request)
        assert request.headers["Authorization"] == "Bearer " + access
        return httpx.Response(401 if len(reads) == 2 else 200, json=[])

    monkeypatch.setattr(vendor.Session, "request", auth)
    real_reader = garmin.GarminReader
    monkeypatch.setattr(garmin, "GarminReader", lambda: real_reader(transport=httpx.MockTransport(activities)))
    assert cli.main(["account-login", "garmin", "--remember", "--json"]) == 0
    assert store.data["garmin"].refresh_token == "synthetic-original-refresh"
    assert cli.main(["account-activities", "garmin", "--json"]) == 0
    assert store.data["garmin"].refresh_token == "synthetic-rotated"
    assert len(reads) == 3
    assert len(auth_calls) == 4  # Login GET, login POST, token exchange, one refresh.
    assert cli.main(["account-logout", "garmin", "--json"]) == 0
    assert "garmin" not in store.data
    output = capsys.readouterr()
    for secret in (access, "synthetic-password", "synthetic-original-refresh", "synthetic-rotated"):
        assert secret not in output.out + output.err
