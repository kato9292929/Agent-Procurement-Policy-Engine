"""JSONL ledger backend: one event per line, for local development and tests.

Appends take an exclusive `flock` spanning both reading the current head and
writing the new line, so concurrent processes produce one chain rather than a
fork. The head is found by seeking backwards from the end rather than by
scanning, so an append stays O(1) in the ledger's length.

`flock` is per host. A deployment spread over machines needs the Postgres
backend, whose advisory lock covers every connection.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from typing import Any, Iterator

from ..errors import LedgerError
from .base import GENESIS, seal


class JsonlLedgerBackend:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    # -- writing --

    def _open(self) -> Any:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # Owner-only: a ledger records what was bought and from whom.
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            return os.fdopen(fd, "r+b")
        except OSError as exc:
            raise LedgerError(f"cannot open ledger {self.path}: {exc}") from exc

    @staticmethod
    def _head(handle: Any) -> str:
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

    def append(self, unsealed: dict[str, Any]) -> dict[str, Any]:
        try:
            with self._open() as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    event = seal(unsealed, self._head(handle))
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

    # -- reading --

    def iter_events(self, **filters: Any) -> Iterator[dict[str, Any]]:
        event_type = filters.get("event_type")
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
                if not isinstance(event, dict):
                    continue
                if event_type and event.get("event_type") != event_type:
                    continue
                yield event

    def find_by_request_id(self, request_id: str) -> list[dict[str, Any]]:
        return [e for e in self.iter_events() if e.get("request_id") == request_id]

    def find_by_decision_id(self, decision_id: str) -> list[dict[str, Any]]:
        return [e for e in self.iter_events() if e.get("decision_id") == decision_id]

    def latest_event_hash(self) -> str | None:
        if not self.path.exists():
            return None
        with self._open() as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            try:
                head = self._head(handle)
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return None if head == GENESIS else head
