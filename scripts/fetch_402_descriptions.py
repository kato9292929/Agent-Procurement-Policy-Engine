#!/usr/bin/env python3
"""Read each route's own description out of its x402 402 challenge.

A paywalled x402 endpoint answers an unpaid request with HTTP 402 and a
challenge body. That body's `accepts[]` entries carry `description`,
`outputSchema` and `mimeType` — the provider describing its own product. That
is a better source for `service.description` than a name copied out of a config
file, and it costs nothing.

    python3 scripts/fetch_402_descriptions.py \
        --config ../x402-Autonomous-Agent-/config/spend-guard-endpoints.json

**This script never pays.** It sends no payment header, no wallet, no
signature, and it stops at the 402. It cannot complete a purchase even if an
endpoint would let it.

It writes a *proposal* next to the config and never edits the config itself:
descriptions are going into a record that decisions get justified from, so a
person should read them before they land.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if sys.version_info < (3, 9):  # pragma: no cover
    sys.exit(f"needs Python 3.9 or newer; this is {sys.version.split()[0]}")

# Endpoints that do something when called rather than return data. None of the
# routes in the config are of this kind today, but the agent's own probe script
# keeps such a gate (src/scripts/verify-products.ts), and a future route could
# be one. A 402 probe should never be the thing that finds out.
EXECUTION_MARKERS = ("/execute", "/trade", "/order", "/swap", "/transfer", "/withdraw")

# Request bodies for the POST routes, taken from the agent's own probe script
# so the provider sees a well-formed request and answers with its real
# challenge rather than a validation error.
POST_BODIES = {
    "portfolio-intelligence": {
        "body": {"walletAddress": "0x0000000000000000000000000000000000000000", "chain": "base"},
        "source": "src/scripts/verify-products.ts:210-220",
    },
    "whale-intent-decoder": {
        "body": {"token": "ETH", "chain": "ethereum", "amount": 100000},
        "source": "src/scripts/verify-products.ts:244-251",
    },
}

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
if not sys.stdout.isatty():
    GREEN = RED = YELLOW = DIM = RESET = ""


def is_execution_route(url: str) -> bool:
    path = url.split("?")[0].lower()
    return any(marker in path for marker in EXECUTION_MARKERS)


def probe(url: str, method: str, body: dict | None, timeout: float) -> dict:
    """One unpaid request. Returns what came back, never raising."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    # Deliberately no X-PAYMENT / Authorization header of any kind.
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        payload = exc.read()
        status = exc.code
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "status": None, "error": f"{type(exc).__name__}: {exc}",
                "ms": round((time.perf_counter() - started) * 1000)}
    result = {"ok": True, "status": status, "ms": round((time.perf_counter() - started) * 1000)}
    try:
        result["json"] = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError):
        result["text"] = payload.decode("utf-8", "replace")[:400]
    return result


def describe_from_challenge(body: object) -> dict:
    """Pull the provider's own words out of an x402 challenge."""
    found = {"description": None, "output_schema": None, "mime_type": None,
             "resource": None, "accepts_count": 0}
    if not isinstance(body, dict):
        return found
    accepts = body.get("accepts")
    if not isinstance(accepts, list) or not accepts:
        # Some servers put a description at the top level instead.
        if isinstance(body.get("description"), str):
            found["description"] = body["description"].strip() or None
        return found
    found["accepts_count"] = len(accepts)
    for entry in accepts:
        if not isinstance(entry, dict):
            continue
        if found["description"] is None and isinstance(entry.get("description"), str):
            found["description"] = entry["description"].strip() or None
        if found["output_schema"] is None and entry.get("outputSchema") is not None:
            found["output_schema"] = entry["outputSchema"]
        if found["mime_type"] is None and isinstance(entry.get("mimeType"), str):
            found["mime_type"] = entry["mimeType"]
        if found["resource"] is None and isinstance(entry.get("resource"), str):
            found["resource"] = entry["resource"]
    return found


