"""Attaching real purchase and delivery results to a shadow decision (chapter 12).

Nothing here changes the existing payment flow. It only records what that flow
already did, keyed back to the decision the guard recorded beforehand, so the
two can be compared.

Correlation is by `request_id`, because that is the only identifier the host
flow is assumed to already carry from candidate to result. When a host turns
out not to carry one, `link_by_decision_id` is the fallback and the host must
hand back the `decision_id` instead; see docs/design.md.
"""

from __future__ import annotations

from typing import Any

from .errors import LedgerError
from .ledger import Ledger

PAYMENT_STATUSES = ("success", "failure", "unknown")


def _decision_for_request(ledger: Ledger, request_id: str) -> tuple[str, str] | None:
    """Find the decision recorded for a request id, newest wins."""
    found: tuple[str, str] | None = None
    for event in ledger.iter_events():
        if event.get("request_id") == request_id and event.get("event_type") == "guard_evaluated":
            found = (str(event.get("decision_id")), str(event.get("candidate_id")))
    return found


class OutcomeRecorder:
    """Appends the facts the existing flow produced. Never blocks it."""

    def __init__(self, ledger: Ledger):
        self.ledger = ledger

    def _append(self, event_type: str, request_id: str, payload: dict[str, Any],
                *, decision_id: str | None = None, candidate_id: str | None = None) -> dict[str, Any]:
        if decision_id is None:
            link = _decision_for_request(self.ledger, request_id)
            if link is None:
                raise LedgerError(f"no guard decision recorded for request {request_id}")
            decision_id, candidate_id = link
        return self.ledger.append(
            event_type,
            decision_id=decision_id,
            candidate_id=candidate_id or "",
            request_id=request_id,
            payload=payload,
        )

    def purchase_attempted(self, request_id: str, **payload: Any) -> dict[str, Any]:
        return self._append("purchase_attempted", request_id, {"attempted": True, **payload})

    def purchase_result(
        self,
        request_id: str,
        *,
        payment_status: str,
        http_status: int | None = None,
        result_class: str | None = None,
        response_received: bool | None = None,
        **payload: Any,
    ) -> dict[str, Any]:
        """Record the minimum chapter 12 asks for.

        `payment_status` allows "unknown" on purpose: a request that timed out
        after broadcasting is genuinely undetermined, and recording it as a
        failure would understate double-spend risk.
        """
        if payment_status not in PAYMENT_STATUSES:
            raise LedgerError(f"payment_status must be one of {PAYMENT_STATUSES}")
        return self._append(
            "purchase_result",
            request_id,
            {
                "payment_status": payment_status,
                "http_status": http_status,
                "result_class": result_class,
                "response_received": response_received,
                **payload,
            },
        )

    def delivery_observed(
        self,
        request_id: str,
        *,
        required_fields_present: bool | None = None,
        period_matches: bool | None = None,
        **payload: Any,
    ) -> dict[str, Any]:
        """Record only mechanical checks of what arrived.

        Judging whether the delivered content was any good is Delivery Review's
        job and is out of scope here; recording a quality verdict from this MVP
        would suggest an evaluation that has not happened.
        """
        return self._append(
            "delivery_observed",
            request_id,
            {
                "required_fields_present": required_fields_present,
                "period_matches": period_matches,
                **payload,
            },
        )

    def link_by_decision_id(self, decision_id: str, event_type: str, **payload: Any) -> dict[str, Any]:
        """Fallback when the host flow does not carry a request id end to end."""
        candidate_id, request_id = "", None
        for event in self.ledger.iter_events():
            if event.get("decision_id") == decision_id:
                candidate_id = str(event.get("candidate_id"))
                request_id = event.get("request_id")
        if not candidate_id:
            raise LedgerError(f"no ledger event for decision {decision_id}")
        return self.ledger.append(
            event_type,
            decision_id=decision_id,
            candidate_id=candidate_id,
            request_id=request_id,
            payload=dict(payload),
        )
