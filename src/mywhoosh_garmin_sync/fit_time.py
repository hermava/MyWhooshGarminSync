"""Conservative, explicit repair of the demonstrated MyWhoosh Unix/FIT mix-up."""

import io
import hashlib
import math
import struct
import time
import warnings
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import fitdecode
from fitdecode.utils import compute_crc

from .errors import FitError
from .fit_processing import MAX_FIT_FRAMES, VALIDATION_SECONDS, validate_fit

FIT_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)
UNIX_FIT_OFFSET = 631065600


@dataclass(frozen=True)
class TimeChange:
    field: str
    before_raw: int
    after_raw: int
    before: str
    after: str


@dataclass(frozen=True)
class TimeObservation:
    label: str
    value: str


@dataclass(frozen=True)
class TimeProposal:
    id: str
    finish_utc: str
    finish_local: str
    explanation: str
    changes: tuple[TimeChange, ...]
    interpretation: str = "utc"
    start_utc: str | None = None
    start_local: str | None = None


@dataclass(frozen=True)
class TimeRepairReport:
    status: str
    reason: str
    timezone: str | None
    source_sha256: str
    output_sha256: str
    changes: tuple[TimeChange, ...] = ()
    observations: tuple[TimeObservation, ...] = ()
    proposals: tuple[TimeProposal, ...] = ()
    reason_code: str = ""
    accepted_proposal: str | None = None
    interpretation: str = "utc"
    start_utc: str | None = None
    start_local: str | None = None
    shifted_fields: int = 0


def _clock(raw):
    # local_date_time is a wall clock, not a UTC instant: do not append Z/UTC.
    return (FIT_EPOCH + timedelta(seconds=raw)).replace(tzinfo=None).isoformat()


def _patch_activity(frame, replacements):
    """Replace existing uint32 fields only, preserving definitions and other bytes."""
    fields = frame.def_mesg.field_defs
    if (frame.time_offset is not None or len({f.def_num for f in fields}) != len(fields)
            or len(frame.chunk.bytes) != 1 + sum(f.size for f in frame.def_mesg.all_field_defs)):
        raise FitError("Activity-Nachricht hat kein eindeutig unterstütztes Datenlayout.")
    result = bytearray(frame.chunk.bytes)
    offset, seen = 1, set()
    for field in fields:
        if field.def_num in replacements:
            if field.size != 4 or field.base_type.identifier != 0x86:
                raise FitError("Activity-Zeitfeld benötigt ein vorhandenes uint32-Feld.")
            struct.pack_into(frame.def_mesg.endian + "I", result, offset, replacements[field.def_num])
            seen.add(field.def_num)
        offset += field.size
    if seen != set(replacements):
        raise FitError("Activity-Nachricht enthält nicht alle erforderlichen Zeitfelder.")
    return bytes(result)


def _changes(stamp, local, end_raw, offset):
    return tuple(TimeChange(name, before, after, _clock(before) + suffix, _clock(after) + suffix)
                 for name, before, after, suffix in (
                     ("activity.timestamp", stamp, end_raw, "Z"),
                     ("activity.local_timestamp", local, end_raw + offset, "")) if before != after)


