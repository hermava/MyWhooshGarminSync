"""Small independently encoded FIT activities; never use private training data."""

import struct
from datetime import datetime, timezone

FIT_EPOCH = datetime(1989, 12, 31, tzinfo=timezone.utc)
START = int((datetime(2024, 1, 1, tzinfo=timezone.utc) - FIT_EPOCH).total_seconds())


def crc16(data):
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc


def definition(local, global_number, fields, endian="<"):
    return bytes([0x40 | local, 0, int(endian == ">")]) + struct.pack(endian + "H", global_number) + bytes([len(fields)]) + b"".join(bytes(f) for f in fields)


def wrap(body, header_size=14):
    header = struct.pack("<BBHI4s", header_size, 0x20, 2184, len(body), b".FIT")
    if header_size == 14:
        header += struct.pack("<H", crc16(header))
    return header + body + struct.pack("<H", crc16(header + body))


def make_fit(*, header_size=14, times=(START, START + 60), start=START, end=START + 60,
             file_type=4, include_session=True, include_activity=True, endian="<", timer=60_000,
             elapsed=60_000):
    body = definition(0, 0, [(0, 1, 0), (4, 4, 0x86)], endian)
    body += b"\x00" + bytes([file_type]) + struct.pack(endian + "I", start)
    body += definition(2, 20, [(253, 4, 0x86), (7, 2, 0x84)], endian)
    for timestamp in times:
        body += b"\x02" + struct.pack(endian + "IH", timestamp, 150)
    if include_session:
        body += definition(1, 18, [(2, 4, 0x86), (253, 4, 0x86), (7, 4, 0x86), (8, 4, 0x86)], endian)
        body += b"\x01" + struct.pack(endian + "IIII", start, end, elapsed, timer)
    if include_activity:
        body += definition(3, 34, [(253, 4, 0x86)], endian)
        body += b"\x03" + struct.pack(endian + "I", end)
    return wrap(body, header_size)


def make_identity_fit(*, endian="<", header_size=14, manufacturer=331, creator_manufacturer=331,
                      missing_fields=False, creator=True, sensor=True, creator_index=0,
                      manufacturer_type=0x84, manufacturer_size=2):
    """Identity data plus unknown fields/messages to verify lossless editing."""
    fields = [(0, 1, 0), (1, manufacturer_size, manufacturer_type), (4, 4, 0x86), (199, 3, 0x0D)]
    payload = bytes([4]) + manufacturer.to_bytes(manufacturer_size, "little" if endian == "<" else "big")
    payload += struct.pack(endian + "I", START) + b"\xAB\xCD\xEF"
    if not missing_fields:
        fields.extend([(2, 2, 0x84), (3, 4, 0x8C)])
        payload += struct.pack(endian + "HI", 0, 111111111)
    body = definition(0, 0, fields, endian) + b"\x00" + payload
    device_fields = [(0, 1, 0x02), (2, 2, 0x84), (198, 3, 0x0D)]
    if not missing_fields:
        device_fields.extend([(4, 2, 0x84), (3, 4, 0x8C), (5, 2, 0x84)])
    body += definition(4, 23, device_fields, endian)
    for include, index, vendor in ((creator, creator_index, creator_manufacturer), (sensor, 1, 123)):
        if include:
            body += b"\x04" + bytes([index]) + struct.pack(endian + "H", vendor) + b"\x12\x34\x56"
            if not missing_fields:
                body += struct.pack(endian + "HIH", 17, 111111111 if index == 0 else 333333333, 123)
    body += definition(6, 0xFF00, [(0, 5, 0x0D)], endian) + b"\x06\xA0\xB0\xC0\xD0\xE0"
    # Reuse existing activity frames after replacing only the fixture's file_id.
    original = make_fit(endian=endian)
    skip = len(definition(0, 0, [(0, 1, 0), (4, 4, 0x86)], endian)) + 6
    body += original[14 + skip:-2]
    return wrap(body, header_size)


def make_time_fit(*, start=START, manufacturer=331, endian="<", header_size=14,
                  local=None, activity_time=None, stop_offset=60, include_stop=True,
                  last_offset=59, activity_timer=60_000, num_sessions=1, local_type=0x86):
    """Known Unix/FIT epoch bug, independently encoded without production helpers."""
    body = definition(0, 0, [(0, 1, 0), (1, 2, 0x84), (4, 4, 0x86)], endian)
    body += b"\x00" + struct.pack(endian + "BHI", 4, manufacturer, start)
    body += definition(2, 20, [(253, 4, 0x86), (7, 2, 0x84)], endian)
    for stamp in (start, start + last_offset):
        body += b"\x02" + struct.pack(endian + "IH", stamp, 150)
    if include_stop:
        body += definition(4, 21, [(253, 4, 0x86), (0, 1, 0), (1, 1, 0)], endian)
        body += b"\x04" + struct.pack(endian + "IBB", start + stop_offset, 8, 9)
    body += definition(1, 18, [(2, 4, 0x86), (253, 4, 0x86), (7, 4, 0x86), (8, 4, 0x86)], endian)
    body += b"\x01" + struct.pack(endian + "IIII", start, start, 60_000, 60_000)
    body += definition(6, 0xFF00, [(0, 3, 0x0D)], endian) + b"\x06\xAB\xCD\xEF"
    body += definition(3, 34, [(253, 4, 0x86), (0, 4, 0x86), (1, 2, 0x84),
                               (5, 4, local_type), (199, 3, 0x0D)], endian)
    body += b"\x03" + struct.pack(endian + "IIHI", start if activity_time is None else activity_time,
                                  activity_timer, num_sessions,
                                  start + last_offset + 631065600 if local is None else local) + b"\x12\x34\x56"
    return wrap(body, header_size)
