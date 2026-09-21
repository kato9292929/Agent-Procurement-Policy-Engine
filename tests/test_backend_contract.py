"""One contract, both stores (D-7).

Every test here is parametrized over the JSONL and Postgres backends, so the
two cannot drift: if a behaviour holds for one and not the other, a test fails
rather than a production surprise appearing later.
"""

from __future__ import annotations

import json

import pytest
import support
from spend_guard.backends.base import GENESIS, prepare_event, verify_chain
from spend_guard.engine import ShadowEngine
from spend_guard.ledger import Ledger
from spend_guard.outcome import OutcomeRecorder
from spend_guard.report import replay, report, sample


def event(n: int, event_type: str = "candidate_observed", **payload):
    return prepare_event(
        event_type,
        decision_id=f"dec_{n}",
        candidate_id=f"cand_{n}",
        request_id=f"req_{n}",
        payload=payload or {"input_hash": f"sha256:{n:064d}"},
        occurred_at="2026-09-21T00:00:00Z",
    )


# -- append and read -------------------------------------------------------


def test_empty_ledger_has_no_head(backend):
    assert backend.latest_event_hash() is None
    assert list(backend.iter_events()) == []


def test_first_event_chains_from_genesis(backend):
    stored = backend.append(event(0))
    assert stored["previous_event_hash"] == GENESIS
    assert backend.latest_event_hash() == stored["event_hash"]


def test_events_come_back_in_append_order(backend):
    appended = [backend.append(event(n)) for n in range(5)]
    read = list(backend.iter_events())
    assert [e["event_id"] for e in read] == [e["event_id"] for e in appended]


def test_the_chain_verifies(backend):
    for n in range(8):
        backend.append(event(n))
    assert verify_chain(list(backend.iter_events())) == []


def test_lookup_by_request_and_decision(backend):
    backend.append(event(1))
    backend.append(event(2))
    assert [e["decision_id"] for e in backend.find_by_request_id("req_1")] == ["dec_1"]
    assert [e["request_id"] for e in backend.find_by_decision_id("dec_2")] == ["req_2"]
    assert backend.find_by_request_id("req_missing") == []


def test_filtering_by_event_type(backend):
    backend.append(event(1, "candidate_observed"))
    backend.append(event(2, "guard_evaluated", decision="PAY"))
    only = list(backend.iter_events(event_type="guard_evaluated"))
    assert [e["event_id"] for e in only] == [e["event_id"] for e in
                                             backend.find_by_decision_id("dec_2")]


def test_round_trip_preserves_the_payload(backend):
    """Unicode, nesting and numbers must survive both stores identically."""
    payload = {"text": "対象企業の決算", "nested": {"a": [1, 2.5, None, True]}, "empty": {}}
    stored = backend.append(event(1, "guard_evaluated", **payload))
    read = list(backend.iter_events())[0]
    assert read["payload"] == stored["payload"] == payload


# -- the two stores agree ---------------------------------------------------


@pytest.mark.postgres
def test_both_backends_produce_identical_hashes(tmp_path, clean_postgres):
    """The same events must hash the same in a file and in a database.

    This is why `event_json` exists: hashing the JSONB column instead would
    fail here, because JSONB reorders keys and renormalises numbers.
    """
    from spend_guard.backends.jsonl import JsonlLedgerBackend
    from spend_guard.backends.postgres import PostgresLedgerBackend

    events = [event(n, "guard_evaluated", note=f"n{n}", score=0.1 * n) for n in range(6)]

    jsonl = JsonlLedgerBackend(tmp_path / "l.jsonl")
    postgres = PostgresLedgerBackend(clean_postgres)
    try:
        from_file = [jsonl.append(json.loads(json.dumps(e))) for e in events]
        from_db = [postgres.append(json.loads(json.dumps(e))) for e in events]
    finally:
        postgres.close()

    assert [e["event_hash"] for e in from_file] == [e["event_hash"] for e in from_db]
    assert [e["previous_event_hash"] for e in from_file] == [e["previous_event_hash"] for e in from_db]


# -- the rest of the system runs on either store ---------------------------


@pytest.fixture
def engine(backend):
    return ShadowEngine(support.policy(), support.judge(), Ledger(backend=backend))


def test_the_full_pipeline_runs_on_either_backend(engine):
    decisions = [engine.process(support.candidate(n)) for n in support.candidates()]
    assert [d.decision for d in decisions if d] == [
        "PAY", "HOLD", "HOLD", "HOLD", "REVIEW", "REVIEW", "REVIEW", "REVIEW", "REVIEW"
    ]


def test_replay_is_identical_on_either_backend(engine):
    for name in support.candidates():
        engine.process(support.candidate(name))
    result = replay(engine.ledger, support.policy())
    assert result["diff"]["changed"] == 0
    assert result["counts"] == {"PAY": 1, "HOLD": 3, "REVIEW": 5}


def test_report_and_feedback_run_on_either_backend(engine):
    decision = engine.process(support.candidate("cand_off_task"))
    engine.ledger.append(
        "human_feedback",
        decision_id=decision.decision_id,
        candidate_id=decision.candidate_id,
        request_id=decision.request_id,
        payload={"label": "not_needed"},
    )
    data = report(engine.ledger, support.policy())
    assert data["decisions"]["counts"]["HOLD"] == 1
    assert data["human_comparison"]["labelled"] == 1
    assert data["human_comparison"]["not_needed_held"] == 1.0


def test_outcomes_and_sampling_run_on_either_backend(engine):
    decision = engine.process(support.candidate("cand_pay_clean"))
    OutcomeRecorder(engine.ledger).purchase_result(
        decision.request_id, payment_status="success", http_status=200
    )
    result = sample(engine.ledger, pay_rate=1.0, seed=1)
    assert result["selected"] == 1
    assert result["items"][0]["decision_id"] == decision.decision_id
