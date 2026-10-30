"""Local FIT tools, interactive account reads and an explicitly confirmed upload."""

import argparse
import getpass
import json
import re
import sys
import warnings
from dataclasses import asdict
from pathlib import Path

from .auth import Session, TokenStore, default_state_dir
from .errors import FitError, SessionError, SyncError
from .fit_processing import FitReport, prepare_fit_target, prepare_original_path, read_fit, save_original, validate_fit
from .mywhoosh import ActivityList, MyWhooshClient


def _limit(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("Erwartet wird eine Zahl von 1 bis 100.") from None
    if not 1 <= number <= 100:
        raise argparse.ArgumentTypeError("Limit muss zwischen 1 und 100 liegen.")
    return number


def _safe(value: str) -> str:
    # Escape all control characters, including newlines, ESC and bidi controls.
    return json.dumps(value, ensure_ascii=True)


def _require_terminal() -> None:
    if not sys.stdin.isatty():
        raise SessionError("Anmeldung benötigt ein interaktives Terminal; keine Zugangsdaten per Argument oder Pipe.")


def _prompt_login(client: MyWhooshClient) -> Session:
    # Keep prompts out of stdout so --json remains usable.
    print("MyWhoosh E-Mail: ", end="", file=sys.stderr, flush=True)
    email = input("").strip()
    password = _read_secret("MyWhoosh Passwort: ")
    try:
        return client.login(email, password)
    finally:
        password = ""


def _read_secret(prompt: str) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise SessionError("Verdeckte Passworteingabe ist in diesem Terminal nicht verfügbar.") from None


def _account_command(args: argparse.Namespace) -> int:
    from .accounts import AccountSessions

    signing_in = args.command == "account-login"
    if signing_in:
        _require_terminal()
    store = None
    if not signing_in or args.remember:
        from .windows_credentials import WindowsSessionStore
        store = WindowsSessionStore()
    accounts = AccountSessions(store=store, mywhoosh_factory=MyWhooshClient)
    if args.command == "account-logout":
        accounts.sign_out(args.provider)
        if args.json:
            print(json.dumps({"provider": args.provider, "signed_out_locally": True}))
        else:
            print("Saved session removed locally. This does not revoke it at the provider.")
        return 0
    if signing_in:
        print(f"{args.provider} email: ", end="", file=sys.stderr, flush=True)
        email = input("").strip()
        password = _read_secret(f"{args.provider} password: ")

        def prompt_mfa():
            code = _read_secret("Garmin MFA code: ").strip()
            if not re.fullmatch(r"[0-9]{6,8}", code):
                raise SessionError("Garmin MFA code must contain six to eight digits.")
            return code

        try:
            if args.provider == "mywhoosh":
                result = accounts.sign_in_mywhoosh(email, password, remember=args.remember, limit=args.limit)
            else:
                result = accounts.sign_in_garmin(email, password, prompt_mfa,
                                                 remember=args.remember, limit=args.limit)
        finally:
            password = ""
    elif args.provider == "mywhoosh":
        result = accounts.mywhoosh_activities(limit=args.limit)
    else:
        result = accounts.garmin_activities(limit=args.limit)
    if args.provider == "mywhoosh":
        _print_activities(result, json_output=args.json)
    else:
        rows = [asdict(activity) for activity in result]
        if args.json:
            print(json.dumps({"activities": rows, "window_limit": args.limit}, ensure_ascii=True))
        else:
            for row in rows:
                print(" | ".join(_safe(str(value)) if value is not None else "unknown" for value in row.values()))
            print(f"{len(rows)} Garmin activities read.")
    return 0


def _sync_command(args):
    from .accounts import AccountSessions
    from .config import DeviceConfig, load_device_config
    from .sync import SyncEngine, SyncSettings
    from .workspace import Workspace

    _require_terminal()
    config = DeviceConfig() if args.profile == "original" else load_device_config(args.config)
    profile = (None if args.profile == "original" else args.profile) if args.profile else config.profile
    unit_id = config.unit_id
    if args.device_unit_id:
        try:
            unit_id = int(_read_secret("Device Unit ID: "))
        except ValueError:
            raise SessionError("Enter a numeric device Unit ID.") from None
    settings = SyncSettings(args.days, args.limit, profile, unit_id, args.timezone, not args.no_adjust_times)
    workspace = Workspace(args.data_dir)
    with workspace.locked():
        accounts = AccountSessions()  # Explicit interactive mode: no credential store.
        for provider in ("mywhoosh", "garmin"):
            print(f"{provider} email: ", end="", file=sys.stderr, flush=True)
            email = input("").strip()
            password = _read_secret(f"{provider} password: ")

            def mfa():
                value = _read_secret("Garmin MFA code: ").strip()
                if not re.fullmatch(r"[0-9]{6,8}", value):
                    raise SessionError("Garmin MFA code must contain six to eight digits.")
                return value

            try:
                if provider == "mywhoosh":
                    accounts.sign_in_mywhoosh(email, password)
                else:
                    accounts.sign_in_garmin(email, password, mfa)
            finally:
                password = ""
        engine = SyncEngine(accounts, workspace, legacy_directory=workspace.root.parent / "garmin-uploads")
        plan = engine.preview(settings, progress=lambda identity, stage: print(stage, file=sys.stderr))
        summary = {"plan_id": plan.identity, "profile": settings.profile, "truncated": plan.truncated,
                   "review_count": plan.review_count,
                   "items": [asdict(item) for item in plan.items]}
        if args.json:
            print(json.dumps(summary, ensure_ascii=True))
        else:
            for item in plan.items:
                print(f"{_safe(item.source_id)} | {_safe(item.title)} | {item.status}")
                if item.time_detail:
                    print(f"  {item.time_detail}")
        ready = [item.source_id for item in plan.items if item.status == "ready"]
        if args.preview_only or not ready:
            return 0
        selected = args.activity_id or ready[:1]
        if len(selected) != len(set(selected)) or not set(selected) <= set(ready):
            raise SessionError("Select ready activity IDs from the current preview.")
        confirmation = "SYNC " + plan.identity[:8]
        print(f"Upload {len(selected)} selected ride(s) using profile {settings.profile or 'original'}? "
              f"Older duplicates are not ruled out. Type {confirmation}: ", end="", file=sys.stderr, flush=True)
        if input("").strip() != confirmation:
            return 130
        result = engine.execute(plan.identity, selected, progress=lambda identity, stage: print(stage, file=sys.stderr))
        if args.json:
            print(json.dumps(asdict(result), ensure_ascii=True))
        else:
            for item in result.items:
                print(f"{_safe(item.source_id)} | {item.status}" + (f" | Garmin {item.garmin_id}" if item.garmin_id else ""))
        return 1 if result.stopped else 0


def _garmin_command(args: argparse.Namespace) -> int:
    _require_terminal()
    # Import lazily so base commands do not require Garmin/native dependencies.
    from .garmin import GarminReader, GarminUploader, match_fit

    report = None
    uploading = args.command == "garmin-upload"
    if args.command in ("garmin-match", "garmin-upload"):
        data = read_fit(args.file)
        report = validate_fit(data)
        if not report.valid or len(report.session_times) != 1:
            raise FitError("Garmin-Abgleich benötigt eine bestandene FIT-Prüfung mit genau einer Session.")
        # Reject missing comparison metadata before asking for credentials.
        match_fit(report, (), window_limit=args.limit)
    if uploading:
        from .upload import UploadJournal

        journal = UploadJournal(report.sha256)
        journal.check_available()
    reader_type = GarminUploader if uploading else GarminReader
    with reader_type() as reader:
        print("Garmin E-Mail: ", end="", file=sys.stderr, flush=True)
        email = input("").strip()
        password = _read_secret("Garmin Passwort: ")

        def prompt_mfa():
            code = _read_secret("Garmin MFA-Code: ").strip()
            if not re.fullmatch(r"[0-9]{6,8}", code):
                raise SessionError("Garmin-MFA-Code muss aus sechs bis acht Ziffern bestehen.")
            return code

        try:
            reader.login(email, password, prompt_mfa)
        finally:
            password = ""
        activities = reader.activities(limit=args.limit)
        if uploading:
            return _upload_selected(args, reader, data, report, activities, journal)
    if report is None:
        rows = [asdict(activity) for activity in activities]
        if args.json:
            print(json.dumps({"activities": rows, "window_limit": args.limit}, ensure_ascii=True))
        else:
            for row in rows:
                print(" | ".join(_safe(str(value)) if value is not None else "unbekannt" for value in row.values()))
            print(f"{len(rows)} Garmin-Aktivitäten gelesen (maximal {args.limit} jüngste Einträge).")
        return 0
    result = match_fit(report, activities, window_limit=args.limit)
    if args.json:
        print(json.dumps(asdict(result), ensure_ascii=True))
    else:
        labels = {
            "candidate_found": "Ein mögliches Gegenstück gefunden.",
            "ambiguous": "Mehrere mögliche Gegenstücke oder unvollständige Daten; manuell prüfen.",
            "inconclusive": "Abgleich wegen unvollständiger Garmin-Daten nicht entscheidbar.",
            "no_candidate_in_window": "Kein passender Kandidat im geprüften Ausschnitt gefunden.",
        }
        print(labels[result.status])
        print(f"Geprüft: {result.checked_activities} der maximal {args.limit} jüngsten Garmin-Aktivitäten.")
        for candidate in result.candidates:
            print(f"Kandidat: {_safe(candidate.id)} | {_safe(candidate.title)}")
        print("Dies ist ein Abgleich anhand von Startzeit und Dauer, kein sicherer Duplikatnachweis.")
    return 0


def _upload_selected(args, reader, data, report, activities, journal) -> int:
    from .garmin import match_fit

    check = match_fit(report, activities, window_limit=100)
    if check.status != "no_candidate_in_window":
        raise FitError("Upload gesperrt: Garmin-Abgleich enthält Kandidaten oder unvollständige Daten.")
    timing = report.session_times[0]
    print(f"Zum Upload ausgewählt: {_safe(str(args.file))}", file=sys.stderr)
    print(f"SHA-256: {report.sha256} | Größe: {report.size_bytes} Bytes", file=sys.stderr)
    print(f"Start (UTC): {timing.start_utc} | Gesamtdauer: {timing.elapsed_seconds} s | Timer: {timing.timer_seconds} s",
          file=sys.stderr)
    for warning in report.warnings:
        print(f"FIT-Hinweis: {warning}", file=sys.stderr)
    print(f"Kein Kandidat unter {check.checked_activities} der maximal 100 jüngsten Garmin-Aktivitäten. "
          "Ältere Duplikate sind damit nicht ausgeschlossen.", file=sys.stderr)
    confirmation = f"UPLOAD {report.sha256[:12]}"
    print(f"Genau diese Datei wird an Garmin gesendet. Zur Freigabe {confirmation} eingeben: ",
          end="", file=sys.stderr, flush=True)
    if input("").strip() != confirmation:
        print("Upload nicht bestätigt; nichts gesendet.", file=sys.stderr)
        return 130
    try:
        # The uploader checks Garmin again after the user prompt, then creates
        # the durable journal marker immediately before its only POST attempt.
        result = reader.upload_once(data, before_send=journal.begin)
        journal.finish(result)
    except BaseException:
        if journal.started:
            print("Ein Upload-Vermerk wurde angelegt. Bei unklarem Ausgang nicht erneut senden; "
                  "Garmin und den lokalen Vermerk manuell prüfen.", file=sys.stderr)
        raise
    if args.json:
        print(json.dumps({**asdict(result), "sha256": report.sha256,
                          "receipt_path": str(journal.path)}, ensure_ascii=True))
    else:
        if result.status == "uploaded":
            print(f"Garmin-Upload bestätigt: Aktivitäts-ID {result.activity_id}.")
            print("Aktivität erneut gelesen; Startzeit und Dauer passen zur FIT-Datei.")
        else:
            print("Upload-Ergebnis unklar oder nicht vollständig bestätigt. Nicht erneut senden; Garmin manuell prüfen.")
            print(f"Diagnose: {_upload_diagnosis(result.reason)}")
            if result.activity_id:
                print(f"Gemeldete Garmin-Aktivitäts-ID: {result.activity_id}")
        print(f"Lokaler Upload-Vermerk: {_safe(str(journal.path))}")
    return 0 if result.status == "uploaded" else 1


def _upload_diagnosis(reason: str) -> str:
    stages = {"upload": "Upload-Anfrage", "verification": "Gegenprüfung"}
    match = re.fullmatch(r"(upload|verification)_http_([1-5][0-9]{2})", reason)
    if match:
        return f"{stages[match[1]]}: Garmin hat HTTP {match[2]} zurückgegeben."
    messages = {
        "timeout": "Zeitüberschreitung; der Verarbeitungsstand bei Garmin ist unbekannt.",
        "transport_error": "Verbindungsfehler; der Verarbeitungsstand bei Garmin ist unbekannt.",
        "response_compressed": "Komprimierte Antwort wurde nicht ausgewertet.",
        "response_not_json": "Antwort hatte keinen unterstützten JSON-Inhaltstyp.",
        "response_too_large": "Antwort überschritt das Größenlimit.",
        "response_time_limit": "Antwort überschritt das Zeitbudget.",
        "invalid_json": "Antwort enthielt kein gültiges JSON.",
    }
    for stage, label in stages.items():
        prefix = stage + "_"
        if reason.startswith(prefix) and reason[len(prefix):] in messages:
            return f"{label}: {messages[reason[len(prefix):]]}"
    return {
        "upload_not_confirmed": "Upload-Anfrage nicht bestätigt; ältere Vermerke enthalten keine genauere Fehlerart.",
        "unknown_import_result": "Importantwort hatte ein unbekanntes Format oder meldete Fehler.",
        "missing_activity_id": "Importantwort enthielt keine gültige Garmin-Aktivitäts-ID.",
        "verification_failed": "Zurückgegebene Aktivität konnte nicht vollständig geprüft werden.",
        "verification_mismatch": "Zurückgegebene Aktivität stimmt bei ID, Startzeit oder Dauer nicht überein.",
    }.get(reason, "Keine unterstützte Diagnose verfügbar.")


def _print_activities(result: ActivityList, *, json_output: bool) -> None:
    rows = [
        {"id": a.id, "title": a.title,
         "start_time_utc": a.start_time.isoformat() if a.start_time else None,
         "duration": a.duration}
        for a in result.activities
    ]
    if json_output:
        print(json.dumps({"activities": rows, "truncated": result.truncated}, ensure_ascii=True))
    else:
        for row in rows:
            print(" | ".join(_safe(str(value)) if value is not None else "unbekannt"
                             for value in row.values()))
        print(f"{len(rows)} Aktivitäten gelesen.")
        if result.truncated:
            print("Auswahl durch Limit oder Seitenbegrenzung gekürzt.")


def _print_fit_report(report: FitReport, path: Path, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps({"path": str(path), **asdict(report)}, ensure_ascii=True))
        return
    print(f"Original: {_safe(str(path))}")
    print("FIT-Prüfung: " + ("bestanden" if report.valid else "fehlgeschlagen; Original unverändert erhalten"))
    print("CRC und Nachrichtenstruktur: " + ("bestanden" if report.integrity_ok else "nicht vollständig bestätigt"))
    print(f"Größe: {report.size_bytes} Bytes | Records: {report.records} | Sessions: {report.sessions}")
    print(f"SHA-256: {report.sha256}")
    if report.first_record_utc and report.last_record_utc:
        print(f"Records (UTC): {report.first_record_utc} bis {report.last_record_utc}")
    for index, timing in enumerate(report.session_times, start=1):
        print(f"Session {index} Start (UTC): {timing.start_utc or 'unbekannt'}")
        print(f"Session {index} timestamp (UTC): {timing.timestamp_utc or 'unbekannt'}")
        print(f"Session {index} Gesamtdauer: {timing.elapsed_seconds} s | Timer: {timing.timer_seconds} s")
        print(f"Session {index} Ende aus Start + Gesamtdauer (UTC): {timing.elapsed_end_utc or 'unbekannt'}")
    if report.last_record_offset_seconds is not None:
        print(f"Letzter Record minus Session-Ende: {report.last_record_offset_seconds:+.3f} s")
    for message in report.issues:
        print(f"Befund: {message}")
    for message in report.warnings:
        print(f"Hinweis: {message}")


def _download(args: argparse.Namespace) -> int:
    if args.interactive:
        _require_terminal()
        session = None
    else:
        session = TokenStore(args.state_dir).load()
    target = prepare_original_path(args.output_dir, args.activity_id)
    with MyWhooshClient() as client:
        if session is None:
            session = _prompt_login(client)
        listing = client.activities(session, limit=100)
        selected = next((a for a in listing.activities if a.id == args.activity_id), None)
        if selected is None:
            if listing.truncated:
                raise FitError("Aktivität wurde innerhalb der begrenzten Aktivitätenliste nicht gefunden.")
            raise FitError("Aktivität wurde in der Aktivitätenliste nicht gefunden.")
        if not selected.activity_file_id:
            raise FitError("Für diese Aktivität ist noch keine FIT-Dateikennung verfügbar.")
        data = client.download_fit(session, selected.activity_file_id)
    save_original(data, target)
    report = validate_fit(data, expected_start=selected.start_time)
    _print_fit_report(report, target, json_output=args.json)
    return 0 if report.valid else 1


def _prepare_fit(args: argparse.Namespace) -> int:
    from .config import load_device_config
    from .fit_identity import prepare_edge530

    config = load_device_config(args.config)
    profile = args.profile or config.profile
    if profile is None:
        raise SessionError("Geräteprofil fehlt. --profile edge530 angeben oder [device].profile in config.local.toml setzen.")
    data = read_fit(args.file)
    target = args.output or args.file.with_name(args.file.stem + ".edge530.fit")
    if target.suffix.lower() != ".fit":
        raise FitError("Zieldatei für das Geräteprofil muss die Endung .fit haben.")
    if args.file.absolute() == target.absolute():
        raise FitError("Original und abgeleitete FIT-Datei benötigen unterschiedliche Pfade.")
    if not args.dry_run:
        target = prepare_fit_target(target)
    unit_id = config.unit_id
    if args.device_unit_id:
        if not sys.stdin.isatty():
            raise SessionError("Verdeckte Unit-ID-Eingabe benötigt ein interaktives Terminal.")
        value = _read_secret("Eigene Edge-530-Unit-ID (nicht die Seriennummer; Eingabe verdeckt): ").strip()
        if not re.fullmatch(r"[0-9]{1,10}", value) or not 1 <= int(value) <= 0xFFFFFFFF:
            raise FitError("Unit-ID muss eine positive 32-Bit-Gerätekennung sein.")
        unit_id = int(value)
        value = ""
    try:
        output, report = prepare_edge530(data, unit_id=unit_id)
    finally:
        unit_id = None
    if not args.dry_run:
        save_original(output, target)
    if args.json:
        print(json.dumps({"source_path": str(args.file), "output_path": str(target),
                          "dry_run": args.dry_run, **asdict(report)}, ensure_ascii=True))
    else:
        print("Geräteprofil: Garmin Edge 530 (experimentell)")
        print(f"Quelle: {_safe(str(args.file))}")
        print(f"Abgeleitete Datei: {_safe(str(target))}" + (" (Dry-Run; nicht geschrieben)" if args.dry_run else ""))
        print(f"Quell-SHA-256: {report.source_sha256}")
        print(f"Ziel-SHA-256: {report.output_sha256}")
        for change in report.changes:
            values = ("gesetzt; Wert ausgeblendet" if change.value_hidden
                      else f"{change.before if change.before is not None else 'fehlend'} -> {change.after}")
            print(f"Änderung: {change.message}[{change.message_index}].{change.field}: {values}")
        print("FIT-Prüfung einschließlich CRC und Zeitangaben: bestanden.")
        for note in report.warnings:
            print(f"Hinweis: {note}")
        print("Kein Upload ausgeführt. Eine bereits importierte Aktivität bleibt unverändert.")
    return 0


def _repair_fit_time(args: argparse.Namespace) -> int:
    from .fit_time import prepare_time_repair

    data = read_fit(args.file)
    output, report = prepare_time_repair(data, timezone_name=args.timezone)
    written = False
    if args.output is not None and report.status == "ready":
        if args.output.suffix.lower() != ".fit":
            raise FitError("Zeitkorrigierte Zieldatei benötigt die Endung .fit.")
        if args.file.absolute() == args.output.absolute():
            raise FitError("Original und zeitkorrigierte Datei benötigen unterschiedliche Pfade.")
        target = prepare_fit_target(args.output)
        save_original(output, target)
        written = True
    if args.json:
        print(json.dumps({"source_path": str(args.file), "output_path": str(args.output) if written else None,
                          "written": written, **asdict(report)}, ensure_ascii=True))
    else:
        print(f"Quelle: {_safe(str(args.file))}")
        print(f"Zeitzone: {_safe(report.timezone)}")
        print(f"Zeitprüfung: {report.status}; {report.reason}")
        for change in report.changes:
            print(f"Änderung: {change.field}: {change.before} -> {change.after}")
        print(f"Quell-SHA-256: {report.source_sha256}")
        print(f"Ziel-SHA-256: {report.output_sha256}")
        if written:
            print(f"Separate Kopie: {_safe(str(target))}")
        else:
            print("Keine Datei geschrieben. Mit --output DATEI.fit eine vorgeschlagene Korrektur speichern.")
        print("Original unverändert. Kein Upload ausgeführt; bestehende Upload-Vermerke bleiben erhalten.")
    return 1 if report.status == "needs_review" else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MyWhoosh-/Garmin-Aktivitäten, FIT-Prüfung und manueller Upload")
    parser.add_argument("--state-dir", type=Path, default=default_state_dir(),
                        help="Privates Sitzungsverzeichnis außerhalb des Repositorys")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="Lokale Voraussetzungen und Sitzungsdatei prüfen; kein Netzwerk")
    commands.add_parser("login", help="Interaktiv anmelden und Sitzung lokal speichern")
    sync = commands.add_parser("sync", help="Preview and explicitly sync selected MyWhoosh rides to Garmin")
    sync.add_argument("--interactive", action="store_true", required=True, help="Prompt for both accounts; no saved credentials")
    sync.add_argument("--days", type=int, default=7)
    sync.add_argument("--limit", type=int, default=10)
    sync.add_argument("--profile", choices=("original", "edge530"))
    sync.add_argument("--config", type=Path)
    sync.add_argument("--device-unit-id", action="store_true")
    sync.add_argument("--timezone", help="Timezone where you trained, e.g. Europe/Berlin; used for automatic time adjustment")
    sync.add_argument("--no-adjust-times", action="store_true", help="Keep original FIT timestamps unchanged")
    sync.add_argument("--data-dir", type=Path, default=Path("private/sync"))
    sync.add_argument("--activity-id", action="append", help="Ready ID to upload; repeat for a batch, default newest ready only")
    sync.add_argument("--preview-only", action="store_true")
    sync.add_argument("--json", action="store_true")
    for command, description in (
        ("account-login", "Sign in and read activities; optionally remember the session on Windows"),
        ("account-activities", "Read activities using a saved Windows account session"),
        ("account-logout", "Remove a saved account session from Windows Credential Manager"),
    ):
        account = commands.add_parser(command, help=description)
        account.add_argument("provider", choices=("mywhoosh", "garmin"))
        account.add_argument("--json", action="store_true")
        if command != "account-logout":
            account.add_argument("--limit", type=_limit, default=10)
        if command == "account-login":
            account.add_argument("--remember", action="store_true",
                                 help="Store session tokens in Windows Credential Manager; never the password")
    activities = commands.add_parser("activities", help="Aktivitäten mit gespeicherter oder interaktiver Sitzung lesen")
    activities.add_argument("--limit", type=_limit, default=10)
    activities.add_argument("--json", action="store_true", help="Aktivitäten als JSON ausgeben")
    activities.add_argument("--interactive", action="store_true",
                            help="Bei jedem Aufruf anmelden; keine Sitzung lesen oder speichern")
    download = commands.add_parser("download", help="FIT einer expliziten Aktivitäts-ID herunterladen und prüfen")
    download.add_argument("activity_id", help="Quell-ID aus der Aktivitätenliste")
    download.add_argument("--interactive", action="store_true", help="Manuell anmelden; keine Tokenablage verwenden")
    download.add_argument("--output-dir", type=Path, default=Path("private/fit"), help="Privates Zielverzeichnis (0700)")
    download.add_argument("--json", action="store_true", help="Prüfbericht als JSON ausgeben")
    validate = commands.add_parser("validate", help="Lokale FIT-Datei ohne Netzwerk und ohne Änderungen prüfen")
    validate.add_argument("file", type=Path)
    validate.add_argument("--json", action="store_true")
    prepare = commands.add_parser("prepare-fit", help="Experimentelle FIT-Kopie mit Geräteprofil erzeugen; kein Netzwerk")
    prepare.add_argument("file", type=Path)
    prepare.add_argument("--profile", choices=("edge530",), help="Geräteprofil; sonst aus config.local.toml")
    prepare.add_argument("--config", type=Path, help="Gerätekonfiguration; Standard: config.local.toml im Arbeitsverzeichnis")
    prepare.add_argument("--device-unit-id", action="store_true", help="Unit-ID verdeckt eingeben; überschreibt die Konfiguration")
    prepare.add_argument("--output", type=Path, help="Separate FIT-Zieldatei; Standard: QUELLNAME.edge530.fit")
    prepare.add_argument("--dry-run", action="store_true", help="Änderungen prüfen und anzeigen; keine Datei schreiben")
    prepare.add_argument("--json", action="store_true")
    repair_time = commands.add_parser("repair-fit-time", help="MyWhoosh-Zeitfehler lokal prüfen; standardmäßig nur Vorschau")
    repair_time.add_argument("file", type=Path, help="Unverändertes MyWhoosh-Original; vor dem Geräteprofil anwenden")
    repair_time.add_argument("--timezone", required=True, help="Explizite IANA-Zeitzone, z. B. Europe/Berlin")
    repair_time.add_argument("--output", type=Path, help="Vorgeschlagene Korrektur in separate neue FIT-Datei schreiben")
    repair_time.add_argument("--json", action="store_true")
    for command, help_text in (("garmin-activities", "Garmin-Aktivitäten nach manueller Anmeldung lesen"),
                               ("garmin-match", "Lokale FIT-Datei mit Garmin-Aktivitäten vergleichen; kein Upload"),
                               ("garmin-upload", "Genau eine FIT-Datei nach Abgleich und manueller Freigabe hochladen")):
        garmin = commands.add_parser(command, help=help_text)
        garmin.add_argument("--interactive", action="store_true", required=True,
                            help="Manuelle Anmeldung ohne Tokenablage (erforderlich)")
        if command == "garmin-upload":
            garmin.set_defaults(limit=100)
        else:
            garmin.add_argument("--limit", type=_limit, default=100 if command == "garmin-match" else 10)
        garmin.add_argument("--json", action="store_true")
        if command in ("garmin-match", "garmin-upload"):
            garmin.add_argument("file", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "sync":
            return _sync_command(args)
        if args.command.startswith("account-"):
            return _account_command(args)
        if args.command == "prepare-fit":
            return _prepare_fit(args)
        if args.command == "repair-fit-time":
            return _repair_fit_time(args)
        if args.command in ("garmin-activities", "garmin-match", "garmin-upload"):
            return _garmin_command(args)
        if args.command == "validate":
            report = validate_fit(read_fit(args.file))
            _print_fit_report(report, args.file, json_output=args.json)
            return 0 if report.valid else 1
        if args.command == "download":
            return _download(args)
        if args.command == "activities" and args.interactive:
            _require_terminal()
            with MyWhooshClient() as client:
                session = _prompt_login(client)
                result = client.activities(session, limit=args.limit)
            _print_activities(result, json_output=args.json)
            return 0

        store = TokenStore(args.state_dir)
        if args.command == "check":
            print(f"Python: {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
            print("Netzwerk: nicht verwendet")
            try:
                store.load()
            except SessionError as exc:
                print(f"Sitzung: {exc}")
                return 1
            print("Sitzung: lokal lesbar; Gültigkeit beim Anbieter nicht geprüft")
            return 0
        if args.command == "login":
            _require_terminal()
            # Validate location before sending any credentials.
            store.prepare()
            with MyWhooshClient() as client:
                session = _prompt_login(client)
            store.save(session)
            print("Sitzung gespeichert. Das Passwort wurde nicht gespeichert.")
            return 0
        session = store.load()
        with MyWhooshClient() as client:
            result = client.activities(session, limit=args.limit)
        _print_activities(result, json_output=args.json)
        return 0
    except SyncError as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("Abgebrochen.", file=sys.stderr)
        return 130
    except Exception:
        # Do not print third-party exceptions: these can contain private payloads.
        print("Unerwarteter lokaler Fehler; keine Details mit möglichen Geheimnissen ausgegeben.", file=sys.stderr)
        return 1
