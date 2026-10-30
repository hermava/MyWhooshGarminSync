"""Sync tab shared with the desktop account window."""

from zoneinfo import available_timezones

from .errors import SessionError
from .sync import SyncSettings

STATUS = {"ready": "Ready", "uploaded": "Already synced", "needs_review": "Check Garmin",
          "possible_duplicate": "Possible duplicate", "failed": "Not ready", "blocked": "Not ready"}
TIME_STATUS = {"corrected": "Corrected", "approved": "Approved", "unchanged": "Unchanged", "not_applicable": "Unchanged",
               "needs_timezone": "Choose timezone", "needs_review": "Time unavailable", "disabled": "Original times"}
DETAIL = {
    "previous_attempt": "A previous upload record exists. Check the activity in Garmin before continuing.",
    "remote_candidate": "A matching activity may already be in Garmin. Check it in Garmin Connect before continuing.",
    "inconclusive_match": "Recent Garmin activities do not provide enough data for a safe match.",
    "preparation_failed": "The FIT could not be prepared safely. Its original is preserved.",
    "pre_send_failed": "No upload was started for this ride. Check your connection and preview again.",
    "timezone_required": "Choose the timezone where you trained, then preview again. Nothing has been uploaded.",
    "time_review": "Times could not be adjusted reliably. Check your timezone or disable automatic adjustment to keep the original times.",
}


