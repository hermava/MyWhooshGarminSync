"""Experimental identity-only FIT copies, preserving unrelated binary data."""

import io
import struct
import time
import warnings
from dataclasses import dataclass

import fitdecode
from fitdecode.utils import compute_crc

from .errors import FitError
from .fit_processing import MAX_FIT_BYTES, MAX_FIT_FRAMES, VALIDATION_SECONDS, validate_fit

# Official FIT profile: Garmin manufacturer 1; garmin_product.edge_530 = 3121.
GARMIN = 1
EDGE_530 = 3121
MYWHOOSH = 331
MAX_IDENTITY_MESSAGES = 128


@dataclass(frozen=True)
class IdentityChange:
    message: str
    message_index: int
    field: str
    action: str
    before: int | None
    after: int | None
    value_hidden: bool = False


@dataclass(frozen=True)
class IdentityReport:
    profile: str
    source_sha256: str
    output_sha256: str
    source_size: int
    output_size: int
    changes: tuple[IdentityChange, ...]
    warnings: tuple[str, ...]


def _rewrite(frame, *, unit_id: int | None, index: int) -> tuple[bytes, list[IdentityChange]]:
    """Use raw definitions and payloads; never reconstruct other fields."""
    definition = frame.def_mesg
    fields = definition.field_defs
    raw = frame.chunk.bytes
    if frame.time_offset is not None:
        raise FitError("Komprimierte Geräte-Nachrichten werden für dieses Profil nicht umgeschrieben.")
    if len(raw) != 1 + sum(f.size for f in definition.all_field_defs):
        raise FitError("Geräte-Nachricht hat ein nicht unterstütztes Datenlayout.")
    if len({f.def_num for f in fields}) != len(fields):
        raise FitError("Doppelte Gerätefelder verhindern eine eindeutige Konvertierung.")
    file_id = frame.global_mesg_num == 0
    # def number -> (name, value, size, FIT base type)
    desired = {1 if file_id else 2: ("manufacturer", GARMIN, 2, 0x84),
               2 if file_id else 4: ("product", EDGE_530, 2, 0x84)}
    if unit_id is not None:
        desired[3] = ("serial_number", unit_id, 4, 0x8C)
    parts = []
    changes = []
    offset = 1
    seen = set()
    for field in fields:
        part = raw[offset:offset + field.size]
        offset += field.size
        if field.def_num in desired:
            name, value, size, base_type = desired[field.def_num]
            if field.size != size or field.base_type.identifier != base_type:
                raise FitError("Gerätefeld hat einen nicht unterstützten Typ oder eine ungültige Größe.")
            before = int.from_bytes(part, "little" if definition.endian == "<" else "big")
            replacement = value.to_bytes(size, "little" if definition.endian == "<" else "big")
            if part != replacement:
                hidden = name == "serial_number"
                changes.append(IdentityChange(frame.name, index, name, "replaced",
                                               None if hidden else before, None if hidden else value, hidden))
            part = replacement
            seen.add(field.def_num)
        parts.append(part)
    extra_definitions = bytearray()
    for number, (name, value, size, base_type) in desired.items():
        if number in seen:
            continue
        extra_definitions.extend((number, size, base_type))
        parts.append(value.to_bytes(size, "little" if definition.endian == "<" else "big"))
        hidden = name == "serial_number"
        changes.append(IdentityChange(frame.name, index, name, "added", None,
                                       None if hidden else value, hidden))
    # Developer fields follow native fields. Their bytes and definitions remain exact.
    payload = raw[:1] + b"".join(parts) + raw[offset:]
    if not extra_definitions:
        return payload, changes
    old_definition = definition.chunk.bytes
    count = len(fields) + len(extra_definitions) // 3
    if count > 255:
        raise FitError("Zu viele Felder für die Geräte-Konvertierung.")
    end_fields = 6 + len(fields) * 3
    new_definition = (old_definition[:5] + bytes([count]) + old_definition[6:end_fields]
                      + extra_definitions + old_definition[end_fields:])
    # A local definition is scoped to this message only. Restore it afterwards so
    # later records reusing the same local number keep their original layout.
    return new_definition + payload + old_definition, changes


