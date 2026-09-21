"""Guarantees specific to the Postgres ledger (D-3, D-4, D-7).

Skipped unless SPEND_GUARD_TEST_DATABASE_URL points at a database.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
import support
from spend_guard.backends.base import prepare_event, verify_chain
from spend_guard.errors import LedgerError
from spend_guard.ledger import Ledger

pytestmark = pytest.mark.postgres

psycopg = pytest.importorskip("psycopg")


def event(n: int):
    return prepare_event(
        "candidate_observed",
        decision_id=f"dec_{n}",
        candidate_id=f"cand_{n}",
        request_id=f"req_{n}",
        payload={"input_hash": f"sha256:{n:064d}"},
    )


def backend_for(dsn: str):
    from spend_guard.backends.postgres import PostgresLedgerBackend

    return PostgresLedgerBackend(dsn)


# -- D-3: the table really is append-only ----------------------------------


@pytest.fixture
def writer_dsn(clean_postgres):
    """A login role holding spend_guard_writer, as production would use."""
    with psycopg.connect(clean_postgres, autocommit=True) as connection:
        connection.execute("DROP ROLE IF EXISTS sg_writer_login")
        connection.execute("CREATE ROLE sg_writer_login LOGIN PASSWORD 'test-only-not-a-secret'")
        connection.execute("GRANT spend_guard_writer TO sg_writer_login")
    yield clean_postgres.replace("postgres@", "sg_writer_login:test-only-not-a-secret@")
    with psycopg.connect(clean_postgres, autocommit=True) as connection:
        connection.execute("DROP ROLE IF EXISTS sg_writer_login")


def test_the_writer_role_can_insert_and_read(writer_dsn):
    backend = backend_for(writer_dsn)
    try:
        backend.append(event(1))
        assert len(list(backend.iter_events())) == 1
    finally:
        backend.close()


@pytest.mark.parametrize("statement", [
    "UPDATE spend_guard_ledger SET event_type = 'tampered'",
    "DELETE FROM spend_guard_ledger",
    "TRUNCATE spend_guard_ledger",
])
def test_the_writer_role_cannot_rewrite_history(writer_dsn, clean_postgres, statement):
    backend_for(clean_postgres).append(event(1))
    with psycopg.connect(writer_dsn, autocommit=True) as connection:
        with pytest.raises(psycopg.Error) as caught:
            connection.execute(statement)
    assert "permission denied" in str(caught.value).lower() or "append-only" in str(caught.value)


@pytest.mark.parametrize("statement", [
    "UPDATE spend_guard_ledger SET event_type = 'tampered'",
    "DELETE FROM spend_guard_ledger",
    "TRUNCATE spend_guard_ledger",
])
def test_even_the_owner_is_stopped_by_the_trigger(clean_postgres, statement):
    """Grants cannot restrict the owner, so the trigger is the layer that does."""
    backend_for(clean_postgres).append(event(1))
    with psycopg.connect(clean_postgres, autocommit=True) as connection:
        with pytest.raises(psycopg.Error) as caught:
            connection.execute(statement)
    assert "append-only" in str(caught.value)


def test_a_stored_row_survives_a_rejected_update(clean_postgres):
    backend = backend_for(clean_postgres)
    stored = backend.append(event(1))
    with psycopg.connect(clean_postgres, autocommit=True) as connection:
        with pytest.raises(psycopg.Error):
            connection.execute("UPDATE spend_guard_ledger SET event_json = '{}'")
    assert list(backend.iter_events())[0]["event_hash"] == stored["event_hash"]
    backend.close()


# -- D-2: event_json is the hash basis, payload is only for searching ------


def test_event_json_and_payload_agree(clean_postgres):
    backend = backend_for(clean_postgres)
    payload = {"b": 2, "a": 1, "nested": {"z": [1, 2.5]}, "text": "決算"}
    backend.append(prepare_event("guard_evaluated", decision_id="d", candidate_id="c",
                                 request_id="r", payload=payload))
    with psycopg.connect(clean_postgres) as connection:
        row = connection.execute("SELECT event_json, payload FROM spend_guard_ledger").fetchone()
    from_json = json.loads(row[0])
    assert from_json["payload"] == row[1] == payload
    backend.close()


def test_payload_is_queryable_as_jsonb(clean_postgres):
    backend = backend_for(clean_postgres)
    backend.append(prepare_event("guard_evaluated", decision_id="d1", candidate_id="c1",
                                 request_id="r1", payload={"decision": "HOLD"}))
    backend.append(prepare_event("guard_evaluated", decision_id="d2", candidate_id="c2",
                                 request_id="r2", payload={"decision": "PAY"}))
    with psycopg.connect(clean_postgres) as connection:
        rows = connection.execute(
            "SELECT decision_id FROM spend_guard_ledger WHERE payload->>'decision' = 'HOLD'"
        ).fetchall()
    assert [r[0] for r in rows] == ["d1"]
    backend.close()


# -- D-4: concurrent appends ------------------------------------------------

WRITER = """
import sys
sys.path.insert(0, {src!r})
from spend_guard.backends.base import prepare_event
from spend_guard.backends.postgres import PostgresLedgerBackend

