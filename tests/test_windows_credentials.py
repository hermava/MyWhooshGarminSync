import ctypes
import json
import os
import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mywhoosh_garmin_sync.errors import SessionError
from mywhoosh_garmin_sync.sessions import SavedSession
from mywhoosh_garmin_sync.windows_credentials import (
    MAX_BLOB_BYTES, PREFIX, WindowsSessionStore, _Credential, _NativeCredentials,
)


class FakeCredentials:
    def __init__(self):
        self.data = {}
        self.fail_write = None
        self.writes = 0

    def read(self, name):
        return self.data.get(name)

    def write(self, name, data):
        self.writes += 1
        if self.writes == self.fail_write:
            raise SessionError("Synthetic storage failure.")
        assert 0 < len(data) <= MAX_BLOB_BYTES
        self.data[name] = data

    def delete(self, name):
        self.data.pop(name, None)

    def names(self, prefix):
        return tuple(name for name in self.data if name.startswith(prefix))


def test_store_supports_large_tokens_and_separate_providers_without_files(tmp_path):
    api = FakeCredentials()
    store = WindowsSessionStore(api=api)
    garmin = SavedSession("garmin", "a" * 12000, refresh_token="r" * 12000, client_id="client")
    whoosh = SavedSession("mywhoosh", "synthetic-token", whoosh_id="synthetic-rider")
    assert store.load("mywhoosh") is None
    store.save(garmin)
    store.save(whoosh)
    # A fresh instance simulates the next application run.
    restarted = WindowsSessionStore(api=api)
    assert restarted.load("garmin") == garmin
    assert restarted.load("mywhoosh") == whoosh
    assert len(api.data) > 3
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("failure_offset", [1, 2, 3])
def test_interrupted_replacement_preserves_previous_session(failure_offset):
    api = FakeCredentials()
    store = WindowsSessionStore(api=api)
    old = SavedSession("garmin", "synthetic-old")
    store.save(old)
    before = dict(api.data)
    api.fail_write = api.writes + failure_offset
    with pytest.raises(SessionError):
        store.save(SavedSession("garmin", "n" * 4000))
    assert store.load("garmin") == old
    assert api.data == before


def test_successful_replacement_removes_old_generation():
    api = FakeCredentials()
    store = WindowsSessionStore(api=api)
    store.save(SavedSession("garmin", "synthetic-old"))
    old_chunks = set(api.data) - {PREFIX + "garmin/current"}
    store.save(SavedSession("garmin", "synthetic-new"))
    assert not old_chunks.intersection(api.data)
    assert store.load("garmin").access_token == "synthetic-new"


@pytest.mark.parametrize("damage", ["manifest", "missing", "changed", "too-many", "path"])
def test_corrupt_records_do_not_become_new_empty_sessions(damage):
    api = FakeCredentials()
    store = WindowsSessionStore(api=api)
    store.save(SavedSession("garmin", "synthetic-token"))
    name = PREFIX + "garmin/current"
    manifest = json.loads(api.data[name])
    chunk = next(key for key in api.data if key != name)
    if damage == "manifest":
        api.data[name] = b"{"
    elif damage == "missing":
        del api.data[chunk]
    elif damage == "changed":
        api.data[chunk] = b"synthetic-other"
    else:
        manifest.update({"chunks": 100000} if damage == "too-many" else {"generation": "../garmin"})
        api.data[name] = json.dumps(manifest).encode()
    with pytest.raises(SessionError) as error:
        store.load("garmin")
    assert "synthetic-token" not in str(error.value)


def test_sign_out_removes_corrupt_and_unpublished_records_only_for_selected_provider():
    api = FakeCredentials()
    store = WindowsSessionStore(api=api)
    store.save(SavedSession("mywhoosh", "synthetic-whoosh"))
    api.data.update({PREFIX + "garmin/current": b"{", PREFIX + "garmin/orphan/0": b"secret",
                     "AnotherApp/session": b"unrelated"})
    store.delete("garmin")
    store.delete("garmin")
    assert not any(name.startswith(PREFIX + "garmin/") for name in api.data)
    assert store.load("mywhoosh").access_token == "synthetic-whoosh"
    assert api.data["AnotherApp/session"] == b"unrelated"


@pytest.mark.skipif(os.name == "nt", reason="Tests fail-closed behavior on non-Windows hosts")
def test_default_store_has_no_plaintext_fallback_on_linux():
    with pytest.raises(SessionError, match="Windows Credential Manager"):
        WindowsSessionStore()


