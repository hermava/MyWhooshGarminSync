# CLI guide

The current CLI targets Linux/POSIX. Run commands from the repository directory
after completing the setup in the [README](../README.md). Examples use synthetic
placeholders: replace `ACTIVITY_ID` with an ID from your own activity list.
CLI prompts and diagnostic messages are currently in German.

For the complete download → preparation → confirmed upload workflow, use the
[sync command](sync.md). `mywhoosh-sync sync --interactive --preview-only` creates
a local preview without uploading activities; omit `--preview-only` to confirm
the newest ready ride or specify explicit `--activity-id` selections.

For the separate account service and optional Windows Credential Manager storage,
see [account sessions](sessions.md). The commands below retain their original
authentication and upload behavior.

## Read and download MyWhoosh rides

```bash
.venv/bin/mywhoosh-sync activities --interactive --limit 10
.venv/bin/mywhoosh-sync download ACTIVITY_ID --interactive
.venv/bin/mywhoosh-sync validate private/fit/ACTIVITY_ID.original.fit
```

`--interactive` prompts for credentials on each call without reading or saving
sessions. Enter credentials in your own terminal, not in shell arguments or chat.
Concealed input prevents normal display; it does not isolate process memory from
other programs with sufficient access.

Downloads preserve the original under `private/fit/` and then check FIT structure,
CRC and times. An invalid original is kept for investigation. Existing files are
not overwritten. `validate` is local and read-only. Files are limited to 16 MiB;
activity lookup is bounded to five pages and 100 entries.

For an optional saved MyWhoosh session:

```bash
.venv/bin/mywhoosh-sync login
.venv/bin/mywhoosh-sync activities --limit 10
```

Only the session is saved, not the password. The default location is
`~/.local/state/mywhoosh-garmin-sync/session.json` (or the equivalent under an
absolute `XDG_STATE_HOME`). Global `--state-dir PATH` must precede the command.
Storage must be outside Git worktrees, owned by the current user, with directory
mode `0700` and file mode `0600`; symlinks and multiple hard links are rejected.
Local storage is not additional encryption. MyWhoosh token lifetime, reuse and
renewal have not been verified through a saved-session account test.

`check` checks local session readability without network access. A readable file
does not prove the provider still accepts its token. No automatic MyWhoosh refresh
or re-login is implemented. The CLI does not load `.env` files.

## Inspect and repair MyWhoosh time metadata

`validate` warns when the Activity local clock differs from UTC by more than
14 hours. This is a warning, not a CRC failure or an automatic correction.

Preview the known MyWhoosh Unix/FIT epoch error with an explicit IANA timezone:

```bash
.venv/bin/mywhoosh-sync repair-fit-time private/fit/ACTIVITY_ID.original.fit --timezone Europe/Berlin
```

Only an explicit output path writes the proposed correction:

```bash
.venv/bin/mywhoosh-sync repair-fit-time private/fit/ACTIVITY_ID.original.fit --timezone Europe/Berlin --output private/fit/ACTIVITY_ID.time-fixed.fit
```

The repair accepts MyWhoosh originals with one session and one Activity message.
It requires agreement between the recorded stop, last sample and start plus elapsed
duration, and the specific epoch error must match the final sample/stop. It then
sets `activity.timestamp` to the confirmed stop and `activity.local_timestamp` to
the corresponding local wall clock. The workout date determines daylight saving
time; virtual route coordinates and the computer's current timezone are not used.

Correct files and recordings from other manufacturers remain unchanged. Uncertain
inputs report `needs_review`, return exit code 1 and never produce an output file.
Measurements, session fields, original bytes and existing upload records remain
unchanged. New copies have recalculated CRCs and never replace existing files.
`--json` includes status, proposed changes, hashes and whether a copy was written.

Apply time repair **before** the optional device profile, using the time-fixed copy
as input to `prepare-fit`. The shared GUI/CLI sync now runs this check automatically
before its device profile; choose a timezone in the GUI or pass `sync --timezone
Europe/Berlin`. The standalone local file commands retain their Linux/POSIX storage
contract. No account or network access
is required; a file correction does not update an activity already in Garmin.

## Optional experimental Edge 530 profile

To configure your own device:

```bash
cp config.example.toml config.local.toml
```

Edit the local file, replacing the example Unit ID with your own:

```toml
[device]
profile = "edge530"
unit_id = 123456789
```

Then prepare a separate copy:

```bash
.venv/bin/mywhoosh-sync prepare-fit private/fit/ACTIVITY_ID.original.fit
```

The output is `private/fit/ACTIVITY_ID.original.edge530.fit`. This command does
not upload anything or modify an activity already in Garmin. It changes recorder
identity metadata, preserves workout measurements, and recalculates CRC. It does
not calculate Training Effect itself. Only the `edge530` profile is supported.

`config.local.toml` is ignored by Git and is only read by `prepare-fit`. It holds
device settings, not account credentials. The Unit ID is a device identifier,
not an account password; it becomes part of the derived FIT even though reports
hide it. Do not share the personal file or derived FIT as a neutral example.

Useful options:

- `--dry-run`: inspect proposed changes without creating files.
- `--config FILE`: use another device TOML file.
- `--profile edge530 --device-unit-id`: select the profile and prompt for a Unit ID;
  manual input overrides configuration without changing it.
- `--output FILE.fit`: choose a new destination; existing files are rejected.

## Read, compare and upload to Garmin

Read-only commands:

```bash
.venv/bin/mywhoosh-sync garmin-activities --interactive --limit 10
.venv/bin/mywhoosh-sync garmin-match private/fit/ACTIVITY_ID.original.edge530.fit --interactive
```

Each call asks for Garmin credentials and MFA when required. Credentials and tokens
remain in memory; saved Garmin tokens are neither loaded nor written. Currently
only `garmin.com` accounts are supported.

Matching compares UTC start time (within five seconds) and a comparable duration
(within three seconds), using at most 100 recent Garmin activities. It requires a
valid FIT with one session. No match in this window is not proof of no duplicate.

To send exactly one selected file:

```bash
.venv/bin/mywhoosh-sync garmin-upload private/fit/ACTIVITY_ID.original.edge530.fit --interactive
```

Use the original file path instead if you do not want the experimental profile.
Upload never applies that profile automatically.

The command validates the selected bytes, checks recent activities, and displays
file, hash and timing details. Sending requires typing the displayed
`UPLOAD <hash-prefix>` confirmation. Another check follows before sending; there
is no force option or automatic upload retry. Garmin visibility and connected-service
settings still apply.

A journal is persisted before sending under `private/garmin-uploads/<SHA-256>.json`.
Success requires an import ID and a separate activity read-back matching ID, start
and duration. An uncertain result or interrupted attempt may still mean Garmin
accepted the activity. Check Garmin manually and keep the journal; do not delete
it to retry. Existing markers block the same bytes, including after a crash.
This protection is local to the journal and file hash, not a global sync database.

## Output and troubleshooting

Use `mywhoosh-sync COMMAND --help` for supported options. Activity, FIT and Garmin
commands support `--json`; prompts remain on stderr. Output includes private
activity metadata, although passwords, tokens and signed download URLs are hidden.

If a file or directory fails permission checks, inspect ownership and permissions
before retrying. Do not solve a file-location issue by moving session tokens into
the repository. Preserve originals and upload journals while investigating failures.
