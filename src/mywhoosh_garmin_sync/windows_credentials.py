"""Explicit Windows Credential Manager storage; never falls back to plain files.

One active account per provider, scoped to the current Windows user and machine.
Credential blobs are limited to 2560 bytes, so an immutable generation of chunks
is published with one manifest write. Callers must serialize account operations.
"""

import contextlib
import ctypes
import hashlib
import json
import os
import re
import uuid
from typing import Protocol

from .errors import SessionError
from .sessions import MAX_SESSION_BYTES, SavedSession, check_provider

MAX_BLOB_BYTES = 2560
MAX_CHUNKS = (MAX_SESSION_BYTES + MAX_BLOB_BYTES - 1) // MAX_BLOB_BYTES
PREFIX = "MyWhooshGarminSync/session/v1/"


class CredentialAPI(Protocol):
    def read(self, name: str) -> bytes | None: ...
    def write(self, name: str, data: bytes) -> None: ...
    def delete(self, name: str) -> None: ...
    def names(self, prefix: str) -> tuple[str, ...]: ...


class _Credential(ctypes.Structure):
    # Use fixed-width Windows DWORDs, also when testing the binding on Linux.
    _fields_ = [
        ("Flags", ctypes.c_uint32), ("Type", ctypes.c_uint32),
        ("TargetName", ctypes.c_wchar_p), ("Comment", ctypes.c_wchar_p),
        ("LastWritten", ctypes.c_uint32 * 2), ("CredentialBlobSize", ctypes.c_uint32),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)), ("Persist", ctypes.c_uint32),
        ("AttributeCount", ctypes.c_uint32), ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.c_wchar_p), ("UserName", ctypes.c_wchar_p),
    ]


class _NativeCredentials:
    def __init__(self, *, library=None, last_error=None):
        if library is None:
            if os.name != "nt":
                raise SessionError("Saved account sessions currently require Windows Credential Manager.")
            try:
                library = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
            except OSError:
                raise SessionError("Windows Credential Manager is unavailable.") from None
        self._lib = library
        self._last_error = last_error if last_error is not None else ctypes.get_last_error
        pointer = ctypes.POINTER(_Credential)
        signatures = {
            "CredReadW": ([ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(pointer)], ctypes.c_int32),
            "CredWriteW": ([pointer, ctypes.c_uint32], ctypes.c_int32),
            "CredDeleteW": ([ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32], ctypes.c_int32),
            "CredEnumerateW": ([ctypes.c_wchar_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32),
                                ctypes.POINTER(ctypes.POINTER(pointer))], ctypes.c_int32),
            "CredFree": ([ctypes.c_void_p], None),
        }
        for name, (args, result) in signatures.items():
            function = getattr(library, name)
            function.argtypes, function.restype = args, result

    def _failed(self) -> bool:
        if self._last_error() == 1168:  # ERROR_NOT_FOUND is different from access failure.
            return True
        raise SessionError("Windows could not access saved account sessions.")

    def read(self, name):
        result = ctypes.POINTER(_Credential)()
        if not self._lib.CredReadW(name, 1, 0, ctypes.byref(result)):
            self._failed()
            return None
        try:
            record = result.contents
            if record.CredentialBlobSize > MAX_BLOB_BYTES or (record.CredentialBlobSize and not record.CredentialBlob):
                raise SessionError("Windows returned an invalid credential record.")
            return ctypes.string_at(record.CredentialBlob, record.CredentialBlobSize)
        finally:
            self._lib.CredFree(result)

    def write(self, name, data):
        if not 0 < len(data) <= MAX_BLOB_BYTES:
            raise SessionError("Credential record exceeds the Windows size limit.")
        blob = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
        record = _Credential(Type=1, TargetName=name, CredentialBlobSize=len(data),
                             CredentialBlob=blob, Persist=2, UserName="MyWhooshGarminSync")
        if not self._lib.CredWriteW(ctypes.byref(record), 0):
            raise SessionError("Windows could not save the account session.")

    def delete(self, name):
        if not self._lib.CredDeleteW(name, 1, 0):
            self._failed()

    def names(self, prefix):
        count = ctypes.c_uint32()
        records = ctypes.POINTER(ctypes.POINTER(_Credential))()
        if not self._lib.CredEnumerateW(prefix + "*", 0, ctypes.byref(count), ctypes.byref(records)):
            self._failed()
            return ()
        try:
            if count.value > 4096:
                raise SessionError("Too many saved account records. Review Windows Credential Manager.")
            return tuple(records[index].contents.TargetName for index in range(count.value)
                         if records[index].contents.Type == 1
                         and records[index].contents.TargetName
                         and records[index].contents.TargetName.startswith(prefix))
        finally:
            self._lib.CredFree(records)


