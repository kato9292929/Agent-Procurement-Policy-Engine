-- Spend Guard ledger: append-only event log.
--
-- Two columns hold the event, and the difference matters:
--
--   event_json  the RFC 8785 canonical form. This is the record of truth and
--               the ONLY thing event_hash is computed or verified against.
--   payload     JSONB, for querying. JSONB reorders object keys and
--               renormalises numbers, so a hash taken over it would disagree
--               with the JSONL backend for the very same event.
--
-- The application writes both from one in-memory object, so they cannot drift.

CREATE TABLE IF NOT EXISTS spend_guard_ledger (
  seq                 BIGSERIAL PRIMARY KEY,
  event_id            TEXT NOT NULL UNIQUE,
  event_type          TEXT NOT NULL,
  schema_version      TEXT NOT NULL,
  decision_id         TEXT,
  candidate_id        TEXT,
  request_id          TEXT,
  occurred_at         TIMESTAMPTZ NOT NULL,
  previous_event_hash TEXT,
  event_hash          TEXT NOT NULL UNIQUE,
  event_json          TEXT NOT NULL,   -- canonical form; the hash basis
  payload             JSONB NOT NULL,  -- search only; never hashed
  recorded_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS spend_guard_ledger_decision_id_idx ON spend_guard_ledger (decision_id);
CREATE INDEX IF NOT EXISTS spend_guard_ledger_request_id_idx  ON spend_guard_ledger (request_id);
CREATE INDEX IF NOT EXISTS spend_guard_ledger_type_time_idx   ON spend_guard_ledger (event_type, occurred_at);
