"""Chapter 12: real purchase results attach to the shadow decision."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import support
from spend_guard.errors import LedgerError
from spend_guard.outcome import OutcomeRecorder
from spend_guard.report import report


class OutcomeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.engine = support.engine(self.dir)
        self.decision = self.engine.process(support.candidate("cand_pay_clean"))
        self.recorder = OutcomeRecorder(self.engine.ledger)
        self.addCleanup(self.tmp.cleanup)

    def test_a_result_links_to_the_decision_by_request_id(self):
        event = self.recorder.purchase_result(
            self.decision.request_id, payment_status="success",
            http_status=200, response_received=True)
        self.assertEqual(event["decision_id"], self.decision.decision_id)
        self.assertEqual(event["candidate_id"], self.decision.candidate_id)

    def test_results_are_appended_not_merged_into_the_decision(self):
        before = (self.dir / "ledger.jsonl").read_text(encoding="utf-8")
        self.recorder.purchase_attempted(self.decision.request_id)
        self.recorder.purchase_result(self.decision.request_id, payment_status="success")
        self.recorder.delivery_observed(self.decision.request_id, required_fields_present=True)
        after = (self.dir / "ledger.jsonl").read_text(encoding="utf-8")
        self.assertTrue(after.startswith(before))
        kinds = [e["event_type"] for e in self.engine.ledger.read()]
        self.assertEqual(kinds[-3:], ["purchase_attempted", "purchase_result", "delivery_observed"])

    def test_unknown_payment_status_is_allowed(self):
        """A broadcast that timed out is genuinely undetermined, not a failure."""
        event = self.recorder.purchase_result(self.decision.request_id, payment_status="unknown")
        self.assertEqual(event["payload"]["payment_status"], "unknown")

    def test_an_invented_payment_status_is_rejected(self):
        with self.assertRaises(LedgerError):
            self.recorder.purchase_result(self.decision.request_id, payment_status="probably")

    def test_a_result_for_an_unknown_request_is_rejected(self):
        with self.assertRaises(LedgerError):
            self.recorder.purchase_result("req_never_seen", payment_status="success")

    def test_the_decision_id_fallback_works_without_a_request_id(self):
        event = self.recorder.link_by_decision_id(
            self.decision.decision_id, "purchase_result", payment_status="success")
        self.assertEqual(event["decision_id"], self.decision.decision_id)

    def test_successful_purchases_reach_the_report(self):
        self.recorder.purchase_result(self.decision.request_id, payment_status="success")
        self.assertEqual(report(self.engine.ledger)["human_comparison"]["purchases_succeeded"], 1)


if __name__ == "__main__":
    unittest.main()
