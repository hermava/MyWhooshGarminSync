"""Lossless payload editing for explicitly confirmed local-clock recordings."""

from datetime import datetime, timedelta, timezone
import struct
import time

import fitdecode
from fitdecode.utils import compute_crc

from .errors import FitError
from .fit_processing import MAX_FIT_BYTES, VALIDATION_SECONDS, validate_fit

EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)


def local_clock_to_utc(raw, zone):
    """Reject nonexistent or repeated wall clocks rather than guessing DST folds."""
    naive = (EPOCH + timedelta(seconds=raw)).replace(tzinfo=None)
    candidates = set()
    for fold in (0, 1):
        utc = naive.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
        if utc.astimezone(zone).replace(tzinfo=None) == naive:
            candidates.add(int((utc - EPOCH).total_seconds()))
    if len(candidates) != 1:
        raise FitError("Ortszeit ist bei der Zeitumstellung mehrdeutig oder existiert nicht; keine automatische Umrechnung.", code="local_clock_ambiguous")
    result = candidates.pop()
    if not 0x10000000 <= result < 0xFFFFFFFF:
        raise FitError("Umrechnung liegt außerhalb des FIT-Zeitbereichs.")
    return result


def rewrite_local_timeline(data, frames, original, *, zone, start_raw, finish_raw):
    """Convert absolute native date_time fields, retaining non-time bytes exactly.

    Compressed timestamps are expanded into temporary full-timestamp definitions;
    original definitions are restored immediately, preserving local-number reuse.
    Durations, local wall clocks and invalid/relative date values are not shifted.
    A changing offset is refused because elapsed durations must remain intact.
    """
    delta = local_clock_to_utc(start_raw, zone) - start_raw
    cache, count = {}, 0

    def convert(raw):
        nonlocal count
        if raw == 0xFFFFFFFF or raw < 0x10000000:
            return raw
        if raw not in cache:
            cache[raw] = local_clock_to_utc(raw, zone)
            if cache[raw] - raw != delta:
                raise FitError("Aufzeichnung überschreitet eine Zeitumstellung; Zeitabstände können nicht unverändert umgerechnet werden.", code="local_offset_changes")
        count += 1
        return cache[raw]

    body = bytearray()
    deadline = time.monotonic() + VALIDATION_SECONDS
    for frame in frames:
        if time.monotonic() >= deadline:
            raise FitError("Umrechnung der Zeitachse hat ihr Zeitlimit erreicht.")
        if frame.frame_type != fitdecode.FIT_FRAME_DATA:
            body.extend(frame.chunk.bytes)
            continue
        definition = frame.def_mesg
        raw = bytearray(frame.chunk.bytes)
        fields = definition.field_defs
        if (len({f.def_num for f in fields}) != len(fields)
                or len(raw) != 1 + sum(f.size for f in definition.all_field_defs)):
            raise FitError("FIT-Nachricht hat kein eindeutig unterstütztes Datenlayout.")
        offset = 1
        for field in fields:
            # Include resolved date_time subfields and the protocol-wide timestamp.
            resolved = [f for f in frame.fields if f.field_def is field]
            absolute = (field.type.name == "date_time" or field.def_num == 253
                        or any(f.field and f.field.type.name == "date_time" for f in resolved))
            if absolute:
                if field.base_type.identifier != 0x86 or field.size % 4:
                    raise FitError("Absolutes Zeitfeld benötigt uint32-Werte.")
                for position in range(offset, offset + field.size, 4):
                    value = struct.unpack_from(definition.endian + "I", raw, position)[0]
                    if frame.name == "activity" and field.def_num == 253:
                        value = finish_raw
                    struct.pack_into(definition.endian + "I", raw, position, convert(value))
            elif frame.name == "activity" and field.def_num == 5:
                if field.size != 4 or field.base_type.identifier != 0x86:
                    raise FitError("Lokales Activity-Zeitfeld benötigt uint32.")
                struct.pack_into(definition.endian + "I", raw, offset, finish_raw)
            offset += field.size
        if frame.time_offset is None:
            body.extend(raw)
        else:
            if any(f.def_num == 253 for f in fields) or len(fields) >= 255:
                raise FitError("Komprimierte Zeitangabe hat eine widersprüchliche Definition.")
            timestamp = frame.get_raw_value("timestamp", fallback=None)
            if type(timestamp) is not int:
                raise FitError("Komprimierte Zeitangabe konnte nicht rekonstruiert werden.")
            timestamp = convert(timestamp)
            old_def = definition.chunk.bytes
            native_end = 6 + 3 * len(fields)
            expanded = (old_def[:5] + bytes([len(fields) + 1]) + old_def[6:native_end]
                        + bytes((253, 4, 0x86)) + old_def[native_end:])
            native_size = sum(f.size for f in fields)
            payload = (bytes([frame.local_mesg_num]) + raw[1:1 + native_size]
                       + struct.pack(definition.endian + "I", timestamp) + raw[1 + native_size:])
            body.extend(expanded + payload + old_def)
        if len(body) + data[0] + 2 > MAX_FIT_BYTES:
            raise FitError("Umgeschriebene Zeitachse überschreitet das FIT-Größenlimit.")
    header = bytearray(data[:data[0]])
    struct.pack_into("<I", header, 4, len(body))
    if len(header) == 14:
        struct.pack_into("<H", header, 12, compute_crc(header[:12]))
    output = bytes(header + body)
    output += struct.pack("<H", compute_crc(output))
    checked = validate_fit(output)
    def shifted(value):
        return datetime.fromisoformat(value) + timedelta(seconds=delta) if value else None
    if (not checked.valid or not checked.integrity_ok or checked.records != original.records
            or checked.sessions != original.sessions
            or datetime.fromisoformat(checked.first_record_utc) != shifted(original.first_record_utc)
            or datetime.fromisoformat(checked.last_record_utc) != shifted(original.last_record_utc)):
        raise FitError("Umgeschriebene Zeitachse hat die FIT-Gegenprüfung nicht bestanden.")
    for old, new in zip(original.session_times, checked.session_times, strict=True):
        if (new.elapsed_seconds != old.elapsed_seconds or new.timer_seconds != old.timer_seconds
                or datetime.fromisoformat(new.start_utc) != shifted(old.start_utc)
                or datetime.fromisoformat(new.timestamp_utc) != shifted(old.timestamp_utc)
                or (new.elapsed_end_utc and datetime.fromisoformat(new.elapsed_end_utc) != shifted(old.elapsed_end_utc))):
            raise FitError("Session-Zeiten oder Dauer wurden nicht konsistent umgerechnet.")
    return output, checked, count
