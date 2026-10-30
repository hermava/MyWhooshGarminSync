from dataclasses import replace
import hashlib
import json

import pytest

from mywhoosh_garmin_sync.errors import SessionError
from mywhoosh_garmin_sync.fit_identity import prepare_edge530
from mywhoosh_garmin_sync.fit_time import prepare_time_repair, prepare_automatic_time_repair
from mywhoosh_garmin_sync.sync import SyncSettings
from mywhoosh_garmin_sync.sync_state import SyncState
from fit_factory import START, make_time_fit
from test_sync import engine_for, selected


@pytest.mark.parametrize("profile", [None, "edge530"])
def test_sync_repairs_before_device_profile_and_uploads_exact_preview_bytes(tmp_path, profile):
    engine, accounts = engine_for(tmp_path)
    source = accounts.files["ride-0"] = make_time_fit()
    settings = SyncSettings(profile=profile, unit_id=222222222 if profile else None, timezone="Europe/Berlin")
    plan = engine.preview(settings)
    item = plan.items[0]
    assert item.status == "ready" and item.time_status == "corrected"
    assert "Europe/Berlin" in item.time_detail and "00:01:00" in item.time_detail
    assert not accounts.posts
    corrected, _ = prepare_automatic_time_repair(source, timezone_name="Europe/Berlin")
    expected = prepare_edge530(corrected, unit_id=222222222)[0] if profile else corrected
    with SyncState(engine.workspace) as state:
        row = state.get(plan.scope, item.source_id)
        assert row["target_name"] != row["original_name"]
        assert engine.workspace.read(row["original_name"], maximum=10000) == source
        assert engine.workspace.read(row["target_name"], maximum=10000) == expected
    result = engine.execute(plan.identity, selected(plan))
    assert result.items[0].status == "uploaded"
    assert result.items[0].time_status == "corrected"
    assert result.items[0].time_detail == item.time_detail
    sent = accounts.posts[0].content.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
    assert sent == expected
    for variant in (source, corrected, expected):
        assert engine.journal.path(hashlib.sha256(variant).hexdigest() + ".json").exists()
    # Changing timezone must not reinterpret, reprepare or resend an uploaded ride.
    again = engine.preview(replace(settings, timezone="UTC"))
    assert again.items[0].status == "uploaded"
    assert again.items[0].time_detail == item.time_detail
    assert not selected(again) and len(accounts.posts) == 1


def test_bad_clock_without_timezone_blocks_until_user_selects_one(tmp_path):
    engine, accounts = engine_for(tmp_path)
    accounts.files["ride-0"] = make_time_fit()
    blocked = engine.preview(SyncSettings())
    assert blocked.items[0].status == "blocked"
    assert blocked.items[0].time_status == "needs_timezone"
    assert blocked.items[0].reason == "timezone_required"
    with pytest.raises(SessionError):
        engine.execute(blocked.identity, ["ride-0"])
    fixed = engine.preview(SyncSettings(timezone="Europe/Berlin"))
    assert fixed.items[0].status == "ready" and fixed.items[0].time_status == "corrected"
    assert not accounts.posts


def test_ambiguous_time_stays_blocked_and_original_is_preserved(tmp_path):
    engine, accounts = engine_for(tmp_path)
    source = accounts.files["ride-0"] = make_time_fit(stop_offset=65)
    plan = engine.preview(SyncSettings(timezone="Europe/Berlin"))
    assert plan.items[0].status == "blocked" and plan.items[0].reason == "time_review"
    assert plan.items[0].time_status == "needs_review"
    with SyncState(engine.workspace) as state:
        row = state.get(plan.scope, "ride-0")
        assert row["target_hash"] is None and row["target_name"] is None
        assert engine.workspace.read(row["original_name"], maximum=10000) == source
    assert not accounts.posts and not selected(plan)


def test_correct_mywhoosh_times_are_not_changed_without_timezone(tmp_path):
    engine, accounts = engine_for(tmp_path)
    source = accounts.files["ride-0"] = make_time_fit(activity_time=START + 60, local=START + 60 + 3600)
    plan = engine.preview(SyncSettings())
    assert plan.items[0].status == "ready" and plan.items[0].time_status == "unchanged"
    assert plan.items[0].target_hash == hashlib.sha256(source).hexdigest()


@pytest.mark.parametrize("variant", ["original", "time_fixed", "old_device"])
def test_legacy_uploads_of_all_related_variants_block_corrected_sync(tmp_path, variant):
    engine, accounts = engine_for(tmp_path)
    original = accounts.files["ride-0"] = make_time_fit()
    data = {"original": original,
            "time_fixed": prepare_time_repair(original, timezone_name="Europe/Berlin")[0],
            "old_device": prepare_edge530(original, unit_id=222222222)[0]}[variant]
    engine.journal.write(hashlib.sha256(data).hexdigest() + ".json", b'{"status":"uploaded"}')
    plan = engine.preview(SyncSettings(profile="edge530", unit_id=222222222, timezone="Europe/Berlin"))
    assert plan.items[0].status == "needs_review" and plan.items[0].reason == "previous_attempt"
    assert not accounts.posts and not selected(plan)


def test_old_device_upload_after_preview_is_checked_again_before_post(tmp_path):
    engine, accounts = engine_for(tmp_path)
    original = accounts.files["ride-0"] = make_time_fit()
    plan = engine.preview(SyncSettings(profile="edge530", unit_id=222222222, timezone="Europe/Berlin"))
    old_copy = prepare_edge530(original, unit_id=222222222)[0]
    engine.journal.write(hashlib.sha256(old_copy).hexdigest() + ".json", b'{"status":"uploaded"}')
    result = engine.execute(plan.identity, selected(plan))
    assert result.stopped and not accounts.posts


