"""Ledger storage backends."""

from .base import (
    EVENT_TYPES,
    GENESIS,
    LedgerBackend,
    prepare_event,
    seal,
    verify_chain,
)
from .jsonl import JsonlLedgerBackend

__all__ = [
    "EVENT_TYPES",
    "GENESIS",
    "LedgerBackend",
    "JsonlLedgerBackend",
    "prepare_event",
    "seal",
    "verify_chain",
    "build_backend",
]


def build_backend(config: dict | None = None, *, path: str | None = None):
    """Create the backend named by `ledger.backend`.

    Postgres is imported lazily, so a JSONL-only install never needs psycopg.
    """
    import os

    config = dict(config or {})
    kind = str(config.get("backend", "jsonl")).lower()
    if kind == "jsonl":
        return JsonlLedgerBackend(path or config.get("path") or "./var/spend-guard/ledger.jsonl")
    if kind == "postgres":
        from ..errors import LedgerError
        from .postgres import PostgresLedgerBackend

        variable = config.get("dsn_env") or "SPEND_GUARD_DATABASE_URL"
        dsn = os.environ.get(variable)
        if not dsn:
            raise LedgerError(f"{variable} is not set, so the postgres ledger cannot be opened")
        return PostgresLedgerBackend(
            dsn,
            lock_timeout_ms=int(config.get("lock_timeout_ms", 2000)),
            statement_timeout_ms=int(config.get("statement_timeout_ms", 2000)),
        )
    from ..errors import InputError

    raise InputError(f"unknown ledger backend {kind!r}; expected 'jsonl' or 'postgres'")
