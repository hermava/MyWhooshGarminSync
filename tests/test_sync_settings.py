import json

import pytest

from mywhoosh_garmin_sync.errors import SessionError
from mywhoosh_garmin_sync.sync import SyncEngine, SyncSettings, load_settings
from mywhoosh_garmin_sync.workspace import Workspace


@pytest.mark.parametrize("options", [{"days": 0}, {"days": True}, {"days": 366}, {"limit": 21},
                                     {"limit": 0}, {"profile": "unknown"}, {"profile": "edge530"},
                                     {"unit_id": True}, {"unit_id": 0}, {"unit_id": 0x100000000},
                                     {"timezone": "Not/AZone"}, {"timezone": ""}, {"timezone": 123}, {"adjust_times": "false"}, {"adjust_times": 0}])
def test_settings_are_bounded_and_edge_profile_uses_own_unit_id(options):
    with pytest.raises(SessionError):
        SyncSettings(**options)


def test_no_saved_settings_uses_only_neutral_device_config_fixture(tmp_path):
    assert load_settings(Workspace(tmp_path / "absent")) == SyncSettings()
    assert not (tmp_path / "absent").exists()


def test_saved_settings_reject_unknown_secret_fields_and_do_not_echo_data(tmp_path):
    workspace = Workspace(tmp_path / "data")
    workspace.write("settings.json", json.dumps({"password": "synthetic-private-password"}).encode())
    with pytest.raises(SessionError) as error:
        load_settings(workspace)
    assert "synthetic-private-password" not in str(error.value)


def test_existing_settings_without_timezone_are_loaded_without_guessing(tmp_path):
    workspace = Workspace(tmp_path / "data")
    workspace.write("settings.json", json.dumps({"days": 7, "limit": 10, "profile": None, "unit_id": None}).encode())
    assert load_settings(workspace).timezone is None


def test_device_preferences_survive_restart_without_accounts_or_preview(tmp_path):
    workspace = Workspace(tmp_path / "data")
    settings = SyncSettings(profile="edge530", unit_id=222222222, timezone="Europe/Berlin")
    SyncEngine(object(), workspace, legacy_directory=tmp_path / "journal").save_settings(settings)
    assert load_settings(Workspace(workspace.root)) == settings
    assert not workspace.path("sync.sqlite3").exists()


def test_existing_settings_enable_automatic_adjustment_and_explicit_false_survives(tmp_path):
    workspace = Workspace(tmp_path / "data")
    old = {"days": 7, "limit": 10, "profile": None, "unit_id": None}
    workspace.write("settings.json", json.dumps(old).encode())
    assert load_settings(workspace).adjust_times is True
    old["adjust_times"] = False
    workspace.write("settings.json", json.dumps(old).encode(), replace=True)
    assert load_settings(workspace).adjust_times is False
