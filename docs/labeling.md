# Labelling guide: was this purchase needed?

Spend Guard records what it *would* have decided. Without human labels there
is nothing to compare those decisions against, so none of the accuracy metrics
in `guard report` can be computed and the thresholds cannot be adjusted.

This describes what each label means.

## The weekly routine

```bash
# Everything since last Monday
guard sample --since 2026-09-15T00:00:00Z --seed 20260921 --json > review.json
```

- **`HOLD` and `REVIEW`: every one.** These are the decisions the guard would
  have acted on, so a wrong one is expensive in both directions.
- **`PAY`: a 20% random sample.** A full census would cost far more labelling
  for far less signal, but the sample still catches purchases the guard waved
  through that it should not have.

Only decisions whose purchase actually completed are offered — without a
result there is nothing to judge — and already-labelled decisions are never
offered twice. Passing `--seed` makes the selection reproducible, and the
same seed keeps picking the same purchases even as the ledger grows.

Record each answer:

```bash
guard feedback --decision-id dec_... --label not_needed --note "already had the segment data"
```

Labels are appended, never overwritten. Re-labelling a decision is fine: the
latest label counts and the earlier ones stay in the ledger.

## The labels

### `not_needed` — the purchase was not necessary

Any **one** of these is enough:

- The data was not used in the task's output.
- Something already acquired would have served instead.
- The task would have completed without the purchase.

### `needed` — the purchase was necessary

**Both** must hold:

- The data was used in the task's output, **and**
- no already-available data would have served instead.

### `unknown` — not enough to go on

Use this when the record does not say whether the data was used, or when you
cannot tell whether an alternative existed. `unknown` is a real answer, not a
failure: guessing would poison the comparison that the labels exist to support.

## Things that are easy to get wrong

- **Judge the purchase, not the decision.** The question is whether the
  purchase was needed, not whether the guard's verdict looks reasonable.
  Labelling to agree with the verdict makes the comparison circular.
- **Price is irrelevant.** A cheap purchase that went unused is still
  `not_needed`; an expensive one that was essential is still `needed`.
- **Hindsight is allowed.** You may use what is known now, including whether
  the data ended up in the output. That is the whole point: the guard had to
  decide in advance, and you are measuring how well it did.
- **"It might be useful later" is `not_needed`** unless it was used in this
  task's output. Speculative purchases are exactly what shadow mode is
  measuring.

## When the thresholds get reviewed

At **100 labels**, or **two weeks** after shadow mode starts, whichever comes
first. `guard report` tracks the countdown in
`human_comparison.labels_until_threshold_review`.

At that point:

```bash
guard report --distribution      # the real score spread
guard replay --policy policies/shadow-v2-candidate.json   # what would change
```

Read these three together, never `not_needed_held` alone:

| Metric | Meaning |
|---|---|
| `not_needed_held` | unnecessary purchases the guard would have stopped — higher is better |
| `needed_held` | necessary purchases it would have stopped by mistake — **lower is better** |
| `labelled_reviewed` | share sent to a person — the workload the guard creates |

Holding everything scores a perfect `not_needed_held` and is useless. A change
to the thresholds means a **new `policy_version`**, never an edit to an
existing one, so past decisions stay reproducible.