def _write_copy(data, frames, activity, original, end_raw, zone, observations, proposals, accepted):
    end = FIT_EPOCH + timedelta(seconds=end_raw)
    offset = int(end.astimezone(zone).utcoffset().total_seconds())
    corrected_local = end_raw + offset
    if not 0x10000000 <= corrected_local < 0xFFFFFFFF:
        raise FitError("Korrigierte lokale Zeit liegt außerhalb des FIT-Zeitbereichs.")
    replacement = _patch_activity(activity, {253: end_raw, 5: corrected_local})
    body = b"".join(replacement if frame is activity else frame.chunk.bytes for frame in frames)
    header = bytearray(data[:data[0]])
    struct.pack_into("<I", header, 4, len(body))
    if len(header) == 14:
        struct.pack_into("<H", header, 12, compute_crc(header[:12]))
    output = bytes(header) + body
    output += struct.pack("<H", compute_crc(output))
    checked = validate_fit(output)
    if (not checked.valid or not checked.integrity_ok or checked.session_times != original.session_times
            or checked.records != original.records or checked.first_record_utc != original.first_record_utc
            or checked.last_record_utc != original.last_record_utc):
        raise FitError("Zeitkorrigierte FIT-Datei hat die Integritäts- und Messdatenprüfung nicht bestanden.")
    changes = _changes(activity.get_raw_value("timestamp"), activity.get_raw_value("local_timestamp"), end_raw, offset)
    return output, TimeRepairReport("ready", "Activity-Abschlusszeiten in separater Kopie korrigierbar.", zone.key,
                                   original.sha256, checked.sha256, changes, tuple(observations), tuple(proposals),
                                   "user_approved" if accepted else "confirmed_epoch_error", accepted)


