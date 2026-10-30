"""Preview, explicitly selected batch sync and read-only result reconciliation."""

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import secrets
import threading
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import AuthError, FitError, NetworkError, RateLimitError, SessionError, SyncCancelled, SyncError
from .fit_identity import prepare_edge530
from .fit_processing import MAX_FIT_BYTES, validate_fit
from .fit_time import TimeRepairReport, prepare_time_repair, prepare_automatic_time_repair
from .garmin import UploadResult, match_fit
from .sync_state import SyncState
from .workspace import Workspace


@dataclass(frozen=True)
class SyncSettings:
    days: int = 7
    limit: int = 10
    profile: str | None = None
    unit_id: int | None = field(default=None, repr=False)
    timezone: str | None = None
    adjust_times: bool = True

    def __post_init__(self):
        if type(self.adjust_times) is not bool:
            raise SessionError("Automatic time adjustment must be enabled or disabled.")
        if type(self.days) is not int or not 1 <= self.days <= 365:
            raise SessionError("Choose a lookback between 1 and 365 days.")
        if type(self.limit) is not int or not 1 <= self.limit <= 20:
            raise SessionError("Choose between 1 and 20 rides per preview.")
        if self.profile not in (None, "edge530"):
            raise SessionError("This device profile is not supported.")
        if self.unit_id is not None and (type(self.unit_id) is not int or not 1 <= self.unit_id <= 0xFFFFFFFF):
            raise SessionError("Enter a positive 32-bit device Unit ID.")
        if self.profile == "edge530" and self.unit_id is None:
            raise SessionError("Enter your own device Unit ID for the Edge 530 profile.")
        if self.timezone is not None:
            try:
                ZoneInfo(self.timezone)
            except (ZoneInfoNotFoundError, ValueError, TypeError):
                raise SessionError("Choose a valid timezone, such as Europe/Berlin.") from None


@dataclass(frozen=True)
class SyncItem:
    source_id: str
    title: str
    start_utc: str | None
    status: str
    reason: str = ""
    garmin_id: str | None = None
    target_hash: str | None = None
    time_status: str = ""
    time_detail: str = ""
    related_hashes: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class SyncPlan:
    identity: str
    scope: str
    settings: SyncSettings
    created_utc: datetime
    items: tuple[SyncItem, ...]
    truncated: bool = False
    review_count: int = 0


@dataclass(frozen=True)
class SyncResult:
    items: tuple[SyncItem, ...]
    stopped: bool = False


def _retry_authorization(row, target, source_hash, *, active=True):
    raw = row.get("retry_authorization", "") if row else ""
    if not raw:
        return None
    try:
        retry = json.loads(raw)
        if (not isinstance(retry, dict) or set(retry) != {"nonce", "source_sha256", "target_account", "deleted_ids", "used", "verified_at_utc"}
                or not isinstance(retry["nonce"], str) or len(retry["nonce"]) != 32
                or any(c not in "0123456789abcdef" for c in retry["nonce"])
                or retry["source_sha256"] != source_hash or retry["target_account"] != target
                or type(retry["used"]) is not bool or not isinstance(retry["verified_at_utc"], str)
                or not isinstance(retry["deleted_ids"], list) or not 1 <= len(retry["deleted_ids"]) <= 10
                or any(not isinstance(i, str) or not i.isascii() or not i.isdecimal() or len(i) > 64 or int(i) <= 0 for i in retry["deleted_ids"])):
            raise ValueError
    except (ValueError, TypeError, RecursionError):
        raise SessionError("Saved retry authorization is invalid. Keep existing upload records.") from None
    return None if active and retry["used"] else retry


def load_settings(workspace):
    data = workspace.read("settings.json", maximum=8192)
    if data is None:
        from .config import load_device_config
        device = load_device_config()
        return SyncSettings(profile=device.profile, unit_id=device.unit_id)
    try:
        raw = json.loads(data)
        required = {"days", "limit", "profile", "unit_id"}
        if not isinstance(raw, dict) or not required <= set(raw) or set(raw) - required - {"timezone", "adjust_times"}:
            raise ValueError
        return SyncSettings(**raw)
    except (ValueError, TypeError, RecursionError):
        raise SessionError("Sync settings are invalid. Correct the settings before syncing.") from None


