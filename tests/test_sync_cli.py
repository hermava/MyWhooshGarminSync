import json
import re
import sys

import pytest

from mywhoosh_garmin_sync import accounts as account_module, main as cli, sync
from test_sync import Accounts, NOW


def setup_cli(monkeypatch, capsys, *, consent=True):
    fake = Accounts(count=2)
    fake.sign_in_mywhoosh = lambda email, password: None
    fake.sign_in_garmin = lambda email, password, prompt: None
    monkeypatch.setattr(account_module, "AccountSessions", lambda: fake)
    real_engine = sync.SyncEngine
    monkeypatch.setattr(sync, "SyncEngine", lambda *args, **kwargs: real_engine(*args, **kwargs, clock=lambda: NOW))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "synthetic-password")
    calls = []
    output = []

    def enter(prompt):
        calls.append(True)
        if len(calls) <= 2:
            return "example@example.invalid"
        captured = capsys.readouterr()
        output.append(captured.out + captured.err)
        match = re.search(r"Type (SYNC [a-f0-9]{8}):", captured.err)
        assert match
        return match[1] if consent else "cancel"

    monkeypatch.setattr("builtins.input", enter)
    return fake, output


def test_sync_preview_only_has_no_post_and_no_secret_output(tmp_path, monkeypatch, capsys):
    fake, output = setup_cli(monkeypatch, capsys)
    assert cli.main(["sync", "--interactive", "--preview-only", "--json", "--data-dir", str(tmp_path / "data")]) == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert len(data["items"]) == 2
    assert not fake.posts
    assert "unit_id" not in captured.out
    assert "synthetic-password" not in captured.out + captured.err


@pytest.mark.parametrize("consent", [True, False])
def test_cli_requires_frozen_plan_consent_and_defaults_to_newest_only(tmp_path, monkeypatch, capsys, consent):
    fake, output = setup_cli(monkeypatch, capsys, consent=consent)
    code = cli.main(["sync", "--interactive", "--data-dir", str(tmp_path / "data")])
    assert code == (0 if consent else 130)
    assert len(fake.posts) == int(consent)
    if consent:
        assert fake.remote[0].start_time_utc == fake.rides[1].start_time.isoformat()
    captured = capsys.readouterr()
    assert "synthetic-password" not in "".join(output) + captured.out + captured.err


def test_sync_requires_explicit_interactive_mode():
    with pytest.raises(SystemExit) as error:
        cli.main(["sync"])
    assert error.value.code == 2


def test_invalid_scope_is_rejected_before_credentials(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("Invalid scope must not prompt"))
    assert cli.main(["sync", "--interactive", "--days", "0", "--data-dir", str(tmp_path / "absent")]) == 1
    assert not (tmp_path / "absent").exists()
