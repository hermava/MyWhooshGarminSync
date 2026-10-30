import io
import struct
from datetime import datetime

import fitdecode
import pytest

from mywhoosh_garmin_sync.fit_time import prepare_time_repair
from mywhoosh_garmin_sync.fit_processing import validate_fit
from mywhoosh_garmin_sync.errors import FitError
from fit_factory import FIT_EPOCH, START, definition, make_time_fit, wrap


def messages(data):
    with fitdecode.FitReader(io.BytesIO(data), keep_raw_chunks=True, check_crc=fitdecode.CrcCheck.RAISE) as reader:
        return [f for f in reader if f.frame_type == fitdecode.FIT_FRAME_DATA]


def local_proposal(data, zone="Europe/Berlin"):
    _, report = prepare_time_repair(data, timezone_name=zone)
    return next(p for p in report.proposals if p.interpretation == "local")


@pytest.mark.parametrize("endian,header_size", [("<", 12), ("<", 14), (">", 12), (">", 14)])
def test_complete_timeline_preserves_every_non_time_field_and_unknown_message(endian, header_size):
    source = make_time_fit(endian=endian, header_size=header_size)
    extra = definition(5, 19, [(253, 4, 0x86), (2, 4, 0x86), (7, 4, 0x86), (9, 4, 0x86)], endian)
    extra += b"\x05" + struct.pack(endian + "IIII", START + 60, START, 60_000, 12345)
    extra += definition(9, 0xFF01, [(253, 4, 0x86), (7, 3, 0x0D)], endian)
    extra += b"\x09" + struct.pack(endian + "I", START + 30) + b"\x12\x34\x56"
    source = wrap(source[header_size:-2] + extra, header_size)
    proposal = local_proposal(source)
    output, report = prepare_time_repair(source, timezone_name="Europe/Berlin", proposal_id=proposal.id)
    assert report.interpretation == "local" and report.shifted_fields >= 10
    before, after = messages(source), messages(output)
    for old, new in zip(before, after, strict=True):
        for field in old.fields:
            new_raw = new.get_raw_value(field.def_num)
            absolute = field.type.name == "date_time" or field.def_num == 253
            if old.name == "activity" and field.def_num == 253:
                assert new_raw == START + 60 - 3600
            elif old.name == "activity" and field.def_num == 5:
                assert new_raw == START + 60
            elif absolute:
                assert new_raw == field.raw_value - 3600
            else:
                assert new_raw == field.raw_value
    original, corrected = validate_fit(source), validate_fit(output)
    assert corrected.valid and corrected.integrity_ok
    assert corrected.session_times[0].elapsed_seconds == original.session_times[0].elapsed_seconds
    assert corrected.session_times[0].timer_seconds == original.session_times[0].timer_seconds
    assert corrected.first_record_utc == "2023-12-31T23:00:00+00:00"
    assert corrected.last_record_utc == "2023-12-31T23:00:59+00:00"
    with pytest.raises(FitError):
        prepare_time_repair(output, timezone_name="Europe/Berlin", proposal_id=proposal.id)


@pytest.mark.parametrize("start,offset", [("2024-01-15T18:30:00", 3600), ("2024-07-15T18:30:00", 7200)])
def test_workout_date_selects_winter_or_summer_offset(start, offset):
    raw = int((datetime.fromisoformat(start).replace(tzinfo=FIT_EPOCH.tzinfo) - FIT_EPOCH).total_seconds())
    source = make_time_fit(start=raw)
    proposal = local_proposal(source)
    output, report = prepare_time_repair(source, timezone_name="Europe/Berlin", proposal_id=proposal.id)
    assert report.start_local == start
    assert next(m for m in messages(output) if m.name == "record").get_raw_value("timestamp") == raw - offset


@pytest.mark.parametrize("start", ["2024-03-31T02:30:00", "2024-10-27T02:30:00"])
def test_nonexistent_or_ambiguous_local_clock_never_gets_guessed(start):
    raw = int((datetime.fromisoformat(start).replace(tzinfo=FIT_EPOCH.tzinfo) - FIT_EPOCH).total_seconds())
    source = make_time_fit(start=raw)
    _, report = prepare_time_repair(source, timezone_name="Europe/Berlin")
    assert not [p for p in report.proposals if p.interpretation == "local"]


@pytest.mark.parametrize("endian", ["<", ">"])
def test_compressed_record_timestamps_are_expanded_without_changing_power(endian):
    source = make_time_fit(endian=endian, last_offset=19)
    body = bytearray()
    with fitdecode.FitReader(io.BytesIO(source), keep_raw_chunks=True) as reader:
        for frame in reader:
            if frame.frame_type in (fitdecode.FIT_FRAME_HEADER, fitdecode.FIT_FRAME_CRC):
                continue
            if frame.frame_type == fitdecode.FIT_FRAME_DATA and frame.name == "record" and frame.get_raw_value("timestamp") != START:
                body.extend(definition(2, 20, [(7, 2, 0x84)], endian))
                for offset in (19, 29):
                    body.extend(bytes([0x80 | (2 << 5) | ((START + offset) & 31)]) + struct.pack(endian + "H", 150))
            else:
                body.extend(frame.chunk.bytes)
    source = wrap(body)
    assert validate_fit(source).valid
    proposal = local_proposal(source)
    output, _ = prepare_time_repair(source, timezone_name="Europe/Berlin", proposal_id=proposal.id)
    records = [m for m in messages(output) if m.name == "record"]
    assert [r.get_raw_value("timestamp") for r in records] == [START + n - 3600 for n in (0, 19, 29)]
    assert [r.get_value("power") for r in records] == [150, 150, 150]
    assert validate_fit(output).valid


def test_developer_payload_bytes_are_preserved():
    source = make_time_fit()
    body = bytearray(definition(7, 207, [(3, 1, 0x02)]) + b"\x07\x00")
    body.extend(definition(8, 206, [(0, 1, 0x02), (1, 1, 0x02), (2, 1, 0x02)]) + b"\x08\x00\x00\x84")
    with fitdecode.FitReader(io.BytesIO(source), keep_raw_chunks=True) as reader:
        for frame in reader:
            if frame.frame_type in (fitdecode.FIT_FRAME_HEADER, fitdecode.FIT_FRAME_CRC):
                continue
            chunk = frame.chunk.bytes
            if frame.frame_type == fitdecode.FIT_FRAME_DEFINITION and frame.global_mesg_num == 20:
                chunk = bytes([chunk[0] | 0x20]) + chunk[1:] + b"\x01\x00\x02\x00"
            body.extend(chunk)
            if frame.frame_type == fitdecode.FIT_FRAME_DATA and frame.name == "record":
                body.extend(b"\xCD\xAB")
    source = wrap(body)
    proposal = local_proposal(source)
    output, _ = prepare_time_repair(source, timezone_name="Europe/Berlin", proposal_id=proposal.id)
    for frame in messages(output):
        if frame.name == "record":
            assert frame.chunk.bytes[-2:] == b"\xCD\xAB"
    assert validate_fit(output).valid
