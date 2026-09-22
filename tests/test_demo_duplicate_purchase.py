"""The demo case: the same route twice in one run lands on HOLD.

Built from the real route in the agent's `spend-guard-endpoints.json`, so the
demo cannot drift away from the configuration it claims to exercise. The
config's `demo.duplicate_purchase` switch is off by default; this asserts that
too, because a demo switch that ships enabled would make every ordinary run
buy something twice.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import support
from spend_guard.engine import ShadowEngine
from spend_guard.judges.fixture import FixtureProcurementJudge
from spend_guard.ledger import Ledger
from spend_guard.models import SIGNAL_EXACT_REPURCHASE
from spend_guard.policy import Policy

AA_CONFIG = Path("/home/user/x402-autonomous-agent-/config/spend-guard-endpoints.json")
LOCAL_COPY = support.ROOT / "fixtures" / "candidates" / "aa-demo-duplicate.jsonl"

# Jev sees two different states: the second candidate carries the first
# purchase in prior_acquisitions, so it answers differently.
ANSWERS = {
    "aa_demo_dup_1": {"task_fit": 0.91, "incremental_value": 0.86, "duplication_risk": 0.05,
                      "evidence_sufficiency": 0.84, "confidence": 0.9,
                      "usage": {"input_tokens": 760, "output_tokens": 145}},
    "aa_demo_dup_2": {"task_fit": 0.90, "incremental_value": 0.09, "duplication_risk": 0.93,
                      "evidence_sufficiency": 0.83, "confidence": 0.9,
                      "usage": {"input_tokens": 810, "output_tokens": 145}},
}


def load_pair() -> list[dict]:
    """The committed pair, so this runs without the agent's repo checked out."""
    return [json.loads(line) for line in LOCAL_COPY.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture
def engine(tmp_path):
    return ShadowEngine(
        Policy.load(support.ROOT / "policies" / "shadow-v2.json"),
        FixtureProcurementJudge(ANSWERS),
        Ledger(tmp_path / "ledger.jsonl"),
    )


def test_the_first_purchase_is_pay(engine):
    first, _ = load_pair()
    decision = engine.process(first)
    assert decision.decision == "PAY"
    assert decision.deterministic_checks.signals == ()


def test_the_repeat_is_held_on_exact_repurchase(engine):
    first, second = load_pair()
    engine.process(first)
    decision = engine.process(second)
    assert decision.decision == "HOLD"
    assert SIGNAL_EXACT_REPURCHASE in decision.deterministic_checks.signals
    # Rule 3 has to be the rule that decided, not a semantic score that happened
    # to agree: the demo is about the deterministic check firing.
    assert decision.reason_codes[0] == SIGNAL_EXACT_REPURCHASE


def test_the_repeat_is_still_recorded_not_blocked(engine):
    """Shadow mode does not stop the second payment; it records the verdict."""
    first, second = load_pair()
    engine.process(first)
    engine.process(second)
    verdicts = [e["payload"]["decision"] for e in engine.ledger.read()
                if e["event_type"] == "guard_evaluated"]
    assert verdicts == ["PAY", "HOLD"], "both purchases are evaluated and recorded"


def test_the_pair_matches_a_real_route(engine):
    """Both candidates use one real route's identifiers and its real price."""
    first, second = load_pair()
    for field in ("provider_id", "service_id", "route_id"):
        assert first["service"][field] == second["service"][field]
    assert first["service"]["request_params_hash"] == second["service"]["request_params_hash"]
    assert first["quote"] == second["quote"]
    assert first["quote"]["currency"] == "USDC"


def test_the_window_is_what_makes_it_fire(engine):
    """Outside exact_repurchase_window the same pair is not a repurchase."""
    first, second = load_pair()
    stale = json.loads(json.dumps(second))
    stale["candidate_id"] = "aa_demo_dup_stale"
    stale["request_id"] = "req_aa_demo_dup_stale"
    stale["prior_acquisitions"][0]["acquired_at"] = "2026-09-20T09:00:00Z"  # 2 days before
    engine.process(first)
    decision = engine.process(stale)
    assert SIGNAL_EXACT_REPURCHASE not in decision.deterministic_checks.signals


@pytest.mark.skipif(not AA_CONFIG.exists(), reason="the agent's repo is not checked out here")
def test_the_committed_pair_still_matches_the_agents_config():
    """Catches the config moving on without the demo pair being regenerated."""
    config = json.loads(AA_CONFIG.read_text(encoding="utf-8"))
    demo = config["demo"]["duplicate_purchase"]
    assert demo["enabled"] is False, "the demo switch must ship off"
    route = next(r for r in config["routes"] if r["route_key"] == demo["route_key"])
    first, _ = load_pair()
    assert first["service"]["provider_id"] == route["provider_id"]
    assert first["service"]["route_id"] == route["route_id"]
    assert first["quote"]["amount"] == route["price"]["amount"]
    params = json.dumps({"url": route["url"], "method": route["http_method"]}, sort_keys=True)
    expected = "sha256:" + hashlib.sha256(params.encode()).hexdigest()
    assert first["service"]["request_params_hash"] == expected
