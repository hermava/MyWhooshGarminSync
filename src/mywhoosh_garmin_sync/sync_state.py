"""Durable per-account activity state and pre-send upload intents."""

import os
import sqlite3
import json

from .errors import SessionError


class SyncState:
    def __init__(self, workspace):
        path = workspace.check_file("sync.sqlite3")
        for suffix in ("-journal", "-wal", "-shm"):
            workspace.check_file("sync.sqlite3" + suffix)
        if not path.exists():
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        try:
            self.db = sqlite3.connect(path, timeout=5)
            self.db.row_factory = sqlite3.Row
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, 4, 5):
                raise SessionError("Sync database version is not supported.")
            if version == 0 and self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone():
                raise SessionError("The existing database is not a sync database.")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA journal_mode=DELETE")
            self.db.execute("PRAGMA trusted_schema=OFF")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS rides (
                    scope TEXT NOT NULL, source_id TEXT NOT NULL, status TEXT NOT NULL,
                    original_name TEXT, original_hash TEXT, target_name TEXT, target_hash TEXT,
                    garmin_id TEXT, reason TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '', start_utc TEXT,
                    time_status TEXT NOT NULL DEFAULT '', time_detail TEXT NOT NULL DEFAULT '',
                    time_approval TEXT NOT NULL DEFAULT '',
                    retry_authorization TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(scope, source_id)
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    target_account TEXT NOT NULL, file_hash TEXT NOT NULL, status TEXT NOT NULL,
                    scope TEXT NOT NULL, source_id TEXT NOT NULL, garmin_id TEXT,
                    PRIMARY KEY(target_account, file_hash)
                );
            """)
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                columns = {row[1] for row in self.db.execute("PRAGMA table_info(rides)")}
                if "title" not in columns:
                    self.db.execute("ALTER TABLE rides ADD COLUMN title TEXT NOT NULL DEFAULT ''")
                if "start_utc" not in columns:
                    self.db.execute("ALTER TABLE rides ADD COLUMN start_utc TEXT")
                for name in ("time_status", "time_detail", "time_approval", "retry_authorization"):
                    if name not in columns:
                        self.db.execute(f"ALTER TABLE rides ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
                self.db.execute("PRAGMA user_version=5")
            if self.db.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
                raise SessionError("Sync database is damaged. Keep it for review; do not reset it to retry uploads.")
        except (sqlite3.Error, SessionError):
            if hasattr(self, "db"):
                self.db.close()
            raise SessionError("Sync database could not be opened safely. Keep existing data for review.") from None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def get(self, scope, source_id):
        result = self.db.execute("SELECT * FROM rides WHERE scope=? AND source_id=?", (scope, source_id)).fetchone()
        return dict(result) if result else None

    def update(self, scope, source_id, status, **values):
        allowed = {"original_name", "original_hash", "target_name", "target_hash", "garmin_id", "reason", "title", "start_utc",
                   "time_status", "time_detail", "time_approval", "retry_authorization"}
        if set(values) - allowed:
            raise ValueError("Unknown sync state field")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO rides(scope,source_id,status) VALUES(?,?,?)", (scope, source_id, status))
            columns = ["status", *values]
            self.db.execute("UPDATE rides SET " + ",".join(name + "=?" for name in columns)
                            + " WHERE scope=? AND source_id=?", (status, *values.values(), scope, source_id))
        return self.get(scope, source_id)

    def attempted(self, target, file_hash, *, scope=None, source_id=None, deleted_ids=()):
        rows = self.db.execute("SELECT * FROM attempts WHERE (target_account=? OR target_account LIKE ?) AND file_hash=?",
                               (target, target + ":%", file_hash))
        return any(not (row["scope"] == scope and row["source_id"] == source_id and row["status"] == "uploaded"
                        and row["garmin_id"] in deleted_ids) for row in rows)

    def reviews(self, scope, exclude):
        rows = self.db.execute("SELECT * FROM rides WHERE scope=? AND status IN ('upload_pending','needs_review','possible_duplicate') ORDER BY start_utc DESC", (scope,))
        result = []
        for row in rows:
            if row["source_id"] not in exclude:
                result.append(dict(row))
                if len(result) == 21:
                    break
        return result

    def begin_upload(self, scope, source_id, target, file_hash, *, retry_nonce=None):
        # Both records commit before POST. Any crash after this point is blocked.
        try:
            with self.db:
                row = self.get(scope, source_id)
                if not row or row["status"] != "ready" or row["target_hash"] != file_hash:
                    raise SessionError("The sync preview is no longer ready. Preview again.")
                authorization = None
                attempt_target = target
                if retry_nonce:
                    authorization = json.loads(row["retry_authorization"])
                    if (authorization["nonce"] != retry_nonce or authorization["used"] is not False
                            or authorization["target_account"] != target
                            or authorization["source_sha256"] != row["original_hash"]):
                        raise SessionError("The retry authorization is no longer valid.")
                    attempt_target += ":" + retry_nonce
                self.db.execute("INSERT INTO attempts VALUES(?,?,?,?,?,NULL)",
                                (attempt_target, file_hash, "upload_pending", scope, source_id))
                self.db.execute("UPDATE rides SET status='upload_pending' WHERE scope=? AND source_id=?", (scope, source_id))
                if authorization is not None:
                    authorization["used"] = True
                    self.db.execute("UPDATE rides SET retry_authorization=? WHERE scope=? AND source_id=?",
                                    (json.dumps(authorization), scope, source_id))
        except sqlite3.IntegrityError:
            raise SessionError("This file already has an upload attempt. Check Garmin before retrying.") from None

    def finish(self, scope, source_id, target, file_hash, result):
        with self.db:
            self.db.execute("UPDATE attempts SET status=?,garmin_id=? WHERE target_account=? AND file_hash=?",
                            (result.status, result.activity_id, target, file_hash))
            self.db.execute("UPDATE rides SET status=?,garmin_id=?,reason=? WHERE scope=? AND source_id=?",
                            (result.status, result.activity_id, result.reason, scope, source_id))