def _item(activity, row):
    return SyncItem(activity.id, activity.title, row.get("start_utc") or (activity.start_time.isoformat() if activity.start_time else None),
                    "needs_review" if row["status"] == "upload_pending" else row["status"],
                    row.get("reason", ""), row.get("garmin_id"), row.get("target_hash"),
                    row.get("time_status", ""), row.get("time_detail", ""))


class SyncEngine:
    def __init__(self, accounts, workspace, *, legacy_directory, clock=None):
        self.accounts = accounts
        self.workspace = workspace
        self.journal = Workspace(legacy_directory)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._plan: SyncPlan | None = None
        self._running = threading.Lock()

    def save_settings(self, settings):
        """Persist valid desktop preferences independently of account/network work."""
        with self.workspace.locked():
            self.workspace.write("settings.json", json.dumps(asdict(settings)).encode(), replace=True)

    def _scope(self):
        source, target = self.accounts.sync_identity()
        return hashlib.sha256(f"{source}:{target}".encode()).hexdigest(), target

    @staticmethod
    def _cancel(cancel):
        if cancel():
            raise SyncCancelled()

    def _legacy_exists(self, hashes, *, deleted_ids=()):
        for value in hashes:
            if not value:
                continue
            paths = [self.journal.path(value + ".json")]
            if deleted_ids:
                extras = list(self.journal.root.glob(value + ".retry-*.json"))
                if len(extras) > 32:
                    return True
                paths.extend(extras)
            for path in paths:
                if not path.exists() and not path.is_symlink():
                    continue
                if not deleted_ids:
                    return True
                try:
                    marker = json.loads(self.journal.read(path.name, maximum=8192))
                except (ValueError, TypeError, SessionError):
                    return True
                if (not isinstance(marker, dict) or marker.get("status") != "uploaded"
                        or marker.get("activity_id") not in deleted_ids):
                    return True
        return False

    def _prepare(self, activity, settings, scope, target, state, remote, cancel, progress):
        previous = state.get(scope, activity.id)
        if previous and previous["status"] in ("uploaded", "upload_pending", "needs_review", "possible_duplicate"):
            return _item(activity, previous)
        stem = hashlib.sha256(f"{scope}:{activity.id}".encode()).hexdigest()
        original_name = stem + ".original.fit"
        self._cancel(cancel)
        progress(activity.id, "Downloading and validating FIT")
        data = self.workspace.read(original_name, maximum=MAX_FIT_BYTES)
        if data is None:
            data = self.accounts.download_fit(activity)
            self._cancel(cancel)
            self.workspace.write(original_name, data)
        original_hash = hashlib.sha256(data).hexdigest()
        if previous and previous["original_hash"] and previous["original_hash"] != original_hash:
            raise FitError("The cached original has changed. Keep it for review.")
        retry = _retry_authorization(previous, target, original_hash)
        deleted_ids = tuple(retry["deleted_ids"]) if retry else ()
        state.update(scope, activity.id, "downloaded", original_name=original_name, original_hash=original_hash,
                     start_utc=activity.start_time.isoformat() if activity.start_time else None)
        report = validate_fit(data, expected_start=activity.start_time)
        if not report.valid:
            raise FitError("FIT validation failed. The original was preserved.")
        self._cancel(cancel)
        original_data = data
        original_report = report
        progress(activity.id, "Checking FIT time metadata")
        if settings.adjust_times:
            data, time_report = prepare_automatic_time_repair(data, timezone_name=settings.timezone)
        else:
            time_report = TimeRepairReport("disabled", "Automatic time adjustment is off.", settings.timezone,
                                           original_hash, original_hash)
        time_status = (("approved" if time_report.accepted_proposal else "corrected")
                       if time_report.status == "ready" else time_report.status)
        time_detail = {
            "unchanged": "Time unchanged: no confirmed Unix/FIT time error.",
            "not_applicable": "Time unchanged: no supported MyWhoosh time correction applies to this FIT.",
            "needs_timezone": "Time error detected. Choose the timezone where you trained and preview again.",
            "needs_review": "Time metadata is ambiguous. This ride is blocked; its original is preserved.",
            "disabled": "Automatic time adjustment is off. Original timestamps will be uploaded unchanged.",
        }.get(time_status, "")
        if time_status in ("corrected", "approved"):
            changes = "; ".join(f"{ {'activity.timestamp': 'UTC finish', 'session.start_time': 'UTC start'}.get(c.field, 'Local finish')}: {c.before} → {c.after}"
                                for c in time_report.changes)
            time_detail = f"Time {'approved by you' if time_status == 'approved' else 'corrected'} using {settings.timezone}. {changes}"
            if time_report.interpretation == "local":
                time_detail = (f"Entire recording converted from {settings.timezone} local time to UTC. "
                               f"Start: {time_report.start_utc}; {changes}")
        elif time_status == "not_applicable" and any("Activity-Lokalzeit" in warning for warning in report.warnings):
            time_status = "needs_review"
            time_detail = "Time metadata is inconsistent and cannot be corrected automatically. The ride is blocked."
        state.update(scope, activity.id, "downloaded", time_status=time_status, time_detail=time_detail)
        prepared_start = (datetime.fromisoformat(time_report.start_utc) if time_report.interpretation == "local"
                          else activity.start_time)
        if settings.adjust_times and prepared_start and prepared_start > self.clock():
            return _item(activity, state.update(scope, activity.id, "blocked", target_name=None, target_hash=None,
                         reason="time_review", time_status="needs_review",
                         time_detail="The recording still starts in the future after time adjustment. Check the selected timezone."))
        if time_status in ("needs_timezone", "needs_review"):
            return _item(activity, state.update(scope, activity.id, "blocked", target_name=None, target_hash=None,
                         reason="timezone_required" if time_status == "needs_timezone" else "time_review"))
        hashes = {original_hash, hashlib.sha256(data).hexdigest()}
        if settings.adjust_times and time_status in ("corrected", "approved"):
            # Preserve guards for previous UTC-only repairs, before automatic local conversion.
            old_time_copy, _ = prepare_time_repair(original_data, timezone_name=settings.timezone)
            hashes.add(hashlib.sha256(old_time_copy).hexdigest())
            if settings.profile:
                old_device_copy, _ = prepare_edge530(old_time_copy, unit_id=settings.unit_id)
                hashes.add(hashlib.sha256(old_device_copy).hexdigest())
        if settings.profile:
            progress(activity.id, "Preparing Garmin device copy")
            data, _ = prepare_edge530(data, unit_id=settings.unit_id)
            if time_status in ("corrected", "approved"):
                # Old manual uploads recorded only the identity-only copy's hash.
                legacy_copy, _ = prepare_edge530(original_data, unit_id=settings.unit_id)
                hashes.add(hashlib.sha256(legacy_copy).hexdigest())
        report = validate_fit(data, expected_start=prepared_start)
        match_fit(report, (), window_limit=100)
        target_hash = hashlib.sha256(data).hexdigest()
        hashes.add(target_hash)
        target_name = stem + "." + target_hash + ".fit" if target_hash != original_hash else original_name
        existing = self.workspace.read(target_name, maximum=MAX_FIT_BYTES)
        if existing is None:
            self.workspace.write(target_name, data)
        elif existing != data:
            raise FitError("The prepared FIT copy has changed. Keep it for review.")
        fields = dict(target_name=target_name, target_hash=target_hash, reason="", garmin_id=None,
                      start_utc=report.session_times[0].start_utc)
        if self._legacy_exists(hashes, deleted_ids=deleted_ids) or any(
                state.attempted(target, value, scope=scope, source_id=activity.id, deleted_ids=deleted_ids) for value in hashes):
            return _item(activity, state.update(scope, activity.id, "needs_review", **{**fields, "reason": "previous_attempt"}))
        check = match_fit(report, remote, window_limit=100)
        if time_report.interpretation == "local":
            original_check = match_fit(original_report, remote, window_limit=100)
            if original_check.status != "no_candidate_in_window":
                check = original_check
        if check.status == "candidate_found":
            return _item(activity, state.update(scope, activity.id, "possible_duplicate",
                         **{**fields, "reason": "remote_candidate", "garmin_id": check.candidates[0].id}))
        if check.status != "no_candidate_in_window":
            return _item(activity, state.update(scope, activity.id, "blocked", **{**fields, "reason": "inconclusive_match"}))
        return replace(_item(activity, state.update(scope, activity.id, "ready", **fields)), related_hashes=tuple(sorted(hashes)))

    def preview(self, settings, *, cancel=lambda: False, progress=lambda *args: None):
        if not self._running.acquire(blocking=False):
            raise SessionError("A sync operation is already running.")
        self._plan = None
        try:
            with self.workspace.locked(), SyncState(self.workspace) as state:
                self._cancel(cancel)
                scope, target = self._scope()
                self.workspace.write("settings.json", json.dumps(asdict(settings)).encode(), replace=True)
                listing = self.accounts.mywhoosh_activities(limit=100)
                remote = self.accounts.garmin_activities(limit=100)
                now = self.clock()
                cutoff = now - timedelta(days=settings.days)
                def in_scope(activity):
                    if activity.start_time is None:
                        return False
                    if cutoff <= activity.start_time <= now:
                        return True
                    if settings.timezone:
                        # Discovery only: a mislabeled local clock may look future.
                        local = activity.start_time.replace(tzinfo=None).replace(tzinfo=ZoneInfo(settings.timezone))
                        return cutoff <= local.astimezone(timezone.utc) <= now
                    return False
                eligible = sorted((a for a in listing.activities if in_scope(a)), key=lambda a: a.start_time, reverse=True)
                items = []
                for activity in eligible[:settings.limit]:
                    self._cancel(cancel)
                    existing = state.get(scope, activity.id)
                    state.update(scope, activity.id, existing["status"] if existing else "discovered",
                                 title=activity.title, start_utc=(existing["start_utc"] if existing and existing["start_utc"]
                                                               else activity.start_time.isoformat()))
                    try:
                        item = self._prepare(activity, settings, scope, target, state, remote, cancel, progress)
                    except (AuthError, NetworkError, RateLimitError, SyncCancelled):
                        raise
                    except SyncError:
                        previous = state.get(scope, activity.id)
                        status = "needs_review" if previous and previous["status"] in ("uploaded", "upload_pending", "needs_review") else "failed"
                        item = _item(activity, state.update(scope, activity.id, status, reason="preparation_failed"))
                    items.append(item)
                # Unconfirmed earlier writes must remain resolvable even after
                # they disappear from MyWhoosh's bounded list or date window.
                reviews = state.reviews(scope, {item.source_id for item in items})
                for row in reviews[:20]:
                    items.append(SyncItem(row["source_id"], row["title"] or "Previous upload requiring review",
                                          row["start_utc"], "needs_review" if row["status"] == "upload_pending" else row["status"],
                                          row["reason"], row["garmin_id"], row["target_hash"],
                                          row["time_status"], row["time_detail"]))
                self._plan = SyncPlan(secrets.token_hex(16), scope, settings, self.clock(), tuple(items),
                                      listing.truncated or len(eligible) > settings.limit or len(reviews) > 20, min(len(reviews), 20))
                return self._plan
        finally:
            self._running.release()

    def _checked_plan(self, identity):
        plan = self._plan
        scope, target = self._scope()
        if (plan is None or identity != plan.identity or plan.scope != scope
                or not timedelta(0) <= self.clock() - plan.created_utc <= timedelta(minutes=15)):
            raise SessionError("The preview has expired or the accounts changed. Preview again.")
        return plan, target

    def execute(self, identity, selected, *, cancel=lambda: False, progress=lambda *args: None):
        if not self._running.acquire(blocking=False):
            raise SessionError("A sync operation is already running.")
        try:
            with self.workspace.locked(), SyncState(self.workspace) as state:
                plan, target = self._checked_plan(identity)
                ready = {item.source_id: item for item in plan.items if item.status == "ready"}
                if not selected or len(selected) > 20 or len(set(selected)) != len(selected) or not set(selected) <= ready.keys():
                    raise SessionError("Select ready rides from the current preview.")
                self._plan = None  # One consent cannot be replayed, including after an error.
                results = []
                for source_id in selected:
                    if cancel():
                        return SyncResult(tuple(results), stopped=True)
                    item = ready[source_id]
                    row = state.get(plan.scope, source_id)
                    if not row or row["status"] != "ready" or row["target_hash"] != item.target_hash:
                        raise SessionError("Local sync status changed. Preview again.")
                    data = self.workspace.read(row["target_name"], maximum=MAX_FIT_BYTES)
                    if data is None or hashlib.sha256(data).hexdigest() != item.target_hash:
                        raise FitError("The selected file changed or is missing. Preview again.")
                    hashes = set((row["original_hash"], row["target_hash"], *item.related_hashes))
                    original = self.workspace.read(row["original_name"], maximum=MAX_FIT_BYTES)
                    if original is None or hashlib.sha256(original).hexdigest() != row["original_hash"]:
                        raise FitError("The cached original changed. Preview again.")
                    original_timing = validate_fit(original)
                    retry = _retry_authorization(row, target, row["original_hash"])
                    deleted_ids = tuple(retry["deleted_ids"]) if retry else ()
                    attempt_target = target + ":" + retry["nonce"] if retry else target
                    started = False
                    markers = []

                    def begin():
                        nonlocal started
                        self._cancel(cancel)
                        if self._legacy_exists(hashes, deleted_ids=deleted_ids):
                            raise SessionError("A previous upload was found. Check Garmin before retrying.")
                        state.begin_upload(plan.scope, source_id, target, item.target_hash,
                                           retry_nonce=retry["nonce"] if retry else None)
                        started = True
                        for file_hash in sorted(hashes):
                            name = file_hash + ".json"
                            if retry and self.journal.path(name).exists():
                                name = file_hash + ".retry-" + retry["nonce"] + ".json"
                            self.journal.write(name, json.dumps({"sha256": file_hash, "status": "upload_pending",
                                               "recorded_at_utc": self.clock().isoformat()}).encode())
                            markers.append((name, file_hash))

                    progress(source_id, "Checking Garmin and uploading")
                    try:
                        # Refresh/read before constructing the one-shot uploader.
                        self.accounts.garmin_activities(limit=100)
                        with self.accounts.garmin_connection(upload=True) as uploader:
                            for identity in deleted_ids:
                                self._cancel(cancel)
                                if not uploader.activity_missing(identity):
                                    raise SessionError("A previously deleted activity is present in Garmin. Preview again.")
                            result = uploader.upload_once(data, before_send=begin, comparison_reports=(original_timing,))
                    except SyncCancelled:
                        if not started:
                            return SyncResult(tuple(results), stopped=True)
                        result = UploadResult("needs_review", None, "interrupted_after_intent")
                    except Exception as error:
                        if not started:
                            state.update(plan.scope, source_id, "failed", reason="pre_send_failed")
                            if isinstance(error, AuthError):
                                raise
                            results.append(replace(item, status="failed", reason="pre_send_failed"))
                            return SyncResult(tuple(results), stopped=True)
                        result = UploadResult("needs_review", None, "unconfirmed_after_intent")
                    try:
                        state.finish(plan.scope, source_id, attempt_target, item.target_hash, result)
                        for name, file_hash in markers:
                            self.journal.write(name, json.dumps({"sha256": file_hash, **asdict(result),
                                               "recorded_at_utc": self.clock().isoformat()}).encode(), replace=True)
                    except Exception:
                        # Pending intent remains durable if final persistence fails.
                        result = UploadResult("needs_review", result.activity_id, "local_confirmation_failed")
                    results.append(replace(item, status=result.status, reason=result.reason, garmin_id=result.activity_id))
                    progress(source_id, "Uploaded" if result.status == "uploaded" else "Check Garmin manually")
                    if result.status != "uploaded":
                        return SyncResult(tuple(results), stopped=True)
                return SyncResult(tuple(results))
        finally:
            self._running.release()

    def reconcile(self, source_id, garmin_id):
        if not self._running.acquire(blocking=False):
            raise SessionError("A sync operation is already running.")
        try:
            return self._reconcile(source_id, garmin_id)
        finally:
            self._running.release()

    def authorize_retry(self, identity, source_id, *, cancel=lambda: False, progress=lambda *args: None):
        """Reopen only a confirmed upload whose exact Garmin IDs now return 404."""
        if not self._running.acquire(blocking=False):
            raise SessionError("A sync operation is already running.")
        try:
            with self.workspace.locked(), SyncState(self.workspace) as state:
                plan, target = self._checked_plan(identity)
                row = state.get(plan.scope, source_id)
                if (not any(i.source_id == source_id for i in plan.items) or not row
                        or row["status"] != "uploaded" or not row["garmin_id"]):
                    raise SessionError("Only a confirmed upload with a known Garmin ID can be reopened.")
                original = self.workspace.read(row["original_name"], maximum=MAX_FIT_BYTES)
                if original is None or hashlib.sha256(original).hexdigest() != row["original_hash"]:
                    raise FitError("The preserved original is missing or changed.")
                old = _retry_authorization(row, target, row["original_hash"], active=False)
                identities = list(dict.fromkeys([*(old["deleted_ids"] if old else []), row["garmin_id"]]))
                if len(identities) > 10:
                    raise SessionError("This ride has too many previous retries for automatic reopening.")
                progress(source_id, "Checking that the previous Garmin activity was deleted")
                self.accounts.garmin_activities(limit=100)  # Refresh first, no credential retry on writes.
                with self.accounts.garmin_connection() as reader:
                    for activity_id in identities:
                        self._cancel(cancel)
                        if not reader.activity_missing(activity_id):
                            raise SessionError("The previous activity is still present in Garmin.", code="retry_present")
                    remote = reader.activities(limit=100)
                reports = [validate_fit(original)]
                if row["target_name"]:
                    prepared = self.workspace.read(row["target_name"], maximum=MAX_FIT_BYTES)
                    if prepared is None or hashlib.sha256(prepared).hexdigest() != row["target_hash"]:
                        raise FitError("The previous prepared file is missing or changed.")
                    reports.append(validate_fit(prepared))
                if any(match_fit(report, remote, window_limit=100).status != "no_candidate_in_window" for report in reports):
                    raise SessionError("A matching Garmin activity is still listed. Wait and check Garmin again.", code="retry_match")
                if self._legacy_exists((row["original_hash"], row["target_hash"]), deleted_ids=identities):
                    raise SessionError("Unclear or unrelated upload records remain. They cannot be bypassed by reopening this ride.", code="retry_uncertain")
                self._cancel(cancel)
                authorization = {"nonce": secrets.token_hex(16), "source_sha256": row["original_hash"],
                                 "target_account": target, "deleted_ids": identities, "used": False,
                                 "verified_at_utc": self.clock().isoformat()}
                state.update(plan.scope, source_id, "retry_allowed", reason="deletion_verified",
                             retry_authorization=json.dumps(authorization))
                self._plan = None
        finally:
            self._running.release()
        return self.preview(plan.settings, cancel=cancel, progress=progress)

    def _reconcile(self, source_id, garmin_id):
        with self.workspace.locked(), SyncState(self.workspace) as state:
            scope, target = self._scope()
            row = state.get(scope, source_id)
            if not row or row["status"] not in ("upload_pending", "needs_review", "possible_duplicate") or not row["target_name"]:
                raise SessionError("Select a ride that needs Garmin verification.")
            data = self.workspace.read(row["target_name"], maximum=MAX_FIT_BYTES)
            if data is None or hashlib.sha256(data).hexdigest() != row["target_hash"]:
                raise FitError("The prepared FIT is missing or changed. Keep the upload records.")
            self.accounts.garmin_activities(limit=100)
            with self.accounts.garmin_connection() as reader:
                activity = reader.activity(garmin_id)
            report = validate_fit(data)
            if match_fit(report, (activity,), window_limit=1).status != "candidate_found":
                raise FitError("This Garmin activity does not match the selected ride.")
            with state.db:
                retry = _retry_authorization(row, target, row["original_hash"], active=False)
                attempt_target = target + ":" + retry["nonce"] if retry else target
                state.db.execute("INSERT OR REPLACE INTO attempts VALUES(?,?,?,?,?,?)",
                                 (attempt_target, row["target_hash"], "uploaded", scope, source_id, garmin_id))
                state.db.execute("UPDATE rides SET status='uploaded',garmin_id=?,reason='manually_verified' WHERE scope=? AND source_id=?",
                                 (garmin_id, scope, source_id))
                if retry:
                    retry["used"] = True
                    state.db.execute("UPDATE rides SET retry_authorization=? WHERE scope=? AND source_id=?",
                                     (json.dumps(retry), scope, source_id))
            self._plan = None
            return source_id
