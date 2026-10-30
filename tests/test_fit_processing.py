import hashlib
import os
from datetime import datetime, timedelta, timezone

import pytest

from mywhoosh_garmin_sync.errors import FitError
from mywhoosh_garmin_sync.fit_processing import prepare_original_path, read_fit, save_original, validate_fit
from mywhoosh_garmin_sync import fit_processing
from fit_factory import FIT_EPOCH, START, crc16, make_fit, wrap


@pytest.mark.parametrize("header_size,endian", [(12, "<"), (14, "<"), (14, ">")])
def test_valid_fit_is_parsed_without_modifying_bytes(header_size, endian):
    data = make_fit(header_size=header_size, endian=endian)
    digest = hashlib.sha256(data).hexdigest()
    report = validate_fit(data)
    assert report.valid, report.issues
    assert report.sha256 == digest == hashlib.sha256(data).hexdigest()
    assert report.records == 2
    assert report.sessions == 1
    assert report.first_record_utc == "2024-01-01T00:00:00+00:00"
    assert report.last_record_utc == "2024-01-01T00:01:00+00:00"
    assert report.issues == report.warnings == ()
    assert report.integrity_ok
    assert report.session_times[0].elapsed_end_utc == report.last_record_utc


@pytest.mark.parametrize("mutation", ["header-crc", "file-crc", "truncated", "trailing", "signature", "html"])
def test_corrupt_or_non_fit_file_rejected(mutation):
    data = bytearray(make_fit())
    if mutation == "header-crc":
        data[12] ^= 1
    elif mutation == "file-crc":
        data[-1] ^= 1
    elif mutation == "truncated":
        data = data[:-1]
    elif mutation == "trailing":
        data += b"extra"
    elif mutation == "signature":
        data[8] = 0
    else:
        data = b"<html>synthetic-private-error</html>"
    report = validate_fit(bytes(data))
    assert not report.valid
    assert report.issues
    assert "synthetic-private-error" not in str(report)
    if mutation.endswith("crc"):
        assert any("CRC" in issue for issue in report.issues)


@pytest.mark.parametrize("options,expected", [
    ({"file_type": 6}, "Trainingsaktivität"),
    ({"include_session": False}, "Session- und Record"),
    ({"times": ()}, "Session- und Record"),
    ({"times": (START + 60, START)}, "rückwärts"),
    ({"times": (START - 10, START)}, "vor der Session"),
    ({"times": (START, START + 70)}, "nach der Session"),
    ({"end": START - 1}, "Start- oder Nachrichtenzeiten"),
    ({"times": (0xFFFFFFFF,)}, "UTC-Zeitstempel"),
    ({"timer": 120_000}, "Dauerangaben"),
])
def test_semantic_problems_require_review(options, expected):
    report = validate_fit(make_fit(**options))
    assert not report.valid
    assert any(expected in issue for issue in report.issues)


def test_valid_crc_does_not_hide_broken_message_structure():
    report = validate_fit(wrap(b"\x00\x01\x02\x03"))
    assert not report.valid
    assert any("Nachrichtenstruktur" in issue for issue in report.issues)
    assert not report.integrity_ok


@pytest.mark.parametrize("summary_time", [START, START + 20, START + 180])
def test_session_span_uses_start_plus_elapsed_not_summary_timestamp(summary_time):
    report = validate_fit(make_fit(end=summary_time))
    assert report.valid, report.issues
    assert report.integrity_ok
    assert report.session_times[0].start_utc == "2024-01-01T00:00:00+00:00"
    assert report.session_times[0].elapsed_end_utc == "2024-01-01T00:01:00+00:00"
    assert report.session_times[0].elapsed_seconds == 60


def test_pause_time_is_included_in_session_span():
    report = validate_fit(make_fit(end=START, times=(START, START + 120), elapsed=120_000, timer=30_000))
    assert report.valid, report.issues
    assert report.session_times[0].elapsed_end_utc == "2024-01-01T00:02:00+00:00"
    assert report.session_times[0].timer_seconds == 30


def test_records_outside_elapsed_span_still_require_review():
    report = validate_fit(make_fit(end=START + 120, times=(START, START + 70)))
    assert not report.valid
    assert report.integrity_ok
    assert any("nach der Session" in issue for issue in report.issues)
    assert report.last_record_offset_seconds == 10


def test_session_limit_requires_review(monkeypatch):
    monkeypatch.setattr(fit_processing, "MAX_FIT_SESSIONS", 0)
    report = validate_fit(make_fit())
    assert not report.valid
    assert not report.integrity_ok
    assert any("Sessionlimit" in issue for issue in report.issues)


