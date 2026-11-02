"""Exact audit snapshots for explicitly reviewed, unconfirmed upload attempts."""

import hashlib
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from .errors import FitError, SessionError
from .fit_processing import MAX_FIT_BYTES, validate_fit

SHA = re.compile(r"[0-9a-f]{64}")
NONCE = re.compile(r"[0-9a-f]{32}")


def load_review(workspace, retry, scope, source_id):
    if not retry or "review_hash" not in retry:
        return None
    data = workspace.read("retry-review-" + retry["nonce"] + ".json", maximum=131072)
    try:
        if data is None or hashlib.sha256(data).hexdigest() != retry["review_hash"]:
            raise ValueError
        review = json.loads(data)
        if (set(review) != {"kind", "scope", "source_id", "attempts", "journals", "files"}
                or review["kind"] not in ("unconfirmed", "deleted")
                or review["scope"] != scope or review["source_id"] != source_id
                or not isinstance(review["attempts"], list) or not 1 <= len(review["attempts"]) <= 10):
            raise ValueError
        for attempt in review["attempts"]:
            if (set(attempt) != {"target_account", "file_hash", "status", "scope", "source_id", "garmin_id"}
                    or attempt["scope"] != scope or attempt["source_id"] != source_id
                    or attempt["status"] not in ("upload_pending", "needs_review", "uploaded")
                    or not isinstance(attempt["file_hash"], str) or not SHA.fullmatch(attempt["file_hash"])
                    or not isinstance(attempt["target_account"], str)
                    or not (attempt["target_account"] == retry["target_account"]
                            or (attempt["target_account"].startswith(retry["target_account"] + ":")
                                and NONCE.fullmatch(attempt["target_account"][len(retry["target_account"]) + 1:])))):
                raise ValueError
        for key in ("journals", "files"):
            if not isinstance(review[key], dict) or len(review[key]) > 100:
                raise ValueError
            for name, digest in review[key].items():
                if (not isinstance(name, str) or Path(name).name != name or name in ("", ".", "..")
                        or not isinstance(digest, str) or not SHA.fullmatch(digest)):
                    raise ValueError
    except (ValueError, TypeError, KeyError, RecursionError):
        raise SessionError("The saved retry review is invalid. Keep existing upload records.") from None
    return review


def review_expired(review, retry, now):
    if not review or review["kind"] != "unconfirmed":
        return False
    try:
        stamp = datetime.fromisoformat(retry["verified_at_utc"])
        return not timedelta(0) <= now - stamp <= timedelta(minutes=15)
    except (ValueError, TypeError):
        return True


def comparison_reports(workspace, review):
    reports = []
    if review:
        for name, digest in review["files"].items():
            data = workspace.read(name, maximum=MAX_FIT_BYTES)
            if data is None or hashlib.sha256(data).hexdigest() != digest:
                raise FitError("A previously attempted FIT is missing or changed. Keep existing upload records.")
            report = validate_fit(data)
            if not report.valid:
                raise FitError("A previously attempted FIT did not pass validation.")
            reports.append(report)
    return tuple(reports)


def capture_review(workspace, journal, row, attempts, hashes, *, target, scope, source_id,
                   kind="unconfirmed", previous=None):
    if not 1 <= len(attempts) <= 10:
        raise SessionError("No owned upload attempt can be reviewed, or the retry limit was reached.", code="retry_uncertain")
    if kind == "deleted" and any(a["status"] != "uploaded" and a not in (previous or {}).get("attempts", ()) for a in attempts):
        raise SessionError("An unconfirmed attempt still requires manual absence confirmation.", code="retry_uncertain")
    files = {row["original_name"]: row["original_hash"]}
    stem = row["original_name"].removesuffix(".original.fit")
    for attempt in attempts:
        digest = attempt["file_hash"]
        if not isinstance(digest, str) or not SHA.fullmatch(digest):
            raise SessionError("A previous upload record is invalid.", code="retry_uncertain")
        name = row["original_name"] if digest == row["original_hash"] else stem + "." + digest + ".fit"
        files[name] = digest
        hashes.add(digest)
    review = {"kind": kind, "scope": scope, "source_id": source_id,
              "attempts": attempts, "journals": {}, "files": files}
    comparison_reports(workspace, review)
    for digest in hashes:
        paths = [journal.path(digest + ".json"), *journal.root.glob(digest + ".retry-*.json")]
        if len(paths) > 33:
            raise SessionError("Too many upload records need review.", code="retry_uncertain")
        for path in paths:
            data = journal.read(path.name, maximum=8192)
            if data is None:
                continue
            try:
                marker = json.loads(data)
                candidates = attempts
                if ".retry-" in path.name:
                    nonce = path.name.split(".retry-", 1)[1].removesuffix(".json")
                    candidates = [a for a in attempts if NONCE.fullmatch(nonce) and a["target_account"] == target + ":" + nonce]
                if (not isinstance(marker, dict) or marker.get("sha256") != digest
                        or not any(marker.get("status") == a["status"] and marker.get("activity_id") == a["garmin_id"] for a in candidates)):
                    raise ValueError
            except (ValueError, TypeError, RecursionError):
                raise SessionError("An unrelated or unclear upload record cannot be included in this retry.", code="retry_uncertain") from None
            review["journals"][path.name] = hashlib.sha256(data).hexdigest()
    if len(review["journals"]) > 100:
        raise SessionError("Too many upload records need review.", code="retry_uncertain")
    return review
