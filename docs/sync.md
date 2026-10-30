# Sync workflow

The desktop **Sync rides** tab and the CLI `sync` command use the same service:
discover recent MyWhoosh rides, download originals, validate FIT structure/CRC/times,
repair confirmed MyWhoosh time errors, optionally prepare an Edge 530 copy, compare Garmin activities, then upload only the
rides explicitly selected and confirmed by the user. A local Windows test EXE is
available; see the [Windows guide](windows-build.md).

The new complete workflow has been tested with synthetic FIT files and simulated
provider responses, including the real Tk window under WSL and native Windows.
The packaged EXE also passed its offline startup check. Real account login,
download, manual preparation and individual Garmin upload were tested earlier;
the complete batch workflow still needs a user-run account test.

## Desktop

1. Connect both accounts. Windows can remember sessions; WSL/Linux uses memory only.
2. Open **Sync rides**. The default scope is the past 7 days, at most 10 rides.
   Keep the original device or choose the experimental Edge 530 profile and your own Unit ID.
   Select the **Timezone** where you trained (for example `Europe/Berlin`).
   Valid GUI settings are saved automatically, separately from account tokens,
   even when closing without running a preview. The chosen device is restored on restart.
3. **Preview sync** downloads and prepares local files but does not upload activities.
4. Review statuses and check rides in the SYNC column. Only the newest ready ride
   is selected initially; **Select ready** selects or clears the full ready set.
5. **Sync selected** asks for confirmation showing the selected rides, profile and
   any time corrections.
   Confirmed imports are read back from Garmin before success is recorded.

A preview is valid for 15 minutes after preparation. Changing settings, account
sign-ins or the underlying prepared bytes requires a new preview. A consumed
preview cannot be submitted again. The GUI remains responsive during operations.

The scope is bounded to 1–365 days and 1–20 rides per preview. Discovery reads at
most 100 MyWhoosh entries / five pages. Garmin matching checks at most the latest
100 activities using UTC start time and comparable duration. It cannot rule out
older duplicates or prove identical FIT contents. Incomplete matches block uploads.

## CLI

Run from the project directory with both accounts entered in your own terminal:

```bash
.venv/bin/mywhoosh-sync sync --interactive --preview-only
.venv/bin/mywhoosh-sync sync --interactive
```

The second command previews and then asks for `SYNC <displayed-plan-prefix>` before
uploading the newest ready ride. Repeated `--activity-id ID` options explicitly
choose other ready rides from the preview. There is no force or unattended yes flag.

Options include `--days`, `--limit`, `--profile original|edge530`, `--config FILE`,
`--device-unit-id` (concealed prompt), `--timezone Europe/Berlin`,
`--no-adjust-times` (preserve original timestamps), and `--data-dir`. Without an explicit profile,
CLI device settings come from `config.local.toml`. `--profile original` bypasses
that device config. Interactive sync never loads or saves account credentials.

`--json` emits a preview object and, after a confirmed upload, a separate result
object on the next line. Prompts and progress go to stderr. Output includes private
activity metadata and hashes, but omits Unit ID, credentials and signed URLs.

## Automatic time adjustment

**Adjust times automatically** starts checked whenever the desktop app opens,
even if it was previously disabled. You can uncheck it for the current run.
Select the timezone where you trained once. For the supported faulty
MyWhoosh exports, the tool interprets recorded clocks as local time and converts
file creation, measurement, event, lap, session and Activity timestamps to UTC.
It also repairs the Unix/FIT epoch mix-up in the local finish field. The finish
anchor is session start plus elapsed duration, checked against measurement/stop
metadata. Summer/winter offsets come from the workout date, never virtual GPS.

**Sync selected** shows each selected ride's old/new UTC start and finish and
local finish before asking for upload confirmation. The TIME column summarizes
**Corrected**, **Unchanged**, **Choose timezone**, **Time unavailable**, or
**Original times**. Select a row to see details; there is no separate time-review
step. The upload result retains the correction feedback.

Uncheck the option to keep every original timestamp, including when using the
Edge 530 device profile. Previous manual time approvals are retained as historical
state but do not override the checkbox. Changing this option invalidates the
preview. Every new copy is derived from the immutable original, so corrections
never accumulate. Already correct recordings and unsupported non-MyWhoosh files
remain unchanged. Missing required timezone or inconsistent, repeated/nonexistent
DST clocks block automatic adjustment instead of choosing unsupported times.

