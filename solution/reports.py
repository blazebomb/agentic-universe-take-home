import csv
import json
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from .ledger import connect
from .normalize import IST

BUCKETS = (
    "yet_to_reach_out", "in_progress_telephony_failed", "in_progress_dialed", "completed",
)


@dataclass
class LeadTotals:
    calls: int = 0
    connected: int = 0
    failed: int = 0
    terminal: bool = False
    success: bool = False
    last_call: str = ""
    disposition: str = ""

    @property
    def bucket(self):
        if not self.calls:
            return "yet_to_reach_out"
        if self.terminal:
            return "completed"
        if self.failed == self.calls:
            return "in_progress_telephony_failed"
        return "in_progress_dialed"


@dataclass
class DailyTotals:
    calls: int = 0
    connected: int = 0
    successes: set[int] = field(default_factory=set)


def call_totals(db):
    lead_totals = defaultdict(LeadTotals)
    daily_totals = defaultdict(DailyTotals)
    query = Path(__file__).with_name("calls.sql").read_text(encoding="utf-8")
    for call in db.execute(query):
        started = datetime.fromisoformat(call["initiated_at"]).astimezone(IST)
        lead = lead_totals[call["lead_id"]]
        lead.calls += 1
        lead.connected += call["connected"]
        lead.failed += call["failed"]
        lead.last_call = started.isoformat()
        if call["code"] is not None:
            lead.disposition = call["code"]
            lead.terminal |= bool(call["is_terminal"])
            lead.success |= bool(call["is_success"])

        day = daily_totals[(call["workflow_id"], started.date().isoformat())]
        day.calls += 1
        day.connected += call["connected"]
        if call["is_success"]:
            day.successes.add(call["lead_id"])

    return lead_totals, daily_totals


def quality_counts(db):
    counts = dict(db.execute("""
        SELECT COALESCE(SUM(lead_rows_total), 0) AS lead_rows_total,
               COALESCE(SUM(duplicate_lead_rows_merged), 0) AS duplicate_lead_rows_merged,
               COALESCE(SUM(field_warnings), 0) AS field_warnings,
               COALESCE(SUM(events_total), 0) AS events_total,
               COALESCE(SUM(malformed_events), 0) AS malformed_events,
               COALESCE(SUM(duplicate_events_dropped), 0) AS duplicate_events_dropped
        FROM drops
    """).fetchone())
    reasons = dict(db.execute("SELECT reason, COUNT(*) FROM rejections GROUP BY reason"))
    counts["lead_rows_rejected"] = sum(reasons.values())
    counts["rejections_by_reason"] = reasons
    counts["orphan_events"], counts["unknown_disposition_events"] = db.execute("""
        SELECT (SELECT COUNT(*) FROM events WHERE event_type IS NOT NULL) - COUNT(*),
               COUNT(CASE WHEN event_type IN ('disposition.set', 'disposition.override')
                          AND disposition_code IS NULL THEN 1 END)
        FROM linked_events
    """).fetchone()
    return counts


def write_csv(path, columns, rows):
    with path.open("w", newline="", encoding="utf-8") as output:
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(columns)
        writer.writerows(rows)


def lead_report_rows(db, totals, batches):
    for lead in db.execute("""
        SELECT id, workflow_id, external_ref, batch_id, phone_masked
        FROM leads ORDER BY workflow_id, external_ref, id
    """):
        summary = totals.get(lead["id"], LeadTotals())
        bucket = summary.bucket
        batches[(lead["workflow_id"], lead["batch_id"])][bucket] += 1
        yield (
            lead["workflow_id"], lead["external_ref"], lead["batch_id"], lead["phone_masked"],
            bucket, summary.calls, summary.connected, summary.last_call, summary.disposition,
            str(summary.success).lower(),
        )


def write_reports(db, out):
    leads, daily = call_totals(db)
    batches = defaultdict(lambda: dict.fromkeys(BUCKETS, 0))
    write_csv(
        out / "lead_report.csv",
        ("workflow_id", "external_ref", "batch_id", "phone_masked", "bucket", "total_calls",
         "connected_calls", "last_call_at", "current_disposition", "ever_success"),
        lead_report_rows(db, leads, batches),
    )
    write_csv(
        out / "batch_summary.csv",
        ("workflow_id", "batch_id", "total_leads", *BUCKETS),
        ((*key, sum(counts.values()), *(counts[bucket] for bucket in BUCKETS))
         for key, counts in sorted(batches.items())),
    )
    write_csv(
        out / "daily_overview.csv",
        ("workflow_id", "date", "total_calls", "connected_calls", "connectivity_rate", "success_leads"),
        ((*key, day.calls, day.connected, f"{day.connected / day.calls:.4f}", len(day.successes))
         for key, day in sorted(daily.items())),
    )
    write_csv(
        out / "rejections.csv",
        ("workflow_id", "batch_id", "row_number", "reason"),
        db.execute("""SELECT workflow_id, batch_id, row_number, reason FROM rejections
                      ORDER BY workflow_id, batch_id, row_number, drop_fingerprint"""),
    )
    (out / "data_quality.json").write_text(
        json.dumps(quality_counts(db), indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )


def report(db_path, out):
    if not db_path.is_file():
        raise ValueError(f"Database does not exist: {db_path}")
    out.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=out) as directory:
        staging = Path(directory)
        with closing(connect(db_path)) as db, db:
            db.execute("BEGIN")
            write_reports(db, staging)
        for file in staging.iterdir():
            file.replace(out / file.name)
