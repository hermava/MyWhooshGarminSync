from contextlib import nullcontext
from dataclasses import replace

import httpx
import pytest

from mywhoosh_garmin_sync.accounts import AccountSessions
from mywhoosh_garmin_sync.errors import AuthError, NetworkError, ProtocolError, RateLimitError, SessionError
from mywhoosh_garmin_sync.mywhoosh import MyWhooshClient
from mywhoosh_garmin_sync.sessions import SavedSession, SignInRequired


class Store:
    def __init__(self, events=None):
        self.data = {}
        self.events = events if events is not None else []
        self.fail_save = False

    def load(self, provider):
        self.events.append("load-" + provider)
        return self.data.get(provider)

    def save(self, session):
        self.events.append("save-" + session.provider)
        if self.fail_save:
            raise SessionError("Synthetic vault unavailable.")
        self.data[session.provider] = session

    def delete(self, provider):
        self.events.append("delete-" + provider)
        self.data.pop(provider, None)


class Reader:
    def __init__(self, events=None, *, renewed=False, errors=(), refresh_error=None):
        self.events = events if events is not None else []
        self.renewed = renewed
        self.errors = iter(errors)
        self.refresh_error = refresh_error
        self.state = SavedSession("garmin", "synthetic-access", refresh_token="synthetic-refresh", client_id="client")

    def login(self, email, password, prompt_mfa):
        assert email == "example@example.invalid"
        assert password == "synthetic-password"
        assert prompt_mfa() == "123456"
        self.events.append("login")

    def restore_session(self, session):
        self.state = session
        self.events.append("restore")
        if self.renewed:
            self.refresh_session()
        return self.renewed

    def refresh_session(self):
        self.events.append("refresh")
        if self.refresh_error:
            raise self.refresh_error
        self.state = SavedSession("garmin", "synthetic-new-access", refresh_token="synthetic-rotated", client_id="client")

    def export_session(self):
        return self.state

    def activities(self, *, limit):
        assert limit == 10
        self.events.append("read")
        error = next(self.errors, None)
        if error:
            raise error
        return ()


def remembered_reader(*, renewed=False, errors=(), refresh_error=None):
    events = []
    store = Store(events)
    reader = Reader(events, renewed=renewed, errors=errors, refresh_error=refresh_error)
    store.data["garmin"] = reader.state
    service = AccountSessions(store=store, garmin_factory=lambda: nullcontext(reader))
    return service, reader, store, events


def test_garmin_restart_uses_saved_session_and_never_requests_credentials():
    service, reader, store, events = remembered_reader()
    assert service.garmin_activities() == ()
    assert events == ["load-garmin", "restore", "read"]


def test_401_refreshes_once_and_persists_rotation_before_replaying_read():
    service, reader, store, events = remembered_reader(errors=[AuthError("Expired", code="http_401")])
    assert service.garmin_activities() == ()
    assert events == ["load-garmin", "restore", "read", "refresh", "save-garmin", "read"]
    assert store.data["garmin"].refresh_token == "synthetic-rotated"


def test_proactive_refresh_is_saved_before_first_api_read():
    service, reader, store, events = remembered_reader(renewed=True)
    service.garmin_activities()
    assert events == ["load-garmin", "restore", "refresh", "save-garmin", "read"]


@pytest.mark.parametrize("renewed", [False, True])
def test_repeated_401_requires_login_without_refresh_loop(renewed):
    errors = [AuthError("Expired", code="http_401")] * 2
    service, reader, store, events = remembered_reader(renewed=renewed, errors=errors)
    with pytest.raises(SignInRequired) as error:
        service.garmin_activities()
    assert error.value.provider == "garmin"
    assert events.count("refresh") == 1
    assert events.count("read") == (1 if renewed else 2)
    assert "delete-garmin" not in events


@pytest.mark.parametrize("failure", [AuthError("Denied", code="http_403"), RateLimitError("Rate limited"),
                                     NetworkError("Offline"), ProtocolError("Unknown response")])
def test_non_expiry_errors_do_not_refresh_or_remove_saved_session(failure):
    service, reader, store, events = remembered_reader(errors=[failure])
    with pytest.raises(type(failure)) as error:
        service.garmin_activities()
    assert error.value is failure
    assert events == ["load-garmin", "restore", "read"]


def test_expired_refresh_token_requires_login_without_deleting_state():
    service, reader, store, events = remembered_reader(
        errors=[AuthError("Expired", code="http_401")],
        refresh_error=AuthError("Expired refresh", code="session_expired"))
    with pytest.raises(SignInRequired):
        service.garmin_activities()
    assert "garmin" in store.data
    assert events.count("read") == 1


def test_failed_persistence_stops_read_after_refresh():
    service, reader, store, events = remembered_reader(renewed=True)
    store.fail_save = True
    with pytest.raises(SessionError):
        service.garmin_activities()
    assert "read" not in events
    # Keep the newly rotated credentials in memory instead of reverting to stale ones.
    assert service._current["garmin"].refresh_token == "synthetic-rotated"


def test_retry_after_storage_recovery_saves_rotated_token_before_any_network():
    service, reader, store, events = remembered_reader(renewed=True)
    store.fail_save = True
    with pytest.raises(SessionError):
        service.garmin_activities()
    events.clear()
    # Persistence still fails: no restore, refresh or read may happen.
    with pytest.raises(SessionError):
        service.garmin_activities()
    assert events == ["save-garmin"]
    events.clear()
    store.fail_save = False
    reader.renewed = False
    service.garmin_activities()
    assert events == ["save-garmin", "restore", "read"]
    assert store.data["garmin"].refresh_token == "synthetic-rotated"


