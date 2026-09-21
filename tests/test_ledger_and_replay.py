"""Chapter 13: ledger integrity, the send limit, and replay determinism."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

import support
from spend_guard.canonical import canonicalize, hash_json
from spend_guard.judges.jev import PRIOR_ACQUISITION_FIELDS, SERVICE_FIELDS, project_state
from spend_guard.ledger import GENESIS, Ledger, redact, verify_chain
from spend_guard.normalize import input_hash, normalize
from spend_guard.report import replay, report

SECRET = "sk-live-must-never-be-stored"


class CanonicalTests(unittest.TestCase):
    def test_key_order_does_not_change_the_hash(self):
        self.assertEqual(hash_json({"a": 1, "b": {"c": 2, "d": 3}}),
                         hash_json({"b": {"d": 3, "c": 2}, "a": 1}))

    def test_whitespace_and_indentation_do_not_change_the_hash(self):
        text = '{ "a" : 1 ,\n  "b" : [ 1 , 2 ] }'
        self.assertEqual(hash_json(json.loads(text)), hash_json({"b": [1, 2], "a": 1}))

    def test_reordered_candidate_hashes_alike(self):
        raw = support.candidate("cand_pay_clean")
        reordered = json.loads(json.dumps(raw, sort_keys=True))
        reordered = dict(reversed(list(reordered.items())))
        self.assertEqual(input_hash(normalize(raw)), input_hash(normalize(reordered)))

    def test_numbers_follow_the_ecmascript_form(self):
        self.assertEqual(canonicalize({"a": 1e21}), '{"a":1e+21}')
        self.assertEqual(canonicalize({"a": 1e-7}), '{"a":1e-7}')
        self.assertEqual(canonicalize({"a": 1e16}), '{"a":10000000000000000}')
        self.assertEqual(canonicalize({"a": -0.0}), '{"a":0}')

    def test_keys_sort_by_utf16_code_unit(self):
        # U+FF3A sorts after U+1F600 by code unit, though before it by code point.
        self.assertEqual(canonicalize({"Ｚ": 1, "\U0001f600": 2}), '{"\U0001f600":2,"Ｚ":1}')


class PrivacyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_jev_payload_contains_only_allowlisted_fields(self):
        raw = support.candidate("cand_pay_clean")
        raw["service"]["api_key"] = SECRET
        raw["authorization"] = f"Bearer {SECRET}"
        state = project_state(normalize(raw))
        self.assertEqual(set(state), {"task", "service", "prior_acquisitions"})
        self.assertEqual(set(state["service"]), set(SERVICE_FIELDS))
        for prior in state["prior_acquisitions"]:
            self.assertEqual(set(prior), set(PRIOR_ACQUISITION_FIELDS))
        blob = json.dumps(state)
        self.assertNotIn(SECRET, blob)

    def test_jev_payload_withholds_ids_hashes_and_the_amount(self):
        """Hashes carry no meaning a semantic judge can use; the amount is not asked about."""
        state = project_state(normalize(support.candidate("cand_pay_clean")))
        blob = json.dumps(state)
        for absent in ("request_id", "idempotency_key", "request_params_hash",
                       "content_fingerprint", "quote", "candidate_id"):
            self.assertNotIn(absent, blob)

    def test_secrets_are_stripped_before_reaching_the_ledger(self):
        raw = support.candidate("cand_pay_clean")
        raw["service"]["api_key"] = SECRET
        raw["service"]["authorization"] = f"Bearer {SECRET}"
        raw["wallet_private_key"] = SECRET
        engine = support.engine(self.dir)
        engine.process(raw)
        text = (self.dir / "ledger.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(SECRET, text)
        self.assertIn("[redacted]", text)

    def test_redaction_keeps_structure(self):
        cleaned = redact({"keep": 1, "api_key": "x", "nested": [{"auth_token": "y", "ok": 2}]})
        self.assertEqual(cleaned, {"keep": 1, "api_key": "[redacted]",
                                   "nested": [{"auth_token": "[redacted]", "ok": 2}]})

    def test_ledger_file_is_owner_only(self):
        engine = support.engine(self.dir)
        engine.process(support.candidate("cand_pay_clean"))
        self.assertEqual((self.dir / "ledger.jsonl").stat().st_mode & 0o777, 0o600)


class ChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_chain_links_and_verifies(self):
        engine = support.engine(self.dir)
        for name in support.candidates():
            engine.process(support.candidate(name))
        events = engine.ledger.read()
        self.assertEqual(events[0]["previous_event_hash"], GENESIS)
        self.assertEqual(verify_chain(events), [])

    def test_tampering_with_a_recorded_line_is_detected(self):
        engine = support.engine(self.dir)
        engine.process(support.candidate("cand_off_task"))
        path = self.dir / "ledger.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        event = json.loads(lines[-1])
        event["payload"]["decision"] = "PAY"  # rewrite history
        lines[-1] = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertTrue(verify_chain(Ledger(path).read()))

    def test_concurrent_appends_do_not_fork_the_chain(self):
        ledger = Ledger(self.dir / "ledger.jsonl")
        writers, per_writer = 8, 25
        errors: list[Exception] = []

        def write(worker: int) -> None:
            try:
                for index in range(per_writer):
                    ledger.append(
                        "candidate_observed",
                        decision_id=f"dec_{worker}_{index}",
                        candidate_id=f"cand_{worker}_{index}",
                        request_id=f"req_{worker}_{index}",
                        payload={"input_hash": f"sha256:{worker}{index}"},
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(w,)) for w in range(writers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        events = ledger.read()
        self.assertEqual(len(events), writers * per_writer)
        self.assertEqual(verify_chain(events), [], "concurrent appends forked the chain")
        hashes = [e["event_hash"] for e in events]
        self.assertEqual(len(set(hashes)), len(hashes))

    def test_long_lines_do_not_break_tail_reading(self):
        """The tail scan reads backwards in blocks; a record over one block must still parse."""
        ledger = Ledger(self.dir / "ledger.jsonl")
        big = "x" * 9000
        ledger.append("candidate_observed", decision_id="d1", candidate_id="c1",
                      request_id="r1", payload={"note": big})
        second = ledger.append("candidate_observed", decision_id="d2", candidate_id="c2",
                               request_id="r2", payload={})
        events = ledger.read()
        self.assertEqual(second["previous_event_hash"], events[0]["event_hash"])
        self.assertEqual(verify_chain(events), [])

    def test_an_event_is_never_updated_in_place(self):
        engine = support.engine(self.dir)
        decision = engine.process(support.candidate("cand_pay_clean"))
        before = (self.dir / "ledger.jsonl").read_text(encoding="utf-8")
        engine.ledger.append(
            "purchase_result",
            decision_id=decision.decision_id,
            candidate_id=decision.candidate_id,
            request_id=decision.request_id,
            payload={"payment_status": "success"},
        )
        after = (self.dir / "ledger.jsonl").read_text(encoding="utf-8")
        self.assertTrue(after.startswith(before), "an earlier line was rewritten")


class ReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.judge = support.judge()
        self.engine = support.engine(self.dir, the_judge=self.judge)
        for name in support.candidates():
            self.engine.process(support.candidate(name))
        self.calls_after_run = len(self.judge.calls)
        self.addCleanup(self.tmp.cleanup)

    def test_replay_does_not_call_jev(self):
        replay(self.engine.ledger, support.policy())
        self.assertEqual(len(self.judge.calls), self.calls_after_run)

    def test_replay_is_deterministic_and_reproduces_the_original(self):
        first = replay(self.engine.ledger, support.policy())
        second = replay(self.engine.ledger, support.policy())
        self.assertEqual(first, second)
        self.assertEqual(first["diff"]["changed"], 0)

    def test_changing_a_threshold_moves_decisions_and_reports_the_diff(self):
        """Dropping the evidence floor should release exactly the thin-evidence case.

        Loosening `duplication_risk_max` alone would move nothing, because the
        two duplication HOLDs are each held by a second rule as well, so this
        picks the threshold whose effect is isolated.
        """
        loosened = support.policy(thresholds={"evidence_sufficiency_min": 0.10})
        result = replay(self.engine.ledger, loosened)
        self.assertEqual(result["diff"]["changed"], 1)
        self.assertEqual(result["diff"]["transitions"], {"REVIEW->PAY": 1})
        for item in result["diff"]["details"]:
            self.assertNotEqual(item["from"], item["to"])
        self.assertEqual(
            sum(result["diff"]["transitions"].values()), result["diff"]["changed"]
        )

    def test_a_threshold_whose_rule_is_shadowed_changes_nothing(self):
        """Both duplication HOLDs are also held by rule 8, so relaxing rule 6 is a no-op."""
        result = replay(self.engine.ledger, support.policy(thresholds={"duplication_risk_max": 0.99}))
        self.assertEqual(result["diff"]["changed"], 0)

    def test_raising_confidence_min_pushes_everything_to_review(self):
        strict = support.policy(thresholds={"confidence_min": 0.99})
        result = replay(self.engine.ledger, strict)
        self.assertEqual(result["counts"]["PAY"], 0)
        self.assertGreater(result["counts"]["REVIEW"], 0)

    def test_report_counts_review_separately_from_hold(self):
        data = report(self.engine.ledger)
        counts = data["decisions"]["counts"]
        self.assertEqual(sum(counts.values()), data["decisions"]["evaluated"])
        self.assertIn("REVIEW", counts)
        self.assertIn("HOLD", counts)
        self.assertIsNot(counts["REVIEW"], counts["HOLD"])

    def test_report_surfaces_human_feedback_rates(self):
        decisions = [
            e["payload"]
            for e in self.engine.ledger.read()
            if e["event_type"] == "guard_evaluated"
        ]
        held = next(p for p in decisions if p["decision"] == "HOLD")
        self.engine.ledger.append(
            "human_feedback",
            decision_id=held["decision_id"],
            candidate_id=held["candidate_id"],
            request_id=held["request_id"],
            payload={"label": "not_needed", "note": "already had it"},
        )
        data = report(self.engine.ledger)
        self.assertEqual(data["human_comparison"]["labelled"], 1)
        self.assertEqual(data["human_comparison"]["not_needed_held"], 1.0)

    def test_latest_feedback_wins_and_history_is_kept(self):
        payload = next(
            e["payload"] for e in self.engine.ledger.read() if e["event_type"] == "guard_evaluated"
        )
        for label in ("unknown", "needed"):
            self.engine.ledger.append(
                "human_feedback",
                decision_id=payload["decision_id"],
                candidate_id=payload["candidate_id"],
                request_id=payload["request_id"],
                payload={"label": label},
            )
        events = [e for e in self.engine.ledger.read() if e["event_type"] == "human_feedback"]
        self.assertEqual([e["payload"]["label"] for e in events], ["unknown", "needed"])
        self.assertEqual(report(self.engine.ledger)["human_comparison"]["labelled"], 1)


if __name__ == "__main__":
    unittest.main()
