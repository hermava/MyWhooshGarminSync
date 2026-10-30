import hashlib
import json
import sys

import httpx
import pytest

from mywhoosh_garmin_sync import garmin
from mywhoosh_garmin_sync import main as cli
from fit_factory import make_fit
from test_garmin import row
from test_garmin_cli import Backend
from test_garmin_upload import accepted, detail


def setup_upload(tmp_path, monkeypatch, *, confirmation="confirm", initial=None, refreshed=None,
                 post_failure=None, mutate_source=False):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "synthetic.fit"
    data = make_fit()
    source.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    backend = Backend()
    calls = []
    prompts = []
    lists = iter([initial or [], refreshed or []])

    def handle(request):
        calls.append(request)
        if request.method == "POST":
            if post_failure == "interrupt":
                raise KeyboardInterrupt
            if post_failure == "unexpected":
                raise RuntimeError("synthetic-private-error")
            if post_failure == "timeout":
                raise httpx.ReadTimeout("synthetic-private-error", request=request)
            if post_failure and post_failure.isdecimal():
                return httpx.Response(int(post_failure), text="synthetic-private-error")
            assert data in request.content
            return httpx.Response(200, json=accepted())
        if str(request.url).startswith(garmin.ACTIVITIES_URL):
            return httpx.Response(200, json=next(lists))
        return httpx.Response(200, json=detail())

    real_uploader = garmin.GarminUploader
    monkeypatch.setattr(garmin, "GarminUploader", lambda: real_uploader(backend=backend, transport=httpx.MockTransport(handle)))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)

    def answer(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:
            return "example@example.invalid"
        if mutate_source:
            source.write_bytes(b"changed-after-validation")
        if confirmation == "eof":
            raise EOFError
        return f"UPLOAD {digest[:12]}" if confirmation == "confirm" else confirmation

    monkeypatch.setattr("builtins.input", answer)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "synthetic-password")

    def forbidden(*args):
        pytest.fail("Uploads must not read or write account token stores")

    monkeypatch.setattr(cli, "TokenStore", forbidden)
    receipt = tmp_path / "private" / "garmin-uploads" / f"{digest}.json"
    return source, data, receipt, calls, prompts, backend


@pytest.mark.parametrize("mutate_source", [False, True])
def test_upload_cli_confirms_selected_bytes_and_saves_verified_result(tmp_path, monkeypatch, capsys, mutate_source):
    source, data, receipt, calls, prompts, backend = setup_upload(tmp_path, monkeypatch, mutate_source=mutate_source)
    assert cli.main(["garmin-upload", str(source), "--interactive", "--json"]) == 0
    output = capsys.readouterr()
    result = json.loads(output.out)
    assert result["status"] == "uploaded"
    assert result["activity_id"] == "123"
    assert result["sha256"] == hashlib.sha256(data).hexdigest()
    assert json.loads(receipt.read_text())["status"] == "uploaded"
    assert len(prompts) == 2
    assert [r.method for r in calls] == ["GET", "GET", "POST", "GET"]
    assert backend.closed
    if not mutate_source:
        assert source.read_bytes() == data
    for secret in ("example@example.invalid", "synthetic-password", "synthetic-token"):
        assert secret not in output.out + output.err
    assert "Ältere Duplikate" in output.err


@pytest.mark.parametrize("answer", ["no", "", "yes", "UPLOAD wrong-hash", "eof"])
def test_missing_confirmation_never_creates_attempt_or_posts(tmp_path, monkeypatch, capsys, answer):
    source, data, receipt, calls, prompts, backend = setup_upload(tmp_path, monkeypatch, confirmation=answer)
    assert cli.main(["garmin-upload", str(source), "--interactive"]) == 130
    assert not receipt.exists()
    assert [r.method for r in calls] == ["GET"]
    assert source.read_bytes() == data
    assert backend.closed


