"""Replay and aggregate metrics from the ledger (chapters 10 and 14).

Both read the ledger and nothing else. Replay never calls Jev: it re-runs the
pure aggregation over judgments already recorded, so the same ledger and the
same policy always give the same answer.
"""

from __future__ import annotations

from typing import Any

from .aggregate import aggregate, compare
from .ledger import Ledger
from .models import (
    DeterministicChecks,
    QUESTIONS,
    SEMANTIC_OK,
    SIGNAL_DUPLICATE_REQUEST_ID,
    SIGNAL_EXACT_REPURCHASE,
    SemanticJudgment,
)
from .policy import Policy

FEEDBACK_LABELS = ("needed", "not_needed", "unknown")


def _evaluations(ledger: Ledger) -> dict[str, dict[str, Any]]:
    """Latest `guard_evaluated` payload per decision, oldest events first."""
    found: dict[str, dict[str, Any]] = {}
    for event in ledger.iter_events():
        if event.get("event_type") == "guard_evaluated":
            decision_id = event.get("decision_id")
            if isinstance(decision_id, str):
                found[decision_id] = event.get("payload") or {}
    return found


def _feedback(ledger: Ledger) -> dict[str, dict[str, Any]]:
    """Latest human label per decision. Earlier labels stay in the ledger."""
    found: dict[str, dict[str, Any]] = {}
    for event in ledger.iter_events():
        if event.get("event_type") == "human_feedback":
            decision_id = event.get("decision_id")
            if isinstance(decision_id, str):
                found[decision_id] = event.get("payload") or {}
    return found


def _purchases(ledger: Ledger) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for event in ledger.iter_events():
        if event.get("event_type") in ("purchase_attempted", "purchase_result"):
            decision_id = event.get("decision_id")
            if isinstance(decision_id, str):
                found.setdefault(decision_id, {}).update(event.get("payload") or {})
    return found


def replay(ledger: Ledger, policy: Policy) -> dict[str, Any]:
    """Re-aggregate every recorded evaluation under `policy`.

    Deterministic and offline by construction: the only inputs are the stored
    `deterministic_checks` and `semantic_judgments` blocks.
    """
    before: dict[str, str] = {}
    after: dict[str, str] = {}
    replayed: dict[str, dict[str, Any]] = {}
    for decision_id, payload in _evaluations(ledger).items():
        checks = DeterministicChecks.from_dict(payload.get("deterministic_checks") or {})
        judgment = SemanticJudgment.from_dict(payload.get("semantic_judgments") or {})
        verdict, reasons = aggregate(checks, judgment, policy)
        before[decision_id] = str(payload.get("decision"))
        after[decision_id] = verdict
        replayed[decision_id] = {"decision": verdict, "reason_codes": list(reasons)}
    return {
        "policy_version": policy.policy_version,
        "counts": _counts(after.values()),
        "diff": compare(before, after),
        "decisions": replayed,
    }


def _counts(values: Any) -> dict[str, int]:
    counts = {"PAY": 0, "HOLD": 0, "REVIEW": 0}
    for value in values:
        if value in counts:
            counts[value] += 1
    return counts


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def _percentiles(samples: list[float]) -> dict[str, float | None]:
    if not samples:
        return {"p50": None, "p95": None}
    ordered = sorted(samples)

    def pick(fraction: float) -> float:
        index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
        return round(ordered[index], 3)

    return {"p50": pick(0.50), "p95": pick(0.95)}


