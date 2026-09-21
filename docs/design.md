# Spend Guard shadow mode — design decisions and open questions

This records what was decided, why, and what is still unknown. Chapter numbers
refer to the implementation instructions (v2).

---

## 0. What the repository contained before this work

**The repository was empty.** No commits locally, and `git ls-remote origin`
returned nothing. There was no existing x402 purchase flow to attach to.

The instructions assume a host codebase and forbid choosing endpoint names,
wallets, environment variables, payment routes, or a ledger location before
inspecting it. That inspection was done and found nothing, so each of those
questions is answered below either by a decision recorded here with its
reasoning, or by an explicit TODO where no defensible answer exists yet.

| Chapter 0 question | Finding |
|---|---|
| Current branch / HEAD | `claude/hopeful-newton-9mfkeg`, no commits (HEAD unborn) |
| Working tree | Empty; no tracked or untracked files |
| Where x402 candidates are generated | **Does not exist** |
| Where real settlement begins | **Does not exist** |
| Where payment results / API responses / logs are stored | **Does not exist** |
| Sync or async, how many workers | **Unknown** — no host process to inspect |
| Existing retry / idempotency mechanism | **Does not exist**; see "Retry detection" below |
| Is a correlation id carried candidate → result | **Unknown**; see "Correlating real purchases" |
| Durable ledger storage available | **Unknown**; see "Where the ledger lives" |
| Files read | None existed. The two reference repositories were cloned read-only for design study. |
| Files written | Everything in this repository (see README) |

Because no payment path exists, **nothing in this repository is wired to one.**
The integration point is built and tested as a seam (`ShadowGuard`), against a
stand-in payment flow in `tests/test_payment_path.py`. Wiring it to a real flow
is future work that cannot be done or verified yet.

No wallet, endpoint, or environment variable was invented. The only external
endpoint referenced is Jev's, which comes from the reference implementation
rather than from a choice made here.

---

## Terms the instructions left as TODO

The instruction document listed *Jev*, *TypeSafe*, and *Class 5* as TODO. Two
of the three are resolved from the cited reference repositories, which are the
instructions' own source:

- **Jev** — the first of TypeSafe's *System One* models. It does not generate
  text; it answers a typed question about a piece of state with a calibrated
  probability. Called over `POST https://api.typesafe.ai/v1/systemone` with a
  bearer token from `TYPESAFE_API_KEY`. Model alias `jev-latest`. Question
  primitives: `noul` (yes/no), `choice` (named options), `score` (ordered
  rubric). Source: `sharziki/semdecide` `src/reflex_guard/providers/typesafe.py`
  and `qkal/canny` `README.md`.
- **TypeSafe** — TypeSafe AI (typesafe.ai), the provider hosting the Jev API.
  Jev is their model; SemDecide and Canny are independent projects that call it.
- **Class 5** — **still unresolved.** It appears in the instructions only in the
  out-of-scope list, so nothing here depends on it. No guess was made.

Jev's probabilities are calibrated but drift by roughly 0.05 between runs. That
drift is why the thresholds below have a deliberate gap around each decision
boundary, and why no test asserts an exact probability from a live call.

---

## Language and runtime

**Python 3.11+, standard library only.**

The instructions say to match the existing language and CLI conventions. There
were none, so this is a fresh choice:

- The runtime has **no dependencies at all**, so the guard cannot break a
  payment path by dragging in a conflicting package. This is the single most
  important property for something that sits next to money, and it is the same
  choice both reference projects made.
- `jsonschema` is used by the test suite only, and those tests skip when it is
  absent.

Revisit this if the host x402 flow turns out to be TypeScript — the seam is
small enough to port, and the ledger format is language-neutral by design.

---

## Evaluation is asynchronous (chapter 6)

**Decision: asynchronous by default.** `ShadowGuard.observe()` runs on the
payment path and does only three things: normalise the candidate, append
`candidate_observed`, and enqueue. Checks, the Jev call and `guard_evaluated`
happen on a worker thread.

Shadow mode does not use the verdict to decide anything, so making the payment
path wait for a network call to Jev would buy nothing and cost latency on the
one path where latency is least acceptable.

Measured, 200 candidates against a judge deliberately made 250 ms slow:

| | inline cost on the payment path |
|---|---|
| p50 | 0.99 ms |
| p95 | 1.52 ms |
| max | 62.7 ms |
| total | 265 ms, against 50,000 ms had it been synchronous |

**The tail is worth knowing about.** `observe()` appends `candidate_observed`
under `flock` and `fsync`s it, so it contends with the worker thread appending
`guard_evaluated` to the same file. p50 and p95 stay near a millisecond, but a
contended append can reach tens of milliseconds. That is the price of a durable
record of the candidate, and durability is the right trade for the observation
itself — but if a host's purchase path cannot tolerate a ~60 ms tail, the fix is
to buffer the `candidate_observed` append rather than to make evaluation
synchronous. Not done here: with no host to measure against, tuning this would
be guesswork.

