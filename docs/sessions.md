# Account sessions

The account service supports the CLI and the [account window](desktop.md).
It supports signing in, reading activities, reusing saved sessions and signing out
locally. The [sync service](sync.md) uses it for bounded downloads and explicitly
confirmed uploads. Existing interactive
commands retain their memory-only behavior.

## Commands

With the CLI installed, these new commands are available:

```text
mywhoosh-sync account-login mywhoosh --remember
mywhoosh-sync account-login garmin --remember
mywhoosh-sync account-activities mywhoosh
mywhoosh-sync account-activities garmin
mywhoosh-sync account-logout garmin
```

`account-login` asks for credentials in an interactive terminal, signs in and lists
recent activities. `--remember` explicitly enables Windows Credential Manager.
Without it, the new command does not access saved sessions and its session ends
with the process, even if a previous saved session exists. The legacy MyWhoosh
`login` command continues to use its separate POSIX session file.

`account-activities` loads a saved Windows session without prompting. A missing or
expired session reports that sign-in is required. `account-logout` removes the
selected provider's stored session locally; it does not revoke credentials at the
provider or affect the legacy MyWhoosh POSIX session file.

Login/list commands accept `--limit 1..100`; all three commands accept `--json`.
Authentication prompts remain on stderr. `--state-dir` does not select the Windows
credential store. The GUI uses the same service with one background operation at a time.

## Storage

- One active account per provider, tied to the current Windows user and machine.
- Only session tokens and required provider identifiers are stored. No passwords,
  MFA codes, workout data or account email addresses are part of the session record.
- There is no plaintext fallback. Requesting persistent storage on Linux fails
  before credentials are requested; ordinary memory-only login remains available.
- Tokens can exceed Windows' 2560-byte credential-blob limit. Records are split
  into immutable chunks, then made current through a manifest written last. A failed
  pre-publication write retains the previous session. Hash/schema validation rejects
  incomplete or inconsistent records rather than silently discarding them.
- Local sign-out removes all entries in the selected provider's namespace,
  including unpublished chunks from an interrupted save. Other providers and apps
  are not touched. Desktop operations are serialized; a data-directory process lock
  is held until current work finishes. Separate data folders are separate lock domains.
- Windows protection does not isolate credentials from other programs with access
  under the same account. Copying portable configuration to another PC does not
  transfer the Windows credentials.

The binding uses Windows' [generic credential records](https://learn.microsoft.com/en-us/windows/win32/api/wincred/ns-wincred-credentialw)
with local-machine persistence scoped to the current user. It uses the official
[read](https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credreadw),
[write](https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-credwritew)
and [delete](https://learn.microsoft.com/en-us/windows/win32/api/wincred/nf-wincred-creddeletew)
APIs, with no additional Python dependency.

## Renewal and failures

Garmin's pinned connector provides access and refresh tokens. The service refreshes
when the access token is near expiry, or once after an activity GET returns 401.
New refresh credentials are saved before another activity read. If saving fails,
the read stops and the error is reported; newly rotated credentials stay in the
service's memory while that instance exists. A later explicit read retries the
pending save before any network request. A failed attempt to sign into another
account does not silently reactivate the old saved account within that instance.

Refresh is one bounded request through the existing restricted auth transport.
There is no automatic password login, alternate auth flow or upload replay.
Refresh rejection (`invalid_grant` / 401) asks for sign-in. A 403, rate limit,
network failure or unexpected response does not trigger a credential retry or
delete the stored session.

MyWhoosh reuses the existing access token. No refresh endpoint or fixed lifetime
has been established; a 401 asks for sign-in. Token lifetime and real saved-session
reuse still need user-run verification. Neither provider has a guaranteed fixed
number of days signed in.

## Validation and remaining work

Tests use synthetic tokens, fake credential APIs and mocked provider responses.
They cover persistence across service instances, token rotation, bounded retries,
error separation, storage interruptions, local sign-out and the native API binding.
A native Windows test uses a random isolated namespace and synthetic data; it is
skipped on Linux. No real-account requests are made by tests.

The native Windows 11 test run passed the Credential Manager round trip and sync
workspace ACL/locking checks. Real-account session renewal still needs manual
verification. Legacy manual FIT / journal commands retain POSIX assumptions;
the desktop sync uses the separate Windows-compatible workspace.

Session snapshots now include a local fingerprint of the provider and normalized
sign-in name to separate sync histories. Old snapshots remain readable, but require
one fresh sign-in before syncing. Fingerprints are account metadata, not anonymized
identities. They survive token rotation; no plaintext email is added to the snapshot.

Next steps: user-run session reuse and sync checks in the packaged Windows app.
The individual account commands remain read-only; use the explicit sync workflow
to transfer workouts.
