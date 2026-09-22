#!/usr/bin/env python3
"""Verify the Jev adapter against the real TypeSafe API. No installs needed.

Same checks as `tests/live`, but as a plain script so it runs anywhere Python
does - no pip, no pytest, standard library only.

    TYPESAFE_API_KEY='...' python3 scripts/jev_live_check.py

Add --auth proxy when an outbound proxy attaches the credential instead, in
which case no key is needed here.

Nothing this prints or writes contains the API key. The response shape saved
for the docs has every leaf replaced by a type placeholder.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

if sys.version_info < (3, 9):  # pragma: no cover
    sys.exit(f"needs Python 3.9 or newer; this is {sys.version.split()[0]}")

from spend_guard.errors import ProviderError  # noqa: E402
from spend_guard.judges.jev import (  # noqa: E402
    PRIOR_ACQUISITION_FIELDS,
    SERVICE_FIELDS,
    JevProcurementJudge,
)
from spend_guard.models import QUESTIONS  # noqa: E402
from spend_guard.normalize import normalize  # noqa: E402
from spend_guard.policy import Policy  # noqa: E402

CANDIDATES = ["cand_pay_clean", "cand_semantic_dupe", "cand_exact_repurchase",
              "cand_off_task", "cand_thin_evidence"]
SHAPE_PATH = ROOT / "docs" / "jev-live-response.json"

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
if not sys.stdout.isatty():
    GREEN = RED = YELLOW = DIM = RESET = ""

results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    status = "PASS" if ok else "FAIL"
    colour = GREEN if ok else RED
    print(f"  {colour}{status}{RESET}  {name}" + (f"  {DIM}{detail}{RESET}" if detail else ""))
    results.append((status, name, detail))
    return ok


def skip(name: str, why: str) -> None:
    print(f"  {YELLOW}SKIP{RESET}  {name}  {DIM}{why}{RESET}")
    results.append(("SKIP", name, why))


def load_candidates() -> dict:
    path = ROOT / "fixtures" / "candidates" / "candidates.jsonl"
    found = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            item = json.loads(line)
            found[item["candidate_id"]] = item
    return found


def shape_of(value, depth: int = 0):
    """Structure only: every leaf becomes a type placeholder."""
    if depth > 8:
        return "<...>"
    if isinstance(value, dict):
        return {k: shape_of(v, depth + 1) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [shape_of(value[0], depth + 1)] if value else []
    if isinstance(value, bool):
        return "<bool>"
    if isinstance(value, int):
        return "<int>"
    if isinstance(value, float):
        return "<float 0..1>" if 0.0 <= value <= 1.0 else "<float>"
    if value is None:
        return "<null>"
    return "<string>"


class Recorder:
    """Passes requests through, keeping what was sent and received."""

    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0
        self.sent: list[dict] = []
        self.received: list[dict] = []
        self.latencies: list[float] = []

    def __call__(self, request, timeout):
        if self.used >= self.limit:
            raise SystemExit(f"call budget of {self.limit} exhausted")
        self.used += 1
        self.sent.append(json.loads(request.data.decode("utf-8")))
        started = time.perf_counter()
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
        self.latencies.append((time.perf_counter() - started) * 1000)
        self.received.append(json.loads(body))
        return body


def main() -> int:
    parser = argparse.ArgumentParser(description="Live Jev adapter check")
    parser.add_argument("--auth", choices=("key", "proxy"), default=None)
    parser.add_argument("--max-calls", type=int, default=20)
    args = parser.parse_args()

    auth = args.auth or ("proxy" if os.environ.get("SPEND_GUARD_JEV_AUTH") == "proxy" else "key")
    key = os.environ.get("TYPESAFE_API_KEY")
    if auth == "key" and not key:
        sys.exit(
            "TYPESAFE_API_KEY is not set.\n"
            "  TYPESAFE_API_KEY='...' python3 scripts/jev_live_check.py\n"
            "Or use --auth proxy when a proxy attaches the credential."
        )

    policy = Policy.from_dict({
        **json.loads((ROOT / "policies" / "shadow-v1.json").read_text(encoding="utf-8")),
        "jev": {"model": "jev-latest", "timeout_ms": 20000, "retries": 1},
    })
    candidates = load_candidates()
    recorder = Recorder(args.max_calls)
    judge = JevProcurementJudge(api_key=key, transport=recorder, auth=auth)

    print(f"\nJev live check  {DIM}auth={auth}  url={judge.url}{RESET}\n")
    print("Calling the real API...")
    judgments = []
    for name in CANDIDATES:
        try:
            judgments.append((name, judge.evaluate(normalize(candidates[name]), policy)))
            print(f"  {DIM}{name}{RESET}")
        except ProviderError as error:
            print(f"  {RED}{name}: {error} (kind={error.kind}){RESET}")
            print(f"\n{RED}Could not reach Jev. Nothing else can be checked.{RESET}")
            if error.detail:
                print(f"detail: {error.detail}")
            return 1

    print("\n1. Normal path")
    check("response parses into a judgment", all(j.status == "OK" for _, j in judgments))
    check("all four scores present", all(set(j.scores) == set(QUESTIONS) for _, j in judgments))
    check("every score within 0..1",
          all(0.0 <= v <= 1.0 for _, j in judgments for v in j.scores.values()))
    has_confidence = all(set(j.confidence) == set(QUESTIONS) for _, j in judgments)
    check("PER-QUESTION CONFIDENCE returned", has_confidence,
          "" if has_confidence else "<-- the assumption chapter 7 depends on")
    check("token counts returned for costing",
          all(isinstance(j.usage.get("input_tokens"), int)
              and isinstance(j.usage.get("output_tokens"), int) for _, j in judgments))
    check("model identifier returned",
          all(isinstance(j.model, str) and j.model for _, j in judgments))

    print("\n2. Failure paths")
    if auth == "proxy":
        skip("bad key is PROVIDER_ERROR", "this process cannot choose the credential in proxy mode")
    else:
        try:
            JevProcurementJudge(api_key="sk-definitely-not-a-valid-key",
                                sleep=lambda _: None).evaluate(
                normalize(candidates["cand_pay_clean"]), policy)
            check("bad key is PROVIDER_ERROR", False, "no error raised")
        except ProviderError as error:
            check("bad key is PROVIDER_ERROR", error.kind == "PROVIDER_ERROR", f"kind={error.kind}")
            check("key absent from the error message", "sk-definitely" not in str(error))

    tiny = Policy.from_dict({
        **json.loads((ROOT / "policies" / "shadow-v1.json").read_text(encoding="utf-8")),
        "jev": {"model": "jev-latest", "timeout_ms": 1, "retries": 0},
    })
    try:
        JevProcurementJudge(api_key=key, auth=auth, sleep=lambda _: None).evaluate(
            normalize(candidates["cand_pay_clean"]), tiny)
        check("1ms timeout is TIMEOUT", False, "no error raised")
    except ProviderError as error:
        check("1ms timeout is TIMEOUT", error.kind == "TIMEOUT", f"kind={error.kind}")

    print("\n3. What actually left the machine")
    allowlist_ok = True
    for sent in recorder.sent:
        allowlist_ok &= set(sent) == {"state", "model", "questions"}
        allowlist_ok &= set(sent["state"]) == {"task", "service", "prior_acquisitions"}
        allowlist_ok &= set(sent["state"]["service"]) == set(SERVICE_FIELDS)
        allowlist_ok &= all(set(p) == set(PRIOR_ACQUISITION_FIELDS)
                            for p in sent["state"]["prior_acquisitions"])
    check("request contained only allowlisted fields", allowlist_ok)

    blob = json.dumps(recorder.sent)
    leaked = [f for f in ("request_id", "idempotency_key", "request_params_hash",
                          "content_fingerprint", "candidate_id", "quote", "amount") if f in blob]
    check("no ids, hashes or amounts were sent", not leaked, f"leaked: {leaked}" if leaked else "")
    check("every question sent as type=choice",
          all(q["type"] == "choice" for s in recorder.sent for q in s["questions"].values()))

    print("\n4. Observed values (reported, never asserted - Jev drifts ~0.05 between runs)")
    for name, judgment in judgments:
        print(f"  {name}")
        print(f"    scores     { {k: round(v, 3) for k, v in judgment.scores.items()} }")
        print(f"    confidence { {k: round(v, 3) for k, v in judgment.confidence.items()} }")
        print(f"    model={judgment.model}  usage={judgment.usage}")

    ordered = sorted(recorder.latencies)
    p50, longest = ordered[len(ordered) // 2], ordered[-1]
    print(f"\n  latency p50={p50:.0f}ms max={longest:.0f}ms over {len(ordered)} calls")

    SHAPE_PATH.write_text(json.dumps({
        "_comment": ("Structure of a real Jev response, recorded by "
                     "scripts/jev_live_check.py. Every leaf is a type placeholder: "
                     "no real values, no credentials, no request bodies."),
        "latency_ms": {"p50": round(p50), "max": round(longest)},
        "response_shape": shape_of(recorder.received[0]),
        "request_shape": shape_of(recorder.sent[0]),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    failed = [r for r in results if r[0] == "FAIL"]
    print(f"\n{'=' * 62}")
    print(f"{len(results) - len(failed)} passed, {len(failed)} failed, "
          f"{recorder.used} API calls used")
    print(f"response shape written to {SHAPE_PATH.relative_to(ROOT)}")
    if failed:
        print(f"\n{RED}Failures:{RESET}")
        for _, name, detail in failed:
            print(f"  - {name} {detail}")
    print(f"{'=' * 62}")
    print(f"\n{DIM}Safe to paste back: everything above, plus the file just written.{RESET}\n")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