@pytest.mark.parametrize("stage", ["initial", "refreshed"])
@pytest.mark.parametrize("activity", [row(), row(startTimeGMT=None)])
def test_candidate_and_incomplete_data_block_before_or_after_confirmation(tmp_path, monkeypatch, capsys, stage, activity):
    setup = {stage: [activity]}
    source, data, receipt, calls, prompts, backend = setup_upload(tmp_path, monkeypatch, **setup)
    assert cli.main(["garmin-upload", str(source), "--interactive"]) == 1
    assert "Upload gesperrt" in capsys.readouterr().err
    assert not receipt.exists()
    assert all(r.method == "GET" for r in calls)
    assert len(prompts) == (1 if stage == "initial" else 2)
    assert backend.closed


@pytest.mark.parametrize("failure,exit_code", [("interrupt", 130), ("unexpected", 1), ("timeout", 1),
                                              ("403", 1), ("409", 1), ("429", 1), ("500", 1)])
def test_unclear_attempt_persists_and_blocks_rerun_before_credentials(tmp_path, monkeypatch, capsys, failure, exit_code):
    source, data, receipt, calls, prompts, backend = setup_upload(tmp_path, monkeypatch, post_failure=failure)
    assert cli.main(["garmin-upload", str(source), "--interactive"]) == exit_code
    output = capsys.readouterr()
    assert "nicht erneut senden" in (output.err + output.out).lower()
    assert "synthetic-private-error" not in output.out + output.err
    stored = json.loads(receipt.read_text())
    assert stored["status"] == ("upload_pending" if failure in ("interrupt", "unexpected") else "needs_review")
    if failure == "timeout":
        assert stored["reason"] == "upload_timeout"
        assert "Zeitüberschreitung" in output.out
    if failure.isdecimal():
        assert stored["reason"] == f"upload_http_{failure}"
        assert f"HTTP {failure}" in output.out
    assert [r.method for r in calls] == ["GET", "GET", "POST"]
    assert backend.closed

    def forbidden(*args):
        pytest.fail("Existing attempt must be rejected before login or further HTTP calls")

    monkeypatch.setattr(garmin, "GarminUploader", forbidden)
    monkeypatch.setattr("builtins.input", forbidden)
    assert cli.main(["garmin-upload", str(source), "--interactive"]) == 1
    assert "bereits ein Upload-Vermerk" in capsys.readouterr().err
    assert len(calls) == 3


@pytest.mark.parametrize("reason", ["upload_not_confirmed", "unknown_import_result", "missing_activity_id",
                                   "verification_failed", "verification_mismatch", "verification_http_404",
                                   "verification_timeout", "upload_response_compressed", "upload_invalid_json",
                                   "upload_response_too_large", "upload_response_time_limit", "upload_transport_error"])
def test_diagnosis_explains_known_stage_and_error_without_raw_payload(reason):
    message = cli._upload_diagnosis(reason)
    assert message != "Keine unterstützte Diagnose verfügbar."


def test_unknown_diagnosis_never_echoes_input():
    assert cli._upload_diagnosis("upload_http_403 synthetic-private-error") == "Keine unterstützte Diagnose verfügbar."


def test_upload_requires_interactive_terminal_and_does_not_support_force(monkeypatch, capsys):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert cli.main(["garmin-upload", "unused.fit", "--interactive"]) == 1
    assert "interaktives Terminal" in capsys.readouterr().err
    for args in (["garmin-upload", "unused.fit"],
                 ["garmin-upload", "unused.fit", "--interactive", "--yes"],
                 ["garmin-upload", "unused.fit", "--interactive", "--limit", "1"]):
        with pytest.raises(SystemExit) as error:
            cli.main(args)
        assert error.value.code == 2


def test_failed_journal_sync_prevents_any_post(tmp_path, monkeypatch, capsys):
    from mywhoosh_garmin_sync import upload

    source, data, receipt, calls, prompts, backend = setup_upload(tmp_path, monkeypatch)

    def failed_sync(*args):
        raise OSError("synthetic-private-error")

    monkeypatch.setattr(upload.os, "fsync", failed_sync)
    assert cli.main(["garmin-upload", str(source), "--interactive"]) == 1
    output = capsys.readouterr()
    assert "kein Upload ausgeführt" in output.err
    assert "synthetic-private-error" not in output.err
    assert receipt.exists()
    assert json.loads(receipt.read_text())["status"] == "upload_pending"
    assert [r.method for r in calls] == ["GET", "GET"]
    assert source.read_bytes() == data
    assert backend.closed
