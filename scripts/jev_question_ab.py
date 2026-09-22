#!/usr/bin/env python3
"""Compare two question wordings on the real API. No installs needed.

Asks the same candidates under two policies and reports what changed, so a
rewording can be judged on evidence rather than on whether it reads better.

    TYPESAFE_API_KEY='...' python3 scripts/jev_question_ab.py \
        --before policies/shadow-v1.json --after policies/shadow-v2.json

`replay` cannot answer this question: it never calls Jev, so it can re-decide
stored scores under new thresholds but can never produce the scores a
different wording would have given. That is why a rewording has to be settled
before a long shadow run and a threshold does not.

Scores are reported, never asserted. Jev drifts about 0.05 between runs, and
these samples are small.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

if sys.version_info < (3, 9):  # pragma: no cover
    sys.exit(f"needs Python 3.9 or newer; this is {sys.version.split()[0]}")

from spend_guard.errors import ProviderError  # noqa: E402
from spend_guard.judges.base import resolve_questions  # noqa: E402
from spend_guard.judges.jev import JevProcurementJudge, apply_pricing  # noqa: E402
from spend_guard.models import QUESTIONS  # noqa: E402
from spend_guard.normalize import normalize  # noqa: E402
from spend_guard.policy import Policy  # noqa: E402

# Below this median confidence, the question is not earning its API call and
# the deterministic fallback is the better answer.
CONFIDENCE_FLOOR = 0.60

BOLD, DIM, GREEN, RED, RESET = "\033[1m", "\033[2m", "\033[32m", "\033[31m", ""
if sys.stdout.isatty():
    RESET = "\033[0m"
else:
    BOLD = DIM = GREEN = RED = ""


class Budget:
    def __init__(self, limit: int):
        self.limit, self.used = limit, 0
        self.latencies: list[float] = []
        self.cost = 0.0

    def transport(self, request, timeout):
        if self.used >= self.limit:
            raise SystemExit(f"call budget of {self.limit} exhausted")
        self.used += 1
        started = time.perf_counter()
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
        self.latencies.append((time.perf_counter() - started) * 1000)
        return body


def group_of(raw: dict) -> str:
    """Candidates may carry `_ab_group` to be reported apart from the rest.

    A question that cannot be answered from the input at all - `required_data`
    empty, so "does it provide each required item" has nothing to check - would
    drag a single median down and could retire a question for the wrong reason.
    Such candidates are still asked and still reported; they are just not mixed
    into the group the decision rule is applied to.
    """
    return str(raw.get("_ab_group") or "all")


def load_candidates(paths: list[Path]) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for path in paths:
        if not path.exists():
            sys.exit(f"no such candidate file: {path}")
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                found[item["candidate_id"]] = item
    return found


def spread(values: list[float]) -> str:
    if not values:
        return "n=0"
    ordered = sorted(values)
    return (f"n={len(ordered)} min={ordered[0]:.2f} "
            f"median={statistics.median(ordered):.2f} max={ordered[-1]:.2f}")


def run(policy: Policy, candidates: dict[str, dict], budget: Budget, key, auth):
    judge = JevProcurementJudge(api_key=key, transport=budget.transport, auth=auth)
    out = {}
    for name, raw in candidates.items():
        try:
            judgment = judge.evaluate(normalize(raw), policy)
        except ProviderError as error:
            sys.exit(f"{name}: Jev failed ({error.kind}). Nothing can be compared.")
        usage = apply_pricing(dict(judgment.usage), policy.pricing)
        if isinstance(usage.get("cost_usd"), float):
            budget.cost += usage["cost_usd"]
        out[name] = judgment
        print(f"    {DIM}{name}{RESET}")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare two Jev question wordings")
    parser.add_argument("--before", default="policies/shadow-v1.json")
    parser.add_argument("--after", default="policies/shadow-v2.json")
    parser.add_argument("--candidates", action="append", default=None,
                        help="JSONL of candidates; repeatable (default: the fixtures)")
    parser.add_argument("--question", default="evidence_sufficiency")
    parser.add_argument("--max-calls", type=int, default=20)
    parser.add_argument("--out", default="docs/jev-question-ab.json")
    args = parser.parse_args()

    auth = "proxy" if os.environ.get("SPEND_GUARD_JEV_AUTH") == "proxy" else "key"
    key = os.environ.get("TYPESAFE_API_KEY")
    if auth == "key" and not key:
        sys.exit("TYPESAFE_API_KEY is not set (or use SPEND_GUARD_JEV_AUTH=proxy)")

    before = Policy.load(ROOT / args.before)
    after = Policy.load(ROOT / args.after)
    paths = [Path(p) for p in (args.candidates or
                               [ROOT / "fixtures" / "candidates" / "candidates.jsonl"])]
    candidates = load_candidates(paths)

    needed = len(candidates) * 2
    if needed > args.max_calls:
        sys.exit(
            f"{len(candidates)} candidates x 2 wordings = {needed} calls, over the "
            f"{args.max_calls} cap.\nRaise it deliberately with --max-calls {needed} "
            f"(about USD {needed * 3.2e-05:.5f}), or pass fewer candidates."
        )

    differing = [q for q in QUESTIONS
                 if resolve_questions(before)[q] != resolve_questions(after)[q]]
    print(f"\n{BOLD}Question wording A/B{RESET}  {DIM}{before.policy_version} -> "
          f"{after.policy_version}{RESET}")
    print(f"  candidates: {len(candidates)}   calls: {needed}   "
          f"questions that differ: {differing or 'NONE - nothing to compare'}")
    if not differing:
        return 1
    for label, policy in (("before", before), ("after", after)):
        spec = resolve_questions(policy)[args.question]
        print(f"\n  {label}: {DIM}{spec['instructions']}{RESET}")
        print(f"          {DIM}options: {list(spec['criteria'])}{RESET}")

    budget = Budget(args.max_calls)
    print(f"\n  asking under {before.policy_version}...")
    results_before = run(before, candidates, budget, key, auth)
    print(f"  asking under {after.policy_version}...")
    results_after = run(after, candidates, budget, key, auth)

    q = args.question
    groups: dict[str, list[str]] = {}
    for name, raw in candidates.items():
        groups.setdefault(group_of(raw), []).append(name)

    def stats(names, source, field):
        picked = [getattr(source[n], field)[q] for n in names]
        return picked

    per_group = {}
    for group, names in sorted(groups.items()):
        cb = stats(names, results_before, "confidence")
        ca = stats(names, results_after, "confidence")
        sb = stats(names, results_before, "scores")
        sa = stats(names, results_after, "scores")
        per_group[group] = {
            "n": len(names),
            "median_confidence": {"before": statistics.median(cb), "after": statistics.median(ca)},
            "median_score": {"before": statistics.median(sb), "after": statistics.median(sa)},
        }
        print(f"\n{BOLD}{q} - group `{group}` (n={len(names)}){RESET}")
        print(f"  score       before  {spread(sb)}")
        print(f"              after   {spread(sa)}")
        print(f"  confidence  before  {spread(cb)}")
        print(f"              after   {spread(ca)}")

    # The rule is applied to the group where the question is fairly testable.
    decision_group = "answerable" if "answerable" in groups else sorted(groups)[0]
    names = groups[decision_group]
    conf_b = stats(names, results_before, "confidence")
    conf_a = stats(names, results_after, "confidence")
    if len(groups) > 1:
        print(f"\n  {DIM}decision rule applied to group `{decision_group}` "
              f"(n={len(names)}); other groups reported for context only{RESET}")

    median_after = statistics.median(conf_a) if conf_a else 0.0
    improved = median_after > (statistics.median(conf_b) if conf_b else 0.0)
    verdict_ok = median_after >= CONFIDENCE_FLOOR
    mark = f"{GREEN}PASS{RESET}" if verdict_ok else f"{RED}BELOW FLOOR{RESET}"
    print(f"\n  median confidence after rewording: {median_after:.2f}   "
          f"floor {CONFIDENCE_FLOOR}  ->  {mark}")
    print(f"  {'improved' if improved else 'did NOT improve'} on the previous wording")
    if not verdict_ok:
        print(f"  {RED}Recommendation: drop {q} as a Jev question and replace it with a "
              f"deterministic check{RESET}\n  (task.purpose, task.required_data and "
              f"service.description all present). Implementation needs approval.")

    print(f"\n{BOLD}per candidate{RESET}  {DIM}(reported, never asserted){RESET}")
    print(f"  {'candidate':30} {'group':18} {'score b->a':>15}  {'confidence b->a':>17}")
    for group, names in sorted(groups.items()):
        for name in names:
            b, a = results_before[name], results_after[name]
            print(f"  {name:30} {group:18} {b.scores[q]:6.2f} -> {a.scores[q]:4.2f}  "
                  f"{b.confidence[q]:8.2f} -> {a.confidence[q]:6.2f}")

    ordered = sorted(budget.latencies)
    payload = {
        "_comment": ("Real Jev answers under two question wordings. Values are real; "
                     "no credentials or request bodies are stored."),
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "before": {"policy_version": before.policy_version,
                   "question": resolve_questions(before)[q]},
        "after": {"policy_version": after.policy_version,
                  "question": resolve_questions(after)[q]},
        "question": q,
        "confidence_floor": CONFIDENCE_FLOOR,
        "decision_group": decision_group,
        "per_group": per_group,
        "median_confidence": {"before": statistics.median(conf_b), "after": median_after},
        "meets_floor": verdict_ok,
        "per_candidate": {
            name: {
                "before": {"score": results_before[name].scores[q],
                           "confidence": results_before[name].confidence[q]},
                "after": {"score": results_after[name].scores[q],
                          "confidence": results_after[name].confidence[q]},
            } for name in candidates
        },
        "all_scores_after": {name: results_after[name].scores for name in candidates},
        "all_confidence_after": {name: results_after[name].confidence for name in candidates},
        "calls": budget.used,
        "cost_usd": round(budget.cost, 8),
        "latency_ms": {"p50": round(ordered[len(ordered) // 2]), "max": round(ordered[-1])},
    }
    out = ROOT / args.out
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"\n  {budget.used} calls, USD {budget.cost:.6f}, "
          f"latency p50={payload['latency_ms']['p50']}ms max={payload['latency_ms']['max']}ms")
    print(f"  written to {args.out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
