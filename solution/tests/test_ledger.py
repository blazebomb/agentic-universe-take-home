import csv
import json
import sqlite3
from contextlib import closing

import pytest
from cryptography.fernet import Fernet

from solution.ledger import connect, ingest
from solution.normalize import amount, calendar_date, phone
from solution.reports import LeadTotals, report

WORKFLOW = "motor_renewal"


@pytest.mark.parametrize("raw", ["9876543210", "+91 98765-43210", "09876543210", "(91) 9876543210"])
def test_phone_formats(raw):
    assert phone(raw) == "+919876543210"


@pytest.mark.parametrize("raw", ["5987654321", "987654321", "+1 9876543210", "N/A"])
def test_invalid_phone(raw):
    with pytest.raises(ValueError, match="invalid_phone"):
        phone(raw)


@pytest.mark.parametrize("raw", ["2024-02-29", "29/02/2024", "29-02-2024", "29-fEb-2024"])
def test_date_formats(raw):
    assert calendar_date(raw) == "2024-02-29"


@pytest.mark.parametrize("raw", ["29/02/2026", "02/29/2024", "31-APR-2024"])
def test_invalid_date(raw):
    with pytest.raises(ValueError):
        calendar_date(raw)


def test_currency_rounding():
    assert amount("Rs. 1,234.565") == "1234.57"
    for invalid in ("-1", "NaN", "1,2", "Rs. 12,34"):
        with pytest.raises(ValueError):
            amount(invalid)


@pytest.mark.parametrize(
    "calls,failed,terminal,bucket",
    [
        (0, 0, False, "yet_to_reach_out"),
        (2, 2, False, "in_progress_telephony_failed"),
        (2, 1, False, "in_progress_dialed"),
        (2, 2, True, "completed"),
    ],
)
def test_buckets(calls, failed, terminal, bucket):
    assert LeadTotals(calls=calls, failed=failed, terminal=terminal).bucket == bucket


@pytest.fixture
def key(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("LEDGER_KEY", key)
    return key


def write_leads(drop, batch, content):
    path = drop / "leads" / WORKFLOW / f"{batch}.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def write_events(drop, entries):
    path = drop / "events" / "events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(entry if isinstance(entry, str) else json.dumps(entry) for entry in entries) + "\n",
        encoding="utf-8",
    )


def event(event_id, call_id, kind, when, number="9876543210", code=None):
    result = {
        "event_id": event_id, "call_id": call_id, "event_type": kind,
        "occurred_at": when, "workflow_id": WORKFLOW,
        "payload": {"disposition_code": code, "overridden_by": "Private Supervisor"},
    }
    if number is not None:
        result["phone"] = number
    return result


def read_csv(out, file):
    with (out / file).open(newline="", encoding="utf-8") as source:
        return list(csv.DictReader(source))


def read_quality(out):
    return json.loads((out / "data_quality.json").read_text(encoding="utf-8"))


