-- Append-only enforcement, in two independent layers.
--
-- Grants stop the application from rewriting history even if its code has a
-- bug. The trigger additionally stops the table OWNER, whose privileges the
-- grants cannot restrict -- that is the case where an operator at a psql
-- prompt would otherwise silently rewrite a record.

CREATE OR REPLACE FUNCTION spend_guard_ledger_append_only()
RETURNS TRIGGER AS $$
BEGIN
  RAISE EXCEPTION
    'spend_guard_ledger is append-only: % is not permitted', TG_OP
    USING HINT = 'Record a correcting event instead of changing a stored one.';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS spend_guard_ledger_no_update ON spend_guard_ledger;
CREATE TRIGGER spend_guard_ledger_no_update
  BEFORE UPDATE ON spend_guard_ledger
  FOR EACH ROW EXECUTE FUNCTION spend_guard_ledger_append_only();

DROP TRIGGER IF EXISTS spend_guard_ledger_no_delete ON spend_guard_ledger;
CREATE TRIGGER spend_guard_ledger_no_delete
  BEFORE DELETE ON spend_guard_ledger
  FOR EACH ROW EXECUTE FUNCTION spend_guard_ledger_append_only();

-- TRUNCATE bypasses row-level triggers, so it needs a statement-level one.
DROP TRIGGER IF EXISTS spend_guard_ledger_no_truncate ON spend_guard_ledger;
CREATE TRIGGER spend_guard_ledger_no_truncate
  BEFORE TRUNCATE ON spend_guard_ledger
  FOR EACH STATEMENT EXECUTE FUNCTION spend_guard_ledger_append_only();

-- Roles. Passwords are set by the operator (see docs/postgres-setup.md);
-- this migration deliberately contains no credentials.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'spend_guard_writer') THEN
    CREATE ROLE spend_guard_writer NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'spend_guard_reader') THEN
    CREATE ROLE spend_guard_reader NOLOGIN;
  END IF;
END
$$;

-- Writer: insert and read only. No UPDATE, DELETE or TRUNCATE is granted.
GRANT USAGE ON SCHEMA public TO spend_guard_writer;
GRANT SELECT, INSERT ON spend_guard_ledger TO spend_guard_writer;
GRANT USAGE ON SEQUENCE spend_guard_ledger_seq_seq TO spend_guard_writer;

-- Reader: select only.
GRANT USAGE ON SCHEMA public TO spend_guard_reader;
GRANT SELECT ON spend_guard_ledger TO spend_guard_reader;

REVOKE UPDATE, DELETE, TRUNCATE ON spend_guard_ledger FROM spend_guard_writer;
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON spend_guard_ledger FROM spend_guard_reader;
