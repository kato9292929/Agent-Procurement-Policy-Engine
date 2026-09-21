"""CLI contract (chapter 10): exit codes, JSON output, and offline dry runs."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path

import support
from spend_guard.cli import main
from spend_guard.errors import (
    EXIT_HOLD,
    EXIT_INPUT_ERROR,
    EXIT_PAY,
    EXIT_PROVIDER_ERROR,
    EXIT_REVIEW,
)


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.ledger = self.dir / "ledger.jsonl"
        self.addCleanup(self.tmp.cleanup)

    def write(self, name: str) -> str:
        path = self.dir / f"{name}.json"
        path.write_text(json.dumps(support.candidate(name)), encoding="utf-8")
        return str(path)

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = main(["--policy", str(support.POLICY_PATH), "--ledger", str(self.ledger), *args],
                    out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def evaluate(self, name: str, *extra: str) -> tuple[int, str, str]:
        return self.run_cli("evaluate", "--input", self.write(name), "--json",
                            "--dry-run", "--fixture", str(support.ANSWERS), *extra)

    def test_pay_exits_zero(self):
        code, out, _ = self.evaluate("cand_pay_clean")
        self.assertEqual(code, EXIT_PAY)
        self.assertEqual(json.loads(out)["decision"], "PAY")

    def test_hold_and_review_have_distinct_codes(self):
        self.assertEqual(self.evaluate("cand_off_task")[0], EXIT_HOLD)
        self.assertEqual(self.evaluate("cand_thin_evidence")[0], EXIT_REVIEW)

    def test_a_provider_failure_does_not_share_a_code_with_uncertainty(self):
        """Chapter 10: semantic uncertainty and system failure must differ."""
        uncertain = self.evaluate("cand_low_confidence")[0]
        failed = self.evaluate("cand_jev_timeout")[0]
        self.assertEqual(uncertain, EXIT_REVIEW)
        self.assertEqual(failed, EXIT_PROVIDER_ERROR)
        self.assertNotEqual(uncertain, failed)

    def test_bad_input_is_an_input_error(self):
        path = self.dir / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        code, _, err = self.run_cli("evaluate", "--input", str(path), "--dry-run",
                                    "--fixture", str(support.ANSWERS))
        self.assertEqual(code, EXIT_INPUT_ERROR)
        self.assertEqual(json.loads(err)["error"], "input_error")

    def test_a_missing_file_is_an_input_error(self):
        code, _, _ = self.run_cli("evaluate", "--input", str(self.dir / "nope.json"),
                                  "--dry-run", "--fixture", str(support.ANSWERS))
        self.assertEqual(code, EXIT_INPUT_ERROR)

    def test_dry_run_without_a_fixture_is_rejected_rather_than_calling_jev(self):
        code, _, err = self.run_cli("evaluate", "--input", self.write("cand_pay_clean"), "--dry-run")
        self.assertEqual(code, EXIT_INPUT_ERROR)
        self.assertIn("fixture", json.loads(err)["message"])

    def test_jsonl_mode_reports_the_worst_outcome(self):
        code, out, _ = self.run_cli(
            "evaluate", "--input", str(support.CANDIDATES), "--jsonl",
            "--dry-run", "--fixture", str(support.ANSWERS))
        lines = [json.loads(line) for line in out.splitlines() if line.strip()]
        self.assertEqual(len(lines), 9)
        self.assertEqual(code, EXIT_REVIEW)

    def test_the_decision_record_carries_every_chapter_seven_field(self):
        _, out, _ = self.evaluate("cand_pay_clean")
        record = json.loads(out)
        for field in ("schema_version", "decision_id", "candidate_id", "request_id", "mode",
                      "decision", "reason_codes", "deterministic_checks", "semantic_judgments",
                      "policy", "input_hash", "created_at", "latency_ms"):
            self.assertIn(field, record)
        self.assertEqual(record["policy"]["policy_version"], "shadow-v1")
        self.assertEqual(set(record["semantic_judgments"]["confidence"]),
                         {"task_fit", "incremental_value", "duplication_risk", "evidence_sufficiency"})

    def test_no_single_overall_confidence_is_emitted(self):
        """Chapter 7: per-question confidences only, never one blended number."""
        _, out, _ = self.evaluate("cand_pay_clean")
        judgment = json.loads(out)["semantic_judgments"]
        self.assertIsInstance(judgment["confidence"], dict)
        self.assertNotIn("overall", judgment["confidence"])
        self.assertNotIn("score", judgment)

    def test_replay_and_report_run_over_a_written_ledger(self):
        self.run_cli("evaluate", "--input", str(support.CANDIDATES), "--jsonl",
                     "--dry-run", "--fixture", str(support.ANSWERS))
        code, out, _ = self.run_cli("replay")
        self.assertEqual(code, EXIT_PAY)
        self.assertEqual(json.loads(out)["diff"]["changed"], 0)

        code, out, _ = self.run_cli("report", "--verify-chain", "--distribution")
        data = json.loads(out)
        self.assertEqual(code, EXIT_PAY)
        self.assertEqual(data["chain_problems"], [])
        self.assertEqual(data["decisions"]["evaluated"], 9)
        self.assertIn("score_distribution", data)

    def test_feedback_appends_without_touching_earlier_events(self):
        self.run_cli("evaluate", "--input", self.write("cand_off_task"), "--json",
                     "--dry-run", "--fixture", str(support.ANSWERS))
        before = self.ledger.read_text(encoding="utf-8")
        decision_id = json.loads(
            [line for line in before.splitlines() if '"guard_evaluated"' in line][0]
        )["decision_id"]

        code, out, _ = self.run_cli("feedback", "--decision-id", decision_id,
                                    "--label", "not_needed", "--note", "already held")
        self.assertEqual(code, EXIT_PAY)
        self.assertEqual(json.loads(out)["label"], "not_needed")
        after = self.ledger.read_text(encoding="utf-8")
        self.assertTrue(after.startswith(before))

    def test_feedback_for_an_unknown_decision_is_an_input_error(self):
        code, _, _ = self.run_cli("feedback", "--decision-id", "dec_nope", "--label", "needed")
        self.assertEqual(code, EXIT_INPUT_ERROR)

    def test_an_invalid_label_is_rejected_by_the_parser(self):
        with self.assertRaises(SystemExit):
            self.run_cli("feedback", "--decision-id", "dec_x", "--label", "maybe")


if __name__ == "__main__":
    unittest.main()
