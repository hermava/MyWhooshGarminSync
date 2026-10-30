"""English account setup, activity preview and explicitly confirmed workout sync."""

import sys
import unicodedata
from collections import deque
from pathlib import Path

from .accounts import AccountSessions
from .desktop_controller import DesktopController
from .errors import SessionError
from .gui_theme import COLORS, apply_theme

NAMES = {"mywhoosh": "MyWhoosh", "garmin": "Garmin Connect"}


def _storage_hint(can_remember):
    if can_remember:
        return "Your session stays on this Windows account. Your password is never saved."
    if sys.platform == "win32":
        return "Protected storage is unavailable on this PC. Sign-in will last for this run."
    return "Windows only: saved sign-in is unavailable when running under WSL / Linux."


def _display(value):
    if value is None:
        return "—"
    # Do not render provider-supplied controls or bidi overrides as UI instructions.
    return "".join(char if not unicodedata.category(char).startswith("C") else " "
                   for char in str(value))[:160]


def _rows(provider, result):
    if provider == "mywhoosh":
        return [(_display(item.title), _display(item.start_time.isoformat() if item.start_time else None),
                 _display(item.duration)) for item in result.activities]
    return [(_display(item.title), _display(item.start_time_utc),
             _display(f"{item.duration_seconds / 60:.1f} min" if item.duration_seconds is not None else None))
            for item in result]


