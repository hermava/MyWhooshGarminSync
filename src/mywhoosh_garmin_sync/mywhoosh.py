"""Bounded HTTP reader based on publicly observed, undocumented endpoints."""

import json
import logging
import math
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from .auth import Session
from .errors import AuthError, NetworkError, ProtocolError, RateLimitError, SessionError
from .fit_processing import MAX_FIT_BYTES

LOGIN_URL = "https://services.mywhoosh.com/http-service/api/login"
ACTIVITIES_URL = "https://service14.mywhoosh.com/v2/rider/profile/activities"
DOWNLOAD_URL = "https://service14.mywhoosh.com/v2/rider/profile/download-activity-file"
MAX_RESPONSE_BYTES = 1_048_576
MAX_PAGES = 5
RUN_BUDGET_SECONDS = 60.0


@dataclass(frozen=True)
class Activity:
    id: str
    title: str
    start_time: datetime | None
    duration: str | None
    activity_file_id: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ActivityList:
    activities: tuple[Activity, ...]
    truncated: bool


def _parse_activity(raw: object) -> Activity:
    if not isinstance(raw, dict):
        raise ProtocolError("Ungültiger Aktivitätseintrag.")
    activity_id = raw.get("id")
    title = raw.get("title", "Aktivität")
    duration = raw.get("rideDuration")
    file_id = raw.get("activityFileId") or None
    if (
        not isinstance(activity_id, str) or not activity_id.strip()
        or len(activity_id) > 256
        or not isinstance(title, str) or len(title) > 1024
        or (duration is not None and (not isinstance(duration, str) or len(duration) > 64))
        or (file_id is not None and (not isinstance(file_id, str) or len(file_id) > 512))
    ):
        raise ProtocolError("Aktivität hat fehlende oder ungültige Pflichtfelder.")
    start = None
    try:
        if raw.get("startDatetime"):
            if not isinstance(raw["startDatetime"], str):
                raise ValueError
            start = datetime.fromisoformat(raw["startDatetime"])
            if start.tzinfo is None:
                raise ValueError
            start = start.astimezone(timezone.utc)
        elif raw.get("date") is not None:
            timestamp = raw["date"]
            if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
                raise ValueError
            if not math.isfinite(timestamp):
                raise ValueError
            start = datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        raise ProtocolError("Aktivität enthält einen ungültigen oder unklaren Zeitstempel.") from None
    return Activity(activity_id, title, start, duration, file_id)


