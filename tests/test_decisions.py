"""Chapter 13: each aggregation rule reaches the decision it is meant to."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import support
from spend_guard.models import (
    SEMANTIC_INVALID_RESPONSE,
    SEMANTIC_SKIPPED,
    SEMANTIC_TIMEOUT,
    SIGNAL_EXACT_REPURCHASE,
)


class DecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.judge = support.judge()
        self.engine = support.engine(self.dir, the_judge=self.judge)
        self.addCleanup(self.tmp.cleanup)

    def decide(self, name: str, **overrides):
        return self.engine.process(support.candidate(name, **overrides))

    def test_fitting_unheld_well_evidenced_candidate_pays(self):
        decision = self.decide("cand_pay_clean")
        self.assertEqual(decision.decision, "PAY")
        self.assertIn("TASK_MATCH", decision.reason_codes)

    def test_semantic_overlap_holds(self):
        decision = self.decide("cand_semantic_dupe")
        self.assertEqual(decision.decision, "HOLD")
        self.assertIn("DUPLICATION_RISK", decision.reason_codes)

    def test_exact_repurchase_inside_window_holds(self):
        decision = self.decide("cand_exact_repurchase")
        self.assertEqual(decision.decision, "HOLD")
        self.assertIn(SIGNAL_EXACT_REPURCHASE, decision.deterministic_checks.signals)
        self.assertIn(SIGNAL_EXACT_REPURCHASE, decision.reason_codes)

    def test_exact_repurchase_outside_window_does_not_signal(self):
        raw = support.candidate("cand_exact_repurchase")
        raw["prior_acquisitions"][0]["acquired_at"] = "2026-09-19T00:00:00Z"  # 25h earlier
        decision = self.engine.process(raw)
        self.assertNotIn(SIGNAL_EXACT_REPURCHASE, decision.deterministic_checks.signals)

    def test_exact_repurchase_still_calls_jev_for_comparison(self):
        """Rule 3 decides on its own, but the semantic answer is still recorded."""
        decision = self.decide("cand_exact_repurchase")
        self.assertIn("cand_exact_repurchase", self.judge.calls)
        self.assertEqual(decision.semantic_judgments.status, "OK")
        self.assertIsNotNone(decision.semantic_judgments.scores["duplication_risk"])

    def test_off_task_candidate_holds(self):
        decision = self.decide("cand_off_task")
        self.assertEqual(decision.decision, "HOLD")
        self.assertIn("TASK_MISMATCH", decision.reason_codes)

    def test_missing_required_input_reviews_without_calling_jev(self):
        decision = self.decide("cand_missing_fields")
        self.assertEqual(decision.decision, "REVIEW")
        self.assertEqual(decision.semantic_judgments.status, SEMANTIC_SKIPPED)
        self.assertEqual(self.judge.calls, [], "Jev must not be called for rule 1")
        self.assertIn("MISSING_REQUIRED_FIELDS", decision.reason_codes)
        self.assertIn("service.route_id", decision.deterministic_checks.missing_fields)

    def test_low_confidence_reviews(self):
        decision = self.decide("cand_low_confidence")
        self.assertEqual(decision.decision, "REVIEW")
        self.assertIn("LOW_CONFIDENCE_TASK_FIT", decision.reason_codes)

    def test_insufficient_evidence_reviews(self):
        decision = self.decide("cand_thin_evidence")
        self.assertEqual(decision.decision, "REVIEW")
        self.assertIn("EVIDENCE_INSUFFICIENT", decision.reason_codes)

    def test_jev_timeout_reviews(self):
        decision = self.decide("cand_jev_timeout")
        self.assertEqual(decision.decision, "REVIEW")
        self.assertEqual(decision.semantic_judgments.status, SEMANTIC_TIMEOUT)

    def test_jev_invalid_response_reviews(self):
        decision = self.decide("cand_jev_invalid")
        self.assertEqual(decision.decision, "REVIEW")
        self.assertEqual(decision.semantic_judgments.status, SEMANTIC_INVALID_RESPONSE)

    def test_provider_failure_is_not_recorded_as_a_low_score(self):
        """A failure and a confident 'no' must stay distinguishable."""
        decision = self.decide("cand_jev_timeout")
        self.assertEqual(decision.semantic_judgments.scores, {})
        self.assertNotIn("TASK_MISMATCH", decision.reason_codes)

    def test_every_decision_is_one_of_the_three(self):
        for name in support.candidates():
            with self.subTest(name=name):
                decision = self.engine.process(support.candidate(name))
                self.assertIn(decision.decision, ("PAY", "HOLD", "REVIEW"))
                self.assertEqual(decision.mode, "shadow")


if __name__ == "__main__":
    unittest.main()
