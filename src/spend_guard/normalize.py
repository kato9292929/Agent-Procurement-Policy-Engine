"""Adapter: whatever the host purchase flow has, in the chapter 5 shape.

Values the host cannot supply become `null` and are listed in
`missing_fields`. Nothing here invents a value, because a fabricated provider
id or price would be indistinguishable from an observed one in the ledger.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from .canonical import canonicalize, hash_json
from .errors import InputError
from .models import Candidate, PriorAcquisition

# Fields that must be present for a candidate to be judged at all. Chapter 8
# rule 1 sends a candidate missing any of these straight to REVIEW, unjudged.
REQUIRED_FIELDS = (
    "request_id",
    "service.provider_id",
    "service.service_id",
    "service.route_id",
    "quote.amount",
    "quote.currency",
    "quote.network",
)

# Optional but worth recording as missing, because their absence degrades the
# semantic judgment rather than blocking it.
TRACKED_OPTIONAL_FIELDS = (
    "observed_at",
    "task.purpose",
    "task.required_data",
    "service.description",
    "service.request_params_hash",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _amount(value: Any) -> str | None:
    """Money stays a decimal string; binary floats must not touch an amount."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def _prior(raw: Any) -> PriorAcquisition | None:
    if not isinstance(raw, dict):
        return None
    return PriorAcquisition(
        provider_id=_text(raw.get("provider_id")),
        service_id=_text(raw.get("service_id")),
        route_id=_text(raw.get("route_id")),
        request_params_hash=_text(raw.get("request_params_hash")),
        content_fingerprint=_text(raw.get("content_fingerprint")),
        summary=_text(raw.get("summary")),
        acquired_at=_text(raw.get("acquired_at")),
    )


def _get(data: dict[str, Any], path: str) -> Any:
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def normalize(raw: Any, *, max_input_bytes: int = 262144) -> Candidate:
    """Normalise one raw purchase candidate. Raises only on unusable input."""
    if not isinstance(raw, dict):
        raise InputError("candidate must be a JSON object")
    size = len(canonicalize_safe(raw).encode("utf-8"))
    if size > max_input_bytes:
        raise InputError(f"candidate is {size} bytes, over the {max_input_bytes} byte limit")

    task_raw = raw.get("task") if isinstance(raw.get("task"), dict) else {}
    service_raw = raw.get("service") if isinstance(raw.get("service"), dict) else {}
    quote_raw = raw.get("quote") if isinstance(raw.get("quote"), dict) else {}

    task = {
        "purpose": _text(task_raw.get("purpose")),
        "required_data": _string_list(task_raw.get("required_data")),
    }
    service = {
        "provider_id": _text(service_raw.get("provider_id")),
        "service_id": _text(service_raw.get("service_id")),
        "route_id": _text(service_raw.get("route_id")),
        "access_method": _text(service_raw.get("access_method")),
        "description": _text(service_raw.get("description")),
        "request_params_hash": _text(service_raw.get("request_params_hash")),
    }
    quote = {
        "amount": _amount(quote_raw.get("amount")),
        "currency": _text(quote_raw.get("currency")),
        "network": _text(quote_raw.get("network")),
    }
    priors = tuple(p for p in (_prior(item) for item in (raw.get("prior_acquisitions") or [])) if p)

    candidate = Candidate(
        candidate_id=_text(raw.get("candidate_id")) or new_id("cand"),
        request_id=_text(raw.get("request_id")),
        idempotency_key=_text(raw.get("idempotency_key")),
        retry_of=_text(raw.get("retry_of")),
        observed_at=_text(raw.get("observed_at")) or utc_now(),
        task=task,
        service=service,
        quote=quote,
        prior_acquisitions=priors,
    )

    normalized = candidate.as_dict()
    missing = [p for p in REQUIRED_FIELDS if _is_absent(_get(normalized, p))]
    missing += [p for p in TRACKED_OPTIONAL_FIELDS if _is_absent(_get(normalized, p))]
    for index, prior in enumerate(priors):
        if prior.summary is None:
            missing.append(f"prior_acquisitions.{index}.summary")

    return Candidate(**{**candidate.__dict__, "missing_fields": tuple(missing)})


def _is_absent(value: Any) -> bool:
    return value is None or value == [] or value == ""


def canonicalize_safe(value: Any) -> str:
    """Canonicalize, turning a non-JSON value into an InputError rather than a crash."""
    try:
        return canonicalize(value)
    except (TypeError, ValueError) as exc:
        raise InputError(f"candidate is not JSON-serialisable: {exc}") from exc


def input_hash(candidate: Candidate) -> str:
    """Stable across key order and whitespace, per chapter 7."""
    return hash_json(candidate.hashable())