def test_timezone_and_feedback_survive_restart_without_account_identifiers(tmp_path):
    from mywhoosh_garmin_sync.sync import SyncEngine, load_settings
    engine, accounts = engine_for(tmp_path)
    accounts.files["ride-0"] = make_time_fit()
    engine.preview(SyncSettings(timezone="Europe/Berlin"))
    settings = load_settings(engine.workspace)
    assert settings.timezone == "Europe/Berlin"
    raw = json.loads(engine.workspace.read("settings.json", maximum=8192))
    assert set(raw) == {"days", "limit", "profile", "unit_id", "timezone", "adjust_times"}
    restarted = SyncEngine(accounts, engine.workspace, legacy_directory=engine.journal.root, clock=engine.clock)
    plan = restarted.preview(settings)
    assert plan.items[0].time_status == "corrected"


@pytest.mark.parametrize("profile", [None, "edge530"])
def test_disabled_adjustment_ignores_previous_approvals_and_preserves_all_times(tmp_path, profile):
    engine, accounts = engine_for(tmp_path)
    source = accounts.files["ride-0"] = make_time_fit(include_stop=False)
    settings = SyncSettings(timezone="Europe/Berlin", profile=profile,
                            unit_id=222222222 if profile else None, adjust_times=False)
    plan = engine.preview(settings)
    assert plan.items[0].status == "ready" and plan.items[0].time_status == "disabled"
    with SyncState(engine.workspace) as state:
        row = state.get(plan.scope, "ride-0")
        state.update(plan.scope, "ride-0", "ready", time_approval='{"old":"approval"}')
        expected = prepare_edge530(source, unit_id=222222222)[0] if profile else source
        assert engine.workspace.read(row["target_name"], maximum=10000) == expected
    again = engine.preview(settings)
    assert again.items[0].target_hash == hashlib.sha256(expected).hexdigest()
    assert engine.execute(again.identity, selected(again)).items[0].status == "uploaded"
    assert accounts.remote[0].start_time_utc == "2024-01-01T00:00:00+00:00"


def test_toggling_adjustment_reprepares_from_original_without_cumulative_shifts(tmp_path):
    engine, accounts = engine_for(tmp_path)
    accounts.files["ride-0"] = make_time_fit(include_stop=False)
    settings = SyncSettings(timezone="Europe/Berlin")
    first = engine.preview(settings)
    unchanged = engine.preview(replace(settings, adjust_times=False))
    assert unchanged.items[0].start_utc == "2024-01-01T00:00:00+00:00"
    with pytest.raises(SessionError):
        engine.execute(first.identity, selected(first))
    again = engine.preview(settings)
    assert again.items[0].target_hash == first.items[0].target_hash
    assert again.items[0].start_utc == "2023-12-31T23:00:00+00:00"
    assert not accounts.posts


def test_local_clock_conversion_uses_corrected_start_in_preview_upload_and_restart(tmp_path):
    engine, accounts = engine_for(tmp_path)
    accounts.files["ride-0"] = make_time_fit(include_stop=False)
    settings = SyncSettings(timezone="Europe/Berlin", profile="edge530", unit_id=222222222)
    plan = engine.preview(settings)
    corrected = plan
    assert corrected.items[0].status == "ready"
    assert corrected.items[0].start_utc == "2023-12-31T23:00:00+00:00"
    assert "Entire recording converted" in corrected.items[0].time_detail
    assert not accounts.posts
    assert engine.execute(corrected.identity, selected(corrected)).items[0].status == "uploaded"
    assert accounts.remote[0].start_time_utc == "2023-12-31T23:00:00+00:00"
    again = engine.preview(settings)
    assert again.items[0].status == "uploaded"
    assert again.items[0].start_utc == "2023-12-31T23:00:00+00:00"
    assert len(accounts.posts) == 1


@pytest.mark.parametrize("start", ["2024-01-01T00:00:00+00:00", "2023-12-31T23:00:00+00:00"])
def test_remote_duplicates_with_original_or_corrected_clock_block_local_conversion(tmp_path, start):
    from mywhoosh_garmin_sync.garmin import GarminActivity
    engine, accounts = engine_for(tmp_path)
    accounts.files["ride-0"] = make_time_fit(include_stop=False)
    accounts.remote.append(GarminActivity("501", "Existing ride", start, 60, 60))
    corrected = engine.preview(SyncSettings(timezone="Europe/Berlin"))
    assert corrected.items[0].status == "possible_duplicate"
    assert not accounts.posts


def test_late_original_clock_duplicate_is_checked_at_final_upload_preflight(tmp_path):
    from mywhoosh_garmin_sync.garmin import GarminActivity
    engine, accounts = engine_for(tmp_path)
    accounts.files["ride-0"] = make_time_fit(include_stop=False)
    corrected = engine.preview(SyncSettings(timezone="Europe/Berlin"))
    accounts.remote.append(GarminActivity("501", "Late existing ride", "2024-01-01T00:00:00+00:00", 60, 60))
    result = engine.execute(corrected.identity, selected(corrected))
    assert result.stopped and not accounts.posts


def test_future_looking_local_api_clock_is_discoverable_but_requires_correct_interpretation(tmp_path):
    from datetime import timedelta
    engine, accounts = engine_for(tmp_path)
    accounts.files["ride-0"] = make_time_fit()
    engine.clock = lambda: accounts.rides[0].start_time - timedelta(hours=1) + timedelta(minutes=2)
    plan = engine.preview(SyncSettings(timezone="Europe/Berlin"))
    assert len(plan.items) == 1 and plan.items[0].status == "ready"
    assert plan.items[0].start_utc == "2023-12-31T23:00:00+00:00"
    assert not accounts.posts
