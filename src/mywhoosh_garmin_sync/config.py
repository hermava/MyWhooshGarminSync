"""Small local device configuration; no account credentials or environment files."""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .errors import SessionError

MAX_CONFIG_BYTES = 8192
DEFAULT_CONFIG_PATH = Path("config.local.toml")


@dataclass(frozen=True)
class DeviceConfig:
    profile: str | None = None
    unit_id: int | None = field(default=None, repr=False)


def load_device_config(path: Path | None = None) -> DeviceConfig:
    target = path if path is not None else DEFAULT_CONFIG_PATH
    try:
        with target.open("rb") as handle:
            content = handle.read(MAX_CONFIG_BYTES + 1)
    except FileNotFoundError:
        if path is None:
            return DeviceConfig()
        raise SessionError("Die angegebene Konfigurationsdatei wurde nicht gefunden.") from None
    except OSError:
        raise SessionError("Gerätekonfiguration kann nicht gelesen werden.") from None
    if len(content) > MAX_CONFIG_BYTES:
        raise SessionError("Gerätekonfiguration überschreitet das Größenlimit.")
    try:
        raw = tomllib.loads(content.decode("utf-8"))
    except (ValueError, UnicodeError):
        raise SessionError("Gerätekonfiguration enthält kein gültiges UTF-8-TOML.") from None
    if set(raw) - {"device"} or not isinstance(raw.get("device", {}), dict):
        raise SessionError("Gerätekonfiguration unterstützt nur den Abschnitt [device].")
    device = raw.get("device", {})
    if set(device) - {"profile", "unit_id"}:
        raise SessionError("Unbekannte Geräteeinstellung; erlaubt sind profile und unit_id.")
    profile = device.get("profile")
    if profile is not None and profile != "edge530":
        raise SessionError("Geräteprofil wird nicht unterstützt; derzeit ist edge530 verfügbar.")
    unit_id = device.get("unit_id")
    if unit_id is not None and (type(unit_id) is not int or not 1 <= unit_id <= 0xFFFFFFFF):
        raise SessionError("device.unit_id muss eine positive 32-Bit-Gerätekennung sein.")
    return DeviceConfig(profile, unit_id)
