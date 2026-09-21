# Setting up the Postgres ledger (for the repository owner)

This is the operator's half of the Postgres backend. Creating the database,
running the migrations and setting the environment variables are deliberately
**not** automated: they need production credentials, which no code in this
repository should ever hold.

The application half is done: `PostgresLedgerBackend`, the migrations, and the
`guard verify` / `export` / `import` commands.

## Why Postgres and not a Railway Volume

A volume would be simpler, but a service with a volume attached cannot run
replicas and takes downtime on every redeploy. That would change the
availability of the **payment service** — the one thing shadow mode must not
touch. A separate Postgres keeps the ledger durable without putting the
payment service's uptime at the mercy of the guard.

## 1. Add Postgres on Railway

1. Open the Railway project.
2. **New → Database → Add PostgreSQL.**
3. Wait for it to provision. It publishes `DATABASE_URL` on the Postgres
   service.

## 2. Run the migrations

From a machine that can reach the database (Railway's shell, or locally with
the public connection string):

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/001_spend_guard_ledger.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/002_spend_guard_append_only.sql
```

`001` creates the table and its indexes. `002` makes it append-only:

- `spend_guard_writer` gets `INSERT` and `SELECT` only — no `UPDATE`,
  `DELETE`, or `TRUNCATE`.
- `spend_guard_reader` gets `SELECT` only.
- Triggers raise on `UPDATE`, `DELETE` and `TRUNCATE`. This layer exists
  because grants cannot restrict the **table owner**, which is exactly the
  account someone would be using at a `psql` prompt.

Both files are safe to re-run.

## 3. Create the login roles and set passwords

The migration creates the two roles `NOLOGIN` and with no password, so no
credential is ever committed. Give them a login identity yourself:

```sql
CREATE ROLE spend_guard_app LOGIN PASSWORD '<generate a strong password>';
GRANT spend_guard_writer TO spend_guard_app;

CREATE ROLE spend_guard_analyst LOGIN PASSWORD '<a different strong password>';
GRANT spend_guard_reader TO spend_guard_analyst;
```

Use `spend_guard_app` for the payment service and `spend_guard_analyst` for
anyone running `guard report` by hand. Do not use the Postgres superuser: the
append-only triggers will still stop it, but its grants would not.

## 4. Point the payment service at it

On the **payment service** (not the Postgres service), add:

| Variable | Value |
|---|---|
| `SPEND_GUARD_DATABASE_URL` | the connection string using `spend_guard_app` |

Railway reference variables let you build it from the Postgres service without
copying the password around. In the payment service's variables:

```
SPEND_GUARD_DATABASE_URL=postgresql://spend_guard_app:${{Postgres.PGPASSWORD_APP}}@${{Postgres.PGHOST}}:${{Postgres.PGPORT}}/${{Postgres.PGDATABASE}}
```

Substitute the real service name if the Postgres service is not called
`Postgres`, and store the app role's password as a variable on the Postgres
service rather than inline.

Then switch the policy over:

```json
"ledger": {
  "backend": "postgres",
  "dsn_env": "SPEND_GUARD_DATABASE_URL",
  "lock_timeout_ms": 2000,
  "statement_timeout_ms": 2000
}
```

The connection string is read from the environment and is never written to the
policy file, the logs, the ledger, or any error message. That last one is
enforced by a test.

## 5. Check it

```bash
SPEND_GUARD_DATABASE_URL=... guard --backend postgres verify
```

Expected on an empty ledger:

```json
{"backend": "postgres", "events": 0, "chain_intact": true, "problems": []}
```

`chain_intact: false` means a stored event no longer matches its hash. Do not
"fix" it by editing rows — that is the thing the chain exists to detect.
Capture the output and investigate.

## 6. Move an existing JSONL ledger across

```bash
guard --backend postgres import --from var/spend-guard/ledger.jsonl
guard --backend postgres verify
```

`import` verifies the source chain **before** writing anything, so a ledger
that is already broken is not silently baked into the new store. Events are
re-sealed against the target so the imported run chains onto whatever is
already there; add `--append` to load into a non-empty ledger.

To go the other way, for analysis or backup:

```bash
guard --backend postgres export --out ledger-$(date +%F).jsonl
```

## Operational notes

- **The guard never blocks a payment.** While the database is unreachable,
  candidates queue in memory up to `hook.max_pending`, then get dropped and
  counted. The payment path is unaffected either way.
- **Watch `orphaned_candidates`** in `guard report`. It counts observations
  whose judgment never arrived, which is the ledger-visible symptom of an
  outage or a queue overflow.
- **Drops and shutdown losses are not in the ledger.** A candidate that was
  never written leaves no trace in it by definition. Those counts come from
  the host log (`spend_guard.dropped`, `spend_guard.unflushed_at_shutdown`).
- **Backups.** Railway's Postgres backups cover this table. Because it is
  append-only and hash-chained, a restore can be checked with `guard verify`.
