import json
from dataclasses import replace

import httpx
import pytest

from mywhoosh_garmin_sync import garmin
from mywhoosh_garmin_sync.errors import AuthError, FitError, NetworkError, ProtocolError, RateLimitError
from mywhoosh_garmin_sync.fit_processing import validate_fit
from fit_factory import make_fit


class FakeAuth:
    def __init__(self):
        self.closed = False
        self.logins = 0

    def login(self, email, password, prompt_mfa):
        assert email == "example@example.invalid"
        assert password == "synthetic-password"
        self.logins += 1

    def headers(self):
        return {"Authorization": "Bearer synthetic-token"}

    def close(self):
        self.closed = True


def row(identity=123, **overrides):
    return {"activityId": identity, "activityName": "Synthetic Ride",
            "startTimeGMT": "2024-01-01 00:00:00", "duration": 60, "elapsedDuration": 60,
            **overrides}


def test_read_contract_limits_and_cleanup():
    backend = FakeAuth()
    calls = []

    def handle(request):
        calls.append(request)
        assert request.method == "GET"
        assert str(request.url).split("?")[0] == garmin.ACTIVITIES_URL
        assert dict(request.url.params) == {"start": "0", "limit": "10"}
        assert request.headers["Accept-Encoding"] == "identity"
        assert request.headers["Authorization"] == "Bearer synthetic-token"
        return httpx.Response(200, json=[row()])

    with garmin.GarminReader(backend=backend, transport=httpx.MockTransport(handle)) as reader:
        reader.login("example@example.invalid", "synthetic-password", lambda: "123456")
        result = reader.activities()
    assert result[0].id == "123"
    assert result[0].start_time_utc == "2024-01-01T00:00:00+00:00"
    assert backend.closed
    assert len(calls) == 1


@pytest.mark.parametrize("body", [{}, [row(activityId=True)], [row(activityId="abc")], [row(duration=-1)],
                                 [row(duration=True)], [row(startTimeGMT="invalid")], [row(), row()],
                                 [row(), row(124)]])
def test_unknown_response_schema_fails_closed(body):
    with garmin.GarminReader(backend=FakeAuth(), transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))) as reader:
        reader.login("example@example.invalid", "synthetic-password", lambda: "123456")
        with pytest.raises(ProtocolError):
            reader.activities(limit=1)


@pytest.mark.parametrize("status,expected", [(401, AuthError), (403, AuthError), (429, RateLimitError),
                                             (500, NetworkError), (302, NetworkError)])
def test_http_failures_are_redacted_and_not_retried(status, expected):
    calls = []
    backend = FakeAuth()

    def handle(request):
        calls.append(request)
        return httpx.Response(status, text="synthetic-secret", headers={"Location": "https://example.invalid"})

    with garmin.GarminReader(backend=backend, transport=httpx.MockTransport(handle)) as reader:
        reader.login("example@example.invalid", "synthetic-password", lambda: "123456")
        with pytest.raises(expected) as error:
            reader.activities()
    assert len(calls) == 1
    assert backend.closed
    assert "synthetic-secret" not in str(error.value)


def test_empty_list_is_valid_and_network_timeout_is_redacted():
    with garmin.GarminReader(backend=FakeAuth(), transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[]))) as reader:
        reader.login("example@example.invalid", "synthetic-password", lambda: "123456")
        assert reader.activities() == ()

    def fail(request):
        raise httpx.ReadTimeout("synthetic-secret", request=request)

    with garmin.GarminReader(backend=FakeAuth(), transport=httpx.MockTransport(fail)) as reader:
        reader.login("example@example.invalid", "synthetic-password", lambda: "123456")
        with pytest.raises(NetworkError) as error:
            reader.activities()
    assert "synthetic-secret" not in str(error.value)


@pytest.mark.parametrize("kind", ["json", "size", "html"])
def test_bad_response_is_rejected(kind, monkeypatch):
    monkeypatch.setattr(garmin, "MAX_RESPONSE_BYTES", 128)
    responses = {
        "json": httpx.Response(200, content=b"{synthetic-secret", headers={"Content-Type": "application/json"}),
        "size": httpx.Response(200, content=b"x" * 129, headers={"Content-Type": "application/json"}),
        "html": httpx.Response(200, text="<html>synthetic-secret</html>"),
    }
    with garmin.GarminReader(backend=FakeAuth(), transport=httpx.MockTransport(lambda r: responses[kind])) as reader:
        reader.login("example@example.invalid", "synthetic-password", lambda: "123456")
        with pytest.raises(ProtocolError) as error:
            reader.activities()
    assert "synthetic-secret" not in str(error.value)


def test_read_requires_login_and_valid_limit():
    with garmin.GarminReader(backend=FakeAuth()) as reader:
        with pytest.raises(AuthError):
            reader.activities()
        reader.login("example@example.invalid", "synthetic-password", lambda: "123456")
        with pytest.raises(ValueError):
            reader.activities(limit=101)


def test_failed_relogin_cannot_reuse_previous_authenticated_state():
    with garmin.GarminReader(backend=FakeAuth()) as reader:
        reader.login("example@example.invalid", "synthetic-password", lambda: "123456")
        with pytest.raises(AuthError):
            reader.login("", "", lambda: "123456")
        with pytest.raises(AuthError):
            reader.activities()


@pytest.mark.parametrize("rows,status", [
    ([row()], "candidate_found"), ([row(), row(124)], "ambiguous"), ([], "no_candidate_in_window"),
    ([row(startTimeGMT="2024-01-01 00:10:00")], "no_candidate_in_window"),
    ([row(duration=500, elapsedDuration=500)], "no_candidate_in_window"),
    ([row(startTimeGMT=None)], "inconclusive"),
    ([row(duration=None, elapsedDuration=None)], "inconclusive"),
    ([row(), row(124, startTimeGMT=None)], "ambiguous"),
])
def test_match_status_is_conservative(rows, status):
    activities = tuple(garmin._parse_activity(r) for r in rows)
    result = garmin.match_fit(validate_fit(make_fit()), activities, window_limit=100)
    assert result.status == status
    assert result.window_limit == 100
    assert result.checked_activities == len(rows)


def test_match_uses_comparable_timer_duration_when_elapsed_is_unavailable():
    report = validate_fit(make_fit(timer=30_000))
    activities = (garmin._parse_activity(row(elapsedDuration=None, duration=30)),)
    assert garmin.match_fit(report, activities, window_limit=100).status == "candidate_found"
    # Elapsed duration must never be compared against active timer duration.
    activities = (garmin._parse_activity(row(elapsedDuration=None, duration=60)),)
    assert garmin.match_fit(report, activities, window_limit=100).status == "no_candidate_in_window"


def test_match_rejects_invalid_fit_and_multisession():
    report = validate_fit(make_fit())
    for invalid in (validate_fit(b"not-fit"), replace(report, session_times=report.session_times * 2)):
        with pytest.raises(FitError):
            garmin.match_fit(invalid, (), window_limit=100)
