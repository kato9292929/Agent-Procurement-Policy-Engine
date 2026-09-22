"""Jev adapter: the only place that speaks to TypeSafe AI.

Jev is TypeSafe's System One model. It does not generate text; it answers a
typed question about a piece of state with a probability. The request shape is
``POST https://api.typesafe.ai/v1/systemone`` with a bearer key.

Two responsibilities live here and nowhere else:

* The chapter 4 send limit. The caller's candidate is never forwarded as-is.
  `project_state` copies out an explicit allowlist of fields, so a field added
  to the candidate later cannot start leaving the machine by accident.
* Turning provider-specific failures into `ProviderError`, so nothing
  Jev-shaped reaches the aggregation or the ledger.
"""

from __future__ import annotations

import json
import math
import os
import shlex
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping

from ..errors import ProviderError
from ..models import (
    Candidate,
    QUESTIONS,
    SEMANTIC_OK,
    SemanticJudgment,
)
from ..policy import Policy
from .base import resolve_questions

DEFAULT_URL = "https://api.typesafe.ai/v1/systemone"
DEFAULT_MODEL = "jev-latest"

# When the credential is attached by an outbound proxy rather than held by this
# process, the request must go out WITHOUT an Authorization header of its own:
# the proxy adds it after the request leaves. Set SPEND_GUARD_JEV_AUTH=proxy.
# This is how a Claude Code cloud environment's "API credentials" work, and it
# is strictly better than holding the key here - the key never enters the
# sandbox at all, so nothing running in it can read or leak the key.
AUTH_PROXY = "proxy"
AUTH_KEY = "key"


def auth_mode(env: Mapping[str, str] | None = None) -> str:
    environ = os.environ if env is None else env
    return AUTH_PROXY if environ.get("SPEND_GUARD_JEV_AUTH") == AUTH_PROXY else AUTH_KEY

Transport = Callable[[urllib.request.Request, float], bytes]

# Exactly what may be sent. Anything not named here stays on this machine:
# request ids, idempotency keys, parameter hashes, content fingerprints, the
# quoted amount, and every credential.
PRIOR_ACQUISITION_FIELDS = ("provider_id", "service_id", "route_id", "summary", "acquired_at")
SERVICE_FIELDS = ("provider_id", "service_id", "route_id", "access_method", "description")


def project_state(candidate: Candidate) -> dict[str, Any]:
    """Build the Jev payload from an allowlist, never by filtering the candidate.

    Hashes (`request_params_hash`, `content_fingerprint`) are withheld because
    they carry no meaning a semantic judge could use; `summary` is what lets
    Jev reason about what was already acquired.
    """
    return {
        "task": {
            "purpose": candidate.task.get("purpose"),
            "required_data": list(candidate.task.get("required_data") or []),
        },
        "service": {name: candidate.service.get(name) for name in SERVICE_FIELDS},
        "prior_acquisitions": [
            {name: getattr(prior, name) for name in PRIOR_ACQUISITION_FIELDS}
            for prior in candidate.prior_acquisitions
        ],
    }


def build_questions(policy: Policy | None = None) -> dict[str, dict[str, Any]]:
    """One `choice` question per signal, with the policy's wording.

    The choice primitive is used rather than a plain yes/no because chapter 7
    requires a confidence per question, and only choice and score answers carry
    one. Deriving a confidence from a bare probability would be inventing it.
    """
    return {
        name: {
            "type": "choice",
            "instructions": spec["instructions"],
            "criteria": dict(spec["criteria"]),  # type: ignore[arg-type]
        }
        for name, spec in resolve_questions(policy).items()
    }


def positive_option(name: str, policy: Policy | None = None) -> str:
    return str(resolve_questions(policy)[name]["positive"])


def load_api_key(env: Mapping[str, str] | None = None) -> str:
    environ = os.environ if env is None else env
    if key := environ.get("TYPESAFE_API_KEY"):
        return key
    path = Path(
        environ.get("TYPESAFE_CREDENTIALS_FILE", "~/.config/typesafe/credentials.env")
    ).expanduser()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ProviderError(
            "Jev credentials unavailable", kind="PROVIDER_ERROR", detail=str(exc)
        ) from exc
    for raw in lines:
        line = raw.strip()
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if not line.startswith("TYPESAFE_API_KEY="):
            continue
        parsed = shlex.split(line.split("=", 1)[1], comments=True)
        if len(parsed) == 1 and parsed[0]:
            return parsed[0]
    raise ProviderError("Jev credentials unavailable", kind="PROVIDER_ERROR")


