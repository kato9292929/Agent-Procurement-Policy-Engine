"""The Jev adapter, exercised over a fake transport. Never touches the network."""

from __future__ import annotations

import json
import unittest
import urllib.error
import urllib.request

import support
from spend_guard.errors import ProviderError
from spend_guard.judges.jev import (
    JevProcurementJudge,
    apply_pricing,
    build_questions,
    parse_response,
)
from spend_guard.models import QUESTIONS
from spend_guard.normalize import normalize


def good_body(**overrides) -> dict:
    answers = {
        "task_fit": {"choice": "fits", "probabilities": {"fits": 0.94, "does_not_fit": 0.06}, "confidence": 0.93},
        "incremental_value": {"choice": "adds_value", "probabilities": {"adds_value": 0.87, "no_new_value": 0.13}, "confidence": 0.82},
        "duplication_risk": {"choice": "distinct", "probabilities": {"duplicate": 0.11, "distinct": 0.89}, "confidence": 0.90},
        "evidence_sufficiency": {"choice": "sufficient", "probabilities": {"sufficient": 0.91, "insufficient": 0.09}, "confidence": 0.88},
    }
    body = {"answers": answers, "model": "jev-1.13.0", "usage": {"input_tokens": 850, "output_tokens": 120}}
    body.update(overrides)
    return body


class Transport:
    """Captures the request and returns a canned body, an error, or a timeout."""

    def __init__(self, body=None, error: Exception | None = None):
        self.body = body
        self.error = error
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return json.dumps(self.body).encode("utf-8")


class JevAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.candidate = normalize(support.candidate("cand_pay_clean"))
        self.policy = support.policy(jev={"model": "jev-latest", "timeout_ms": 100, "retries": 0})

    def judge(self, transport) -> JevProcurementJudge:
        return JevProcurementJudge(api_key="test-key", transport=transport, sleep=lambda _: None)

    def test_a_good_answer_becomes_scores_and_confidences(self):
        judgment = self.judge(Transport(good_body())).evaluate(self.candidate, self.policy)
        self.assertEqual(judgment.status, "OK")
        self.assertEqual(judgment.model, "jev-1.13.0")
        self.assertAlmostEqual(judgment.scores["task_fit"], 0.94)
        self.assertAlmostEqual(judgment.scores["duplication_risk"], 0.11)
        self.assertEqual(set(judgment.confidence), set(QUESTIONS))

    def test_duplication_risk_reads_the_duplicate_probability(self):
        """The score must be P(duplicate), not P(chosen option)."""
        body = good_body()
        body["answers"]["duplication_risk"] = {
            "choice": "duplicate",
            "probabilities": {"duplicate": 0.84, "distinct": 0.16},
            "confidence": 0.9,
        }
        judgment = self.judge(Transport(body)).evaluate(self.candidate, self.policy)
        self.assertAlmostEqual(judgment.scores["duplication_risk"], 0.84)

    def test_the_request_carries_only_the_allowlisted_state(self):
        transport = Transport(good_body())
        self.judge(transport).evaluate(self.candidate, self.policy)
        sent = json.loads(transport.requests[0].data.decode("utf-8"))
        self.assertEqual(set(sent), {"state", "model", "questions"})
        self.assertEqual(set(sent["state"]), {"task", "service", "prior_acquisitions"})
        self.assertEqual(set(sent["questions"]), set(QUESTIONS))
        self.assertNotIn("test-key", json.dumps(sent))

    def test_the_api_key_travels_in_the_header_not_the_body(self):
        transport = Transport(good_body())
        self.judge(transport).evaluate(self.candidate, self.policy)
        request = transport.requests[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        self.assertNotIn("test-key", request.data.decode("utf-8"))

    def test_every_question_is_a_choice_so_a_confidence_comes_back(self):
        for name, question in build_questions().items():
            with self.subTest(name=name):
                self.assertEqual(question["type"], "choice")
                self.assertEqual(len(question["criteria"]), 2)

    def test_price_fairness_and_trust_are_never_asked(self):
        blob = json.dumps(build_questions()).lower()
        for forbidden in ("price", "cost", "trustworth", "safe", "fraud"):
            self.assertNotIn(forbidden, blob)

    # -- failure modes stay distinct --

    def test_http_error_is_a_provider_error(self):
        error = urllib.error.HTTPError("u", 500, "boom", {}, None)
        with self.assertRaises(ProviderError) as caught:
            self.judge(Transport(error=error)).evaluate(self.candidate, self.policy)
        self.assertEqual(caught.exception.kind, "PROVIDER_ERROR")

    def test_timeout_is_its_own_kind(self):
        with self.assertRaises(ProviderError) as caught:
            self.judge(Transport(error=TimeoutError("slow"))).evaluate(self.candidate, self.policy)
        self.assertEqual(caught.exception.kind, "TIMEOUT")

    def test_a_socket_timeout_wrapped_in_urlerror_is_still_a_timeout(self):
        error = urllib.error.URLError(TimeoutError("timed out"))
        with self.assertRaises(ProviderError) as caught:
            self.judge(Transport(error=error)).evaluate(self.candidate, self.policy)
        self.assertEqual(caught.exception.kind, "TIMEOUT")

    def test_malformed_json_is_an_invalid_response(self):
        class Broken(Transport):
            def __call__(self, request, timeout):
                return b"{not json"

        with self.assertRaises(ProviderError) as caught:
            self.judge(Broken()).evaluate(self.candidate, self.policy)
        self.assertEqual(caught.exception.kind, "INVALID_RESPONSE")

    def test_a_missing_answer_is_an_invalid_response(self):
        body = good_body()
        del body["answers"]["task_fit"]
        with self.assertRaises(ProviderError) as caught:
            self.judge(Transport(body)).evaluate(self.candidate, self.policy)
        self.assertEqual(caught.exception.kind, "INVALID_RESPONSE")

    def test_an_out_of_range_probability_is_an_invalid_response(self):
        body = good_body()
        body["answers"]["task_fit"]["probabilities"]["fits"] = 1.4
        with self.assertRaises(ProviderError):
            parse_response(body)

    def test_a_missing_confidence_is_an_invalid_response(self):
        body = good_body()
        del body["answers"]["task_fit"]["confidence"]
        with self.assertRaises(ProviderError):
            parse_response(body)

    def test_provider_detail_stays_out_of_the_message(self):
        """Chapter 9: provider-specific error text must not be stored as-is."""
        error = urllib.error.HTTPError("u", 503, "upstream pool exhausted at host-7", {}, None)
        with self.assertRaises(ProviderError) as caught:
            self.judge(Transport(error=error)).evaluate(self.candidate, self.policy)
        self.assertNotIn("host-7", str(caught.exception))

    def test_transient_errors_are_retried_then_surface(self):
        transport = Transport(error=urllib.error.HTTPError("u", 503, "x", {}, None))
        policy = support.policy(jev={"model": "jev-latest", "timeout_ms": 100, "retries": 2})
        with self.assertRaises(ProviderError):
            self.judge(transport).evaluate(self.candidate, policy)
        self.assertEqual(len(transport.requests), 3)

    def test_a_client_error_is_not_retried(self):
        transport = Transport(error=urllib.error.HTTPError("u", 401, "x", {}, None))
        policy = support.policy(jev={"model": "jev-latest", "timeout_ms": 100, "retries": 2})
        with self.assertRaises(ProviderError):
            self.judge(transport).evaluate(self.candidate, policy)
        self.assertEqual(len(transport.requests), 1)

    # -- cost --

    def test_cost_stays_null_without_a_configured_price(self):
        """A missing price leaves the cost null rather than guessing a rate."""
        unpriced = support.policy(
            jev={"model": "jev-latest", "timeout_ms": 100, "retries": 0},
            pricing={"input_per_mtok_usd": None, "output_per_mtok_usd": None, "source": None},
        )
        judgment = self.judge(Transport(good_body())).evaluate(self.candidate, unpriced)
        self.assertIsNone(judgment.usage["cost_usd"])

    def test_cost_is_filled_in_from_the_configured_price(self):
        """shadow-v1 now carries a real rate, so a live judgment is costed."""
        judgment = self.judge(Transport(good_body())).evaluate(self.candidate, self.policy)
        expected = 850 * self.policy.pricing["input_per_mtok_usd"] / 1_000_000
        self.assertAlmostEqual(judgment.usage["cost_usd"], expected)

    def test_cost_is_computed_when_a_price_is_configured(self):
        usage = apply_pricing(
            {"input_tokens": 1_000_000, "output_tokens": 0, "cost_usd": None},
            {"input_per_mtok_usd": 2.0, "output_per_mtok_usd": 6.0},
        )
        self.assertAlmostEqual(usage["cost_usd"], 2.0)


if __name__ == "__main__":
    unittest.main()
