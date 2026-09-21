"""Versioned policy: thresholds and windows, loaded from disk, never hardcoded.

Chapter 8 requires thresholds to live in version-controlled configuration
rather than in code, and outside anywhere the agent can rewrite at run time.
`Policy.load` therefore only reads; nothing in this package writes a policy
file back.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import InputError

_DURATION = re.compile(
    r"^P(?!$)(?:(\d+(?:\.\d+)?)D)?"
    r"(?:T(?!$)(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?$"
)

REQUIRED_THRESHOLDS = (
    "task_fit_min",
    "incremental_value_min",
    "duplication_risk_max",
    "evidence_sufficiency_min",
    "confidence_min",
)


def parse_duration(text: str) -> float:
    """Parse the ISO 8601 durations used for `exact_repurchase_window`."""
    match = _DURATION.match(text or "")
    if not match:
        raise InputError(f"invalid ISO 8601 duration: {text!r}")
    days, hours, minutes, seconds = (float(g) if g else 0.0 for g in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


@dataclass(frozen=True)
class Policy:
    policy_version: str
    thresholds: dict[str, float]
    exact_repurchase_window: str = "PT24H"
    provisional: bool = True
    max_input_bytes: int = 262144
    retry_detection: str = "idempotency_key"
    jev: dict[str, Any] = field(default_factory=dict)
    hook: dict[str, Any] = field(default_factory=dict)
    pricing: dict[str, Any] = field(default_factory=dict)
    ledger: dict[str, Any] = field(default_factory=dict)
    report: dict[str, Any] = field(default_factory=dict)

    @property
    def repurchase_window_seconds(self) -> float:
        return parse_duration(self.exact_repurchase_window)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Policy":
        if not isinstance(data, dict):
            raise InputError("policy must be a JSON object")
        version = data.get("policy_version")
        if not isinstance(version, str) or not version:
            raise InputError("policy.policy_version must be a non-empty string")
        thresholds = data.get("thresholds")
        if not isinstance(thresholds, dict):
            raise InputError("policy.thresholds must be an object")
        clean: dict[str, float] = {}
        for name in REQUIRED_THRESHOLDS:
            value = thresholds.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise InputError(f"policy.thresholds.{name} must be a number")
            if not 0.0 <= float(value) <= 1.0:
                raise InputError(f"policy.thresholds.{name} must be within 0..1")
            clean[name] = float(value)
        window = data.get("exact_repurchase_window", "PT24H")
        parse_duration(window)  # reject a bad window at load time, not mid-evaluation
        return cls(
            policy_version=version,
            thresholds=clean,
            exact_repurchase_window=window,
            provisional=bool(data.get("provisional", True)),
            max_input_bytes=int(data.get("max_input_bytes", 262144)),
            retry_detection=str(data.get("retry_detection", "idempotency_key")),
            jev=dict(data.get("jev") or {}),
            hook=dict(data.get("hook") or {}),
            pricing=dict(data.get("pricing") or {}),
            ledger=dict(data.get("ledger") or {}),
            report=dict(data.get("report") or {}),
        )

    @classmethod
    def load(cls, path: str | Path) -> "Policy":
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise InputError(f"cannot read policy {path}: {exc}") from exc
        try:
            return cls.from_dict(json.loads(text))
        except json.JSONDecodeError as exc:
            raise InputError(f"policy {path} is not valid JSON: {exc}") from exc

    def as_record(self) -> dict[str, Any]:
        """The `policy` block embedded in every decision record."""
        return {
            "policy_version": self.policy_version,
            "thresholds": dict(self.thresholds),
            "exact_repurchase_window": self.exact_repurchase_window,
        }
