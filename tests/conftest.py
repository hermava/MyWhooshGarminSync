import socket

import pytest


@pytest.fixture(autouse=True)
def isolated_device_config(tmp_path, monkeypatch):
    """Tests must never read the developer's actual device configuration."""
    from mywhoosh_garmin_sync import config

    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", tmp_path / "config.local.toml")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Fail closed if any test accidentally attempts a real connection."""
    def blocked(*args, **kwargs):
        raise AssertionError("Real network access is forbidden in offline tests")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket, "getaddrinfo", blocked)
    # curl_cffi uses native libcurl and can bypass Python's socket functions.
    try:
        from curl_cffi import requests as curl_requests
    except ImportError:
        pass
    else:
        monkeypatch.setattr(curl_requests.Session, "request", blocked)