class SyncTab:
    def __init__(self, parent, tk, ttk, controller, settings, display):
        self.controller, self.display = controller, display
        self.plan = None
        self.items = {}
        self.selected = set()
        self.row_ids = {}
        self._busy = False
        self._settings_host = parent
        self._settings_after = None
        engine = getattr(controller, "sync_engine", None)
        self._settings_writer = engine.save_settings if engine is not None else None
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        settings_box = ttk.Frame(parent, style="Card.TFrame", padding=18)
        self.settings_box = settings_box
        settings_box.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        settings_box.columnconfigure(1, weight=1)
        self.profile = tk.StringVar(value="Garmin Edge 530 (experimental)" if settings.profile else "Keep original device")
        self.unit = tk.StringVar(value=str(settings.unit_id) if settings.unit_id is not None else "")
        self.days = tk.StringVar(value=str(settings.days))
        self.limit = tk.StringVar(value=str(settings.limit))
        self.timezone = tk.StringVar(value=settings.timezone or "")
        self.adjust_times = tk.BooleanVar(value=True)
        ttk.Label(settings_box, text="Device profile", style="Card.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 12))
        profile = ttk.Combobox(settings_box, textvariable=self.profile, state="readonly",
                               values=("Keep original device", "Garmin Edge 530 (experimental)"), width=30)
        profile.grid(row=0, column=1, sticky="ew")
        ttk.Label(settings_box, text="Unit ID", style="Card.TLabel").grid(row=0, column=2, padx=(18, 8))
        unit = ttk.Entry(settings_box, textvariable=self.unit, width=14, show="•")
        unit.grid(row=0, column=3)
        ttk.Label(settings_box, text="Past days", style="Card.TLabel").grid(row=1, column=0, sticky="w", pady=(12, 0))
        days = ttk.Spinbox(settings_box, from_=1, to=365, textvariable=self.days, width=6)
        days.grid(row=1, column=1, sticky="w", pady=(12, 0))
        ttk.Label(settings_box, text="Max rides", style="Card.TLabel").grid(row=1, column=2, padx=(18, 8), pady=(12, 0))
        limit = ttk.Spinbox(settings_box, from_=1, to=20, textvariable=self.limit, width=6)
        limit.grid(row=1, column=3, sticky="w", pady=(12, 0))
        ttk.Label(settings_box, text="Timezone", style="Card.TLabel").grid(row=2, column=0, sticky="w", pady=(12, 0))
        zone = ttk.Combobox(settings_box, textvariable=self.timezone, state="readonly",
                             values=("", *sorted(available_timezones())), width=30)
        zone.grid(row=2, column=1, sticky="ew", pady=(12, 0))
        self.adjust_checkbox = ttk.Checkbutton(settings_box, text="Adjust times automatically", variable=self.adjust_times)
        self.adjust_checkbox.grid(row=2, column=2, columnspan=2, sticky="w", padx=(18, 0), pady=(12, 0))
        ttk.Label(settings_box, text="Use the timezone where you trained. Your settings are saved automatically. "
                  "The Edge 530 profile is experimental; Garmin training metrics are not guaranteed.",
                  style="CardMuted.TLabel", wraplength=720).grid(row=3, column=0, columnspan=4, sticky="w", pady=(12, 0))
        table = ttk.Frame(parent, style="Card.TFrame", padding=16)
        table.grid(row=1, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(table, columns=("pick", "title", "date", "status", "time"), show="headings", selectmode="browse", height=5)
        for name, text, width in (("pick", "SYNC", 55), ("title", "RIDE", 210), ("date", "START (UTC)", 165),
                                  ("status", "STATUS", 135), ("time", "TIME", 135)):
            self.tree.heading(name, text=text)
            self.tree.column(name, width=width, minwidth=45, stretch=name != "pick")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<ButtonRelease-1>", self._toggle)
        self.tree.bind("<<TreeviewSelect>>", self._details)
        self.message = tk.StringVar(value="Choose your scope, then preview. No activities are sent during preview.")
        ttk.Label(parent, textvariable=self.message, wraplength=820, style="Subtitle.TLabel").grid(row=2, column=0, sticky="w", pady=(12, 8))
        actions = ttk.Frame(parent)
        actions.grid(row=3, column=0, sticky="ew")
        self.preview_button = ttk.Button(actions, text="Preview sync", command=self.preview)
        self.preview_button.pack(side="left", padx=(0, 8))
        self.all_button = ttk.Button(actions, text="Select ready", command=self.select_all)
        self.all_button.pack(side="left", padx=(0, 8))
        self.retry_button = ttk.Button(actions, text="Retry deleted", command=self.retry_deleted)
        self.retry_button.pack(side="left", padx=(0, 8))
        self.cancel_button = ttk.Button(actions, text="Stop", command=controller.cancel)
        self.cancel_button.pack(side="right", padx=(8, 0))
        self.upload_button = ttk.Button(actions, text="Sync selected", style="Primary.TButton", command=self.upload)
        self.upload_button.pack(side="right")
        self.summary = tk.StringVar()
        self.summary_box = ttk.Frame(parent, style="Card.TFrame", padding=12)
        self.summary_box.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        self.summary_box.columnconfigure(0, weight=1)
        ttk.Label(self.summary_box, textvariable=self.summary, style="Card.TLabel").grid(row=0, column=0, sticky="w")
        edit = ttk.Button(self.summary_box, text="Edit settings", command=self._show_settings)
        edit.grid(row=0, column=1, padx=(12, 0))
        self.summary_box.grid_remove()
        self.inputs = (profile, unit, days, limit, zone, edit, self.adjust_checkbox)
        for variable in (self.profile, self.unit, self.days, self.limit, self.timezone, self.adjust_times):
            variable.trace_add("write", self._settings_changed)
        self.set_busy(False)

    def _settings_changed(self, *args):
        self.invalidate()
        if self._settings_writer is not None:
            if self._settings_after is not None:
                self._settings_host.after_cancel(self._settings_after)
            self._settings_after = self._settings_host.after(600, self.flush_settings)

    def flush_settings(self):
        callback = getattr(self, "_settings_writer", None)
        if callback is None:
            return True
        if self._settings_after is not None:
            self._settings_host.after_cancel(self._settings_after)
            self._settings_after = None
        try:
            settings = self.settings()
        except SessionError:
            # Keep the last complete settings while a field is being edited.
            return False
        try:
            callback(settings)
            return True
        except SessionError:
            self.message.set("Settings could not be saved. Check the app's data folder before closing.")
            return False

    def _show_settings(self):
        if self._busy:
            return
        self.summary_box.grid_remove()
        self.settings_box.grid()

    def settings(self):
        try:
            return SyncSettings(days=int(self.days.get()), limit=int(self.limit.get()),
                                profile="edge530" if self.profile.get().startswith("Garmin") else None,
                                unit_id=int(self.unit.get()) if self.unit.get().strip() else None,
                                timezone=self.timezone.get() or None, adjust_times=self.adjust_times.get())
        except ValueError:
            raise SessionError("Days, ride limit and Unit ID must be whole numbers.") from None

    def invalidate(self, *, clear=False):
        self.plan = None
        self.selected.clear()
        if clear:
            for row in self.tree.get_children():
                self.tree.delete(row)
            self.items.clear()
            self.row_ids.clear()
        self._render_rows()
        self.message.set("Settings changed. Preview again before syncing.")
        self.set_busy(self._busy)

    def _render_rows(self):
        for row_id, source_id in self.row_ids.items():
            item = self.items[source_id]
            self.tree.item(row_id, values=("✓" if source_id in self.selected else "", self.display(item.title),
                                          self.display(item.start_utc), STATUS.get(item.status, "Check Garmin"),
                                          TIME_STATUS.get(item.time_status, "—")))

    def set_busy(self, busy):
        self._busy = busy
        for widget in (*self.inputs, self.preview_button):
            widget.state(["disabled"] if busy else ["!disabled"])
        self.all_button.state(["!disabled"] if not busy and self.plan and any(i.status == "ready" for i in self.items.values()) else ["disabled"])
        self.upload_button.state(["!disabled"] if not busy and self.plan and self.selected else ["disabled"])
        self.retry_button.state(["!disabled"] if not busy and self.plan and any(i.status == "uploaded" and i.garmin_id for i in self.items.values()) else ["disabled"])
        self.cancel_button.state(["!disabled"] if busy else ["disabled"])

    def preview(self):
        if self.controller.busy:
            return
        try:
            settings = self.settings()
        except SessionError as error:
            self.message.set(str(error))
            return
        self.invalidate()
        if self.controller.preview_sync(settings):
            self.message.set("Checking rides, preparing FIT files and comparing with Garmin…")

    def select_all(self):
        if self._busy or self.plan is None:
            return
        ready = {item.source_id for item in self.items.values() if item.status == "ready"}
        self.selected = set() if self.selected == ready else ready
        self._render_rows()
        self.set_busy(False)

    def _toggle(self, event):
        if self._busy or self.plan is None:
            return
        source_id = self.row_ids.get(self.tree.identify_row(event.y))
        if self.tree.identify_column(event.x) != "#1":
            return
        if source_id and self.items[source_id].status == "ready":
            if source_id in self.selected:
                self.selected.remove(source_id)
            else:
                self.selected.add(source_id)
            self._render_rows()
            self.set_busy(False)

    def _details(self, event=None):
        row = self.tree.selection()
        if row and row[0] in self.row_ids:
            item = self.items[self.row_ids[row[0]]]
            detail = DETAIL.get(item.reason, "This ride is ready to sync." if item.status == "ready"
                                else "Check this ride in Garmin before any further upload.")
            if item.time_detail:
                detail += "\n" + "\n".join(self.display(part) for part in item.time_detail.split("; "))
            self.message.set(detail)

    def upload(self):
        if self.controller.busy or self.plan is None or not self.selected:
            return
        from tkinter import messagebox
        titles = "\n".join("• " + self.display(self.items[key].title) for key in self.selected)
        profile = "Garmin Edge 530 (experimental)" if self.plan.settings.profile else "Keep original device"
        corrected = sum(self.items[key].time_status in ("corrected", "approved") for key in self.selected)
        time_note = f"\nTime corrected for {corrected} ride(s), using {self.plan.settings.timezone}." if corrected else ""
        for item in self.plan.items:
            if item.source_id in self.selected and item.time_status in ("corrected", "approved"):
                time_note += f"\n\n{self.display(item.title)}\n" + "\n".join(self.display(part) for part in item.time_detail.split("; "))
        if not self.plan.settings.adjust_times:
            time_note += "\nAutomatic time adjustment is off. Original timestamps will be sent unchanged."
        if not messagebox.askyesno("Sync selected rides", f"Upload {len(self.selected)} selected ride(s) to Garmin Connect?\n\n{titles}\n\nProfile: {profile}{time_note}\nOlder duplicates outside the recent Garmin window are not ruled out.", parent=self.tree.winfo_toplevel()):
            return
        if self.controller.execute_sync(self.plan.identity, tuple(i.source_id for i in self.plan.items if i.source_id in self.selected)):
            self.plan = None
            self.selected.clear()

    def retry_deleted(self):
        if self.controller.busy or self.plan is None:
            return
        rows = self.tree.selection()
        if not rows:
            self.message.set("Highlight the confirmed upload you deleted from Garmin first.")
            return
        item = self.items[self.row_ids[rows[0]]]
        if item.status != "uploaded" or not item.garmin_id:
            self.message.set("Only confirmed app uploads with a known Garmin ID can be reopened here. Unclear upload records stay blocked.")
            return
        from tkinter import messagebox
        if not messagebox.askyesno("Check deleted Garmin activity",
                                  f"Did you delete this activity from Garmin Connect?\n\n{self.display(item.title)}\nGarmin ID: {item.garmin_id}\n\n"
                                  "The app will check deletion and prepare a new preview. Existing history is kept. Nothing is uploaded yet.",
                                  parent=self.tree.winfo_toplevel()):
            return
        if self.controller.retry_deleted(self.plan.identity, item.source_id):
            self.plan = None
            self.selected.clear()
            self.message.set("Checking that the confirmed activity was deleted from Garmin…")

    def handle(self, event):
        if event.kind == "progress":
            self.message.set(event.value[1])
        elif event.kind == "done" and event.action in ("preview", "retry_deleted"):
            self.plan = event.value
            self.summary.set(f"Past {self.plan.settings.days} days · Up to {self.plan.settings.limit} rides · "
                             + ("Edge 530 (experimental)" if self.plan.settings.profile else "Original device")
                             + f" · {self.plan.settings.timezone or 'Choose timezone for time repair'}")
            self.settings_box.grid_remove()
            self.summary_box.grid()
            self.items = {item.source_id: item for item in self.plan.items}
            self.selected = set()
            self.row_ids.clear()
            for row in self.tree.get_children():
                self.tree.delete(row)
            for index, item in enumerate(self.plan.items):
                row = self.tree.insert("", "end", values=("", "", "", ""))
                self.row_ids[row] = item.source_id
                if item.status == "ready" and not self.selected:
                    self.selected.add(item.source_id)  # First run defaults to the newest ready ride only.
            self._render_rows()
            ready = sum(item.status == "ready" for item in self.items.values())
            corrected = sum(item.time_status in ("corrected", "approved") for item in self.items.values())
            time_review = sum(item.time_status in ("needs_review", "needs_timezone") for item in self.items.values())
            self.message.set(("Deletion confirmed. New preview prepared; nothing uploaded. " if event.action == "retry_deleted" else "")
                             + f"{ready} ready of {len(self.items)} rides. Only checked rides will be sent. "
                             + (f"{corrected} time corrected. " if corrected else "")
                             + (f"{time_review} could not adjust time; check the timezone. " if time_review else "")
                             + ("The preview is limited; older rides may be outside it. " if self.plan.truncated else "")
                             + ("Earlier unresolved uploads are also shown for review. " if self.plan.review_count else "")
                             + "Matching checks up to 100 recent Garmin activities.")
        elif event.kind == "done" and event.action == "upload":
            for item in event.value.items:
                self.items[item.source_id] = item
            self.plan = None
            self.selected.clear()
            self._render_rows()
            count = sum(item.status == "uploaded" for item in event.value.items)
            corrected = sum(item.status == "uploaded" and item.time_status in ("corrected", "approved") for item in event.value.items)
            self.message.set(f"{count} ride(s) confirmed in Garmin. " + (f"{corrected} with corrected time. " if corrected else "")
                             + ("Stopped. Check any unclear results before continuing." if event.value.stopped else "Sync complete.")
                             + " Preview again for the latest status.")
        elif event.kind == "sign_in_required":
            self.plan = None
            self.message.set("Please sign in to both accounts, then preview again.")
        elif event.kind == "error":
            self.plan = None
            self.message.set((event.value or "Deletion could not be confirmed. Existing upload records were kept.")
                             if event.action == "retry_deleted" else
                             "Sync could not continue. Check your accounts, connection and device settings, then preview again.")
        elif event.kind == "cancelled":
            self.plan = None
            self.message.set("Stopped. Preview again when you are ready.")
        self.set_busy(self._busy)
