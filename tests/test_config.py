import io
import json
import sys

import fitdecode
import pytest

from mywhoosh_garmin_sync import config
from mywhoosh_garmin_sync import main as cli
from mywhoosh_garmin_sync.errors import SessionError
from fit_factory import make_identity_fit


def test_absent_default_is_optional_but_explicit_missing_file_is_an_error(tmp_path):
    assert config.load_device_config() == config.DeviceConfig()
    with pytest.raises(SessionError, match="nicht gefunden"):
        config.load_device_config(tmp_path / "missing.toml")


@pytest.mark.parametrize("unit_id", [1, 222222222, 4000000001, 0xFFFFFFFF])
def test_each_user_can_configure_their_own_unsigned_unit_id(tmp_path, unit_id):
    path = tmp_path / "device.toml"
    path.write_text(f'[device]\nprofile = "edge530"\nunit_id = {unit_id}\n')
    device = config.load_device_config(path)
    assert device.profile == "edge530"
    assert device.unit_id == unit_id
    assert "unit_id" not in repr(device)


@pytest.mark.parametrize("body", ['[device]\nunit_id = 0', '[device]\nunit_id = -1',
                                  '[device]\nunit_id = 4294967296', '[device]\nunit_id = true',
                                  '[device]\nunit_id = "synthetic-private-value"',
                                  '[device]\nunit_id = 1.5', '[device]\nprofile = "unsupported"',
                                  '[device]\nprofile = false', '[device]\nunitid = 222222222',
                                  '[account]\npassword = "synthetic-private-value"',
                                  'device = "synthetic-private-value"', '[device\n',
                                  '[device]\nunit_id = "synthetic-private-value"\nunit_id = 1'])
def test_bad_or_unknown_settings_fail_without_echoing_values(tmp_path, body):
    path = tmp_path / "device.toml"
    path.write_text(body)
    with pytest.raises(SessionError) as error:
        config.load_device_config(path)
    assert "synthetic-private-value" not in str(error.value)
    assert "222222222" not in str(error.value)


@pytest.mark.parametrize("body", [b"x" * (config.MAX_CONFIG_BYTES + 1), b"\xFF"])
def test_config_read_is_bounded_and_requires_utf8(tmp_path, body):
    path = tmp_path / "device.toml"
    path.write_bytes(body)
    with pytest.raises(SessionError):
        config.load_device_config(path)


@pytest.mark.parametrize("explicit,prompt", [(False, False), (True, False), (False, True)])
def test_preparation_uses_local_config_and_manual_input_can_override(tmp_path, monkeypatch, capsys, explicit, prompt):
    path = tmp_path / "device.toml" if explicit else config.DEFAULT_CONFIG_PATH
    path.write_text('[device]\nprofile = "edge530"\nunit_id = 4000000001\n')
    source = tmp_path / "source.fit"
    source.write_bytes(make_identity_fit())
    destination = tmp_path / "output.fit"
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)

    def forbidden(*args):
        pytest.fail("Configured preparation needs no prompt or account state")

    monkeypatch.setattr(cli.getpass, "getpass", forbidden)
    monkeypatch.setattr(cli, "TokenStore", forbidden)
    args = ["prepare-fit", str(source), "--output", str(destination), "--json"]
    if explicit:
        args += ["--config", str(path)]
    if prompt:
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(cli.getpass, "getpass", lambda question: "222222222")
        args.append("--device-unit-id")
    assert cli.main(args) == 0
    output = capsys.readouterr()
    assert json.loads(output.out)["profile"] == "edge530"
    assert "4000000001" not in output.out + output.err
    assert "222222222" not in output.out + output.err
    with fitdecode.FitReader(io.BytesIO(destination.read_bytes())) as reader:
        identities = [m for m in reader if m.frame_type == fitdecode.FIT_FRAME_DATA and
                      (m.name == "file_id" or m.name == "device_info" and m.get_raw_value("device_index") == 0)]
    assert len(identities) == 2
    assert all(m.get_raw_value("serial_number") == (222222222 if prompt else 4000000001) for m in identities)


def test_invalid_config_is_rejected_before_opening_fit_or_prompting(tmp_path, monkeypatch, capsys):
    config.DEFAULT_CONFIG_PATH.write_text('[device]\nunit_id = "synthetic-private-value"\n')

    def forbidden(*args):
        pytest.fail("Invalid configuration must be caught before accessing FIT or prompting")

    monkeypatch.setattr(cli, "read_fit", forbidden)
    monkeypatch.setattr(cli.getpass, "getpass", forbidden)
    assert cli.main(["prepare-fit", "unused.fit", "--profile", "edge530"]) == 1
    assert "synthetic-private-value" not in capsys.readouterr().err


def test_account_read_commands_do_not_load_local_device_config(monkeypatch, capsys):
    def forbidden(*args):
        pytest.fail("Device configuration only belongs to prepare-fit")

    monkeypatch.setattr(config, "load_device_config", forbidden)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert cli.main(["garmin-activities", "--interactive"]) == 1
    assert "interaktives Terminal" in capsys.readouterr().err
