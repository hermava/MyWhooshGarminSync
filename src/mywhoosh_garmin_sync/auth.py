"""Private, atomic session storage for the local POSIX user."""

import json
import os
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .errors import SessionError

MAX_SESSION_BYTES = 32_768


@dataclass(frozen=True)
class Session:
    access_token: str = field(repr=False)
    whoosh_id: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.access_token, str)
            or not self.access_token
            or len(self.access_token) > 16_384
            or not self.access_token.isascii()
            or any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in self.access_token)
            or (self.whoosh_id is not None and not isinstance(self.whoosh_id, str))
        ):
            raise SessionError("Sitzungsdaten fehlen oder haben ein ungültiges Format.")


def default_state_dir() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", ""))
    if not base.is_absolute():
        base = Path.home() / ".local" / "state"
    return base / "mywhoosh-garmin-sync"


class TokenStore:
    def __init__(self, directory: Path) -> None:
        # Keep the original path to detect symlinks before resolving anything.
        self.directory = Path(os.path.abspath(directory.expanduser()))
        self.path = self.directory / "session.json"

    def _validate_location(self) -> None:
        if os.name != "posix":
            raise SessionError("Die Tokenablage unterstützt zunächst nur POSIX-Systeme.")
        source_root = Path(__file__).resolve().parents[2]
        if (source_root / "pyproject.toml").is_file() and self.directory.is_relative_to(source_root):
            raise SessionError("Sitzungen müssen außerhalb des Projektverzeichnisses liegen.")
        for ancestor in (self.directory, *self.directory.parents):
            if ancestor.is_symlink():
                raise SessionError("Symlinks sind im Sitzungspfad nicht zulässig.")
            marker = ancestor / ".git"
            if marker.is_file() or (marker / "HEAD").is_file():
                raise SessionError("Sitzungen müssen außerhalb von Git-Arbeitsbäumen liegen.")

    @staticmethod
    def _check_private(info: os.stat_result, *, directory: bool) -> None:
        expected_mode = 0o700 if directory else 0o600
        correct_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if (
            not correct_type
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != expected_mode
            or (not directory and info.st_nlink != 1)
        ):
            raise SessionError("Sitzungsablage benötigt eigenen Besitzer und Rechte 0700/0600.")

    def load(self) -> Session:
        try:
            self._validate_location()
            self._check_private(self.directory.lstat(), directory=True)
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as handle:
                self._check_private(os.fstat(handle.fileno()), directory=False)
                data = handle.read(MAX_SESSION_BYTES + 1)
            if len(data) > MAX_SESSION_BYTES:
                raise SessionError("Sitzungsdatei überschreitet die erlaubte Größe.")
            payload = json.loads(data)
            if not isinstance(payload, dict) or payload.get("version") != 1:
                raise SessionError("Sitzungsdatei hat ein unbekanntes Format.")
            return Session(payload.get("access_token"), payload.get("whoosh_id"))
        except FileNotFoundError:
            raise SessionError("Keine gespeicherte Sitzung. Zuerst interaktiv anmelden.") from None
        except (OSError, ValueError, TypeError):
            raise SessionError("Sitzungsdatei kann nicht sicher gelesen werden.") from None

    def prepare(self) -> None:
        """Check storage before interactive login or transmitting credentials."""
        try:
            self._validate_location()
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._check_private(self.directory.lstat(), directory=True)
            if self.path.exists() or self.path.is_symlink():
                self._check_private(self.path.lstat(), directory=False)
        except OSError:
            raise SessionError("Sitzungsablage kann nicht sicher vorbereitet werden.") from None

    def save(self, session: Session) -> None:
        temporary = None
        try:
            self.prepare()
            data = json.dumps({
                "version": 1,
                "access_token": session.access_token,
                "whoosh_id": session.whoosh_id,
            }).encode("utf-8")
            if len(data) > MAX_SESSION_BYTES:
                raise SessionError("Sitzungsdaten überschreiten die erlaubte Größe.")
            fd, name = tempfile.mkstemp(prefix=".session-", dir=self.directory)
            temporary = Path(name)
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError:
            raise SessionError("Sitzungsdatei kann nicht sicher gespeichert werden.") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
