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
