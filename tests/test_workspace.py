import ctypes
import os
import stat
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mywhoosh_garmin_sync.errors import SessionError
from mywhoosh_garmin_sync.workspace import Workspace, _windows_permissions


def test_private_storage_is_exclusive_and_rejects_modified_permissions(tmp_path):
    workspace = Workspace(tmp_path / "data")
    with workspace.locked():
        workspace.write("test.fit", b"synthetic")
        assert workspace.read("test.fit", maximum=100) == b"synthetic"
        with pytest.raises(SessionError):
            workspace.write("test.fit", b"replacement")
        assert workspace.read("test.fit", maximum=100) == b"synthetic"
    if os.name == "posix":
        assert stat.S_IMODE(workspace.root.stat().st_mode) == 0o700
        assert stat.S_IMODE((workspace.root / "test.fit").stat().st_mode) == 0o600
        (workspace.root / "test.fit").chmod(0o644)
        with pytest.raises(SessionError):
            workspace.read("test.fit", maximum=100)


def test_independent_workspaces_compete_for_one_os_lock_and_release_on_exit(tmp_path):
    first, second = Workspace(tmp_path / "data"), Workspace(tmp_path / "data")
    with first.locked():
        with first.locked():
            with pytest.raises(SessionError, match="Another app"):
                with second.locked():
                    pytest.fail("Second lock must not be acquired")
    with second.locked():
        second.write("result.json", b"{}")


def test_process_lock_blocks_a_second_process_and_recovers_after_owner_exit(tmp_path):
    workspace = Workspace(tmp_path / "data")
    code = """
import sys
from mywhoosh_garmin_sync.workspace import Workspace
from mywhoosh_garmin_sync.errors import SessionError
try:
    with Workspace(sys.argv[1]).locked():
        pass
except SessionError:
    raise SystemExit(3)
"""
    with workspace.locked():
        result = subprocess.run([sys.executable, "-c", code, str(workspace.root)], capture_output=True, timeout=10)
        assert result.returncode == 3, result.stderr
    result = subprocess.run([sys.executable, "-c", code, str(workspace.root)], capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("name", ["../outside", "/absolute", "", ".", ".."])
def test_filenames_cannot_escape_workspace(tmp_path, name):
    with pytest.raises(SessionError):
        Workspace(tmp_path / "data").path(name)


@pytest.mark.skipif(os.name != "posix", reason="POSIX link and mode checks")
def test_links_and_unsafe_existing_directories_are_not_adopted(tmp_path):
    root = tmp_path / "data"
    root.mkdir(mode=0o755)
    with pytest.raises(SessionError):
        Workspace(root).prepare()
    root.chmod(0o700)
    other = tmp_path / "other"
    other.mkdir(mode=0o700)
    linked = root / "linked"
    linked.symlink_to(other, target_is_directory=True)
    with pytest.raises(SessionError):
        Workspace(linked).prepare()


@pytest.mark.parametrize("failure", ["convert", "apply", None])
def test_windows_binding_applies_protected_owner_system_acl_and_frees_descriptor(tmp_path, failure):
    api = SimpleNamespace(ConvertStringSecurityDescriptorToSecurityDescriptorW=Mock(), SetFileSecurityW=Mock())
    kernel = SimpleNamespace(LocalFree=Mock())

    def convert(sddl, revision, output, length):
        assert sddl == "D:P(A;OICI;FA;;;OW)(A;OICI;FA;;;SY)"
        assert revision == 1
        ctypes.cast(output, ctypes.POINTER(ctypes.c_void_p))[0] = 123
        return int(failure != "convert")

    api.ConvertStringSecurityDescriptorToSecurityDescriptorW.side_effect = convert
    api.SetFileSecurityW.return_value = int(failure != "apply")
    if failure:
        with pytest.raises(SessionError):
            _windows_permissions(tmp_path, library=api, kernel=kernel)
    else:
        _windows_permissions(tmp_path, library=api, kernel=kernel)
        assert api.SetFileSecurityW.call_args.args[1] == 0x80000004
    assert kernel.LocalFree.call_count == (0 if failure == "convert" else 1)