backend = PostgresLedgerBackend(sys.argv[1])
worker = sys.argv[2]
for index in range({count}):
    backend.append(prepare_event(
        "candidate_observed",
        decision_id=f"dec_{{worker}}_{{index}}",
        candidate_id=f"cand_{{worker}}_{{index}}",
        request_id=f"req_{{worker}}_{{index}}",
        payload={{"input_hash": f"sha256:{{worker}}{{index}}"}},
    ))
backend.close()
"""


def test_six_processes_appending_do_not_fork_the_chain(clean_postgres):
    """The advisory lock covers every connection, not one host's file."""
    script = WRITER.format(src=str(support.ROOT / "src"), count=20)
    processes = [
        subprocess.Popen([sys.executable, "-c", script, clean_postgres, str(worker)])
        for worker in range(6)
    ]
    for process in processes:
        assert process.wait(timeout=120) == 0

    backend = backend_for(clean_postgres)
    events = list(backend.iter_events())
    backend.close()
    assert len(events) == 120
    assert verify_chain(events) == [], "the chain forked across processes"
    assert len({e["event_hash"] for e in events}) == 120


# -- D-4: outage handling ---------------------------------------------------


def test_a_down_database_raises_rather_than_corrupting(clean_postgres):
    unreachable = "postgresql://postgres@127.0.0.1:1/spend_guard_test"
    backend = backend_for(unreachable)
    with pytest.raises(LedgerError):
        backend.append(event(1))


def test_writes_resume_after_the_database_comes_back(clean_postgres):
    """The held connection is dropped on failure and reopened on the next call."""
    backend = backend_for("postgresql://postgres@127.0.0.1:1/spend_guard_test")
    with pytest.raises(LedgerError):
        backend.append(event(1))

    backend._dsn = clean_postgres  # the outage ends
    stored = backend.append(event(2))
    assert stored["event_hash"]
    assert len(list(backend.iter_events())) == 1
    backend.close()


def test_the_connection_string_never_appears_in_an_error(clean_postgres):
    secret_dsn = "postgresql://someone:hunter2@127.0.0.1:1/spend_guard_test"
    backend = backend_for(secret_dsn)
    with pytest.raises(LedgerError) as caught:
        backend.append(event(1))
    message = str(caught.value)
    for fragment in ("hunter2", "someone", "127.0.0.1"):
        assert fragment not in message, f"{fragment} leaked into the error message"


def test_the_connection_string_never_reaches_the_ledger(clean_postgres):
    """A payload carrying a DSN must be redacted like any other credential."""
    ledger = Ledger(backend=backend_for(clean_postgres))
    ledger.append(
        "purchase_result",
        decision_id="d1",
        candidate_id="c1",
        request_id="r1",
        payload={"database_url": "postgresql://u:hunter2@host/db", "payment_status": "success"},
    )
    with psycopg.connect(clean_postgres) as connection:
        row = connection.execute("SELECT event_json FROM spend_guard_ledger").fetchone()
    assert "hunter2" not in row[0]
    assert "[redacted]" in row[0]
    ledger.backend.close()
