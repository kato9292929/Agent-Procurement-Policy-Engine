"""E-4 (choosing what a person labels) and B-4 (losses the ledger can show)."""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone

import pytest
import support
from spend_guard.cli import main
from spend_guard.errors import EXIT_INPUT_ERROR, EXIT_LEDGER_ERROR, EXIT_PAY
from spend_guard.outcome import OutcomeRecorder
from spend_guard.report import orphaned_candidates, report, sample


@pytest.fixture
def stocked(tmp_path):
    """A ledger with every verdict present and every purchase completed."""
    engine = support.engine(tmp_path)
    decisions = {}
    for name in support.candidates():
        decision = engine.process(support.candidate(name))
        if decision:
            decisions[name] = decision
    recorder = OutcomeRecorder(engine.ledger)
    for decision in decisions.values():
        recorder.purchase_result(decision.request_id, payment_status="success", http_status=200)
    return engine, decisions


# -- E-1 / E-4: who gets labelled ------------------------------------------


def test_hold_and_review_are_taken_in_full(stocked):
    engine, decisions = stocked
    result = sample(engine.ledger, pay_rate=0.0, seed=1)
    taken = {item["decision"] for item in result["items"]}
    assert "PAY" not in taken, "pay_rate 0 must take no PAY"
    assert result["selected_by_decision"]["HOLD"] == 3
    assert result["selected_by_decision"]["REVIEW"] == 5


def test_pay_is_sampled_at_the_requested_rate(tmp_path):
    """Over many PAY decisions the share taken should land near --pay-rate."""
    engine = support.engine(tmp_path)
    recorder = OutcomeRecorder(engine.ledger)
    for index in range(400):
        raw = support.candidate("cand_pay_clean", candidate_id=f"c{index}", request_id=f"r{index}")
        decision = engine.process(raw)
        recorder.purchase_result(decision.request_id, payment_status="success")

    result = sample(engine.ledger, pay_rate=0.2, seed=7)
    share = result["selected_by_decision"]["PAY"] / 400
    assert 0.14 < share < 0.26, f"sampled {share:.3f}, expected about 0.2"


def test_the_same_seed_gives_the_same_selection(stocked):
    engine, _ = stocked
    first = sample(engine.ledger, pay_rate=0.5, seed=42)
    second = sample(engine.ledger, pay_rate=0.5, seed=42)
    assert [i["decision_id"] for i in first["items"]] == [i["decision_id"] for i in second["items"]]
    assert first["deterministic"] is True


def test_different_seeds_can_give_different_selections(tmp_path):
    engine = support.engine(tmp_path)
    recorder = OutcomeRecorder(engine.ledger)
    for index in range(200):
        raw = support.candidate("cand_pay_clean", candidate_id=f"c{index}", request_id=f"r{index}")
        decision = engine.process(raw)
        recorder.purchase_result(decision.request_id, payment_status="success")
    one = {i["decision_id"] for i in sample(engine.ledger, pay_rate=0.2, seed=1)["items"]}
    two = {i["decision_id"] for i in sample(engine.ledger, pay_rate=0.2, seed=2)["items"]}
    assert one != two


def test_selection_is_stable_as_the_ledger_grows(tmp_path):
    """Hashing rather than shuffling keeps earlier picks from moving."""
    engine = support.engine(tmp_path)
    recorder = OutcomeRecorder(engine.ledger)

    def add(start, stop):
        for index in range(start, stop):
            raw = support.candidate("cand_pay_clean", candidate_id=f"c{index}", request_id=f"r{index}")
            decision = engine.process(raw)
            recorder.purchase_result(decision.request_id, payment_status="success")

    add(0, 50)
    before = {i["decision_id"] for i in sample(engine.ledger, pay_rate=0.3, seed=5)["items"]}
    add(50, 100)
    after = {i["decision_id"] for i in sample(engine.ledger, pay_rate=0.3, seed=5)["items"]}
    assert before <= after, "an earlier pick was dropped when the ledger grew"


