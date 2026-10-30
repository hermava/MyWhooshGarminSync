import json
import os
import stat

import pytest

from mywhoosh_garmin_sync import upload
from mywhoosh_garmin_sync.errors import SessionError
from mywhoosh_garmin_sync.garmin import UploadResult

HASH = "a" * 64


def test_pending_survives_process_lifetime_and_blocks_another_attempt(tmp_path):
    directory = tmp_path / "uploads"
    first = upload.UploadJournal(HASH, directory)
    first.check_available()
    second = upload.UploadJournal(HASH, directory)
    second.check_available()
    first.begin()
    pending = json.loads(first.path.read_text())
    assert pending["status"] == "upload_pending"
    assert pending["sha256"] == HASH
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(first.path.stat().st_mode) == 0o600
    with pytest.raises(SessionError, match="bereits ein Upload-Vermerk"):
        second.begin()
    assert json.loads(first.path.read_text()) == pending


@pytest.mark.parametrize("status,identity", [("uploaded", "123"), ("needs_review", None)])
def test_completed_records_remain_blocking_and_contain_no_account_secrets(tmp_path, status, identity):
    journal = upload.UploadJournal(HASH, tmp_path / "uploads")
    journal.begin()
    journal.finish(UploadResult(status, identity, "synthetic-result"))
    payload = json.loads(journal.path.read_text())
    assert payload["status"] == status
    assert payload["activity_id"] == identity
    assert set(payload) == {"status", "activity_id", "reason", "sha256", "recorded_at_utc"}
    assert stat.S_IMODE(journal.path.stat().st_mode) == 0o600
    with pytest.raises(SessionError):
        upload.UploadJournal(HASH, journal.directory).check_available()


@pytest.mark.parametrize("content", [b"", b"invalid-json"])
def test_incomplete_existing_marker_is_never_discarded(tmp_path, content):
    journal = upload.UploadJournal(HASH, tmp_path / "uploads")
    journal.check_available()
    journal.path.write_bytes(content)
    with pytest.raises(SessionError):
        journal.begin()
    assert journal.path.read_bytes() == content


def test_journal_denies_symlink_paths_and_shared_directories(tmp_path):
    directory = tmp_path / "real"
    directory.mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(directory, target_is_directory=True)
    with pytest.raises(SessionError, match="Symlinks"):
        upload.UploadJournal(HASH, linked / "uploads").check_available()
    journal = upload.UploadJournal(HASH, directory)
    journal.path.symlink_to(tmp_path / "missing")
    with pytest.raises(SessionError, match="bereits ein Upload-Vermerk"):
        journal.begin()
    directory.chmod(0o755)
    with pytest.raises(SessionError, match="0700"):
        journal.check_available()


def test_atomic_exclusive_create_arbitrates_race_after_precheck(tmp_path, monkeypatch):
    directory = tmp_path / "uploads"
    winner = upload.UploadJournal(HASH, directory)
    winner.check_available()
    loser = upload.UploadJournal(HASH, directory)
    monkeypatch.setattr(loser, "check_available", lambda: None)
    winner.begin()
    with pytest.raises(SessionError, match="exklusiv"):
        loser.begin()
    assert not loser.started
    assert json.loads(winner.path.read_text())["status"] == "upload_pending"


@pytest.mark.parametrize("failure", ["fsync", "replace"])
def test_disk_error_does_not_remove_pending_marker(tmp_path, monkeypatch, failure):
    journal = upload.UploadJournal(HASH, tmp_path / "uploads")
    journal.begin()

    def fail(*args):
        raise OSError("synthetic-private-path")

    monkeypatch.setattr(os, failure, fail)
    with pytest.raises(SessionError, match="Nicht erneut senden") as error:
        journal.finish(UploadResult("uploaded", "123", "synthetic-result"))
    assert "synthetic-private-path" not in str(error.value)
    assert json.loads(journal.path.read_text())["status"] == "upload_pending"
    assert list(journal.directory.iterdir()) == [journal.path]
