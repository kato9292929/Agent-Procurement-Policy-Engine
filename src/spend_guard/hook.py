"""The seam the existing purchase flow calls (chapter 6).

Shadow mode must not change whether a payment happens, how long the payment
path takes, or how it fails. Those three are enforced here, not left to the
caller's discipline:

* **Whether.** `observe` returns None and has no return value the caller could
  branch on. There is deliberately no API that hands a verdict back.
* **How long.** `observe` normalises the candidate and hands it to a bounded
  in-memory queue. It takes no file lock, opens no connection and performs no
  I/O, so nothing another process holds can stall the payment path.
* **How it fails.** Every exception raised inside the guard is caught at this
  boundary. A guard that cannot record still lets the purchase proceed, and
  the loss is counted for the operational log.

Three threads, one direction of travel::

    payment path --> _pending --> writer --> _evaluating --> evaluator
                                    ^                            |
                                    +--------- _writes ----------+

The **writer is the only thread that touches storage**, so all appends in a
process are serial by construction and the ledger lock is never contended
from within. Evaluation runs on its own thread so a slow Jev call cannot hold
up recording the next candidate.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable

from .engine import ShadowEngine

FallbackLogger = Callable[[str, dict[str, Any]], None]

_STOP = object()


class ShadowGuard:
    """Fail-open wrapper around `ShadowEngine`. Nothing it does can raise."""

    def __init__(
        self,
        engine: ShadowEngine,
        *,
        mode: str = "async",
        hook_timeout_ms: int | None = None,
        max_pending: int | None = None,
        shutdown_drain_ms: int | None = None,
        queue_size: int | None = None,
        log: FallbackLogger | None = None,
    ):
        if mode not in ("async", "sync"):
            raise ValueError("mode must be 'async' or 'sync'")
        settings = dict(engine.policy.hook or {})
        self.engine = engine
        self.mode = mode
        self.hook_timeout_ms = (
            hook_timeout_ms if hook_timeout_ms is not None else settings.get("hook_timeout_ms")
        )
        limit = max_pending or queue_size or settings.get("max_pending") or 1024
        self.max_pending = int(limit)
        self.shutdown_drain_ms = int(
            shutdown_drain_ms
            if shutdown_drain_ms is not None
            else settings.get("shutdown_drain_ms", 5000)
        )
        self.log = log or (lambda event, fields: None)

        self.counters = {
            "observed": 0,
            "written": 0,
            "evaluated": 0,
            "dropped": 0,
            "timed_out": 0,
            "unflushed_at_shutdown": 0,
            "write_failures": 0,
        }
        self.overheads_ms: list[float] = []

        # The writer queue is unbounded and back pressure is applied by
        # `_inflight` instead. A bounded queue would also refuse the decisions
        # the evaluator feeds back, which must never be dropped once the
        # observation they belong to has been written.
        self._writes: queue.Queue[Any] = queue.Queue()
        self._evaluating: queue.Queue[Any] = queue.Queue()
        self._inflight = 0
        self._lock = threading.Lock()
        self._idle = threading.Condition(self._lock)
        self._writer: threading.Thread | None = None
        self._evaluator: threading.Thread | None = None

    # -- lifecycle --

    def start(self) -> "ShadowGuard":
        if self.mode == "async" and self._writer is None:
            self._writer = threading.Thread(target=self._write_loop, name="spend-guard-writer", daemon=True)
            self._evaluator = threading.Thread(
                target=self._evaluate_loop, name="spend-guard-judge", daemon=True
            )
            self._writer.start()
            self._evaluator.start()
        return self

    def stop(self, timeout: float | None = None) -> dict[str, int]:
        """Drain within `shutdown_drain_ms`, then report what did not make it.

        Anything still queued when the budget runs out is counted and logged
        rather than silently lost: a shadow ledger with unexplained gaps would
        skew every metric computed from it.
        """
        if self._writer is None:
            return dict(self.counters)
        budget = (timeout * 1000) if timeout is not None else self.shutdown_drain_ms
        deadline = time.monotonic() + budget / 1000.0

        # An empty queue does not mean the work is finished: the writer may
        # have taken the last item and still be appending it. Wait on the
        # in-flight count, which only falls once an item is fully handled.
        with self._idle:
            while self._inflight > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._idle.wait(timeout=min(remaining, 0.05))
            unflushed = self._inflight

        self._writes.put(_STOP)
        self._evaluating.put(_STOP)
        remaining = max(0.0, deadline - time.monotonic()) + 0.5
        self._writer.join(timeout=remaining)
        if self._evaluator:
            self._evaluator.join(timeout=remaining)
        self._writer = None
        self._evaluator = None

        if unflushed:
            with self._lock:
                self.counters["unflushed_at_shutdown"] += unflushed
            self.log(
                "spend_guard.unflushed_at_shutdown",
                {"count": unflushed, "budget_ms": budget},
            )
        return dict(self.counters)

    def __enter__(self) -> "ShadowGuard":
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.stop()

    # -- the payment path calls this and nothing else --

    def observe(self, raw: Any) -> None:
        """Record a purchase candidate. Returns nothing, raises nothing.

        No lock, no file handle, no socket: only normalisation and a
        non-blocking enqueue, so a ledger held open by another process cannot
        slow the payment path down.
        """
        started = time.perf_counter()
        try:
            observed = self.engine.prepare(raw)
            with self._lock:
                self.counters["observed"] += 1
            if observed["duplicate_of"]:
                self._offer(observed, write_only=True)
                return
            if self.mode == "async":
                self._offer(observed)
            else:
                self._evaluate_sync(observed, started)
        except Exception as error:  # noqa: BLE001 - the payment path must not see this
            self._drop("prepare_failed", error)
        finally:
            self._record_overhead(started)

    def _offer(self, observed: dict[str, Any], *, write_only: bool = False) -> None:
        if self.mode != "async":
            self._safely(lambda: self.engine.record_observation(observed), "write_failed")
            return
        with self._lock:
            if self._inflight >= self.max_pending:
                over_limit = True
            else:
                self._inflight += 1
                over_limit = False
        if over_limit:
            # Back pressure must never become latency on the payment path.
            self._drop("queue_full", None)
            return
        self._writes.put(("observe", observed, write_only))

    # -- writer: the only thread that touches storage --

    def _write_loop(self) -> None:
        while True:
            item = self._writes.get()
            if item is _STOP:
                break
            kind = item[0]
            if kind == "observe":
                _, observed, write_only = item
                written = self._safely(
                    lambda: self.engine.record_observation(observed), "write_failed"
                )
                if written:
                    with self._lock:
                        self.counters["written"] += 1
                if written and not write_only:
                    self._evaluating.put(observed)  # still in flight
                else:
                    self._finished()
            elif kind == "decision":
                decision = item[1]
                if self._safely(lambda: self.engine.record_decision(decision), "write_failed"):
                    with self._lock:
                        self.counters["evaluated"] += 1
                self._finished()

    # -- evaluator: checks and Jev, never storage --

    def _evaluate_loop(self) -> None:
        while True:
            observed = self._evaluating.get()
            if observed is _STOP:
                break
            try:
                decision = self.engine.decide(observed)
            except Exception as error:  # noqa: BLE001 - one bad candidate must not kill the thread
                self._drop("evaluate_failed", error)
                self._finished()
                continue
            self._writes.put(("decision", decision))

    # -- synchronous fallback --

    def _evaluate_sync(self, observed: dict[str, Any], started: float) -> None:
        """For hosts that cannot run a thread. Bounded by `hook_timeout_ms`.

        Keep `jev.timeout_ms` below `hook_timeout_ms`: the per-request timeout
        is what actually bounds the wait, while the budget check only records
        an overrun after the fact.
        """
        budget = self.hook_timeout_ms
        if budget is not None and (time.perf_counter() - started) * 1000 >= budget:
            self._timeout(observed)
            return
        self._safely(lambda: self.engine.record_observation(observed), "write_failed")
        try:
            decision = self.engine.decide(observed)
        except Exception as error:  # noqa: BLE001
            self._drop("evaluate_failed", error)
            return
        if self._safely(lambda: self.engine.record_decision(decision), "write_failed"):
            with self._lock:
                self.counters["evaluated"] += 1
        if budget is not None and (time.perf_counter() - started) * 1000 > budget:
            with self._lock:
                self.counters["timed_out"] += 1
            self.log("spend_guard.hook_over_budget", {"budget_ms": budget})

    def _timeout(self, observed: dict[str, Any]) -> None:
        with self._lock:
            self.counters["timed_out"] += 1
        self.log("spend_guard.hook_timeout", {"decision_id": observed.get("decision_id")})

    # -- bookkeeping --

    def _finished(self) -> None:
        """One queued candidate has reached its end state, however it ended."""
        with self._idle:
            self._inflight = max(0, self._inflight - 1)
            if self._inflight == 0:
                self._idle.notify_all()

    def _safely(self, action: Callable[[], Any], reason: str) -> bool:
        try:
            action()
            return True
        except Exception as error:  # noqa: BLE001
            with self._lock:
                self.counters["write_failures"] += 1
            self._drop(reason, error)
            return False

    def _drop(self, reason: str, error: Exception | None) -> None:
        """Count and log a loss. Only the class of failure is logged, never payload data."""
        with self._lock:
            self.counters["dropped"] += 1
            count = self.counters["dropped"]
        self.log(
            "spend_guard.dropped",
            {"reason": reason, "error": type(error).__name__ if error else None, "count": count},
        )

    def _record_overhead(self, started: float) -> None:
        elapsed = (time.perf_counter() - started) * 1000
        with self._lock:
            self.overheads_ms.append(elapsed)

    def overhead_percentiles(self) -> dict[str, float | None]:
        """p50/p95/p99/max of the time the guard added to the payment path."""
        with self._lock:
            samples = sorted(self.overheads_ms)
        return {
            "p50": _percentile(samples, 0.50),
            "p95": _percentile(samples, 0.95),
            "p99": _percentile(samples, 0.99),
            "max": round(samples[-1], 3) if samples else None,
        }


def _percentile(samples: list[float], fraction: float) -> float | None:
    if not samples:
        return None
    index = min(len(samples) - 1, int(round(fraction * (len(samples) - 1))))
    return round(samples[index], 3)