def test_reports_relink_late_events_and_keep_override_history(tmp_path, key):
    first, second = tmp_path / "first", tmp_path / "second"
    db, out = tmp_path / "ledger.db", tmp_path / "reports"
    write_leads(
        first, "batch_001",
        "  CUSTOMER__NAME ,Mobile No,Policy,Language,Expiry,ignored\n"
        " , , , , , \n"
        ",invalid,,,,\n"
        "No phone,,,bad,impossible,\n"
        "Bad phone,123456789,XX,bad,impossible,\n"
        "Private Customer,9876543210,ref-original,HI,01-Dec-2026,extra\n"
        "No calls,8765432109,ref-unreached,,,\n"
        "Failed,7654321098,ref-failed,,31/02/2026,\n"
        "Dialed,6543210987,ref-dialed,,,\n",
    )
    write_leads(
        second, "batch_002",
        "Policy No,Phone Number,Name,Language\n"
        "ref-updated,09876543210,Private Customer Updated,bad\n"
        "ref-late,+91 99887 76655,Late Customer,\n",
    )
    first_start = event("a", "c1", "call.initiated", "2026-10-01T19:00:00Z")
    late_phone = "9988776655"
    write_events(first, [
        event("h", "c3", "disposition.override", "2026-10-02T01:00:00+05:30", None, "renewed"),
        event("c", "c1", "disposition.override", "2026-10-02 00:38:00", None, "not_interested"),
        event("b", "c1", "disposition.set", "2026-10-01T19:12:00Z", code="renewed"),
        first_start,
        event("d", "c1", "disposition.override", "2026-10-01T19:10:00Z", None, "renewed"),
        event("e", "c1", "call.connected", "2026-10-02 00:31:00"),
        event("f", "c2", "call.initiated", "2026-10-03 09:00:00"),
        event("g", "c2", "disposition.set", "2026-10-03 09:01:00", code="unknown"),
        event("i", "c3", "call.initiated", "2026-10-02 00:40:00", late_phone),
        event("j", "c3", "disposition.set", "2026-10-02 00:41:00", late_phone, "unknown"),
        event("k", "c4", "call.initiated", "2026-10-02 01:20:00", late_phone),
        event("l", "c4", "disposition.set", "2026-10-02 01:21:00", late_phone, "renewed"),
        event("m", "c5", "call.initiated", "2026-10-02 01:20:00", "7654321098"),
        event("n", "c5", "call.telephony_failed", "2026-10-02 01:21:00", "7654321098"),
        event("o", "c6", "call.initiated", "2026-10-02 01:20:00", "6543210987"),
        event("p", "no-start", "call.connected", "2026-10-02 01:20:00", "8765432109"),
        "{", "[]", "", first_start,
    ])
    write_events(second, [first_start])

    ingest(db, [first])
    report(db, out)
    assert read_quality(out)["orphan_events"] == 5
    assert read_quality(out)["unknown_disposition_events"] == 1
    ingest(db, [second])
    report(db, out)

    leads = {row["external_ref"]: row for row in read_csv(out, "lead_report.csv")}
    assert list(leads) == ["ref-dialed", "ref-failed", "ref-late", "ref-unreached", "ref-updated"]
    assert leads["ref-updated"] == {
        "workflow_id": WORKFLOW, "external_ref": "ref-updated", "batch_id": "batch_001",
        "phone_masked": "+91******3210", "bucket": "completed", "total_calls": "2",
        "connected_calls": "1", "last_call_at": "2026-10-03T09:00:00+05:30",
        "current_disposition": "renewed", "ever_success": "true",
    }
    assert leads["ref-unreached"]["bucket"] == "yet_to_reach_out"
    assert leads["ref-failed"]["bucket"] == "in_progress_telephony_failed"
    assert leads["ref-dialed"]["bucket"] == "in_progress_dialed"
    assert read_csv(out, "daily_overview.csv") == [
        {"workflow_id": WORKFLOW, "date": "2026-10-02", "total_calls": "5", "connected_calls": "1",
         "connectivity_rate": "0.2000", "success_leads": "2"},
        {"workflow_id": WORKFLOW, "date": "2026-10-03", "total_calls": "1", "connected_calls": "0",
         "connectivity_rate": "0.0000", "success_leads": "0"},
    ]
    assert [(row["batch_id"], row["total_leads"]) for row in read_csv(out, "batch_summary.csv")] == [
        ("batch_001", "4"), ("batch_002", "1"),
    ]
    assert [(row["row_number"], row["reason"]) for row in read_csv(out, "rejections.csv")] == [
        ("1", "missing_required:customer_name"), ("2", "missing_required:phone"), ("3", "invalid_phone"),
    ]
    assert read_quality(out) == {
        "lead_rows_total": 9, "lead_rows_rejected": 3, "duplicate_lead_rows_merged": 1,
        "field_warnings": 2, "events_total": 20, "malformed_events": 2,
        "duplicate_events_dropped": 2, "orphan_events": 0, "unknown_disposition_events": 2,
        "rejections_by_reason": {
            "missing_required:customer_name": 1, "missing_required:phone": 1, "invalid_phone": 1,
        },
    }
    with closing(connect(db)) as conn:
        attributes = json.loads(conn.execute(
            "SELECT attributes FROM leads WHERE external_ref = 'ref-updated'"
        ).fetchone()[0])
        assert attributes["preferred_language"] == "hi"
        assert json.loads(Fernet(key).decrypt(attributes["phone"])) == "+919876543210"
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM events WHERE event_id = 'b'")

    before = db.read_bytes()
    for private in (b"Private Customer", b"Private Supervisor", b"9876543210", key.encode()):
        assert private not in before
    ingest(db, [first, second])
    assert db.read_bytes() == before

    all_db, all_out = tmp_path / "all.db", tmp_path / "all-reports"
    ingest(all_db, [first, second])
    report(all_db, all_out)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == {
        p.name: p.read_bytes() for p in all_out.iterdir()
    }


def test_drop_rolls_back_on_broken_csv(tmp_path, key):
    drop = tmp_path / "drop"
    db = tmp_path / "ledger.db"
    write_leads(drop, "batch_001", "Name,Phone,Policy\nA,9876543210,p1\n")
    bad = write_leads(drop, "batch_002", 'Name,Phone,Policy\n"unfinished')
    with pytest.raises(csv.Error):
        ingest(db, [drop])
    with closing(connect(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM drops").fetchone()[0] == 0
    bad.write_text("Name,Phone,Policy\nB,8765432109,p2\n", encoding="utf-8")
    ingest(db, [drop])
    with closing(connect(db)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 2


def test_wrong_key_is_rejected(tmp_path, key, monkeypatch):
    drop = tmp_path / "drop"
    db = tmp_path / "ledger.db"
    write_leads(drop, "batch_001", "Name,Phone,Policy\nA,9876543210,p1\n")
    ingest(db, [drop])
    monkeypatch.setenv("LEDGER_KEY", Fernet.generate_key().decode())
    with pytest.raises(ValueError, match="does not match"):
        ingest(db, [drop])
