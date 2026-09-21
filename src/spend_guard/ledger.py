"""The ledger: an append-only event log over a pluggable backend.

Records are never updated in place. A later fact about the same decision is a
new event carrying the same `decision_id`, so the file stays a history rather
than a current-state table, and `guard replay` can re-derive any decision from
it without calling Jev.

`Ledger` is a thin facade. All storage, locking and hashing live in the
backend (`spend_guard.backends`), which is what lets the same contract tests
run against JSONL and Postgres.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterator

from .backends import (
    EVENT_TYPES,
    GENESIS,
    JsonlLedgerBackend,
    LedgerBackend,
    prepare_event,
    verify_chain,
)

__all__ = [
    "EVENT_TYPES",
    "GENESIS",
    "Ledger",
    "LedgerIndex",
    "redact",
    "verify_chain",
    "REDACTED",
]

# Keys whose values must never reach the ledger, matched case-insensitively
# anywhere in the key. A payload is scrubbed on the way in, so a caller that
# hands over a raw provider response cannot turn the ledger into a secret store.
_SECRET_KEY = re.compile(
    r"authorization|api[-_]?key|secret|token|password|passphrase|private[-_]?key|"
    r"signature|signed|mnemonic|seed[-_]?phrase|cookie|credential|bearer|wallet[-_]?key|"
    r"dsn|database[-_]?url|conn(ection)?[-_]?string",
    re.IGNORECASE,
)
REDACTED = "[redacted]"


def redact(value: Any, *, depth: int = 0) -> Any:
    """Drop secret-looking fields. Structure is kept so the record stays readable."""
    if depth > 12:
        return REDACTED
    if isinstance(value, dict):
        return {
            key: (REDACTED if _SECRET_KEY.search(str(key)) else redact(item, depth=depth + 1))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, depth=depth + 1) for item in value]
    return value


class Ledger:
    """Convenience API over a `LedgerBackend`."""

    def __init__(self, path: str | Path | None = None, *, backend: LedgerBackend | None = None):
        if backend is None:
            if path is None:
                raise ValueError("Ledger needs either a path or a backend")
            backend = JsonlLedgerBackend(path)
        self.backend = backend

    @property
    def path(self) -> Path | None:
        """The file a JSONL-backed ledger lives in, else None."""
        return getattr(self.backend, "path", None)

    @path.setter
    def path(self, value: str | Path) -> None:
        self.backend = JsonlLedgerBackend(value)

    def append(self, event_type: str, **kwargs: Any) -> dict[str, Any]:
        """Build an unsealed event and hand it to the backend to seal and store."""
        return self.backend.append(prepare_event(event_type, **kwargs))

    def append_prepared(self, unsealed: dict[str, Any]) -> dict[str, Any]:
        """Append an event already built by `prepare_event` (used by the writer thread)."""
        return self.backend.append(unsealed)

    def iter_events(self, **filters: Any) -> Iterator[dict[str, Any]]:
        return self.backend.iter_events(**filters)

    def read(self) -> list[dict[str, Any]]:
        return list(self.iter_events())

    def find_by_request_id(self, request_id: str) -> list[dict[str, Any]]:
        return self.backend.find_by_request_id(request_id)

    def find_by_decision_id(self, decision_id: str) -> list[dict[str, Any]]:
        return self.backend.find_by_decision_id(decision_id)

    def latest_event_hash(self) -> str | None:
        return self.backend.latest_event_hash()

    def verify(self) -> list[str]:
        return verify_chain(self.read())


class LedgerIndex:
    """The lookups chapter 9 needs, built from one pass over the ledger.

    Held in memory so the payment path never queries storage to decide whether
    a candidate is a repeat. The full scan on construction is the known scaling
    limit; see docs/design.md.
    """

    def __init__(self, ledger: Ledger | None = None):
        self.first_by_request: dict[str, dict[str, Any]] = {}
        self.evaluated: dict[tuple[str, str], str] = {}
        if ledger is not None:
            for event in ledger.iter_events():
                self.observe_event(event)

    def observe_event(self, event: dict[str, Any]) -> None:
        payload = event.get("payload") or {}
        request_id = event.get("request_id")
        if event.get("event_type") == "candidate_observed" and isinstance(request_id, str):
            candidate = payload.get("candidate") or {}
            self.first_by_request.setdefault(
                request_id,
                {
                    "decision_id": event.get("decision_id"),
                    "candidate_id": event.get("candidate_id"),
                    "idempotency_key": candidate.get("idempotency_key"),
                    "input_hash": payload.get("input_hash"),
                    "occurred_at": event.get("occurred_at"),
                },
            )
            input_hash = payload.get("input_hash")
            if isinstance(input_hash, str) and not payload.get("duplicate_of"):
                self.evaluated.setdefault((request_id, input_hash), str(event.get("decision_id")))

    def observe_pending(self, request_id: str | None, input_hash: str, record: dict[str, Any]) -> None:
        """Register a candidate before it is written.

        The payment path must not wait for storage, so a candidate is indexed
        as soon as it is observed. Without this, a burst of identical
        candidates would all miss the repeat check while still queued.
        """
        if not isinstance(request_id, str):
            return
        self.first_by_request.setdefault(request_id, record)
        self.evaluated.setdefault((request_id, input_hash), str(record.get("decision_id")))

    def prior_request(
        self, request_id: str, *, exclude_decision_id: str | None = None
    ) -> dict[str, Any] | None:
        record = self.first_by_request.get(request_id)
        if record is None:
            return None
        if exclude_decision_id is not None and record.get("decision_id") == exclude_decision_id:
            return None  # the candidate's own observation, not an earlier purchase
        return record

    def evaluated_decision(self, request_id: str | None, input_hash: str) -> str | None:
        if request_id is None:
            return None
        return self.evaluated.get((request_id, input_hash))
