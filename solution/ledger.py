import hashlib
import hmac
import json
import os
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

from cryptography.fernet import Fernet

from .normalize import lead_rows, load_dispositions, load_schemas, phone, timestamp

EVENT_TYPES = {
    "call.initiated", "call.connected", "call.ended", "call.telephony_failed",
    "disposition.set", "disposition.override",
}


class Privacy:
    def __init__(self, key):
        self.cipher = Fernet(key)
        self.match_key = hmac.digest(key.encode("ascii"), b"phone matching", "sha256")

    def phone_key(self, workflow, number):
        message = f"{workflow}\0{number}".encode()
        return hmac.digest(self.match_key, message, "sha256").hex()

    def encrypt(self, value):
        return self.cipher.encrypt(json.dumps(value, ensure_ascii=False).encode())

    @property
    def key_check(self):
        return hmac.digest(self.match_key, b"ledger key check", "sha256").hex()


def connect(path):
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA recursive_triggers = ON")
    return db


def drop_files(path):
    if not path.is_dir():
        raise ValueError(f"Drop directory does not exist: {path}")
    leads = sorted((path / "leads").glob("*/*.csv"), key=lambda file: (file.name, file.parent.name))
    events = sorted((path / "events").glob("*.jsonl"))
    if not (leads or events):
        raise ValueError(f"No lead or event files found in {path}")
    return leads, events


def fingerprint(path, files):
    digest = hashlib.sha256()
    for file in files:
        relative = file.relative_to(path).as_posix().encode()
        with file.open("rb") as source:
            content = hashlib.file_digest(source, "sha256").digest()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(content)
    return digest.hexdigest()


def ingest(db_path, paths):
    privacy = Privacy(os.environ["LEDGER_KEY"])
    schemas = load_schemas()
    catalogue = load_dispositions()
    codes = {(workflow, code) for workflow, code, _, _ in catalogue}
    with closing(connect(db_path)) as db:
        db.executescript(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
        for path in paths:
            leads, events = drop_files(path)
            drop_id = fingerprint(path, leads + events)
            with db:
                db.execute("BEGIN IMMEDIATE")
                check = db.execute("SELECT value FROM metadata WHERE name = 'key_check'").fetchone()
                if check and not hmac.compare_digest(check["value"], privacy.key_check):
                    raise ValueError("LEDGER_KEY does not match this database")
                if db.execute("SELECT 1 FROM drops WHERE fingerprint = ?", (drop_id,)).fetchone():
                    continue
                db.execute("INSERT OR IGNORE INTO metadata VALUES ('key_check', ?)", (privacy.key_check,))
                db.executemany("INSERT OR IGNORE INTO dispositions VALUES (?, ?, ?, ?)", catalogue)
                db.execute("INSERT INTO drops VALUES (?, 0, 0, 0, 0, 0, 0)", (drop_id,))
                counts = Counter()
                for file in leads:
                    ingest_leads(db, file, schemas, drop_id, privacy, counts)
                for file in events:
                    ingest_events(db, file, privacy, codes, counts)
                db.execute(
                    """UPDATE drops SET lead_rows_total = ?, duplicate_lead_rows_merged = ?,
                       field_warnings = ?, events_total = ?, malformed_events = ?,
                       duplicate_events_dropped = ? WHERE fingerprint = ?""",
                    (counts["lead_rows_total"], counts["duplicate_lead_rows_merged"],
                     counts["field_warnings"], counts["events_total"], counts["malformed_events"],
                     counts["duplicate_events_dropped"], drop_id),
                )


def ingest_leads(db, file, schemas, drop_id, privacy, counts):
    workflow = file.parent.name
    if workflow not in schemas:
        raise ValueError(f"No lead schema for workflow {workflow}")
    fields = schemas[workflow]
    reference = next(field["name"] for field in fields if field.get("reference"))
    private_fields = {field["name"] for field in fields if field.get("pii")}
    defaults = {field["name"]: field["default"] for field in fields if "default" in field}
    for row_number, (values, warnings, reason) in lead_rows(file, fields):
        counts["lead_rows_total"] += 1
        if reason:
            db.execute(
                "INSERT INTO rejections VALUES (?, ?, ?, ?, ?)",
                (drop_id, workflow, file.stem, row_number, reason),
            )
            continue
        counts["field_warnings"] += warnings
        number = values["phone"]
        phone_key = privacy.phone_key(workflow, number)
        existing = db.execute(
            "SELECT id, attributes FROM leads WHERE workflow_id = ? AND phone_key = ?",
            (workflow, phone_key),
        ).fetchone()
        attributes = (
    json.loads(existing["attributes"]) if existing
    else dict.fromkeys(field["name"] for field in fields)
)
        changes = values if existing else defaults | values
        for name, value in changes.items():
            attributes[name] = (
                privacy.encrypt(value).decode("ascii") if name in private_fields else value
            )
        encoded = json.dumps(attributes, ensure_ascii=False)
        if existing:
            counts["duplicate_lead_rows_merged"] += 1
            db.execute(
                "UPDATE leads SET external_ref = ?, attributes = ? WHERE id = ?",
                (values[reference], encoded, existing["id"]),
            )
        else:
            db.execute(
                """INSERT INTO leads (workflow_id, phone_key, phone_masked, batch_id,
                                      external_ref, attributes) VALUES (?, ?, ?, ?, ?, ?)""",
                (workflow, phone_key, "+91******" + number[-4:], file.stem, values[reference], encoded),
            )


def read_event(line):
    try:
        value = json.loads(line)
    except (ValueError, UnicodeError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


def event_values(event, privacy, codes):
    workflow, call_id, kind = (event.get(key) for key in ("workflow_id", "call_id", "event_type"))
    if not all(isinstance(value, str) and value.strip() for value in (workflow, call_id, kind)):
        raise ValueError("missing event fields")
    if kind not in EVENT_TYPES:
        raise ValueError("unknown event type")
    when = timestamp(event.get("occurred_at"))
    match_key = None
    raw_phone = event.get("phone")
    if kind != "disposition.override" and isinstance(raw_phone, str):
        try:
            match_key = privacy.phone_key(workflow, phone(raw_phone))
        except ValueError:
            pass

    payload = event.get("payload")
    code = payload.get("disposition_code") if isinstance(payload, dict) else None
    known_code = code if isinstance(code, str) and (workflow, code) in codes else None
    return workflow, call_id, kind, when, match_key, known_code, privacy.encrypt(event)


def ingest_events(db, file, privacy, codes, counts):
    with file.open("rb") as source:
        for line in source:
            if not line.strip():
                continue
            counts["events_total"] += 1
            event = read_event(line)
            event_id = event.get("event_id")
            if not isinstance(event_id, str) or not event_id.strip():
                counts["malformed_events"] += 1
                continue
            if db.execute("SELECT 1 FROM events WHERE event_id = ?", (event_id,)).fetchone():
                counts["duplicate_events_dropped"] += 1
                continue
            try:
                values = event_values(event, privacy, codes)
            except (ValueError, OverflowError, RecursionError):
                counts["malformed_events"] += 1
                db.execute("INSERT INTO events (event_id) VALUES (?)", (event_id,))
                continue
            db.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (event_id, *values))
