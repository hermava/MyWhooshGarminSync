import json
import os
import sys

import httpx
import pytest

from mywhoosh_garmin_sync import main as cli, windows_credentials
from mywhoosh_garmin_sync.mywhoosh import MyWhooshClient
from mywhoosh_garmin_sync.sessions import SavedSession
from test_accounts import Store


def prompt_login(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "example@example.invalid")
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "synthetic-password")


def test_explicit_memory_only_account_login_does_not_access_windows_storage(monkeypatch, capsys):
    prompt_login(monkeypatch)

    def forbidden(*args, **kwargs):
        pytest.fail("Memory-only sign-in must not access persistent sessions")

    monkeypatch.setattr(windows_credentials, "WindowsSessionStore", forbidden)
    responses = iter([{"Success": True, "AccessToken": "synthetic-access"},
                      {"data": {"results": [], "totalPages": 0}}])
    monkeypatch.setattr(cli, "MyWhooshClient", lambda: MyWhooshClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=next(responses)))))
    assert cli.main(["account-login", "mywhoosh", "--json"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["activities"] == []
    assert "synthetic-access" not in output.out + output.err
    assert "synthetic-password" not in output.out + output.err


def test_cli_can_login_restart_list_and_sign_out_through_injected_protected_store(monkeypatch, capsys):
    prompt_login(monkeypatch)
    store = Store()
    monkeypatch.setattr(windows_credentials, "WindowsSessionStore", lambda: store)
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"Success": True, "AccessToken": "synthetic-access"})
        assert request.headers["Authorization"] == "Bearer synthetic-access"
        return httpx.Response(200, json={"data": {"results": [], "totalPages": 0}})

    monkeypatch.setattr(cli, "MyWhooshClient", lambda: MyWhooshClient(transport=httpx.MockTransport(handle)))
    assert cli.main(["account-login", "mywhoosh", "--remember", "--json"]) == 0
    assert cli.main(["account-activities", "mywhoosh", "--json"]) == 0
    assert len(calls) == 3
    assert cli.main(["account-logout", "mywhoosh", "--json"]) == 0
    assert cli.main(["account-activities", "mywhoosh", "--json"]) == 1
    assert "mywhoosh" not in store.data
    output = capsys.readouterr()
    assert "synthetic-access" not in output.out + output.err
    assert "synthetic-password" not in output.out + output.err


@pytest.mark.skipif(os.name == "nt", reason="Non-Windows fail-closed behavior")
def test_remember_on_linux_fails_before_credentials_or_network(monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def forbidden(*args, **kwargs):
        pytest.fail("Unavailable secure storage must fail before credential input")

    monkeypatch.setattr("builtins.input", forbidden)
    assert cli.main(["account-login", "garmin", "--remember"]) == 1
    assert "Windows Credential Manager" in capsys.readouterr().err


def test_missing_saved_session_never_prompts_or_logs_in(monkeypatch, capsys):
    monkeypatch.setattr(windows_credentials, "WindowsSessionStore", Store)

    def forbidden(*args, **kwargs):
        pytest.fail("Saved-session reads must ask the frontend to sign in, not prompt automatically")

    monkeypatch.setattr("builtins.input", forbidden)
    assert cli.main(["account-activities", "garmin"]) == 1
    assert "Please sign in to Garmin" in capsys.readouterr().err


def test_logout_one_provider_does_not_delete_another(monkeypatch, capsys):
    store = Store()
    store.data = {provider: SavedSession(provider, "synthetic-token") for provider in ("mywhoosh", "garmin")}
    monkeypatch.setattr(windows_credentials, "WindowsSessionStore", lambda: store)
    assert cli.main(["account-logout", "garmin", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"provider": "garmin", "signed_out_locally": True}
    assert set(store.data) == {"mywhoosh"}
