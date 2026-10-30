"""Bounded Garmin auth/reads, explicit session reuse and a guarded FIT upload."""

import contextlib
import json
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from typing import Callable, Protocol
from urllib.parse import urlsplit

import httpx

from .errors import AuthError, FitError, NetworkError, ProtocolError, RateLimitError, SessionError, SyncError
from .fit_processing import FitReport, validate_fit
from .sessions import SavedSession

ACTIVITIES_URL = "https://connectapi.garmin.com/activitylist-service/activities/search/activities"
UPLOAD_URL = "https://connectapi.garmin.com/upload-service/upload"
ACTIVITY_URL = "https://connectapi.garmin.com/activity-service/activity/"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
AUTH_BUDGET_SECONDS = 120.0
MAX_AUTH_REQUESTS = 10


class AuthBackend(Protocol):
    def login(self, email: str, password: str, prompt_mfa: Callable[[], str]) -> None: ...
    def restore_session(self, session: SavedSession) -> bool: ...
    def export_session(self) -> SavedSession: ...
    def refresh_session(self) -> None: ...
    def headers(self) -> dict[str, str]: ...
    def close(self) -> None: ...


def _quiet_transport_logs() -> None:
    # These libraries can include account data, headers and tickets in logs.
    prefixes = ("garminconnect", "curl_cffi", "urllib3", "httpcore", "httpx")
    for name in (*prefixes, *tuple(logging.Logger.manager.loggerDict)):
        if name.startswith(prefixes):
            logging.getLogger(name).disabled = True


