"""Serialized background account/sync operations and a cancellable MFA bridge."""

import queue
import re
import threading
from dataclasses import dataclass, field

from .errors import AuthError, NetworkError, ProtocolError, RateLimitError, SessionError, SyncCancelled, SyncError
from .sessions import SignInRequired, check_provider


class Cancelled(SyncCancelled):
    def __init__(self):
        SyncError.__init__(self, "Sign-in cancelled.", code="cancelled")


@dataclass
class MfaRequest:
    _ready: threading.Event = field(default_factory=threading.Event, repr=False)
    _code: str | None = field(default=None, repr=False)

    def submit(self, code: str) -> bool:
        if not re.fullmatch(r"[0-9]{6,8}", code.strip()) or self._ready.is_set():
            return False
        self._code = code.strip()
        self._ready.set()
        return True

    def cancel(self):
        self._code = None
        self._ready.set()


@dataclass(frozen=True)
class DesktopEvent:
    kind: str
    provider: str
    action: str
    value: object = field(default=None, repr=False)
    remembered: bool = False


def _error_message(error: Exception) -> str:
    # Provider/third-party messages may contain secrets or use a different language.
    if isinstance(error, RateLimitError):
        return "The service is temporarily limiting requests. Please try again later."
    if isinstance(error, NetworkError):
        return "Could not reach the service. Check your connection and try again."
    if isinstance(error, AuthError):
        if error.code == "http_403":
            return "The service denied access. Please try again later."
        return "Sign-in could not be confirmed. Check your account details or verification code."
    if isinstance(error, SessionError):
        if error.code == "retry_present":
            return "The previous activity is still present in Garmin. Delete it manually, then check again."
        if error.code == "retry_match":
            return "A matching activity is still listed in Garmin. Wait for deletion to finish and check again."
        if error.code == "retry_uncertain":
            return "An unclear or unrelated upload record still exists. It was kept and the ride was not reopened."
        if error.code in ("garmin_dependency_missing", "garmin_dependency_version"):
            return "Garmin support is missing or incompatible. Please use a complete app build."
        return "Could not access saved sign-in data. Try signing out, or sign in without staying signed in."
    if isinstance(error, ProtocolError):
        return "The service returned an unexpected response. Please try again later."
    return "The operation could not be completed. Please try again."


class DesktopController:
    """One worker operation at a time; UI widgets are only touched by the UI thread."""

    def __init__(self, accounts, *, sync_engine=None):
        self.accounts = accounts
        self.sync_engine = sync_engine
        self._events = queue.Queue()
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._busy = False
        self._worker: threading.Thread | None = None
        self._cancel_requested = threading.Event()

    @property
    def busy(self):
        with self._lock:
            return self._busy

    def _emit(self, event):
        if not self._closed.is_set():
            self._events.put(event)

    def _mfa(self):
        request = MfaRequest()
        self._emit(DesktopEvent("mfa", "garmin", "login", request))
        while not request._ready.wait(0.1):
            if self._closed.is_set() or self._cancel_requested.is_set():
                request.cancel()
        try:
            if self._closed.is_set() or self._cancel_requested.is_set() or request._code is None:
                raise Cancelled()
            return request._code
        finally:
            request._code = None

    def _start(self, provider, action, operation):
        if provider != "sync":
            check_provider(provider)
        with self._lock:
            if self._busy or self._closed.is_set():
                return False
            self._busy = True
            self._cancel_requested.clear()
        self._emit(DesktopEvent("busy", provider, action))

        def run():
            try:
                value = operation()
                self._emit(DesktopEvent("done", provider, action, value,
                                        remembered=False if provider == "sync" else self.accounts.is_remembered(provider)))
            except SignInRequired as error:
                self._emit(DesktopEvent("sign_in_required", provider, action, error.provider))
            except SyncCancelled:
                self._emit(DesktopEvent("cancelled", provider, action))
            except Exception as error:
                self._emit(DesktopEvent("error", provider, action, _error_message(error)))
            finally:
                with self._lock:
                    self._busy = False
                    self._emit(DesktopEvent("idle", provider, action))

        # The main window waits for this worker before releasing the data lock;
        # completed or uncertain writes are recorded before normal shutdown.
        self._worker = threading.Thread(target=run, name="account-preview", daemon=True)
        self._worker.start()
        return True

    def sign_in(self, provider, email, password, *, remember):
        def login():
            if self._closed.is_set() or self._cancel_requested.is_set():
                raise Cancelled()
            if provider == "mywhoosh":
                return self.accounts.sign_in_mywhoosh(email, password, remember=remember)
            return self.accounts.sign_in_garmin(email, password, self._mfa, remember=remember)

        return self._start(provider, "login", login)

    def read(self, provider):
        method = self.accounts.mywhoosh_activities if provider == "mywhoosh" else self.accounts.garmin_activities
        return self._start(provider, "read", method)

    def sign_out(self, provider):
        return self._start(provider, "logout", lambda: self.accounts.sign_out(provider))

    def events(self):
        result = []
        while True:
            try:
                result.append(self._events.get_nowait())
            except queue.Empty:
                return result

    def close(self):
        self._cancel_requested.set()
        self._closed.set()

    def cancel(self):
        self._cancel_requested.set()

    def wait(self):
        if self._worker is not None and self._worker is not threading.current_thread():
            self._worker.join()

    def preview_sync(self, settings):
        return self._start("sync", "preview", lambda: self.sync_engine.preview(
            settings, cancel=self._cancel_requested.is_set,
            progress=lambda identity, stage: self._emit(DesktopEvent("progress", "sync", "preview", (identity, stage)))))

    def execute_sync(self, identity, selected):
        return self._start("sync", "upload", lambda: self.sync_engine.execute(
            identity, tuple(selected), cancel=self._cancel_requested.is_set,
            progress=lambda source_id, stage: self._emit(DesktopEvent("progress", "sync", "upload", (source_id, stage)))))

    def retry_deleted(self, identity, source_id):
        return self._start("sync", "retry_deleted", lambda: self.sync_engine.authorize_retry(
            identity, source_id, cancel=self._cancel_requested.is_set,
            progress=lambda source_id, stage: self._emit(DesktopEvent("progress", "sync", "retry_deleted", (source_id, stage)))))

