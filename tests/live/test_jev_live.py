"""Live verification of the Jev adapter against the real TypeSafe API.

The adapter was written from the reference implementations without ever
calling the API. These tests check the assumptions that were made:

* the response structure matches what `parse_response` expects
* `choice` questions really do return a per-question confidence
* token counts usable for costing really are returned
* a bad key yields PROVIDER_ERROR and a tiny timeout yields TIMEOUT
* the request body really does carry only allowlisted fields

**No assertion pins a score to a value.** Jev's probabilities drift by around
0.05 between runs, so asserting one would make this suite flaky and would
prove nothing about the aggregation. Only structure, ranges and types are
checked; the observed numbers are reported, not asserted.

Run with:

    TYPESAFE_API_KEY=... python3 -m pytest tests/live -m live -q -s
"""

from __future__ import annotations

import json
import time
import urllib.request

import pytest
import support
from spend_guard.errors import ProviderError
from spend_guard.judges.jev import (
    PRIOR_ACQUISITION_FIELDS,
    SERVICE_FIELDS,
    JevProcurementJudge,
    build_questions,
)
from spend_guard.models import QUESTIONS
from spend_guard.normalize import normalize

pytestmark = pytest.mark.live

# Enough candidates to see the shape vary, few enough to stay inside the cap.
LIVE_CANDIDATES = [
    "cand_pay_clean",
    "cand_semantic_dupe",
    "cand_exact_repurchase",
    "cand_off_task",
    "cand_thin_evidence",
]
SHAPE_PATH = support.ROOT / "docs" / "jev-live-response.json"


