"""Record shapes for candidates, judgments, and decisions (chapters 5 and 7)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SCHEMA_VERSION = "1"

DECISIONS = ("PAY", "HOLD", "REVIEW")

# `deterministic_checks.signals` vocabulary (chapter 4).
SIGNAL_RETRY_OBSERVED = "RETRY_OBSERVED"
SIGNAL_DUPLICATE_REQUEST_ID = "DUPLICATE_REQUEST_ID"
SIGNAL_EXACT_REPURCHASE = "EXACT_REPURCHASE"

# `semantic_judgments.status` vocabulary (chapter 7).
SEMANTIC_OK = "OK"
SEMANTIC_SKIPPED = "SKIPPED"
SEMANTIC_PROVIDER_ERROR = "PROVIDER_ERROR"
SEMANTIC_INVALID_RESPONSE = "INVALID_RESPONSE"
SEMANTIC_TIMEOUT = "TIMEOUT"

QUESTIONS = ("task_fit", "incremental_value", "duplication_risk", "evidence_sufficiency")


@dataclass(frozen=True)
class PriorAcquisition:
    provider_id: str | None = None
    service_id: str | None = None
    route_id: str | None = None
    request_params_hash: str | None = None
    content_fingerprint: str | None = None
    summary: str | None = None
    acquired_at: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "service_id": self.service_id,
            "route_id": self.route_id,
            "request_params_hash": self.request_params_hash,
            "content_fingerprint": self.content_fingerprint,
            "summary": self.summary,
            "acquired_at": self.acquired_at,
        }


@dataclass(frozen=True)
class Candidate:
    """A purchase candidate normalised into the chapter 5 shape.

    `prior_acquisitions` is a snapshot taken when the candidate was observed.
    It is never refreshed at evaluation time: re-reading the ledger later would
    let the purchase under evaluation appear as already acquired and be scored
    as a duplicate of itself.
    """

    candidate_id: str
    request_id: str | None
    observed_at: str | None
    task: dict[str, Any]
    service: dict[str, Any]
    quote: dict[str, Any]
    prior_acquisitions: tuple[PriorAcquisition, ...] = ()
    idempotency_key: str | None = None
    retry_of: str | None = None
    missing_fields: tuple[str, ...] = ()
    schema_version: str = SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "candidate_id": self.candidate_id,
            "request_id": self.request_id,
            "idempotency_key": self.idempotency_key,
            "retry_of": self.retry_of,
            "observed_at": self.observed_at,
            "task": dict(self.task),
            "service": dict(self.service),
            "quote": dict(self.quote),
            "prior_acquisitions": [p.as_dict() for p in self.prior_acquisitions],
        }

    def hashable(self) -> dict[str, Any]:
        """The part of the candidate `input_hash` covers.

        `candidate_id` is excluded on purpose: two observations of the same
        purchase carry different candidate ids but must hash alike, or the
        re-evaluation guard in chapter 9 would never fire.
        """
        data = self.as_dict()
        data.pop("candidate_id", None)
        return data


@dataclass(frozen=True)
class DeterministicChecks:
    status: str = "PASS"
    failed: tuple[str, ...] = ()
    missing_fields: tuple[str, ...] = ()
    signals: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "failed": list(self.failed),
            "missing_fields": list(self.missing_fields),
            "signals": list(self.signals),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DeterministicChecks":
        return cls(
            status=str(data.get("status", "PASS")),
            failed=tuple(data.get("failed") or ()),
            missing_fields=tuple(data.get("missing_fields") or ()),
            signals=tuple(data.get("signals") or ()),
        )


@dataclass(frozen=True)
class SemanticJudgment:
    """Jev's answers, kept separate from the deterministic checks.

    Scores and confidences are stored before aggregation so that `guard replay`
    can re-decide under a different policy without calling Jev again.
    """

    status: str
    model: str | None = None
    scores: dict[str, float] = field(default_factory=dict)
    confidence: dict[str, float] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"status": self.status, "model": self.model}
        for name in QUESTIONS:
            data[name] = self.scores.get(name)
        data["confidence"] = {n: self.confidence.get(n) for n in QUESTIONS} if self.confidence else {}
        data["usage"] = dict(self.usage) if self.usage else {
            "input_tokens": None, "output_tokens": None, "cost_usd": None
        }
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SemanticJudgment":
        scores = {n: data[n] for n in QUESTIONS if isinstance(data.get(n), (int, float))}
        raw_confidence = data.get("confidence") or {}
        confidence = {
            n: raw_confidence[n]
            for n in QUESTIONS
            if isinstance(raw_confidence.get(n), (int, float))
        }
        return cls(
            status=str(data.get("status", SEMANTIC_PROVIDER_ERROR)),
            model=data.get("model"),
            scores=scores,
            confidence=confidence,
            usage=dict(data.get("usage") or {}),
        )

    @classmethod
    def skipped(cls) -> "SemanticJudgment":
        return cls(status=SEMANTIC_SKIPPED)


@dataclass(frozen=True)
class Decision:
    decision_id: str
    candidate_id: str
    request_id: str | None
    decision: str
    reason_codes: tuple[str, ...]
    deterministic_checks: DeterministicChecks
    semantic_judgments: SemanticJudgment
    policy: dict[str, Any]
    input_hash: str
    created_at: str
    latency_ms: int
    mode: str = "shadow"
    schema_version: str = SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "decision_id": self.decision_id,
            "candidate_id": self.candidate_id,
            "request_id": self.request_id,
            "mode": self.mode,
            "decision": self.decision,
            "reason_codes": list(self.reason_codes),
            "deterministic_checks": self.deterministic_checks.as_dict(),
            "semantic_judgments": self.semantic_judgments.as_dict(),
            "policy": dict(self.policy),
            "input_hash": self.input_hash,
            "created_at": self.created_at,
            "latency_ms": self.latency_ms,
        }