def prepare_time_repair(data: bytes, *, timezone_name: str | None,
                        proposal_id: str | None = None) -> tuple[bytes, TimeRepairReport]:
    """Return an in-memory copy/preview; uncertain or correct inputs stay unchanged.

    Only original MyWhoosh, one session/activity, the proven epoch discrepancy and
    an independently corroborated stop are accepted. Never infer a timezone from
    virtual GPS coordinates or the machine's current timezone.
    """
    try:
        zone = ZoneInfo(timezone_name) if timezone_name is not None else None
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise FitError("Gültige IANA-Zeitzone angeben, zum Beispiel Europe/Berlin; Zeitzonendaten müssen installiert sein.") from None
    original = validate_fit(data)
    if not original.valid or not original.integrity_ok:
        raise FitError("Zeitkorrektur benötigt eine bestandene FIT-Prüfung einschließlich CRC.")

    observations, proposals = [], []

    def unchanged(status, reason, code=""):
        if proposal_id is not None:
            raise FitError("Zeitvorschlag ist nicht mehr gültig. Vorschau erneut erstellen.")
        return data, TimeRepairReport(status, reason, zone.key if zone else None, original.sha256, original.sha256,
                                     observations=tuple(observations), proposals=tuple(proposals), reason_code=code)

    frames, activities, stops, manufacturer = [], [], [], None
    deadline = time.monotonic() + VALIDATION_SECONDS
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            with fitdecode.FitReader(io.BytesIO(data), keep_raw_chunks=True,
                                     check_crc=fitdecode.CrcCheck.RAISE,
                                     error_handling=fitdecode.ErrorHandling.RAISE) as reader:
                for index, frame in enumerate(reader):
                    if index >= MAX_FIT_FRAMES or time.monotonic() >= deadline:
                        raise FitError("Zeitkorrektur hat ihr Zeit- oder Nachrichtenlimit erreicht.")
                    if frame.frame_type in (fitdecode.FIT_FRAME_HEADER, fitdecode.FIT_FRAME_CRC):
                        continue
                    frames.append(frame)
                    if frame.frame_type != fitdecode.FIT_FRAME_DATA:
                        continue
                    if frame.name == "file_id":
                        manufacturer = frame.get_raw_value("manufacturer", fallback=None)
                    elif frame.name == "activity":
                        activities.append(frame)
                    elif (frame.name == "event" and frame.get_value("event", fallback=None) in ("session", "timer")
                          and frame.get_value("event_type", fallback=None) in ("stop", "stop_all", "stop_disable", "stop_disable_all")):
                        stop = frame.get_value("timestamp", fallback=None)
                        if isinstance(stop, datetime) and stop.tzinfo is not None:
                            stops.append(stop)
    except FitError:
        raise
    except Exception:
        raise FitError("FIT-Zeitfelder konnten nicht sicher gelesen werden.") from None
    for index, timing in enumerate(original.session_times):
        prefix = f"Session {index + 1}: " if original.sessions > 1 else ""
        observations.extend((TimeObservation(prefix + "Start (UTC)", timing.start_utc or "Not recorded"),
                             TimeObservation(prefix + "Elapsed duration", f"{timing.elapsed_seconds} seconds"),
                             TimeObservation(prefix + "Timer duration", f"{timing.timer_seconds} seconds"),
                             TimeObservation(prefix + "Start + elapsed duration (UTC)", timing.elapsed_end_utc or "Not recorded")))
    observations.append(TimeObservation("Last measurement (UTC)", original.last_record_utc or "Not recorded"))
    observations.append(TimeObservation("Latest recorded stop (UTC)", max(stops).isoformat() if stops else "Not recorded"))
    if manufacturer != 331:
        return unchanged("not_applicable", "Nur MyWhoosh-Originale werden korrigiert; Zeitkorrektur vor dem Geräteprofil anwenden.")
    if original.sessions != 1 or len(activities) != 1:
        return unchanged("needs_review", "Zeitkorrektur benötigt genau eine Session und eine Activity-Nachricht.", "multiple_sessions")
    activity = activities[0]
    stamp = activity.get_raw_value("timestamp", fallback=None)
    local = activity.get_raw_value("local_timestamp", fallback=None)
    for label, raw in (("Stored activity finish (UTC)", stamp), ("Stored local finish", local)):
        observations.append(TimeObservation(label, _clock(raw) + ("Z" if label.endswith("(UTC)") else "")
                                            if type(raw) is int else "Not recorded"))
    if type(local) is int:
        observations.append(TimeObservation("Local value interpreted as Unix time (UTC)",
                                             datetime.fromtimestamp(local, timezone.utc).isoformat()))
    if not activity.has_field("local_timestamp"):
        return unchanged("not_applicable", "Kein lokales Activity-Zeitfeld vorhanden; keine Zeitkorrektur erforderlich.")
    if any(type(value) is not int for value in (stamp, local)):
        return unchanged("needs_review", "Activity enthält nicht beide gültigen Zeitfelder.", "missing_fields")
    # Preserve plausible, already encoded wall clocks, including other timezones.
    if abs(local - stamp) <= 14 * 3600 and (local - stamp) % 900 == 0:
        return unchanged("unchanged", "Kein nachgewiesener Unix/FIT-Zeitfehler; Datei unverändert.")
    timing = original.session_times[0]
    # Reviewable alternatives use recorded anchors, never arbitrary user-entered
    # clocks. Broader tolerances require explicit approval and never auto-apply.
    start = datetime.fromisoformat(timing.start_utc)
    last_record = datetime.fromisoformat(original.last_record_utc)
    elapsed_end = datetime.fromisoformat(timing.elapsed_end_utc) if timing.elapsed_end_utc else None
    if zone and elapsed_end and activity.get_raw_value("num_sessions", fallback=None) == 1:
        candidates = {}
        if 0 <= (elapsed_end - last_record).total_seconds() <= 120:
            raw = math.ceil((elapsed_end - FIT_EPOCH).total_seconds())
            candidates[raw] = "Use the recorded start plus elapsed duration. Measurements and workout duration stay unchanged."
        if stops:
            stop = max(stops).astimezone(timezone.utc)
            if 0 <= (stop - last_record).total_seconds() <= 300 and abs((stop - elapsed_end).total_seconds()) <= 300:
                raw = int((stop - FIT_EPOCH).total_seconds())
                candidates[raw] = ("The final stop and start plus elapsed duration agree."
                                   if raw in candidates else "Use the latest recorded stop; it differs from start plus elapsed duration.")
        for raw, explanation in candidates.items():
            end_candidate = FIT_EPOCH + timedelta(seconds=raw)
            normalized = FIT_EPOCH + timedelta(seconds=local - UNIX_FIT_OFFSET)
            if (not start - timedelta(hours=14) <= normalized <= end_candidate + timedelta(days=1)
                    or not int((start - FIT_EPOCH).total_seconds()) <= stamp <= raw + 300):
                continue
            offset = int(end_candidate.astimezone(zone).utcoffset().total_seconds())
            if not 0x10000000 <= raw + offset < 0xFFFFFFFF:
                continue
            try:
                _patch_activity(activity, {253: raw, 5: raw + offset})
            except FitError:
                continue
            identity = hashlib.sha256(f"time-review-v1:{original.sha256}:{zone.key}:{raw}".encode()).hexdigest()
            proposals.append(TimeProposal(identity, _clock(raw) + "Z", _clock(raw + offset), explanation,
                                           _changes(stamp, local, raw, offset), "utc", start.isoformat(),
                                           start.astimezone(zone).replace(tzinfo=None).isoformat()))
            from .fit_timeline import local_clock_to_utc, rewrite_local_timeline
            try:
                local_start_raw = int((start - FIT_EPOCH).total_seconds())
                local_start_utc = local_clock_to_utc(local_start_raw, zone)
                local_end_utc = local_clock_to_utc(raw, zone)
                # Verify the complete rewrite before offering it to the user.
                rewrite_local_timeline(data, frames, original, zone=zone, start_raw=local_start_raw, finish_raw=raw)
            except FitError as error:
                note = {
                    "local_clock_ambiguous": "Not offered: a recorded local clock is repeated or nonexistent during daylight saving change.",
                    "local_offset_changes": "Not offered: the UTC offset changes within the recording; preserving all time intervals cannot be verified.",
                }.get(error.code, "Not offered: complete conversion of this file's time fields could not be verified.")
                observation = TimeObservation("Local-clock conversion", note)
                if observation not in observations:
                    observations.append(observation)
                continue
            local_id = hashlib.sha256(f"time-review-local-v1:{original.sha256}:{zone.key}:{raw}".encode()).hexdigest()
            local_changes = (
                TimeChange("session.start_time", local_start_raw, local_start_utc, _clock(local_start_raw) + "Z", _clock(local_start_utc) + "Z"),
                TimeChange("activity.timestamp", stamp, local_end_utc, _clock(stamp) + "Z", _clock(local_end_utc) + "Z"),
                TimeChange("activity.local_timestamp", local, raw, _clock(local), _clock(raw)))
            proposals.append(TimeProposal(local_id, _clock(local_end_utc) + "Z", _clock(raw),
                                           "Stored clocks are local time. Convert all recording timestamps to UTC; keep duration and sensor values unchanged.",
                                           local_changes, "local", _clock(local_start_utc) + "Z", _clock(local_start_raw)))
    if proposal_id is not None:
        chosen = next((p for p in proposals if p.id == proposal_id), None)
        if chosen is None:
            raise FitError("Zeitvorschlag ist nicht mehr gültig. Vorschau erneut erstellen.")
        if chosen.interpretation == "local":
            from .fit_timeline import rewrite_local_timeline
            local_start_raw = int((start - FIT_EPOCH).total_seconds())
            local_end_raw = int((datetime.fromisoformat(chosen.finish_local).replace(tzinfo=timezone.utc) - FIT_EPOCH).total_seconds())
            output, checked, count = rewrite_local_timeline(data, frames, original, zone=zone,
                                                            start_raw=local_start_raw, finish_raw=local_end_raw)
            return output, TimeRepairReport("ready", "Ortszeit-Aufzeichnung vollständig nach UTC umgerechnet.", zone.key,
                                            original.sha256, checked.sha256, chosen.changes, tuple(observations), tuple(proposals),
                                            "user_approved_local", proposal_id, "local", chosen.start_utc, chosen.start_local, count)
        end_raw = int((datetime.fromisoformat(chosen.finish_utc) - FIT_EPOCH).total_seconds())
        return _write_copy(data, frames, activity, original, end_raw, zone, observations, proposals, proposal_id)
    if not timing.elapsed_end_utc or timing.timer_seconds is None or not stops:
        return unchanged("needs_review", "Trainingsende ist ohne Gesamtdauer, Timer und Stopp-Ereignis nicht eindeutig bestätigt.", "missing_stop")
    end = max(stops).astimezone(timezone.utc)
    elapsed_end = datetime.fromisoformat(timing.elapsed_end_utc)
    last_record = datetime.fromisoformat(original.last_record_utc)
    if (abs((end - elapsed_end).total_seconds()) > 1 or not 0 <= (end - last_record).total_seconds() <= 2):
        return unchanged("needs_review", "Stopp, letzter Messpunkt und Ende aus Start plus Dauer widersprechen sich.", "conflicting_end")
    start_raw = int((datetime.fromisoformat(timing.start_utc) - FIT_EPOCH).total_seconds())
    end_raw = int((end - FIT_EPOCH).total_seconds())
    if stamp not in (start_raw, end_raw):
        return unchanged("needs_review", "Activity-Zeitpunkt passt nicht zum bekannten MyWhoosh-Muster.", "unexpected_activity_time")
    timer = activity.get_value("total_timer_time", fallback=None)
    if (activity.get_raw_value("num_sessions", fallback=None) != 1 or isinstance(timer, bool)
            or not isinstance(timer, (int, float)) or not math.isfinite(timer)
            or abs(timer - timing.timer_seconds) > 0.001):
        return unchanged("needs_review", "Activity und Session stimmen bei Anzahl oder Timer nicht überein.", "timer_mismatch")
    # The observed faulty field holds Unix UTC seconds at the final sample/stop.
    normalized = local - UNIX_FIT_OFFSET
    last_raw = int((last_record - FIT_EPOCH).total_seconds())
    if not last_raw <= normalized <= end_raw:
        return unchanged("needs_review", "Unix/FIT-Verwechslung passt nicht eindeutig zu den aufgezeichneten Endzeiten.", "unconfirmed_epoch")
    if zone is None:
        return unchanged("needs_timezone", "Bekannter Zeitfehler erkannt; vor der Korrektur explizite IANA-Zeitzone wählen.", "timezone_required")
    return _write_copy(data, frames, activity, original, end_raw, zone, observations, proposals, None)


