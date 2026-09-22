# Rewording `evidence_sufficiency` (shadow-v1 → shadow-v2)

## Why this could not wait for the shadow run

Thresholds and aggregation rules can be fixed later: `guard replay` re-decides
stored scores under a new policy, so a month of data stays usable even if the
thresholds shipped with it were wrong.

**A question's wording cannot.** `replay` never calls Jev. It can re-decide
scores, but it can never produce the scores a different wording *would* have
given. A broken question means a month of `evidence_sufficiency` values that
no amount of recomputation will recover — while fixing it now costs a handful
of API calls at roughly USD 0.00003 each.

So: rule 5's scope stays as it is and gets settled from real data later; the
wording gets settled first.

## What was wrong with the shadow-v1 wording

> Is there enough information here to judge whether this purchase is needed?
> Judge the evidence available, not the purchase itself.

Measured 2026-09-22 across five candidates:

| | values |
|---|---|
| score | 0.76, 0.43, 0.34, 0.29, 0.00 |
| confidence | 0.51, 0.14, 0.32, 0.43, 1.00 |

Below 0.80 on **every** candidate, including the one written to be well
specified — and with low confidence in its own answers. Low scores together
with low confidence in those scores points at a question the model cannot get
a purchase on, rather than at genuinely thin input.

The likely reason: it is abstract and self-referential. Jev receives only the
candidate, so "is the evidence sufficient" has no fixed referent — sufficient
for whom, against what standard? With nothing to anchor to, the answer sticks
low.

## The shadow-v2 wording

> Judging only from the service description, does this service provide each
> item listed in the task's required data? Answer from the description and the
> required-data list alone; do not consider price, provider reputation, or
> whether the purchase is worthwhile.

Options change from `sufficient` / `insufficient` to `provides` /
`does_not_provide`.

Every term now names something Jev is actually sent: `service.description` and
`task.required_data`. The question is answerable from the input alone, which
is what the other three questions already are.

## Why the wording is versioned, not edited

Scores produced under two wordings are **not comparable**. Mixing them in one
column would be a silent error — the numbers all look like probabilities.

So the wording now lives in the policy, not only in code:

- A policy's `questions` block overrides any question by name; anything it
  leaves out keeps the shadow-v1 wording in `judges/base.py`.
- Every decision records `policy.question_set`, a hash of the exact set used,
  so any stored score can be traced to the wording that produced it.
- `shadow-v2` changes **only** `evidence_sufficiency`, and keeps shadow-v1's
  thresholds unchanged. Changing two things at once would make the comparison
  uninterpretable.

`shadow-v2-candidate` remains separate: it varies `confidence_min` and keeps
the v1 wording, so the two experiments do not contaminate each other.

## Measuring the rewording

```bash
TYPESAFE_API_KEY='...' python3 scripts/jev_question_ab.py \
    --before policies/shadow-v1.json \
    --after  policies/shadow-v2.json \
    --candidates fixtures/candidates/candidates.jsonl \
    --candidates fixtures/candidates/aa-routes.jsonl
```

It asks the same candidates under both wordings and reports the score and
confidence spread for each, per candidate and in aggregate. Calls are capped;
the script refuses to start and names the number it needs rather than
overspending silently.

Values are reported, never asserted — Jev drifts about 0.05 between runs and
these samples are small.

## The decision rule, fixed in advance

**If the median confidence after the rewording is below 0.60, drop
`evidence_sufficiency` as a Jev question.**

Committing to the threshold before seeing the numbers is the point; deciding
afterwards is how a bad question survives.

The replacement would be a deterministic check — `task.purpose`,
`task.required_data` and `service.description` all present and non-empty —
which is a weaker signal but an honest one, and free. That check belongs with
the other input checks in `checks.py`, and would make rule 7 fire on a missing
field rather than on a model's discomfort.

**Implementation of that fallback is not done and needs approval**, per the
instruction that an option be proposed rather than applied.
