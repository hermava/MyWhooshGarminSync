import hashlib
import json

import httpx
import pytest

from mywhoosh_garmin_sync.errors import SessionError, SyncError
from mywhoosh_garmin_sync.sync import SyncSettings
from mywhoosh_garmin_sync.sync_state import SyncState
from test_sync import engine_for, selected


def uploaded(tmp_path, *, identity_fit=False):
    engine, accounts = engine_for(tmp_path, identity_fit=identity_fit)
    plan = engine.preview(SyncSettings())
    result = engine.execute(plan.identity, selected(plan))
    assert result.items[0].status == "uploaded"
    return engine, accounts


@pytest.mark.parametrize("change_profile", [False, True])
def test_deleted_confirmed_upload_can_retry_once_without_erasing_history(tmp_path, change_profile):
    engine, accounts = uploaded(tmp_path, identity_fit=change_profile)
    before = {p.name: p.read_bytes() for p in engine.journal.root.glob("*.json")}
    accounts.remote.clear()
    settings = SyncSettings(profile="edge530", unit_id=222222222) if change_profile else SyncSettings()
    plan = engine.preview(settings)
    assert plan.items[0].status == "uploaded"
    retry = engine.authorize_retry(plan.identity, "ride-0")
    assert retry.items[0].status == "ready" and len(accounts.posts) == 1
    assert all(engine.journal.path(name).read_bytes() == data for name, data in before.items())
    result = engine.execute(retry.identity, selected(retry))
    assert result.items[0].status == "uploaded" and result.items[0].garmin_id == "102"
    assert len(accounts.posts) == 2
    assert all(engine.journal.path(name).read_bytes() == data for name, data in before.items())
    with SyncState(engine.workspace) as state:
        rows = state.db.execute("SELECT * FROM attempts").fetchall()
        assert len(rows) == 2 and {r["garmin_id"] for r in rows} == {"101", "102"}
        assert json.loads(state.get(retry.scope, "ride-0")["retry_authorization"])["used"]
    assert engine.preview(settings).items[0].status == "uploaded"
    assert list(engine.journal.root.glob("*.retry-*.json"))


def test_repeated_confirmed_deletions_keep_all_attempts(tmp_path):
    engine, accounts = uploaded(tmp_path)
    for expected in (2, 3):
        accounts.remote.clear()
        plan = engine.preview(SyncSettings())
        retry = engine.authorize_retry(plan.identity, "ride-0")
        assert engine.execute(retry.identity, selected(retry)).items[0].status == "uploaded"
        assert len(accounts.posts) == expected
    with SyncState(engine.workspace) as state:
        assert state.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 3


@pytest.mark.parametrize("response", [401, 403, 429, 500, "malformed", "timeout"])
def test_auth_network_or_invalid_response_never_confirms_deletion(tmp_path, response):
    engine, accounts = uploaded(tmp_path)
    accounts.remote.clear()
    plan = engine.preview(SyncSettings())
    original = accounts.handle
    def handle(request):
        if request.url.path.endswith("/101"):
            if response == "timeout":
                raise httpx.ReadTimeout("synthetic")
            return httpx.Response(200, json={"unexpected": True}) if response == "malformed" else httpx.Response(response)
        return original(request)
    accounts.handle = handle
    with pytest.raises(SyncError):
        engine.authorize_retry(plan.identity, "ride-0")
    with SyncState(engine.workspace) as state:
        row = state.get(plan.scope, "ride-0")
        assert row["status"] == "uploaded" and row["retry_authorization"] == ""
    assert len(accounts.posts) == 1


def test_missing_from_list_is_not_proof_of_deletion(tmp_path):
    engine, accounts = uploaded(tmp_path)
    plan = engine.preview(SyncSettings())
    original = accounts.handle
    accounts.handle = lambda request: httpx.Response(200, json=[]) if request.url.path.endswith("/activities") else original(request)
    with pytest.raises(SessionError, match="still present"):
        engine.authorize_retry(plan.identity, "ride-0")
    assert len(accounts.posts) == 1


def test_reappearing_activity_blocks_before_second_post(tmp_path):
    engine, accounts = uploaded(tmp_path)
    old = accounts.remote.pop()
    plan = engine.preview(SyncSettings())
    retry = engine.authorize_retry(plan.identity, "ride-0")
    accounts.remote.append(old)
    result = engine.execute(retry.identity, selected(retry))
    assert result.stopped and len(accounts.posts) == 1


