import builtins
import os
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from collections import deque

import pytest

from mywhoosh_garmin_sync import gui
from mywhoosh_garmin_sync.desktop_controller import DesktopEvent
from mywhoosh_garmin_sync.mywhoosh import Activity, ActivityList


class Variable:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class Tree:
    def __init__(self):
        self.values = {}

    def get_children(self):
        return list(self.values)

    def delete(self, item):
        del self.values[item]

    def insert(self, parent, index, *, values):
        self.values[len(self.values)] = values


def window_for_handlers(controller):
    window = object.__new__(gui.AccountWindow)
    window.controller = controller
    window._closed = False
    window._close_requested = False
    window.sync_tab = None
    if controller is not None and not hasattr(controller, "busy"):
        controller.busy = False
    window._restore_pending = deque()
    window.panels = {provider: dict(email=Variable("example@example.invalid"), password=Variable("synthetic-password"),
                                   remember=Variable(False), status=Variable(), tree=Tree(),
                                   badge=SimpleNamespace(configure=lambda **kwargs: None))
                     for provider in ("mywhoosh", "garmin")}
    window.root = SimpleNamespace(after=lambda *args: None, destroy=lambda: None)
    window._set_busy = lambda busy: None
    return window


def test_password_field_is_cleared_immediately_after_starting_login():
    calls = []
    controller = SimpleNamespace(busy=False, sign_in=lambda *args, **kwargs: calls.append((args, kwargs)) or True)
    window = window_for_handlers(controller)
    window._login("mywhoosh")
    assert calls[0] == (("mywhoosh", "example@example.invalid", "synthetic-password"), {"remember": False})
    assert window.panels["mywhoosh"]["password"].get() == ""


def test_busy_window_does_not_submit_a_second_login():
    def forbidden(*args, **kwargs):
        pytest.fail("Busy window must not submit another password")

    window = window_for_handlers(SimpleNamespace(busy=True, sign_in=forbidden))
    window._login("mywhoosh")


def test_missing_credentials_are_rejected_locally():
    window = window_for_handlers(SimpleNamespace(busy=False))
    window.panels["garmin"]["password"].set("")
    window._login("garmin")
    assert "email and password" in window.panels["garmin"]["status"].get()


def test_activity_preview_replaces_old_rows_and_handles_sign_out():
    result = ActivityList((Activity("synthetic-id", "Synthetic Ride", datetime(2024, 1, 1, tzinfo=timezone.utc), "00:30:00"),), False)
    batches = iter([[DesktopEvent("done", "mywhoosh", "read", result, remembered=True)],
                    [DesktopEvent("done", "mywhoosh", "logout")]])
    window = window_for_handlers(SimpleNamespace(events=lambda: next(batches)))
    window.panels["mywhoosh"]["tree"].values = {0: ("Old Ride",)}
    window._poll()
    panel = window.panels["mywhoosh"]
    assert len(panel["tree"].values) == 1
    assert panel["tree"].values[0][0] == "Synthetic Ride"
    assert panel["remember"].get() is True
    assert "1 recent activities" in panel["status"].get()
    window._poll()
    assert panel["tree"].values == {}
    assert panel["email"].get() == ""
    assert panel["password"].get() == ""
    assert panel["remember"].get() is False


def test_sign_in_required_clears_stale_activity_preview():
    window = window_for_handlers(SimpleNamespace(events=lambda: [DesktopEvent("sign_in_required", "garmin", "read")]))
    window.panels["garmin"]["tree"].values = {0: ("Old Ride",)}
    window._poll()
    assert window.panels["garmin"]["tree"].values == {}
    assert window.panels["garmin"]["status"].get() == "Please sign in to continue."


def test_attempt_to_switch_accounts_clears_previous_accounts_activity_preview():
    events = [DesktopEvent("busy", "garmin", "login"),
              DesktopEvent("error", "garmin", "login", "Sign-in could not be confirmed.")]
    window = window_for_handlers(SimpleNamespace(events=lambda: events))
    window.panels["garmin"]["tree"].values = {0: ("Previous account's ride",)}
    window._poll()
    assert window.panels["garmin"]["tree"].values == {}
    assert window.panels["garmin"]["status"].get() == "Sign-in could not be confirmed."


def test_unexpected_callback_does_not_display_or_print_exception(capsys):
    window = window_for_handlers(None)
    window._callback_error(RuntimeError, RuntimeError("synthetic-private-password"), None)
    assert not capsys.readouterr().err
    assert "synthetic-private-password" not in window.panels["garmin"]["status"].get()
    assert window.panels["garmin"]["password"].get() == ""


def test_provider_titles_cannot_render_control_characters_or_bidi_overrides():
    assert gui._display("Ride\n\x1b[31m\u202econtrol") == "Ride  [31m control"


