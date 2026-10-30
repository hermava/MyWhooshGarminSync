import json
import sys
import warnings

import httpx
import pytest

from mywhoosh_garmin_sync import main as cli
from mywhoosh_garmin_sync.auth import Session, TokenStore
from mywhoosh_garmin_sync.mywhoosh import MyWhooshClient


def test_check_missing_session_is_local_and_readonly(tmp_path, capsys):
    directory = tmp_path / "absent"
    assert cli.main(["--state-dir", str(directory), "check"]) == 1
    assert "nicht verwendet" in capsys.readouterr().out
    assert not directory.exists()


def test_check_valid_session_does_not_expose_token(tmp_path, capsys):
    TokenStore(tmp_path / "private").save(Session("synthetic-token"))
    assert cli.main(["--state-dir", str(tmp_path / "private"), "check"]) == 0
    output = capsys.readouterr().out
    assert "nicht geprüft" in output
    assert "synthetic-token" not in output


@pytest.mark.parametrize("command", [["login"], ["activities", "--interactive"]])
def test_noninteractive_login_rejected_before_network(tmp_path, capsys, monkeypatch, command):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert cli.main(["--state-dir", str(tmp_path / "absent"), *command]) == 1
    assert "interaktives Terminal" in capsys.readouterr().err
    assert not (tmp_path / "absent").exists()


def test_interactive_login_saves_only_session(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "example@example.invalid")
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "synthetic-password")
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"Success": True, "AccessToken": "synthetic-token"})

    monkeypatch.setattr(cli, "MyWhooshClient", lambda: MyWhooshClient(transport=httpx.MockTransport(handle)))
    assert cli.main(["--state-dir", str(tmp_path / "private"), "login"]) == 0
    data = (tmp_path / "private/session.json").read_text()
    assert "synthetic-password" not in data
    assert "example@example.invalid" not in data
    output = capsys.readouterr().out
    assert "synthetic-token" not in output
    assert "synthetic-password" not in output
    assert len(calls) == 1


@pytest.mark.parametrize("command", [["login"], ["activities", "--interactive"]])
def test_password_echo_fallback_is_rejected(tmp_path, monkeypatch, capsys, command):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "example@example.invalid")

    def warn(prompt):
        warnings.warn("cannot hide input", cli.getpass.GetPassWarning)
        pytest.fail("must not fall back to visible input")

    monkeypatch.setattr(cli.getpass, "getpass", warn)
    assert cli.main(["--state-dir", str(tmp_path / "private"), *command]) == 1
    assert "Verdeckte Passworteingabe" in capsys.readouterr().err


@pytest.mark.parametrize("json_output", [False, True])
def test_activity_output_escapes_controls(tmp_path, monkeypatch, capsys, json_output):
    TokenStore(tmp_path / "private").save(Session("synthetic-token"))
    title = "Ride\n\x1b[31m\u202eevil"
    body = {"data": {"results": [{"id": "ride-1", "title": title}], "totalPages": 1}}
    monkeypatch.setattr(cli, "MyWhooshClient", lambda: MyWhooshClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))))
    args = ["--state-dir", str(tmp_path / "private"), "activities"]
    if json_output:
        args.append("--json")
    assert cli.main(args) == 0
    output = capsys.readouterr().out
    assert "\x1b" not in output
    assert "\u202e" not in output
    assert "synthetic-token" not in output
    if json_output:
        assert json.loads(output)["activities"][0]["title"] == title


@pytest.mark.parametrize("limit", ["0", "101", "-1", "NaN"])
def test_invalid_limit_is_argument_error(limit):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["activities", "--limit", limit])
    assert exit_info.value.code == 2


def test_unexpected_exception_does_not_leak(tmp_path, monkeypatch, capsys):
    TokenStore(tmp_path / "private").save(Session("synthetic-token"))

    def broken():
        raise RuntimeError("private-password")

    monkeypatch.setattr(cli, "MyWhooshClient", broken)
    assert cli.main(["--state-dir", str(tmp_path / "private"), "activities"]) == 1
    assert "private-password" not in capsys.readouterr().err


