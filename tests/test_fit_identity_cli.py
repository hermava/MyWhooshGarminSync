import getpass
import json
import stat
import sys
import warnings

import pytest

from mywhoosh_garmin_sync import main as cli
from mywhoosh_garmin_sync.fit_processing import validate_fit
from fit_factory import make_identity_fit


@pytest.mark.parametrize("dry_run,unit_id", [(False, False), (False, True), (True, False), (True, True)])
def test_prepare_cli_is_local_and_never_overwrites_original(tmp_path, monkeypatch, capsys, dry_run, unit_id):
    source = tmp_path / "synthetic.original.fit"
    data = make_identity_fit(missing_fields=True)
    source.write_bytes(data)
    timestamp = source.stat().st_mtime_ns
    target = tmp_path / "copies" / "synthetic.edge530.fit"

    def forbidden(*args, **kwargs):
        pytest.fail("Preparing FIT copies must not access account state or HTTP")

    monkeypatch.setattr(cli, "TokenStore", forbidden)
    monkeypatch.setattr(cli, "MyWhooshClient", forbidden)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "222222222")
    args = ["prepare-fit", str(source), "--profile", "edge530", "--output", str(target), "--json"]
    if dry_run:
        args.append("--dry-run")
    if unit_id:
        args.append("--device-unit-id")
    assert cli.main(args) == 0
    output = capsys.readouterr()
    report = json.loads(output.out)
    assert report["profile"] == "edge530"
    assert report["dry_run"] == dry_run
    assert "222222222" not in output.out + output.err
    assert source.read_bytes() == data
    assert source.stat().st_mtime_ns == timestamp
    if dry_run:
        assert not target.parent.exists()
    else:
        assert validate_fit(target.read_bytes()).valid
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
        assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
        assert cli.main(args) == 1
        assert "existiert bereits" in capsys.readouterr().err


def test_default_output_name_and_change_summary(tmp_path, capsys):
    source = tmp_path / "synthetic.original.fit"
    source.write_bytes(make_identity_fit())
    assert cli.main(["prepare-fit", str(source), "--profile", "edge530"]) == 0
    output = capsys.readouterr().out
    assert source.with_name("synthetic.original.edge530.fit").exists()
    assert "331 -> 1" in output
    assert "3121" in output
    assert "Kein Upload ausgeführt" in output
    assert "111111111" not in output


@pytest.mark.parametrize("kind", ["same_path", "existing", "symlink", "shared_directory"])
def test_unsafe_or_existing_target_is_rejected_without_modifying_files(tmp_path, capsys, kind):
    source = tmp_path / "source.fit"
    data = make_identity_fit()
    source.write_bytes(data)
    directory = tmp_path / "output"
    directory.mkdir(mode=0o700)
    target = directory / "target.fit"
    if kind == "same_path":
        target = source
    elif kind == "existing":
        target.write_bytes(b"previous-copy")
    elif kind == "symlink":
        target.symlink_to(source)
    else:
        directory.chmod(0o755)
    assert cli.main(["prepare-fit", str(source), "--profile", "edge530", "--output", str(target)]) == 1
    assert source.read_bytes() == data
    if kind == "existing":
        assert target.read_bytes() == b"previous-copy"


@pytest.mark.parametrize("mode", ["invalid", "eof", "notty", "noecho"])
def test_unit_id_input_fails_closed_and_is_not_echoed(tmp_path, monkeypatch, capsys, mode):
    source = tmp_path / "source.fit"
    source.write_bytes(make_identity_fit())
    monkeypatch.setattr(sys.stdin, "isatty", lambda: mode != "notty")

    def secret(prompt):
        if mode == "eof":
            raise EOFError
        if mode == "noecho":
            warnings.warn("synthetic-private-error", getpass.GetPassWarning)
        return "synthetic-private-error"

    monkeypatch.setattr(cli.getpass, "getpass", secret)
    assert cli.main(["prepare-fit", str(source), "--profile", "edge530", "--device-unit-id"]) == (130 if mode == "eof" else 1)
    output = capsys.readouterr()
    assert "synthetic-private-error" not in output.out + output.err
    assert not source.with_name("source.edge530.fit").exists()


def test_prepare_requires_profile_when_not_configured(capsys):
    assert cli.main(["prepare-fit", "unused.fit"]) == 1
    assert "Geräteprofil fehlt" in capsys.readouterr().err
