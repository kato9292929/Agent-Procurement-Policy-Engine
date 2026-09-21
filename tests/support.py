"""Shared helpers. No test here touches the network."""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from spend_guard.judges import FixtureProcurementJudge  # noqa: E402
from spend_guard.ledger import Ledger  # noqa: E402
from spend_guard.policy import Policy  # noqa: E402
from spend_guard.engine import ShadowEngine  # noqa: E402

POLICY_PATH = ROOT / "policies" / "shadow-v1.json"
CANDIDATES = ROOT / "fixtures" / "candidates" / "candidates.jsonl"
ANSWERS = ROOT / "fixtures" / "judge" / "answers.json"


def policy(**overrides) -> Policy:
    data = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    thresholds = overrides.pop("thresholds", None)
    if thresholds:
        data["thresholds"].update(thresholds)
    data.update(overrides)
    return Policy.from_dict(data)


def candidates() -> dict[str, dict]:
    out = {}
    for line in CANDIDATES.read_text(encoding="utf-8").splitlines():
        if line.strip():
            item = json.loads(line)
            out[item["candidate_id"]] = item
    return out


def candidate(name: str, **overrides) -> dict:
    raw = copy.deepcopy(candidates()[name])
    raw.update(overrides)
    return raw


def judge() -> FixtureProcurementJudge:
    return FixtureProcurementJudge.from_file(ANSWERS)


def engine(tmp: Path, *, the_judge=None, the_policy=None) -> ShadowEngine:
    return ShadowEngine(
        the_policy or policy(),
        the_judge or judge(),
        Ledger(tmp / "ledger.jsonl"),
    )