def test_uncertain_result_after_retry_consumes_authorization_and_stays_blocked(tmp_path):
    engine, accounts = uploaded(tmp_path)
    accounts.remote.clear()
    plan = engine.preview(SyncSettings())
    retry = engine.authorize_retry(plan.identity, "ride-0")
    accounts.mode = "timeout-after-write"
    result = engine.execute(retry.identity, selected(retry))
    assert result.items[0].status == "needs_review"
    blocked = engine.preview(SyncSettings())
    assert blocked.items[0].status == "needs_review"
    with pytest.raises(SessionError):
        engine.authorize_retry(blocked.identity, "ride-0")
    with SyncState(engine.workspace) as state:
        assert state.db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 2
        assert json.loads(state.get(blocked.scope, "ride-0")["retry_authorization"])["used"]
    assert len(accounts.posts) == 2


def test_unclear_related_journal_cannot_be_bypassed(tmp_path):
    engine, accounts = uploaded(tmp_path)
    file_hash = hashlib.sha256(accounts.files["ride-0"]).hexdigest()
    engine.journal.write(file_hash + ".retry-unrelated.json", b'{"status":"upload_pending"}')
    accounts.remote.clear()
    plan = engine.preview(SyncSettings())
    with pytest.raises(SessionError, match="Unclear"):
        engine.authorize_retry(plan.identity, "ride-0")
    with SyncState(engine.workspace) as state:
        assert state.get(plan.scope, "ride-0")["retry_authorization"] == ""
    assert len(accounts.posts) == 1


def test_uncertain_original_upload_is_never_reopened(tmp_path):
    engine, accounts = engine_for(tmp_path)
    accounts.mode = "unknown-import"
    plan = engine.preview(SyncSettings())
    engine.execute(plan.identity, selected(plan))
    accounts.remote.clear()
    blocked = engine.preview(SyncSettings())
    with pytest.raises(SessionError):
        engine.authorize_retry(blocked.identity, "ride-0")
    assert len(accounts.posts) == 1


def uncertain_upload(tmp_path, *, historical=False):
    from fit_factory import make_time_fit
    engine, accounts = engine_for(tmp_path)
    accounts.files["ride-0"] = make_time_fit()
    accounts.mode = "http-520-after-write"
    settings = SyncSettings(timezone="Europe/Berlin", profile="edge530", unit_id=222222222)
    with pytest.MonkeyPatch.context() as patch:
        if historical:
            from dataclasses import replace
            from mywhoosh_garmin_sync import sync as sync_module
            from mywhoosh_garmin_sync.fit_time import prepare_time_repair
            def previous_auto(data, *, timezone_name):
                _, report = prepare_time_repair(data, timezone_name=timezone_name)
                local = next(p for p in report.proposals if p.interpretation == "local")
                output, converted = prepare_time_repair(data, timezone_name=timezone_name, proposal_id=local.id)
                return output, replace(converted, accepted_proposal=None)
            patch.setattr(sync_module, "prepare_automatic_time_repair", previous_auto)
        plan = engine.preview(settings)
        result = engine.execute(plan.identity, selected(plan))
    assert result.items[0].reason == "upload_http_520"
    return engine, accounts, settings


@pytest.mark.parametrize("change_config", [False, True])
def test_reviewed_520_can_retry_with_current_config_without_erasing_receipts(tmp_path, change_config):
    from dataclasses import replace
    engine, accounts, settings = uncertain_upload(tmp_path)
    before = {p.name: p.read_bytes() for p in engine.journal.root.glob("*.json")}
    accounts.remote.clear()  # User checked the web site and found no import.
    accounts.mode = "success"
    if change_config:
        settings = replace(settings, unit_id=333333333, adjust_times=False)
    plan = engine.preview(settings)
    assert plan.items[0].status == "needs_review"
    retry = engine.authorize_uncertain_retry(plan.identity, "ride-0", confirmed_missing=True)
    assert retry.items[0].status == "ready" and len(accounts.posts) == 1
    assert retry.settings == settings
    assert retry.items[0].start_utc == "2024-01-01T00:00:00+00:00"
    result = engine.execute(retry.identity, selected(retry))
    assert result.items[0].status == "uploaded" and len(accounts.posts) == 2
    assert all(engine.journal.path(name).read_bytes() == data for name, data in before.items())
    with SyncState(engine.workspace) as state:
        records = state.owned_attempts(retry.scope, "ride-0")
        assert len(records) == 2 and {r["status"] for r in records} == {"needs_review", "uploaded"}
        assert json.loads(state.get(retry.scope, "ride-0")["retry_authorization"])["used"]
    assert len(list(engine.workspace.root.glob("retry-review-*.json"))) == 1


@pytest.mark.parametrize("confirmed", [False, None, "yes", 1])
def test_uncertain_retry_requires_explicit_manual_confirmation(tmp_path, confirmed):
    engine, accounts, settings = uncertain_upload(tmp_path)
    accounts.remote.clear()
    plan = engine.preview(settings)
    with pytest.raises(SessionError, match="Confirm"):
        engine.authorize_uncertain_retry(plan.identity, "ride-0", confirmed_missing=confirmed)
    assert len(accounts.posts) == 1


