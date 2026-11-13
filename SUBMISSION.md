# Submission

## Candidate

- **Name:** [Candidate to fill in]
- **Assignment chosen:** A, Interaction Ledger & Reporting Pipeline
- **Time spent:** [Candidate to fill in]

## How to run

From the repository root, with Python 3.11+ and SQLite 3.35+:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -r solution/requirements.txt

export LEDGER_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data
python -m solution ingest data/drop_1
python -m solution ingest data/drop_2
python -m solution report --out reports
python -m pytest -q
```

Generate the key once for a new database. Keep it outside Git and reuse it when adding drops to
that database. Reports run without the key. Use `python -m solution --db other.db ...` to select a
different database. Ingest also accepts both drop paths in one command, in delivery order.

## Design

SQLite stores leads, event records, disposition codes, rejections and per-drop counts. Leads use a
unique pair of workflow and keyed phone digest. Later rows update only supplied, valid fields and
keep the original batch. A default fills a field on creation and is never applied as an update.

The event ledger uses the source event ID as its primary key and blocks updates and deletes. It
keeps the original event encrypted, with separate columns for fields needed by reports. The
`linked_events` view resolves phone matches and overrides against the leads present at report time.
Unlinked rows stay in the ledger until a later drop supplies a match. SQL groups linked events into
calls and ranks dispositions. Python builds the batch, lead and daily totals from those calls.

Fernet encrypts the schema-marked lead fields and the original event records. A separate derived
HMAC key gives a stable lookup for each workflow and phone. The database also stores the masked
phone suffix and the report reference required by the supplied schemas.

## Decisions & assumptions

| Decision | Why | Alternative considered |
| --- | --- | --- |
| Identify a drop by relative filenames and file contents | A copied or renamed directory can be replayed safely | Use a directory name as the delivery ID |
| Resolve orphan and unknown-code counts when reporting | A late lead can move an event from orphan to linked or unknown | Maintain a table of pending links during ingest |
| Use half-up rounding for decimal fields | Exact decimal arithmetic with a predictable tie rule | Half-even rounding |
| Break equal disposition timestamps by event ID and equal call start times by call ID | Keep results stable across delivery order | Use arrival order |
| Treat a repeated call initiation as one call, with the earliest start | Calls are identified by workflow, call ID and lead | Use the last initiation |
| Match overrides within the same workflow and prefer a linked initiation if call ownership conflicts | The override carries a workflow and call ID but no phone | Reject ambiguous call ownership |
| Use the first nonempty matching column from left to right | Alias columns may overlap | Reject the entire file for overlapping columns |
| Treat missing structural event fields or invalid timestamps as malformed | Such events cannot contribute reliable call facts | Keep a separate format-error category |

## Failure modes considered

- Each drop commits in one transaction. An unreadable file or structurally broken CSV rolls back
  that drop. Previously committed drops remain available.
- Replaying identical bytes is a no-op. A corrected file is a new delivery. Rebuild from the
  intended drops in order if a correction should replace an earlier delivery.
- The database checks the key on ingest. The source data is already plaintext on disk, so this
  protects the pipeline's database and reports. Key rotation and schema migration are outside the
  implementation.
- Input directories are assumed to be finished and immutable while ingest is running. Report files
  are generated from one read snapshot and staged before being replaced one at a time.

## Testing

The test suite covers parsing, buckets, late leads, duplicate events, override order, IST dates,
success counts, key mismatch and rollback. The end-to-end fixture also checks all five reports,
encrypted storage and byte-for-byte database replay.

## Results

Both generated drops ran on small seeds 42 and 913. All five reports matched a separate check
against the source rows. Seed 913 also produced byte-identical reports when the drops were passed
together and separately.

| Seed | Lead rows | Event lines | Ingest | Report |
| --- | ---: | ---: | ---: | ---: |
| 42 | 2,057 | 8,745 | 0.62 s | 0.16 s |
| 913 | 2,067 | 8,931 | 0.71 s | 0.16 s |

Times are local wall-clock measurements. Seed 913 had 501 events still orphaned after both drops
and 20 linked disposition events with unknown codes.

## If I had another day

1. Add an explicit replacement and backfill workflow for corrected deliveries.
2. Benchmark large drops and profile the joins before deciding whether to persist derived calls.
3. Add report generation directories and an atomic pointer to the latest complete set.
