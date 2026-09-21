"""Chapter 13: duplicate request ids, retries, and re-evaluation."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import support
from spend_guard.models import SIGNAL_DUPLICATE_REQUEST_ID, SIGNAL_RETRY_OBSERVED


class DuplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.judge = support.judge()
        self.engine = support.engine(self.dir, the_judge=self.judge)
        self.addCleanup(self.tmp.cleanup)

    def test_first_sighting_of_a_request_id_raises_no_signal(self):
        decision = self.engine.process(support.candidate("cand_pay_clean"))
        self.assertEqual(decision.deterministic_checks.signals, ())

    def test_unexplained_reuse_of_a_request_id_reviews(self):
        """No idempotency key and no retry marker: treated as a possible double spend."""
        self.engine.process(support.candidate("cand_pay_clean", idempotency_key=None))
        second = self.engine.process(
            support.candidate(
                "cand_pay_clean",
                candidate_id="cand_second",
                idempotency_key=None,
                observed_at="2026-09-21T00:10:00Z",
            )
        )
        self.assertIn(SIGNAL_DUPLICATE_REQUEST_ID, second.deterministic_checks.signals)
        self.assertEqual(second.decision, "REVIEW")

    def test_identified_retry_is_recorded_and_not_treated_as_a_failure(self):
        """A matching idempotency key marks a retry, which must not become REVIEW."""
        self.engine.process(support.candidate("cand_pay_clean"))
        retry = self.engine.process(
            support.candidate(
                "cand_pay_clean", candidate_id="cand_retry", observed_at="2026-09-21T00:10:00Z"
            )
        )
        self.assertIn(SIGNAL_RETRY_OBSERVED, retry.deterministic_checks.signals)
        self.assertNotIn(SIGNAL_DUPLICATE_REQUEST_ID, retry.deterministic_checks.signals)
        self.assertEqual(retry.deterministic_checks.status, "PASS")
        self.assertNotIn(SIGNAL_RETRY_OBSERVED, retry.reason_codes)
        self.assertEqual(retry.decision, "PAY")

    def test_explicit_retry_marker_is_also_accepted(self):
        self.engine.process(support.candidate("cand_pay_clean", idempotency_key=None))
        retry = self.engine.process(
            support.candidate(
                "cand_pay_clean",
                candidate_id="cand_retry",
                idempotency_key=None,
                retry_of="cand_pay_clean",
                observed_at="2026-09-21T00:10:00Z",
            )
        )
        self.assertIn(SIGNAL_RETRY_OBSERVED, retry.deterministic_checks.signals)

    def test_same_request_and_input_hash_does_not_call_jev_again(self):
        self.engine.process(support.candidate("cand_pay_clean"))
        self.assertEqual(len(self.judge.calls), 1)
        repeat = self.engine.process(support.candidate("cand_pay_clean"))
        self.assertIsNone(repeat, "an identical re-observation must not be re-judged")
        self.assertEqual(len(self.judge.calls), 1, "Jev must not be called a second time")

    def test_repeat_observation_points_at_the_original_decision(self):
        first = self.engine.process(support.candidate("cand_pay_clean"))
        self.engine.observe(support.candidate("cand_pay_clean"))
        events = self.engine.ledger.read()
        repeat = [
            e
            for e in events
            if e["event_type"] == "candidate_observed" and e["payload"].get("duplicate_of")
        ]
        self.assertEqual(len(repeat), 1)
        self.assertEqual(repeat[0]["payload"]["duplicate_of"], first.decision_id)

    def test_changed_input_under_the_same_request_id_is_evaluated_again(self):
        self.engine.process(support.candidate("cand_pay_clean"))
        changed = support.candidate("cand_pay_clean", candidate_id="cand_changed")
        changed["quote"]["amount"] = "0.09"
        changed["observed_at"] = "2026-09-21T00:10:00Z"
        second = self.engine.process(changed)
        self.assertIsNotNone(second, "a different input_hash is a different evaluation")
        self.assertEqual(len(self.judge.calls), 2)

    def test_candidate_id_does_not_change_the_input_hash(self):
        """Two observations of the same purchase must hash alike."""
        from spend_guard.normalize import input_hash, normalize

        raw = support.candidate("cand_pay_clean")
        other = support.candidate("cand_pay_clean", candidate_id="cand_other_name")
        self.assertEqual(
            input_hash(normalize(raw)), input_hash(normalize(other))
        )

    def test_async_evaluation_cannot_see_its_own_purchase_as_prior(self):
        """Chapter 5: the prior-acquisitions snapshot is frozen at observation.

        The ledger gains the candidate's own record between observe and
        evaluate. If evaluation re-read the world instead of using the
        snapshot, the purchase would score as a duplicate of itself.
        """
        raw = support.candidate("cand_pay_clean")
        observed = self.engine.observe(raw)
        snapshot = tuple(observed["candidate"].prior_acquisitions)

        # Something else is bought, and lands in the ledger, before evaluation.
        self.engine.process(support.candidate("cand_off_task"))

        decision = self.engine.evaluate(
            observed["candidate"],
            decision_id=observed["decision_id"],
            digest=observed["input_hash"],
        )
        self.assertEqual(observed["candidate"].prior_acquisitions, snapshot)
        self.assertNotIn("EXACT_REPURCHASE", decision.reason_codes)
        self.assertEqual(decision.decision, "PAY")


if __name__ == "__main__":
    unittest.main()
