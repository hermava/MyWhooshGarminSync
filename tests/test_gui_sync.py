from dataclasses import replace
from types import SimpleNamespace

from mywhoosh_garmin_sync.desktop_controller import DesktopEvent
from mywhoosh_garmin_sync.gui_sync import SyncTab
from mywhoosh_garmin_sync.sync import SyncItem, SyncPlan, SyncResult, SyncSettings
from test_gui import Variable
from test_sync import NOW


class Button:
    def state(self, values):
        self.disabled = "disabled" in values

    def grid(self):
        pass

    def grid_remove(self):
        pass


class Tree:
    def __init__(self):
        self.rows = {}
        self.picked = ()

    def get_children(self):
        return tuple(self.rows)

    def delete(self, row):
        del self.rows[row]

    def insert(self, *args, values):
        row = f"row-{len(self.rows)}"
        self.rows[row] = values
        return row

    def item(self, row, *, values):
        self.rows[row] = values

    def selection(self):
        return self.picked

    def winfo_toplevel(self):
        return None


def tab():
    result = object.__new__(SyncTab)
    result.controller = SimpleNamespace(busy=False)
    result.display = str
    result.plan = None
    result.items, result.row_ids, result.selected = {}, {}, set()
    result._busy = False
    result.profile, result.unit = Variable("Original FIT"), Variable("")
    result.days, result.limit = Variable("7"), Variable("10")
    result.timezone = Variable("")
    result.adjust_times = Variable(True)
    result.message = Variable()
    result.summary = Variable()
    result.settings_box, result.summary_box = Button(), Button()
    result.tree = Tree()
    result.inputs = (Button(),)
    for name in ("preview_button", "upload_button", "verify_button", "time_button", "retry_button", "cancel_button", "all_button"):
        setattr(result, name, Button())
    return result


def plan():
    return SyncPlan("synthetic-plan", "a" * 64, SyncSettings(), NOW,
                    (SyncItem("newest", "Synthetic Newest", NOW.isoformat(), "ready"),
                     SyncItem("older", "Synthetic Older", NOW.isoformat(), "ready"),
                     SyncItem("done", "Synthetic Done", NOW.isoformat(), "uploaded"),
                     SyncItem("review", "Synthetic Unclear", NOW.isoformat(), "needs_review")))


def test_first_preview_selects_only_newest_ready_not_whole_history():
    ui = tab()
    ui.handle(DesktopEvent("done", "sync", "preview", plan()))
    assert ui.selected == {"newest"}
    assert not ui.upload_button.disabled
    ui.select_all()
    assert ui.selected == {"newest", "older"}
    ui.select_all()
    assert not ui.selected
    assert ui.upload_button.disabled


def test_editing_settings_invalidates_previous_consent():
    ui = tab()
    ui.handle(DesktopEvent("done", "sync", "preview", plan()))
    ui.days.set("30")
    ui.invalidate()
    assert ui.plan is None
    assert not ui.selected
    assert ui.upload_button.disabled


def test_account_change_clears_previous_accounts_sync_metadata():
    ui = tab()
    ui.handle(DesktopEvent("done", "sync", "preview", plan()))
    ui.invalidate(clear=True)
    assert not ui.items
    assert not ui.tree.rows
    assert ui.plan is None
    assert ui.upload_button.disabled


def test_declined_upload_confirmation_does_not_call_engine(monkeypatch):
    from tkinter import messagebox
    monkeypatch.setattr(messagebox, "askyesno", lambda *args, **kwargs: False)
    ui = tab()
    ui.handle(DesktopEvent("done", "sync", "preview", plan()))
    ui.upload()  # No execute_sync exists on the fake controller.
    assert ui.plan is not None


def test_confirmation_sends_exact_selection_only_once(monkeypatch):
    from tkinter import messagebox
    monkeypatch.setattr(messagebox, "askyesno", lambda *args, **kwargs: True)
    ui = tab()
    calls = []
    ui.controller.execute_sync = lambda identity, ids: calls.append((identity, ids)) or True
    ui.handle(DesktopEvent("done", "sync", "preview", plan()))
    ui.upload()
    ui.upload()
    assert calls == [("synthetic-plan", ("newest",))]
    assert ui.plan is None


def test_batch_result_updates_rows_and_requires_new_preview():
    ui = tab()
    preview = plan()
    ui.handle(DesktopEvent("done", "sync", "preview", preview))
    result = SyncResult((replace(preview.items[0], status="uploaded", garmin_id="123"),), stopped=True)
    ui.handle(DesktopEvent("done", "sync", "upload", result))
    assert ui.items["newest"].status == "uploaded"
    assert ui.plan is None
    assert ui.upload_button.disabled
    assert "Stopped" in ui.message.get()


