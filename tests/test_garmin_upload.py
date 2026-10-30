import json

import httpx
import pytest

from mywhoosh_garmin_sync import garmin
from mywhoosh_garmin_sync.errors import AuthError, FitError, SessionError
from fit_factory import make_fit
from test_garmin import FakeAuth, row


def accepted(identity=123, **changes):
    return {"detailedImportResult": {"successes": [{"internalId": identity}], "failures": [], **changes}}


def detail(**changes):
    source = row(**changes)
    return {"activityId": source.pop("activityId"), "activityName": source.pop("activityName"),
            "summaryDTO": source}


def uploader(handler):
    client = garmin.GarminUploader(backend=FakeAuth(), transport=httpx.MockTransport(handler))
    client.login("example@example.invalid", "synthetic-password", lambda: "123456")
    return client


def test_upload_exact_validated_bytes_once_and_verify_returned_id():
    data = make_fit()
    calls = []
    marker = []

    def handle(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer synthetic-token"
        assert request.extensions["timeout"] == {"connect": 5.0, "read": 15.0, "write": 15.0, "pool": 15.0}
        if request.method == "POST":
            assert marker == ["pending"]
            assert str(request.url) == garmin.UPLOAD_URL
            assert request.headers["Content-Type"].startswith("multipart/form-data;")
            # Parse multipart so the bytes are compared independently of httpx's encoding.
            from email.parser import BytesParser
            from email.policy import default
            message = BytesParser(policy=default).parsebytes(
                f"Content-Type: {request.headers['Content-Type']}\r\n\r\n".encode() + request.content)
            parts = list(message.iter_parts())
            assert len(parts) == 1
            assert parts[0].get_param("name", header="content-disposition") == "file"
            assert parts[0].get_filename() == "activity.fit"
            assert parts[0].get_payload(decode=True) == data
            return httpx.Response(201, json=accepted())
        if str(request.url).startswith(garmin.ACTIVITIES_URL):
            assert dict(request.url.params) == {"start": "0", "limit": "100"}
            return httpx.Response(200, json=[])
        assert str(request.url) == garmin.ACTIVITY_URL + "123"
        return httpx.Response(200, json=detail())

    with uploader(handle) as client:
        result = client.upload_once(data, before_send=lambda: marker.append("pending"))
        assert result == garmin.UploadResult("uploaded", "123", "import_confirmed_and_times_matched")
        with pytest.raises(SessionError, match="bereits ein Upload"):
            client.upload_once(data, before_send=lambda: pytest.fail("Do not start another attempt"))
    assert [r.method for r in calls] == ["GET", "POST", "GET"]


@pytest.mark.parametrize("case", ["timeout", "disconnect", "401", "403", "409", "429", "500", "302", "202",
                                 "invalid_json", "html", "compressed", "oversized"])
def test_unclear_upload_is_never_retried_or_followed(case, monkeypatch):
    monkeypatch.setattr(garmin, "MAX_RESPONSE_BYTES", 1024)
    calls = []

    def handle(request):
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=[])
        if case == "timeout":
            raise httpx.ReadTimeout("synthetic-private-error", request=request)
        if case == "disconnect":
            raise httpx.RemoteProtocolError("synthetic-private-error", request=request)
        if case.isdecimal():
            return httpx.Response(int(case), json=accepted(), headers={"Location": "https://example.invalid/secret"})
        if case == "invalid_json":
            return httpx.Response(200, content=b"synthetic-private-error", headers={"Content-Type": "application/json"})
        if case == "html":
            return httpx.Response(200, text="synthetic-private-error")
        if case == "compressed":
            return httpx.Response(200, content=b"", headers={"Content-Encoding": "gzip"})
        return httpx.Response(200, content=b"x" * 1025, headers={"Content-Type": "application/json"})

    with uploader(handle) as client:
        result = client.upload_once(make_fit(), before_send=lambda: None)
        assert result.status == "needs_review"
        expected = {"timeout": "timeout", "disconnect": "transport_error", "invalid_json": "invalid_json",
                    "html": "response_not_json", "compressed": "response_compressed", "oversized": "response_too_large"}
        assert result.reason == "upload_" + (f"http_{case}" if case.isdecimal() else expected[case])
        assert "synthetic-private-error" not in json.dumps(result.__dict__)
        with pytest.raises(SessionError):
            client.upload_once(make_fit(), before_send=lambda: None)
    assert [r.method for r in calls] == ["GET", "POST"]