def test_missing_elapsed_does_not_use_summary_time_as_fallback():
    report = validate_fit(make_fit(end=START, elapsed=0xFFFFFFFF))
    assert report.valid, report.issues
    assert report.session_times[0].elapsed_end_utc is None
    assert any("nicht prüfbar" in warning for warning in report.warnings)


def test_missing_activity_summary_is_warning_only():
    report = validate_fit(make_fit(include_activity=False))
    assert report.valid
    assert report.warnings


def test_api_start_mismatch_is_reported_without_repair():
    data = make_fit()
    report = validate_fit(data, expected_start=datetime(2024, 1, 1, 1, tzinfo=timezone.utc))
    assert not report.valid
    assert any("fünf Minuten" in issue for issue in report.issues)
    assert data == make_fit()


def test_future_timestamps_require_review():
    future = int((datetime.now(timezone.utc) + timedelta(days=3) - FIT_EPOCH).total_seconds())
    report = validate_fit(make_fit(start=future, end=future + 60, times=(future, future + 60)))
    assert not report.valid
    assert any("Zeitstempel" in issue for issue in report.issues)


def test_optional_zero_header_crc_is_accepted():
    data = make_fit()
    without_header_crc = data[:12] + b"\x00\x00" + data[14:-2]
    data = without_header_crc + crc16(without_header_crc).to_bytes(2, "little")
    assert validate_fit(data).valid


def test_processing_deadline_requires_review(monkeypatch):
    clock = iter([0.0, 31.0])
    monkeypatch.setattr(fit_processing.time, "monotonic", lambda: next(clock))
    report = validate_fit(make_fit())
    assert not report.valid
    assert any("Zeit- oder Nachrichtenlimit" in issue for issue in report.issues)


def test_limits_fail_closed(monkeypatch):
    monkeypatch.setattr(fit_processing, "MAX_FIT_FRAMES", 2)
    report = validate_fit(make_fit())
    assert not report.valid
    assert any("Nachrichtenlimit" in issue for issue in report.issues)
    monkeypatch.setattr(fit_processing, "MAX_FIT_BYTES", 32)
    assert not validate_fit(make_fit()).valid


def test_save_original_is_private_exclusive_and_byte_identical(tmp_path):
    data = make_fit()
    target = prepare_original_path(tmp_path / "private", "synthetic-ride")
    save_original(data, target)
    assert read_fit(target) == data
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.parent.stat().st_mode & 0o777 == 0o700
    assert list(target.parent.iterdir()) == [target]
    with pytest.raises(FitError, match="existiert"):
        save_original(b"different", target)
    assert target.read_bytes() == data
    with pytest.raises(FitError, match="existiert"):
        prepare_original_path(target.parent, "synthetic-ride")


def test_invalid_fit_original_still_preserved(tmp_path):
    target = prepare_original_path(tmp_path / "private", "synthetic-ride")
    data = b"not a valid FIT file"
    save_original(data, target)
    assert not validate_fit(read_fit(target)).valid
    assert target.read_bytes() == data


@pytest.mark.parametrize("kind", ["symlink", "permissions", "path-traversal"])
def test_unsafe_output_rejected(tmp_path, kind):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    if kind == "symlink":
        link = tmp_path / "link"
        link.symlink_to(directory, target_is_directory=True)
        directory = link
    elif kind == "permissions":
        directory.chmod(0o755)
    with pytest.raises(FitError):
        prepare_original_path(directory, "../escape" if kind == "path-traversal" else "ride")


def test_partial_write_does_not_publish_or_leave_temporary(tmp_path, monkeypatch):
    target = prepare_original_path(tmp_path / "private", "synthetic-ride")

    def fail(*args):
        raise OSError("synthetic-private-error")

    monkeypatch.setattr(os, "link", fail)
    with pytest.raises(FitError) as error:
        save_original(make_fit(), target)
    assert "synthetic-private-error" not in str(error.value)
    assert list(target.parent.iterdir()) == []


def test_validate_refuses_symlink_and_fifo_without_blocking(tmp_path):
    actual = tmp_path / "source.fit"
    actual.write_bytes(make_fit())
    link = tmp_path / "link.fit"
    link.symlink_to(actual)
    with pytest.raises(FitError):
        read_fit(link)
    fifo = tmp_path / "fifo.fit"
    os.mkfifo(fifo)
    with pytest.raises(FitError):
        read_fit(fifo)