class WindowsSessionStore:
    def __init__(self, *, api: CredentialAPI | None = None):
        self._api = api if api is not None else _NativeCredentials()

    @staticmethod
    def _prefix(provider):
        check_provider(provider)
        return PREFIX + provider + "/"

    def _manifest(self, provider):
        data = self._api.read(self._prefix(provider) + "current")
        if data is None:
            return None
        try:
            if len(data) > MAX_BLOB_BYTES:
                raise ValueError
            raw = json.loads(data)
            if (not isinstance(raw, dict) or set(raw) != {"version", "generation", "chunks", "sha256"}
                    or type(raw["version"]) is not int or raw["version"] != 1
                    or not isinstance(raw["generation"], str) or not re.fullmatch(r"[0-9a-f]{32}", raw["generation"])
                    or type(raw["chunks"]) is not int or not 1 <= raw["chunks"] <= MAX_CHUNKS
                    or not isinstance(raw["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", raw["sha256"])):
                raise ValueError
            return raw
        except (ValueError, TypeError, RecursionError):
            raise SessionError("Saved session manifest is invalid. Sign out locally to remove it.") from None

    def load(self, provider):
        manifest = self._manifest(provider)
        if manifest is None:
            return None
        chunks = []
        for index in range(manifest["chunks"]):
            chunk = self._api.read(f"{self._prefix(provider)}{manifest['generation']}/{index}")
            if chunk is None or not 0 < len(chunk) <= MAX_BLOB_BYTES:
                raise SessionError("Saved session is incomplete. Please sign out locally and sign in again.")
            chunks.append(chunk)
        data = b"".join(chunks)
        if hashlib.sha256(data).hexdigest() != manifest["sha256"]:
            raise SessionError("Saved session is inconsistent. Please sign out locally and sign in again.")
        return SavedSession.deserialize(data, provider)

    def save(self, session):
        data = session.serialize()
        provider = session.provider
        old = self._manifest(provider)
        generation = uuid.uuid4().hex
        names = []
        try:
            for index, start in enumerate(range(0, len(data), MAX_BLOB_BYTES)):
                name = f"{self._prefix(provider)}{generation}/{index}"
                names.append(name)
                self._api.write(name, data[start:start + MAX_BLOB_BYTES])
            manifest = {"version": 1, "generation": generation, "chunks": len(names),
                        "sha256": hashlib.sha256(data).hexdigest()}
            self._api.write(self._prefix(provider) + "current", json.dumps(manifest).encode())
        except BaseException:
            for name in names:
                with contextlib.suppress(SessionError):
                    self._api.delete(name)
            raise
        # The new manifest is committed. Old immutable chunks are no longer needed.
        if old:
            for index in range(old["chunks"]):
                self._api.delete(f"{self._prefix(provider)}{old['generation']}/{index}")

    def delete(self, provider):
        prefix = self._prefix(provider)
        # Includes unpublished generations left by a terminated save, even when
        # the current manifest is damaged. Never enumerate another app's entries.
        self._api.delete(prefix + "current")
        for name in self._api.names(prefix):
            if name.startswith(prefix):
                self._api.delete(name)