def _create_backend() -> AuthBackend:
    """Pin the private integration points; importing this module needs no extra."""
    try:
        if version("garminconnect") != "0.3.20" or version("curl_cffi") != "0.16.3":
            raise SessionError("Garmin-Abhängigkeiten stimmen nicht mit requirements-garmin.lock überein.",
                               code="garmin_dependency_version")
        from curl_cffi import requests as curl_requests
        from garminconnect.client import Client
        from garminconnect.exceptions import (
            GarminConnectAuthenticationError, GarminConnectTooManyRequestsError,
        )
    except (ImportError, PackageNotFoundError):
        raise SessionError("Garmin-Abhängigkeiten fehlen. requirements-garmin.lock in der Projekt-.venv installieren.",
                           code="garmin_dependency_missing") from None
    _quiet_transport_logs()

    class Budget:
        deadline = 0.0
        requests = 0
        failure: SyncError | None = None
        input_error: BaseException | None = None

    budget = Budget()
    allowed = {
        ("GET", "sso.garmin.com", "/mobile/sso/en_US/sign-in"),
        ("POST", "sso.garmin.com", "/mobile/api/login"),
        ("POST", "sso.garmin.com", "/mobile/api/mfa/verifyCode"),
        ("POST", "diauth.garmin.com", "/di-oauth2-service/oauth/token"),
    }

    class BoundedSession(curl_requests.Session):
        def request(self, method, url, **kwargs):
            if budget.failure is not None:
                raise budget.failure
            parts = urlsplit(str(url))
            remaining = budget.deadline - time.monotonic()
            if (parts.scheme != "https" or parts.username or parts.password or parts.fragment
                    or parts.port not in (None, 443)
                    or (method.upper(), parts.hostname, parts.path) not in allowed):
                budget.failure = AuthError("Zusätzliches Garmin-Anmeldeverfahren oder MFA-Fallback wird nicht ausgeführt.")
            elif remaining <= 0 or budget.requests >= MAX_AUTH_REQUESTS:
                budget.failure = NetworkError("Garmin-Anmeldung hat ihr Zeit- oder Anfragenlimit erreicht.")
            if budget.failure is not None:
                raise budget.failure
            budget.requests += 1
            kwargs.update(timeout=min(15.0, remaining), verify=True, allow_redirects=False)
            try:
                response = super().request(method, url, **kwargs)
            except Exception:
                budget.failure = NetworkError("Garmin-Verbindung fehlgeschlagen; keine weiteren Netzversuche.")
                raise budget.failure from None
            if response.status_code in (427, 429):
                budget.failure = RateLimitError("Garmin begrenzt oder blockiert Anfragen. Später erneut versuchen.")
            elif 300 <= response.status_code < 400:
                budget.failure = AuthError("Garmin verlangt eine unerwartete Weiterleitung; Anmeldung abgebrochen.")
            elif len(response.content) > MAX_RESPONSE_BYTES:
                budget.failure = ProtocolError("Garmin-Anmeldeantwort überschreitet das Größenlimit.")
            if budget.failure is not None:
                raise budget.failure
            return response

    class MemoryClient(Client):
        def __init__(self):
            super().__init__(verify_login=False, pool_connections=1, pool_maxsize=1)
            self.skip_strategies = {"mobile+requests", "widget+cffi", "portal+cffi", "portal+requests"}
            self._manual_session = BoundedSession(impersonate="safari_ios", trust_env=False, verify=True)

        def _mobile_login_cffi(self, email, password):
            # One TLS profile, no credential retries or alternate login chain.
            self._do_mobile_login(self._manual_session, email, password)

        def _http_post(self, url, **kwargs):
            return self._manual_session.post(url, **kwargs)

        def _establish_session(self, ticket, sess=None, service_url=None):
            # DI tokens only; no browser-cookie / redirect fallback.
            self._exchange_service_ticket(ticket, service_url=service_url)

        def login(self, email, password, prompt_mfa):
            budget.deadline = time.monotonic() + AUTH_BUDGET_SECONDS
            budget.requests = 0
            budget.failure = None
            budget.input_error = None

            def paused_prompt():
                before = time.monotonic()
                try:
                    return prompt_mfa()
                except (EOFError, KeyboardInterrupt, SyncError) as exc:
                    budget.input_error = exc
                    raise
                finally:
                    budget.deadline += time.monotonic() - before

            try:
                super().login(email, password, prompt_mfa=paused_prompt, return_on_mfa=False)
                if not self.di_token:
                    raise AuthError("Garmin-Anmeldung hat keinen verwendbaren Sitzungstoken geliefert.")
            except Exception as exc:
                if budget.input_error is not None:
                    raise budget.input_error from None
                if budget.failure is not None:
                    raise budget.failure from None
                if isinstance(exc, GarminConnectTooManyRequestsError):
                    raise RateLimitError("Garmin begrenzt Anfragen. Später erneut versuchen.") from None
                if isinstance(exc, (GarminConnectAuthenticationError, AuthError)):
                    raise AuthError("Garmin-Anmeldung oder MFA nicht bestätigt. Zugangsdaten und Code prüfen.") from None
                raise NetworkError("Garmin-Anmeldung konnte nicht abgeschlossen werden.") from None

        def headers(self):
            return self.get_api_headers()

        def export_session(self):
            return SavedSession("garmin", self.di_token, refresh_token=self.di_refresh_token,
                                client_id=self.di_client_id)

        def restore_session(self, session):
            if session.provider != "garmin":
                raise SessionError("Cannot use another provider's session for Garmin.")
            self._clear_auth_state()
            self.di_token = session.access_token
            self.di_refresh_token = session.refresh_token
            self.di_client_id = session.client_id
            if self._token_expires_soon():
                self.refresh_session()
                return True
            return False

        def refresh_session(self):
            if not self.di_refresh_token or not self.di_client_id:
                raise AuthError("Garmin sign-in is required to renew this session.", code="session_expired")
            budget.deadline = time.monotonic() + AUTH_BUDGET_SECONDS
            budget.requests = 0
            budget.failure = None
            budget.input_error = None
            # One refresh request, no credential login, fallback or vendor retry.
            response = self._di_token_post(self.di_client_id, "application/json", {
                "grant_type": "refresh_token", "client_id": self.di_client_id,
                "refresh_token": self.di_refresh_token,
            })
            if response.status_code == 401:
                raise AuthError("Garmin session renewal was rejected. Please sign in again.", code="session_expired")
            if response.status_code == 403:
                raise AuthError("Garmin denied session renewal.", code="http_403")
            if response.status_code >= 500:
                raise NetworkError("Garmin session renewal is temporarily unavailable.")
            try:
                data = response.json()
            except (ValueError, TypeError):
                raise ProtocolError("Garmin returned an invalid session renewal response.") from None
            if not isinstance(data, dict):
                raise ProtocolError("Garmin returned an unknown session renewal response.")
            if response.status_code == 400 and data.get("error") == "invalid_grant":
                raise AuthError("Garmin session has expired. Please sign in again.", code="session_expired")
            if response.status_code != 200:
                raise ProtocolError("Garmin did not confirm session renewal.")
            try:
                renewed = SavedSession("garmin", data.get("access_token"),
                                       refresh_token=data.get("refresh_token", self.di_refresh_token),
                                       client_id=self.di_client_id)
                if renewed.refresh_token is None:
                    raise SessionError("Missing refresh token.")
            except SessionError:
                raise ProtocolError("Garmin returned unusable renewed session credentials.") from None
            self.di_token = renewed.access_token
            self.di_refresh_token = renewed.refresh_token

        def close(self):
            self._clear_auth_state()
            for session in (self._manual_session, self.cs, self._api_session):
                with contextlib.suppress(Exception):
                    session.close()

    return MemoryClient()


