import json

import pytest

from mywhoosh_garmin_sync.errors import SessionError
from mywhoosh_garmin_sync.sessions import SavedSession


@pytest.mark.parametrize("provider", ["mywhoosh", "garmin"])
def test_session_round_trip_hides_secrets_in_repr(provider):
    session = SavedSession(provider, "synthetic-access-token")
    assert SavedSession.deserialize(session.serialize(), provider) == session
    assert "synthetic-access-token" not in repr(session)
    assert "password" not in session.serialize().decode()


@pytest.mark.parametrize("overrides", [
    {"provider": "unknown"}, {"access_token": ""}, {"access_token": "line\nbreak"},
    {"access_token": "non-ascii-ä"}, {"access_token": "a" * 16385},
    {"refresh_token": True}, {"client_id": {}}, {"whoosh_id": 123},
    {"provider": "garmin", "whoosh_id": "another-provider"},
    {"refresh_token": "unsupported-mywhoosh-refresh"},
])
def test_invalid_session_rejected_without_echo(overrides):
    with pytest.raises(SessionError):
        SavedSession(**{"provider": "mywhoosh", "access_token": "synthetic-token", **overrides})


@pytest.mark.parametrize("mutation", [
    lambda raw: raw.update(password="synthetic-password"),
    lambda raw: raw.update(version=True),
    lambda raw: raw.update(provider="garmin"),
    lambda raw: raw.pop("whoosh_id"),
])
def test_stored_schema_cannot_silently_accept_unknown_fields_or_wrong_account(mutation):
    raw = json.loads(SavedSession("mywhoosh", "synthetic-token").serialize())
    mutation(raw)
    with pytest.raises(SessionError):
        SavedSession.deserialize(json.dumps(raw).encode(), "mywhoosh")


@pytest.mark.parametrize("data", [b"[]", b"\xff", b"{", b"x" * 32769],
                         ids=["array", "non-utf8", "invalid-json", "oversized"])
def test_malformed_storage_is_safe(data):
    with pytest.raises(SessionError) as error:
        SavedSession.deserialize(data, "garmin")
    assert "synthetic-token" not in str(error.value)


def test_legacy_record_without_account_key_remains_readable():
    raw = json.loads(SavedSession("garmin", "synthetic-token").serialize())
    del raw["account_key"]
    restored = SavedSession.deserialize(json.dumps(raw).encode(), "garmin")
    assert restored.account_key is None
