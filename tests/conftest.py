"""Shared fixtures, including the two ledger backends the contract tests share."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

MIGRATIONS = ROOT / "migrations"
TEST_DSN_VAR = "SPEND_GUARD_TEST_DATABASE_URL"


def database_url() -> str | None:
    return os.environ.get(TEST_DSN_VAR)


def apply_migrations(dsn: str) -> None:
    import psycopg

    with psycopg.connect(dsn) as connection:
        for name in sorted(p.name for p in MIGRATIONS.glob("*.sql")):
            connection.execute((MIGRATIONS / name).read_text(encoding="utf-8"))
        connection.commit()


@pytest.fixture(scope="session")
def postgres_dsn() -> str:
    dsn = database_url()
    if not dsn:
        pytest.skip(f"{TEST_DSN_VAR} is not set")
    pytest.importorskip("psycopg", reason="psycopg is needed for the postgres backend")
    apply_migrations(dsn)
    return dsn


@pytest.fixture
def clean_postgres(postgres_dsn: str) -> str:
    """An empty ledger table.

    The table is append-only by trigger, so tests cannot DELETE from it. The
    trigger is dropped for the truncate and immediately restored, which also
    keeps the append-only guarantee itself under test for the rest of the run.
    """
    import psycopg

    with psycopg.connect(postgres_dsn) as connection:
        connection.execute("DROP TRIGGER IF EXISTS spend_guard_ledger_no_truncate ON spend_guard_ledger")
        connection.execute("TRUNCATE spend_guard_ledger RESTART IDENTITY")
        connection.execute(
            "CREATE TRIGGER spend_guard_ledger_no_truncate BEFORE TRUNCATE ON spend_guard_ledger "
            "FOR EACH STATEMENT EXECUTE FUNCTION spend_guard_ledger_append_only()"
        )
        connection.commit()
    return postgres_dsn


@pytest.fixture(params=["jsonl", "postgres"])
def backend(request, tmp_path):
    """Every contract test runs against both stores, from one definition."""
    if request.param == "jsonl":
        from spend_guard.backends.jsonl import JsonlLedgerBackend

        return JsonlLedgerBackend(tmp_path / "ledger.jsonl")

    dsn = database_url()
    if not dsn:
        pytest.skip(f"{TEST_DSN_VAR} is not set")
    pytest.importorskip("psycopg")
    request.getfixturevalue("clean_postgres")
    from spend_guard.backends.postgres import PostgresLedgerBackend

    created = PostgresLedgerBackend(dsn)
    request.addfinalizer(created.close)
    return created
