"""Question wording is versioned with the policy (shadow-v2).

Scores produced under different wordings are not comparable, so the wording
has to travel with the `policy_version` and every decision has to record which
set produced it. Otherwise a rewording silently mixes two different questions
into one column of numbers.
"""

from __future__ import annotations

import json

import pytest
import support
from spend_guard.errors import ProviderError
from spend_guard.judges.base import QUESTION_SPECS, resolve_questions, question_set_hash
from spend_guard.judges.jev import build_questions, parse_response, positive_option
from spend_guard.models import QUESTIONS
from spend_guard.policy import Policy

V1 = support.ROOT / "policies" / "shadow-v1.json"
V2 = support.ROOT / "policies" / "shadow-v2.json"


@pytest.fixture
def v1() -> Policy:
    return Policy.load(V1)


@pytest.fixture
def v2() -> Policy:
    return Policy.load(V2)


# -- merging ---------------------------------------------------------------


def test_a_policy_without_overrides_gets_the_default_wording(v1):
    assert resolve_questions(v1) == QUESTION_SPECS


def test_shadow_v2_changes_evidence_sufficiency_and_nothing_else(v1, v2):
    differing = [q for q in QUESTIONS if resolve_questions(v1)[q] != resolve_questions(v2)[q]]
    assert differing == ["evidence_sufficiency"], (
        "the comparison only isolates the rewording if nothing else moved"
    )


def test_shadow_v2_keeps_shadow_v1_thresholds(v1, v2):
    """Changing two things at once would make the A/B uninterpretable."""
    assert v1.thresholds == v2.thresholds


def test_notes_and_unknown_names_in_the_questions_block_are_ignored():
    policy = Policy.from_dict({
        **json.loads(V1.read_text(encoding="utf-8")),
        "questions": {"_note": "prose", "not_a_question": {"instructions": "x"}},
    })
    assert resolve_questions(policy) == QUESTION_SPECS


def test_an_override_may_change_only_one_field():
    policy = Policy.from_dict({
        **json.loads(V1.read_text(encoding="utf-8")),
        "questions": {"task_fit": {"instructions": "reworded"}},
    })
    task_fit = resolve_questions(policy)["task_fit"]
    assert task_fit["instructions"] == "reworded"
    assert task_fit["criteria"] == QUESTION_SPECS["task_fit"]["criteria"]
    assert task_fit["positive"] == QUESTION_SPECS["task_fit"]["positive"]


# -- identifying which wording produced a score ----------------------------


def test_the_two_policies_hash_differently(v1, v2):
    assert question_set_hash(v1) != question_set_hash(v2)


def test_the_hash_is_stable_for_the_same_wording(v1):
    assert question_set_hash(v1) == question_set_hash(Policy.load(V1))


def test_every_decision_records_the_question_set(v1, v2):
    assert v1.as_record()["question_set"] == question_set_hash(v1)
    assert v2.as_record()["question_set"] != v1.as_record()["question_set"]


def test_a_recorded_decision_carries_the_question_set(tmp_path, v2):
    engine = support.engine(tmp_path, the_policy=v2)
    decision = engine.process(support.candidate("cand_pay_clean"))
    assert decision.as_dict()["policy"]["question_set"] == question_set_hash(v2)


# -- the renamed option must be parsed, or every v2 score is wrong ---------


def test_the_positive_option_follows_the_policy(v1, v2):
    assert positive_option("evidence_sufficiency", v1) == "sufficient"
    assert positive_option("evidence_sufficiency", v2) == "provides"


def test_the_v2_answer_is_read_from_its_own_option_name(v2):
    """`provides`, not `sufficient`. Reading the wrong key would invert the score."""
    body = {
        "answers": {
            "task_fit": {"choice": "fits", "probabilities": {"fits": 0.9, "does_not_fit": 0.1},
                         "confidence": 0.9},
            "incremental_value": {"choice": "adds_value",
                                  "probabilities": {"adds_value": 0.9, "no_new_value": 0.1},
                                  "confidence": 0.9},
            "duplication_risk": {"choice": "distinct",
                                 "probabilities": {"duplicate": 0.1, "distinct": 0.9},
                                 "confidence": 0.9},
            "evidence_sufficiency": {"choice": "provides",
                                     "probabilities": {"provides": 0.87, "does_not_provide": 0.13},
                                     "confidence": 0.88},
        },
        "model": "jev-1.13.0",
        "usage": {"input_tokens": 790, "output_tokens": 145},
    }
    assert parse_response(body, v2).scores["evidence_sufficiency"] == pytest.approx(0.87)


def test_a_v1_shaped_answer_is_rejected_under_v2(v2):
    """A stale answer must fail loudly rather than be scored against the wrong key."""
    body = {
        "answers": {
            "task_fit": {"choice": "fits", "probabilities": {"fits": 0.9, "does_not_fit": 0.1},
                         "confidence": 0.9},
            "incremental_value": {"choice": "adds_value",
                                  "probabilities": {"adds_value": 0.9, "no_new_value": 0.1},
                                  "confidence": 0.9},
            "duplication_risk": {"choice": "distinct",
                                 "probabilities": {"duplicate": 0.1, "distinct": 0.9},
                                 "confidence": 0.9},
            "evidence_sufficiency": {"choice": "sufficient",
                                     "probabilities": {"sufficient": 0.9, "insufficient": 0.1},
                                     "confidence": 0.9},
        },
        "model": "jev-1.13.0",
    }
    with pytest.raises(ProviderError) as caught:
        parse_response(body, v2)
    assert caught.value.kind == "INVALID_RESPONSE"


def test_the_request_carries_the_policy_wording(v2):
    sent = build_questions(v2)["evidence_sufficiency"]
    assert set(sent["criteria"]) == {"provides", "does_not_provide"}
    assert "required data" in sent["instructions"]
    assert sent["type"] == "choice"


def test_the_reworded_question_is_answerable_from_the_input_alone(v2):
    """The whole point of the rewording: it names fields Jev actually receives."""
    text = resolve_questions(v2)["evidence_sufficiency"]["instructions"].lower()
    assert "description" in text and "required" in text
    for absent in ("price", "reputation", "worthwhile"):
        assert f"consider {absent}" not in text or "do not consider" in text


# -- cost ------------------------------------------------------------------


def test_cost_is_computed_now_that_a_price_is_configured(v2):
    from spend_guard.judges.jev import apply_pricing

    usage = apply_pricing({"input_tokens": 750, "output_tokens": 145, "cost_usd": None},
                          v2.pricing)
    assert usage["cost_usd"] == pytest.approx(750 * 0.042 / 1_000_000)
    assert v2.pricing["output_per_mtok_usd"] == 0.0


def test_the_price_records_where_it_came_from(v2):
    source = v2.pricing["source"]
    assert "2026-09-22" in source
    assert "0.042" in source
    # It was relayed, not read from the vendor by this code; say so.
    assert "not independently verified" in source.lower()
