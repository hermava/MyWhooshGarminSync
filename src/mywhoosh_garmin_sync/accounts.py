"""Shared account sessions for the CLI, desktop and guarded sync service.

No password retention or automatic credential login. Frontends
must serialize operations (including sign-out) and close/discard this service on
exit. Storage is accessed only when explicitly supplied by the caller.
"""

from typing import Callable
from contextlib import contextmanager
from dataclasses import replace
import hashlib

from .auth import Session
from .errors import AuthError, SessionError
from .mywhoosh import MyWhooshClient
from .sessions import SavedSession, SessionStore, SignInRequired, check_provider


def _garmin_reader():
    from .garmin import GarminReader
    return GarminReader()


def _garmin_uploader():
    from .garmin import GarminUploader
    return GarminUploader()


def _account_key(provider, email):
    return hashlib.sha256(f"{provider}:{email.strip().casefold()}".encode()).hexdigest()


class AccountSessions:
    def __init__(self, *, store: SessionStore | None = None,
                 mywhoosh_factory: Callable = MyWhooshClient, garmin_factory: Callable = _garmin_reader,
                 uploader_factory: Callable = _garmin_uploader):
        self._store = store
        self._mywhoosh_factory = mywhoosh_factory
        self._garmin_factory = garmin_factory
        self._uploader_factory = uploader_factory
        self._current: dict[str, SavedSession] = {}
        self._remembered: set[str] = set()
        self._pending_save: set[str] = set()
        self._requires_sign_in: set[str] = set()

    def is_remembered(self, provider: str) -> bool:
        check_provider(provider)
        return provider in self._remembered and provider not in self._requires_sign_in

    def _load(self, provider):
        if provider in self._requires_sign_in:
            raise SignInRequired(provider)
        if provider not in self._current and self._store is not None:
            session = self._store.load(provider)
            if session is not None:
                self._current[provider] = session
                self._remembered.add(provider)
        if provider not in self._current:
            raise SignInRequired(provider)
        if provider in self._pending_save:
            self._persist(provider)
        return self._current[provider]

    def _persist(self, provider):
        self._pending_save.add(provider)
        self._store.save(self._current[provider])
        self._pending_save.discard(provider)

    def _remember(self, session, remember):
        self._current[session.provider] = session
        self._requires_sign_in.discard(session.provider)
        if remember:
            self._remembered.add(session.provider)
            self._persist(session.provider)
        else:
            self._remembered.discard(session.provider)
            if self._store is not None:
                self._store.delete(session.provider)

    def _update(self, session):
        previous = self._current.get(session.provider)
        if previous is not None and session.account_key is None:
            session = replace(session, account_key=previous.account_key)
        self._current[session.provider] = session
        if session.provider in self._remembered:
            self._persist(session.provider)

    def _before_sign_in(self, provider, remember):
        if type(remember) is not bool:
            raise SessionError("Stay signed in must be explicitly enabled or disabled.")
        if remember and self._store is None:
            raise SessionError("Protected session storage is not configured.")
        self._current.pop(provider, None)
        self._remembered.discard(provider)
        self._pending_save.discard(provider)
        self._requires_sign_in.add(provider)

    def sign_in_mywhoosh(self, email, password, *, remember=False, limit=10):
        self._before_sign_in("mywhoosh", remember)
        with self._mywhoosh_factory() as client:
            session = client.login(email, password)
            self._remember(SavedSession("mywhoosh", session.access_token, whoosh_id=session.whoosh_id,
                                        account_key=_account_key("mywhoosh", email)), remember)
            return client.activities(session, limit=limit)

    def mywhoosh_activities(self, *, limit=10):
        session = self._load("mywhoosh")
        with self._mywhoosh_factory() as client:
            try:
                return client.activities(Session(session.access_token, session.whoosh_id), limit=limit)
            except AuthError as exc:
                if exc.code == "http_401":
                    self._requires_sign_in.add("mywhoosh")
                    raise SignInRequired("mywhoosh") from None
                raise

    def sign_in_garmin(self, email, password, prompt_mfa, *, remember=False, limit=10):
        self._before_sign_in("garmin", remember)
        with self._garmin_factory() as reader:
            reader.login(email, password, prompt_mfa)
            self._remember(replace(reader.export_session(), account_key=_account_key("garmin", email)), remember)
            return reader.activities(limit=limit)

    def garmin_activities(self, *, limit=10):
        session = self._load("garmin")
        with self._garmin_factory() as reader:
            try:
                renewed = reader.restore_session(session)
                if renewed:
                    # Persist rotated refresh credentials before any further I/O.
                    self._update(reader.export_session())
                try:
                    return reader.activities(limit=limit)
                except AuthError as exc:
                    # Only replay this read, once, on an actual 401. Never refresh
                    # on a 403 challenge, rate limit, timeout or malformed reply.
                    if exc.code != "http_401" or renewed:
                        raise
                reader.refresh_session()
                self._update(reader.export_session())
                return reader.activities(limit=limit)
            except AuthError as exc:
                if exc.code in ("session_expired", "http_401"):
                    self._requires_sign_in.add("garmin")
                    raise SignInRequired("garmin") from None
                raise

    def sign_out(self, provider):
        check_provider(provider)
        self._current.pop(provider, None)
        self._remembered.discard(provider)
        self._pending_save.discard(provider)
        self._requires_sign_in.add(provider)
        if self._store is not None:
            self._store.delete(provider)

    def sync_identity(self):
        keys = []
        for provider in ("mywhoosh", "garmin"):
            session = self._load(provider)
            if session.account_key is None:
                raise SignInRequired(provider)  # Older snapshots need one fresh sign-in.
            keys.append(session.account_key)
        return tuple(keys)

    def download_fit(self, activity):
        session = self._load("mywhoosh")
        if not activity.activity_file_id:
            raise SessionError("This ride does not have a downloadable FIT file yet.")
        with self._mywhoosh_factory() as client:
            try:
                return client.download_fit(Session(session.access_token, session.whoosh_id), activity.activity_file_id)
            except AuthError as exc:
                if exc.code == "http_401":
                    self._requires_sign_in.add("mywhoosh")
                    raise SignInRequired("mywhoosh") from None
                raise

    @contextmanager
    def garmin_connection(self, *, upload=False):
        session = self._load("garmin")
        factory = self._uploader_factory if upload else self._garmin_factory
        with factory() as reader:
            try:
                if reader.restore_session(session):
                    self._update(reader.export_session())
                yield reader
            except AuthError as exc:
                if exc.code in ("session_expired", "http_401"):
                    self._requires_sign_in.add("garmin")
                    raise SignInRequired("garmin") from None
                raise
