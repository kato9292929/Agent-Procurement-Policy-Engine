"""Chapter 8 aggregation: checks plus judgments in, PAY/HOLD/REVIEW out.

The first matching rule decides; every matching rule contributes a reason
code, so the record shows all of why, not just the winning rule. Deterministic
results and Jev scores are never averaged into one number.

This function is pure. `guard replay` re-runs it over stored records with a
different policy, which is only sound because it touches nothing but its
arguments.
"""

from __future__ import annotations

from typing import Any

from .models import (
    QUESTIONS,
    SEMANTIC_INVALID_RESPONSE,
    SEMANTIC_OK,
    SEMANTIC_PROVIDER_ERROR,
    SEMANTIC_SKIPPED,
    SEMANTIC_TIMEOUT,
    SIGNAL_DUPLICATE_REQUEST_ID,
    SIGNAL_EXACT_REPURCHASE,
    DeterministicChecks,
    SemanticJudgment,
)
from .policy import Policy

SEMANTIC_FAILURES = (SEMANTIC_PROVIDER_ERROR, SEMANTIC_INVALID_RESPONSE, SEMANTIC_TIMEOUT)


def aggregate(
    checks: DeterministicChecks,
    judgment: SemanticJudgment,
    policy: Policy,
) -> tuple[str, tuple[str, ...]]:
    """Return `(decision, reason_codes)`.

    Rules run in the order chapter 8 lists them. Rules 2 and 3 fire on
    deterministic signals; Jev is still called for those candidates so that
    the two kinds of judgment can be compared later, which is why its reason
    codes are collected here even when a deterministic rule already decided.
    """
    thresholds = policy.thresholds
    reasons: list[str] = []
    decision: str | None = None

    def settle(value: str, code: str) -> None:
        nonlocal decision
        reasons.append(code)
        if decision is None:
            decision = value

    # 1. Missing required input or a failed check: Jev is not consulted at all.
    if checks.status != "PASS" or SEMANTIC_SKIPPED == judgment.status:
        if checks.status != "PASS":
            for code in checks.failed:
                settle("REVIEW", code)
            if not checks.failed:
                settle("REVIEW", "DETERMINISTIC_CHECK_FAILED")
        else:
            settle("REVIEW", "SEMANTIC_SKIPPED")
        return decision or "REVIEW", tuple(reasons)

    # 2. A request id reappearing without evidence of a retry: possible double spend.
    if SIGNAL_DUPLICATE_REQUEST_ID in checks.signals:
        settle("REVIEW", SIGNAL_DUPLICATE_REQUEST_ID)

    # 3. The identical request, bought again inside the window.
    if SIGNAL_EXACT_REPURCHASE in checks.signals:
        settle("HOLD", SIGNAL_EXACT_REPURCHASE)

    # 4. Jev unavailable, slow, or unintelligible. Never scored as a low answer.
    if judgment.status in SEMANTIC_FAILURES:
        settle("REVIEW", judgment.status)
        return decision or "REVIEW", tuple(reasons)
    if judgment.status != SEMANTIC_OK:
        settle("REVIEW", "SEMANTIC_STATUS_UNKNOWN")
        return decision or "REVIEW", tuple(reasons)

    missing_scores = [n for n in QUESTIONS if not isinstance(judgment.scores.get(n), (int, float))]
    if missing_scores:
        settle("REVIEW", "SEMANTIC_SCORE_MISSING")
        return decision or "REVIEW", tuple(reasons)

    # 5. Any answer the model is not confident in. Not forced to a binary.
    floor = thresholds["confidence_min"]
    low = [n for n in QUESTIONS if float(judgment.confidence.get(n, 0.0)) < floor]
    if low:
        for name in low:
            settle("REVIEW", f"LOW_CONFIDENCE_{name.upper()}")

    # 6. Semantically redundant with what is already held.
    if float(judgment.scores["duplication_risk"]) >= thresholds["duplication_risk_max"]:
        settle("HOLD", "DUPLICATION_RISK")

    # 7. Not enough evidence to judge the purchase at all.
    if float(judgment.scores["evidence_sufficiency"]) < thresholds["evidence_sufficiency_min"]:
        settle("REVIEW", "EVIDENCE_INSUFFICIENT")

    # 8. Off-task, or adding nothing on top of what is held.
    if float(judgment.scores["task_fit"]) < thresholds["task_fit_min"]:
        settle("HOLD", "TASK_MISMATCH")
    if float(judgment.scores["incremental_value"]) < thresholds["incremental_value_min"]:
        settle("HOLD", "NO_INCREMENTAL_VALUE")

    # 9. Everything clears.
    if decision is None:
        return "PAY", ("TASK_MATCH", "INCREMENTAL_VALUE", "NOT_DUPLICATE", "EVIDENCE_SUFFICIENT")
    return decision, tuple(reasons)


def compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Summarise how a replay changed things, for `guard replay` output."""
    changed = [
        {
            "decision_id": key,
            "from": before[key],
            "to": after[key],
        }
        for key in sorted(before)
        if key in after and before[key] != after[key]
    ]
    matrix: dict[str, int] = {}
    for item in changed:
        matrix[f"{item['from']}->{item['to']}"] = matrix.get(f"{item['from']}->{item['to']}", 0) + 1
    return {"total": len(before), "changed": len(changed), "transitions": matrix, "details": changed}