def test_invalid_device_settings_do_not_start_preview():
    ui = tab()
    ui.profile.set("Garmin Edge 530 (experimental)")
    ui.preview()
    assert "Unit ID" in ui.message.get()


def test_preferences_save_without_preview_and_incomplete_edits_keep_last_valid_value():
    ui = tab()
    saved = []
    ui._settings_writer = saved.append
    ui._settings_after = None
    ui.profile.set("Garmin Edge 530 (experimental)")
    ui.unit.set("222222222")
    assert ui.flush_settings()
    assert saved[-1].profile == "edge530"
    ui.unit.set("")
    assert not ui.flush_settings()
    assert len(saved) == 1 and saved[-1].unit_id == 222222222


def test_deleted_retry_requires_confirmation_and_never_calls_upload(monkeypatch):
    from tkinter import messagebox
    ui = tab()
    preview = plan()
    preview = replace(preview, items=(replace(preview.items[2], garmin_id="123"),))
    ui.handle(DesktopEvent("done", "sync", "preview", preview))
    ui.tree.picked = (next(iter(ui.row_ids)),)
    calls = []
    ui.controller.retry_deleted = lambda *args: calls.append(args) or True
    monkeypatch.setattr(messagebox, "askyesno", lambda *args, **kwargs: False)
    ui.retry_deleted()
    assert not calls and ui.plan is not None
    monkeypatch.setattr(messagebox, "askyesno", lambda *args, **kwargs: True)
    ui.retry_deleted()
    assert calls == [(preview.identity, "done")]
    assert ui.plan is None and not ui.selected


def test_timezone_is_explicit_and_changing_it_requires_a_new_preview():
    ui = tab()
    assert ui.settings().timezone is None
    ui.timezone.set("Europe/Berlin")
    assert ui.settings().timezone == "Europe/Berlin"
    ui.handle(DesktopEvent("done", "sync", "preview", plan()))
    ui.timezone.set("UTC")
    ui.invalidate()
    assert ui.plan is None and not ui.selected
    ui.timezone.set("Not/AZone")
    ui.preview()
    assert "valid timezone" in ui.message.get()


def test_time_feedback_shows_in_table_details_confirmation_and_result(monkeypatch):
    from tkinter import messagebox
    ui = tab()
    preview = plan()
    corrected = replace(preview.items[0], time_status="corrected",
                        time_detail="Time corrected using Europe/Berlin. UTC finish: old → new; Local finish: old → new")
    blocked = replace(preview.items[1], status="blocked", reason="timezone_required", time_status="needs_timezone")
    preview = replace(preview, settings=SyncSettings(timezone="Europe/Berlin"), items=(corrected, blocked))
    ui.handle(DesktopEvent("done", "sync", "preview", preview))
    assert ui.selected == {"newest"}
    assert "1 time corrected" in ui.message.get()
    assert list(ui.tree.rows.values())[0][-1] == "Corrected"
    assert list(ui.tree.rows.values())[1][-1] == "Choose timezone"
    ui.tree.picked = (next(iter(ui.row_ids)),)
    ui._details()
    assert "Local finish" in ui.message.get() and "Europe/Berlin" in ui.message.get()
    confirmations = []
    monkeypatch.setattr(messagebox, "askyesno", lambda title, body, **kwargs: confirmations.append(body) or False)
    ui.upload()
    assert "Time corrected for 1 ride(s), using Europe/Berlin" in confirmations[0]
    assert "UTC finish: old → new" in confirmations[0] and "Local finish: old → new" in confirmations[0]
    ui.handle(DesktopEvent("done", "sync", "upload", SyncResult((replace(corrected, status="uploaded"),))))
    assert "with corrected time" in ui.message.get()
    assert list(ui.tree.rows.values())[0][-1] == "Corrected"


def test_time_checkbox_invalidates_preview_and_disabled_confirmation_is_explicit(monkeypatch):
    from tkinter import messagebox
    ui = tab()
    ui._settings_writer = None
    ui.handle(DesktopEvent("done", "sync", "preview", plan()))
    ui.adjust_times.set(False)
    ui._settings_changed()
    assert ui.plan is None and not ui.selected
    assert ui.settings().adjust_times is False
    preview = replace(plan(), settings=SyncSettings(adjust_times=False))
    ui.handle(DesktopEvent("done", "sync", "preview", preview))
    prompts = []
    monkeypatch.setattr(messagebox, "askyesno", lambda title, body, **kwargs: prompts.append(body) or False)
    ui.upload()
    assert "Original timestamps will be sent unchanged" in prompts[0]
    assert ui.plan is not None
