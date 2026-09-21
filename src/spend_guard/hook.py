"""The seam the existing purchase flow calls (chapter 6).

Shadow mode must not change whether a payment happens, how long the payment
path takes, or how it fails. Those three are enforced here, not left to the
caller's discipline:

* **Whether.** `observe` returns None and has no return value the caller could
  branch on. There is deliberately no API that hands a verdict back to the
  payment path.
* **How long.** In the default asynchronous mode the payment path pays only
  for normalising the candidate and queueing it. Checks, the Jev call and the
  ledger append happen on a worker thread. The synchronous mode exists for
  hosts that cannot run one, and is bounded by `hook_timeout_ms`.
* **How it fails.** Every exception raised inside the guard is caught at this
  boundary. A guard that cannot record still lets the purchase proceed, and
  the loss is counted in `dropped` for the operational log.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable

from .engine import ShadowEngine
from .models import Decision

FallbackLogger = Callable[[str, dict[str, Any]], None]


class ShadowGuard:
    """Fail-open wrapper around `ShadowEngine`. Nothing it does can raise."""

    def __init__(
        self,
        engine: ShadowEngine,
        *,
        mode: str = "async",
        hook_timeout_ms: int | None = None,
        queue_size: int = 1024,
        log: FallbackLogger | None = None,
    ):
        if mode not in ("async", "sync"):
            raise ValueError("mode must be 'async' or 'sync'")
        self.engine = engine
        self.mode = mode
        self.hook_timeout_ms = hook_timeout_ms
        self.log = log or (lambda event, fields: None)
        self.counters = {"observed": 0, "evaluated": 0, "dropped": 0, "timed_out": 0}
        self.overheads_ms: list[float] = []
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=queue_size)
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # -- lifecycle --

    def start(self) -> "ShadowGuard":
        if self.mode == "async" and self._worker is None:
            self._worker = threading.Thread(
                target=self._drain, name="spend-guard", daemon=True
            )
            self._worker.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        """Stop the worker after the queue drains. Safe to call more than once."""
        if self._worker is None:
            return
        self._queue.join()
        self._stop.set()
        self._queue.put(None)
        self._worker.join(timeout=timeout)
        self._worker = None

    def __enter__(self) -> "ShadowGuard":
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.stop()

    # -- the payment path calls this and nothing else --

    def observe(self, raw: Any) -> None:
        """Record a purchase candidate. Returns nothing, raises nothing.

        The caller proceeds to its existing purchase logic immediately
        afterwards, unchanged.
        """
        started = time.perf_counter()
        try:
            observed = self.engine.observe(raw)
            with self._lock:
                self.counters["observed"] += 1
            if observed["duplicate_of"]:
                return
            if self.mode == "async":
                self._enqueue(observed)
            else:
                self._evaluate_sync(observed, started)
        except Exception as error:  # noqa: BLE001 - the payment path must not see this
            self._drop("observe_failed", error)
        finally:
            self._record_overhead(started)

    def _enqueue(self, observed: dict[str, Any]) -> None:
        try:
            self._queue.put_nowait(observed)
        except queue.Full:
            # Back pressure must not become latency on the payment path.
            self._drop("queue_full", None)

    def _evaluate_sync(self, observed: dict[str, Any], started: float) -> None:
        """Synchronous fallback, bounded by `hook_timeout_ms`.

        The budget is checked before the evaluation and enforced afterwards; a
        Jev call that overruns is recorded as a TIMEOUT review rather than
        being allowed to hold the payment path open indefinitely. Keeping the
        per-request Jev timeout below `hook_timeout_ms` is what actually bounds
        the wait, which is why the design doc asks for both to be set together.
        """
        budget = self.hook_timeout_ms
        if budget is not None and (time.perf_counter() - started) * 1000 >= budget:
            self._timeout(observed)
            return
        try:
            self._run(observed)
        except Exception as error:  # noqa: BLE001
            self._drop("evaluate_failed", error)
            return
        if budget is not None and (time.perf_counter() - started) * 1000 > budget:
            with self._lock:
                self.counters["timed_out"] += 1
            self.log("spend_guard.hook_over_budget", {"budget_ms": budget})

    def _timeout(self, observed: dict[str, Any]) -> None:
        with self._lock:
            self.counters["timed_out"] += 1
        self.log("spend_guard.hook_timeout", {"decision_id": observed.get("decision_id")})

    # -- worker --

    def _drain(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is None:
                self._queue.task_done()
                break
            try:
                self._run(item)
            except Exception as error:  # noqa: BLE001 - one bad candidate must not kill the worker
                self._drop("evaluate_failed", error)
            finally:
                self._queue.task_done()

    def _run(self, observed: dict[str, Any]) -> Decision:
        decision = self.engine.evaluate(
            observed["candidate"],
            decision_id=observed["decision_id"],
            digest=observed["input_hash"],
        )
        with self._lock:
            self.counters["evaluated"] += 1
        return decision

    # -- bookkeeping --

    def _drop(self, reason: str, error: Exception | None) -> None:
        """Count and log a loss. Only the class of failure is logged, not payload data."""
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
        """p50/p95 of the time the guard added to the payment path (chapter 14)."""
        with self._lock:
            samples = sorted(self.overheads_ms)
        return {"p50": _percentile(samples, 0.50), "p95": _percentile(samples, 0.95)}


def _percentile(samples: list[float], fraction: float) -> float | None:
    if not samples:
        return None
    index = min(len(samples) - 1, int(round(fraction * (len(samples) - 1))))
    return round(samples[index], 3)
