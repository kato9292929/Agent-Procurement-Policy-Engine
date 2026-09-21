"""Offline judge for dry runs and tests.

Answers come from a fixture file keyed by `candidate_id`, so the whole
pipeline can be exercised with no API key, no network, and no cost. A fixture
entry may also ask for a failure, which is how the provider-error, timeout and
invalid-response paths of chapter 13 are tested.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from ..errors import InputError, ProviderError
from ..models import QUESTIONS, SEMANTIC_OK, Candidate, SemanticJudgment
from ..policy import Policy

FAILURE_KINDS = {"PROVIDER_ERROR", "TIMEOUT", "INVALID_RESPONSE"}


class FixtureProcurementJudge:
    """Replays recorded answers. `calls` records what it was asked, for tests."""

    def __init__(self, answers: Mapping[str, Any] | None = None, *, model: str = "jev-fixture"):
        self.answers = dict(answers or {})
        self.model = model
        self.calls: list[str] = []

    @classmethod
    def from_file(cls, path: str | Path, **kwargs: Any) -> "FixtureProcurementJudge":
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except OSError as exc:
            raise InputError(f"cannot read fixture {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise InputError(f"fixture {path} is not valid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise InputError(f"fixture {path} must be a JSON object keyed by candidate_id")
        return cls(data, **kwargs)

    def evaluate(self, candidate: Candidate, policy: Policy) -> SemanticJudgment:
        self.calls.append(candidate.candidate_id)
        entry = self.answers.get(candidate.candidate_id, self.answers.get("_default"))
        if entry is None:
            raise InputError(f"fixture has no entry for candidate {candidate.candidate_id}")

        failure = entry.get("fail")
        if failure:
            kind = str(failure).upper()
            if kind not in FAILURE_KINDS:
                raise InputError(f"fixture failure kind must be one of {sorted(FAILURE_KINDS)}")
            raise ProviderError(f"fixture failure: {kind}", kind=kind, detail="fixture")

        scores = {name: float(entry[name]) for name in QUESTIONS if name in entry}
        missing = [name for name in QUESTIONS if name not in scores]
        if missing:
            raise InputError(f"fixture entry is missing scores: {missing}")
        raw_confidence = entry.get("confidence") or {}
        if isinstance(raw_confidence, (int, float)):
            raw_confidence = {name: float(raw_confidence) for name in QUESTIONS}
        confidence = {name: float(raw_confidence.get(name, 1.0)) for name in QUESTIONS}
        usage = dict(entry.get("usage") or {"input_tokens": None, "output_tokens": None})
        usage.setdefault("cost_usd", None)
        return SemanticJudgment(
            status=SEMANTIC_OK,
            model=entry.get("model", self.model),
            scores=scores,
            confidence=confidence,
            usage=usage,
        )
