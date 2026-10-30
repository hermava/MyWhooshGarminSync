import os
from pathlib import Path

import pytest

from mywhoosh_garmin_sync.auth import MAX_SESSION_BYTES, Session, TokenStore, default_state_dir
from mywhoosh_garmin_sync.errors import SessionError


def test_roundtrip_atomic_replacement_and_private_modes(tmp_path):
    store = TokenStore(tmp_path / "private")
    store.save(Session("synthetic-token", "synthetic-id"))
    original_inode = store.path.stat().st_ino
    assert store.load() == Session("synthetic-token", "synthetic-id")
    store.save(Session("replacement-token"))
    assert store.load().access_token == "replacement-token"
    assert store.path.stat().st_ino != original_inode
    assert store.directory.stat().st_mode & 0o777 == 0o700
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in store.directory.iterdir()] == ["session.json"]


def test_repr_does_not_disclose_secrets():
    assert "synthetic-token" not in repr(Session("synthetic-token", "private-id"))
    assert "private-id" not in repr(Session("synthetic-token", "private-id"))


def test_missing_session_does_not_create_directory(tmp_path):
    store = TokenStore(tmp_path / "absent")
    with pytest.raises(SessionError, match="Keine gespeicherte"):
        store.load()
    assert not store.directory.exists()


@pytest.mark.parametrize("target", ["directory", "file"])
def test_refuse_insecure_permissions(tmp_path, target):
    store = TokenStore(tmp_path / "private")
    store.save(Session("synthetic-token"))
    path = store.directory if target == "directory" else store.path
    path.chmod(0o755 if target == "directory" else 0o644)
    with pytest.raises(SessionError, match="0700/0600"):
        store.load()
    with pytest.raises(SessionError, match="0700/0600"):
        store.save(Session("replacement-token"))


@pytest.mark.parametrize("target", ["directory", "file", "parent"])
def test_refuse_symlinks(tmp_path, target):
    store = TokenStore(tmp_path / "real")
    store.save(Session("synthetic-token"))
    if target == "directory":
        link = tmp_path / "link"
        link.symlink_to(store.directory, target_is_directory=True)
        unsafe = TokenStore(link)
    elif target == "parent":
        link = tmp_path / "link"
        link.symlink_to(tmp_path, target_is_directory=True)
        unsafe = TokenStore(link / "real")
    else:
        real = store.directory / "real.json"
        store.path.rename(real)
        store.path.symlink_to(real)
        unsafe = store
    with pytest.raises(SessionError):
        unsafe.load()
    with pytest.raises(SessionError):
        unsafe.save(Session("replacement-token"))
    assert "replacement-token" not in (store.directory / ("real.json" if target == "file" else "session.json")).read_text()


def test_refuse_hardlink(tmp_path):
    store = TokenStore(tmp_path / "private")
    store.save(Session("synthetic-token"))
    os.link(store.path, tmp_path / "hardlink")
    with pytest.raises(SessionError):
        store.load()


@pytest.mark.parametrize("content", [b"not-json", b"\xff", b"[]", b"{}", b'{"version":1}',
                                         b"x" * (MAX_SESSION_BYTES + 1)],
                         ids=["invalid-json", "invalid-encoding", "list", "empty", "missing-token", "too-large"])
def test_invalid_session_files_fail_without_content(tmp_path, content):
    store = TokenStore(tmp_path / "private")
    store.save(Session("synthetic-token"))
    store.path.write_bytes(content)
    with pytest.raises(SessionError) as error:
        store.load()
    assert "not-json" not in str(error.value)


@pytest.mark.parametrize("token", [None, "", 1, "x\ny", "x y", "ümlaut", "a" * 16385])
def test_reject_invalid_tokens(token):
    with pytest.raises(SessionError):
        Session(token)


def test_refuse_store_in_project_or_git_tree(tmp_path):
    with pytest.raises(SessionError, match="Projektverzeichnis"):
        TokenStore(Path(__file__).resolve().parents[1] / "private").prepare()
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git/HEAD").write_text("ref: refs/heads/main\n")
    with pytest.raises(SessionError, match="Git-Arbeitsbäumen"):
        TokenStore(tmp_path / "private").prepare()
    assert not (tmp_path / "private").exists()


def test_failed_atomic_write_preserves_previous_session(tmp_path, monkeypatch):
    store = TokenStore(tmp_path / "private")
    store.save(Session("original-token"))

    def fail(*args):
        raise OSError("synthetic secret in external exception")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(SessionError) as error:
        store.save(Session("replacement-token"))
    assert "secret" not in str(error.value)
    assert store.load().access_token == "original-token"
    assert len(list(store.directory.iterdir())) == 1


def test_default_state_dir_requires_absolute_xdg(monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", "relative-path")
    assert default_state_dir() == Path.home() / ".local/state/mywhoosh-garmin-sync"
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/custom-state")
    assert default_state_dir() == Path("/tmp/custom-state/mywhoosh-garmin-sync")


def test_refuse_foreign_owner(tmp_path, monkeypatch):
    store = TokenStore(tmp_path / "private")
    store.save(Session("synthetic-token"))
    uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: uid + 1)
    with pytest.raises(SessionError, match="Besitzer"):
        store.load()
