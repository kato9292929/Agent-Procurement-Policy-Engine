"""Real output is validated against the published schemas.

`jsonschema` is a development convenience only; the runtime has no
dependencies, so this module skips when it is absent rather than failing.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import support
from spend_guard.outcome import OutcomeRecorder

try:
    from jsonschema import Draft202012Validator
except ImportError:  # pragma: no cover
    Draft202012Validator = None

SCHEMAS = support.ROOT / "schemas"


def load(name: str):
    return json.loads((SCHEMAS / name).read_text(encoding="utf-8"))


@unittest.skipIf(Draft202012Validator is None, "jsonschema not installed")
class SchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.engine = support.engine(self.dir)
        self.decisions = [
            self.engine.process(support.candidate(name)) for name in support.candidates()
        ]
        recorder = OutcomeRecorder(self.engine.ledger)
        first = self.decisions[0]
        recorder.purchase_attempted(first.request_id)
        recorder.purchase_result(first.request_id, payment_status="success", http_status=200)
        recorder.delivery_observed(first.request_id, required_fields_present=True)
        self.engine.ledger.append(
            "human_feedback", decision_id=first.decision_id, candidate_id=first.candidate_id,
            request_id=first.request_id, payload={"label": "needed"})
        self.addCleanup(self.tmp.cleanup)

    def test_schemas_are_themselves_valid(self):
        for name in ("candidate.schema.json", "decision.schema.json", "ledger-event.schema.json"):
            with self.subTest(name=name):
                Draft202012Validator.check_schema(load(name))

    def test_every_decision_matches_the_decision_schema(self):
        validator = Draft202012Validator(load("decision.schema.json"))
        for decision in self.decisions:
            with self.subTest(decision=decision.candidate_id):
                errors = sorted(validator.iter_errors(decision.as_dict()), key=lambda e: e.path)
                self.assertEqual([e.message for e in errors], [])

    def test_every_ledger_event_matches_the_event_schema(self):
        validator = Draft202012Validator(load("ledger-event.schema.json"))
        events = self.engine.ledger.read()
        self.assertGreater(len(events), 20)
        for event in events:
            with self.subTest(event=event["event_type"]):
                errors = sorted(validator.iter_errors(event), key=lambda e: e.path)
                self.assertEqual([e.message for e in errors], [])

    def test_every_candidate_fixture_matches_the_candidate_schema(self):
        validator = Draft202012Validator(load("candidate.schema.json"))
        for name, raw in support.candidates().items():
            with self.subTest(name=name):
                normalized = support.engine(self.dir).observe(raw)["candidate"].as_dict()
                errors = sorted(validator.iter_errors(normalized), key=lambda e: e.path)
                self.assertEqual([e.message for e in errors], [])


if __name__ == "__main__":
    unittest.main()
