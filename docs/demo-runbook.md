# Demo runbook: Spend Guard over real x402 settlement

The point of this demo is **not** to measure whether the agent's purchases are
wasteful. It is to show the guard observing, judging and recording real
payments — including a purchase it would have held.

Shadow mode never blocks. The held purchase still settles on chain; the ledger
records that the guard would have stopped it. That contrast is the demo.

## Order of operations

### 1. Descriptions from the providers themselves

16 of the agent's 20 routes have no description beyond their name, and a name
tells the judge almost nothing — run the demo on those and nearly everything
becomes `REVIEW`, which shows nothing.

A paywalled x402 endpoint answers an unpaid request with a 402 challenge whose
`accepts[].description` is the provider describing its own product:

```bash
python3 scripts/fetch_402_descriptions.py \
    --config ../x402-Autonomous-Agent-/config/spend-guard-endpoints.json
```

**No payment header is sent and no purchase can complete**; the script stops at
the 402. It writes `spend-guard-descriptions.proposed.json` beside the config
and **changes nothing** — these descriptions become the record that purchase
decisions are justified from, so a person reads them first.

Routes that come back without a description are listed at the end. Those need
a description written by hand; they are all endpoints you own.

Then merge the proposal into `description` / `description_source` in
`config/spend-guard-endpoints.json`.

### 2. Task definitions — already written

Every route has `task.purpose` and `task.required_data`, and every one says
where it came from:

| `task_origin` | routes | meaning |
|---|---|---|
| `code` | 3 | Mode A's signals. Derived from the agent's code, with citations. |
| `demo` | 17 | Written for the demo. Mode B and Mode C buy unconditionally and declare no per-route need, so there was nothing to derive. |

`demo` is not a claim about what the agent needs. It is a plausible task so the
judge has something to judge against.

### 3. The question-wording A/B

Once descriptions are in, re-run the comparison so it measures the wording
against real text rather than names:

```bash
python3 scripts/jev_question_ab.py \
    --candidates fixtures/candidates/aa-routes.jsonl --max-calls 14
```

7 real routes × 2 wordings = 14 calls, about USD 0.0005. Regenerate the
candidates first if the config changed.

The script reports `code_task` and `demo_task` separately and applies the 0.60
median-confidence rule to `code_task`, so a demo-written task cannot decide
whether a question is retired.

### 4. Wiring — blocked

Chapters 3 to 7 of the integration spec are not in my hands. Nothing here
implements them.

### 5. The held purchase

`config/spend-guard-endpoints.json` carries:

```json
"demo": { "duplicate_purchase": { "enabled": false, "route_key": "smart-money-screener" } }
```

**Off by default.** Enabled, the run requests that one route a second time; the
second candidate carries the first purchase in `prior_acquisitions`, the
deterministic check fires `EXACT_REPURCHASE`, and rule 3 aggregates to `HOLD`.

`smart-money-screener` was chosen because it is the cheapest route on Base at
USD 0.05, takes no request body, and the agent's own README records that it
returns zero candidates anyway — so the deliberately wasted purchase is as
small and as harmless as the endpoint list allows.

Expected ledger, on real settlement:

| | verdict | reason | payment |
|---|---|---|---|
| 1st request | `PAY` | task match, incremental value, not duplicate | settles |
| 2nd request | `HOLD` | `EXACT_REPURCHASE` (rule 3) | **also settles** |

That both payments settle is the point: shadow mode observes, it does not
intervene. `tests/test_demo_duplicate_purchase.py` holds this shape in place
against the real route's identifiers and price.

To show it:

```bash
# enable the switch in config/spend-guard-endpoints.json, run the agent, then
guard --ledger <path> report --verify-chain
guard --ledger <path> replay
```

Turn the switch back off afterwards.

## Not part of this demo

- **Human labelling** (`guard sample`, `docs/labeling.md`). The command still
  works; it is simply not on the demo path, because the demo makes no claim
  about whether a purchase was needed.
- **`consumed`** — whether purchased data was later used. Not recorded.

Both belong to measuring waste, which is the other goal.

## What the demo does not show

The thresholds are still the untuned `shadow-v1` values, and the live run of
2026-09-22 found `confidence_min` too strict against Jev's real confidence
distribution: 4 of 5 candidates went to `REVIEW`, including a clean `PAY` case
(`docs/jev-live-verification.md`). Expect more `REVIEW` in the demo than the
verdicts deserve. `policies/shadow-v2-candidate.json` is there to replay
against and show how the same ledger re-decides under a different threshold —
which is itself worth demonstrating.