def report(ledger: Ledger) -> dict[str, Any]:
    """The chapter 14 metrics.

    REVIEW is counted separately from HOLD throughout: a candidate sent to a
    person is not the same outcome as one the guard would have stopped.
    """
    events = ledger.read()
    evaluations = _evaluations(ledger)
    feedback = _feedback(ledger)
    purchases = _purchases(ledger)

    observed = sum(1 for e in events if e.get("event_type") == "candidate_observed")
    repeats = sum(
        1
        for e in events
        if e.get("event_type") == "candidate_observed" and (e.get("payload") or {}).get("duplicate_of")
    )

    counts = {"PAY": 0, "HOLD": 0, "REVIEW": 0}
    review_reasons: dict[str, int] = {}
    signals = {SIGNAL_DUPLICATE_REQUEST_ID: 0, SIGNAL_EXACT_REPURCHASE: 0}
    provider_errors = 0
    missing_input = 0
    disagreements = 0
    latencies: list[float] = []
    hook_overheads: list[float] = []
    cost_usd = 0.0
    cost_known = False
    tokens_in = tokens_out = 0

    for payload in evaluations.values():
        decision = str(payload.get("decision"))
        if decision in counts:
            counts[decision] += 1
        checks = payload.get("deterministic_checks") or {}
        judgment = payload.get("semantic_judgments") or {}
        for signal in checks.get("signals") or []:
            if signal in signals:
                signals[signal] += 1
        if decision == "REVIEW":
            for code in payload.get("reason_codes") or []:
                review_reasons[str(code)] = review_reasons.get(str(code), 0) + 1
        if judgment.get("status") in ("PROVIDER_ERROR", "INVALID_RESPONSE", "TIMEOUT"):
            provider_errors += 1
        if checks.get("missing_fields"):
            missing_input += 1
        # Deterministic and semantic verdicts disagreeing is worth watching:
        # an exact repurchase that Jev scores as clearly distinct means one of
        # the two is looking at the wrong thing.
        if SIGNAL_EXACT_REPURCHASE in (checks.get("signals") or []):
            risk = judgment.get("duplication_risk")
            if isinstance(risk, (int, float)) and risk < 0.5:
                disagreements += 1
        if isinstance(payload.get("latency_ms"), (int, float)):
            latencies.append(float(payload["latency_ms"]))
        usage = judgment.get("usage") or {}
        if isinstance(usage.get("cost_usd"), (int, float)):
            cost_usd += float(usage["cost_usd"])
            cost_known = True
        for name, bucket in (("input_tokens", "in"), ("output_tokens", "out")):
            value = usage.get(name)
            if isinstance(value, int):
                if bucket == "in":
                    tokens_in += value
                else:
                    tokens_out += value

    for event in events:
        if event.get("event_type") == "candidate_observed":
            overhead = (event.get("payload") or {}).get("hook_overhead_ms")
            if isinstance(overhead, (int, float)):
                hook_overheads.append(float(overhead))

    total = sum(counts.values())
    purchased = sum(1 for p in purchases.values() if p.get("payment_status") == "success")

    labelled = {d: f.get("label") for d, f in feedback.items() if d in evaluations}
    not_needed = [d for d, label in labelled.items() if label == "not_needed"]
    needed = [d for d, label in labelled.items() if label == "needed"]

    def share(ids: list[str], verdict: str) -> float | None:
        return _rate(sum(1 for d in ids if evaluations[d].get("decision") == verdict), len(ids))

    return {
        "schema_version": "1",
        "decisions": {
            "candidates_observed": observed,
            "repeat_observations": repeats,
            "evaluated": total,
            "counts": counts,
            "rates": {k: _rate(v, total) for k, v in counts.items()},
            "review_reasons": dict(sorted(review_reasons.items(), key=lambda kv: -kv[1])),
            "signals": signals,
            "deterministic_vs_semantic_disagreements": disagreements,
        },
        "human_comparison": {
            "purchases_succeeded": purchased,
            "feedback_coverage": _rate(len(labelled), total),
            "labelled": len(labelled),
            "not_needed_held": share(not_needed, "HOLD"),
            "needed_held": share(needed, "HOLD"),
            "labelled_reviewed": share(list(labelled), "REVIEW"),
        },
        "cost_and_performance": {
            "judgment_cost_usd": round(cost_usd, 6) if cost_known else None,
            "input_tokens": tokens_in or None,
            "output_tokens": tokens_out or None,
            "decision_latency_ms": _percentiles(latencies),
            "hook_overhead_ms": _percentiles(hook_overheads),
            "provider_error_rate": _rate(provider_errors, total),
            "missing_input_rate": _rate(missing_input, total),
            "ledger_write_failures": None,
        },
        "notes": [
            "ledger_write_failures is not derivable from the ledger itself; it comes "
            "from the host's own log (see chapter 6 fail-open behaviour).",
            "Savings alone is not a success metric: read needed_held together with "
            "counts before changing a threshold.",
        ],
    }


def score_distribution(ledger: Ledger) -> dict[str, Any]:
    """Raw pre-aggregation scores, for choosing thresholds from real output.

    Chapter 8 rules out picking thresholds from fixtures: fixture scores are
    invented, so they say nothing about how the model's output is distributed.
    """
    buckets: dict[str, list[float]] = {name: [] for name in QUESTIONS}
    confidences: dict[str, list[float]] = {name: [] for name in QUESTIONS}
    for payload in _evaluations(ledger).values():
        judgment = payload.get("semantic_judgments") or {}
        if judgment.get("status") != SEMANTIC_OK:
            continue
        confidence = judgment.get("confidence") or {}
        for name in QUESTIONS:
            if isinstance(judgment.get(name), (int, float)):
                buckets[name].append(float(judgment[name]))
            if isinstance(confidence.get(name), (int, float)):
                confidences[name].append(float(confidence[name]))
    return {
        "scores": {name: _summary(values) for name, values in buckets.items()},
        "confidence": {name: _summary(values) for name, values in confidences.items()},
    }


def _summary(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0, "min": None, "p50": None, "max": None}
    ordered = sorted(values)
    return {
        "n": len(ordered),
        "min": round(ordered[0], 4),
        "p50": round(ordered[len(ordered) // 2], 4),
        "max": round(ordered[-1], 4),
    }
