import io
from datetime import datetime, timezone

import fitdecode
import pytest

from mywhoosh_garmin_sync import fit_time
from mywhoosh_garmin_sync.errors import FitError
from mywhoosh_garmin_sync.fit_identity import prepare_edge530
from mywhoosh_garmin_sync.fit_processing import validate_fit
from fit_factory import FIT_EPOCH, START, make_time_fit, wrap


@pytest.mark.parametrize("clock,expected", [
    ("2024-01-15T18:30:00", "2024-01-15T17:30:00Z"),
    ("2024-07-15T18:30:00", "2024-07-15T16:30:00Z"),
    ("2024-03-31T02:30:00", None),
    ("2024-10-27T02:30:00", None),
])
def test_automatic_local_policy_uses_date_offset_and_refuses_ambiguous_clocks(clock, expected):
    raw = int((datetime.fromisoformat(clock).replace(tzinfo=FIT_EPOCH.tzinfo) - FIT_EPOCH).total_seconds())
    source = make_time_fit(start=raw)
    output, report = fit_time.prepare_automatic_time_repair(source, timezone_name="Europe/Berlin")
    if expected:
        assert report.status == "ready" and report.start_utc == expected
        assert report.accepted_proposal is None and report.reason_code == "automatic_local"
        assert validate_fit(output).valid
    else:
        assert report.status == "needs_review" and output == source and not report.changes


def test_automatic_adjustment_uses_recording_duration_when_faulty_local_finish_drifts():
    source = make_time_fit(local=START + 120 + fit_time.UNIX_FIT_OFFSET)
    output, report = fit_time.prepare_automatic_time_repair(source, timezone_name="Europe/Berlin")
    assert output != source and report.status == "ready"
    assert report.start_utc == "2023-12-31T23:00:00Z"
    assert report.changes[-1].after == "2024-01-01T00:01:00"


def frames(data):
    with fitdecode.FitReader(io.BytesIO(data), keep_raw_chunks=True,
                             check_crc=fitdecode.CrcCheck.RAISE) as reader:
        return [f for f in reader if f.frame_type not in (fitdecode.FIT_FRAME_HEADER, fitdecode.FIT_FRAME_CRC)]


@pytest.mark.parametrize("endian,header_size", [("<", 12), ("<", 14), (">", 12), (">", 14)])
def test_only_activity_clock_bytes_change_with_crc_unknown_data_and_measurements_preserved(endian, header_size):
    data = make_time_fit(endian=endian, header_size=header_size)
    output, report = fit_time.prepare_time_repair(data, timezone_name="Europe/Berlin")
    assert report.status == "ready"
    before, after = validate_fit(data), validate_fit(output)
    assert after.valid and after.integrity_ok
    assert before.warnings and not after.warnings
    assert before.session_times == after.session_times
    assert before.records == after.records
    assert before.first_record_utc == after.first_record_utc
    assert before.last_record_utc == after.last_record_utc
    for original, converted in zip(frames(data), frames(output), strict=True):
        if original.frame_type == fitdecode.FIT_FRAME_DATA and original.name == "activity":
            assert converted.get_raw_value("timestamp") == START + 60
            assert converted.get_raw_value("local_timestamp") == START + 60 + 3600
            assert converted.get_raw_value(199) == original.get_raw_value(199)
            assert converted.get_value("total_timer_time") == original.get_value("total_timer_time")
        else:
            assert original.chunk.bytes == converted.chunk.bytes
    assert [c.field for c in report.changes] == ["activity.timestamp", "activity.local_timestamp"]
    assert report.changes[0].after.endswith("Z")
    assert not report.changes[1].after.endswith("Z")
    repeated, again = fit_time.prepare_time_repair(output, timezone_name="UTC")
    assert repeated == output and again.status == "unchanged" and not again.changes
    # Independent time repair can be composed with the unchanged device profile.
    converted, _ = prepare_edge530(output, unit_id=222222222)
    assert validate_fit(converted).valid


@pytest.mark.parametrize("end,offset", [
    ("2024-01-01T12:00:00+00:00", 3600),
    ("2024-07-01T12:00:00+00:00", 7200),
    ("2024-03-31T00:30:00+00:00", 3600),
    ("2024-03-31T01:30:00+00:00", 7200),
    ("2024-10-27T00:30:00+00:00", 7200),
    ("2024-10-27T01:30:00+00:00", 3600),
])
def test_timezone_uses_workout_date_including_both_dst_transitions(end, offset):
    end_raw = int((datetime.fromisoformat(end) - FIT_EPOCH).total_seconds())
    output, report = fit_time.prepare_time_repair(make_time_fit(start=end_raw - 60), timezone_name="Europe/Berlin")
    assert report.status == "ready"
    activity = next(f for f in frames(output) if f.frame_type == fitdecode.FIT_FRAME_DATA and f.name == "activity")
    assert activity.get_raw_value("local_timestamp") - activity.get_raw_value("timestamp") == offset


@pytest.mark.parametrize("zone,offset", [("UTC", 0), ("Asia/Kathmandu", 20700), ("America/New_York", -18000)])
def test_other_explicit_timezones_are_supported(zone, offset):
    _, report = fit_time.prepare_time_repair(make_time_fit(), timezone_name=zone)
    assert report.changes[-1].after_raw == START + 60 + offset


@pytest.mark.parametrize("options", [
    {"include_stop": False}, {"stop_offset": 65}, {"stop_offset": 58},
    {"last_offset": 50}, {"activity_time": START + 10},
    {"activity_timer": 59_000}, {"num_sessions": 2},
    {"local": START + 500 + 631065600},
    {"local": START + 59 + 631065600 + 7200},
    {"local": START + 59},
])
def test_missing_conflicting_or_unproven_inputs_are_not_repaired(options):
    data = make_time_fit(**options)
    output, report = fit_time.prepare_time_repair(data, timezone_name="Europe/Berlin")
    assert output == data and report.status == "needs_review"
    assert not report.changes and report.source_sha256 == report.output_sha256


