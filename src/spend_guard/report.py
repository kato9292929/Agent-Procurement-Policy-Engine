"""Replay and aggregate metrics from the ledger (chapters 10 and 14).

Both read the ledger and nothing else. Replay never calls Jev: it re-runs the
pure aggregation over judgments already recorded, so the same ledger and the
same policy always give the same answer.
"""

from __future__ import annotations

from typing import Any

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from .aggregate import aggregate, compare
from .errors import InputError
from .ledger import Ledger
from .models import (
    DeterministicChecks,
    QUESTIONS,
    SEMANTIC_OK,
    SIGNAL_DUPLICATE_REQUEST_ID,
    SIGNAL_EXACT_REPURCHASE,
    SemanticJudgment,
)
from .policy import Policy, parse_duration

FEEDBACK_LABELS = ("needed", "not_needed", "unknown")

# Number of human labels after which the thresholds get their first review.
REVIEW_TARGET_LABELS = 100


def _parse_time(text: Any) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def orphaned_candidates(ledger: Ledger, *, orphan_after: str = "PT15M", now: datetime | None = None) -> dict[str, Any]:
    """Observations whose judgment never arrived.

    A gap here means the guard recorded that a purchase was considered but
    never recorded what it thought, so every rate computed from the ledger is
    understated. Recent candidates are excluded because they may simply still
    be queued.
    """
    window = timedelta(seconds=parse_duration(orphan_after))
    moment = now or datetime.now(timezone.utc)
    judged = {
        e.get("decision_id")
        for e in ledger.iter_events(event_type="guard_evaluated")
    }
    orphans = []
    for event in ledger.iter_events(event_type="candidate_observed"):
        payload = event.get("payload") or {}
        if payload.get("duplicate_of"):
            continue  # a repeat points at an existing decision and is never judged again
        if event.get("decision_id") in judged:
            continue
        occurred = _parse_time(event.get("occurred_at"))
        if occurred is not None and moment - occurred < window:
            continue  # still within the window; it may yet be written
        orphans.append(event.get("decision_id"))
    return {"count": len(orphans), "orphan_after": orphan_after, "decision_ids": orphans[:50]}


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


