WITH ranked_dispositions AS (
    SELECT e.workflow_id, e.call_id, e.lead_id, d.code, d.is_terminal, d.is_success,
           ROW_NUMBER() OVER (
               PARTITION BY e.workflow_id, e.call_id, e.lead_id
               ORDER BY (e.event_type = 'disposition.override') DESC,
                        e.occurred_at DESC, e.event_id DESC
           ) AS rank
    FROM linked_events e
    JOIN dispositions d ON d.workflow_id = e.workflow_id AND d.code = e.disposition_code
    WHERE e.event_type IN ('disposition.set', 'disposition.override')
), calls AS (
    SELECT workflow_id, call_id, lead_id,
           MIN(CASE WHEN event_type = 'call.initiated' THEN occurred_at END) AS initiated_at,
           MAX(event_type = 'call.connected') AS connected,
           MAX(event_type = 'call.telephony_failed') AS failed
    FROM linked_events
    GROUP BY workflow_id, call_id, lead_id
    HAVING initiated_at IS NOT NULL
)
SELECT c.*, d.code, d.is_terminal, d.is_success
FROM calls c
LEFT JOIN ranked_dispositions d USING (workflow_id, call_id, lead_id)
WHERE d.rank = 1 OR d.rank IS NULL
ORDER BY c.lead_id, c.initiated_at, c.call_id
