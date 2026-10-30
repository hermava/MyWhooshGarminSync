"""Offline frozen-build check. Never opens real account sessions or configuration."""

import json
import base64
from importlib.metadata import version
from pathlib import Path


def run(report_path):
    report_path = Path(report_path)
    root = None
    controller = None
    result = {"status": "failed", "checks": []}
    try:
        import tkinter as tk
        from tkinter import ttk
        from .desktop_controller import DesktopController
        from .garmin import _create_backend
        from .gui import AccountWindow
        from .fit_time import prepare_automatic_time_repair
        from .sync import SyncEngine, SyncSettings, load_settings
        from .sync_state import SyncState
        from .windows_credentials import _NativeCredentials
        from .workspace import Workspace

        # Loading these bindings does not enumerate or read saved credentials.
        _NativeCredentials()
        result["checks"].append("windows_credential_bindings")
        backend = _create_backend()
        backend.close()
        result["checks"].append("garmin_backend_and_native_curl")
        workspace = Workspace(report_path.parent / "smoke-data")
        with workspace.locked():
            workspace.write("synthetic.txt", b"build check")
            assert workspace.read("synthetic.txt", maximum=100) == b"build check"
            with SyncState(workspace) as state:
                state.update("synthetic", "synthetic", "discovered")
                assert state.get("synthetic", "synthetic")["status"] == "discovered"
        result["checks"].append("private_windows_storage_lock_and_sqlite")
        preferences = SyncSettings(profile="edge530", unit_id=222222222, timezone="Europe/Berlin")
        SyncEngine(None, workspace, legacy_directory=workspace.root / "garmin-uploads").save_settings(preferences)
        assert load_settings(workspace) == preferences
        result["checks"].append("packaged_device_preferences_saved_without_preview")
        # Synthetic one-minute activity, no accounts, serial numbers or GPS data.
        synthetic = base64.b64decode(
            "DiCICJ4AAAAuRklU9adAAAAAAAMAAQABAoQEBIYABEsBgLT0P0IAABQAAv0EhgcChAKAtPQ/lgACu7T0P5YARAAAFQAD/QSG"
            "AAEAAQEABLy09D8ICUEAABIABAIEhv0EhgcEhggEhgGAtPQ/gLT0P2DqAABg6gAARgAAAP8BAAMNBqvN70MAACIABf0EhgAEhgEC"
            "hAUEhscDDQOAtPQ/YOoAAAEAuwCSZRI0VnP2")
        corrected, repair = prepare_automatic_time_repair(synthetic, timezone_name="Europe/Berlin")
        assert repair.status == "ready" and corrected != synthetic
        assert repair.changes[-1].after == "2024-01-01T00:01:00"
        result["checks"].append("packaged_time_repair_and_timezone_data")
        root = tk.Tk()
        root.withdraw()
        controller = DesktopController(None)
        window = AccountWindow(root, tk, ttk, controller, can_remember=False,
                               sync_settings=load_settings(workspace))
        root.update_idletasks()
        assert window.sync_tab is not None
        assert window.sync_tab.settings().timezone == "Europe/Berlin"
        assert window.sync_tab.settings().profile == "edge530"
        assert window.sync_tab.retry_button.cget("text") == "Retry deleted"
        result["checks"].append("packaged_tk_and_sync_window")
        root.deiconify()
        root.update()
        assert repair.start_utc == "2023-12-31T23:00:00Z" and repair.shifted_fields > 2
        assert window.sync_tab.adjust_times.get() is True
        assert not hasattr(window.sync_tab, "verify_button")
        assert not hasattr(window.sync_tab, "time_button")
        window.sync_tab.adjust_checkbox.invoke()
        assert window.sync_tab.settings().adjust_times is False
        result["checks"].append("packaged_automatic_time_checkbox")
        result["checks"].append("packaged_local_recording_to_utc")
        result["versions"] = {name: version(name) for name in ("mywhoosh-garmin-sync", "garminconnect", "curl_cffi")}
        result["status"] = "passed"
    except Exception as error:
        # Never write raw exception messages or account data to a build report.
        result["error_type"] = type(error).__name__
    finally:
        if controller is not None:
            controller.close()
            controller.wait()
        if root is not None:
            root.destroy()
    with report_path.open("x", encoding="utf-8") as report:
        json.dump(result, report, indent=2)
    return 0 if result["status"] == "passed" else 1