A synchronous mode exists for hosts that cannot run a worker thread. It is
bounded by `hook_timeout_ms`, which is **TODO**: a sound value needs the
measured duration of the host's existing purchase call, and there is no host
call to measure. When setting it, also set `jev.timeout_ms` below it — the
per-request timeout is what actually bounds the wait; the budget check only
records an overrun after the fact.

Three non-interference properties are enforced at the seam rather than left to
the caller, and each is tested separately:

| Property | How it is enforced | Test |
|---|---|---|
| Whether a payment happens | `observe()` returns `None`. There is no API that hands a verdict back. | `test_payment_runs_for_every_shadow_decision`, `test_observe_returns_nothing_to_branch_on` |
| How long the payment path takes | Async worker; overhead measured and reported | `test_async_mode_keeps_a_slow_jev_off_the_payment_path` |
| How it fails | Every exception caught at the boundary; losses counted in `dropped` | `test_guard_exception_does_not_reach_the_payment_path`, `test_unwritable_ledger_does_not_reach_the_payment_path` |

Back pressure is dropped rather than queued indefinitely: a full queue must not
turn into latency on the payment path. Drops are counted and logged by class
only — never with candidate data.

---

## The ledger

### Concurrent writes

`event_hash` chains each event to its predecessor, so two processes appending
at once would both claim the same predecessor and fork the chain. Every append
therefore takes an exclusive `flock` spanning *both* reading the current head
and writing the new line.

The head is found by seeking backwards from the end of the file in blocks, not
by scanning the whole ledger, so append stays O(1) in the ledger's length.

This is verified across **real processes**, not just threads:
`tests/test_concurrency.py` runs six subprocesses appending 120 events total
and asserts the chain verifies end to end.

**Limit:** `flock` is per-host. A deployment spread over several machines with
a shared filesystem needs a single writer process instead. This is recorded as
a TODO rather than solved, because the deployment shape is unknown.

### Where the ledger lives

**TODO — unresolved, and deliberately not guessed.**

Chapter 0 asks whether durable storage exists. There is no host application and
no deployment to inspect, so this cannot be answered. Changing the production
environment is out of scope, which rules out provisioning storage here.

The default is a local path (`./var/spend-guard/ledger.jsonl`) and every
component takes the path as a parameter. **Nothing in this repository has been
run against a production environment, and no claim is made that records would
survive a redeploy there.** If the eventual host has an ephemeral filesystem,
the ledger must move to durable storage before shadow mode means anything —
metrics computed from a ledger that vanishes are worthless.

### What is never stored

Keys matching `authorization`, `api_key`, `secret`, `token`, `password`,
`private_key`, `signature`, `mnemonic`, `cookie`, `credential`, `bearer`, or
`wallet_key` are replaced with `[redacted]` on the way in, at any nesting
depth. The ledger file is created mode `0600`.

Provider-specific error text is never stored: a Jev failure is recorded as one
of `PROVIDER_ERROR`, `TIMEOUT`, or `INVALID_RESPONSE`, with the detail kept on
the exception for operator logs only.

### Canonical JSON

`input_hash` and `event_hash` use RFC 8785 (JSON Canonicalization Scheme), so
reordering keys or reindenting the input never changes a hash. Python's
`json.dumps(sort_keys=True)` is *not* sufficient — it sorts by code point where
JCS sorts by UTF-16 code unit, and it formats numbers differently from
ECMAScript above 1e16 and below 1e-6. Both are implemented in `canonical.py`
and tested.

`input_hash` deliberately excludes `candidate_id`: two observations of the same
purchase carry different candidate ids, and if they hashed differently the
re-evaluation guard in chapter 9 would never fire.

---

## Correlating real purchases (chapter 12)

**Primary: `request_id`.** `OutcomeRecorder` looks up the decision recorded for
a request id and appends `purchase_attempted`, `purchase_result` and
`delivery_observed` against it.

**Whether the host actually carries a `request_id` end to end is unknown** —
there is no host. `OutcomeRecorder.link_by_decision_id` is the fallback for a
host that does not, where the host hands back the `decision_id` instead.

Chapter 12 asks that a host needing a new correlation mechanism be reported
before implementation rather than changed unilaterally. That point has not been
reached: no host payment logic has been modified, because none exists.

`payment_status` permits `unknown` on purpose. A request that timed out after
broadcasting is genuinely undetermined, and recording it as a failure would
understate double-spend risk.

---

## Retry detection (chapter 4)

Chapter 4 says to follow whatever the host already does, and to treat every
reappearance as `DUPLICATE_REQUEST_ID` if that cannot be determined.

It cannot be determined — there is no host. So:

- A reappearing `request_id` counts as a retry **only** with a matching
  host-set `idempotency_key`, or an explicit `retry_of` marker.
- **Every other reappearance is `DUPLICATE_REQUEST_ID`**, which aggregates to
  `REVIEW`.

This errs toward sending possible double spends to a person. Revisit once a
host flow exists; the setting is `retry_detection` in the policy file.

One subtlety worth knowing: the candidate's own `candidate_observed` event is
already in the ledger by the time the checks run, so the duplicate lookup must
exclude the decision under evaluation. Without that exclusion every candidate
matches itself. This was caught in testing and is covered by
`test_first_sighting_of_a_request_id_raises_no_signal`.