def test_startup_handles_missing_tk_without_traceback_or_storage_access(monkeypatch, capsys):
    original = builtins.__import__

    def imported(name, *args, **kwargs):
        if name == "tkinter":
            raise ImportError("synthetic-private-message")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", imported)
    assert gui.main() == 1
    output = capsys.readouterr()
    assert "Tk support" in output.err
    assert "synthetic-private-message" not in output.err


def test_close_clears_passwords_and_notifies_worker():
    calls = []
    window = window_for_handlers(SimpleNamespace(close=lambda: calls.append("close")))
    window.close()
    assert window._closed
    assert calls == ["close"]
    assert all(panel["password"].get() == "" for panel in window.panels.values())


def test_close_waits_for_busy_operation_before_destroying_window():
    calls = []
    controller = SimpleNamespace(busy=True, cancel=lambda: calls.append("stop"), close=lambda: calls.append("close"),
                                 events=lambda: [DesktopEvent("idle", "garmin", "read")])
    window = window_for_handlers(controller)
    window.root.destroy = lambda: calls.append("destroy")
    window.close()
    assert calls == ["stop"]
    assert not window._closed
    controller.busy = False
    window._poll()
    assert calls == ["stop", "close", "destroy"]


def test_windows_restore_checks_each_provider_once_without_password_prompt():
    calls = []
    controller = SimpleNamespace(busy=False, read=lambda provider: calls.append(provider) or True)
    window = window_for_handlers(controller)
    window._restore_pending = deque(("mywhoosh", "garmin"))
    window._restore_next()
    assert calls == ["mywhoosh"]
    controller.busy = True
    window._restore_next()
    assert calls == ["mywhoosh"]
    controller.busy = False
    window._restore_next()
    window._restore_next()
    assert calls == ["mywhoosh", "garmin"]


def test_manual_sign_in_is_not_followed_by_a_duplicate_automatic_restore():
    controller = SimpleNamespace(busy=False, sign_in=lambda *args, **kwargs: True)
    window = window_for_handlers(controller)
    window._restore_pending = deque(("mywhoosh", "garmin"))
    window._login("mywhoosh")
    assert list(window._restore_pending) == ["garmin"]


def test_unavailable_storage_is_explained_as_windows_only_in_wsl(monkeypatch):
    monkeypatch.setattr(gui.sys, "platform", "linux")
    assert "WSL / Linux" in gui._storage_hint(False)
    assert "Windows only" in gui._storage_hint(False)
    monkeypatch.setattr(gui.sys, "platform", "win32")
    assert "unavailable on this PC" in gui._storage_hint(False)
    assert "password is never saved" in gui._storage_hint(True)


def test_restore_is_not_started_when_window_is_closed():
    def forbidden(provider):
        pytest.fail("Closing the window must cancel pending restore work")

    window = window_for_handlers(SimpleNamespace(busy=False, read=forbidden))
    window._restore_pending = deque(("mywhoosh",))
    window._closed = True
    window._restore_next()


@pytest.mark.skipif(os.environ.get("MYWHOOSH_GUI_SMOKE_TEST") != "1",
                    reason="Opt-in test requiring Tk and a graphical desktop")
def test_native_window_uses_masked_inputs_and_does_not_start_network_or_load_sessions():
    tk = pytest.importorskip("tkinter")
    from tkinter import ttk

    try:
        root = tk.Tk()
    except tk.TclError:
        if os.name == "nt":
            raise
        pytest.skip("No graphical desktop available")

    class Controller:
        busy = False

        def events(self):
            return ()

        def __getattr__(self, name):
            pytest.fail("Opening the window must not call account or storage operations")

    try:
        window = gui.AccountWindow(root, tk, ttk, Controller(), can_remember=False)
        root.update()
        root.update_idletasks()
        assert set(window.panels) == {"mywhoosh", "garmin"}
        for panel in window.panels.values():
            assert panel["controls"][1].cget("show") == "•"
            assert panel["checkbox"].instate(["disabled"])
        # Actions must remain reachable at the smallest supported laptop size.
        root.geometry("860x600")
        root.update()
        root.update_idletasks()
        window.connection_canvas.yview_moveto(1.0)
        root.update_idletasks()
        bottom = window.connection_canvas.winfo_rooty() + window.connection_canvas.winfo_height()
        for panel in window.panels.values():
            logout = panel["controls"][-1]
            assert logout.winfo_rooty() >= window.connection_canvas.winfo_rooty()
            assert logout.winfo_rooty() + logout.winfo_height() <= bottom
        window.notebook.select(1)
        root.update()
        root.update_idletasks()
        assert all(panel["tree"].winfo_ismapped() for panel in window.panels.values())
    finally:
        root.destroy()


@pytest.mark.skipif(os.environ.get("MYWHOOSH_GUI_SMOKE_TEST") != "1",
                    reason="Opt-in end-to-end GUI test requiring a graphical desktop")
