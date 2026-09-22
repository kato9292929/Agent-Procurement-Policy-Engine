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

| | MVP (inline append) | now (enqueue only) |
|---|---|---|
| p50 | 0.92 ms | **0.25 ms** |
| p99 | 1.86 ms | **0.42 ms** |
| max | 3.12 ms | **1.19 ms** |

With six other processes competing for the same ledger file:

| | MVP (inline append) | now (enqueue only) |
|---|---|---|
| p50 | 5.23 ms | **0.32 ms** |
| p99 | 8.70 ms | **0.93 ms** |
| max | 11.17 ms | **1.28 ms** |

### Where the MVP's time went

The MVP's `observe()` appended inline, so it paid for a `flock` and an
`fsync` on the payment path. Breaking that down over 300 uncontended
appends:

| step | p50 | p99 | max |
|---|---|---|---|
| normalise | 0.199 ms | 0.350 ms | 0.529 ms |
| acquire `flock` | 0.001 ms | 0.007 ms | 0.029 ms |
| seal + write | 0.060 ms | 0.133 ms | 0.166 ms |
| **`fsync`** | **0.423 ms** | **1.084 ms** | 1.801 ms |
| total | 0.759 ms | 1.603 ms | 2.484 ms |

So `fsync` was the dominant fixed cost, and `flock` was free *until
contended* — at which point it became the whole story (p50 5.23 ms above).

**The 62.7 ms outlier reported for the MVP did not reproduce.** Re-running
the MVP's exact configuration (inline append plus a worker appending
`guard_evaluated` to the same file) gave a max of 2.47 ms over 200
candidates, across repeated trials. Three causes were ruled out by
measurement rather than by argument:

- **Not GC.** Instrumenting `gc.callbacks` over 300 appends, exactly one
  iteration coincided with a collection, and it was not among the slow ones.
- **Not thread startup.** The worker starts once, long before the timings.
- **Not lock contention in-process.** Uncontended `flock` acquisition is
  about 1 microsecond.

One isolated 34.8 ms sample did appear, with no GC cycle and no lock wait,
which points at container CPU scheduling rather than anything in this code.
That is consistent with the original 62.7 ms being environment noise, but it
was not reproduced and so is **not a confirmed diagnosis**.

It also stopped mattering. `observe()` now performs no I/O at all — no lock,
no file handle, no socket — so none of `fsync` cost, lock contention or
storage availability can reach the payment path. The test
`test_observe_returns_immediately_while_another_process_holds_the_lock`
holds an exclusive `flock` on the ledger for three seconds and asserts that
`observe()` still returns in under 50 ms; it measures about 0.2 ms.

### Threads

    payment path --> _writes --> writer --> _evaluating --> evaluator
                                   ^                            |
                                   +--------- _writes ----------+

The **writer is the only thread that touches storage**, so every append in a
process is serial by construction and the ledger lock is never contended from
within. Evaluation runs on its own thread, so a slow Jev call cannot delay
recording the next candidate.

Back pressure is bounded by `hook.max_pending` and applied with an in-flight
counter rather than a bounded queue, because a bounded queue would also refuse
the decisions the evaluator feeds back — and those must never be dropped once
the observation they belong to has been written. Over the limit, candidates
are dropped and counted; the payment path never waits.

On shutdown the guard drains for up to `hook.shutdown_drain_ms`. Whatever is
left is counted in `unflushed_at_shutdown` and logged. An empty queue is not
treated as "done": the writer may have taken the last item and still be
appending it, so shutdown waits on the in-flight count instead.

Three non-interference properties are enforced at the seam rather than left to
the caller, and each is tested separately:

| Property | How it is enforced | Test |
|---|---|---|
| Whether a payment happens | `observe()` returns `None`. There is no API that hands a verdict back. | `test_payment_runs_for_every_shadow_decision`, `test_observe_returns_nothing_to_branch_on` |
| How long the payment path takes | No I/O on the payment path at all; overhead measured and reported | `test_observe_returns_immediately_while_another_process_holds_the_lock`, `test_observe_stays_fast_under_six_competing_processes` |
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

**Resolved: Postgres in production, JSONL for local work and tests.**

The MVP left this open. It is now decided, and the application half is built:
`LedgerBackend` has two implementations and the same contract tests run against
both, so they cannot drift.

**A Railway Volume was considered and rejected.** It is simpler, but a service
with a volume attached cannot run replicas and takes downtime on every
redeploy. That would change the availability of the *payment service* — the one
thing shadow mode must not touch. A separate Postgres keeps the ledger durable
without putting the payment service's uptime at the mercy of the guard.