---

## What Jev is asked, and what it is sent

Four narrow, independent questions: `task_fit`, `incremental_value`,
`duplication_risk`, `evidence_sufficiency`. Price fairness, counterparty
trustworthiness and safety are **not** asked — without comparison data those
answers would be guesses recorded as evidence. A test asserts the question text
contains no such terms.

Each is sent as a `choice` question with two named options rather than a `noul`
predicate. `noul` answers carry a probability but no confidence, and chapter 7
requires a confidence per question; deriving one from a bare probability would
be inventing it. The score is the probability of the named positive option.

**The send limit is enforced by construction.** `project_state` builds the
payload from an explicit allowlist rather than by filtering the candidate, so a
field added to the candidate later cannot start leaving the machine by
accident. Withheld: every credential, `request_id`, `idempotency_key`,
`request_params_hash`, `content_fingerprint`, `candidate_id`, and the quoted
amount. Hashes are withheld because they carry no meaning a semantic judge
could use — `summary` is what lets Jev reason about what is already held.

---

## Thresholds

**Every threshold in `policies/shadow-v1.json` is provisional.** They are the
values the instructions suggested, not values derived from this workload.

Fixtures cannot be used to pick thresholds. Fixture scores are invented, so the
distribution they form says nothing about how Jev's output is actually
distributed. Treating a passing fixture suite as threshold validation would be
the central mistake here.

The procedure for replacing them:

1. Run shadow mode. Every pre-aggregation score and confidence is stored, so
   nothing is lost to the current thresholds.
2. Collect `human_feedback` via `guard feedback`.
3. `guard report --distribution` shows the real score spread.
4. `guard replay --policy <candidate>` re-decides the whole ledger under a new
   policy, offline, and prints exactly which decisions move.
5. A change means a **new `policy_version`**, never an edit in place — replay
   of historical decisions must stay reproducible.

`confidence_min` is the most volatile knob: it can make almost everything
`REVIEW` or make `REVIEW` never fire. Check the `REVIEW` share early and report
it if it lands at either extreme.

Thresholds are loaded from a version-controlled file, never hardcoded, and
nothing in this package writes a policy file back.

---

## Reference projects and licence check

Both were cloned read-only and studied. **No code was copied from either.** The
designs below were adopted; everything in `src/` was written for this project.

| Project | Licence | Checked | Dependencies |
|---|---|---|---|
| [qkal/Canny](https://github.com/qkal/Canny) | MIT (Copyright (c) 2026 Kal) | `LICENSE` read in the clone | TypeScript/pnpm — none pulled in |
| [sharziki/semdecide](https://github.com/sharziki/semdecide) | MIT (Copyright (c) 2026 Sharvil Saxena) | `LICENSE` read in the clone | `dependencies = []` — none pulled in |

Since no source was copied, MIT attribution is not triggered; both are credited
here and in the README regardless. **This repository has no `LICENSE` file yet
— that is the owner's decision to make** and is left as a TODO.

From Canny: separating observed fact from model judgment in the record;
append-only events; recording model, thresholds, input hash and latency; the
ledger and fixed checks working with Jev unavailable; replaying the record to
re-derive verdicts; the judge as a swappable adapter.

From SemDecide: a versioned JSON schema; `REVIEW` instead of forcing borderline
results into a binary; separating semantic uncertainty, missing input, provider
failure and parse failure into distinct states; JSONL in and out; keeping
provider-specific errors out of stored output; and keeping deterministic checks
around money regardless of what the model says.

---

## Open TODOs

Unresolved, and not guessed at:

1. **Ledger storage location in production**, including whether the filesystem
   survives a redeploy. Blocks any claim that shadow records persist.
2. **`hook_timeout_ms`** for synchronous mode — needs the measured duration of
   the host's existing purchase call.
3. **Retry convention** of the host flow. Until known, all unexplained
   `request_id` reuse is `DUPLICATE_REQUEST_ID`.
4. **Whether the host carries `request_id` end to end.** If not, the correlation
   mechanism must be proposed and agreed before any host change.
5. **Jev token pricing** — `pricing` is unset, so `cost_usd` stays null rather
   than being guessed. Fill both rates and cite the source before reading any
   cost figure.
6. **Class 5** — still undefined; nothing depends on it.
7. **Human evaluation operations** (chapter 14): who labels, how often, how
   candidates are sampled (suggested: all `HOLD` and `REVIEW`, a fixed random
   share of `PAY`), and what "was not needed" means precisely. Without this
   none of the comparison metrics can be computed.
8. **Multi-host ledger writes** — `flock` covers one host only.
9. **`LedgerIndex` scans the whole ledger** on construction. Fine at MVP volume;
   it needs an index file before the ledger reaches the hundreds of thousands.
10. **`ledger_write_failures`** in `guard report` is always null. It is not
    derivable from the ledger itself — a failed write leaves no record there by
    definition — and must come from the host's own log.
11. **No `LICENSE` file.** Owner's decision.
