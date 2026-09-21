"""The ledger storage seam.

Everything that reads or writes the ledger goes through `LedgerBackend`, so
the JSONL file and the Postgres table behave identically and the same contract
tests run against both.

The backend owns hashing. A caller builds an *unsealed* event with
`prepare_event` and hands it over; the backend decides `previous_event_hash`
and `event_hash` while holding whatever lock makes its appends serial. Doing
it the other way round would let two writers seal against the same
predecessor and fork the chain.
"""

from __future__ import annotations

from typing import Any, Iterator, Protocol

from ..canonical import hash_json
from ..errors import LedgerError
from ..models import SCHEMA_VERSION
from ..normalize import new_id, utc_now

EVENT_TYPES = (
    "candidate_observed",
    "guard_evaluated",
    "purchase_attempted",
    "purchase_result",
    "delivery_observed",
    "human_feedback",
)

GENESIS = "sha256:" + "0" * 64

HASH_FIELDS = ("previous_event_hash", "event_hash")


def prepare_event(
    event_type: str,
    *,
    decision_id: str,
    candidate_id: str,
    request_id: str | None,
    payload: dict[str, Any],
    occurred_at: str | None = None,
    event_id: str | None = None,
) -> dict[str, Any]:
    """Build an event with everything except the two hashes.

    Redaction happens here, at the point the payload enters the ledger layer,
    so it applies to every backend rather than to whichever one remembered.
    """
    from ..ledger import redact

    if event_type not in EVENT_TYPES:
        raise LedgerError(f"unknown event_type {event_type!r}")
    return {
        "event_id": event_id or new_id("evt"),
        "event_type": event_type,
        "schema_version": SCHEMA_VERSION,
        "decision_id": decision_id,
        "candidate_id": candidate_id,
        "request_id": request_id,
        "occurred_at": occurred_at or utc_now(),
        "payload": redact(payload),
    }


def seal(unsealed: dict[str, Any], previous_event_hash: str) -> dict[str, Any]:
    """Attach the chain links. `event_hash` covers everything but itself."""
    event = {k: v for k, v in unsealed.items() if k not in HASH_FIELDS}
    event["previous_event_hash"] = previous_event_hash
    event["event_hash"] = hash_json(event)
    return event


def verify_chain(events: list[dict[str, Any]]) -> list[str]:
    """Return one problem per broken link; empty means the chain is intact."""
    problems: list[str] = []
    expected = GENESIS
    for index, event in enumerate(events):
        if event.get("previous_event_hash") != expected:
            problems.append(f"event {index} ({event.get('event_id')}): previous_event_hash mismatch")
        body = {k: v for k, v in event.items() if k != "event_hash"}
        if hash_json(body) != event.get("event_hash"):
            problems.append(f"event {index} ({event.get('event_id')}): event_hash mismatch")
        expected = event.get("event_hash") or expected
    return problems


class LedgerBackend(Protocol):
    """Append-only event storage. Nothing here updates or deletes."""

    def append(self, unsealed: dict[str, Any]) -> dict[str, Any]:
        """Seal and store one event, returning the sealed event."""

    def iter_events(self, **filters: Any) -> Iterator[dict[str, Any]]:
        """Yield events in append order."""

    def find_by_request_id(self, request_id: str) -> list[dict[str, Any]]: ...

    def find_by_decision_id(self, decision_id: str) -> list[dict[str, Any]]: ...

    def latest_event_hash(self) -> str | None:
        """Head of the chain, or None when the ledger is empty."""