def prepare_automatic_time_repair(data: bytes, *, timezone_name: str | None):
    """Apply the selected MyWhoosh local-clock policy only to a verified proposal.

    Use session start plus elapsed duration as the finish anchor. Already correct
    files stay unchanged; unsupported or ambiguous timelines are never guessed.
    """
    output, report = prepare_time_repair(data, timezone_name=timezone_name)
    if report.status in ("unchanged", "not_applicable", "needs_timezone"):
        return output, report
    if report.reason_code not in ("confirmed_epoch_error", "missing_stop", "unconfirmed_epoch"):
        return data, replace(report, status="needs_review", output_sha256=report.source_sha256, changes=())
    if timezone_name is None:
        return data, replace(report, status="needs_timezone", output_sha256=report.source_sha256, changes=())
    original = validate_fit(data)
    finish = original.session_times[0].elapsed_end_utc if original.sessions == 1 else None
    finish_clock = _clock(math.ceil((datetime.fromisoformat(finish) - FIT_EPOCH).total_seconds())) if finish else None
    candidates = [p for p in report.proposals if p.interpretation == "local" and p.finish_local == finish_clock]
    if len(candidates) != 1 or original.session_times[0].timer_seconds is None:
        return data, replace(report, status="needs_review", output_sha256=report.source_sha256, changes=(),
                             reason="No unambiguous local-clock conversion is available.")
    output, converted = prepare_time_repair(data, timezone_name=timezone_name, proposal_id=candidates[0].id)
    return output, replace(converted, reason_code="automatic_local", accepted_proposal=None)