def main() -> int:
    parser = argparse.ArgumentParser(description="Read route descriptions from 402 challenges")
    parser.add_argument("--config", required=True,
                        help="path to spend-guard-endpoints.json")
    parser.add_argument("--out", default=None,
                        help="where to write the proposal (default: next to the config)")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--only", action="append",
                        help="probe only this route_key; repeatable")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    if not config_path.exists():
        sys.exit(f"no such config: {config_path}")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    routes = config["routes"]
    if args.only:
        routes = [r for r in routes if r["route_key"] in set(args.only)]
    out_path = Path(args.out) if args.out else config_path.with_name(
        "spend-guard-descriptions.proposed.json")

    print(f"\nProbing {len(routes)} routes for their 402 challenge "
          f"{DIM}(no payment header is sent){RESET}\n")

    proposals, failures, skipped = {}, [], []
    for route in routes:
        key, url = route["route_key"], route["url"]
        method = route.get("http_method", "GET").upper()

        if is_execution_route(url):
            skipped.append({"route_key": key, "url": url, "why": "looks like an execution endpoint"})
            print(f"  {YELLOW}SKIP{RESET} {key:26} {DIM}execution-looking path, not probed{RESET}")
            continue

        body_spec = POST_BODIES.get(key) if method == "POST" else None
        if method == "POST" and body_spec is None:
            skipped.append({"route_key": key, "url": url,
                            "why": "POST route with no known request body"})
            print(f"  {YELLOW}SKIP{RESET} {key:26} {DIM}POST with no known body{RESET}")
            continue

        result = probe(url, method, body_spec["body"] if body_spec else None, args.timeout)
        if not result["ok"]:
            failures.append({"route_key": key, "url": url, "reason": result["error"]})
            print(f"  {RED}FAIL{RESET} {key:26} {DIM}{result['error'][:60]}{RESET}")
            continue

        status = result["status"]
        found = describe_from_challenge(result.get("json"))
        if status == 402 and found["description"]:
            proposals[key] = {
                "description": found["description"],
                "description_source": {
                    "kind": "x402_challenge",
                    "citations": [url],
                    "note": (f"Read from the route's own 402 challenge on "
                             f"{time.strftime('%Y-%m-%d')} (accepts[].description). "
                             f"No payment was made."),
                },
                "output_schema": found["output_schema"],
                "mime_type": found["mime_type"],
            }
            print(f"  {GREEN}OK{RESET}   {key:26} {DIM}402, "
                  f"{len(found['description'])} chars{RESET}")
        else:
            why = (f"HTTP {status}" if status != 402
                   else f"402 but no description in accepts[] (n={found['accepts_count']})")
            failures.append({"route_key": key, "url": url, "reason": why,
                             "output_schema_present": found["output_schema"] is not None})
            colour = YELLOW if status == 402 else RED
            print(f"  {colour}NONE{RESET} {key:26} {DIM}{why}{RESET}")

    payload = {
        "_comment": ("PROPOSAL, not applied. Descriptions read from each route's own x402 "
                     "challenge; no payment was made and no payment header was sent. Review "
                     "these before merging them into spend-guard-endpoints.json: they end up "
                     "in the record that purchase decisions are justified from."),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "config": str(config_path),
        "probed": len(routes),
        "obtained": len(proposals),
        "proposals": proposals,
        "no_description": failures,
        "skipped": skipped,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"\n{'=' * 64}")
    print(f"  description obtained : {len(proposals)}/{len(routes)}")
    print(f"  no description       : {len(failures)}")
    print(f"  skipped              : {len(skipped)}")
    if failures:
        print(f"\n  {YELLOW}These need a description written by hand:{RESET}")
        for item in failures:
            print(f"    {item['route_key']:26} {item['reason']}")
    print(f"\n  proposal written to {out_path}")
    print(f"  {DIM}Nothing was applied. Review, then merge into the config.{RESET}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