def test_native_sync_tab_previews_confirms_and_uploads_only_synthetic_data(tmp_path, monkeypatch):
    tk = pytest.importorskip("tkinter")
    from tkinter import ttk, messagebox
    from mywhoosh_garmin_sync.desktop_controller import DesktopController
    from mywhoosh_garmin_sync.sync import SyncSettings
    from test_sync import engine_for
    from fit_factory import make_time_fit

    try:
        root = tk.Tk()
    except tk.TclError:
        if os.name == "nt":
            raise
        pytest.skip("No graphical desktop available")
    engine, accounts = engine_for(tmp_path)
    accounts.files["ride-0"] = make_time_fit()
    controller = DesktopController(accounts, sync_engine=engine)
    monkeypatch.setattr(messagebox, "askyesno", lambda *args, **kwargs: True)
    window = gui.AccountWindow(root, tk, ttk, controller, can_remember=False,
                               sync_settings=SyncSettings(timezone="Europe/Berlin"))

    def wait_for(condition):
        deadline = time.monotonic() + 5
        while not condition() and time.monotonic() < deadline:
            root.update()
            time.sleep(0.01)
        assert condition(), window.sync_tab.message.get()

    try:
        window.notebook.select(2)
        window.sync_tab.preview()
        wait_for(lambda: window.sync_tab.plan is not None and not controller.busy)
        assert window.sync_tab.selected == {"ride-0"}
        assert window.sync_tab.items["ride-0"].time_status == "corrected"
        assert window.sync_tab.timezone.get() == "Europe/Berlin"
        assert window.sync_tab.adjust_times.get() is True
        assert window.sync_tab.items["ride-0"].start_utc == "2023-12-31T23:00:00+00:00"
        assert not hasattr(window.sync_tab, "verify_button")
        assert not hasattr(window.sync_tab, "time_button")
        assert not accounts.posts
        root.geometry("860x600")
        root.update()
        root.update_idletasks()
        button = window.sync_tab.upload_button
        assert button.winfo_rooty() + button.winfo_height() <= root.winfo_rooty() + root.winfo_height()
        assert window.sync_tab.tree.winfo_height() > 75
        window.sync_tab.upload()
        wait_for(lambda: window.sync_tab.items["ride-0"].status == "uploaded")
        assert len(accounts.posts) == 1
        assert window.sync_tab.plan is None
        accounts.remote.clear()  # Simulate deletion by the user in Garmin Connect.
        window.sync_tab.preview()
        wait_for(lambda: window.sync_tab.plan is not None and not controller.busy)
        tab = window.sync_tab
        tab.tree.selection_set(next(iter(tab.row_ids)))
        tab.retry_button.invoke()
        wait_for(lambda: tab.plan is not None and not controller.busy)
        assert tab.items["ride-0"].status == "ready" and len(accounts.posts) == 1
        tab.upload()
        wait_for(lambda: tab.items["ride-0"].status == "uploaded")
        assert len(accounts.posts) == 2
    finally:
        controller.close()
        controller.wait()
        root.destroy()


@pytest.mark.skipif(os.environ.get("MYWHOOSH_GUI_SMOKE_TEST") != "1",
                    reason="Opt-in saved GUI preferences test requiring a graphical desktop")
def test_native_device_selection_survives_close_without_preview(tmp_path):
    tk = pytest.importorskip("tkinter")
    from tkinter import ttk
    from mywhoosh_garmin_sync.desktop_controller import DesktopController
    from mywhoosh_garmin_sync.sync import SyncSettings, load_settings
    from test_sync import engine_for
    engine, accounts = engine_for(tmp_path)
    root = tk.Tk()
    controller = DesktopController(accounts, sync_engine=engine)
    window = gui.AccountWindow(root, tk, ttk, controller, can_remember=False,
                               sync_settings=SyncSettings())
    try:
        window.sync_tab.profile.set("Garmin Edge 530 (experimental)")
        window.sync_tab.unit.set("222222222")
        window.sync_tab.adjust_checkbox.invoke()
        window.close()  # Flush the debounce even when closing immediately.
        assert load_settings(engine.workspace).profile == "edge530"
        assert load_settings(engine.workspace).adjust_times is False
        assert not accounts.posts
    finally:
        if not window._closed:
            window.close()
    reopened = tk.Tk()
    next_controller = DesktopController(accounts, sync_engine=engine)
    next_window = gui.AccountWindow(reopened, tk, ttk, next_controller, can_remember=False,
                                    sync_settings=load_settings(engine.workspace))
    try:
        assert next_window.sync_tab.profile.get() == "Garmin Edge 530 (experimental)"
        assert next_window.sync_tab.unit.get() == "222222222"
        assert next_window.sync_tab.adjust_times.get() is True
        assert not accounts.posts
    finally:
        next_window.close()
