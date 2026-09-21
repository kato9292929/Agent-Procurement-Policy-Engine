"""Postgres ledger backend.

`psycopg` is imported inside this module, never at package import time, so the
JSONL backend keeps the runtime dependency-free. Install with
`pip install 'spend-guard[postgres]'`.

Appends serialise on a transaction-scoped advisory lock, which covers every
connection to the database rather than one host's file, so replicas and
several processes still produce one chain.

Two columns hold the event:

* `event_json` — the RFC 8785 canonical form. This is the record of truth and
  the only thing hashes are computed or verified against.
* `payload` — JSONB, for querying only. JSONB reorders keys and renormalises
  numbers, so hashing it would give a different answer than the JSONL backend.
"""

from __future__ import annotations

import json
from typing import Any, Iterator

from ..canonical import canonicalize
from ..errors import LedgerError
from .base import GENESIS, seal

# Any constant works; it only has to be the same everywhere. Derived from the
# table name so it will not collide with another application's advisory locks.
APPEND_LOCK_KEY = 0x5F9C_4A21


def _require_psycopg():
    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise LedgerError(
            "the postgres backend needs psycopg 3: pip install 'spend-guard[postgres]'"
        ) from exc
    return psycopg


class PostgresLedgerBackend:
    def __init__(
        self,
        dsn: str,
        *,
        lock_timeout_ms: int = 2000,
        statement_timeout_ms: int = 2000,
        connect: Any = None,
    ):
        if not dsn:
            raise LedgerError("postgres backend needs a connection string")
        self._dsn = dsn
        self.lock_timeout_ms = lock_timeout_ms
        self.statement_timeout_ms = statement_timeout_ms
        self._connect = connect
        self._connection: Any = None

    # -- connection handling --

    def _new_connection(self) -> Any:
        psycopg = _require_psycopg()
        connector = self._connect or psycopg.connect
        try:
            connection = connector(self._dsn)
        except Exception as exc:  # noqa: BLE001 - the DSN must not reach the message
            raise LedgerError(f"cannot connect to the ledger database: {type(exc).__name__}") from None
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"SET lock_timeout = {int(self.lock_timeout_ms)}")
                cursor.execute(f"SET statement_timeout = {int(self.statement_timeout_ms)}")
            connection.commit()
        except Exception as exc:  # noqa: BLE001
            connection.close()
            raise LedgerError(f"cannot configure the ledger connection: {type(exc).__name__}") from None
        return connection

    def connection(self) -> Any:
        """One connection held open, reopened after a drop.

        The DSN carries credentials, so no exception raised here ever includes
        it — only the exception's class name.
        """
        if self._connection is None or getattr(self._connection, "closed", False):
            self._connection = self._new_connection()
        return self._connection

    def close(self) -> None:
        if self._connection is not None and not getattr(self._connection, "closed", False):
            try:
                self._connection.close()
            except Exception:  # noqa: BLE001 - closing must not raise
                pass
        self._connection = None

    def _reset(self) -> None:
        """Drop the connection so the next call reconnects."""
        self.close()

    # -- writing --

    def append(self, unsealed: dict[str, Any]) -> dict[str, Any]:
        """Seal and insert in one transaction, serialised by an advisory lock."""
        try:
            connection = self.connection()
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_advisory_xact_lock(%s)", (APPEND_LOCK_KEY,))
                    cursor.execute(
                        "SELECT event_hash FROM spend_guard_ledger ORDER BY seq DESC LIMIT 1"
                    )
                    row = cursor.fetchone()
                    event = seal(unsealed, row[0] if row else GENESIS)
                    event_json = canonicalize(event)
                    cursor.execute(
                        """
                        INSERT INTO spend_guard_ledger
                          (event_id, event_type, schema_version, decision_id, candidate_id,
                           request_id, occurred_at, previous_event_hash, event_hash,
                           event_json, payload)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            event["event_id"],
                            event["event_type"],
                            event["schema_version"],
                            event["decision_id"],
                            event["candidate_id"],
                            event["request_id"],
                            event["occurred_at"],
                            event["previous_event_hash"],
                            event["event_hash"],
                            event_json,
                            # payload is derived from the same object that was
                            # canonicalized, so the two columns cannot disagree.
                            json.dumps(event["payload"], ensure_ascii=False),
                        ),
                    )
            return event
        except LedgerError:
            raise
        except Exception as exc:  # noqa: BLE001 - never leak the DSN
            self._reset()
            raise LedgerError(f"cannot append to the ledger database: {type(exc).__name__}") from None

    # -- reading --

    def _query(self, where: str = "", params: tuple = ()) -> Iterator[dict[str, Any]]:
        sql = f"SELECT event_json FROM spend_guard_ledger {where} ORDER BY seq ASC"
        try:
            connection = self.connection()
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                rows = cursor.fetchall()
            connection.rollback()  # end the implicit read transaction
        except Exception as exc:  # noqa: BLE001
            self._reset()
            raise LedgerError(f"cannot read the ledger database: {type(exc).__name__}") from None
        for row in rows:
            yield json.loads(row[0])

    def iter_events(self, **filters: Any) -> Iterator[dict[str, Any]]:
        event_type = filters.get("event_type")
        if event_type:
            yield from self._query("WHERE event_type = %s", (event_type,))
        else:
            yield from self._query()

    def find_by_request_id(self, request_id: str) -> list[dict[str, Any]]:
        return list(self._query("WHERE request_id = %s", (request_id,)))

    def find_by_decision_id(self, decision_id: str) -> list[dict[str, Any]]:
        return list(self._query("WHERE decision_id = %s", (decision_id,)))

    def latest_event_hash(self) -> str | None:
        try:
            connection = self.connection()
            with connection.cursor() as cursor:
                cursor.execute("SELECT event_hash FROM spend_guard_ledger ORDER BY seq DESC LIMIT 1")
                row = cursor.fetchone()
            connection.rollback()
        except Exception as exc:  # noqa: BLE001
            self._reset()
            raise LedgerError(f"cannot read the ledger database: {type(exc).__name__}") from None
        return row[0] if row else None
