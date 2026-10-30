import io
import json
from dataclasses import asdict

import fitdecode
import pytest

from mywhoosh_garmin_sync import fit_identity
from mywhoosh_garmin_sync.errors import FitError
from mywhoosh_garmin_sync.fit_processing import validate_fit
from fit_factory import definition, make_fit, make_identity_fit, wrap


def messages(data):
    with fitdecode.FitReader(io.BytesIO(data), keep_raw_chunks=True,
                             check_crc=fitdecode.CrcCheck.RAISE) as reader:
        return [m for m in reader if m.frame_type == fitdecode.FIT_FRAME_DATA]


@pytest.mark.parametrize("header_size,endian", [(12, "<"), (14, "<"), (12, ">"), (14, ">")])
@pytest.mark.parametrize("missing_fields", [False, True])
@pytest.mark.parametrize("unit_id", [None, 222222222])
def test_only_identity_fields_change_and_unknown_data_is_preserved(header_size, endian, missing_fields, unit_id):
    source = make_identity_fit(header_size=header_size, endian=endian, missing_fields=missing_fields)
    output, report = fit_identity.prepare_edge530(source, unit_id=unit_id)
    assert validate_fit(output).valid
    assert validate_fit(output).records == validate_fit(source).records
    assert validate_fit(output).session_times == validate_fit(source).session_times
    assert report.source_sha256 == validate_fit(source).sha256
    assert report.output_sha256 == validate_fit(output).sha256
    assert source != output
    original_messages, converted_messages = messages(source), messages(output)
    assert len(original_messages) == len(converted_messages)
    for original, converted in zip(original_messages, converted_messages, strict=True):
        changed = (original.name == "file_id" or
                   original.name == "device_info" and original.get_raw_value("device_index") == 0)
        if not changed:
            assert converted.chunk.bytes == original.chunk.bytes
            continue
        assert converted.get_raw_value("manufacturer") == 1
        assert converted.get_raw_value("garmin_product") == 3121
        assert converted.get_value("garmin_product") == "edge_530"
        if unit_id is not None:
            assert converted.get_raw_value("serial_number") == unit_id
        else:
            assert converted.get_raw_value("serial_number", fallback=None) == original.get_raw_value("serial_number", fallback=None)
        for field in original.def_mesg.field_defs:
            if field.name in ("manufacturer", "product", "serial_number"):
                continue
            assert converted.get_raw_value(field.def_num) == original.get_raw_value(field.def_num)
    # Native device fields added to the creator must not bleed into its sensor,
    # which uses the same original local definition immediately afterwards.
    sensor = [m for m in converted_messages if m.name == "device_info" and m.get_raw_value("device_index") == 1][0]
    assert sensor.get_raw_value("manufacturer") == 123
    assert sensor.has_field("product") == (not missing_fields)
    serialized = json.dumps(asdict(report))
    for private_id in ("111111111", "222222222", "333333333"):
        assert private_id not in serialized
    assert report.changes


def test_missing_creator_does_not_turn_sensors_into_recorders():
    source = make_identity_fit(creator=False)
    output, report = fit_identity.prepare_edge530(source)
    assert any("Keine device_info" in text for text in report.warnings)
    assert all(c.message == "file_id" for c in report.changes)
    assert [m.chunk.bytes for m in messages(source) if m.name == "device_info"] == [
        m.chunk.bytes for m in messages(output) if m.name == "device_info"]


@pytest.mark.parametrize("options", [{"manufacturer": 1}, {"manufacturer": 0xFFFF},
                                    {"creator_manufacturer": 123}, {"creator_index": 0xFF},
                                    {"manufacturer_type": 0x86, "manufacturer_size": 4}])
def test_ambiguous_origin_or_malformed_identity_is_rejected(options):
    with pytest.raises(FitError):
        fit_identity.prepare_edge530(make_identity_fit(**options))


@pytest.mark.parametrize("value", [0, -1, True, 0x100000000, "222222222"])
def test_invalid_unit_id_is_rejected_without_echo(value):
    with pytest.raises(FitError, match="positive 32-Bit") as error:
        fit_identity.prepare_edge530(make_identity_fit(), unit_id=value)
    assert "222222222" not in str(error.value)