def test_manual_confirmation_never_overrides_a_remote_match(tmp_path):
    engine, accounts, settings = uncertain_upload(tmp_path)
    plan = engine.preview(settings)
    with pytest.raises(SessionError, match="matching"):
        engine.authorize_uncertain_retry(plan.identity, "ride-0", confirmed_missing=True)
    assert len(accounts.posts) == 1


def test_late_match_to_old_corrected_clock_blocks_changed_config_retry(tmp_path):
    from dataclasses import replace
    engine, accounts, settings = uncertain_upload(tmp_path, historical=True)
    previous = accounts.remote.pop()
    plan = engine.preview(replace(settings, adjust_times=False))
    retry = engine.authorize_uncertain_retry(plan.identity, "ride-0", confirmed_missing=True)
    accounts.remote.append(previous)
    assert engine.execute(retry.identity, selected(retry)).stopped
    assert len(accounts.posts) == 1


@pytest.mark.parametrize("change", ["journal", "attempt", "new_journal", "deleted_journal"])
def test_reviewed_records_cannot_change_before_retry_post(tmp_path, change):
    engine, accounts, settings = uncertain_upload(tmp_path)
    accounts.remote.clear()
    plan = engine.preview(settings)
    retry = engine.authorize_uncertain_retry(plan.identity, "ride-0", confirmed_missing=True)
    accounts.mode = "success"
    if change == "attempt":
        with SyncState(engine.workspace) as state:
            with state.db:
                state.db.execute("UPDATE attempts SET garmin_id='999'")
    else:
        marker = next(engine.journal.root.glob("*.json"))
        if change == "deleted_journal":
            marker.unlink()
        else:
            name = marker.name if change == "journal" else marker.stem + ".retry-" + "0" * 32 + ".json"
            engine.journal.write(name, b'{"status":"upload_pending"}', replace=change == "journal")
    assert engine.execute(retry.identity, selected(retry)).stopped
    assert len(accounts.posts) == 1


def test_second_uncertain_result_requires_another_explicit_review(tmp_path):
    engine, accounts, settings = uncertain_upload(tmp_path)
    for expected in (2, 3):
        accounts.remote.clear()
        plan = engine.preview(settings)
        retry = engine.authorize_uncertain_retry(plan.identity, "ride-0", confirmed_missing=True)
        result = engine.execute(retry.identity, selected(retry))
        assert result.items[0].status == "needs_review" and len(accounts.posts) == expected
        assert engine.preview(settings).items[0].status == "needs_review"
    with SyncState(engine.workspace) as state:
        assert len(state.owned_attempts(retry.scope, "ride-0")) == 3
    assert len(list(engine.workspace.root.glob("retry-review-*.json"))) == 2


def test_retry_review_expires_without_reopening_on_restart(tmp_path):
    from datetime import timedelta
    engine, accounts, settings = uncertain_upload(tmp_path)
    accounts.remote.clear()
    plan = engine.preview(settings)
    engine.authorize_uncertain_retry(plan.identity, "ride-0", confirmed_missing=True)
    clock = engine.clock()
    engine.clock = lambda: clock + timedelta(minutes=16)
    expired = engine.preview(settings)
    assert expired.items[0].status == "needs_review" and expired.items[0].reason == "retry_expired"
    assert len(accounts.posts) == 1


def test_success_after_review_can_still_be_retried_after_confirmed_deletion(tmp_path):
    engine, accounts, settings = uncertain_upload(tmp_path)
    accounts.remote.clear()
    plan = engine.preview(settings)
    retry = engine.authorize_uncertain_retry(plan.identity, "ride-0", confirmed_missing=True)
    accounts.mode = "success"
    assert engine.execute(retry.identity, selected(retry)).items[0].status == "uploaded"
    accounts.remote.clear()
    plan = engine.preview(settings)
    deleted = engine.authorize_retry(plan.identity, "ride-0")
    assert engine.execute(deleted.identity, selected(deleted)).items[0].status == "uploaded"
    assert len(accounts.posts) == 3


def test_reviewed_retry_uses_cache_even_outside_source_date_window(tmp_path):
    from datetime import timedelta
    engine, accounts, settings = uncertain_upload(tmp_path)
    accounts.remote.clear()
    clock = engine.clock()
    engine.clock = lambda: clock + timedelta(days=10)
    plan = engine.preview(settings)
    assert plan.items[0].status == "needs_review" and plan.review_count == 1
    retry = engine.authorize_uncertain_retry(plan.identity, "ride-0", confirmed_missing=True)
    assert retry.items[0].status == "ready"
    assert engine.preview(settings).items[0].status == "ready"
    accounts.mode = "success"
    fresh = engine.preview(settings)
    assert engine.execute(fresh.identity, selected(fresh)).items[0].status == "uploaded"