@pytest.mark.parametrize("manufacturer", [1, 123, 0xFFFF])
def test_never_repairs_non_mywhoosh_recordings(manufacturer):
    data = make_time_fit(manufacturer=manufacturer)
    output, report = fit_time.prepare_time_repair(data, timezone_name="UTC")
    assert output == data and report.status == "not_applicable"


def test_correct_mywhoosh_recording_is_preserved_even_if_requested_timezone_differs():
    data = make_time_fit(local=START + 60 + 7200, activity_time=START + 60)
    output, report = fit_time.prepare_time_repair(data, timezone_name="UTC")
    assert output == data and report.status == "unchanged"


@pytest.mark.parametrize("zone", ["Not/AZone", "/etc/passwd", ""])
def test_invalid_timezone_is_reported_without_echoing_input(zone):
    with pytest.raises(FitError, match="IANA-Zeitzone"):
        fit_time.prepare_time_repair(make_time_fit(), timezone_name=zone)


def test_bad_crc_and_unsupported_field_type_are_rejected():
    data = make_time_fit()
    with pytest.raises(FitError, match="CRC"):
        fit_time.prepare_time_repair(data[:-1], timezone_name="UTC")
    with pytest.raises(FitError, match="uint32"):
        fit_time.prepare_time_repair(make_time_fit(local_type=0x8C), timezone_name="UTC")


def test_duplicate_activity_requires_review():
    data = make_time_fit()
    body = data[14:-2] + next(f.chunk.bytes for f in frames(data)
                             if f.frame_type == fitdecode.FIT_FRAME_DATA and f.name == "activity")
    duplicated = wrap(body)
    output, report = fit_time.prepare_time_repair(duplicated, timezone_name="UTC")
    assert output == duplicated and report.status == "needs_review"


def test_limits_are_enforced(monkeypatch):
    monkeypatch.setattr(fit_time, "MAX_FIT_FRAMES", 1)
    with pytest.raises(FitError, match="limit"):
        fit_time.prepare_time_repair(make_time_fit(), timezone_name="UTC")


def test_missing_timezone_detects_proven_error_without_applying_any_correction():
    source = make_time_fit()
    output, report = fit_time.prepare_time_repair(source, timezone_name=None)
    assert report.status == "needs_timezone" and report.timezone is None
    assert output == source and not report.changes


def test_invalid_present_local_field_requires_review():
    source = make_time_fit(local=0xFFFFFFFF)
    output, report = fit_time.prepare_time_repair(source, timezone_name="UTC")
    assert output == source and report.status == "needs_review"


@pytest.mark.parametrize("options,code", [({"include_stop": False}, "missing_stop"),
                                        ({"stop_offset": 65}, "conflicting_end"),
                                        ({"local": START + 631065600}, "unconfirmed_epoch"),
                                        ({"local": START + 59 + 631065600 + 7200}, "unconfirmed_epoch")])
def test_review_has_evidence_and_explicitly_accepted_proposals_preserve_other_bytes(options, code):
    source = make_time_fit(**options)
    untouched, report = fit_time.prepare_time_repair(source, timezone_name="Europe/Berlin")
    assert untouched == source and report.status == "needs_review" and report.reason_code == code
    assert len(report.observations) >= 9 and report.proposals
    for candidate in report.proposals:
        if candidate.interpretation != "utc":
            continue
        output, approved = fit_time.prepare_time_repair(source, timezone_name="Europe/Berlin", proposal_id=candidate.id)
        assert approved.status == "ready" and approved.accepted_proposal == candidate.id
        assert approved.changes == candidate.changes
        assert validate_fit(output).valid
        for old, new in zip(frames(source), frames(output), strict=True):
            if old.frame_type == fitdecode.FIT_FRAME_DATA and old.name == "activity":
                for number in (0, 1, 199):
                    assert old.get_raw_value(number) == new.get_raw_value(number)
            else:
                assert old.chunk.bytes == new.chunk.bytes


def test_conflicting_stop_offers_distinct_recorded_alternatives():
    _, report = fit_time.prepare_time_repair(make_time_fit(stop_offset=65), timezone_name="UTC")
    assert len([p for p in report.proposals if p.interpretation == "utc"]) == 2
    assert {p.finish_utc for p in report.proposals} == {"2024-01-01T00:01:00Z", "2024-01-01T00:01:05Z"}


@pytest.mark.parametrize("options", [{"manufacturer": 1}, {"num_sessions": 2},
                                    {"include_stop": False, "last_offset": 0, "local": START + 3 * 86400 + 631065600}])
def test_unsupported_sources_never_get_a_bypass_proposal(options):
    source = make_time_fit(**options)
    output, report = fit_time.prepare_time_repair(source, timezone_name="UTC")
    assert output == source and not report.proposals
    with pytest.raises(FitError):
        fit_time.prepare_time_repair(source, timezone_name="UTC", proposal_id="invented")


def test_proposal_cannot_be_reused_with_a_different_timezone_or_original():
    source = make_time_fit(include_stop=False)
    _, report = fit_time.prepare_time_repair(source, timezone_name="UTC")
    identity = report.proposals[0].id
    for data, zone in ((source, "Europe/Berlin"), (make_time_fit(include_stop=False, activity_timer=59_000), "UTC")):
        with pytest.raises(FitError):
            fit_time.prepare_time_repair(data, timezone_name=zone, proposal_id=identity)