In the table, the event is stored twice, and the difference matters:

- `event_json` — the RFC 8785 canonical form. The record of truth, and the
  **only** thing hashes are computed or verified against.
- `payload` — JSONB, for querying. JSONB reorders keys and renormalises
  numbers, so a hash taken over it would disagree with the JSONL backend for
  the very same event. `test_both_backends_produce_identical_hashes` is what
  keeps that honest.

Both columns are written from one in-memory object, so they cannot drift.

Appends serialise on `pg_advisory_xact_lock`, which covers every connection
rather than one host's file, so replicas still produce one chain. Verified with
six real processes appending concurrently.

Append-only is enforced in two independent layers, because grants cannot
restrict the table **owner** — the account someone would be using at a `psql`
prompt:

1. `spend_guard_writer` holds `INSERT` and `SELECT` only; `spend_guard_reader`
   holds `SELECT`.
2. Triggers raise on `UPDATE`, `DELETE` and `TRUNCATE`.

Both layers are tested, including against the owner.

**What is still the owner's to do:** creating the Railway Postgres, running the
migrations, setting role passwords and the environment variable. Those need
production credentials, which no code here should hold. `docs/postgres-setup.md`
is the runbook.

**The connection string** is read from the environment variable named by
`ledger.dsn_env` and never appears in the policy file, the logs, the ledger, or
any error message — Postgres errors are reduced to their exception class name
before being raised. Tested.

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

**Every threshold in `policies/shadow-v1.json` is provisional, and one is now
known to be wrong.** They are the values the instructions suggested, not
values derived from this workload.

The live run of 2026-09-22 found 11 of 20 real confidences below
`confidence_min` (0.80), which sends 4 of 5 candidates to `REVIEW` including
the clean `PAY` case. Two further problems sit underneath it: rule 5 gates on
questions that are not deciding the outcome, and `evidence_sufficiency` scores
low on every candidate with low confidence in its own answer. See
`docs/jev-live-verification.md`; the observed answers are replayed by
`tests/test_observed_live_data.py`. `shadow-v1` is deliberately left unedited.

Of those three, only the wording is urgent. `replay` re-decides stored scores
under a new policy, so thresholds and rule scope can be settled from a shadow
run's own data. It never calls Jev, so it can never produce the scores a
different wording would have given: a broken question spoils a month of that
column irrecoverably. `shadow-v2` therefore reworks `evidence_sufficiency`
before any long run, and rule 5's scope waits. See
`docs/question-rewording.md`.

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

1. **The Jev adapter has still never called the real API.** No
   `TYPESAFE_API_KEY` was available. The live suite is written and ready
   (`tests/live`, capped at 20 calls, excluded from `pytest -q`), but until it
   is run, the response structure, the presence of a per-question confidence,
   and the token fields are **assumptions**. See
   `docs/jev-live-verification.md`.
2. **`hook_timeout_ms`** for the synchronous fallback — needs the measured
   duration of the host's existing purchase call. Async is the default and does
   not read it.
3. **Retry convention** of the host flow. Until known, all unexplained
   `request_id` reuse is `DUPLICATE_REQUEST_ID`, which errs toward a person.
4. **Whether the host carries `request_id` end to end.** `OutcomeRecorder`
   correlates on it, with `link_by_decision_id` as the fallback.
5. **Jev token pricing** — `pricing` is unset, so `cost_usd` stays null rather
   than guessed. Needs step 1 first.
6. **Class 5** — still undefined; nothing depends on it.
7. **Connecting to a real x402 purchase flow** (part C) was not attempted: no
   target repository was given.
8. **Owner tasks for Postgres**: create the database, run the migrations, set
   role passwords and `SPEND_GUARD_DATABASE_URL`. See `docs/postgres-setup.md`.
9. **The human labelling rota** — who labels and when. The selection tooling
   (`guard sample`) and the criteria (`docs/labeling.md`) exist; the people do
   not.
10. **`LedgerIndex` scans the whole ledger** on construction. Fine at MVP
    volume; it needs an index or a bounded window before the ledger reaches the
    hundreds of thousands.
11. **Dropped and shutdown-lost candidates are not in the ledger.** A candidate
    that was never written leaves no trace in it by definition. They are counted
    in `ShadowGuard.counters` and emitted to the host log;
    `orphaned_candidates` in `guard report` is the ledger-visible symptom.
12. **No `LICENSE` file.** Owner's decision.
