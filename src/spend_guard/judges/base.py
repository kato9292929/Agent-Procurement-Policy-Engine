"""The one seam every semantic judgment goes through.

Business logic depends on this Protocol, never on Jev's response shape, so a
different model or a local judge can be dropped in without touching the
checks, the aggregation, or the ledger.
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from ..canonical import hash_json
from ..models import Candidate, SemanticJudgment
from ..policy import Policy

# The four narrow questions of chapter 4. Price fairness, counterparty
# trustworthiness and safety are deliberately absent: without comparison data
# a model's answer to those would be a guess recorded as evidence.
#
# This is the shadow-v1 wording. A policy may override any question through its
# `questions` block, which is what makes a rewording a new `policy_version`
# rather than an edit: scores produced by different wordings are not
# comparable, so the wording has to be versioned alongside the thresholds.
QUESTION_SPECS: dict[str, dict[str, object]] = {
    "task_fit": {
        "positive": "fits",
        "instructions": (
            "Does the purchase candidate supply data the stated task requires? "
            "Judge only fit with the stated purpose and required data."
        ),
        "criteria": {
            "fits": "The service provides data the stated task needs.",
            "does_not_fit": "The service does not provide data the stated task needs.",
        },
    },
    "incremental_value": {
        "positive": "adds_value",
        "instructions": (
            "Relative to the information already acquired, would this purchase add "
            "information the task still lacks?"
        ),
        "criteria": {
            "adds_value": "It would supply information not already acquired.",
            "no_new_value": "Everything it would supply is already acquired.",
        },
    },
    "duplication_risk": {
        "positive": "duplicate",
        "instructions": (
            "Would this purchase return substantially the same information as something "
            "already acquired? Judge meaning, including rewording and partial overlap; "
            "identical repeat requests are handled elsewhere."
        ),
        "criteria": {
            "duplicate": "It substantially overlaps information already acquired.",
            "distinct": "It covers information that is materially different.",
        },
    },
    "evidence_sufficiency": {
        "positive": "sufficient",
        "instructions": (
            "Is there enough information here to judge whether this purchase is needed? "
            "Judge the evidence available, not the purchase itself."
        ),
        "criteria": {
            "sufficient": "The stated task and service description support a judgment.",
            "insufficient": "Too little is stated to judge whether the purchase is needed.",
        },
    },
}


def resolve_questions(policy: Policy | None = None) -> dict[str, dict[str, Any]]:
    """The question set this policy asks, defaults merged with its overrides.

    A policy overrides a question by name; anything it leaves out keeps the
    shadow-v1 wording above.
    """
    resolved = {name: dict(spec) for name, spec in QUESTION_SPECS.items()}
    overrides: Mapping[str, Any] = (policy.questions if policy else {}) or {}
    for name, override in overrides.items():
        if name.startswith("_") or name not in resolved:
            continue  # notes and unknown names are ignored, never invented
        if not isinstance(override, dict):
            continue
        merged = dict(resolved[name])
        for field in ("positive", "instructions", "criteria"):
            if field in override:
                merged[field] = override[field]
        resolved[name] = merged
    return resolved


def question_set_hash(policy: Policy | None = None) -> str:
    """Identifies the exact wording a score was produced under.

    Recorded on every judgment: comparing `evidence_sufficiency` across a
    rewording is only meaningful if each score says which wording produced it.
    """
    return hash_json(resolve_questions(policy))


class ProcurementJudge(Protocol):
    def evaluate(self, candidate: Candidate, policy: Policy) -> SemanticJudgment:
        """Answer the four questions, or raise ProviderError.

        Implementations must not raise for a merely uncertain answer: low
        confidence is a result, and chapter 8 turns it into REVIEW.
        """