def shape_of(value, depth: int = 0):
    """Describe a value's structure, replacing every leaf with a placeholder.

    What comes back from Jev is recorded for the docs, but the recording must
    not carry real content or anything key-shaped, so only types survive.
    """
    if depth > 8:
        return "<...>"
    if isinstance(value, dict):
        return {k: shape_of(v, depth + 1) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [shape_of(value[0], depth + 1)] if value else []
    if isinstance(value, bool):
        return "<bool>"
    if isinstance(value, int):
        return "<int>"
    if isinstance(value, float):
        return "<float 0..1>" if 0.0 <= value <= 1.0 else "<float>"
    if value is None:
        return "<null>"
    return "<string>"


class RecordingTransport:
    """Passes the request through, keeping the body and the raw reply."""

    def __init__(self, budget):
        self.budget = budget
        self.sent: list[dict] = []
        self.received: list[dict] = []

    def __call__(self, request, timeout):
        self.budget.spend()
        self.sent.append(json.loads(request.data.decode("utf-8")))
        started = time.perf_counter()
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
        self.budget.record((time.perf_counter() - started) * 1000)
        self.received.append(json.loads(body))
        return body


@pytest.fixture(scope="module")
def live_run(request, api_key, budget):
    """Evaluate the sample candidates once; every test reads this one run."""
    request.session._spend_guard_budget = budget
    policy = support.policy(jev={"model": "jev-latest", "timeout_ms": 20000, "retries": 1})
    transport = RecordingTransport(budget)
    judge = JevProcurementJudge(api_key=api_key, transport=transport)

    judgments = []
    for name in LIVE_CANDIDATES:
        candidate = normalize(support.candidate(name))
        judgments.append((name, judge.evaluate(candidate, policy)))
    return {"transport": transport, "judgments": judgments, "budget": budget}


# -- 1. normal path --------------------------------------------------------


def test_response_parses_into_a_judgment(live_run):
    for name, judgment in live_run["judgments"]:
        assert judgment.status == "OK", f"{name} did not parse"
        assert set(judgment.scores) == set(QUESTIONS)


def test_every_score_is_a_probability(live_run):
    """Ranges only. The values themselves are reported, never asserted."""
    for name, judgment in live_run["judgments"]:
        for question, score in judgment.scores.items():
            assert 0.0 <= score <= 1.0, f"{name}.{question} out of range: {score}"


def test_choice_questions_return_a_confidence_per_question(live_run):
    """The reason every question is a `choice` rather than a `noul`."""
    for name, judgment in live_run["judgments"]:
        assert set(judgment.confidence) == set(QUESTIONS), f"{name} lost a confidence"
        for question, value in judgment.confidence.items():
            assert 0.0 <= value <= 1.0, f"{name}.{question} confidence out of range"


def test_usage_carries_token_counts_for_costing(live_run):
    for name, judgment in live_run["judgments"]:
        usage = judgment.usage
        assert isinstance(usage.get("input_tokens"), int), f"{name} has no input_tokens"
        assert isinstance(usage.get("output_tokens"), int), f"{name} has no output_tokens"


def test_model_identifier_is_returned(live_run):
    for name, judgment in live_run["judgments"]:
        assert isinstance(judgment.model, str) and judgment.model, f"{name} has no model"


def test_report_latency_and_record_the_response_shape(live_run):
    """Not a pass/fail check: this writes the observed shape into docs/."""
    latencies = sorted(live_run["budget"].latencies_ms)
    p50, longest = latencies[len(latencies) // 2], latencies[-1]

    observed = live_run["transport"].received[0]
    SHAPE_PATH.write_text(
        json.dumps(
            {
                "_comment": (
                    "Structure of a real Jev response, recorded by "
                    "tests/live/test_jev_live.py. Every leaf is a type "
                    "placeholder: no real values, no credentials, no request "
                    "bodies are stored here."
                ),
                "observed_question_count": len(QUESTIONS),
                "latency_ms": {"p50": round(p50), "max": round(longest)},
                "response_shape": shape_of(observed),
                "request_shape": shape_of(live_run["transport"].sent[0]),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"\n[live] latency p50={p50:.0f}ms max={longest:.0f}ms -> {SHAPE_PATH}")

    for name, judgment in live_run["judgments"]:
        print(f"[live] {name}: scores={judgment.scores} confidence={judgment.confidence}")


# -- 2. failure paths ------------------------------------------------------


def test_a_bad_key_is_a_provider_error(api_key, budget):
    if api_key is None:
        pytest.skip("in proxy auth mode this process cannot choose the credential")
    budget.spend()
    judge = JevProcurementJudge(api_key="sk-definitely-not-a-valid-key", sleep=lambda _: None)
    policy = support.policy(jev={"model": "jev-latest", "timeout_ms": 20000, "retries": 0})
    with pytest.raises(ProviderError) as caught:
        judge.evaluate(normalize(support.candidate("cand_pay_clean")), policy)
    assert caught.value.kind == "PROVIDER_ERROR"
    assert "sk-definitely" not in str(caught.value), "the key leaked into the message"


def test_a_tiny_timeout_is_a_timeout(api_key, budget):
    budget.spend()
    judge = JevProcurementJudge(api_key=api_key, sleep=lambda _: None)
    policy = support.policy(jev={"model": "jev-latest", "timeout_ms": 1, "retries": 0})
    with pytest.raises(ProviderError) as caught:
        judge.evaluate(normalize(support.candidate("cand_pay_clean")), policy)
    assert caught.value.kind == "TIMEOUT"


# -- 3. what actually left the machine -------------------------------------


def test_the_real_request_carried_only_allowlisted_fields(live_run):
    """Checked against the bodies that were genuinely sent, not a simulation."""
    for sent in live_run["transport"].sent:
        assert set(sent) == {"state", "model", "questions"}
        assert set(sent["state"]) == {"task", "service", "prior_acquisitions"}
        assert set(sent["state"]["service"]) == set(SERVICE_FIELDS)
        for prior in sent["state"]["prior_acquisitions"]:
            assert set(prior) == set(PRIOR_ACQUISITION_FIELDS)
        assert set(sent["questions"]) == set(QUESTIONS)

        blob = json.dumps(sent)
        for forbidden in ("request_id", "idempotency_key", "request_params_hash",
                          "content_fingerprint", "candidate_id", "quote", "amount"):
            assert forbidden not in blob, f"{forbidden} was sent to Jev"


def test_questions_sent_were_all_choice_type(live_run):
    for sent in live_run["transport"].sent:
        for name, question in sent["questions"].items():
            assert question["type"] == "choice", f"{name} was not a choice question"
    assert set(build_questions()) == set(QUESTIONS)
