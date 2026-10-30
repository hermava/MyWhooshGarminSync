# Desktop app

The first GUI provides English sign-in for MyWhoosh and Garmin, optional saved
sessions, a Garmin verification-code dialog, recent activity previews and local
sign-out. It uses the same account and sync services as the CLI. The light interface
has Connections, Activity preview and Sync rides tabs, account cards and connection badges.
Forms scroll on smaller laptop screens; activity tables have their own scrollbars.

**The Sync rides tab can upload real workouts after explicit confirmation.**
See [Windows builds](windows-build.md) for the EXE build and testing instructions.
The complete workflow is tested with synthetic data; live sync still needs
user-run checks.

## Start from source

Use a Python 3.12+ environment with the project and Garmin dependencies installed
(see the [README](../README.md)), plus Python's optional Tk support. With that
environment active:

```bash
python -m mywhoosh_garmin_sync.gui
```

After reinstalling the editable package, `mywhoosh-sync-gui` is also available.
The Windows GUI entry point is configured to launch without a console.
The Windows build has its own dependency lock and runs native checks before
producing the executable.

[Tkinter](https://docs.python.org/3.12/library/tkinter.html) must be available in
the selected Python installation, along with a graphical desktop. Tk is now
installed in the Linux/WSL development environment and the real widget smoke test
has passed there with a fake controller. Native Windows widget tests also pass,
including preview and a simulated upload. Installing the Python package alone
does not install Tk.

On Ubuntu 24.04 with the system Python 3.12, install the missing OS package in
your own terminal, then retry using the existing virtual environment:

```bash
sudo apt install python3-tk
.venv/bin/python -m mywhoosh_garmin_sync.gui
```

This installs Tk support for the base Python; recreating the virtual environment
is not necessary. WSL also requires a graphical desktop connection such as WSLg.

## Use the preview

1. Enter your email and password in the relevant account panel, then choose
   **Connect account**. Password fields are masked and cleared when the operation starts.
2. Optionally select **Stay signed in on this PC** before signing in. This stores
   session tokens in Windows Credential Manager, never the password. If protected
   storage is unavailable, the checkbox is disabled and sign-in is memory-only.
   Linux / WSL cannot use Windows Credential Manager: this is stated next to the
   disabled checkbox. Saved sessions are not silently written to a plain file.
3. Enter a Garmin verification code if requested. You can cancel that dialog.
4. On a subsequent Windows run, the window automatically checks saved sessions and
   reads recent activities, one provider at a time. No password is requested unless
   sign-in is needed. Choose **Refresh activities** to read again, and open the
   **Activity preview** tab to view the results. Linux / WSL uses the current
   in-memory session only; restarting requires another login.
5. Open **Sync rides**, choose the date range, maximum ride count, device profile
   and **Timezone** where you trained (for example `Europe/Berlin`),
   then choose **Preview sync**. It downloads and validates FITs locally, compares
   recent Garmin activities and shows which rides are ready. An optional Edge 530
   profile requires your own Unit ID; existing `config.local.toml` device settings
   are used initially if no GUI settings have been saved. Confirmed MyWhoosh time
   errors are corrected automatically in a separate copy before the device profile.
   Valid device, Unit ID, timezone and scope settings are saved automatically,
   including when closing without a preview, and restored on the next start.
   Incomplete edits keep the last valid settings. **Adjust times automatically**
   starts checked on every launch and appears beside the timezone in the settings area. For affected
   MyWhoosh recordings, it converts local clocks to UTC using your workout timezone
   and fixes finish metadata. Uncheck it to send original times unchanged.
   The TIME column shows the result. Select a ride for details; no separate time
   review or Garmin-ID verification is needed. Unresolvable times remain blocked
   while other ready rides are selectable.
6. The newest ready ride is checked by default. Toggle the SYNC column or use
   **Select ready** to change the selection. **Sync selected** displays a confirmation
   with the selected rides, profile and any time corrections before anything is uploaded.
7. The upload confirmation shows the actual old/new times for selected rides.
   Check uncertain upload results directly in Garmin Connect. If you manually
   deleted a confirmed upload, preview again, highlight that ride and choose
   **Retry deleted**. Deletion is checked before preparing a new preview; a new
   upload still requires confirmation. Existing history is kept.
8. **Sign out** forgets that provider's session locally. It does not revoke a token
   at the provider or remove the separate legacy MyWhoosh POSIX session file.

Requests run in a background thread so the window remains responsive. Only one
account operation runs at a time in each window. Messages distinguish required
sign-in from temporary connectivity/service errors, and raw exception details are
not displayed. Account data is shown locally in the activity preview, not logged.
The theme uses the existing Tk/ttk widgets and adds no third-party UI dependency.

After a preview, settings collapse into a summary to keep the ride table visible
on smaller screens. **Edit settings** opens them again; changing a setting requires
a new preview. See the [sync guide](sync.md) for state, recovery and scope limits.

The checkbox is unchecked by default. Signing into another account without it
removes that provider's previous saved session after successful login. When an
existing saved session is read successfully, the checkbox reflects that saved state.

## Development checks and remaining work

The normal offline tests cover worker serialization, error redaction, MFA handoff,
password-field clearing, sync selection/confirmation, durable state and crash recovery. They use
synthetic data and do not open a window.

For the opt-in widget smoke test on a graphical Windows development host,
activate the environment and run in PowerShell:

```powershell
$env:MYWHOOSH_GUI_SMOKE_TEST = "1"
python -m pytest -q tests/test_gui.py
```

Those tests create windows with fake accounts and use synthetic FITs through the
real sync service. They never access real accounts or saved credentials. The native
Windows run passed, including an isolated Credential Manager round trip. Appearance,
keyboard navigation, scaling and real sign-in / MFA still need manual checks.
The normal suite skips the widget test unless explicitly enabled.

**Stop** finishes an in-flight request and prevents further rides from being sent.
Closing during an operation waits for it to finish and record its outcome before
the app exits. A process lock prevents a second app using the same data directory.
If an upload is unclear, do not remove its database or journal records to retry.

Next: user-run verification of real sign-in, saved sessions and sync in the packaged
app. The everyday workflow is a double-click after training, with no terminal or
manual JSON editing.
