"""Private sync files and an OS-released, cross-process application lock."""

import contextlib
import ctypes
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading

from .errors import SessionError


def default_sync_directory():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "data"
    return Path.cwd() / "private" / "sync"


def _windows_permissions(path, *, library=None, kernel=None):
    library = library if library is not None else ctypes.WinDLL("Advapi32.dll", use_last_error=True)
    kernel = kernel if kernel is not None else ctypes.WinDLL("Kernel32.dll", use_last_error=True)
    convert = library.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    convert.restype = ctypes.c_int32
    apply = library.SetFileSecurityW
    apply.argtypes, apply.restype = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_void_p], ctypes.c_int32
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = [ctypes.c_void_p], ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    # Protected DACL: owner rights + SYSTEM, inherited by child files/directories.
    if not convert("D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)", 1, ctypes.byref(descriptor), None):
        raise SessionError("Windows could not prepare private sync storage.")
    try:
        if not apply(str(path), 0x80000004, descriptor):
            raise SessionError("Windows could not protect the sync data directory.")
    finally:
        kernel.LocalFree(descriptor)


class Workspace:
    def __init__(self, directory):
        self.root = Path(os.path.abspath(Path(directory).expanduser()))
        self._depth = 0
        self._fd = None
        self._mutex = threading.RLock()

    @staticmethod
    def _no_links(path):
        if any(p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction())
               for p in (path, *path.parents)):
            raise SessionError("Links are not allowed in the sync storage path.")

    def prepare(self):
        try:
            self._no_links(self.root)
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = self.root.lstat()
            if not stat.S_ISDIR(info.st_mode):
                raise SessionError("Sync storage is not a directory.")
            if os.name == "posix":
                if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
                    raise SessionError("Sync storage needs your ownership and permissions 0700.")
            elif os.name == "nt":
                _windows_permissions(self.root)
            else:
                raise SessionError("Sync storage is not supported on this operating system.")
        except OSError:
            raise SessionError("Private sync storage could not be prepared.") from None

    def path(self, name):
        if not isinstance(name, str) or Path(name).name != name or name in ("", ".", ".."):
            raise SessionError("Invalid sync storage filename.")
        return self.root / name

    def check_file(self, name):
        path = self.path(name)
        self._no_links(path)
        if path.exists():
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SessionError("Sync storage contains an unsafe file.")
            if os.name == "posix" and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600):
                raise SessionError("Sync files need your ownership and permissions 0600.")
            if os.name == "nt":
                _windows_permissions(path)
        return path

    def read(self, name, *, maximum):
        path = self.check_file(name)
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise SessionError("Sync file is not a regular file.")
                data = handle.read(maximum + 1)
            if len(data) > maximum:
                raise SessionError("Sync file exceeds its size limit.")
            return data
        except FileNotFoundError:
            return None
        except OSError:
            raise SessionError("Sync file could not be read safely.") from None

    def sync_directory(self):
        if os.name == "posix":
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def write(self, name, data, *, replace=False):
        self.prepare()
        target = self.check_file(name)
        temporary = None
        try:
            fd, temporary = tempfile.mkstemp(prefix=".sync-", dir=self.root)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            if replace:
                os.replace(temporary, target)
            else:
                os.link(temporary, target)  # Exclusive publication, including on NTFS.
            self.sync_directory()
        except OSError:
            raise SessionError("Sync file could not be stored safely; no existing file was overwritten.") from None
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    @contextlib.contextmanager
    def locked(self):
        with self._mutex:
            if not self._depth:
                self.prepare()
                path = self.check_file(".sync.lock")
                try:
                    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
                except OSError:
                    raise SessionError("The sync data folder could not be locked safely.") from None
                try:
                    if os.fstat(fd).st_size == 0:
                        os.write(fd, b"\0")
                    os.lseek(fd, 0, os.SEEK_SET)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    os.close(fd)
                    raise SessionError("Another app or sync is using this data folder. Close it first.", code="sync_locked") from None
                self._fd = fd
            self._depth += 1
        try:
            yield self
        finally:
            with self._mutex:
                self._depth -= 1
                if not self._depth:
                    os.close(self._fd)
                    self._fd = None
