import json
import sys

import httpx
import pytest

from mywhoosh_garmin_sync import main as cli
from mywhoosh_garmin_sync.mywhoosh import MyWhooshClient
from fit_factory import START, make_fit


def install_mock_download(monkeypatch, *, data, missing_id=False, absent=False, interrupted=False):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "example@example.invalid")
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "synthetic-password")

    def forbidden_store(*args):
        pytest.fail("Interactive download must not access the token store")

    monkeypatch.setattr(cli, "TokenStore", forbidden_store)
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"Success": True, "AccessToken": "synthetic-token"})
        if request.url.path.endswith("/activities"):
            entry = {"id": "synthetic-ride", "startDatetime": "2024-01-01T00:00:00Z"}
            if not missing_id:
                entry["activityFileId"] = "opaque-file-id"
            return httpx.Response(200, json={"data": {"results": [] if absent else [entry], "totalPages": 1}})
        if request.url.path.endswith("/download-activity-file"):
            return httpx.Response(200, json={"data": "https://synthetic.s3.amazonaws.com/ride.fit?secret=synthetic-signature"})
        if interrupted:
            raise httpx.ReadTimeout("synthetic-signature", request=request)
        return httpx.Response(200, content=data)

    monkeypatch.setattr(cli, "MyWhooshClient", lambda: MyWhooshClient(transport=httpx.MockTransport(handle)))
    return calls


@pytest.mark.parametrize("valid", [True, False])
def test_interactive_download_saves_exact_original_and_reports_validity(tmp_path, monkeypatch, capsys, valid):
    data = make_fit() if valid else b"broken synthetic original"
    calls = install_mock_download(monkeypatch, data=data)
    directory = tmp_path / "private"
    assert cli.main(["download", "synthetic-ride", "--interactive", "--output-dir", str(directory), "--json"]) == (0 if valid else 1)
    target = directory / "synthetic-ride.original.fit"
    assert target.read_bytes() == data
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["valid"] == valid
    assert report["size_bytes"] == len(data)
    assert len(calls) == 4
    for secret in ("synthetic-token", "synthetic-password", "synthetic-signature"):
        assert secret not in output.out + output.err


@pytest.mark.parametrize("failure", ["missing-id", "absent", "interrupted"])
def test_download_failure_never_publishes_original(tmp_path, monkeypatch, capsys, failure):
    install_mock_download(monkeypatch, data=make_fit(), missing_id=failure == "missing-id",
                          absent=failure == "absent", interrupted=failure == "interrupted")
    directory = tmp_path / "private"
    assert cli.main(["download", "synthetic-ride", "--interactive", "--output-dir", str(directory)]) == 1
    assert list(directory.iterdir()) == []
    assert "synthetic-signature" not in capsys.readouterr().err


def test_existing_original_rejected_before_login(tmp_path, monkeypatch, capsys):
    calls = install_mock_download(monkeypatch, data=make_fit())
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    target = directory / "synthetic-ride.original.fit"
    target.write_bytes(b"previous-original")
    assert cli.main(["download", "synthetic-ride", "--interactive", "--output-dir", str(directory)]) == 1
    assert "existiert bereits" in capsys.readouterr().err
    assert target.read_bytes() == b"previous-original"
    assert not calls


@pytest.mark.parametrize("valid", [True, False])
def test_validate_is_local_readonly_without_token_access(tmp_path, monkeypatch, capsys, valid):
    source = tmp_path / "source.fit"
    data = make_fit() if valid else b"broken original"
    source.write_bytes(data)
    before = source.stat()

    def forbidden(*args):
        pytest.fail("Local validation must not access auth or HTTP")

    monkeypatch.setattr(cli, "TokenStore", forbidden)
    monkeypatch.setattr(cli, "MyWhooshClient", forbidden)
    assert cli.main(["validate", str(source), "--json"]) == (0 if valid else 1)
    assert json.loads(capsys.readouterr().out)["valid"] == valid
    assert source.read_bytes() == data
    assert source.stat().st_mtime_ns == before.st_mtime_ns


def test_noninteractive_input_rejected_without_creating_output(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    directory = tmp_path / "absent"
    assert cli.main(["download", "synthetic-ride", "--interactive", "--output-dir", str(directory)]) == 1
    assert "interaktives Terminal" in capsys.readouterr().err
    assert not directory.exists()


def test_validation_shows_session_timing_without_modifying_original(tmp_path, capsys):
    source = tmp_path / "synthetic.fit"
    data = make_fit(end=START)
    source.write_bytes(data)
    assert cli.main(["validate", str(source)]) == 0
    output = capsys.readouterr().out
    assert "CRC und Nachrichtenstruktur: bestanden" in output
    assert "timestamp (UTC): 2024-01-01T00:00:00+00:00" in output
    assert "Ende aus Start + Gesamtdauer (UTC): 2024-01-01T00:01:00+00:00" in output
    assert "Letzter Record minus Session-Ende: +0.000 s" in output
    assert source.read_bytes() == data