@dataclass(frozen=True)
class GarminActivity:
    id: str
    title: str
    start_time_utc: str | None
    duration_seconds: float | None
    elapsed_seconds: float | None


def _duration(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ProtocolError("Garmin-Aktivität enthält eine ungültige Dauer.")
    return float(value)


def _parse_activity(raw: object) -> GarminActivity:
    if not isinstance(raw, dict):
        raise ProtocolError("Garmin lieferte einen ungültigen Aktivitätseintrag.")
    identity = raw.get("activityId")
    title = raw.get("activityName", "Aktivität")
    if (isinstance(identity, bool) or not isinstance(identity, (str, int))
            or len(str(identity)) > 64 or not str(identity).isascii() or not str(identity).isdecimal()
            or int(identity) <= 0 or not isinstance(title, str) or len(title) > 1024):
        raise ProtocolError("Garmin-Aktivität enthält ungültige Pflichtfelder.")
    start = raw.get("startTimeGMT")
    if start is not None:
        try:
            if not isinstance(start, str):
                raise ValueError
            parsed = datetime.fromisoformat(start)
            # The explicitly GMT-labelled field is UTC even when no suffix is provided.
            start = (parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None
                     else parsed.astimezone(timezone.utc)).isoformat()
        except ValueError:
            raise ProtocolError("Garmin-Aktivität enthält eine ungültige GMT-Startzeit.") from None
    return GarminActivity(str(identity), title, start, _duration(raw.get("duration")),
                          _duration(raw.get("elapsedDuration")))


def _parse_activity_detail(raw: object) -> GarminActivity:
    # activity-service nests timing fields, unlike activitylist-service.
    if not isinstance(raw, dict) or not isinstance(raw.get("summaryDTO"), dict):
        raise ProtocolError("Garmin-Aktivitätsdetail enthält keine bekannte Zusammenfassung.")
    summary = raw["summaryDTO"]
    return _parse_activity({"activityId": raw.get("activityId"),
                            "activityName": raw.get("activityName", "Aktivität"),
                            "startTimeGMT": summary.get("startTimeGMT"),
                            "duration": summary.get("duration"),
                            "elapsedDuration": summary.get("elapsedDuration")})


class GarminReader:
    def __init__(self, *, backend: AuthBackend | None = None, transport: httpx.BaseTransport | None = None):
        self._backend = backend if backend is not None else _create_backend()
        self._authenticated = False
        _quiet_transport_logs()
        self._http = httpx.Client(transport=transport, verify=True, trust_env=False,
                                 follow_redirects=False, timeout=httpx.Timeout(15.0, connect=5.0),
                                 limits=httpx.Limits(max_connections=1, max_keepalive_connections=1))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        try:
            self._backend.close()
        finally:
            self._http.close()
            self._authenticated = False

    def login(self, email: str, password: str, prompt_mfa: Callable[[], str]) -> None:
        self._authenticated = False
        if not email.strip() or not password:
            raise AuthError("Garmin-E-Mail und Passwort dürfen nicht leer sein.")
        self._backend.login(email.strip(), password, prompt_mfa)
        self._authenticated = True

    def restore_session(self, session: SavedSession) -> bool:
        self._authenticated = False
        refreshed = self._backend.restore_session(session)
        self._authenticated = True
        return refreshed

    def export_session(self) -> SavedSession:
        if not self._authenticated:
            raise AuthError("Sign in before exporting a Garmin session.")
        return self._backend.export_session()

    def refresh_session(self) -> None:
        self._authenticated = False
        self._backend.refresh_session()
        self._authenticated = True

    def activities(self, *, limit: int = 10) -> tuple[GarminActivity, ...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        rows = self._request_json("GET", ACTIVITIES_URL, params={"start": 0, "limit": limit})
        if not isinstance(rows, list) or len(rows) > limit:
            raise ProtocolError("Garmin-Aktivitätsliste hat ein unbekanntes Format oder überschreitet das Limit.")
        activities = tuple(_parse_activity(row) for row in rows)
        if len({a.id for a in activities}) != len(activities):
            raise ProtocolError("Garmin-Aktivitätsliste enthält doppelte Kennungen.")
        return activities

    def activity(self, identity: str) -> GarminActivity:
        if (not isinstance(identity, str) or not identity.isascii() or not identity.isdecimal()
                or len(identity) > 64 or int(identity) <= 0):
            raise SessionError("Enter a valid numeric Garmin activity ID.")
        result = _parse_activity_detail(self._request_json("GET", ACTIVITY_URL + identity))
        if result.id != identity:
            raise ProtocolError("Garmin returned a different activity ID.")
        return result

    def activity_missing(self, identity: str) -> bool:
        """Only an authenticated exact-ID HTTP 404 confirms absence here."""
        try:
            self.activity(identity)
        except NetworkError as error:
            if error.code == "http_404":
                return True
            raise
        return False

    def _request_json(self, method: str, url: str, **kwargs) -> object:
        if not self._authenticated:
            raise AuthError("Zuerst manuell bei Garmin anmelden.")
        deadline = time.monotonic() + 30.0
        headers = {**self._backend.headers(), "Accept-Encoding": "identity", "Accept": "application/json"}
        try:
            with self._http.stream(method, url, headers=headers, **kwargs) as response:
                if response.status_code in (401, 403):
                    raise AuthError("Garmin-Sitzung wurde nicht akzeptiert; erneut manuell anmelden.",
                                    code=f"http_{response.status_code}")
                if response.status_code == 429:
                    raise RateLimitError("Garmin begrenzt Anfragen. Später erneut versuchen.", code="http_429")
                if response.status_code not in ((200, 201) if method == "POST" else (200,)):
                    raise NetworkError("Garmin-Anfrage wurde nicht bestätigt.", code=f"http_{response.status_code}")
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise ProtocolError("Komprimierte Garmin-Antworten werden nicht unterstützt.", code="response_compressed")
                content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                if content_type != "application/json" and not content_type.endswith("+json"):
                    raise ProtocolError("Garmin lieferte keine JSON-Antwort.", code="response_not_json")
                body = bytearray()
                for chunk in response.iter_bytes(chunk_size=16_384):
                    body.extend(chunk)
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise ProtocolError("Garmin-Antwort überschreitet das Größenlimit.", code="response_too_large")
                    if time.monotonic() >= deadline:
                        raise ProtocolError("Garmin-Antwort überschreitet das Zeitlimit.", code="response_time_limit")
            return json.loads(body)
        except httpx.TimeoutException:
            raise NetworkError("Garmin-Anfrage hat das Zeitlimit erreicht.", code="timeout") from None
        except httpx.HTTPError:
            raise NetworkError("Garmin-Anfrage fehlgeschlagen.", code="transport_error") from None
        except (ValueError, RecursionError):
            raise ProtocolError("Garmin lieferte ungültiges JSON.", code="invalid_json") from None


@dataclass(frozen=True)
class UploadResult:
    status: str
    activity_id: str | None
    reason: str


class GarminUploader(GarminReader):
    """One write attempt, with fresh matching and no write retries or refresh."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._upload_sent = False

    def upload_once(self, data: bytes, *, before_send: Callable[[], None],
                    comparison_reports: tuple[FitReport, ...] = ()) -> UploadResult:
        if self._upload_sent:
            raise SessionError("In dieser Sitzung wurde bereits ein Upload versucht; nicht erneut senden.")
        report = validate_fit(data)
        match_fit(report, (), window_limit=100)
        activities = self.activities(limit=100)
        for timing in (report, *comparison_reports):
            check = match_fit(timing, activities, window_limit=100)
            if check.status != "no_candidate_in_window":
                raise FitError("Upload gesperrt: Garmin-Abgleich enthält Kandidaten oder unvollständige Daten.")
        # Persist the attempt before any write can reach Garmin. Callback failure
        # must stop here. Keep the exact validated bytes, never reopen the path.
        before_send()
        self._upload_sent = True
        try:
            raw = self._request_json("POST", UPLOAD_URL,
                                     files={"file": ("activity.fit", data, "application/octet-stream")})
        except SyncError as exc:
            return UploadResult("needs_review", None, f"upload_{exc.code}" if exc.code else "upload_not_confirmed")
        detail = raw.get("detailedImportResult") if isinstance(raw, dict) else None
        if not isinstance(detail, dict) or detail.get("failures") != []:
            return UploadResult("needs_review", None, "unknown_import_result")
        successes = detail.get("successes")
        if not isinstance(successes, list) or len(successes) != 1 or not isinstance(successes[0], dict):
            return UploadResult("needs_review", None, "unknown_import_result")
        identity = successes[0].get("internalId")
        if (isinstance(identity, bool) or not isinstance(identity, (str, int))
                or len(str(identity)) > 64 or not str(identity).isascii()
                or not str(identity).isdecimal() or int(identity) <= 0):
            return UploadResult("needs_review", None, "missing_activity_id")
        identity = str(identity)
        try:
            activity = _parse_activity_detail(self._request_json("GET", ACTIVITY_URL + identity))
            verified = match_fit(report, (activity,), window_limit=1)
        except SyncError as exc:
            return UploadResult("needs_review", identity, f"verification_{exc.code}" if exc.code else "verification_failed")
        if activity.id != identity or verified.status != "candidate_found":
            return UploadResult("needs_review", identity, "verification_mismatch")
        return UploadResult("uploaded", identity, "import_confirmed_and_times_matched")


@dataclass(frozen=True)
class MatchReport:
    status: str
    checked_activities: int
    candidates: tuple[GarminActivity, ...]
    uncertain_entries: int
    window_limit: int


def match_fit(report: FitReport, activities: tuple[GarminActivity, ...], *, window_limit: int) -> MatchReport:
    if not report.valid or len(report.session_times) != 1:
        raise FitError("Garmin-Abgleich benötigt eine bestandene FIT-Prüfung mit genau einer Session.")
    session = report.session_times[0]
    if session.start_utc is None or (session.elapsed_seconds is None and session.timer_seconds is None):
        raise FitError("FIT-Startzeit oder vergleichbare Dauer fehlt für den Garmin-Abgleich.")
    start = datetime.fromisoformat(session.start_utc)
    candidates = []
    uncertain = 0
    for activity in activities:
        if activity.start_time_utc is None:
            uncertain += 1
            continue
        if abs((datetime.fromisoformat(activity.start_time_utc) - start).total_seconds()) > 5:
            continue
        if session.elapsed_seconds is not None and activity.elapsed_seconds is not None:
            source_duration, target_duration = session.elapsed_seconds, activity.elapsed_seconds
        elif session.timer_seconds is not None and activity.duration_seconds is not None:
            source_duration, target_duration = session.timer_seconds, activity.duration_seconds
        else:
            uncertain += 1
            continue
        if abs(source_duration - target_duration) <= 3:
            candidates.append(activity)
    status = ("ambiguous" if len(candidates) > 1 or (candidates and uncertain)
              else "candidate_found" if candidates else "inconclusive" if uncertain else "no_candidate_in_window")
    return MatchReport(status, len(activities), tuple(candidates), uncertain, window_limit)