def test_already_labelled_decisions_are_excluded(stocked):
    engine, decisions = stocked
    target = decisions["cand_off_task"]
    engine.ledger.append(
        "human_feedback", decision_id=target.decision_id, candidate_id=target.candidate_id,
        request_id=target.request_id, payload={"label": "not_needed"})
    result = sample(engine.ledger, pay_rate=1.0, seed=1)
    assert target.decision_id not in {i["decision_id"] for i in result["items"]}
    assert result["skipped"]["already_labelled"] == 1


def test_decisions_without_a_purchase_result_are_excluded(tmp_path):
    """Nothing to judge until the purchase actually happened."""
    engine = support.engine(tmp_path)
    engine.process(support.candidate("cand_off_task"))
    result = sample(engine.ledger, pay_rate=1.0, seed=1)
    assert result["selected"] == 0
    assert result["skipped"]["no_purchase_result"] == 1


def test_since_filters_older_decisions(stocked):
    engine, _ = stocked
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    result = sample(engine.ledger, since=future, pay_rate=1.0, seed=1)
    assert result["selected"] == 0
    assert result["skipped"]["before_since"] > 0


def test_the_sheet_carries_what_a_person_needs_and_no_secrets(stocked):
    engine, _ = stocked
    item = sample(engine.ledger, pay_rate=1.0, seed=1)["items"][0]
    for field in ("decision_id", "decision", "reason_codes", "task_purpose",
                  "provider_id", "service_id", "payment_status", "label_command"):
        assert field in item
    blob = json.dumps(item)
    for absent in ("api_key", "authorization", "idempotency_key", "request_params_hash"):
        assert absent not in blob


def test_an_impossible_pay_rate_is_rejected(stocked):
    engine, _ = stocked
    with pytest.raises(Exception):
        sample(engine.ledger, pay_rate=1.5)


# -- E-2: progress towards the first threshold review ----------------------


def test_report_counts_labels_towards_the_review_target(stocked):
    engine, decisions = stocked
    target = decisions["cand_off_task"]
    engine.ledger.append(
        "human_feedback", decision_id=target.decision_id, candidate_id=target.candidate_id,
        request_id=target.request_id, payload={"label": "not_needed"})
    human = report(engine.ledger, support.policy())["human_comparison"]
    assert human["labelled"] == 1
    assert human["review_target_labels"] == 100
    assert human["labels_until_threshold_review"] == 99


def test_report_breaks_label_coverage_down_by_verdict(stocked):
    """HOLD and REVIEW are labelled in full, PAY only sampled: one rate would hide a gap."""
    engine, decisions = stocked
    target = decisions["cand_off_task"]  # a HOLD
    engine.ledger.append(
        "human_feedback", decision_id=target.decision_id, candidate_id=target.candidate_id,
        request_id=target.request_id, payload={"label": "not_needed"})
    coverage = report(engine.ledger, support.policy())["human_comparison"]["label_coverage_by_decision"]
    assert coverage["HOLD"] == {"evaluated": 3, "labelled": 1, "rate": 0.3333}
    assert coverage["PAY"]["labelled"] == 0


# -- B-4: losses the ledger can show -----------------------------------------


def test_a_judgment_that_never_arrived_is_orphaned(tmp_path):
    engine = support.engine(tmp_path)
    observed = engine.prepare(support.candidate("cand_pay_clean"))
    engine.record_observation(observed)  # observed, never judged

    fresh = orphaned_candidates(engine.ledger, orphan_after="PT15M")
    assert fresh["count"] == 0, "a candidate inside the window may still be queued"

    later = datetime.now(timezone.utc) + timedelta(hours=1)
    aged = orphaned_candidates(engine.ledger, orphan_after="PT15M", now=later)
    assert aged["count"] == 1
    assert aged["decision_ids"] == [observed["decision_id"]]


def test_a_judged_candidate_is_not_orphaned(tmp_path):
    engine = support.engine(tmp_path)
    engine.process(support.candidate("cand_pay_clean"))
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    assert orphaned_candidates(engine.ledger, now=later)["count"] == 0


def test_a_repeat_observation_is_not_orphaned(tmp_path):
    """A repeat points at an existing decision and is never judged again."""
    engine = support.engine(tmp_path)
    engine.process(support.candidate("cand_pay_clean"))
    engine.observe(support.candidate("cand_pay_clean"))
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    assert orphaned_candidates(engine.ledger, now=later)["count"] == 0


