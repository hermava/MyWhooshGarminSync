"""Validated session records shared by storage, account services and frontends."""

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Protocol

from .errors import AuthError, SessionError

PROVIDERS = ("mywhoosh", "garmin")
MAX_SESSION_BYTES = 32_768


def check_provider(provider: str) -> None:
    if provider not in PROVIDERS:
        raise SessionError("Unsupported account provider.")


def _token(value: object, *, optional: bool = False) -> bool:
    return (optional and value is None) or (
        isinstance(value, str) and 0 < len(value) <= 16_384
        and value.isascii() and all(33 <= ord(char) < 127 for char in value)
    )


@dataclass(frozen=True)
class SavedSession:
    provider: str
    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)
    client_id: str | None = field(default=None, repr=False)
    whoosh_id: str | None = field(default=None, repr=False)
    account_key: str | None = field(default=None, repr=False)

    def __post_init__(self):
        check_provider(self.provider)
        if self.account_key is not None and (not isinstance(self.account_key, str)
                                            or not re.fullmatch(r"[a-f0-9]{64}", self.account_key)):
            raise SessionError("Session account key has an invalid format.")
        if not _token(self.access_token) or not _token(self.refresh_token, optional=True):
            raise SessionError("Session tokens have an invalid format.")
        if not _token(self.client_id, optional=True):
            raise SessionError("Session client identifier has an invalid format.")
        if self.whoosh_id is not None and (
            not isinstance(self.whoosh_id, str) or len(self.whoosh_id) > 1024
        ):
            raise SessionError("Session account identifier has an invalid format.")
        if self.provider == "mywhoosh" and (self.refresh_token is not None or self.client_id is not None):
            raise SessionError("Unsupported MyWhoosh refresh credentials.")
        if self.provider == "garmin" and self.whoosh_id is not None:
            raise SessionError("Session contains fields for a different provider.")

    def serialize(self) -> bytes:
        data = json.dumps({"version": 1, **asdict(self)}, ensure_ascii=True).encode("utf-8")
        if len(data) > MAX_SESSION_BYTES:
            raise SessionError("Session exceeds the storage size limit.")
        return data

    @classmethod
    def deserialize(cls, data: bytes, provider: str) -> "SavedSession":
        check_provider(provider)
        try:
            if len(data) > MAX_SESSION_BYTES:
                raise ValueError
            raw = json.loads(data)
            expected = {"version", "provider", "access_token", "refresh_token", "client_id", "whoosh_id"}
            if (not isinstance(raw, dict) or not expected.issubset(raw) or set(raw) - expected - {"account_key"}
                    or type(raw["version"]) is not int or raw["version"] != 1
                    or raw["provider"] != provider):
                raise ValueError
            return cls(**{key: value for key, value in raw.items() if key != "version"})
        except (ValueError, TypeError, RecursionError):
            raise SessionError("Saved session is invalid. Sign out locally before signing in again.") from None


class SessionStore(Protocol):
    def load(self, provider: str) -> SavedSession | None: ...
    def save(self, session: SavedSession) -> None: ...
    def delete(self, provider: str) -> None: ...


class SignInRequired(AuthError):
    def __init__(self, provider: str):
        check_provider(provider)
        super().__init__(f"Please sign in to {'MyWhoosh' if provider == 'mywhoosh' else 'Garmin'}.",
                         code="sign_in_required")
        self.provider = provider
