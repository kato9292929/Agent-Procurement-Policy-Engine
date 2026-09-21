"""Deterministic checks (chapter 4).

These never mix with Jev's scores. Money and irreversible operations keep a
code-owned check that does not depend on a model being reachable or calibrated.

In shadow mode a failed check still does not stop a payment; it only decides
what chapter 8 aggregates.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Protocol

from .models import (
    Candidate,
    DeterministicChecks,
    SIGNAL_DUPLICATE_REQUEST_ID,
    SIGNAL_EXACT_REPURCHASE,
    SIGNAL_RETRY_OBSERVED,
)
from .policy import Policy


class PriorRequestLookup(Protocol):
    """What the checks need from the ledger, kept narrow so tests can fake it."""

    def prior_request(
        self, request_id: str, *, exclude_decision_id: str | None = None
    ) -> dict[str, Any] | None:
        """The earliest record for `request_id`, or None when it is new.

        `exclude_decision_id` drops the decision being evaluated right now.
        Its own `candidate_observed` event is already in the ledger by the time
        the checks run, so without this every candidate matches itself.
        """


def _parse_time(text: str | None) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _is_retry(candidate: Candidate, prior: dict[str, Any], policy: Policy) -> bool:
    """Tell a retry of one purchase from a second purchase reusing an id.

    Chapter 4 requires this to follow whatever the host flow already does. No
    host flow exists yet (see docs/design.md), so the only evidence accepted is
    an idempotency key the host itself set, or an explicit `retry_of` marker.
    Anything else is treated as a possible double spend, which is the
    conservative side: it produces a REVIEW, never a silent pass.
    """
    if policy.retry_detection == "none":
        return False
    if candidate.retry_of:
        return True
    key = candidate.idempotency_key
    return bool(key) and key == prior.get("idempotency_key")


def _exact_repurchase(candidate: Candidate, policy: Policy) -> bool:
    """Same provider, service, route and request parameters, bought recently.

    Compared against the `prior_acquisitions` snapshot rather than the live
    ledger, so an asynchronous evaluation cannot match the candidate against
    its own purchase (chapter 5).
    """
    params_hash = candidate.service.get("request_params_hash")
    if not params_hash:
        return False  # without it, "identical request" cannot be established
    observed = _parse_time(candidate.observed_at)
    window = policy.repurchase_window_seconds
    for prior in candidate.prior_acquisitions:
        if (
            prior.provider_id != candidate.service.get("provider_id")
            or prior.service_id != candidate.service.get("service_id")
            or prior.route_id != candidate.service.get("route_id")
            or prior.request_params_hash != params_hash
        ):
            continue
        acquired = _parse_time(prior.acquired_at)
        if observed is None or acquired is None:
            return True  # identical request, unknown age: report it
        if 0 <= (observed - acquired).total_seconds() <= window:
            return True
    return False


def run(
    candidate: Candidate,
    policy: Policy,
    *,
    lookup: PriorRequestLookup | None = None,
    decision_id: str | None = None,
) -> DeterministicChecks:
    """Run every deterministic check and collect the result.

    A check that raises is recorded as a failure rather than propagated: the
    guard must not become a new way for the payment path to break.
    """
    failed: list[str] = []
    signals: list[str] = []
    missing = list(candidate.missing_fields)

    blocking = [f for f in missing if f in _REQUIRED]
    if blocking:
        failed.append("MISSING_REQUIRED_FIELDS")

    try:
        if candidate.request_id and lookup is not None:
            prior = lookup.prior_request(
                candidate.request_id, exclude_decision_id=decision_id
            )
            if prior is not None:
                signals.append(
                    SIGNAL_RETRY_OBSERVED
                    if _is_retry(candidate, prior, policy)
                    else SIGNAL_DUPLICATE_REQUEST_ID
                )
    except Exception:  # noqa: BLE001 - a ledger problem is a check failure, not a crash
        failed.append("REQUEST_ID_CHECK_FAILED")

    try:
        if _exact_repurchase(candidate, policy):
            signals.append(SIGNAL_EXACT_REPURCHASE)
    except Exception:  # noqa: BLE001
        failed.append("REPURCHASE_CHECK_FAILED")

    status = "FAIL" if failed else "PASS"
    return DeterministicChecks(
        status=status,
        failed=tuple(failed),
        missing_fields=tuple(missing),
        signals=tuple(signals),
    )


def _required() -> Iterable[str]:
    from .normalize import REQUIRED_FIELDS

    return REQUIRED_FIELDS


_REQUIRED = frozenset(_required())