def test_report_says_where_the_uncountable_losses_live(stocked):
    engine, _ = stocked
    integrity = report(engine.ledger, support.policy())["integrity"]
    assert integrity["orphaned_candidates"] == 0
    # These cannot come from the ledger: an unwritten candidate leaves no trace.
    assert "host log" in integrity["dropped_candidates"]
    assert "host log" in integrity["unflushed_at_shutdown"]


# -- CLI surface -----------------------------------------------------------


def run_cli(*args, ledger):
    out, err = io.StringIO(), io.StringIO()
    code = main(["--policy", str(support.POLICY_PATH), "--ledger", str(ledger), *args],
                out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_guard_sample_json_output(stocked):
    engine, _ = stocked
    code, out, _ = run_cli("sample", "--pay-rate", "1.0", "--seed", "3", "--json",
                           ledger=engine.ledger.path)
    assert code == EXIT_PAY
    assert json.loads(out)["selected"] == 9


def test_guard_sample_human_output(stocked):
    engine, _ = stocked
    code, out, _ = run_cli("sample", "--seed", "3", ledger=engine.ledger.path)
    assert code == EXIT_PAY
    assert "to label" in out


def test_guard_sample_rejects_a_bad_since(stocked):
    engine, _ = stocked
    code, _, err = run_cli("sample", "--since", "not-a-date", ledger=engine.ledger.path)
    assert code == EXIT_INPUT_ERROR
    assert json.loads(err)["error"] == "input_error"


def test_guard_verify_reports_an_intact_chain(stocked):
    engine, _ = stocked
    code, out, _ = run_cli("verify", ledger=engine.ledger.path)
    assert code == EXIT_PAY
    assert json.loads(out)["chain_intact"] is True


def test_guard_verify_fails_on_a_tampered_ledger(stocked):
    engine, _ = stocked
    path = engine.ledger.path
    lines = path.read_text(encoding="utf-8").splitlines()
    event = json.loads(lines[-1])
    event["payload"]["payment_status"] = "failure"
    lines[-1] = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    code, out, _ = run_cli("verify", ledger=path)
    assert code == EXIT_LEDGER_ERROR
    assert json.loads(out)["chain_intact"] is False


def test_export_then_import_reproduces_the_chain(stocked, tmp_path):
    engine, _ = stocked
    exported = tmp_path / "exported.jsonl"
    code, _, _ = run_cli("export", "--out", str(exported), ledger=engine.ledger.path)
    assert code == EXIT_PAY

    target = tmp_path / "reimported.jsonl"
    code, out, _ = run_cli("import", "--from", str(exported), ledger=target)
    assert code == EXIT_PAY
    assert json.loads(out)["imported"] == len(engine.ledger.read())

    original = engine.ledger.read()
    restored = [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines()]
    assert [e["event_hash"] for e in restored] == [e["event_hash"] for e in original]


def test_import_refuses_a_broken_source(stocked, tmp_path):
    engine, _ = stocked
    exported = tmp_path / "exported.jsonl"
    run_cli("export", "--out", str(exported), ledger=engine.ledger.path)
    lines = exported.read_text(encoding="utf-8").splitlines()
    broken = json.loads(lines[2])
    broken["payload"]["tampered"] = True
    lines[2] = json.dumps(broken, ensure_ascii=False, separators=(",", ":"))
    exported.write_text("\n".join(lines) + "\n", encoding="utf-8")

    code, out, _ = run_cli("import", "--from", str(exported), ledger=tmp_path / "t.jsonl")
    assert code == EXIT_LEDGER_ERROR
    assert json.loads(out)["error"] == "source_chain_broken"


def test_import_refuses_a_non_empty_target_without_append(stocked, tmp_path):
    engine, _ = stocked
    exported = tmp_path / "exported.jsonl"
    run_cli("export", "--out", str(exported), ledger=engine.ledger.path)
    code, _, err = run_cli("import", "--from", str(exported), ledger=engine.ledger.path)
    assert code == EXIT_INPUT_ERROR
    assert "not empty" in json.loads(err)["message"]