def prepare_edge530(data: bytes, *, unit_id: int | None = None) -> tuple[bytes, IdentityReport]:
    if unit_id is not None and (type(unit_id) is not int or not 1 <= unit_id <= 0xFFFFFFFF):
        raise FitError("Unit-ID muss eine positive 32-Bit-Gerätekennung sein.")
    original = validate_fit(data)
    if not original.valid or len(original.session_times) != 1:
        raise FitError("Geräteprofil benötigt eine bestandene FIT-Prüfung mit genau einer Session.")
    deadline = time.monotonic() + VALIDATION_SECONDS
    body = bytearray()
    changes = []
    identity_messages = 0
    creators = 0
    device_warnings = []
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            with fitdecode.FitReader(io.BytesIO(data), keep_raw_chunks=True,
                                     check_crc=fitdecode.CrcCheck.RAISE,
                                     error_handling=fitdecode.ErrorHandling.RAISE) as reader:
                for index, frame in enumerate(reader):
                    if index >= MAX_FIT_FRAMES or time.monotonic() >= deadline:
                        raise FitError("Geräte-Konvertierung hat ihr Zeit- oder Nachrichtenlimit erreicht.")
                    if frame.frame_type in (fitdecode.FIT_FRAME_HEADER, fitdecode.FIT_FRAME_CRC):
                        continue
                    target = False
                    if frame.frame_type == fitdecode.FIT_FRAME_DATA:
                        if frame.global_mesg_num == 0:
                            if frame.get_raw_value("manufacturer", fallback=None) != MYWHOOSH:
                                raise FitError("Das experimentelle Profil erwartet MyWhoosh als Originalhersteller (331).")
                            target = True
                        elif frame.global_mesg_num == 23:
                            device_index = frame.get_raw_value("device_index", fallback=None)
                            if device_index is None:
                                raise FitError("device_info ohne gültigen device_index ist nicht eindeutig zuzuordnen.")
                            if device_index == 0:
                                manufacturer = frame.get_raw_value("manufacturer", fallback=None)
                                if manufacturer not in (None, MYWHOOSH):
                                    raise FitError("Gerätehersteller des Aufzeichners widerspricht dem MyWhoosh-Original.")
                                target = True
                                creators += 1
                                if frame.has_field("product_name") or frame.has_field("software_version"):
                                    device_warnings.append("Vorhandener Produktname und Softwareversion bleiben unverändert.")
                    if target:
                        identity_messages += 1
                        if identity_messages > MAX_IDENTITY_MESSAGES:
                            raise FitError("Zu viele Geräte-Nachrichten für das experimentelle Profil.")
                        replacement, updates = _rewrite(frame, unit_id=unit_id, index=index)
                        body.extend(replacement)
                        changes.extend(updates)
                    else:
                        body.extend(frame.chunk.bytes)
                    if len(body) + data[0] + 2 > MAX_FIT_BYTES:
                        raise FitError("Abgeleitete FIT-Datei überschreitet das Größenlimit.")
    except FitError:
        raise
    except Exception:
        raise FitError("FIT-Gerätefelder konnten nicht sicher umgeschrieben werden.") from None
    if not changes:
        raise FitError("Das Geräteprofil hat keine Änderungen ergeben.")
    header = bytearray(data[:data[0]])
    struct.pack_into("<I", header, 4, len(body))
    if len(header) == 14:
        struct.pack_into("<H", header, 12, compute_crc(header[:12]))
    output = bytes(header + body)
    output += struct.pack("<H", compute_crc(output))
    checked = validate_fit(output)
    if (not checked.valid or checked.session_times != original.session_times
            or checked.records != original.records or checked.first_record_utc != original.first_record_utc
            or checked.last_record_utc != original.last_record_utc):
        raise FitError("Abgeleitete FIT-Datei hat die Integritäts- oder Zeit-Gegenprüfung nicht bestanden.")
    notes = ["Experimentelles Geräteprofil; Training Effect und Trainingsstatus sind nicht garantiert."]
    if unit_id is None:
        notes.append("Keine eigene Unit-ID gesetzt; vorhandene Serienkennungen bleiben unverändert.")
    if not creators:
        notes.append("Keine device_info für den Aufzeichner vorhanden; nur file_id wurde geändert.")
    if not any(c.field == "serial_number" for c in changes) and unit_id is not None:
        notes.append("Die angegebene Unit-ID war bereits gesetzt.")
    return output, IdentityReport("edge530", original.sha256, checked.sha256, len(data), len(output),
                                   tuple(changes), tuple(notes + sorted(set(device_warnings)) + list(original.warnings)))
