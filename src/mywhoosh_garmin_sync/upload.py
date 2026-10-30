"""Small durable attempt journal for manual uploads, not a sync database."""

import json
import os
import re
import stat
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .errors import SessionError
from .garmin import UploadResult


class UploadJournal:
    def __init__(self, sha256: str, directory: Path = Path("private/garmin-uploads")):
        if not re.fullmatch(r"[a-f0-9]{64}", sha256):
            raise ValueError("Expected a SHA-256 hex digest")
        self.sha256 = sha256
        self.directory = Path(os.path.abspath(directory))
        self.path = self.directory / f"{sha256}.json"
        self.started = False

    def check_available(self) -> None:
        try:
            if any(p.is_symlink() for p in (self.directory, *self.directory.parents)):
                raise SessionError("Symlinks sind im Upload-Protokollpfad nicht zulässig.")
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = self.directory.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o700):
                raise SessionError("Upload-Protokollverzeichnis benötigt eigenen Besitzer und Rechte 0700.")
            if self.path.exists() or self.path.is_symlink():
                raise SessionError("Für diese FIT-Datei existiert bereits ein Upload-Vermerk. Nicht erneut senden; Garmin und lokalen Vermerk manuell prüfen.")
        except OSError:
            raise SessionError("Upload-Protokoll kann nicht sicher vorbereitet werden.") from None

    def _sync_directory(self) -> None:
        fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _payload(self, status: str, **extra) -> bytes:
        return json.dumps({"sha256": self.sha256, "status": status,
                           "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
                           **extra}, ensure_ascii=True).encode("ascii")

    def begin(self) -> None:
        self.check_available()
        try:
            # O_EXCL arbitrates concurrent processes and refuses even empty or
            # malformed existing records. A crash leaves a blocking marker.
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            self.started = True
            with os.fdopen(fd, "wb") as handle:
                handle.write(self._payload("upload_pending"))
                handle.flush()
                os.fsync(handle.fileno())
            self._sync_directory()
        except OSError:
            raise SessionError("Upload-Vermerk konnte nicht exklusiv gesichert werden; kein Upload ausgeführt.") from None

    def finish(self, result: UploadResult) -> None:
        if not self.started:
            raise SessionError("Upload-Vermerk wurde noch nicht angelegt.")
        temporary = None
        try:
            fd, name = tempfile.mkstemp(prefix=".upload-", dir=self.directory)
            temporary = Path(name)
            with os.fdopen(fd, "wb") as handle:
                handle.write(self._payload(**asdict(result)))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            self._sync_directory()
        except OSError:
            raise SessionError("Upload-Ergebnis konnte nicht lokal gesichert werden. Nicht erneut senden; Garmin und lokalen Vermerk prüfen.") from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
