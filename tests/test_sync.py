from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from mywhoosh_garmin_sync.errors import AuthError, SessionError
from mywhoosh_garmin_sync.fit_processing import validate_fit
from mywhoosh_garmin_sync.garmin import GarminActivity, GarminReader, GarminUploader
from mywhoosh_garmin_sync.mywhoosh import Activity, ActivityList
from mywhoosh_garmin_sync.sync import SyncEngine, SyncSettings
from mywhoosh_garmin_sync.sync_state import SyncState
from mywhoosh_garmin_sync.workspace import Workspace
from fit_factory import START, make_fit, make_identity_fit

NOW = datetime(2024, 1, 2, tzinfo=timezone.utc)


class Backend:
    def login(self, *args):
        pass

    def headers(self):
        return {"Authorization": "Bearer synthetic-token"}

    def close(self):
        pass


class Accounts:
    def __init__(self, *, count=1, identity_fit=False):
        self.identity = ("a" * 64, "b" * 64)
        self.files = {}
        self.rides = []
        for index in range(count):
            start = NOW - timedelta(days=1) + timedelta(minutes=index * 5)
            key = f"ride-{index}"
            self.rides.append(Activity(key, "Synthetic Ride " + str(index), start, "00:01:00", "file-" + key))
            offset = index * 300
            self.files[key] = make_identity_fit() if identity_fit else make_fit(
                start=START + offset, end=START + offset + 60, times=(START + offset, START + offset + 60))
        self.remote = []
        self.posts = []
        self.downloads = []
        self.mode = "success"
        self.before_post = lambda: None

    def sync_identity(self):
        return self.identity

    def mywhoosh_activities(self, *, limit):
        assert limit == 100
        return ActivityList(tuple(self.rides), False)

    def garmin_activities(self, *, limit):
        assert limit == 100
        return tuple(self.remote)

    def download_fit(self, activity):
        self.downloads.append(activity.id)
        if not activity.activity_file_id:
            raise SessionError("Synthetic missing file.")
        return self.files[activity.id]

    def handle(self, request):
        if request.url.path.endswith("/activities"):
            if self.mode == "preflight-auth":
                return httpx.Response(401)
            if self.mode == "preflight-network":
                return httpx.Response(500)
            return httpx.Response(200, json=[{"activityId": item.id, "activityName": item.title,
                                             "startTimeGMT": item.start_time_utc, "duration": item.duration_seconds,
                                             "elapsedDuration": item.elapsed_seconds} for item in self.remote])
        if request.method == "POST":
            self.before_post()
            self.posts.append(request)
            # Recover the multipart FIT bytes without using provider responses.
            body = request.content.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
            report = validate_fit(body)
            assert report.valid
            timing = report.session_times[0]
            identity = str(100 + len(self.posts))
            self.remote.append(GarminActivity(identity, "Synthetic Garmin Ride", timing.start_utc,
                                              timing.timer_seconds, timing.elapsed_seconds))
            if self.mode == "timeout-after-write":
                raise httpx.ReadTimeout("synthetic-private-error")
            if self.mode == "unknown-import":
                return httpx.Response(200, json={"synthetic": "unknown"})
            return httpx.Response(200, json={"detailedImportResult": {"failures": [], "successes": [{"internalId": identity}]}})
        identity = request.url.path.rsplit("/", 1)[1]
        item = next((item for item in self.remote if item.id == identity), None)
        if not item:
            return httpx.Response(404)
        return httpx.Response(200, json={"activityId": identity, "activityName": item.title,
                                       "summaryDTO": {"startTimeGMT": item.start_time_utc, "duration": item.duration_seconds,
                                                      "elapsedDuration": item.elapsed_seconds}})

    @contextmanager
    def garmin_connection(self, *, upload=False):
        factory = GarminUploader if upload else GarminReader
        with factory(backend=Backend(), transport=httpx.MockTransport(self.handle)) as reader:
            reader.login("example@example.invalid", "synthetic-password", lambda: "123456")
            yield reader


