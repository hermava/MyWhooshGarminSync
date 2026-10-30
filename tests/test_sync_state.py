import sqlite3
from contextlib import closing

import pytest

from mywhoosh_garmin_sync.errors import SessionError
from mywhoosh_garmin_sync.sync_state import SyncState
from mywhoosh_garmin_sync.workspace import Workspace


@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_old_pending_record_migrates_without_losing_blocking_intent(tmp_path, version):
    workspace = Workspace(tmp_path / "data")
    workspace.write("sync.sqlite3", b"")
    with closing(sqlite3.connect(workspace.path("sync.sqlite3"))) as db:
        db.executescript("""
            CREATE TABLE rides(scope TEXT,source_id TEXT,status TEXT,original_name TEXT,
                original_hash TEXT,target_name TEXT,target_hash TEXT,garmin_id TEXT,
                reason TEXT DEFAULT '',PRIMARY KEY(scope,source_id));
            CREATE TABLE attempts(target_account TEXT,file_hash TEXT,status TEXT,
                scope TEXT,source_id TEXT,garmin_id TEXT,PRIMARY KEY(target_account,file_hash));
            INSERT INTO rides(scope,source_id,status) VALUES('scope','synthetic-id','upload_pending');
            INSERT INTO attempts VALUES('target','hash','upload_pending','scope','synthetic-id',NULL);
            PRAGMA user_version=1;
        """)
        if version >= 2:
            db.execute("ALTER TABLE rides ADD COLUMN title TEXT NOT NULL DEFAULT ''")
            db.execute("ALTER TABLE rides ADD COLUMN start_utc TEXT")
            db.execute("PRAGMA user_version=2")
        if version >= 3:
            db.execute("ALTER TABLE rides ADD COLUMN time_status TEXT NOT NULL DEFAULT ''")
            db.execute("ALTER TABLE rides ADD COLUMN time_detail TEXT NOT NULL DEFAULT ''")
            db.execute("PRAGMA user_version=3")
        if version >= 4:
            db.execute("ALTER TABLE rides ADD COLUMN time_approval TEXT NOT NULL DEFAULT ''")
            db.execute("PRAGMA user_version=4")
        db.commit()
    with SyncState(workspace) as state:
        assert state.db.execute("PRAGMA user_version").fetchone()[0] == 5
        assert state.get("scope", "synthetic-id")["status"] == "upload_pending"
        assert state.attempted("target", "hash")
        assert state.reviews("scope", set())[0]["title"] == ""
        assert state.get("scope", "synthetic-id")["time_status"] == ""
        assert state.get("scope", "synthetic-id")["time_approval"] == ""
        assert state.get("scope", "synthetic-id")["retry_authorization"] == ""


def test_unknown_schema_version_is_not_reset_or_modified(tmp_path):
    workspace = Workspace(tmp_path / "data")
    workspace.write("sync.sqlite3", b"")
    with closing(sqlite3.connect(workspace.path("sync.sqlite3"))) as db:
        db.execute("PRAGMA user_version=99")
    before = workspace.read("sync.sqlite3", maximum=100000)
    with pytest.raises(SessionError):
        SyncState(workspace)
    assert workspace.read("sync.sqlite3", maximum=100000) == before
