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

**Adjust times automatically** starts checked whenever the desktop app opens.
Select your timezone once. Automatic repair changes only the Activity UTC finish
and local finish fields when the recording supports a consistent correction,
including the demonstrated Unix/FIT epoch mix-up. The start, measurement timestamps,
file-creation times, lap/session timestamps and sensor values are preserved.

This restores the previously supported metadata-only behavior. Earlier builds
selected a full local-to-UTC timeline conversion automatically; selecting a timezone
alone does not establish whether the source clocks were local or UTC. That automatic
selection has been removed. Explicit low-level conversion remains implemented, but
is not selected by desktop sync. Files whose measurement clocks are mislabeled may
therefore still need separate investigation; automatic finish repair does not fix
or claim to fix their recording start.

**Sync selected** shows old/new Activity UTC and local finish fields and states
that recording start/measurement timestamps are unchanged. Uncheck the option to
preserve every original time field. The TIME column reports the repair outcome.
Missing timezone or contradictory finish evidence blocks automatic repair. UTC
instants determine summer/winter local offsets using the workout date.

Every prepared copy derives from the immutable original; corrections never
accumulate. Original, metadata-only, device-profile and historical full-timeline
variants remain covered by duplicate guards. Recent-activity matching also checks
previously shifted timelines, including the final preflight before POST, so the
compatibility correction cannot silently resend an old import under a new clock.
Already uploaded or uncertain rides stay protected when changing settings.

## Local data and guards

Source runs use `private/sync/` under the current working directory. The frozen
EXE uses `data/` next to the actual executable, independent of its working directory.
Both locations are ignored by this repository's Git rules.

| File | Purpose |
| --- | --- |
| `settings.json` | Date scope, ride limit, device profile, optional personal Unit ID and timezone |
| `sync.sqlite3` | Per-account activity state, time feedback/approvals, file hashes, upload intents and Garmin IDs |
| `retry-review-*.json` | Immutable audit of explicitly reviewed attempts, receipt hashes and previous FITs |
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
copy activity IDs into a verification dialog. Uncertain uploads stay blocked until an explicit manual review; removing their
local records is not a recovery procedure. Use **Retry upload** as described below.

**Stop** prevents further rides from being sent; an in-flight request finishes and
its outcome is recorded. Closing during work waits for this before releasing the
process lock. Unexpected termination leaves the pre-send intent for manual review.
Confirmed source uploads remain skipped even if local FITs are later removed.

### Retry an unconfirmed upload with current settings

1. Check Garmin Connect **in your browser**, including the workout's actual date,
   and confirm that the activity is missing. A timeout or HTTP 520 may still have
   reached Garmin; absence from a bounded recent list is not proof of non-import.
2. In **Sync rides**, change the device profile, Unit ID, timezone or time adjustment
   if needed, then choose **Preview sync**. The old attempt remains blocked.
3. Highlight that ride and choose **Retry upload**. Explicitly confirm the browser
   check. The app checks any known Garmin IDs and matches the original and all
   previously attempted FIT timelines against up to 100 recent Garmin activities.
   A match, incomplete response, present ID or failed read blocks reopening.
4. Review the fresh preview, prepared from the immutable original with the current
   settings. Only the reviewed ride is selected. **Sync selected** requires a
   separate confirmation before one new upload attempt.

The absence review expires after 15 minutes. The one-use authorization is bound
to the original hash and both accounts' scope. Its audit file records exact prior
attempts and journal hashes; changing these records blocks the retry. Previous
files and journal entries are kept, and a new attempt gets separate records.
Historical corrected timelines are checked again immediately before POST, even
when the new configuration changes the start time. Another unconfirmed outcome
stops the batch and requires another explicit review; there is no automatic retry.

Only recorded app attempts can be reviewed this way. Unrelated legacy records,
damaged or missing FITs, and inconsistent receipts remain protected. A reviewed
cached ride can be prepared even after it ages out of the source date window.

`recorded_at_utc` in a journal is the UTC time the local upload record was written,
not the workout's date. Yesterday's ride can have today's upload log.

### Re-upload after a manual deletion

For a confirmed app upload with a known Garmin ID, preview, highlight the ride
and choose **Retry upload**. Confirm that you deleted it. The app requires an
authenticated 404 for the exact previous ID and no matching activity among the
latest 100. Authentication failures, timeouts or simply disappearing from the
list do not confirm deletion. A fresh preview and **Sync selected** confirmation
are still required. Existing history is preserved. Schema versions 1–4 still
migrate automatically to version 5 without resetting previous state.

No provider data is relayed through a project server, and there is no telemetry.
Next: user-run validation of real sync and saved sessions in the Windows app.