class MyWhooshClient:
    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        # Transport debug logs can contain signed URLs, cookies or headers.
        for name in ("httpx", "httpcore.connection", "httpcore.http11", "httpcore.http2",
                     "httpcore.proxy", "httpcore.socks"):
            logging.getLogger(name).disabled = True
        self._http = httpx.Client(
            transport=transport,
            timeout=httpx.Timeout(15.0, connect=5.0),
            follow_redirects=False,
            trust_env=False,
            verify=True,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
        )

    def __enter__(self) -> "MyWhooshClient":
        return self

    def __exit__(self, *args: object) -> None:
        self._http.close()

    def _post(
        self, url: str, payload: dict, *, deadline: float, session: Session | None = None,
    ) -> dict:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise NetworkError("Zeitbudget für den Abruf ist aufgebraucht.")
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if session is not None:
            headers["Authorization"] = f"Bearer {session.access_token}"
        try:
            with self._http.stream(
                "POST", url, json=payload, headers=headers,
                timeout=httpx.Timeout(min(15.0, remaining), connect=min(5.0, remaining)),
            ) as response:
                if response.status_code in (401, 403):
                    raise AuthError("Anmeldung abgelehnt oder Sitzung abgelaufen. Erneut anmelden.",
                                    code=f"http_{response.status_code}")
                if response.status_code == 429:
                    raise RateLimitError("MyWhoosh begrenzt Anfragen (429). Später erneut versuchen.")
                if response.status_code >= 500:
                    raise NetworkError("MyWhoosh ist vorübergehend nicht verfügbar.")
                if not 200 <= response.status_code < 300:
                    raise ProtocolError("MyWhoosh lieferte einen unerwarteten HTTP-Status.")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ProtocolError("Komprimierte API-Antworten werden zunächst nicht unterstützt.")
                content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if content_type != "application/json" and not content_type.endswith("+json"):
                    raise ProtocolError("MyWhoosh lieferte keine JSON-Antwort.")
                body = bytearray()
                for chunk in response.iter_bytes(chunk_size=16_384):
                    if time.monotonic() >= deadline:
                        raise NetworkError("Zeitbudget für den Abruf ist aufgebraucht.")
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise ProtocolError("MyWhoosh-Antwort überschreitet die erlaubte Größe.")
            result = json.loads(body)
        except httpx.TimeoutException:
            raise NetworkError("Zeitüberschreitung beim MyWhoosh-Abruf.") from None
        except httpx.HTTPError:
            raise NetworkError("MyWhoosh-Verbindung fehlgeschlagen.") from None
        except (ValueError, UnicodeError, RecursionError):
            raise ProtocolError("MyWhoosh lieferte ungültiges JSON.") from None
        if not isinstance(result, dict):
            raise ProtocolError("MyWhoosh lieferte ein unbekanntes Antwortformat.")
        return result

    def login(self, email: str, password: str) -> Session:
        if not email.strip() or not password:
            raise AuthError("E-Mail und Passwort dürfen nicht leer sein.")
        result = self._post(LOGIN_URL, {
            "Username": email.strip(), "Password": password,
            "Platform": "Android", "Action": 1001,
            "CorrelationId": str(uuid.uuid4()), "DeviceId": str(uuid.uuid4()),
            "Authorization": "",
        }, deadline=time.monotonic() + RUN_BUDGET_SECONDS)
        if result.get("Success") is not True:
            raise AuthError("Anmeldung nicht bestätigt. Zugangsdaten oder Anmeldeverfahren prüfen; MFA wird noch nicht unterstützt.")
        try:
            return Session(result.get("AccessToken"), result.get("WhooshId"))
        except SessionError:
            raise ProtocolError("Anmeldeantwort enthält keine verwendbare Sitzung.") from None

    def activities(self, session: Session, *, limit: int = 10) -> ActivityList:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        deadline = time.monotonic() + RUN_BUDGET_SECONDS
        found: list[Activity] = []
        seen: set[str] = set()
        for page in range(1, MAX_PAGES + 1):
            result = self._post(ACTIVITIES_URL, {"sortDate": "DESC", "page": page},
                                deadline=deadline, session=session)
            data = result.get("data")
            if not isinstance(data, dict):
                raise ProtocolError("Aktivitätsantwort hat ein unbekanntes Format.")
            entries, total_pages = data.get("results"), data.get("totalPages")
            if (
                not isinstance(entries, list) or type(total_pages) is not int
                or total_pages < 0 or (total_pages == 0 and entries)
                or (total_pages > 0 and page > total_pages)
                or (not entries and page < total_pages)
            ):
                raise ProtocolError("Aktivitätsantwort hat ungültige Seitendaten.")
            parsed = [_parse_activity(entry) for entry in entries]
            new = [entry for entry in parsed if entry.id not in seen]
            if entries and not new:
                raise ProtocolError("MyWhoosh wiederholt eine bereits gelesene Seite.")
            for entry in new:
                if entry.id not in seen:
                    found.append(entry)
                    seen.add(entry.id)
            if len(found) >= limit:
                return ActivityList(tuple(found[:limit]), len(found) > limit or page < total_pages)
            if page >= total_pages:
                return ActivityList(tuple(found), False)
        return ActivityList(tuple(found), True)

    def download_fit(self, session: Session, file_id: str) -> bytes:
        """Resolve an opaque file ID, then fetch bytes without forwarding auth."""
        if not isinstance(file_id, str) or not file_id.strip() or len(file_id) > 512:
            raise ProtocolError("Keine verwendbare FIT-Dateikennung vorhanden.")
        deadline = time.monotonic() + RUN_BUDGET_SECONDS
        result = self._post(DOWNLOAD_URL, {"fileId": file_id}, deadline=deadline, session=session)
        signed_url = result.get("data")
        if not isinstance(signed_url, str) or len(signed_url) > 8192:
            raise ProtocolError("MyWhoosh lieferte keine verwendbare Download-Adresse.")
        try:
            url = httpx.URL(signed_url)
        except (httpx.InvalidURL, ValueError):
            raise ProtocolError("MyWhoosh lieferte eine ungültige Download-Adresse.") from None
        if (
            url.scheme != "https" or url.userinfo or url.fragment
            or url.port not in (None, 443)
            or not re.fullmatch(r"(?:[a-z0-9][a-z0-9.-]*\.)?s3(?:[.-][a-z0-9-]+)?\.amazonaws\.com", url.host)
        ):
            raise ProtocolError("Download-Adresse ist kein zugelassenes HTTPS-S3-Ziel.")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise NetworkError("Zeitbudget für den FIT-Download ist aufgebraucht.")
        try:
            # No Authorization header; the URL itself contains temporary access.
            with self._http.stream(
                "GET", url, headers={"Accept-Encoding": "identity"},
                timeout=httpx.Timeout(min(15.0, remaining), connect=min(5.0, remaining)),
            ) as response:
                if response.status_code == 429:
                    raise RateLimitError("Download begrenzt (429). Später erneut versuchen.")
                if response.status_code != 200:
                    raise NetworkError("FIT-Download nicht bestätigt; keine Datei gespeichert.")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ProtocolError("Komprimierter FIT-Download wird nicht unterstützt.")
                length = response.headers.get("content-length")
                if length is not None and (not length.isdecimal() or int(length) > MAX_FIT_BYTES):
                    raise ProtocolError("FIT-Download hat eine ungültige oder zu große Dateigröße.")
                body = bytearray()
                for chunk in response.iter_bytes(chunk_size=16_384):
                    if time.monotonic() >= deadline:
                        raise NetworkError("Zeitbudget für den FIT-Download ist aufgebraucht.")
                    body.extend(chunk)
                    if len(body) > MAX_FIT_BYTES:
                        raise ProtocolError("FIT-Download überschreitet die erlaubte Dateigröße.")
                if length is not None and len(body) != int(length):
                    raise ProtocolError("FIT-Download ist unvollständig.")
        except httpx.TimeoutException:
            raise NetworkError("Zeitüberschreitung beim FIT-Download; keine Datei gespeichert.") from None
        except httpx.HTTPError:
            raise NetworkError("FIT-Verbindung fehlgeschlagen; keine Datei gespeichert.") from None
        if not body:
            raise ProtocolError("FIT-Download ist leer.")
        return bytes(body)