@pytest.mark.parametrize("source", [b"not-fit", make_fit(), make_identity_fit()[:-1]])
def test_invalid_or_unattributed_input_is_rejected(source):
    with pytest.raises(FitError):
        fit_identity.prepare_edge530(source)


def test_conversion_cannot_be_applied_again_to_an_already_converted_file():
    converted, _ = fit_identity.prepare_edge530(make_identity_fit())
    with pytest.raises(FitError, match="Originalhersteller"):
        fit_identity.prepare_edge530(converted)


def test_identity_count_limit_stops_conversion(monkeypatch):
    monkeypatch.setattr(fit_identity, "MAX_IDENTITY_MESSAGES", 1)
    with pytest.raises(FitError, match="Zu viele"):
        fit_identity.prepare_edge530(make_identity_fit())


def test_added_identity_fields_preserve_developer_definitions_and_payloads():
    source = make_identity_fit(missing_fields=True)
    body = bytearray()
    with fitdecode.FitReader(io.BytesIO(source), keep_raw_chunks=True) as reader:
        for frame in reader:
            if frame.frame_type in (fitdecode.FIT_FRAME_HEADER, fitdecode.FIT_FRAME_CRC):
                continue
            if frame.frame_type == fitdecode.FIT_FRAME_DEFINITION and frame.global_mesg_num == 23:
                body.extend(definition(7, 207, [(3, 1, 0x02)]) + b"\x07\x00")
                body.extend(definition(8, 206, [(0, 1, 0x02), (1, 1, 0x02), (2, 1, 0x02)]) + b"\x08\x00\x00\x84")
                body.extend(bytes([frame.chunk.bytes[0] | 0x20]) + frame.chunk.bytes[1:] + b"\x01\x00\x02\x00")
            else:
                body.extend(frame.chunk.bytes)
                if frame.frame_type == fitdecode.FIT_FRAME_DATA and frame.global_mesg_num == 23:
                    body.extend(b"\xCD\xAB")
    source = wrap(body)
    assert validate_fit(source).valid
    output, report = fit_identity.prepare_edge530(source, unit_id=222222222)
    for before, after in zip(messages(source), messages(output), strict=True):
        if before.name == "device_info":
            original_developer = [(f.field_def.def_num, f.raw_value) for f in before if f.field_def.is_dev]
            output_developer = [(f.field_def.def_num, f.raw_value) for f in after if f.field_def.is_dev]
            assert original_developer == output_developer == [(0, 0xABCD)]
            assert before.def_mesg.dev_field_defs[0].size == after.def_mesg.dev_field_defs[0].size
        elif before.name != "file_id":
            assert before.chunk.bytes == after.chunk.bytes


def test_derived_size_limit_stops_field_additions(monkeypatch):
    source = make_identity_fit(missing_fields=True)
    monkeypatch.setattr(fit_identity, "MAX_FIT_BYTES", len(source))
    with pytest.raises(FitError, match="Größenlimit"):
        fit_identity.prepare_edge530(source, unit_id=222222222)


def test_new_identity_hash_does_not_bypass_existing_activity_check():
    import httpx
    from mywhoosh_garmin_sync.garmin import GarminUploader
    from test_garmin import FakeAuth, row

    converted, report = fit_identity.prepare_edge530(make_identity_fit(), unit_id=222222222)
    assert report.source_sha256 != report.output_sha256
    calls = []

    def existing_activity(request):
        calls.append(request)
        assert request.method == "GET"
        return httpx.Response(200, json=[row()])

    with GarminUploader(backend=FakeAuth(), transport=httpx.MockTransport(existing_activity)) as uploader:
        uploader.login("example@example.invalid", "synthetic-password", lambda: "123456")
        with pytest.raises(FitError, match="Upload gesperrt"):
            uploader.upload_once(converted, before_send=lambda: pytest.fail("Existing activity must block upload"))
    assert len(calls) == 1