def _default_transport(request: urllib.request.Request, timeout: float) -> bytes:
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _probability(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProviderError(
            "Jev returned an unusable answer", kind="INVALID_RESPONSE", detail=f"{where} not a number"
        )
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ProviderError(
            "Jev returned an unusable answer", kind="INVALID_RESPONSE", detail=f"{where} out of range"
        )
    return number


def parse_response(body: Any, policy: Policy | None = None) -> SemanticJudgment:
    """Validate Jev's reply into a SemanticJudgment, or raise INVALID_RESPONSE."""
    if not isinstance(body, dict):
        raise ProviderError("Jev returned an unusable answer", kind="INVALID_RESPONSE", detail="root")
    answers = body.get("answers")
    if not isinstance(answers, dict) or set(answers) != set(QUESTIONS):
        raise ProviderError(
            "Jev returned an unusable answer", kind="INVALID_RESPONSE", detail="answers"
        )
    scores: dict[str, float] = {}
    confidence: dict[str, float] = {}
    for name in QUESTIONS:
        answer = answers[name]
        if not isinstance(answer, dict):
            raise ProviderError(
                "Jev returned an unusable answer", kind="INVALID_RESPONSE", detail=f"answers.{name}"
            )
        probabilities = answer.get("probabilities")
        positive = positive_option(name, policy)
        if not isinstance(probabilities, dict) or positive not in probabilities:
            raise ProviderError(
                "Jev returned an unusable answer",
                kind="INVALID_RESPONSE",
                detail=f"answers.{name}.probabilities",
            )
        scores[name] = _probability(probabilities[positive], f"answers.{name}")
        confidence[name] = _probability(answer.get("confidence"), f"answers.{name}.confidence")

    model = body.get("model")
    if model is not None and not isinstance(model, str):
        raise ProviderError("Jev returned an unusable answer", kind="INVALID_RESPONSE", detail="model")
    usage_raw = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    usage = {
        "input_tokens": _tokens(usage_raw.get("input_tokens")),
        "output_tokens": _tokens(usage_raw.get("output_tokens")),
        "cost_usd": None,
    }
    return SemanticJudgment(
        status=SEMANTIC_OK, model=model, scores=scores, confidence=confidence, usage=usage
    )


def _tokens(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def apply_pricing(usage: dict[str, Any], pricing: Mapping[str, Any]) -> dict[str, Any]:
    """Fill `cost_usd` from token counts when a unit price is configured.

    Without a configured price the cost stays null rather than being guessed;
    chapter 7 asks for the price source to be recorded in the policy file.
    """
    input_rate = pricing.get("input_per_mtok_usd")
    output_rate = pricing.get("output_per_mtok_usd")
    if not isinstance(input_rate, (int, float)) or not isinstance(output_rate, (int, float)):
        return usage
    tokens_in, tokens_out = usage.get("input_tokens"), usage.get("output_tokens")
    if not isinstance(tokens_in, int) or not isinstance(tokens_out, int):
        return usage
    cost = (tokens_in * float(input_rate) + tokens_out * float(output_rate)) / 1_000_000
    return {**usage, "cost_usd": round(cost, 10)}


class JevProcurementJudge:
    """Calls Jev. Raises ProviderError; never returns a low score for a failure."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        url: str | None = None,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        auth: str | None = None,
    ):
        self.api_key = api_key
        self.url = url or os.environ.get("SPEND_GUARD_JEV_URL") or DEFAULT_URL
        self.transport = transport or _default_transport
        self.sleep = sleep
        self.auth = auth or auth_mode()

    def evaluate(self, candidate: Candidate, policy: Policy) -> SemanticJudgment:
        settings = policy.jev or {}
        timeout = float(settings.get("timeout_ms", 8000)) / 1000.0
        retries = max(0, min(5, int(settings.get("retries", 2))))
        model = str(settings.get("model") or DEFAULT_MODEL)

        payload = json.dumps(
            {"state": project_state(candidate), "model": model, "questions": build_questions(policy)},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.auth != AUTH_PROXY:
            headers["Authorization"] = f"Bearer {self.api_key or load_api_key()}"
        request = urllib.request.Request(self.url, data=payload, headers=headers, method="POST")

        for attempt in range(retries + 1):
            try:
                body = self.transport(request, timeout)
            except urllib.error.HTTPError as exc:
                transient = exc.code == 429 or 500 <= exc.code <= 599
                if transient and attempt < retries:
                    self.sleep(min(2.0, 0.1 * (2**attempt)))
                    continue
                raise ProviderError(
                    "Jev request failed", kind="PROVIDER_ERROR", detail=f"HTTP {exc.code}"
                ) from exc
            except TimeoutError as exc:
                if attempt < retries:
                    self.sleep(min(2.0, 0.1 * (2**attempt)))
                    continue
                raise ProviderError("Jev timed out", kind="TIMEOUT", detail=str(exc)) from exc
            except (urllib.error.URLError, OSError) as exc:
                reason = getattr(exc, "reason", None)
                if isinstance(reason, TimeoutError) or "timed out" in str(reason or exc).lower():
                    if attempt < retries:
                        self.sleep(min(2.0, 0.1 * (2**attempt)))
                        continue
                    raise ProviderError("Jev timed out", kind="TIMEOUT", detail=str(exc)) from exc
                if attempt < retries:
                    self.sleep(min(2.0, 0.1 * (2**attempt)))
                    continue
                raise ProviderError(
                    "Jev request failed", kind="PROVIDER_ERROR", detail=str(exc)
                ) from exc

            try:
                decoded = json.loads(body)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise ProviderError(
                    "Jev returned invalid JSON", kind="INVALID_RESPONSE", detail=str(exc)
                ) from exc
            judgment = parse_response(decoded, policy)
            return SemanticJudgment(
                status=judgment.status,
                model=judgment.model or model,
                scores=judgment.scores,
                confidence=judgment.confidence,
                usage=apply_pricing(judgment.usage, policy.pricing),
            )
        raise AssertionError("unreachable: retry loop exhausted")
