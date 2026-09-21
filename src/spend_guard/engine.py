"""Wiring: normalise, check, judge, aggregate, record.

Nothing here decides whether a payment happens. In shadow mode the decision is
written to the ledger beside whatever the existing flow did, so the two can be
compared later.
"""

from __future__ import annotations

import time
from typing import Any

from .aggregate import aggregate
from .checks import run as run_checks
from .errors import ProviderError
from .judges.base import ProcurementJudge
from .ledger import Ledger, LedgerIndex
from .models import (
    Candidate,
    Decision,
    DeterministicChecks,
    SemanticJudgment,
)
from .normalize import input_hash, new_id, normalize, utc_now
from .policy import Policy


def semantic_failure(error: ProviderError) -> SemanticJudgment:
    """Turn a provider failure into a status, never into a low score.

    A low `task_fit` and an unreachable Jev mean opposite things; collapsing
    them would hide outages inside ordinary-looking HOLDs.
    """
    return SemanticJudgment(status=error.kind)


class ShadowEngine:
    """Evaluates candidates and records the result. Not on the payment path."""

    def __init__(
        self,
        policy: Policy,
        judge: ProcurementJudge,
        ledger: Ledger,
        *,
        index: LedgerIndex | None = None,
    ):
        self.policy = policy
        self.judge = judge
        self.ledger = ledger
        self.index = index if index is not None else LedgerIndex(ledger)

    # -- step 1, on the payment path: cheap, bounded, and it records a snapshot --

    def observe(self, raw: Any, *, decision_id: str | None = None) -> dict[str, Any]:
        """Normalise and record a candidate. Does not call Jev.

        `prior_acquisitions` is frozen here. Chapter 5 requires it: were the
        snapshot taken at evaluation time instead, an asynchronous run could
        see this very purchase among the prior acquisitions and score it as a
        duplicate of itself.
        """
        started = time.perf_counter()
        candidate = normalize(raw, max_input_bytes=self.policy.max_input_bytes)
        digest = input_hash(candidate)
        decision_id = decision_id or new_id("dec")

        duplicate_of = self.index.evaluated_decision(candidate.request_id, digest)
        payload: dict[str, Any] = {
            "candidate": candidate.as_dict(),
            "input_hash": digest,
            "missing_fields": list(candidate.missing_fields),
            "hook_overhead_ms": None,
        }
        if duplicate_of:
            payload["duplicate_of"] = duplicate_of
        payload["hook_overhead_ms"] = round((time.perf_counter() - started) * 1000, 3)

        event = self.ledger.append(
            "candidate_observed",
            decision_id=decision_id,
            candidate_id=candidate.candidate_id,
            request_id=candidate.request_id,
            payload=payload,
        )
        self.index.observe_event(event)
        return {
            "event": event,
            "candidate": candidate,
            "input_hash": digest,
            "decision_id": decision_id,
            "duplicate_of": duplicate_of,
        }

    # -- step 2, off the payment path: checks, Jev, aggregation, record --

    def evaluate(
        self,
        candidate: Candidate,
        *,
        decision_id: str,
        digest: str | None = None,
        record: bool = True,
    ) -> Decision:
        started = time.perf_counter()
        digest = digest or input_hash(candidate)
        checks = run_checks(
            candidate, self.policy, lookup=self.index, decision_id=decision_id
        )
        judgment = self._judge(candidate, checks)
        verdict, reasons = aggregate(checks, judgment, self.policy)

        decision = Decision(
            decision_id=decision_id,
            candidate_id=candidate.candidate_id,
            request_id=candidate.request_id,
            decision=verdict,
            reason_codes=reasons,
            deterministic_checks=checks,
            semantic_judgments=judgment,
            policy=self.policy.as_record(),
            input_hash=digest,
            created_at=utc_now(),
            latency_ms=round((time.perf_counter() - started) * 1000),
        )
        if record:
            event = self.ledger.append(
                "guard_evaluated",
                decision_id=decision_id,
                candidate_id=candidate.candidate_id,
                request_id=candidate.request_id,
                payload=decision.as_dict(),
            )
            self.index.observe_event(event)
        return decision

    def _judge(self, candidate: Candidate, checks: DeterministicChecks) -> SemanticJudgment:
        """Call Jev unless rule 1 already disqualified the candidate.

        Rules 2 and 3 (duplicate request id, exact repurchase) deliberately do
        not skip the call: chapter 8 wants both judgments recorded so their
        agreement can be measured.
        """
        if checks.status != "PASS":
            return SemanticJudgment.skipped()
        try:
            return self.judge.evaluate(candidate, self.policy)
        except ProviderError as error:
            return semantic_failure(error)
        except Exception as error:  # noqa: BLE001 - an adapter bug must not become an outage
            return semantic_failure(ProviderError(str(error), kind="PROVIDER_ERROR"))

    # -- convenience for the CLI: both steps in one call --

    def process(self, raw: Any) -> Decision | None:
        """Observe then evaluate. Returns None when the candidate was a repeat.

        A repeat is a candidate whose `request_id` and `input_hash` were both
        already evaluated: chapter 9 requires the new observation to point at
        the existing decision rather than pay for a second judgment.
        """
        observed = self.observe(raw)
        if observed["duplicate_of"]:
            return None
        return self.evaluate(
            observed["candidate"],
            decision_id=observed["decision_id"],
            digest=observed["input_hash"],
        )