def native_library():
    return SimpleNamespace(**{name: Mock(return_value=1) for name in (
        "CredReadW", "CredWriteW", "CredDeleteW", "CredEnumerateW", "CredFree")})


def test_native_write_uses_local_user_persistence_and_correct_binary_blob():
    lib = native_library()

    def write(pointer, flags):
        record = ctypes.cast(pointer, ctypes.POINTER(_Credential)).contents
        assert flags == 0
        assert record.Type == 1
        assert record.Persist == 2  # CRED_PERSIST_LOCAL_MACHINE, never enterprise roaming.
        assert record.TargetName == PREFIX + "garmin/test"
        assert ctypes.string_at(record.CredentialBlob, record.CredentialBlobSize) == b"a\x00b"
        return 1

    lib.CredWriteW.side_effect = write
    _NativeCredentials(library=lib, last_error=lambda: 0).write(PREFIX + "garmin/test", b"a\x00b")
    assert lib.CredWriteW.call_count == 1


@pytest.mark.parametrize("size", [3, 2561])
def test_native_read_frees_windows_memory_even_for_rejected_blob(size):
    lib = native_library()
    blob = (ctypes.c_ubyte * 3)(97, 0, 98)
    record = _Credential(CredentialBlobSize=size, CredentialBlob=blob)

    def read(name, kind, flags, output):
        assert (kind, flags) == (1, 0)
        ctypes.cast(output, ctypes.POINTER(ctypes.POINTER(_Credential)))[0] = ctypes.pointer(record)
        return 1

    lib.CredReadW.side_effect = read
    api = _NativeCredentials(library=lib, last_error=lambda: 0)
    if size == 3:
        assert api.read("test") == b"a\x00b"
    else:
        with pytest.raises(SessionError):
            api.read("test")
    assert lib.CredFree.call_count == 1


@pytest.mark.parametrize("error", [1168, 5, 1312])
def test_native_distinguishes_absence_from_access_failure(error):
    lib = native_library()
    lib.CredReadW.return_value = 0
    api = _NativeCredentials(library=lib, last_error=lambda: error)
    if error == 1168:
        assert api.read("test") is None
    else:
        with pytest.raises(SessionError):
            api.read("test")
    lib.CredFree.assert_not_called()


def test_native_enumeration_filters_names_and_frees_memory():
    lib = native_library()
    records = [_Credential(Type=1, TargetName=PREFIX + "garmin/test"),
               _Credential(Type=1, TargetName="AnotherApp/test")]
    array = (ctypes.POINTER(_Credential) * 2)(*(ctypes.pointer(item) for item in records))

    def enumerate_records(pattern, flags, count, output):
        assert pattern == PREFIX + "garmin/*"
        assert flags == 0
        ctypes.cast(count, ctypes.POINTER(ctypes.c_uint32))[0] = 2
        ctypes.cast(output, ctypes.POINTER(ctypes.POINTER(ctypes.POINTER(_Credential))))[0] = array
        return 1

    lib.CredEnumerateW.side_effect = enumerate_records
    api = _NativeCredentials(library=lib, last_error=lambda: 0)
    assert api.names(PREFIX + "garmin/") == (PREFIX + "garmin/test",)
    lib.CredFree.assert_called_once()


@pytest.mark.skipif(os.name != "nt", reason="Requires actual Windows Credential Manager")
def test_native_windows_round_trip_in_isolated_synthetic_namespace(monkeypatch):
    from mywhoosh_garmin_sync import windows_credentials

    # Never read, replace or delete a real app session, even on a developer PC.
    namespace = "MyWhooshGarminSync/test/" + uuid.uuid4().hex + "/"
    monkeypatch.setattr(windows_credentials, "PREFIX", namespace)
    store = WindowsSessionStore()
    try:
        assert store.load("garmin") is None
        session = SavedSession("garmin", "a" * 6000, refresh_token="synthetic-refresh", client_id="client")
        store.save(session)
        assert WindowsSessionStore().load("garmin") == session
        store.save(SavedSession("garmin", "synthetic-replacement"))
        assert store.load("garmin").access_token == "synthetic-replacement"
        store.delete("garmin")
        assert store.load("garmin") is None
    finally:
        store.delete("garmin")
