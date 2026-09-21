# Worked example

Generated offline from `fixtures/` with `--dry-run`, so **no real purchase and
no real Jev call is represented here.** The scores in the ledger are invented
fixture values.

- `ledger.sample.jsonl` — all six event types, hash-chained
- `report.sample.json` — the shadow metrics
- `replay.sample.json` — the same ledger re-decided under `shadow-v1`
- `sample.sample.json` — what `guard sample` would put in front of a person

Reproduce:

```bash
export PYTHONPATH=src
python3 -m spend_guard.cli --ledger samples/ledger.sample.jsonl \
  evaluate --input fixtures/candidates/candidates.jsonl --jsonl \
  --dry-run --fixture fixtures/judge/answers.json
```

## Reading it

The `PAY`/`HOLD`/`REVIEW` split is **1 / 3 / 5**. Do not read that as an
expected rate: the fixtures were chosen to exercise every aggregation rule
once, so failure cases are massively over-represented by construction. Real
rates can only come from shadow running against real traffic.

What the sample *does* show is the comparison the MVP exists to make. With five
decisions labelled by a person:

- `not_needed_held: 1.0` — every purchase a person judged unnecessary was held
- `needed_held: 0.0` — no purchase a person judged necessary was held
- `labelled_reviewed: 0.2` — one in five labelled decisions went to a person

Those three read together, not `not_needed_held` alone. Holding everything
would also score 1.0 there, and would be useless.

`label_coverage_by_decision` shows the same split per verdict, which is what
the weekly routine is graded against: `HOLD` and `REVIEW` should reach 1.0,
while `PAY` is only sampled.
