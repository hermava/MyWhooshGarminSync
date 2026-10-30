import json
from datetime import datetime, timezone

import httpx
import pytest

from mywhoosh_garmin_sync.auth import Session
from mywhoosh_garmin_sync.errors import AuthError, NetworkError, ProtocolError, RateLimitError
from mywhoosh_garmin_sync.mywhoosh import ACTIVITIES_URL, LOGIN_URL, MyWhooshClient
from mywhoosh_garmin_sync import mywhoosh


def activity(identity="ride-1", **overrides):
    return {"id": identity, "title": "Evening Ride", "startDatetime": "2026-10-23T14:00:00Z",
            "rideDuration": "00:45:00.000", **overrides}


def page(entries, total=1):
    return {"data": {"results": entries, "totalPages": total}}


def test_login_contract_and_session():
    def handle(request):
        assert str(request.url) == LOGIN_URL
        assert request.method == "POST"
        payload = json.loads(request.content)
        assert payload["Username"] == "example@example.invalid"
        assert payload["Password"] == "synthetic-password"
        assert payload["Action"] == 1001
        assert payload["Platform"] == "Android"
        assert payload["DeviceId"] != payload["CorrelationId"]
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"Success": True, "AccessToken": "synthetic-token", "WhooshId": "rider"})

    with MyWhooshClient(transport=httpx.MockTransport(handle)) as client:
        assert client.login("example@example.invalid", "synthetic-password") == Session("synthetic-token", "rider")


@pytest.mark.parametrize("body,expected", [
    ({"Success": False, "Message": "private-password"}, AuthError),
    ({"Success": True}, ProtocolError),
    ({"Success": True, "AccessToken": "bad\ntoken"}, ProtocolError),
    ({"Success": "true", "AccessToken": "private-token"}, AuthError),
])
def test_invalid_login_does_not_disclose_response(body, expected):
    with MyWhooshClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))) as client:
        with pytest.raises(expected) as error:
            client.login("example@example.invalid", "synthetic-password")
    assert "private-" not in str(error.value)


def test_multipage_deduplication_and_utc():
    calls = []

    def handle(request):
        assert str(request.url) == ACTIVITIES_URL
        assert request.headers["Authorization"] == "Bearer synthetic-token"
        assert request.extensions["timeout"]["connect"] <= 5
        calls.append(json.loads(request.content))
        entries = [activity("a", startDatetime="2026-10-23T16:00:00+02:00")]
        if len(calls) == 2:
            entries += [activity("b", startDatetime=None, date=1792764000)]
        return httpx.Response(200, json=page(entries, total=2))

    with MyWhooshClient(transport=httpx.MockTransport(handle)) as client:
        result = client.activities(Session("synthetic-token"))
    assert [a.id for a in result.activities] == ["a", "b"]
    assert result.activities[0].start_time == datetime(2026, 10, 23, 14, tzinfo=timezone.utc)
    assert result.activities[1].start_time.tzinfo == timezone.utc
    assert result.truncated is False
    assert calls == [{"sortDate": "DESC", "page": 1}, {"sortDate": "DESC", "page": 2}]


@pytest.mark.parametrize("total", [0, 1])
def test_empty_activity_list(total):
    with MyWhooshClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=page([], total)))) as client:
        result = client.activities(Session("synthetic-token"))
    assert result.activities == ()
    assert result.truncated is False


def test_limit_stops_requests_and_reports_truncation():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=page([activity("a"), activity("b")], total=10))

    with MyWhooshClient(transport=httpx.MockTransport(handle)) as client:
        result = client.activities(Session("synthetic-token"), limit=1)
    assert len(calls) == 1
    assert [a.id for a in result.activities] == ["a"]
    assert result.truncated is True


def test_page_limit_stops_requests():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=page([activity(str(len(calls)))], total=10000))

    with MyWhooshClient(transport=httpx.MockTransport(handle)) as client:
        result = client.activities(Session("synthetic-token"), limit=100)
    assert len(calls) == 5
    assert len(result.activities) == 5
    assert result.truncated is True


def test_repeated_page_is_error():
    with MyWhooshClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=page([activity()], 3)))) as client:
        with pytest.raises(ProtocolError, match="wiederholt"):
            client.activities(Session("synthetic-token"))


@pytest.mark.parametrize("body", [
    {}, [], {"data": None}, {"data": {}}, page([], total="1"), page([], total=True),
    page([activity()], total=0), page([], total=3), page([{}]),
    page([activity(id=123)]), page([activity(startDatetime="2026-10-23T16:00:00")]),
    page([activity(startDatetime="bad-date")]), page([activity(rideDuration=[])]),
    page([activity(startDatetime=None, date=True)]),
])
def test_unknown_schema_fails_closed(body):
    with MyWhooshClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))) as client:
        with pytest.raises(ProtocolError):
            client.activities(Session("synthetic-token"))


@pytest.mark.parametrize("status,expected", [
    (401, AuthError), (403, AuthError), (429, RateLimitError),
    (500, NetworkError), (503, NetworkError), (302, ProtocolError), (400, ProtocolError),
])
def test_errors_are_redacted_and_not_retried(status, expected):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, text="private-token private-password",
                              headers={"Location": "https://example.invalid/steal"})

    with MyWhooshClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(expected) as error:
            client.activities(Session("synthetic-token"))
    assert "private-" not in str(error.value)
    assert len(calls) == 1


@pytest.mark.parametrize("exception", [httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError])
def test_network_errors_redacted(exception):
    def handle(request):
        raise exception("private-token private-password", request=request)

    with MyWhooshClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(NetworkError) as error:
            client.activities(Session("synthetic-token"))
    assert "private-" not in str(error.value)


@pytest.mark.parametrize("response", [
    lambda: httpx.Response(200, text="<html>private-token</html>"),
    lambda: httpx.Response(200, content=b"{broken-private-token", headers={"Content-Type": "application/json"}),
    lambda: httpx.Response(200, content=b"x" * (mywhoosh.MAX_RESPONSE_BYTES + 1), headers={"Content-Type": "application/json"}),
])
def test_invalid_or_oversize_response(response):
    with MyWhooshClient(transport=httpx.MockTransport(lambda r: response())) as client:
        with pytest.raises(ProtocolError) as error:
            client.activities(Session("synthetic-token"))
    assert "private-token" not in str(error.value)


def test_deadline_expiry_stops_pagination(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(mywhoosh.time, "monotonic", lambda: now[0])
    calls = []

    def handle(request):
        calls.append(request)
        now[0] = 61.0
        return httpx.Response(200, json=page([activity()], total=10))

    with MyWhooshClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(NetworkError, match="Zeitbudget"):
            client.activities(Session("synthetic-token"))
    assert len(calls) == 1
