"""What shadow-v1 does to the answers Jev actually gave (2026-09-22).

`fixtures/judge/observed-live-2026-09-22.json` holds real answers from
`api.typesafe.ai`, model `jev-1.13.0`, recorded by
`scripts/jev_live_check.py`. Replaying them through the aggregation turns a
one-off observation into something that stays true as the code changes.

These tests assert **the aggregation's behaviour on fixed inputs**, never the
model's behaviour. The numbers are a single sample of five candidates and Jev
drifts about 0.05 between runs, so they cannot calibrate a threshold. What
they can do, and do here, is show that the shipped thresholds are wrong in a
specific, reproducible way.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
import support
from spend_guard.engine import ShadowEngine
from spend_guard.judges.fixture import FixtureProcurementJudge
from spend_guard.ledger import Ledger
from spend_guard.models import QUESTIONS

OBSERVED = support.ROOT / "fixtures" / "judge" / "observed-live-2026-09-22.json"
CANDIDATES = ["cand_pay_clean", "cand_semantic_dupe", "cand_exact_repurchase",
              "cand_off_task", "cand_thin_evidence"]

# What each fixture was built to represent, from the chapter 13 checklist.
INTENDED = {
    "cand_pay_clean": "PAY",
    "cand_semantic_dupe": "HOLD",
    "cand_exact_repurchase": "HOLD",
    "cand_off_task": "HOLD",
    "cand_thin_evidence": "REVIEW",
}


@pytest.fixture
def observed() -> dict:
    return json.loads(OBSERVED.read_text(encoding="utf-8"))


def decide_all(**threshold_overrides) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as tmp:
        engine = ShadowEngine(
            support.policy(thresholds=threshold_overrides),
            FixtureProcurementJudge.from_file(OBSERVED),
            Ledger(Path(tmp) / "ledger.jsonl"),
        )
        return {name: engine.process(support.candidate(name)) for name in CANDIDATES}


# -- the recording itself ---------------------------------------------------


def test_the_observed_answers_have_the_shape_the_adapter_expects(observed):
    for name in CANDIDATES:
        entry = observed[name]
        assert set(QUESTIONS) <= set(entry)
        assert set(entry["confidence"]) == set(QUESTIONS), "a per-question confidence came back"
        assert entry["model"] == "jev-1.13.0"
        assert isinstance(entry["usage"]["input_tokens"], int)


def test_every_observed_value_is_a_probability(observed):
    for name in CANDIDATES:
        entry = observed[name]
        for question in QUESTIONS:
            assert 0.0 <= entry[question] <= 1.0
            assert 0.0 <= entry["confidence"][question] <= 1.0


# -- finding 1: confidence_min is far above the observed distribution -------


def test_more_than_half_the_observed_confidences_fall_below_the_shipped_floor(observed):
    floor = support.policy().thresholds["confidence_min"]
    values = [v for name in CANDIDATES for v in observed[name]["confidence"].values()]
    below = [v for v in values if v < floor]
    assert len(values) == 20
    assert len(below) / len(values) > 0.5, (
        "shadow-v1 assumed most answers would clear confidence_min; they do not"
    )


def test_shadow_v1_sends_almost_everything_to_review_on_real_data():
    """The concrete symptom: four of five, including the clean PAY case."""
    decisions = decide_all()
    verdicts = [d.decision for d in decisions.values()]
    assert verdicts.count("REVIEW") == 4
    assert decisions["cand_pay_clean"].decision == "REVIEW"
    assert "LOW_CONFIDENCE_TASK_FIT" in decisions["cand_pay_clean"].reason_codes


def test_rule_five_fires_on_questions_that_are_not_deciding_anything(observed):
    """The design problem underneath the threshold problem.

    `cand_pay_clean` clears every score threshold, so nothing about the
    purchase is in doubt. It still becomes REVIEW, because rule 5 gates on a
    low confidence in questions that were not going to change the outcome.
    """
    entry = observed["cand_pay_clean"]
    thresholds = support.policy().thresholds
    assert entry["task_fit"] >= thresholds["task_fit_min"]
    assert entry["incremental_value"] >= thresholds["incremental_value_min"]
    assert entry["duplication_risk"] < thresholds["duplication_risk_max"]
    assert entry["evidence_sufficiency"] >= thresholds["evidence_sufficiency_min"]
    # Every score says PAY, yet:
    assert decide_all()["cand_pay_clean"].decision == "REVIEW"


# -- finding 2: evidence_sufficiency answers low, and is unsure about it ----


def test_evidence_sufficiency_scores_low_across_every_candidate(observed):
    """Including the candidate written to be well specified.

    Low scores *and* low confidence together point at the question being read
    differently than intended, rather than at genuinely thin input. Rewording
    it means re-running the live check, so this is recorded, not patched.
    """
    scores = [observed[name]["evidence_sufficiency"] for name in CANDIDATES]
    assert max(scores) < 0.80, "even the best-specified candidate scores low"
    assert sum(1 for s in scores if s < 0.70) >= 4


def test_lowering_confidence_alone_does_not_rescue_the_off_task_case():
    """Rule 7 keeps it in REVIEW, so the floor is not the only thing to fix."""
    decisions = decide_all(confidence_min=0.0)
    assert decisions["cand_off_task"].decision == "REVIEW"
    assert "EVIDENCE_INSUFFICIENT" in decisions["cand_off_task"].reason_codes


# -- the scores themselves are sensible; it is the gating that is not ------


def test_the_model_separated_on_task_from_off_task_decisively(observed):
    assert observed["cand_off_task"]["task_fit"] < 0.1
    assert observed["cand_off_task"]["confidence"]["task_fit"] > 0.9
    assert observed["cand_pay_clean"]["task_fit"] > 0.8


def test_the_model_caught_both_kinds_of_duplication(observed):
    """Semantic overlap and an exact repurchase both scored high."""
    assert observed["cand_semantic_dupe"]["duplication_risk"] > 0.5
    assert observed["cand_exact_repurchase"]["duplication_risk"] > 0.5
    assert observed["cand_pay_clean"]["duplication_risk"] < 0.1


def test_the_candidate_policy_recovers_the_clean_pay_case():
    """shadow-v2-candidate exists to be replayed, not to be deployed."""
    candidate = json.loads(
        (support.ROOT / "policies" / "shadow-v2-candidate.json").read_text(encoding="utf-8")
    )
    decisions = decide_all(**candidate["thresholds"])
    assert decisions["cand_pay_clean"].decision == "PAY"
    assert candidate["provisional"] is True