def engine_for(tmp_path, *, count=1, identity_fit=False):
    accounts = Accounts(count=count, identity_fit=identity_fit)
    workspace = Workspace(tmp_path / "sync")
    engine = SyncEngine(accounts, workspace, legacy_directory=tmp_path / "legacy", clock=lambda: NOW)
    return engine, accounts


def selected(plan):
    return [item.source_id for item in plan.items if item.status == "ready"]


def test_preview_downloads_and_validates_but_never_uploads(tmp_path):
    engine, accounts = engine_for(tmp_path)
    plan = engine.preview(SyncSettings())
    assert selected(plan) == ["ride-0"]
    assert not accounts.posts
    assert len(accounts.downloads) == 1
    assert list(engine.workspace.root.glob("*.original.fit"))
    assert engine.preview(SyncSettings()).items == plan.items
    assert len(accounts.downloads) == 1


def test_success_is_durable_and_profile_changes_cannot_repeat_source_id(tmp_path):
    engine, accounts = engine_for(tmp_path)
    plan = engine.preview(SyncSettings())
    assert engine.execute(plan.identity, selected(plan)).items[0].status == "uploaded"
    restarted = SyncEngine(accounts, engine.workspace, legacy_directory=tmp_path / "legacy", clock=lambda: NOW)
    plan2 = restarted.preview(SyncSettings(profile="edge530", unit_id=123456789))
    assert plan2.items[0].status == "uploaded"
    assert len(accounts.posts) == 1


def test_edge_copy_original_and_intent_exist_before_exact_bytes_are_sent(tmp_path):
    engine, accounts = engine_for(tmp_path, identity_fit=True)
    original = accounts.files["ride-0"]
    plan = engine.preview(SyncSettings(profile="edge530", unit_id=123456789))
    scope = plan.scope

    def before():
        with SyncState(engine.workspace) as state:
            row = state.get(scope, "ride-0")
            assert row["status"] == "upload_pending"
            assert engine.workspace.read(row["original_name"], maximum=999999) == original
            assert row["original_hash"] != row["target_hash"]
            for digest in (row["original_hash"], row["target_hash"]):
                marker = json.loads(engine.journal.read(digest + ".json", maximum=8192))
                assert marker["status"] == "upload_pending"

    accounts.before_post = before
    assert engine.execute(plan.identity, selected(plan)).items[0].status == "uploaded"
    assert len(list(engine.journal.root.glob("*.json"))) == 2


@pytest.mark.parametrize("mode", ["timeout-after-write", "unknown-import"])
def test_uncertain_upload_stops_and_never_retries_after_restart(tmp_path, mode):
    engine, accounts = engine_for(tmp_path, count=2)
    accounts.mode = mode
    plan = engine.preview(SyncSettings())
    result = engine.execute(plan.identity, selected(plan))
    assert result.stopped
    assert len(result.items) == 1
    assert result.items[0].status == "needs_review"
    assert len(accounts.posts) == 1
    restarted = SyncEngine(accounts, engine.workspace, legacy_directory=tmp_path / "legacy", clock=lambda: NOW)
    next_plan = restarted.preview(SyncSettings())
    assert next_plan.items[0].status == "needs_review"
    assert len(accounts.posts) == 1


def test_pending_record_survives_final_database_failure(tmp_path, monkeypatch):
    engine, accounts = engine_for(tmp_path)
    plan = engine.preview(SyncSettings())

    def fail(*args):
        raise SessionError("Synthetic disk failure.")

    monkeypatch.setattr(SyncState, "finish", fail)
    result = engine.execute(plan.identity, selected(plan))
    assert result.items[0].status == "needs_review"
    with SyncState(engine.workspace) as state:
        assert state.get(plan.scope, "ride-0")["status"] == "upload_pending"
    assert engine.preview(SyncSettings()).items[0].status == "needs_review"
    assert len(accounts.posts) == 1


