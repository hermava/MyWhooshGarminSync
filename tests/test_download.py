import json
import logging

import httpx
import pytest

from mywhoosh_garmin_sync.auth import Session
from mywhoosh_garmin_sync.errors import NetworkError, ProtocolError, RateLimitError
from mywhoosh_garmin_sync.mywhoosh import DOWNLOAD_URL, MyWhooshClient
from mywhoosh_garmin_sync import mywhoosh
from fit_factory import make_fit

SIGNED_URL = "https://synthetic-bucket.s3.eu-west-1.amazonaws.com/ride.fit?X-Amz-Signature=synthetic-secret"


def test_download_contract_no_forwarded_auth_and_no_url_logging(caplog):
    caplog.set_level(logging.DEBUG)
    data = make_fit()
    calls = []

    def handle(request):
        calls.append(request)
        if request.method == "POST":
            assert str(request.url) == DOWNLOAD_URL
            assert request.headers["Authorization"] == "Bearer synthetic-token"
            assert json.loads(request.content) == {"fileId": "opaque-file-id"}
            return httpx.Response(200, json={"data": SIGNED_URL}, headers={"Set-Cookie": "api=synthetic-cookie; Path=/; Secure"})
        assert str(request.url) == SIGNED_URL
        assert request.method == "GET"
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        return httpx.Response(200, content=data)

    with MyWhooshClient(transport=httpx.MockTransport(handle)) as client:
        assert client.download_fit(Session("synthetic-token"), "opaque-file-id") == data
    assert len(calls) == 2
    assert "synthetic-secret" not in caplog.text
    assert "synthetic-token" not in caplog.text
    assert "synthetic-cookie" not in caplog.text


@pytest.mark.parametrize("url", [
    "http://synthetic-bucket.s3.eu-west-1.amazonaws.com/ride.fit",
    "https://localhost/ride.fit", "https://127.0.0.1/ride.fit", "file:///tmp/ride.fit",
    "https://s3.amazonaws.com.evil.invalid/ride.fit",
    "https://evil.invalid/s3.amazonaws.com/ride.fit",
    "https://name:password@s3.amazonaws.com/ride.fit",
    "https://s3.amazonaws.com:444/ride.fit", "https://s3.amazonaws.com/ride.fit#fragment",
    "", None, {"url": SIGNED_URL},
])
def test_unsafe_or_unknown_url_rejected_without_get(url):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(200, json={"data": url})

    with MyWhooshClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ProtocolError):
            client.download_fit(Session("synthetic-token"), "opaque-file-id")
    assert len(calls) == 1


@pytest.mark.parametrize("status,expected", [(302, NetworkError), (403, NetworkError), (404, NetworkError),
                                             (500, NetworkError), (206, NetworkError), (429, RateLimitError)])
def test_download_http_error_not_retried_or_redirected(status, expected):
    calls = []

    def handle(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"data": SIGNED_URL})
        return httpx.Response(status, text="synthetic-secret", headers={"Location": SIGNED_URL})

    with MyWhooshClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(expected) as error:
            client.download_fit(Session("synthetic-token"), "opaque-file-id")
    assert "synthetic-secret" not in str(error.value)
    assert "amazonaws" not in str(error.value)
    assert len(calls) == 2


@pytest.mark.parametrize("kind", ["oversize-stream", "oversize-header", "length-mismatch", "empty", "timeout", "connection"])
def test_partial_or_oversize_download_rejected(kind, monkeypatch):
    monkeypatch.setattr(mywhoosh, "MAX_FIT_BYTES", 1024)

    def handle(request):
        if request.method == "POST":
            return httpx.Response(200, json={"data": SIGNED_URL})
        if kind == "timeout":
            raise httpx.ReadTimeout(SIGNED_URL, request=request)
        if kind == "connection":
            raise httpx.ConnectError(SIGNED_URL, request=request)
        if kind == "oversize-header":
            return httpx.Response(200, content=b"x", headers={"Content-Length": "1025"})
        if kind == "length-mismatch":
            return httpx.Response(200, content=b"x", headers={"Content-Length": "2"})
        if kind == "oversize-stream":
            return httpx.Response(200, content=b"x" * 1025)
        return httpx.Response(200, content=b"")

    with MyWhooshClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises((ProtocolError, NetworkError)) as error:
            client.download_fit(Session("synthetic-token"), "opaque-file-id")
    assert "synthetic-secret" not in str(error.value)