class AccountWindow:
    def __init__(self, root, tk, ttk, controller, *, can_remember, sync_settings=None):
        self.root, self.tk, self.ttk = root, tk, ttk
        self.controller = controller
        self.can_remember = can_remember
        self._closed = False
        self._close_requested = False
        self.sync_tab = None
        self._dialog = None
        self._restore_pending = deque(NAMES) if can_remember else deque()
        self.panels = {}
        self.style = apply_theme(root, ttk)
        root.title("MyWhooshGarminSync")
        width = min(1080, max(860, root.winfo_screenwidth() - 80))
        height = min(800, max(600, root.winfo_screenheight() - 120))
        root.geometry(f"{width}x{height}")
        root.minsize(860, 600)
        root.protocol("WM_DELETE_WINDOW", self.close)
        # Suppress raw callback tracebacks, which can expose third-party details.
        root.report_callback_exception = self._callback_error
        content = ttk.Frame(root, padding=(32, 24))
        content.pack(fill="both", expand=True)
        content.columnconfigure(0, weight=1)
        content.columnconfigure(1, weight=1)
        content.rowconfigure(4, weight=1)
        ttk.Label(content, text="MYWHOOSH  /  GARMIN", style="Brand.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(content, text="MYWHOOSH GARMIN SYNC", style="Badge.TLabel").grid(row=0, column=1, sticky="e")
        title = ttk.Label(content, text="Your rides. Connected.", style="Title.TLabel")
        title.grid(row=1, column=0, columnspan=2, sticky="w", pady=(14, 6))
        intro = ttk.Label(content, text="Connect your accounts and preview your latest rides.", style="Subtitle.TLabel")
        intro.grid(row=2, column=0, columnspan=2, sticky="w")
        detail = ttk.Label(content, text="Preview your rides, select what to send, and sync to Garmin Connect." if sync_settings is not None
                           else "Account preview. Sync is available when the sync service is configured.", style="Subtitle.TLabel")
        detail.grid(row=3, column=0, columnspan=2, sticky="w", pady=(4, 14))
        self._hero = (title, intro, detail)
        self.notebook = ttk.Notebook(content)
        self.notebook.grid(row=4, column=0, columnspan=2, sticky="nsew")
        connection_page, activities = ttk.Frame(self.notebook), ttk.Frame(self.notebook)
        self.notebook.add(connection_page, text="Connections")
        self.notebook.add(activities, text="Activity preview")
        self.notebook.bind("<<NotebookTabChanged>>", self._tab_changed)
        if sync_settings is not None:
            from .gui_sync import SyncTab
            sync_page = ttk.Frame(self.notebook)
            self.notebook.add(sync_page, text="Sync rides")
            self.sync_tab = SyncTab(sync_page, tk, ttk, controller, sync_settings, _display)
        # Keep form actions accessible on smaller laptop screens without shrinking
        # text or truncating the storage explanation.
        connection_page.columnconfigure(0, weight=1)
        connection_page.rowconfigure(0, weight=1)
        self.connection_canvas = tk.Canvas(connection_page, background=COLORS["page"],
                                            highlightthickness=0, borderwidth=0)
        self.connection_canvas.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(connection_page, orient="vertical", command=self.connection_canvas.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.connection_canvas.configure(yscrollcommand=scroll.set)
        connections = ttk.Frame(self.connection_canvas)
        item = self.connection_canvas.create_window(0, 0, window=connections, anchor="nw")
        connections.bind("<Configure>", lambda event: self.connection_canvas.configure(
            scrollregion=self.connection_canvas.bbox("all")))
        self.connection_canvas.bind("<Configure>", lambda event: self.connection_canvas.itemconfigure(
            item, width=event.width))
        for page in (connections, activities):
            page.columnconfigure(0, weight=1, uniform="cards")
            page.columnconfigure(1, weight=1, uniform="cards")
            page.rowconfigure(0, weight=1)
        for column, provider in enumerate(NAMES):
            self._panel(connections, activities, provider, column)
        self._bind_scroll(connections)
        ttk.Label(content, text="Passwords are never saved. Originals and sync records are kept locally.",
                  style="Subtitle.TLabel").grid(row=5, column=0, columnspan=2, sticky="w", pady=(18, 10))
        self.progress = ttk.Progressbar(content, mode="indeterminate")
        self.progress.grid(row=6, column=0, columnspan=2, sticky="ew")
        # Remembering a session opts into restoring it on the next Windows start.
        # Linux / WSL never attempts Windows storage or restoration.
        if can_remember:
            root.after(120, self._restore_next)
        root.after(80, self._poll)

    def _bind_scroll(self, widget):
        widget.bind("<MouseWheel>", self._scroll_connections, add="+")
        widget.bind("<Button-4>", self._scroll_connections, add="+")
        widget.bind("<Button-5>", self._scroll_connections, add="+")
        for child in widget.winfo_children():
            self._bind_scroll(child)

    def _tab_changed(self, event=None):
        compact = self.sync_tab is not None and self.notebook.index("current") == 2
        for widget in self._hero:
            widget.grid_remove() if compact else widget.grid()

    def _scroll_connections(self, event):
        number = getattr(event, "num", None)
        delta = getattr(event, "delta", 0)
        if number in (4, 5):
            units = -1 if number == 4 else 1
        elif delta:
            units = (-1 if delta > 0 else 1) * max(1, abs(delta) // 120)
        else:
            return
        self.connection_canvas.yview_scroll(units, "units")
        return "break"

    def _panel(self, parent, activity_parent, provider, column):
        tk, ttk = self.tk, self.ttk
        box = ttk.Frame(parent, style="Card.TFrame", padding=24)
        box.grid(row=0, column=column, sticky="nsew", padx=(0, 10) if column == 0 else (10, 0))
        box.columnconfigure(0, weight=1)
        heading = ttk.Frame(box, style="CardBody.TFrame")
        heading.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(heading, text=NAMES[provider], style="CardTitle.TLabel").pack(side="left")
        badge = ttk.Label(heading, text="Not connected", style="Badge.TLabel")
        badge.pack(side="right", padx=(8, 0))
        ttk.Label(box, text="Your ride source" if provider == "mywhoosh" else "Your training destination",
                  style="CardMuted.TLabel").grid(row=1, column=0, sticky="w", pady=(0, 24))
        email, password, remember, status = tk.StringVar(), tk.StringVar(), tk.BooleanVar(value=False), tk.StringVar(
            value="Sign in to connect your account.")
        ttk.Label(box, text="Email address", style="Card.TLabel").grid(row=2, column=0, sticky="w")
        email_entry = ttk.Entry(box, textvariable=email)
        email_entry.grid(row=3, column=0, sticky="ew", pady=(6, 18))
        ttk.Label(box, text="Password", style="Card.TLabel").grid(row=4, column=0, sticky="w")
        password_entry = ttk.Entry(box, textvariable=password, show="•")
        password_entry.grid(row=5, column=0, sticky="ew", pady=(6, 16))
        password_entry.bind("<Return>", lambda event: self._login(provider))
        checkbox = ttk.Checkbutton(box, text="Stay signed in on this PC" if self.can_remember
                                    else "Stay signed in (Windows only)", variable=remember)
        checkbox.grid(row=6, column=0, sticky="w")
        if not self.can_remember:
            checkbox.state(["disabled"])
        ttk.Label(box, text=_storage_hint(self.can_remember), style="CardMuted.TLabel",
                  wraplength=350).grid(row=7, column=0, sticky="w", pady=(8, 20))
        login = ttk.Button(box, text="Connect account", style="Primary.TButton", command=lambda: self._login(provider))
        login.grid(row=8, column=0, sticky="ew")
        buttons = ttk.Frame(box, style="CardBody.TFrame")
        buttons.grid(row=9, column=0, sticky="ew", pady=(10, 16))
        buttons.columnconfigure(0, weight=1)
        read = ttk.Button(buttons, text="Refresh activities", command=lambda: self._read(provider))
        read.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        logout = ttk.Button(buttons, text="Sign out", style="Quiet.TButton", command=lambda: self._logout(provider))
        logout.grid(row=0, column=1)
        ttk.Label(box, textvariable=status, style="CardStatus.TLabel", wraplength=350).grid(row=10, column=0, sticky="w")
        preview = ttk.Frame(activity_parent, style="Card.TFrame", padding=20)
        preview.grid(row=0, column=column, sticky="nsew", padx=(0, 10) if column == 0 else (10, 0))
        preview.columnconfigure(0, weight=1)
        preview.rowconfigure(2, weight=1)
        ttk.Label(preview, text=NAMES[provider], style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(preview, text="Your latest rides will appear here after connecting.", style="CardMuted.TLabel",
                  wraplength=350).grid(row=1, column=0, sticky="w", pady=(8, 16))
        tree = ttk.Treeview(preview, columns=("title", "date", "duration"), show="headings", height=8)
        for name, label, width in (("title", "RIDE", 140), ("date", "START (UTC)", 120), ("duration", "DURATION", 85)):
            tree.heading(name, text=label)
            tree.column(name, width=width, minwidth=60)
        tree.grid(row=2, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(preview, orient="vertical", command=tree.yview)
        scrollbar.grid(row=2, column=1, sticky="ns")
        tree.configure(yscrollcommand=scrollbar.set)
        horizontal = ttk.Scrollbar(preview, orient="horizontal", command=tree.xview)
        horizontal.grid(row=3, column=0, sticky="ew")
        tree.configure(xscrollcommand=horizontal.set)
        self.panels[provider] = dict(email=email, password=password, remember=remember, status=status,
                                     tree=tree, badge=badge,
                                     controls=(email_entry, password_entry, login, read, logout), checkbox=checkbox)

    def _restore_next(self):
        if self._closed or self._close_requested or not self._restore_pending or self.controller.busy:
            return
        provider = self._restore_pending.popleft()
        if not self.controller.read(provider):
            self._restore_pending.appendleft(provider)

    def _manual_action(self, provider):
        if provider in self._restore_pending:
            self._restore_pending.remove(provider)

    def _read(self, provider):
        if self.controller.busy:
            return
        self._manual_action(provider)
        self.controller.read(provider)

    def _logout(self, provider):
        if self.controller.busy:
            return
        self._manual_action(provider)
        self.controller.sign_out(provider)

    def _badge(self, provider, text, style="Badge.TLabel"):
        self.panels[provider]["badge"].configure(text=text, style=style)

    def _login(self, provider):
        if self.controller.busy:
            return
        panel = self.panels[provider]
        email, password = panel["email"].get().strip(), panel["password"].get()
        if not email or not password:
            panel["status"].set("Enter your email and password to sign in.")
            return
        self._manual_action(provider)
        try:
            if self.controller.sign_in(provider, email, password, remember=panel["remember"].get()):
                panel["password"].set("")
        finally:
            password = ""

    def _set_busy(self, busy):
        if self.sync_tab is not None:
            self.sync_tab.set_busy(busy)
        for panel in self.panels.values():
            for widget in panel["controls"]:
                widget.state(["disabled"] if busy else ["!disabled"])
            panel["checkbox"].state(["disabled"] if busy or not self.can_remember else ["!disabled"])
        if busy:
            self.progress.start(15)
        else:
            self.progress.stop()

    def _poll(self):
        if self._closed:
            return
        for event in self.controller.events():
            if event.provider == "sync":
                if event.kind == "busy":
                    self._set_busy(True)
                elif event.kind == "idle":
                    self._set_busy(False)
                if self.sync_tab is not None:
                    self.sync_tab.handle(event)
                if event.kind == "sign_in_required" and event.value in self.panels:
                    self.panels[event.value]["status"].set("Please sign in to continue syncing.")
                    self._badge(event.value, "Sign in needed", "Attention.Badge.TLabel")
                    self.notebook.select(0)
                continue
            panel = self.panels[event.provider]
            if event.kind == "busy":
                self._set_busy(True)
                if self.sync_tab is not None and event.action in ("login", "logout"):
                    self.sync_tab.invalidate(clear=True)
                    self.sync_tab.message.set("Account sign-in changed. Preview again before syncing.")
                self._badge(event.provider, "Working…", "Connected.Badge.TLabel")
                if event.action in ("login", "logout"):
                    for item in panel["tree"].get_children():
                        panel["tree"].delete(item)
                panel["status"].set({"login": "Signing in…", "read": "Reading your activities…",
                                      "logout": "Signing out…"}[event.action])
            elif event.kind == "mfa":
                panel["status"].set("Enter your verification code to continue.")
                self._mfa_dialog(event.value)
            elif event.kind == "done":
                tree = panel["tree"]
                for item in tree.get_children():
                    tree.delete(item)
                panel["remember"].set(event.remembered)
                if event.action == "logout":
                    self._badge(event.provider, "Not connected")
                    panel["email"].set("")
                    panel["password"].set("")
                    panel["status"].set("Signed out on this PC.")
                else:
                    self._badge(event.provider, "Connected", "Connected.Badge.TLabel")
                    rows = _rows(event.provider, event.value)
                    for values in rows:
                        tree.insert("", "end", values=values)
                    panel["status"].set(f"Connected. {len(rows)} recent activities found.")
            elif event.kind == "sign_in_required":
                self._badge(event.provider, "Sign in needed", "Attention.Badge.TLabel")
                for item in panel["tree"].get_children():
                    panel["tree"].delete(item)
                panel["status"].set("Please sign in to continue.")
            elif event.kind == "error":
                self._badge(event.provider, "Needs attention", "Attention.Badge.TLabel")
                panel["status"].set(event.value)
            elif event.kind == "cancelled":
                self._badge(event.provider, "Not connected")
                panel["status"].set("Sign-in cancelled.")
            elif event.kind == "idle":
                self._set_busy(False)
        if self._close_requested and not self.controller.busy:
            self.close()
            return
        self._restore_next()
        self.root.after(80, self._poll)

    def _mfa_dialog(self, request):
        tk, ttk = self.tk, self.ttk
        dialog = tk.Toplevel(self.root)
        self._dialog = dialog
        dialog.title("Garmin verification")
        dialog.configure(background=self.root.cget("background"))
        dialog.transient(self.root)
        dialog.resizable(False, False)
        body = ttk.Frame(dialog, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Enter the code Garmin sent you.").pack(anchor="w")
        code, error = tk.StringVar(), tk.StringVar()
        entry = ttk.Entry(body, textvariable=code, show="•", width=28)
        entry.pack(fill="x", pady=12)
        ttk.Label(body, textvariable=error).pack(anchor="w")

        def finish(cancel=False):
            if not cancel and not request.submit(code.get()):
                error.set("Enter a code with 6 to 8 digits.")
                return
            code.set("")
            if cancel:
                request.cancel()
            dialog.grab_release()
            dialog.destroy()
            self._dialog = None

        buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="Continue", style="Primary.TButton", command=finish).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=lambda: finish(True)).pack(side="right", padx=8)
        entry.bind("<Return>", lambda event: finish())
        dialog.protocol("WM_DELETE_WINDOW", lambda: finish(True))
        dialog.grab_set()
        entry.focus_set()

    def _callback_error(self, *args):
        for panel in self.panels.values():
            panel["password"].set("")
            panel["status"].set("The window could not complete this action. Please restart the app.")

    def close(self):
        if self.controller.busy:
            self._close_requested = True
            self._restore_pending.clear()
            self.controller.cancel()
            if self.sync_tab is not None:
                self.sync_tab.message.set("Finishing the current request before closing. No further rides will be sent.")
            return
        self._closed = True
        if self.sync_tab is not None:
            self.sync_tab.flush_settings()
        self.controller.close()
        for panel in self.panels.values():
            panel["password"].set("")
        self.root.destroy()


def main():
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError:
        print("The account window requires Python with Tk support.", file=sys.stderr)
        return 1
    try:
        root = tk.Tk()
    except tk.TclError:
        print("The account window could not open. A graphical desktop is required.", file=sys.stderr)
        return 1
    from .windows_credentials import WindowsSessionStore

    try:
        store = WindowsSessionStore()
    except SessionError:
        store = None
    from .workspace import Workspace, default_sync_directory
    from .sync import SyncEngine, SyncSettings, load_settings

    workspace = Workspace(default_sync_directory())
    try:
        with workspace.locked():
            settings_notice = False
            try:
                settings = load_settings(workspace)
            except SessionError:
                settings = SyncSettings()
                settings_notice = True
            accounts = AccountSessions(store=store)
            legacy = workspace.root / "garmin-uploads" if getattr(sys, "frozen", False) else Path("private/garmin-uploads")
            engine = SyncEngine(accounts, workspace, legacy_directory=legacy)
            controller = DesktopController(accounts, sync_engine=engine)
            window = AccountWindow(root, tk, ttk, controller, can_remember=store is not None, sync_settings=settings)
            if settings_notice:
                window.sync_tab.message.set("Saved device settings could not be read. Check your profile and Unit ID before previewing.")
            try:
                root.mainloop()
            finally:
                controller.close()
                controller.wait()  # Keep the process lock until any in-flight upload is recorded.
    except SessionError as error:
        from tkinter import messagebox
        messagebox.showerror("Sync data unavailable", str(error), parent=root)
        root.destroy()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
