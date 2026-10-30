"""Read-only FIT validation and private storage of unmodified originals."""

import hashlib
import io
import math
import os
import re
import stat
import struct
import tempfile
import time
import warnings
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import fitdecode

from .errors import FitError

MAX_FIT_BYTES = 16 * 1024 * 1024
MAX_FIT_FRAMES = 1_000_000
VALIDATION_SECONDS = 30.0
MAX_FIT_SESSIONS = 128


@dataclass(frozen=True)
class SessionTiming:
    start_utc: str | None
    timestamp_utc: str | None
    elapsed_seconds: float | None
    timer_seconds: float | None
    elapsed_end_utc: str | None


@dataclass(frozen=True)
class FitReport:
    valid: bool
    size_bytes: int
    sha256: str
    records: int
    sessions: int
    first_record_utc: str | None
    last_record_utc: str | None
    issues: tuple[str, ...]
    warnings: tuple[str, ...]
    integrity_ok: bool
    session_times: tuple[SessionTiming, ...]
    last_record_offset_seconds: float | None


def prepare_original_path(directory: Path, activity_id: str) -> Path:
    """Prepare a private directory, refusing an existing original before login."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", activity_id):
        raise FitError("Aktivitäts-ID enthält ungültige Zeichen.")
    return prepare_fit_target(directory / f"{activity_id}.original.fit")


def prepare_fit_target(target: Path) -> Path:
    """Prepare an exclusive private destination for an original or derived FIT."""
    target = Path(os.path.abspath(target.expanduser()))
    directory = target.parent
    try:
        if os.name != "posix":
            raise FitError("Private FIT-Ablage unterstützt zunächst nur POSIX-Systeme.")
        if any(p.is_symlink() for p in (directory, *directory.parents)):
            raise FitError("Symlinks sind im FIT-Ausgabepfad nicht zulässig.")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise FitError("FIT-Ausgabeverzeichnis benötigt eigenen Besitzer und Rechte 0700.")
        if target.exists() or target.is_symlink():
            raise FitError("FIT-Zieldatei existiert bereits. Mit validate prüfen oder ein anderes Ziel wählen.")
    except OSError:
        raise FitError("FIT-Ausgabepfad kann nicht sicher vorbereitet werden.") from None
    return target


def save_original(data: bytes, target: Path) -> None:
    """Publish complete bytes atomically using a link that never replaces a file."""
    if not data or len(data) > MAX_FIT_BYTES:
        raise FitError("Original ist leer oder überschreitet die erlaubte Dateigröße.")
    temporary = None
    try:
        fd, name = tempfile.mkstemp(prefix=".fit-", dir=target.parent)
        temporary = Path(name)
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, target)
    except FileExistsError:
        raise FitError("Original existiert bereits und wurde nicht überschrieben.") from None
    except OSError:
        raise FitError("Original kann nicht sicher gespeichert werden.") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def read_fit(path: Path) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FIT_BYTES:
                raise FitError("FIT-Eingabe ist keine reguläre Datei oder überschreitet das Größenlimit.")
            data = handle.read(MAX_FIT_BYTES + 1)
        if len(data) > MAX_FIT_BYTES:
            raise FitError("FIT-Eingabe überschreitet das Größenlimit.")
        return data
    except OSError:
        raise FitError("FIT-Datei kann nicht sicher gelesen werden.") from None


def validate_fit(data: bytes, *, expected_start: datetime | None = None) -> FitReport:
    """Validate one activity file; emit no raw fields and never repair bytes."""
    issues: list[str] = []
    notices: list[str] = []
    counts: Counter = Counter()
    first = last = previous = None
    session_starts: list[datetime] = []
    session_ends: list[datetime] = []
    session_times: list[SessionTiming] = []
    integrity_ok = False
    deadline = time.monotonic() + VALIDATION_SECONDS
    latest = datetime.now(timezone.utc) + timedelta(days=1)
    earliest = datetime(1989, 12, 31, tzinfo=timezone.utc)

    def issue(message: str) -> None:
        if message not in issues:
            issues.append(message)

    def timestamp(value: object) -> datetime | None:
        if not isinstance(value, datetime) or value.tzinfo is None:
            return None
        value = value.astimezone(timezone.utc)
        return value if earliest <= value <= latest else None

    def report() -> FitReport:
        offset = ((last - max(session_ends)).total_seconds()
                  if last and session_ends and len(session_ends) == counts["session"] else None)
        return FitReport(not issues, len(data), hashlib.sha256(data).hexdigest(),
                         counts["record"], counts["session"],
                         first.isoformat() if first else None, last.isoformat() if last else None,
                         tuple(issues), tuple(notices), integrity_ok, tuple(session_times), offset)

    if len(data) < 14 or len(data) > MAX_FIT_BYTES:
        issue("FIT-Dateigröße liegt außerhalb der erlaubten Grenzen.")
        return report()
    header_size = data[0]
    if header_size not in (12, 14) or data[8:12] != b".FIT" or data[1] >> 4 not in (1, 2):
        issue("FIT-Header oder Protokollversion ist ungültig oder wird nicht unterstützt.")
        return report()
    if header_size + struct.unpack_from("<I", data, 4)[0] + 2 != len(data):
        issue("FIT-Dateigröße stimmt nicht mit dem Header überein; Datei ist abgeschnitten oder enthält Zusatzdaten.")
        return report()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            with fitdecode.FitReader(io.BytesIO(data), check_crc=fitdecode.CrcCheck.RAISE,
                                     error_handling=fitdecode.ErrorHandling.RAISE) as reader:
                for index, frame in enumerate(reader):
                    if index >= MAX_FIT_FRAMES or time.monotonic() >= deadline:
                        issue("FIT-Prüfung hat das Zeit- oder Nachrichtenlimit erreicht.")
                        break
                    if frame.frame_type != fitdecode.FIT_FRAME_DATA:
                        continue
                    counts[frame.name] += 1
                    if frame.name == "file_id" and frame.get_value("type", fallback=None) != "activity":
                        issue("FIT-Datei ist nicht als Trainingsaktivität gekennzeichnet.")
                    elif frame.name == "activity":
                        utc_raw = frame.get_raw_value("timestamp", fallback=None)
                        local_raw = frame.get_raw_value("local_timestamp", fallback=None)
                        if (type(utc_raw) is int and type(local_raw) is int
                                and abs(local_raw - utc_raw) > 14 * 3600):
                            notice = "Activity-Lokalzeit weicht um mehr als 14 Stunden von UTC ab; mögliche Unix/FIT-Verwechslung."
                            if notice not in notices:
                                notices.append(notice)
                    elif frame.name == "record":
                        current = timestamp(frame.get_value("timestamp", fallback=None))
                        if current is None:
                            issue("FIT-Record enthält keinen gültigen UTC-Zeitstempel.")
                            continue
                        if previous is not None and current < previous:
                            issue("FIT-Record-Zeitstempel laufen rückwärts.")
                        first = min(first, current) if first else current
                        last = max(last, current) if last else current
                        previous = current
                    elif frame.name == "session":
                        if counts["session"] > MAX_FIT_SESSIONS:
                            issue("FIT-Prüfung hat das Sessionlimit erreicht.")
                            break
                        start = timestamp(frame.get_value("start_time", fallback=None))
                        summary_time = timestamp(frame.get_value("timestamp", fallback=None))
                        if start is None or summary_time is None or summary_time < start:
                            issue("FIT-Session enthält ungültige Start- oder Nachrichtenzeiten.")
                        if start is not None:
                            session_starts.append(start)
                        durations = [frame.get_value(name, fallback=None)
                                     for name in ("total_elapsed_time", "total_timer_time")]
                        bad_duration = any(isinstance(v, bool) or not isinstance(v, (int, float))
                                           or not math.isfinite(v) or v < 0
                                           for v in durations if v is not None)
                        if any(value is None for value in durations):
                            if "FIT-Session enthält nicht alle Dauerangaben." not in notices:
                                notices.append("FIT-Session enthält nicht alle Dauerangaben.")
                        elif not bad_duration and durations[1] > durations[0] + 1:
                            bad_duration = True
                        if bad_duration:
                            issue("FIT-Session enthält unplausible Dauerangaben.")
                        # A summary message spans start_time + total_elapsed_time.
                        # Its own timestamp is not used as the session end.
                        safe_durations = [float(v) if not isinstance(v, bool) and isinstance(v, (int, float))
                                          and math.isfinite(v) and v >= 0 else None for v in durations]
                        elapsed, timer = safe_durations
                        end = None
                        if start is not None and elapsed is not None:
                            try:
                                end = timestamp(start + timedelta(seconds=elapsed))
                            except (OverflowError, ValueError):
                                pass
                            if end is None:
                                issue("Aus FIT-Startzeit und Gesamtdauer ergibt sich keine plausible Endzeit.")
                            else:
                                session_ends.append(end)
                        elif elapsed is None:
                            notice = "Session-Ende ist ohne gültige Gesamtdauer nicht prüfbar."
                            if notice not in notices:
                                notices.append(notice)
                        session_times.append(SessionTiming(
                            start.isoformat() if start else None,
                            summary_time.isoformat() if summary_time else None,
                            elapsed, timer, end.isoformat() if end else None,
                        ))
                else:
                    integrity_ok = True
    except fitdecode.FitCRCError:
        issue("FIT-CRC-Prüfung fehlgeschlagen.")
    except Exception:
        issue("FIT-Nachrichtenstruktur konnte nicht vollständig und fehlerfrei gelesen werden.")
    if counts["file_id"] != 1 or not counts["session"] or not counts["record"]:
        issue("FIT-Datei benötigt genau eine file_id sowie Session- und Record-Nachrichten.")
    if not counts["activity"]:
        notices.append("FIT-Datei enthält keine abschließende Activity-Nachricht.")
    if first and session_starts and first < min(session_starts) - timedelta(seconds=2):
        issue("FIT-Records beginnen vor der Session.")
    if last and session_ends and len(session_ends) == counts["session"] and last > max(session_ends) + timedelta(seconds=2):
        issue("FIT-Records enden nach der Session.")
    if expected_start is not None and session_starts:
        if abs((min(session_starts) - expected_start).total_seconds()) > 300:
            issue("FIT-Startzeit weicht um mehr als fünf Minuten von der Aktivitätenliste ab.")
    return report()
