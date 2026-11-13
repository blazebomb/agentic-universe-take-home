CREATE TABLE IF NOT EXISTS metadata (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drops (
    fingerprint TEXT PRIMARY KEY,
    lead_rows_total INTEGER NOT NULL,
    duplicate_lead_rows_merged INTEGER NOT NULL,
    field_warnings INTEGER NOT NULL,
    events_total INTEGER NOT NULL,
    malformed_events INTEGER NOT NULL,
    duplicate_events_dropped INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    phone_key TEXT NOT NULL,
    phone_masked TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    external_ref TEXT NOT NULL,
    attributes TEXT NOT NULL,
    UNIQUE (workflow_id, phone_key)
);

CREATE TABLE IF NOT EXISTS rejections (
    drop_fingerprint TEXT NOT NULL REFERENCES drops(fingerprint),
    workflow_id TEXT NOT NULL,
    batch_id TEXT NOT NULL,
    row_number INTEGER NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (drop_fingerprint, workflow_id, batch_id, row_number)
);

CREATE TABLE IF NOT EXISTS dispositions (
    workflow_id TEXT NOT NULL,
    code TEXT NOT NULL,
    is_terminal INTEGER NOT NULL,
    is_success INTEGER NOT NULL,
    PRIMARY KEY (workflow_id, code)
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    workflow_id TEXT,
    call_id TEXT,
    event_type TEXT,
    occurred_at TEXT,
    phone_key TEXT,
    disposition_code TEXT,
    encrypted_payload BLOB
);

CREATE INDEX IF NOT EXISTS events_phone ON events(workflow_id, phone_key);
CREATE INDEX IF NOT EXISTS events_call ON events(workflow_id, call_id);

CREATE TRIGGER IF NOT EXISTS no_event_updates BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE TRIGGER IF NOT EXISTS no_event_deletes BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;

CREATE VIEW IF NOT EXISTS linked_events AS
WITH phone_matches AS MATERIALIZED (
    SELECT e.*, l.id AS lead_id
    FROM events e
    JOIN leads l USING (workflow_id, phone_key)
    WHERE e.event_type <> 'disposition.override'
), call_owners AS (
    SELECT workflow_id, call_id, lead_id,
           ROW_NUMBER() OVER (
               PARTITION BY workflow_id, call_id
               ORDER BY event_type <> 'call.initiated', occurred_at, event_id
           ) AS owner_rank
    FROM phone_matches
    WHERE EXISTS (
        SELECT 1 FROM events o
        WHERE o.workflow_id = phone_matches.workflow_id
          AND o.call_id = phone_matches.call_id AND o.event_type = 'disposition.override'
    )
)
SELECT * FROM phone_matches
UNION ALL
SELECT e.*, o.lead_id
FROM events e
JOIN call_owners o USING (workflow_id, call_id)
WHERE e.event_type = 'disposition.override' AND o.owner_rank = 1;
