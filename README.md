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

> **Status.** There is no host x402 purchase flow yet, so nothing here is wired
> to one; the integration point is built and tested as a seam against a
> stand-in payment flow. No real payment has been evaluated, and **the Jev
> adapter has never called the real API** — the live suite is written but needs
> a key (see [`docs/jev-live-verification.md`](docs/jev-live-verification.md)).
> Open questions are in [`docs/design.md`](docs/design.md).

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

# Pick what a person should label this week, then record their answer
python3 -m spend_guard.cli --ledger var/ledger.jsonl sample --seed 20260921
python3 -m spend_guard.cli --ledger var/ledger.jsonl \
  feedback --decision-id dec_... --label not_needed --note "already had this"

# Check the hash chain, and move a ledger between stores
python3 -m spend_guard.cli --ledger var/ledger.jsonl verify
python3 -m spend_guard.cli --backend postgres import --from var/ledger.jsonl
python3 -m spend_guard.cli --backend postgres export --out backup.jsonl
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
| **How long the payment path takes** | `observe()` performs **no I/O at all** — no lock, no file handle, no socket. A writer thread owns every append. Measured p50 0.25 ms, and 0.32 ms with six processes fighting over the ledger |
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

Append-only. Events are never updated; a later fact about a decision is a new
event carrying the same `decision_id`.

Two backends behind one interface, with the **same contract tests run against
both** so they cannot drift:

| | |
|---|---|
| `jsonl` | local development and tests. Appends take an exclusive `flock`. |
| `postgres` | production. Appends serialise on a transaction advisory lock, which covers every connection rather than one host's file. The table is append-only by grant *and* by trigger — the trigger layer exists because grants cannot restrict the table owner. |

Set it with `ledger.backend` in the policy, or `--backend` on any command.
See [`docs/postgres-setup.md`](docs/postgres-setup.md) for the production
runbook. A Railway Volume was considered and rejected: a service with a volume
cannot run replicas and takes downtime on redeploy, which would change the
*payment service's* availability.

`candidate_observed` · `guard_evaluated` · `purchase_attempted` ·
`purchase_result` · `delivery_observed` · `human_feedback`

Each event is hash-chained to its predecessor, so tampering is detectable
(`report --verify-chain`). Appends take an exclusive `flock` spanning both
reading the head and writing, verified across real processes in
`tests/test_concurrency.py`.

Hashes use RFC 8785 canonical JSON, so reordering keys or reindenting never
changes a hash. In Postgres the canonical form is stored in `event_json` and is
the only thing hashed; the JSONB `payload` column is for querying only, because
JSONB reorders keys and renormalises numbers.

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
  backends/      LedgerBackend: jsonl and postgres, one contract
  ledger.py      the append-only event log over a backend
  engine.py      prepare/decide are pure; record_* are the only writes
  hook.py        the payment-path seam: no I/O, writer thread, fail-open
  outcome.py     attaching real purchase and delivery results
  report.py      replay, metrics, orphan detection, review sampling
  cli.py         evaluate / replay / report / feedback / sample / verify / export / import
policies/        versioned thresholds (shadow-v1)
migrations/      Postgres schema and append-only enforcement
schemas/         JSON Schema for candidate, decision, ledger event
fixtures/        candidates and recorded Jev answers, for offline runs
samples/         a worked ledger, report, and replay
docs/            design decisions, Postgres runbook, labelling guide
```

## Tests

```bash
python3 -m pytest -q                       # no network, no database needed

SPEND_GUARD_TEST_DATABASE_URL=postgresql://... python3 -m pytest -q   # adds the Postgres suites
TYPESAFE_API_KEY=... python3 -m pytest tests/live -m live -q -s       # calls the real Jev API
```

The default run needs no network, no database and no key: the Postgres tests
skip without a DSN, and the live tests are excluded outright by
`addopts = -m "not live"`.

The runtime has **no dependencies**. `psycopg` is needed only for the Postgres
backend (`pip install 'spend-guard[postgres]'`) and is imported inside that
backend, never at package import time.

Tests against the live Jev API assert **structure, types and ranges only** —
never a score. Jev's probabilities drift by around 0.05 between runs, so
pinning one would make the suite flaky and would prove nothing about the
aggregation.

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

## Human review

Shadow mode is only worth running if someone labels the results, and
`not_needed_held` on its own is meaningless — holding everything scores
perfectly. Read it with `needed_held` and `labelled_reviewed`.
[`docs/labeling.md`](docs/labeling.md) defines the labels and the weekly
routine; `guard sample` picks the work (all `HOLD` and `REVIEW`, 20% of `PAY`,
reproducible with `--seed`).

## Scope

**Not implemented, on purpose:** allowing or blocking real settlement; anything
touching wallets; production environment changes; new paid APIs or x402
endpoints; Procurement Router provider selection; Delivery Review content
evaluation; Class 5 work ordering.