def test_failed_intent_commit_prevents_post(tmp_path, monkeypatch):
    engine, accounts = engine_for(tmp_path)
    plan = engine.preview(SyncSettings())

    def fail(*args):
        raise SessionError("Synthetic disk failure.")

    monkeypatch.setattr(SyncState, "begin_upload", fail)
    result = engine.execute(plan.identity, selected(plan))
    assert result.items[0].status == "failed"
    assert not accounts.posts


def test_journal_failure_after_intent_prevents_post_but_keeps_blocking_state(tmp_path, monkeypatch):
    engine, accounts = engine_for(tmp_path)
    plan = engine.preview(SyncSettings())
    monkeypatch.setattr(engine.journal, "write", lambda *args, **kwargs: (_ for _ in ()).throw(SessionError("Synthetic disk failure.")))
    assert engine.execute(plan.identity, selected(plan)).items[0].status == "needs_review"
    assert not accounts.posts
    assert engine.preview(SyncSettings()).items[0].status == "needs_review"


def test_legacy_manual_upload_marker_is_respected_without_rewriting_it(tmp_path):
    engine, accounts = engine_for(tmp_path)
    digest = hashlib.sha256(accounts.files["ride-0"]).hexdigest()
    data = b'{"status":"needs_review"}'
    engine.journal.write(digest + ".json", data)
    plan = engine.preview(SyncSettings())
    assert plan.items[0].status == "needs_review"
    assert not accounts.posts
    assert engine.journal.read(digest + ".json", maximum=8192) == data


def test_existing_remote_candidate_is_not_uploaded_and_can_be_verified(tmp_path):
    engine, accounts = engine_for(tmp_path)
    start = accounts.rides[0].start_time.isoformat()
    accounts.remote = [GarminActivity("123", "Synthetic Existing", start, 60, 60)]
    plan = engine.preview(SyncSettings())
    assert plan.items[0].status == "possible_duplicate"
    assert engine.reconcile("ride-0", "123") == "ride-0"
    assert engine.preview(SyncSettings()).items[0].status == "uploaded"
    assert not accounts.posts


def test_delayed_confirmation_resolves_uncertain_result_without_another_post(tmp_path):
    engine, accounts = engine_for(tmp_path)
    accounts.mode = "timeout-after-write"
    plan = engine.preview(SyncSettings())
    engine.execute(plan.identity, selected(plan))
    engine.reconcile("ride-0", "101")
    assert engine.preview(SyncSettings()).items[0].status == "uploaded"
    assert len(accounts.posts) == 1


def test_changed_accounts_or_expired_preview_cannot_upload(tmp_path):
    engine, accounts = engine_for(tmp_path)
    plan = engine.preview(SyncSettings())
    accounts.identity = ("c" * 64, "b" * 64)
    with pytest.raises(SessionError):
        engine.execute(plan.identity, selected(plan))
    accounts.identity = ("a" * 64, "b" * 64)
    engine.clock = lambda: NOW + timedelta(minutes=16)
    with pytest.raises(SessionError):
        engine.execute(plan.identity, selected(plan))
    assert not accounts.posts


@pytest.mark.parametrize("ids", [[], ["unknown"], ["ride-0", "ride-0"]])
def test_only_unique_explicitly_selected_ready_ids_can_upload(tmp_path, ids):
    engine, accounts = engine_for(tmp_path)
    plan = engine.preview(SyncSettings())
    with pytest.raises(SessionError):
        engine.execute(plan.identity, ids)
    assert not accounts.posts


def test_consent_cannot_be_replayed(tmp_path):
    engine, accounts = engine_for(tmp_path)
    plan = engine.preview(SyncSettings())
    engine.execute(plan.identity, selected(plan))
    with pytest.raises(SessionError):
        engine.execute(plan.identity, selected(plan))
    assert len(accounts.posts) == 1


