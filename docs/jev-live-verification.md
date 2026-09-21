# Verifying the Jev adapter against the real API

**Status: NOT YET RUN.** No `TYPESAFE_API_KEY` was available in the session
that built this, so every assumption listed below is still an assumption. The
adapter was written from the reference implementations
([SemDecide](https://github.com/sharziki/semdecide),
[Canny](https://github.com/qkal/Canny)) and has only ever been exercised
against a fake transport.

The test suite that checks these assumptions exists and is ready to run. Until
someone runs it with a key, treat this page as *what the code believes*, not
as verified behaviour.

## Running it

```bash
export TYPESAFE_API_KEY='...'
python3 -m pytest tests/live -m live -q -s
```

- Excluded from the default run by `addopts = -m "not live"` in `pytest.ini`,
  so `pytest -q` never calls the API.
- Skips entirely when the key is unset.
- Capped at **20 calls per session** (`SPEND_GUARD_LIVE_MAX_CALLS`), so a loop
  bug cannot run up a bill. The current tests use 7.

## What it checks

| # | Assumption under test |
|---|---|
| 1 | The response parses with `parse_response` as written |
| 2 | Every score comes back in 0..1 |
| 3 | `choice` questions return a **per-question confidence** |
| 4 | `usage` carries integer token counts usable for costing |
| 5 | A model identifier is returned |
| 6 | A bad key raises `PROVIDER_ERROR`, and the key is not in the message |
| 7 | A 1 ms timeout raises `TIMEOUT`, not `PROVIDER_ERROR` |
| 8 | The bodies actually sent contain only allowlisted fields |
| 9 | Every question really was sent as `type: "choice"` |

Assumption 3 is the one most likely to be wrong, and the most consequential.
Chapter 7 requires a confidence per question. The `noul` primitive returns a
probability with **no** confidence, so every question is sent as a two-option
`choice` instead. If the live API turns out not to return a confidence for
`choice` answers either, then `confidence_min` cannot be evaluated as
specified and the aggregation needs rethinking — not a workaround that invents
a confidence from the probability.

## No score is ever asserted

Jev's probabilities drift by roughly 0.05 between runs. The live tests assert
**structure, types and ranges only**. Observed values are printed, never
compared. Pinning a score would produce a suite that fails for reasons that
have nothing to do with the code.

## What running it produces

`test_report_latency_and_record_the_response_shape` writes
`docs/jev-live-response.json`, containing:

- the real response structure with **every leaf replaced by a type
  placeholder** — no real values, no credentials, no request bodies
- observed latency, p50 and max

That file does not exist yet, because the suite has not been run.

## Afterwards

1. If the structure differs from `parse_response`, fix the adapter and bring
   `fixtures/judge/answers.json` into line with the real response format.
2. Record the measured p50 and max latency in `docs/design.md`; they are what
   `jev.timeout_ms` should be set from.
3. If token counts arrive, fill `pricing.input_per_mtok_usd` and
   `pricing.output_per_mtok_usd` in the policy and cite the source in
   `pricing.source`. Until then `cost_usd` stays `null` rather than guessed.
