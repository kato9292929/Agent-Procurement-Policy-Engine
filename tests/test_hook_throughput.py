"""B-5: the payment path stays fast whatever storage is doing.

The MVP's `observe()` appended inline, so it inherited every stall the ledger
file could suffer. It now only normalises and enqueues, and these tests hold
that property in place.
"""

from __future__ import annotations

import subprocess
import sys
import time

import support
from spend_guard.hook import ShadowGuard
from spend_guard.models import SemanticJudgment

# Holds an exclusive flock on the ledger and does nothing, so any code that
# needs the lock on the payment path would block for the full duration.
LOCK_HOLDER = """
import fcntl, os, sys, time
path, seconds = sys.argv[1], float(sys.argv[2])
fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
print("locked", flush=True)
time.sleep(seconds)
"""


class SlowJudge:
    def __init__(self, delay: float = 0.0):
        self.delay = delay
        self.calls = 0

    def evaluate(self, candidate, policy) -> SemanticJudgment:
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        return SemanticJudgment(
            status="OK",
            model="slow",
            scores={"task_fit": 0.9, "incremental_value": 0.9,
                    "duplication_risk": 0.1, "evidence_sufficiency": 0.9},
            confidence={k: 0.9 for k in ("task_fit", "incremental_value",
                                         "duplication_risk", "evidence_sufficiency")},
        )


def candidates(n: int):
    names = list(support.candidates())
    return [support.candidate(names[i % len(names)], candidate_id=f"c{i}", request_id=f"r{i}")
            for i in range(n)]


def percentiles(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)

    def pick(fraction: float) -> float:
        return ordered[min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))]

    return {"p50": pick(0.50), "p99": pick(0.99), "max": ordered[-1]}


def test_observe_returns_immediately_while_another_process_holds_the_lock(tmp_path):
    """The decisive test: a held lock used to be a stall on the payment path."""
    engine = support.engine(tmp_path)
    path = engine.ledger.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()

    holder = subprocess.Popen(
        [sys.executable, "-c", LOCK_HOLDER, str(path), "3"], stdout=subprocess.PIPE, text=True
    )
    assert holder.stdout.readline().strip() == "locked"
    try:
        guard = ShadowGuard(engine, mode="async").start()
        samples = []
        for raw in candidates(25):
            started = time.perf_counter()
            guard.observe(raw)
            samples.append((time.perf_counter() - started) * 1000)
        stats = percentiles(samples)
        assert stats["max"] < 50, f"observe() waited on the lock: {stats}"
    finally:
        holder.kill()
        holder.wait()
        guard.stop()


def test_observe_stays_fast_under_six_competing_processes(tmp_path):
    engine = support.engine(tmp_path, the_judge=SlowJudge())
    path = engine.ledger.path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()

    holders = [
        subprocess.Popen([sys.executable, "-c", LOCK_HOLDER, str(path), "4"],
                         stdout=subprocess.PIPE, text=True)
        for _ in range(6)
    ]
    try:
        guard = ShadowGuard(engine, mode="async").start()
        samples = []
        for raw in candidates(200):
            started = time.perf_counter()
            guard.observe(raw)
            samples.append((time.perf_counter() - started) * 1000)
        stats = percentiles(samples)
        print(f"\n[B-5] observe() under 6-process contention: {stats}")
        assert stats["p50"] < 5, f"p50 regressed: {stats}"
        assert stats["p99"] < 25, f"p99 regressed: {stats}"
    finally:
        for holder in holders:
            holder.kill()
            holder.wait()
        guard.stop(timeout=5)


def test_a_slow_judge_never_reaches_the_payment_path(tmp_path):
    """MVP conditions: 250ms judge, 200 candidates."""
    judge = SlowJudge(delay=0.25)
    guard = ShadowGuard(support.engine(tmp_path, the_judge=judge), mode="async").start()
    samples = []
    for raw in candidates(200):
        started = time.perf_counter()
        guard.observe(raw)
        samples.append((time.perf_counter() - started) * 1000)
    stats = percentiles(samples)
    print(f"\n[B-5] observe() with a 250ms judge: {stats}")
    assert stats["p50"] < 2
    assert stats["max"] < 50
    guard.stop(timeout=1)


# -- back pressure ---------------------------------------------------------


def test_a_full_queue_drops_instead_of_blocking(tmp_path):
    guard = ShadowGuard(support.engine(tmp_path, the_judge=SlowJudge(0.5)), mode="async",
                        max_pending=3)
    # No threads started, so nothing drains and the limit is reached at once.
    samples = []
    for raw in candidates(20):
        started = time.perf_counter()
        guard.observe(raw)
        samples.append((time.perf_counter() - started) * 1000)
    assert guard.counters["dropped"] == 17, guard.counters
    assert max(samples) < 50, "a full queue blocked the payment path"


def test_dropping_is_counted_and_logged_without_payload_data(tmp_path):
    logged: list[tuple[str, dict]] = []
    guard = ShadowGuard(support.engine(tmp_path), mode="async", max_pending=1,
                        log=lambda e, f: logged.append((e, f)))
    for raw in candidates(4):
        guard.observe(raw)
    drops = [entry for entry in logged if entry[0] == "spend_guard.dropped"]
    assert drops, "a drop was not logged"
    assert set(drops[0][1]) == {"reason", "error", "count"}
    assert drops[0][1]["reason"] == "queue_full"


# -- shutdown --------------------------------------------------------------


def test_shutdown_drains_what_it_can_within_the_budget(tmp_path):
    engine = support.engine(tmp_path, the_judge=SlowJudge())
    guard = ShadowGuard(engine, mode="async", shutdown_drain_ms=5000).start()
    for raw in candidates(20):
        guard.observe(raw)
    counters = guard.stop()
    assert counters["unflushed_at_shutdown"] == 0
    assert counters["written"] == 20
    assert counters["evaluated"] == 20
    kinds = [e["event_type"] for e in engine.ledger.read()]
    assert kinds.count("candidate_observed") == 20
    assert kinds.count("guard_evaluated") == 20


def test_what_shutdown_could_not_flush_is_counted_and_logged(tmp_path):
    """A too-short budget must report the loss rather than hide it."""
    logged: list[tuple[str, dict]] = []
    engine = support.engine(tmp_path, the_judge=SlowJudge(delay=0.2))
    guard = ShadowGuard(engine, mode="async", shutdown_drain_ms=60,
                        log=lambda e, f: logged.append((e, f))).start()
    for raw in candidates(30):
        guard.observe(raw)
    counters = guard.stop()

    assert counters["unflushed_at_shutdown"] > 0, counters
    notices = [entry for entry in logged if entry[0] == "spend_guard.unflushed_at_shutdown"]
    assert notices and notices[0][1]["count"] == counters["unflushed_at_shutdown"]
    assert notices[0][1]["budget_ms"] == 60


def test_stop_is_safe_to_call_twice(tmp_path):
    guard = ShadowGuard(support.engine(tmp_path), mode="async").start()
    guard.observe(support.candidate("cand_pay_clean"))
    guard.stop()
    guard.stop()


def test_percentiles_include_p99_and_max(tmp_path):
    guard = ShadowGuard(support.engine(tmp_path), mode="async").start()
    for raw in candidates(10):
        guard.observe(raw)
    guard.stop()
    stats = guard.overhead_percentiles()
    assert set(stats) == {"p50", "p95", "p99", "max"}
    assert stats["p50"] <= stats["p95"] <= stats["p99"] <= stats["max"]
