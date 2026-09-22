# Verifying the Jev adapter against the real API

**Status: VERIFIED on 2026-09-22.** Run against `api.typesafe.ai`, model
`jev-1.13.0`, five candidates. **12 checks passed, 0 failed.**

Every structural assumption the adapter was written on held, including the one
most likely to be wrong. Two problems were found, and neither is in the
adapter: both are in the *policy* built on top of it.

## What was confirmed

| Assumption | Result |
|---|---|
| The response parses with `parse_response` as written | confirmed |
| Every score comes back in 0..1 | confirmed |
| **`choice` questions return a per-question confidence** | **confirmed** |
| `usage` carries integer token counts usable for costing | confirmed |
| A model identifier is returned | `jev-1.13.0` |
| A bad key raises `PROVIDER_ERROR`, key absent from the message | confirmed |
| A 1 ms timeout raises `TIMEOUT`, not `PROVIDER_ERROR` | confirmed |
| The bodies actually sent contain only allowlisted fields | confirmed |
| Every question really was sent as `type: "choice"` | confirmed |

**No adapter change was needed.** The per-question confidence was the load-
bearing assumption — `noul` returns a probability with no confidence, which is
why every question is sent as a two-option `choice` instead — and it holds.

**Latency: p50 747 ms, max 774 ms** over five calls. That is the number to set
`hook_timeout_ms` from if the synchronous fallback is ever used: roughly three
quarters of a second per judgment, on the payment path. `jev.timeout_ms` at
8000 ms is comfortable.

**Cost is still open.** Token counts arrive (about 690-800 in, 145 out per
judgment), so `cost_usd` is computable the moment the per-token price is
filled into `pricing`. The TypeSafe docs site is not reachable from the
sandbox; the Usage page in the console should have the rate.

## What the real answers did to shadow-v1

The observed answers are recorded in
`fixtures/judge/observed-live-2026-09-22.json` and replayed by
`tests/test_observed_live_data.py`, so these findings stay reproducible.

### Finding 1: `confidence_min` is far above the real distribution

**11 of 20 observed confidences fall below 0.80.** Median 0.77, minimum 0.14.

Rule 5 sends a candidate to `REVIEW` if *any* of the four answers is below the
floor, so on real data **4 of 5 candidates became `REVIEW`, including the
clean `PAY` case**:

| candidate | intended | shadow-v1 on real data |
|---|---|---|
| `cand_pay_clean` | PAY | **REVIEW** |
| `cand_semantic_dupe` | HOLD | **REVIEW** |
| `cand_exact_repurchase` | HOLD | HOLD |
| `cand_off_task` | HOLD | **REVIEW** |
| `cand_thin_evidence` | REVIEW | REVIEW |

This is exactly the failure mode `policies/shadow-v1.json` warned about in its
own `_confidence_min_note`: "set it too high and nearly everything becomes
REVIEW". It did.

### Finding 2: rule 5 gates on questions that decide nothing

Underneath the threshold problem is a design one. `cand_pay_clean` clears
**every** score threshold — nothing about the purchase is in doubt. It still
becomes `REVIEW`, because the model was only 0.77 confident about `task_fit`
and 0.51 about `evidence_sufficiency`, neither of which would have changed the
verdict.

Lowering the floor papers over this. The straighter fix is to gate on the
confidence of the question that actually decided, and that means changing the
aggregation rather than a number — a bigger call than a threshold, so it is
recorded here rather than applied.

### Finding 3: `evidence_sufficiency` may be badly posed

It scored below 0.80 on **every** candidate — 0.76, 0.43, 0.34, 0.29, 0.00 —
including the one written to be well specified. Its confidence was also low
(0.51, 0.14, 0.32, 0.43, 1.00).

Low scores together with low confidence in those scores suggests the model is
reading the question differently than intended, rather than the inputs being
genuinely thin. Rewording it means re-running this check, so it is recorded,
not patched.

Because rule 7 fires on the score alone, this keeps `cand_off_task` in
`REVIEW` even with `confidence_min` at zero.

## What was changed, and what was not

**Changed:** nothing in the adapter. `policies/shadow-v2-candidate.json` was
added to be replayed against:

```bash
guard replay --policy policies/shadow-v2-candidate.json
```

It differs from shadow-v1 in one number, `confidence_min` 0.80 → 0.50, which
recovers the clean `PAY` case (`REVIEW->PAY: 1`).

**Not changed:** `shadow-v1` itself. A threshold change means a new
`policy_version`, never an edit, so replaying historical decisions stays
reproducible.

**0.50 is not a calibrated value.** It is the lowest that rescues one case in
a sample of five, from one run, of a model that drifts about 0.05 between
runs. Choosing a real threshold still needs human labels; see
`docs/labeling.md`. What this run establishes is that the shipped value is
wrong, which is a different thing from knowing the right one.

## Re-running it

### Quickest way: no installs

`pytest` is only the runner; the checks themselves need nothing but the
standard library. If pip or pytest are not available, use the script:

```bash
TYPESAFE_API_KEY='...' python3 scripts/jev_live_check.py
```

Works on Python 3.9+, uses 7 API calls (capped at 20, `--max-calls` to change),
and prints a PASS/FAIL line per check plus the observed scores and latency.
Add `--auth proxy` when a proxy attaches the credential instead of this
process. Nothing it prints or writes contains the key.

### Getting the credential to the tests

Two routes. In a sandboxed environment, **prefer the proxy route**: the key
never enters the sandbox, so nothing running there can read or leak it.

#### A. An outbound proxy attaches it (preferred)

```bash
export SPEND_GUARD_JEV_AUTH=proxy
python3 -m pytest tests/live -m live -q -s
```

The request goes out with **no `Authorization` header of its own**; the proxy
adds the credential after it leaves. This process never holds the key.

In a Claude Code cloud environment this is the **API credentials** feature
(Pro and Max plans). Registering `api.typesafe.ai` there also grants network
access to that host, which the environment's network policy blocks by default
— so this route solves reachability and secrecy in one step.

`test_a_bad_key_is_a_provider_error` skips in this mode, because the process
cannot choose which credential is sent.

#### B. This process holds the key

```bash
export TYPESAFE_API_KEY='...'
python3 -m pytest tests/live -m live -q -s
```

The adapter reads `TYPESAFE_API_KEY`, or `~/.config/typesafe/credentials.env`
(keep it mode `600`). The host must also be reachable: in a Claude Code cloud
environment that means **Custom** network access with `api.typesafe.ai` in the
allowed domains, since **Trusted** does not include it.

- Excluded from the default run by `addopts = -m "not live"` in `pytest.ini`,
  so `pytest -q` never calls the API.
- Skips entirely when the key is unset.
- Capped at **20 calls per session** (`SPEND_GUARD_LIVE_MAX_CALLS`), so a loop
  bug cannot run up a bill. The current tests use 7.

