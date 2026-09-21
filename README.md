# Agent Procurement Policy Engine — x402 Spend Guard (shadow mode)

Shadow mode watches x402 purchase candidates and records the decision it
*would* have made. **It never changes whether a payment happens.**

The point is not to give a model spending authority. It is to measure, on real
data, which purchases should have been stopped, whether needed purchases would
have been stopped by mistake, and whether the inputs needed to judge at all are
even present.

```
PAY      the evidence supports this purchase
HOLD     redundant, off-task, or already bought
REVIEW   a person should look: uncertain, unexplained, or the guard itself failed
```

`REVIEW` is counted separately from `HOLD` everywhere. Sending a purchase to a
person is a different outcome from deciding it should not happen.

> **Status.** This repository was empty before this work; there is no host x402
> purchase flow yet, so nothing here is wired to one. The integration point is
> built and tested as a seam against a stand-in payment flow. No real payment
> has been evaluated, and several operational questions are open — see
> [`docs/design.md`](docs/design.md).

---

## Quick start

No installation and no API key needed for a dry run — everything below runs
offline against fixtures.

```bash
export PYTHONPATH=src

# Evaluate a stream of candidates without ever calling Jev
python3 -m spend_guard.cli \
  --ledger var/ledger.jsonl \
  evaluate --input fixtures/candidates/candidates.jsonl --jsonl \
  --dry-run --fixture fixtures/judge/answers.json

# What happened, and is the ledger intact
python3 -m spend_guard.cli --ledger var/ledger.jsonl report --verify-chain --distribution

# Re-decide the whole ledger under a different policy, offline
python3 -m spend_guard.cli --ledger var/ledger.jsonl replay --policy policies/shadow-v1.json

# Record what a person thought afterwards
python3 -m spend_guard.cli --ledger var/ledger.jsonl \
  feedback --decision-id dec_... --label not_needed --note "already had this"
```

Against the real model, drop `--dry-run --fixture` and set `TYPESAFE_API_KEY`.

### Exit codes

Semantic uncertainty and system failure never share a code.

| Code | Meaning |
|---:|---|
| `0` | `PAY` |
| `10` | `HOLD` |
| `20` | `REVIEW` |
| `2` | invalid local input |
| `4` | Jev provider error |
| `5` | ledger write error |

The ledger-error code is for CLI use only. On the payment path no exception
ever leaves the guard.

---

## Integrating with a purchase flow

```python
from spend_guard import Ledger, Policy, ShadowEngine, ShadowGuard
from spend_guard.judges import JevProcurementJudge

guard = ShadowGuard(
    ShadowEngine(
        Policy.load("policies/shadow-v1.json"),
        JevProcurementJudge(),
        Ledger("/var/lib/spend-guard/ledger.jsonl"),
    ),
    log=my_existing_logger,
).start()

def buy(candidate):
    guard.observe(candidate)     # shadow: returns nothing, raises nothing
    return existing_purchase(candidate)   # unchanged
```

`observe()` returns `None` by design. There is deliberately no API that hands a
verdict back to the payment path, because in shadow mode nothing may act on it.

Afterwards, attach what really happened:

```python
from spend_guard.outcome import OutcomeRecorder

outcomes = OutcomeRecorder(guard.engine.ledger)
outcomes.purchase_result(request_id, payment_status="success", http_status=200)
```

### The three things that stay unchanged

| | How it is guaranteed |
|---|---|
| **Whether a payment happens** | No verdict is returned; the caller has nothing to branch on |
| **How long the payment path takes** | Judgment runs on a worker thread; the payment path pays only for normalise-and-enqueue |
| **How the payment path fails** | Every exception — Jev, ledger, normalisation — is caught at the seam; losses are counted, not raised |

Each is tested separately in `tests/test_payment_path.py`, including a ledger
that cannot be written and a judge that throws.

---

## How a decision is made

Deterministic checks and Jev's answers are **never blended into one score.**

**Code-owned checks** — required fields, and duplication: a `request_id`
reappearing without evidence of a retry (`DUPLICATE_REQUEST_ID`), a retry that
*is* identifiable (`RETRY_OBSERVED`, not a failure), and the identical request
bought again inside the window (`EXACT_REPURCHASE`).

**Jev** answers four narrow, independent questions: `task_fit`,
`incremental_value`, `duplication_risk`, `evidence_sufficiency`. It is never
asked about price fairness, counterparty trust, or safety — without comparison
data those answers would be guesses recorded as evidence.

Rules run in order; the first match decides, and every match contributes a
reason code:

1. missing required input or a failed check → `REVIEW` *(Jev is not called)*
2. `DUPLICATE_REQUEST_ID` → `REVIEW`
3. `EXACT_REPURCHASE` → `HOLD`
4. Jev failed, timed out, or returned nonsense → `REVIEW`
5. any answer below `confidence_min` → `REVIEW`
6. `duplication_risk` at or above its ceiling → `HOLD`
7. `evidence_sufficiency` below its floor → `REVIEW`
8. `task_fit` or `incremental_value` below its floor → `HOLD`
9. otherwise → `PAY`

Rules 2 and 3 still call Jev, so the two kinds of judgment can be compared later.

**Thresholds are provisional.** They live in `policies/shadow-v1.json`, are
never hardcoded, and cannot be chosen from fixtures — fixture scores are
invented. See "Thresholds" in [`docs/design.md`](docs/design.md).

---

## The ledger

Append-only JSONL. Events are never updated; a later fact about a decision is a
new event carrying the same `decision_id`.

`candidate_observed` · `guard_evaluated` · `purchase_attempted` ·
`purchase_result` · `delivery_observed` · `human_feedback`

Each event is hash-chained to its predecessor, so tampering is detectable
(`report --verify-chain`). Appends take an exclusive `flock` spanning both
reading the head and writing, verified across real processes in
`tests/test_concurrency.py`.

Hashes use RFC 8785 canonical JSON, so reordering keys or reindenting never
changes a hash.

Secret-looking keys are redacted on the way in and the file is mode `0600`.
Provider-specific error text is never stored — only `PROVIDER_ERROR`,
`TIMEOUT`, or `INVALID_RESPONSE`.

---

## Layout

```
src/spend_guard/
  canonical.py   RFC 8785 canonical JSON and hashing
  normalize.py   host data -> the candidate shape; missing values stay null
  checks.py      code-owned checks (required fields, duplication)
  judges/        ProcurementJudge; Jev adapter and offline fixture judge
  aggregate.py   the rules above (pure, which is what makes replay sound)
  ledger.py      append-only hash-chained JSONL
  engine.py      observe -> check -> judge -> aggregate -> record
  hook.py        the payment-path seam: async, fail-open
  outcome.py     attaching real purchase and delivery results
  report.py      replay and the shadow metrics
  cli.py         guard evaluate / replay / report / feedback
policies/        versioned thresholds (shadow-v1)
schemas/         JSON Schema for candidate, decision, ledger event
fixtures/        candidates and recorded Jev answers, for offline runs
samples/         a worked ledger, report, and replay
docs/design.md   decisions, trade-offs, and open TODOs
```

## Tests

```bash
PYTHONPATH=src:tests python3 -m unittest discover -s tests -v
```

99 tests, no network. The runtime has **no dependencies**; `jsonschema` is used
only by the schema-conformance tests, which skip when it is absent.

Tests against the live Jev API are deliberately not part of this suite:
probabilities drift between runs, so asserting exact values would make the
suite flaky and would prove nothing about the aggregation.

---

## Jev

[Jev](https://typesafe.ai) is the first of TypeSafe's System One models. It does
not generate text; it answers a typed question about a piece of state with a
calibrated probability. Set `TYPESAFE_API_KEY` to enable it; override the
endpoint with `SPEND_GUARD_JEV_URL`.

**What is sent.** The payload is built from an explicit allowlist — the task
purpose and required data, the service identifiers and description, and short
summaries of what was already acquired. Never sent: any credential,
`request_id`, `idempotency_key`, parameter hashes, content fingerprints, or the
quoted amount.

Every Jev call goes through one interface, `ProcurementJudge`, so another model
or a local judge can replace it without touching the checks, the aggregation,
or the ledger.

Jev being unreachable is recorded as a provider failure, never as a low score:
an outage and a confident "no" mean opposite things.

---

## Prior art

Design ideas were adopted from two MIT-licensed projects. No code was copied
from either; see the licence check in [`docs/design.md`](docs/design.md).

- [Canny](https://github.com/qkal/Canny) — append-only ledgers, replayable
  verdicts, fixed checks that keep working when the model is unavailable.
- [SemDecide](https://github.com/sharziki/semdecide) — typed semantic decisions,
  `REVIEW` rather than forcing a binary, and keeping uncertainty separate from
  provider failure.

## Scope

**Not implemented, on purpose:** allowing or blocking real settlement; anything
touching wallets; production environment changes; new paid APIs or x402
endpoints; Procurement Router provider selection; Delivery Review content
evaluation; Class 5 work ordering.