def test_known_garmin_id_must_be_missing_even_if_recent_list_is_empty(tmp_path):
    engine, accounts, settings = uncertain_upload(tmp_path)
    with SyncState(engine.workspace) as state:
        scope, _ = engine._scope()
        state.update(scope, "ride-0", "needs_review", garmin_id="101")
        with state.db:
            state.db.execute("UPDATE attempts SET garmin_id='101'")
    # Match the persisted read-back failure; all existing receipts stay intact.
    for path in engine.journal.root.glob("*.json"):
        marker = json.loads(path.read_bytes())
        marker["activity_id"] = "101"
        engine.journal.write(path.name, json.dumps(marker).encode(), replace=True)
    original = accounts.handle
    accounts.handle = lambda request: httpx.Response(200, json=[]) if request.url.path.endswith("/activities") else original(request)
    plan = engine.preview(settings)
    with pytest.raises(SessionError, match="still present"):
        engine.authorize_uncertain_retry(plan.identity, "ride-0", confirmed_missing=True)
    assert len(accounts.posts) == 1


@pytest.mark.parametrize("response", [401, 403, 429, 500, "malformed"])
def test_failed_garmin_read_never_authorizes_an_uncertain_retry(tmp_path, response):
    engine, accounts, settings = uncertain_upload(tmp_path)
    accounts.remote.clear()
    plan = engine.preview(settings)
    original = accounts.handle
    def handle(request):
        if request.url.path.endswith("/activities"):
            return httpx.Response(200, json={"unknown": True}) if response == "malformed" else httpx.Response(response)
        return original(request)
    accounts.handle = handle
    with pytest.raises(SyncError):
        engine.authorize_uncertain_retry(plan.identity, "ride-0", confirmed_missing=True)
    with SyncState(engine.workspace) as state:
        row = state.get(plan.scope, "ride-0")
        assert row["status"] == "needs_review" and row["retry_authorization"] == ""
    assert len(accounts.posts) == 1 and not list(engine.workspace.root.glob("retry-review-*.json"))


def test_unrelated_attempt_cannot_be_covered_by_manual_absence_confirmation(tmp_path):
    engine, accounts, settings = uncertain_upload(tmp_path)
    accounts.remote.clear()
    plan = engine.preview(settings)
    with SyncState(engine.workspace) as state:
        row = state.get(plan.scope, "ride-0")
        target = engine._scope()[1]
        with state.db:
            state.db.execute("INSERT INTO attempts VALUES(?,?,?,?,?,NULL)",
                             (target + ":" + "0" * 32, row["target_hash"], "upload_pending", "unrelated-scope", "another-source"))
    with pytest.raises(SessionError, match="unrelated"):
        engine.authorize_uncertain_retry(plan.identity, "ride-0", confirmed_missing=True)
    assert len(accounts.posts) == 1


def test_changed_audit_snapshot_blocks_before_post(tmp_path):
    engine, accounts, settings = uncertain_upload(tmp_path)
    accounts.remote.clear()
    plan = engine.preview(settings)
    retry = engine.authorize_uncertain_retry(plan.identity, "ride-0", confirmed_missing=True)
    name = next(engine.workspace.root.glob("retry-review-*.json")).name
    engine.workspace.write(name, b'{}', replace=True)
    with pytest.raises(SessionError, match="invalid"):
        engine.execute(retry.identity, selected(retry))
    assert len(accounts.posts) == 1


def test_retry_of_previous_full_timeline_520_uses_compatible_metadata_output(tmp_path):
    from mywhoosh_garmin_sync.fit_identity import prepare_edge530
    from mywhoosh_garmin_sync.fit_time import prepare_time_repair
    engine, accounts, settings = uncertain_upload(tmp_path, historical=True)
    receipts = {p.name: p.read_bytes() for p in engine.journal.root.glob("*.json")}
    old_start = accounts.remote[0].start_time_utc
    assert old_start == "2023-12-31T23:00:00+00:00"
    accounts.remote.clear()
    plan = engine.preview(settings)
    retry = engine.authorize_uncertain_retry(plan.identity, "ride-0", confirmed_missing=True)
    assert retry.items[0].status == "ready"
    assert retry.items[0].start_utc == "2024-01-01T00:00:00+00:00"
    expected, _ = prepare_time_repair(accounts.files["ride-0"], timezone_name="Europe/Berlin")
    expected, _ = prepare_edge530(expected, unit_id=settings.unit_id)
    assert retry.items[0].target_hash == hashlib.sha256(expected).hexdigest()
    accounts.mode = "success"
    assert engine.execute(retry.identity, selected(retry)).items[0].status == "uploaded"
    assert all(engine.journal.path(name).read_bytes() == data for name, data in receipts.items())
