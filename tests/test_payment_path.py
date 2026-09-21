"""Chapter 13: the guard changes nothing about the existing payment path.

Three separate claims are tested, because chapter 6 asks for all three:
whether a payment happens, how long the payment path takes, and how it fails.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

import support
from spend_guard.errors import LedgerError
from spend_guard.hook import ShadowGuard
from spend_guard.models import SemanticJudgment


class FakePaymentFlow:
    """Stands in for the existing x402 purchase path. The guard must not alter it."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def purchase(self, raw: dict) -> dict:
        self.calls.append(raw["candidate_id"])
        return {"payment_status": "success", "candidate_id": raw["candidate_id"]}


class SlowJudge:
    def __init__(self, delay: float = 0.3) -> None:
        self.delay = delay
        self.calls = 0

    def evaluate(self, candidate, policy) -> SemanticJudgment:
        self.calls += 1
        time.sleep(self.delay)
        return SemanticJudgment(
            status="OK",
            model="slow",
            scores={
                "task_fit": 0.9,
                "incremental_value": 0.9,
                "duplication_risk": 0.1,
                "evidence_sufficiency": 0.9,
            },
            confidence={
                "task_fit": 0.9,
                "incremental_value": 0.9,
                "duplication_risk": 0.9,
                "evidence_sufficiency": 0.9,
            },
        )


class ExplodingJudge:
    def evaluate(self, candidate, policy):
        raise RuntimeError("judge blew up")


class PaymentPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.flow = FakePaymentFlow()
        self.addCleanup(self.tmp.cleanup)

    def run_flow(self, guard: ShadowGuard, names: list[str]) -> list[dict]:
        """The integration shape: observe, then run the untouched purchase."""
        results = []
        for name in names:
            raw = support.candidate(name)
            guard.observe(raw)          # shadow, returns nothing
            results.append(self.flow.purchase(raw))  # existing logic, unchanged
        return results

    # -- claim 1: whether a payment happens --

    def test_payment_runs_for_every_shadow_decision(self):
        names = ["cand_pay_clean", "cand_off_task", "cand_exact_repurchase", "cand_missing_fields"]
        with ShadowGuard(support.engine(self.dir)) as guard:
            results = self.run_flow(guard, names)
        self.assertEqual(self.flow.calls, names)
        self.assertTrue(all(r["payment_status"] == "success" for r in results))

        decisions = {
            e["payload"]["candidate_id"]: e["payload"]["decision"]
            for e in guard.engine.ledger.read()
            if e["event_type"] == "guard_evaluated"
        }
        self.assertIn("HOLD", decisions.values())
        self.assertIn("REVIEW", decisions.values())
        self.assertEqual(len(self.flow.calls), 4, "a HOLD or REVIEW must not stop a payment")

    def test_observe_returns_nothing_to_branch_on(self):
        """There is deliberately no verdict the payment path could act on."""
        with ShadowGuard(support.engine(self.dir)) as guard:
            self.assertIsNone(guard.observe(support.candidate("cand_off_task")))

    # -- claim 3: how it fails --

    def test_guard_exception_does_not_reach_the_payment_path(self):
        engine = support.engine(self.dir, the_judge=ExplodingJudge())
        with ShadowGuard(engine) as guard:
            self.run_flow(guard, ["cand_pay_clean"])
        self.assertEqual(self.flow.calls, ["cand_pay_clean"])

    def test_malformed_candidate_does_not_reach_the_payment_path(self):
        with ShadowGuard(support.engine(self.dir)) as guard:
            guard.observe("not a candidate at all")
            guard.observe(None)
            result = self.flow.purchase(support.candidate("cand_pay_clean"))
        self.assertEqual(result["payment_status"], "success")
        self.assertEqual(guard.counters["dropped"], 2)

    def test_unwritable_ledger_does_not_reach_the_payment_path(self):
        """A ledger that cannot be written is counted and logged, never raised."""
        blocked = self.dir / "a-file"
        blocked.write_text("not a directory")
        engine = support.engine(self.dir)
        engine.ledger.path = blocked / "ledger.jsonl"
        logged: list[tuple[str, dict]] = []
        with ShadowGuard(engine, log=lambda e, f: logged.append((e, f))) as guard:
            self.run_flow(guard, ["cand_pay_clean"])
        self.assertEqual(self.flow.calls, ["cand_pay_clean"])
        self.assertEqual(guard.counters["dropped"], 1)
        self.assertEqual(logged[0][0], "spend_guard.dropped")
        # Only the class of failure is logged; no candidate data leaks into the host log.
        self.assertEqual(set(logged[0][1]), {"reason", "error", "count"})

    def test_ledger_error_is_raised_from_the_engine_but_not_from_the_guard(self):
        blocked = self.dir / "b-file"
        blocked.write_text("not a directory")
        engine = support.engine(self.dir)
        engine.ledger.path = blocked / "ledger.jsonl"
        with self.assertRaises(LedgerError):
            engine.observe(support.candidate("cand_pay_clean"))  # CLI path surfaces it
        with ShadowGuard(engine) as guard:
            self.assertIsNone(guard.observe(support.candidate("cand_pay_clean")))  # hook swallows it

    # -- claim 2: how long the payment path takes --

    def test_async_mode_keeps_a_slow_jev_off_the_payment_path(self):
        judge = SlowJudge(delay=0.4)
        engine = support.engine(self.dir, the_judge=judge)
        with ShadowGuard(engine, mode="async") as guard:
            started = time.perf_counter()
            self.run_flow(guard, ["cand_pay_clean"])
            elapsed = (time.perf_counter() - started) * 1000
        self.assertLess(elapsed, 200, f"payment path waited {elapsed:.0f}ms on a 400ms judge")
        self.assertEqual(judge.calls, 1, "the judgment still happened, just not inline")
        overhead = guard.overhead_percentiles()
        self.assertLess(overhead["p95"], 200)

    def test_async_evaluation_is_recorded_after_the_payment_returns(self):
        engine = support.engine(self.dir, the_judge=SlowJudge(delay=0.1))
        guard = ShadowGuard(engine, mode="async").start()
        self.run_flow(guard, ["cand_pay_clean"])
        guard.stop()
        kinds = [e["event_type"] for e in engine.ledger.read()]
        self.assertEqual(kinds, ["candidate_observed", "guard_evaluated"])

    def test_sync_mode_respects_the_configured_budget(self):
        """The synchronous fallback records an over-budget run instead of hiding it."""
        engine = support.engine(self.dir, the_judge=SlowJudge(delay=0.2))
        guard = ShadowGuard(engine, mode="sync", hook_timeout_ms=50)
        self.run_flow(guard, ["cand_pay_clean"])
        self.assertEqual(self.flow.calls, ["cand_pay_clean"])
        self.assertEqual(guard.counters["timed_out"], 1)

    def test_hook_overhead_is_measured_for_reporting(self):
        with ShadowGuard(support.engine(self.dir)) as guard:
            self.run_flow(guard, ["cand_pay_clean", "cand_off_task"])
        percentiles = guard.overhead_percentiles()
        self.assertIsNotNone(percentiles["p50"])
        self.assertIsNotNone(percentiles["p95"])
        overheads = [
            e["payload"]["hook_overhead_ms"]
            for e in guard.engine.ledger.read()
            if e["event_type"] == "candidate_observed"
        ]
        self.assertTrue(all(isinstance(v, float) for v in overheads))

    def test_a_full_queue_is_dropped_rather_than_made_to_wait(self):
        engine = support.engine(self.dir, the_judge=SlowJudge(delay=0.5))
        guard = ShadowGuard(engine, mode="async", queue_size=1)
        # No worker started, so the queue cannot drain and fills immediately.
        for name in ["cand_pay_clean", "cand_off_task", "cand_thin_evidence"]:
            guard.observe(support.candidate(name))
        self.assertGreaterEqual(guard.counters["dropped"], 1)
        self.assertEqual(self.flow.purchase(support.candidate("cand_pay_clean"))["payment_status"], "success")


if __name__ == "__main__":
    unittest.main()
