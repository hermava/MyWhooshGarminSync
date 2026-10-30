import json
import os
import stat

import pytest

from mywhoosh_garmin_sync import main as cli
from fit_factory import START, make_time_fit

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Local FIT CLI commands have a POSIX storage contract")


def test_preview_never_creates_files_or_reads_accounts(tmp_path, monkeypatch, capsys):
    source = tmp_path / "source.fit"
    data = make_time_fit()
    source.write_bytes(data)
    before = source.stat().st_mtime_ns
    def forbidden(*args, **kwargs):
        pytest.fail("Time repair must remain local and independent of accounts")
    monkeypatch.setattr(cli, "TokenStore", forbidden)
    monkeypatch.setattr(cli, "MyWhooshClient", forbidden)
    assert cli.main(["repair-fit-time", str(source), "--timezone", "Europe/Berlin", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ready" and not result["written"]
    assert result["output_path"] is None and len(result["changes"]) == 2
    assert source.read_bytes() == data and source.stat().st_mtime_ns == before
    assert list(tmp_path.iterdir()) == [source]


@pytest.mark.skipif(os.name != "posix", reason="Legacy local FIT writer has a POSIX contract")
def test_writing_requires_explicit_new_target_and_preserves_original(tmp_path, capsys):
    source = tmp_path / "source.fit"
    source.write_bytes(make_time_fit())
    original = source.read_bytes()
    target = tmp_path / "copies" / "time-fixed.fit"
    args = ["repair-fit-time", str(source), "--timezone", "Europe/Berlin", "--output", str(target), "--json"]
    assert cli.main(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["written"] and target.read_bytes() != original
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    copied = target.read_bytes()
    assert cli.main(args) == 1
    assert "existiert bereits" in capsys.readouterr().err
    assert target.read_bytes() == copied and source.read_bytes() == original
    assert cli.main(["repair-fit-time", str(source), "--timezone", "UTC", "--output", str(source)]) == 1
    assert source.read_bytes() == original


@pytest.mark.parametrize("options,status,code", [({"include_stop": False}, "needs_review", 1),
                                                ({"manufacturer": 1}, "not_applicable", 0),
                                                ({"activity_time": START + 60, "local": START + 60}, "unchanged", 0)])
def test_uncertain_or_unchanged_inputs_never_create_a_copy(tmp_path, capsys, options, status, code):
    source, target = tmp_path / "source.fit", tmp_path / "copy.fit"
    source.write_bytes(make_time_fit(**options))
    assert cli.main(["repair-fit-time", str(source), "--timezone", "UTC", "--output", str(target), "--json"]) == code
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == status and not result["written"] and not target.exists()


def test_timezone_is_required_and_invalid_zone_creates_no_files(tmp_path, capsys):
    source = tmp_path / "source.fit"
    source.write_bytes(make_time_fit())
    with pytest.raises(SystemExit):
        cli.main(["repair-fit-time", str(source)])
    capsys.readouterr()
    assert cli.main(["repair-fit-time", str(source), "--timezone", "Invalid/Timezone"]) == 1
    assert "IANA-Zeitzone" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == [source]