def test_cancellation_finishes_current_upload_and_stops_before_next(tmp_path):
    engine, accounts = engine_for(tmp_path, count=2)
    plan = engine.preview(SyncSettings())
    cancelled = False

    def progress(source_id, stage):
        nonlocal cancelled
        if stage == "Uploaded":
            cancelled = True

    result = engine.execute(plan.identity, selected(plan), cancel=lambda: cancelled, progress=progress)
    assert result.stopped
    assert len(accounts.posts) == 1
    assert len(result.items) == 1


def test_changed_prepared_file_is_rejected_before_network_write(tmp_path):
    engine, accounts = engine_for(tmp_path)
    plan = engine.preview(SyncSettings())
    with SyncState(engine.workspace) as state:
        row = state.get(plan.scope, "ride-0")
    engine.workspace.write(row["target_name"], b"modified", replace=True)
    with pytest.raises(Exception):
        engine.execute(plan.identity, selected(plan))
    assert not accounts.posts


def test_failed_fit_is_preserved_but_not_ready(tmp_path):
    engine, accounts = engine_for(tmp_path)
    accounts.files["ride-0"] = b"synthetic-invalid-fit"
    plan = engine.preview(SyncSettings())
    assert plan.items[0].status == "failed"
    assert len(list(engine.workspace.root.glob("*.original.fit"))) == 1
    assert not accounts.posts


def test_preview_scope_limits_rides_and_never_selects_whole_history(tmp_path):
    engine, accounts = engine_for(tmp_path, count=3)
    plan = engine.preview(SyncSettings(limit=1))
    assert len(plan.items) == 1
    assert plan.items[0].source_id == "ride-2"
    assert plan.truncated
    assert accounts.downloads == ["ride-2"]


def test_process_crash_after_post_leaves_durable_pending_state(tmp_path):
    code = """
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from test_sync import engine_for, selected
from mywhoosh_garmin_sync.sync import SyncSettings
from mywhoosh_garmin_sync.sync_state import SyncState
engine, accounts = engine_for(Path(sys.argv[1]))
plan = engine.preview(SyncSettings())
def crash(*args):
    os._exit(42)
SyncState.finish = crash
engine.execute(plan.identity, selected(plan))
"""
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path), str(Path(__file__).parent)],
                            capture_output=True, timeout=15)
    assert result.returncode == 42, result.stderr
    engine, accounts = engine_for(tmp_path)
    plan = engine.preview(SyncSettings())
    assert plan.items[0].status == "needs_review"
    assert not accounts.posts


def test_corrupt_database_is_preserved_instead_of_reset(tmp_path):
    engine, accounts = engine_for(tmp_path)
    engine.workspace.write("sync.sqlite3", b"synthetic-corrupt-database")
    with pytest.raises(SessionError):
        engine.preview(SyncSettings())
    assert engine.workspace.read("sync.sqlite3", maximum=100) == b"synthetic-corrupt-database"
    assert not accounts.posts


def test_uncertain_older_upload_remains_visible_when_source_disappears_from_list(tmp_path):
    engine, accounts = engine_for(tmp_path)
    accounts.mode = "timeout-after-write"
    plan = engine.preview(SyncSettings())
    engine.execute(plan.identity, selected(plan))
    accounts.rides.clear()
    engine.clock = lambda: NOW + timedelta(days=30)
    next_plan = engine.preview(SyncSettings(days=1))
    assert next_plan.review_count == 1
    assert next_plan.items[0].source_id == "ride-0"
    assert next_plan.items[0].status == "needs_review"
    engine.reconcile("ride-0", "101")
    assert len(accounts.posts) == 1


def test_new_remote_duplicate_between_preview_and_confirmation_blocks_post(tmp_path):
    engine, accounts = engine_for(tmp_path)
    plan = engine.preview(SyncSettings())
    accounts.remote = [GarminActivity("123", "Synthetic Existing", accounts.rides[0].start_time.isoformat(), 60, 60)]
    result = engine.execute(plan.identity, selected(plan))
    assert result.stopped
    assert not accounts.posts
    assert engine.preview(SyncSettings()).items[0].status == "possible_duplicate"
