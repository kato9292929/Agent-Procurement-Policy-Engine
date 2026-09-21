"""Append-only JSONL ledger with a hash chain (chapter 9).

Records are never updated in place. A later fact about the same decision is a
new event carrying the same `decision_id`, so the file stays a history rather
than a current-state table, and `guard replay` can re-derive any decision from
it without calling Jev.

Concurrency: `event_hash` links each event to the one before it, so two
processes appending at once would both claim the same predecessor and fork the
chain. Every append therefore takes an exclusive `flock` covering both reading
the current head and writing the new line. That serialises writers on one host.
A multi-host deployment needs a single writer instead; see docs/design.md.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
from pathlib import Path
from typing import Any, Iterator

from .canonical import hash_json
from .errors import LedgerError
from .models import SCHEMA_VERSION
from .normalize import new_id, utc_now

EVENT_TYPES = (
    "candidate_observed",
    "guard_evaluated",
    "purchase_attempted",
    "purchase_result",
    "delivery_observed",
    "human_feedback",
)

GENESIS = "sha256:" + "0" * 64

# Keys whose values must never reach the ledger, matched case-insensitively
# anywhere in the key. A payload is scrubbed on the way in, so a caller that
# hands over a raw provider response cannot turn the ledger into a secret store.
_SECRET_KEY = re.compile(
    r"authorization|api[-_]?key|secret|token|password|passphrase|private[-_]?key|"
    r"signature|signed|mnemonic|seed[-_]?phrase|cookie|credential|bearer|wallet[-_]?key",
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


def build_event(
    event_type: str,
    *,
    decision_id: str,
    candidate_id: str,
    request_id: str | None,
    payload: dict[str, Any],
    previous_event_hash: str,
    occurred_at: str | None = None,
) -> dict[str, Any]:
    """Build one event and seal it. `event_hash` covers everything except itself."""
    if event_type not in EVENT_TYPES:
        raise LedgerError(f"unknown event_type {event_type!r}")
    event = {
        "event_id": new_id("evt"),
        "event_type": event_type,
        "schema_version": SCHEMA_VERSION,
        "decision_id": decision_id,
        "candidate_id": candidate_id,
        "request_id": request_id,
        "occurred_at": occurred_at or utc_now(),
        "payload": redact(payload),
        "previous_event_hash": previous_event_hash,
    }
    event["event_hash"] = hash_json(event)
    return event


def verify_chain(events: list[dict[str, Any]]) -> list[str]:
    """Return a problem per broken link. An empty list means the chain is intact."""
    problems: list[str] = []
    expected = GENESIS
    for index, event in enumerate(events):
        if event.get("previous_event_hash") != expected:
            problems.append(f"event {index} ({event.get('event_id')}): previous_event_hash mismatch")
        sealed = {k: v for k, v in event.items() if k != "event_hash"}
        if hash_json(sealed) != event.get("event_hash"):
            problems.append(f"event {index} ({event.get('event_id')}): event_hash mismatch")
        expected = event.get("event_hash") or expected
    return problems


class Ledger:
    """A JSONL file of events. Append-only; nothing here rewrites a line."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def _open(self) -> Any:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Owner-only: a ledger records what was bought and from whom.
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            return os.fdopen(fd, "r+b")
        except OSError as exc:
            raise LedgerError(f"cannot open ledger {self.path}: {exc}") from exc

    @staticmethod
    def _last_hash(handle: Any) -> str:
        """Read the head of the chain by seeking back from the end, not by scanning."""
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        if size == 0:
            return GENESIS
        block = 4096
        buffer = b""
        position = size
        while position > 0:
            step = min(block, position)
            position -= step
            handle.seek(position)
            buffer = handle.read(step) + buffer
            lines = [line for line in buffer.split(b"\n") if line.strip()]
            # Two newlines prove the last line is whole: with fewer, the block
            # may have cut the final record in half.
            if lines and (position == 0 or buffer.count(b"\n") >= 2):
                try:
                    last = json.loads(lines[-1].decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    raise LedgerError(f"ledger tail is not valid JSON: {exc}") from exc
                head = last.get("event_hash")
                if not isinstance(head, str):
                    raise LedgerError("ledger tail has no event_hash")
                return head
        return GENESIS

    def append(self, event_type: str, **kwargs: Any) -> dict[str, Any]:
        """Seal and append one event, returning it.

        The lock spans reading the head and writing the line, so concurrent
        appenders produce one chain rather than a fork.
        """
        try:
            with self._open() as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    previous = self._last_hash(handle)
                    event = build_event(event_type, previous_event_hash=previous, **kwargs)
                    line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
                    handle.seek(0, os.SEEK_END)
                    handle.write(line.encode("utf-8"))
                    handle.flush()
                    os.fsync(handle.fileno())
                    return event
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except LedgerError:
            raise
        except OSError as exc:
            raise LedgerError(f"cannot append to ledger {self.path}: {exc}") from exc

    def read(self) -> list[dict[str, Any]]:
        return list(self.iter_events())

    def iter_events(self) -> Iterator[dict[str, Any]]:
        """Yield events oldest first. A missing ledger reads as empty."""
        try:
            handle = self.path.open("r", encoding="utf-8")
        except FileNotFoundError:
            return
        except OSError as exc:
            raise LedgerError(f"cannot read ledger {self.path}: {exc}") from exc
        with handle:
            for number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise LedgerError(f"{self.path}:{number} is not valid JSON: {exc}") from exc
                if isinstance(event, dict):
                    yield event


class LedgerIndex:
    """The lookups chapter 9 needs, built from one pass over the ledger.

    Holding the whole index in memory is fine at MVP volume; the scan is the
    known scaling limit and is recorded as a TODO in docs/design.md.
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