def test_both_providers_missing_sessions_require_login_without_network():
    service = AccountSessions(store=Store())
    for provider, call in (("mywhoosh", service.mywhoosh_activities), ("garmin", service.garmin_activities)):
        with pytest.raises(SignInRequired) as error:
            call()
        assert error.value.provider == provider


def test_garmin_login_mfa_persists_only_session_then_supports_next_run():
    reader = Reader()
    store = Store()
    factory = lambda: nullcontext(reader)
    service = AccountSessions(store=store, garmin_factory=factory)
    service.sign_in_garmin("example@example.invalid", "synthetic-password", lambda: "123456", remember=True)
    assert store.data["garmin"].access_token == reader.state.access_token
    assert store.data["garmin"].account_key is not None
    service = AccountSessions(store=store, garmin_factory=factory)
    service.garmin_activities()
    assert reader.events.count("login") == 1
    assert "synthetic-password" not in repr(store.data)


def test_remember_false_is_memory_only_and_can_remove_previous_remembered_account():
    service, reader, store, events = remembered_reader()
    service.sign_in_garmin("example@example.invalid", "synthetic-password", lambda: "123456", remember=False)
    assert "garmin" not in store.data
    service.garmin_activities()
    assert "load-garmin" not in events
    assert "save-garmin" not in events


def test_remember_requires_protected_store_before_login():
    service = AccountSessions()
    with pytest.raises(SessionError, match="Protected"):
        service.sign_in_mywhoosh("example@example.invalid", "synthetic-password", remember=True)


def test_mywhoosh_saved_login_and_restart_are_read_only_and_do_not_save_password():
    calls = []

    def handle(request):
        calls.append(request)
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"Success": True, "AccessToken": "synthetic-access", "WhooshId": "rider"})
        assert request.headers["Authorization"] == "Bearer synthetic-access"
        return httpx.Response(200, json={"data": {"results": [], "totalPages": 0}})

    factory = lambda: MyWhooshClient(transport=httpx.MockTransport(handle))
    store = Store()
    service = AccountSessions(store=store, mywhoosh_factory=factory)
    assert not service.sign_in_mywhoosh("example@example.invalid", "synthetic-password", remember=True).activities
    restarted = AccountSessions(store=store, mywhoosh_factory=factory)
    assert not restarted.mywhoosh_activities().activities
    assert len(calls) == 3
    assert store.data["mywhoosh"].whoosh_id == "rider"
    assert b"synthetic-password" not in store.data["mywhoosh"].serialize()


@pytest.mark.parametrize("status,expected", [(401, SignInRequired), (403, AuthError), (429, RateLimitError), (500, NetworkError)])
def test_mywhoosh_errors_do_not_invent_a_refresh_endpoint(status, expected):
    store = Store()
    store.data["mywhoosh"] = SavedSession("mywhoosh", "synthetic-token")
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status)

    service = AccountSessions(store=store, mywhoosh_factory=lambda: MyWhooshClient(transport=httpx.MockTransport(handle)))
    with pytest.raises(expected):
        service.mywhoosh_activities()
    assert len(calls) == 1
    assert "mywhoosh" in store.data


def test_sign_out_forgets_memory_and_only_selected_provider():
    service, reader, store, events = remembered_reader()
    store.data["mywhoosh"] = SavedSession("mywhoosh", "synthetic-whoosh")
    service.garmin_activities()
    service.sign_out("garmin")
    with pytest.raises(SignInRequired):
        service.garmin_activities()
    assert "mywhoosh" in store.data


def test_failed_sign_in_cannot_silently_reload_a_previously_saved_account():
    service, reader, store, events = remembered_reader()

    def rejected(*args):
        raise AuthError("Synthetic rejected login.")

    reader.login = rejected
    with pytest.raises(AuthError):
        service.sign_in_garmin("other@example.invalid", "synthetic-password", lambda: "123456")
    with pytest.raises(SignInRequired):
        service.garmin_activities()
    assert "read" not in events
    assert "load-garmin" not in events
    assert "garmin" in store.data


def test_required_sign_in_does_not_keep_sending_an_expired_mywhoosh_token():
    store = Store()
    store.data["mywhoosh"] = SavedSession("mywhoosh", "synthetic-token")
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(401)

    service = AccountSessions(store=store, mywhoosh_factory=lambda: MyWhooshClient(transport=httpx.MockTransport(handle)))
    for _ in range(2):
        with pytest.raises(SignInRequired):
            service.mywhoosh_activities()
    assert len(calls) == 1


def test_old_saved_sessions_can_read_but_sync_requires_fresh_account_identification():
    service, reader, store, events = remembered_reader()
    store.data["mywhoosh"] = SavedSession("mywhoosh", "synthetic-token")
    assert service.garmin_activities() == ()
    with pytest.raises(SignInRequired):
        service.sync_identity()


def test_refresh_preserves_account_namespace_and_never_persists_email():
    service, reader, store, events = remembered_reader(renewed=True)
    store.data["garmin"] = replace(store.data["garmin"], account_key="b" * 64)
    store.data["mywhoosh"] = SavedSession("mywhoosh", "synthetic-token", account_key="a" * 64)
    service.garmin_activities()
    assert service.sync_identity() == ("a" * 64, "b" * 64)
    assert store.data["garmin"].account_key == "b" * 64