Sensor values, durations, developer payloads and unknown fields are preserved.
Compressed timestamps are expanded where needed. Garmin matching, read-back and
the ride table use the corrected UTC start. Duplicate checks also compare original
clocks and historical original/UTC-only/device variants. Already uploaded or
uncertain rides remain protected when changing this option or timezone.

## Local data and guards

Source runs use `private/sync/` under the current working directory. The frozen
EXE uses `data/` next to the actual executable, independent of its working directory.
Both locations are ignored by this repository's Git rules.

| File | Purpose |
| --- | --- |
| `settings.json` | Date scope, ride limit, device profile, optional personal Unit ID and timezone |
| `sync.sqlite3` | Per-account activity state, time feedback/approvals, file hashes, upload intents and Garmin IDs |
| `*.original.fit` | Immutable downloaded original, named using the account/source ID hash |
| `*.<SHA-256>.fit` | Separate copy when time metadata or device identity is changed |
| `.sync.lock` | OS-managed process lock; released automatically on process exit |

The GUI holds the data-directory lock through shutdown. CLI sync shares that lock;
a second app using the same folder is rejected. Different folders/machines are
separate lock domains. Do not share a live database through a cloud/network folder.

Source IDs are scoped by both configured accounts using fingerprints of provider
and normalized sign-in name. These fingerprints are metadata, not anonymization.
Old session snapshots remain usable for activity reads, but require one fresh login
before syncing so the account scope can be established. Token refresh preserves it.

POSIX storage requires owner-only directories/files (`0700` / `0600`) and rejects
links and unsafe existing files. The sync workspace has a Windows path using
protected owner/SYSTEM ACLs and an OS byte-range lock, verified on native Windows.
Legacy manual FIT commands retain their POSIX contract.
Files are not additionally encrypted. Account tokens use the separate session store.

## Uncertain results and recovery

Before sending, the service commits `upload_pending` in SQLite and exclusively
creates durable hash records for both original and transmitted files. Source runs
use the existing `private/garmin-uploads/` journal so manual CLI uploads and sync
cannot silently bypass each other's file records. Frozen runs use `data/garmin-uploads/`.
Journal protection is conservative across accounts sharing that journal directory.

The batch stops on an unconfirmed result. A timeout, crash or failed final local
save may still mean Garmin imported the ride. Pending/uncertain states stay blocked
on restart; changing the device profile does not bypass a recorded source upload.
Original and prepared file hashes are checked again before sending exact bytes.

**Do not delete/edit the database or upload records to retry an unclear upload.**
Check the ride directly in Garmin Connect. The desktop no longer asks users to
copy activity IDs into a verification dialog. Uncertain upload records stay blocked;
removing their local records is not a recovery procedure. Confirmed uploads that
you deliberately deleted can use **Retry deleted**, described below.

**Stop** prevents further rides from being sent; an in-flight request finishes and
its outcome is recorded. Closing during work waits for this before releasing the
process lock. Unexpected termination leaves the pre-send intent for manual review.
Confirmed source uploads remain skipped even if local FITs are later removed.

### Re-upload after a manual deletion

For a confirmed app upload with a known Garmin activity ID, run **Preview sync**,
highlight the uploaded ride and choose **Retry deleted**. Confirm that you deleted
it in Garmin Connect. The app requires an authenticated 404 for the exact previous
ID and no matching activity among the latest 100. Authentication failures, timeouts
and merely disappearing from that list do not confirm deletion.

This prepares a fresh preview without uploading. Review the device profile and
choose **Sync selected** to confirm the new upload. Deletion is checked again
before sending. The one-use retry authorization is bound to the source hash and
Garmin account and consumed durably before POST. Earlier attempts and journal
records are preserved; new attempts receive separate records. Uncertain uploads
remain blocked. Schema versions 1–4 migrate automatically to version 5 without
resetting previous state.

No provider data is relayed through a project server, and there is no telemetry.
Next: user-run validation of real sync and saved sessions in the Windows app.
