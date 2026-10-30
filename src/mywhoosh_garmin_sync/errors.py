"""Only fixed, secret-free messages should cross the CLI boundary."""


class SyncError(Exception):
    """Expected failure with a message safe to display."""

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code


class AuthError(SyncError):
    pass


class SessionError(SyncError):
    pass


class ProtocolError(SyncError):
    pass


class NetworkError(SyncError):
    pass


class RateLimitError(SyncError):
    pass


class FitError(SyncError):
    pass


class SyncCancelled(SyncError):
    def __init__(self):
        super().__init__("Sync stopped before the next upload.", code="cancelled")
