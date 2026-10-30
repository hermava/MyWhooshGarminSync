# Windows executable

Build on Windows with Python 3.12+ including Tk. The build script creates an
isolated environment in the Windows temporary directory and downloads pinned
project dependencies and PyInstaller from PyPI. It does not change your global
Python installation.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File packaging/build_windows.ps1
```

Use `-PythonExe C:\Path\To\python.exe` to select a particular Python installation.
The script copies only source files, synthetic tests and explicit build inputs.
Personal configuration, sessions, FIT files and upload records are not bundled.

Before publishing locally to `dist/`, the script runs native Windows tests and
launches the packaged EXE for an offline check of Tk, the Garmin backend, native
libraries, timezone data, time correction, private storage, process locking and SQLite. Credential Manager tests
use a unique synthetic namespace and remove their own records afterwards.
No real account sign-in or upload is performed by the build.
Native GUI checks exercise automatic local-clock conversion, the time checkbox,
device selection across restart without a preview, and a confirmed re-upload after
simulated deletion. The frozen EXE check verifies the simplified window, automatic
time conversion, bundled timezone database and synthetic saved preferences.

## Try the app

1. Copy `dist/MyWhooshGarminSync.exe` into a writable local folder on Windows.
2. Double-click it, connect your accounts and optionally choose **Stay signed in
   on this PC**. Saved tokens belong to the current Windows user; passwords are
   never saved.
3. Open **Sync rides**, configure your device profile and the **Timezone** where
   you trained, preview and confirm the
   selected rides. Use your own Unit ID for the experimental Edge 530 profile.
   Confirmed MyWhoosh time errors are corrected automatically; the **TIME** column
   shows the outcome and selecting a ride displays details. Valid device and timezone
   settings are saved automatically and restored on restart.
   **Adjust times automatically** is enabled by default. The upload confirmation
   shows the old/new times; uncheck the option to keep original timestamps.

Python does not need to be installed on the receiving PC. The EXE is an unsigned
development build. Its first launch can take a few seconds while bundled files
are unpacked. It requires a normal Windows desktop and internet access to sync.

Sync settings, original FITs and upload records are stored in `data/` beside the
EXE. Keep this folder when updating the executable. Moving only the EXE to a new
folder creates separate sync history, so keep the EXE and its data folder together.
The folder must support Windows permissions and hard links (use a local NTFS
folder). Do not run the app as administrator.

`dist/` also contains a SHA-256 file, an offline check report and the resolved
build dependency versions. These are build artifacts and are excluded from Git.
An offline build check does not verify real-account login, MFA or provider-side
acceptance; test those manually before distributing the app to friends.

Packaging uses [PyInstaller](https://pyinstaller.org/en/stable/operating-mode.html).
Windows executables must be built with Windows Python, including when the source
repository is accessed from WSL.
