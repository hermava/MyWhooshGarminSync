import json
import sys

import httpx
import pytest

from mywhoosh_garmin_sync import garmin
from mywhoosh_garmin_sync import main as cli
from fit_factory import make_fit


class Backend:
    def __init__(self, *, mfa=False, fail=False):
        self.mfa = mfa
        self.fail = fail
        self.closed = False

    def login(self, email, password, prompt_mfa):
        assert email == "example@example.invalid"
        assert password == "synthetic-password"
        if self.mfa:
            assert prompt_mfa() == "123456"
        if self.fail:
            raise RuntimeError("synthetic-private-error")

    def headers(self):
        return {"Authorization": "Bearer synthetic-token"}

    def close(self):
        self.closed = True


def mock_reader(monkeypatch, *, mfa=False, fail=False):
    backend = Backend(mfa=mfa, fail=fail)
    real_reader = garmin.GarminReader
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=[{"activityId": 123, "activityName": "Synthetic Ride",
                                        "startTimeGMT": "2024-01-01 00:00:00", "duration": 60, "elapsedDuration": 60}])

    monkeypatch.setattr(garmin, "GarminReader", lambda: real_reader(backend=backend, transport=httpx.MockTransport(handle)))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "example@example.invalid")
    secrets = iter(["synthetic-password", "123456"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: next(secrets))

    def forbidden_store(*args):
        pytest.fail("Garmin commands must not access the MyWhoosh token store")

    monkeypatch.setattr(cli, "TokenStore", forbidden_store)
    return backend, calls


@pytest.mark.parametrize("mfa", [False, True])
def test_garmin_activity_cli_only_outputs_activity_summary(monkeypatch, capsys, mfa):
    backend, calls = mock_reader(monkeypatch, mfa=mfa)
    assert cli.main(["garmin-activities", "--interactive", "--json"]) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["activities"][0]["id"] == "123"
    assert backend.closed
    assert len(calls) == 1
    for secret in ("synthetic-password", "synthetic-token", "123456", "example@example.invalid"):
        assert secret not in output.out + output.err


def test_garmin_match_cli_uses_only_selected_local_file(tmp_path, monkeypatch, capsys):
    backend, calls = mock_reader(monkeypatch)
    source = tmp_path / "synthetic.fit"
    data = make_fit()
    source.write_bytes(data)
    assert cli.main(["garmin-match", str(source), "--interactive", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "candidate_found"
    assert result["candidates"][0]["id"] == "123"
    assert result["window_limit"] == 100
    assert source.read_bytes() == data
    assert len(calls) == 1
    assert backend.closed


@pytest.mark.parametrize("command", ["garmin-match", "garmin-upload"])
def test_invalid_fit_rejected_before_auth(tmp_path, monkeypatch, capsys, command):
    source = tmp_path / "invalid.fit"
    source.write_bytes(b"not-fit")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def forbidden(*args):
        pytest.fail("Invalid local FIT must be rejected before auth")

    monkeypatch.setattr(garmin, "GarminReader", forbidden)
    monkeypatch.setattr("builtins.input", forbidden)
    assert cli.main([command, str(source), "--interactive"]) == 1
    assert "bestandene FIT-Prüfung" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["garmin-match", "garmin-upload"])
def test_fit_missing_comparable_duration_rejected_before_auth(tmp_path, monkeypatch, capsys, command):
    source = tmp_path / "no-duration.fit"
    source.write_bytes(make_fit(elapsed=0xFFFFFFFF, timer=0xFFFFFFFF))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def forbidden(*args):
        pytest.fail("Missing FIT comparison metadata must be rejected before auth")

    monkeypatch.setattr(garmin, "GarminReader", forbidden)
    monkeypatch.setattr("builtins.input", forbidden)
    assert cli.main([command, str(source), "--interactive"]) == 1
    assert "vergleichbare Dauer fehlt" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["garmin-activities", "garmin-match"])
def test_garmin_commands_require_terminal(monkeypatch, capsys, command):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    args = [command, "--interactive"]
    if command == "garmin-match":
        args.append("unused.fit")
    assert cli.main(args) == 1
    assert "interaktives Terminal" in capsys.readouterr().err


def test_garmin_requires_explicit_interactive_mode():
    with pytest.raises(SystemExit) as error:
        cli.main(["garmin-activities"])
    assert error.value.code == 2


def test_backend_error_does_not_disclose_vendor_details(monkeypatch, capsys):
    backend, calls = mock_reader(monkeypatch, fail=True)
    assert cli.main(["garmin-activities", "--interactive"]) == 1
    assert "synthetic-private-error" not in capsys.readouterr().err
    assert backend.closed
    assert not calls


def test_invalid_mfa_code_is_rejected_without_echo(monkeypatch, capsys):
    backend, calls = mock_reader(monkeypatch, mfa=True)
    secrets = iter(["synthetic-password", "private-invalid-code"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: next(secrets))
    assert cli.main(["garmin-activities", "--interactive"]) == 1
    output = capsys.readouterr()
    assert "private-invalid-code" not in output.out + output.err
    assert "sechs bis acht" in output.err
    assert backend.closed
    assert not calls
