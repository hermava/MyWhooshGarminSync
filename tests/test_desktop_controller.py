import threading
import time

import pytest

from mywhoosh_garmin_sync.desktop_controller import DesktopController, MfaRequest
from mywhoosh_garmin_sync.errors import AuthError, NetworkError, ProtocolError, RateLimitError, SessionError
from mywhoosh_garmin_sync.sessions import SignInRequired


class Accounts:
    def __init__(self):
        self.calls = []
        self.remembered = False

    def sign_in_mywhoosh(self, email, password, *, remember):
        self.calls.append(("login-mywhoosh", email, password, remember))
        self.remembered = remember
        return "synthetic-activities"

    def sign_in_garmin(self, email, password, prompt_mfa, *, remember):
        code = prompt_mfa()
        self.calls.append(("login-garmin", email, password, code, remember))
        self.remembered = remember
        return ()

    def mywhoosh_activities(self):
        self.calls.append("read-mywhoosh")
        return "synthetic-activities"

    def garmin_activities(self):
        self.calls.append("read-garmin")
        return ()

    def is_remembered(self, provider):
        return self.remembered

    def sign_out(self, provider):
        self.calls.append(("logout", provider))
        self.remembered = False


def finished(controller):
    controller._worker.join(timeout=2)
    assert not controller._worker.is_alive(), "Worker did not finish"
    return controller.events()


def next_event(controller, kind):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        for event in controller.events():
            if event.kind == kind:
                return event
        time.sleep(0.001)
    pytest.fail(f"Missing event {kind}")


def test_login_routes_only_to_requested_provider_and_does_not_publish_credentials():
    accounts = Accounts()
    controller = DesktopController(accounts)
    assert controller.sign_in("mywhoosh", "example@example.invalid", "synthetic-password", remember=True)
    events = finished(controller)
    assert [event.kind for event in events] == ["busy", "done", "idle"]
    assert events[1].value == "synthetic-activities"
    assert events[1].remembered is True
    assert len(accounts.calls) == 1
    assert "synthetic-password" not in repr(events)
    assert "example@example.invalid" not in repr(events)


def test_mfa_waits_for_ui_response_and_uses_only_masked_request_object():
    accounts = Accounts()
    controller = DesktopController(accounts)
    assert controller.sign_in("garmin", "example@example.invalid", "synthetic-password", remember=False)
    event = next_event(controller, "mfa")
    assert controller.busy
    assert not event.value.submit("private-invalid-code")
    assert controller.busy
    assert event.value.submit("123456")
    events = finished(controller)
    assert accounts.calls[0][3] == "123456"
    assert "123456" not in repr(event)
    assert "123456" not in repr(event.value)
    assert event.value._code is None
    assert [event.kind for event in events] == ["done", "idle"]


def test_mfa_cancel_is_reported_as_cancel_without_sending_code():
    accounts = Accounts()
    controller = DesktopController(accounts)
    controller.sign_in("garmin", "example@example.invalid", "synthetic-password", remember=False)
    next_event(controller, "mfa").value.cancel()
    assert [event.kind for event in finished(controller)] == ["cancelled", "idle"]
    assert not accounts.calls


def test_closing_window_releases_mfa_waiter_and_suppresses_late_ui_updates():
    controller = DesktopController(Accounts())
    controller.sign_in("garmin", "example@example.invalid", "synthetic-password", remember=False)
    next_event(controller, "mfa")
    controller.close()
    assert finished(controller) == []
    assert not controller.read("mywhoosh")


def test_operation_is_on_worker_and_concurrent_requests_are_rejected():
    accounts = Accounts()
    entered, release = threading.Event(), threading.Event()
    thread_ids = []

    def read():
        thread_ids.append(threading.get_ident())
        entered.set()
        assert release.wait(2)
        return ()

    accounts.garmin_activities = read
    controller = DesktopController(accounts)
    try:
        assert controller.read("garmin")
        assert entered.wait(2)
        assert not controller.sign_out("garmin")
        assert not controller.sign_in("mywhoosh", "example@example.invalid", "synthetic-password", remember=False)
        assert thread_ids != [threading.get_ident()]
    finally:
        release.set()
    assert [event.kind for event in finished(controller)] == ["busy", "done", "idle"]


@pytest.mark.parametrize("failure,kind,phrase", [
    (SignInRequired("garmin"), "sign_in_required", None),
    (AuthError("synthetic-private-password"), "error", "account details"),
    (AuthError("synthetic-private-password", code="http_403"), "error", "denied access"),
    (NetworkError("synthetic-private-password"), "error", "connection"),
    (RateLimitError("synthetic-private-password"), "error", "limiting"),
    (ProtocolError("synthetic-private-password"), "error", "unexpected response"),
    (SessionError("synthetic-private-password"), "error", "saved sign-in"),
    (SessionError("synthetic-private-password", code="garmin_dependency_missing"), "error", "complete app build"),
    (RuntimeError("synthetic-private-password"), "error", "could not be completed"),
])
def test_errors_are_safe_english_events_with_sign_in_separate_from_network_failure(failure, kind, phrase):
    accounts = Accounts()

    def fail():
        raise failure

    accounts.garmin_activities = fail
    controller = DesktopController(accounts)
    controller.read("garmin")
    events = finished(controller)
    assert events[1].kind == kind
    if phrase:
        assert phrase in events[1].value
    assert "synthetic-private-password" not in str(events[1].value)
    assert events[-1].kind == "idle"


def test_local_sign_out_routes_to_one_provider():
    accounts = Accounts()
    accounts.remembered = True
    controller = DesktopController(accounts)
    controller.sign_out("garmin")
    events = finished(controller)
    assert accounts.calls == [("logout", "garmin")]
    assert events[1].remembered is False


def test_mfa_code_cannot_be_replaced_after_submission():
    request = MfaRequest()
    assert request.submit("123456")
    assert not request.submit("654321")
    assert request._code == "123456"


def test_sync_job_reports_progress_and_does_not_access_account_storage():
    from types import SimpleNamespace
    from mywhoosh_garmin_sync.sync import SyncSettings

    def preview(settings, *, cancel, progress):
        assert not cancel()
        progress("synthetic-id", "Validating")
        return "synthetic-plan"

    controller = DesktopController(object(), sync_engine=SimpleNamespace(preview=preview))
    assert controller.preview_sync(SyncSettings())
    events = finished(controller)
    assert [event.kind for event in events] == ["busy", "progress", "done", "idle"]
    assert events[2].value == "synthetic-plan"
    assert events[2].provider == "sync"


def test_sync_cancellation_requests_stop_without_closing_ui_or_killing_worker():
    from types import SimpleNamespace
    entered, release = threading.Event(), threading.Event()

    def execute(identity, selected, *, cancel, progress):
        entered.set()
        assert release.wait(2)
        assert cancel()
        return "synthetic-safe-stop"

    controller = DesktopController(object(), sync_engine=SimpleNamespace(execute=execute))
    try:
        controller.execute_sync("synthetic-plan", ("synthetic-id",))
        assert entered.wait(2)
        controller.cancel()
        assert not controller._closed.is_set()
    finally:
        release.set()
    assert finished(controller)[1].value == "synthetic-safe-stop"