def test_interactive_activities_prompts_each_time_without_storage(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    prompts = []

    def email(prompt):
        prompts.append("email")
        return "example@example.invalid"

    def password(prompt):
        prompts.append("password")
        return "synthetic-password"

    monkeypatch.setattr("builtins.input", email)
    monkeypatch.setattr(cli.getpass, "getpass", password)

    def forbidden_store(*args):
        pytest.fail("Interactive activity reader must never access the token store")

    monkeypatch.setattr(cli, "TokenStore", forbidden_store)
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path.endswith("/login"):
            assert json.loads(request.content)["Password"] == "synthetic-password"
            return httpx.Response(200, json={"Success": True, "AccessToken": "fresh-synthetic-token"})
        assert request.headers["Authorization"] == "Bearer fresh-synthetic-token"
        return httpx.Response(200, json={"data": {"results": [{"id": "ride-1", "title": "Ride"}], "totalPages": 1}})

    monkeypatch.setattr(cli, "MyWhooshClient", lambda: MyWhooshClient(transport=httpx.MockTransport(handle)))
    directory = tmp_path / "absent"
    for _ in range(2):
        assert cli.main(["--state-dir", str(directory), "activities", "--interactive", "--limit", "1", "--json"]) == 0
        output = capsys.readouterr()
        assert json.loads(output.out)["activities"][0]["id"] == "ride-1"
        for secret in ("synthetic-password", "fresh-synthetic-token", "example@example.invalid"):
            assert secret not in output.out + output.err
    assert prompts == ["email", "password", "email", "password"]
    assert len(calls) == 4
    assert not directory.exists()


@pytest.mark.parametrize("failure", ["none", "login", "activities"])
def test_interactive_activities_leaves_existing_session_untouched(tmp_path, monkeypatch, capsys, failure):
    directory = tmp_path / "private"
    store = TokenStore(directory)
    store.save(Session("previous-synthetic-token"))
    before = store.path.read_bytes()
    before_stat = store.path.stat()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "example@example.invalid")
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "synthetic-password")

    def forbidden_store(*args):
        pytest.fail("Existing sessions must not be opened by interactive mode")

    monkeypatch.setattr(cli, "TokenStore", forbidden_store)

    def handle(request):
        is_login = request.url.path.endswith("/login")
        if (is_login and failure == "login") or (not is_login and failure == "activities"):
            return httpx.Response(401, text="synthetic-password fresh-synthetic-token")
        if is_login:
            return httpx.Response(200, json={"Success": True, "AccessToken": "fresh-synthetic-token"})
        assert request.headers["Authorization"] == "Bearer fresh-synthetic-token"
        return httpx.Response(200, json={"data": {"results": [], "totalPages": 0}})

    monkeypatch.setattr(cli, "MyWhooshClient", lambda: MyWhooshClient(transport=httpx.MockTransport(handle)))
    assert cli.main(["--state-dir", str(directory), "activities", "--interactive"]) == (0 if failure == "none" else 1)
    assert store.path.read_bytes() == before
    assert store.path.stat().st_ino == before_stat.st_ino
    assert store.path.stat().st_mtime_ns == before_stat.st_mtime_ns
    assert list(directory.iterdir()) == [store.path]
    output = capsys.readouterr()
    for secret in ("synthetic-password", "fresh-synthetic-token", "previous-synthetic-token"):
        assert secret not in output.out + output.err


@pytest.mark.parametrize("interruption", [EOFError, KeyboardInterrupt])
def test_interactive_activities_cancelled_before_request(tmp_path, monkeypatch, capsys, interruption):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def cancelled(prompt):
        raise interruption

    monkeypatch.setattr("builtins.input", cancelled)
    directory = tmp_path / "absent"
    assert cli.main(["--state-dir", str(directory), "activities", "--interactive"]) == 130
    assert "Abgebrochen" in capsys.readouterr().err
    assert not directory.exists()