def report(ledger: Ledger, policy: Policy | None = None) -> dict[str, Any]:
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

    # Label coverage per verdict: HOLD and REVIEW are meant to be labelled in
    # full, PAY only sampled, so one overall rate would hide a gap in either.
    coverage_by_decision = {}
    for verdict in ("PAY", "HOLD", "REVIEW"):
        of_verdict = [d for d, p in evaluations.items() if p.get("decision") == verdict]
        coverage_by_decision[verdict] = {
            "evaluated": len(of_verdict),
            "labelled": sum(1 for d in of_verdict if d in labelled),
            "rate": _rate(sum(1 for d in of_verdict if d in labelled), len(of_verdict)),
        }

    orphan_after = (policy.report.get("orphan_after") if policy else None) or "PT15M"
    orphans = orphaned_candidates(ledger, orphan_after=orphan_after)

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
            "labels_until_threshold_review": max(0, REVIEW_TARGET_LABELS - len(labelled)),
            "review_target_labels": REVIEW_TARGET_LABELS,
            "label_coverage_by_decision": coverage_by_decision,
            "not_needed_held": share(not_needed, "HOLD"),
            "needed_held": share(needed, "HOLD"),
            "labelled_reviewed": share(list(labelled), "REVIEW"),
        },
        "integrity": {
            "orphaned_candidates": orphans["count"],
            "orphan_after": orphans["orphan_after"],
            "orphaned_decision_ids": orphans["decision_ids"],
            "dropped_candidates": "see host log (spend_guard.dropped)",
            "unflushed_at_shutdown": "see host log (spend_guard.unflushed_at_shutdown)",
        },
        "cost_and_performance": {
            "judgment_cost_usd": round(cost_usd, 6) if cost_known else None,
            "input_tokens": tokens_in or None,
            "output_tokens": tokens_out or None,
            "decision_latency_ms": _percentiles(latencies),
            "hook_overhead_ms": _percentiles(hook_overheads),
            "provider_error_rate": _rate(provider_errors, total),
            "missing_input_rate": _rate(missing_input, total),
            "ledger_write_failures": "see host log (spend_guard.dropped reason=write_failed)",
        },
        "notes": [
            "Dropped candidates, shutdown losses and write failures cannot be derived "
            "from the ledger: a candidate that was never written leaves no trace in it "
            "by definition. They are counted in ShadowGuard.counters and emitted to the "
            "host log. orphaned_candidates is the ledger-visible symptom of the same "
            "problem, and is computed here.",
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


# -- E-1: choosing what a person should look at -----------------------------


def _candidate_details(ledger: Ledger) -> dict[str, dict[str, Any]]:
    """Task and service description per decision, for the review sheet."""
    found: dict[str, dict[str, Any]] = {}
    for event in ledger.iter_events(event_type="candidate_observed"):
        decision_id = event.get("decision_id")
        candidate = ((event.get("payload") or {}).get("candidate")) or {}
        if isinstance(decision_id, str):
            found[decision_id] = candidate
    return found


def _sample_score(decision_id: str, salt: str) -> float:
    """Stable 0..1 value per decision.

    Hashing rather than shuffling makes the choice independent of ledger
    order, so the same seed picks the same purchases even after more events
    are appended.
    """
    digest = hashlib.sha256(f"{salt}:{decision_id}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


def sample(
    ledger: Ledger,
    *,
    since: str | None = None,
    pay_rate: float = 0.2,
    seed: int | None = None,
) -> dict[str, Any]:
    """Pick the decisions a person should label this week.

    `HOLD` and `REVIEW` are taken in full and `PAY` is sampled, because the
    purpose is to catch purchases that should have been stopped and ones that
    should not have been - a full `PAY` census would cost far more labelling
    for far less signal.

    Only decisions whose purchase actually completed are offered: without a
    result there is nothing for a person to judge. Already-labelled decisions
    are skipped so a weekly run never re-asks the same question.
    """
    if not 0.0 <= pay_rate <= 1.0:
        raise InputError("--pay-rate must be between 0 and 1")
    cutoff = _parse_time(since) if since else None
    if since and cutoff is None:
        raise InputError(f"--since is not a valid timestamp: {since!r}")

    evaluations = _evaluations(ledger)
    labelled = set(_feedback(ledger))
    purchases = _purchases(ledger)
    candidates = _candidate_details(ledger)
    salt = str(seed) if seed is not None else secrets.token_hex(8)

    selected: list[dict[str, Any]] = []
    skipped = {"already_labelled": 0, "no_purchase_result": 0, "before_since": 0, "not_sampled": 0}

    for decision_id, payload in sorted(evaluations.items()):
        if decision_id in labelled:
            skipped["already_labelled"] += 1
            continue
        result = purchases.get(decision_id)
        if not result or "payment_status" not in result:
            skipped["no_purchase_result"] += 1
            continue
        created = _parse_time(payload.get("created_at"))
        if cutoff is not None and created is not None and created < cutoff:
            skipped["before_since"] += 1
            continue

        verdict = str(payload.get("decision"))
        if verdict == "PAY" and _sample_score(decision_id, salt) >= pay_rate:
            skipped["not_sampled"] += 1
            continue

        candidate = candidates.get(decision_id) or {}
        service = candidate.get("service") or {}
        task = candidate.get("task") or {}
        selected.append(
            {
                "decision_id": decision_id,
                "decision": verdict,
                "reason_codes": list(payload.get("reason_codes") or []),
                "created_at": payload.get("created_at"),
                "task_purpose": task.get("purpose"),
                "required_data": list(task.get("required_data") or []),
                "provider_id": service.get("provider_id"),
                "service_id": service.get("service_id"),
                "route_id": service.get("route_id"),
                "service_description": service.get("description"),
                "payment_status": result.get("payment_status"),
                "response_received": result.get("response_received"),
                "required_fields_present": result.get("required_fields_present"),
                "label_command": (
                    f"guard feedback --decision-id {decision_id} --label needed|not_needed|unknown"
                ),
            }
        )

    counts = _counts(item["decision"] for item in selected)
    return {
        "schema_version": "1",
        "since": since,
        "pay_rate": pay_rate,
        "seed": seed,
        "deterministic": seed is not None,
        "selected": len(selected),
        "selected_by_decision": counts,
        "skipped": skipped,
        "items": selected,
    }