@pytest.mark.parametrize("body", [None, {}, {"detailedImportResult": []}, accepted(successes=[]),
                                  accepted(successes=[{}, {}]), accepted(failures=[{"secret": "private"}]),
                                  accepted(successes=[True]), accepted(True), accepted(0), accepted("../secret"),
                                  accepted("1" * 65), accepted(successes=[{}]),
                                  {"detailedImportResult": {"successes": [{"internalId": 123}]}}])
def test_import_response_requires_one_id_and_explicitly_no_failures(body):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=[] if request.method == "GET" else body)

    with uploader(handle) as client:
        assert client.upload_once(make_fit(), before_send=lambda: None).status == "needs_review"
    assert [r.method for r in calls] == ["GET", "POST"]


@pytest.mark.parametrize("verification", ["timeout", "404", "missing_start", "wrong_start", "wrong_duration", "wrong_id",
                                          "unknown_schema", "invalid_summary"])
def test_upload_success_requires_separate_readback(verification):
    calls = []

    def handle(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(200, json=accepted())
        if str(request.url).startswith(garmin.ACTIVITIES_URL):
            return httpx.Response(200, json=[])
        if verification == "timeout":
            raise httpx.ReadTimeout("synthetic-private-error", request=request)
        if verification == "404":
            return httpx.Response(404, json={})
        if verification == "unknown_schema":
            return httpx.Response(200, json=row())
        if verification == "invalid_summary":
            return httpx.Response(200, json={"activityId": 123, "summaryDTO": []})
        overrides = {"missing_start": {"startTimeGMT": None},
                     "wrong_start": {"startTimeGMT": "2024-01-02 00:00:00"},
                     "wrong_duration": {"duration": 999, "elapsedDuration": 999},
                     "wrong_id": {"activityId": 456}}[verification]
        return httpx.Response(200, json=detail(**overrides))

    with uploader(handle) as client:
        result = client.upload_once(make_fit(), before_send=lambda: None)
    assert result.status == "needs_review"
    assert result.activity_id == "123"
    if verification == "404":
        assert result.reason == "verification_http_404"
    if verification == "timeout":
        assert result.reason == "verification_timeout"
    assert [r.method for r in calls] == ["GET", "POST", "GET"]


@pytest.mark.parametrize("rows", [[row()], [row(), row(124)], [row(startTimeGMT=None)],
                                 [row(duration=None, elapsedDuration=None)]])
def test_candidate_or_incomplete_preflight_blocks_upload_and_journal(rows):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=rows)

    with uploader(handle) as client:
        with pytest.raises(FitError, match="Upload gesperrt"):
            client.upload_once(make_fit(), before_send=lambda: pytest.fail("Must not create attempt"))
    assert [r.method for r in calls] == ["GET"]


def test_bad_fit_or_missing_login_never_attempts_write():
    with garmin.GarminUploader(backend=FakeAuth()) as client:
        with pytest.raises(AuthError):
            client.upload_once(make_fit(), before_send=lambda: pytest.fail("No login"))
        with pytest.raises(FitError):
            client.upload_once(b"not-fit", before_send=lambda: pytest.fail("Invalid FIT"))


def test_failed_durable_marker_blocks_post():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=[])

    def failed_marker():
        raise SessionError("synthetic-disk-failure")

    with uploader(handle) as client:
        with pytest.raises(SessionError, match="synthetic-disk-failure"):
            client.upload_once(make_fit(), before_send=failed_marker)
    assert [r.method for r in calls] == ["GET"]
