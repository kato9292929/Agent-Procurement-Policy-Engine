"""`guard` command line (chapters 10, D-6 and E-1).

Exit codes keep semantic uncertainty and system failure apart:

    0   PAY                 2   invalid local input
    10  HOLD                4   Jev provider error
    20  REVIEW              5   ledger read/write error

The ledger-error code applies to CLI use only. On the payment path chapter 6
takes over and no exception leaves the guard.

Every command works against either backend: `--backend jsonl` (default, from
the policy) or `--backend postgres`, which reads its connection string from
the environment variable named by `ledger.dsn_env`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterator, Sequence, TextIO

from .backends import build_backend, prepare_event, verify_chain
from .engine import ShadowEngine
from .errors import (
    DECISION_EXIT_CODES,
    EXIT_INPUT_ERROR,
    EXIT_LEDGER_ERROR,
    EXIT_PAY,
    EXIT_PROVIDER_ERROR,
    InputError,
    LedgerError,
    ProviderError,
)
from .judges import FixtureProcurementJudge, JevProcurementJudge
from .judges.base import ProcurementJudge
from .ledger import Ledger
from .models import SEMANTIC_OK
from .policy import Policy
from .report import FEEDBACK_LABELS, replay, report, sample, score_distribution

DEFAULT_POLICY = Path(__file__).resolve().parents[2] / "policies" / "shadow-v1.json"


def _emit(data: Any, stream: TextIO) -> None:
    stream.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _load_json(path: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise InputError(f"{path} is not valid JSON: {exc}") from exc


def _load_jsonl(path: str) -> Iterator[Any]:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise InputError(f"cannot read {path}: {exc}") from exc
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError as exc:
            raise InputError(f"{path}:{number} is not valid JSON: {exc}") from exc


def _policy(args: argparse.Namespace) -> Policy:
    """The policy to use, letting a subcommand's own --policy win.

    `guard replay --policy <other>` is how a candidate policy gets compared
    against what is recorded, so --policy has to be accepted after the
    subcommand as well as before it.
    """
    return Policy.load(getattr(args, "policy_override", None) or args.policy)


def _ledger(args: argparse.Namespace, policy: Policy | None = None) -> Ledger:
    """Open the ledger named by the policy, with CLI flags taking precedence."""
    policy = policy or _policy(args)
    config = dict(policy.ledger)
    if getattr(args, "backend", None):
        config["backend"] = args.backend
    path = getattr(args, "ledger", None) or config.get("path")
    return Ledger(backend=build_backend(config, path=path))


def _judge(args: argparse.Namespace) -> ProcurementJudge:
    """Pick the judge. `--dry-run` never reaches the network."""
    if args.dry_run or args.judge == "fixture":
        if args.fixture:
            return FixtureProcurementJudge.from_file(args.fixture)
        raise InputError("--judge fixture (and --dry-run) require --fixture")
    return JevProcurementJudge()


def cmd_evaluate(args: argparse.Namespace, out: TextIO) -> int:
    policy = _policy(args)
    engine = ShadowEngine(policy, _judge(args), _ledger(args, policy))
    if args.jsonl:
        worst = EXIT_PAY
        for raw in _load_jsonl(args.input):
            decision = engine.process(raw)
            if decision is None:
                out.write(json.dumps({"skipped": "already_evaluated"}, ensure_ascii=False) + "\n")
                continue
            out.write(json.dumps(decision.as_dict(), ensure_ascii=False) + "\n")
            worst = max(worst, DECISION_EXIT_CODES[decision.decision])
        return worst

    decision = engine.process(_load_json(args.input))
    if decision is None:
        _emit({"skipped": "already_evaluated"}, out)
        return EXIT_PAY
    _emit(
        decision.as_dict()
        if args.json
        else {"decision": decision.decision, "reason_codes": list(decision.reason_codes)},
        out,
    )
    # A provider failure is an operational problem even though the shadow
    # decision (REVIEW) is a perfectly ordinary outcome, so it gets its own code.
    if decision.semantic_judgments.status not in (SEMANTIC_OK, "SKIPPED"):
        return EXIT_PROVIDER_ERROR
    return DECISION_EXIT_CODES[decision.decision]


def cmd_replay(args: argparse.Namespace, out: TextIO) -> int:
    policy = _policy(args)
    _emit(replay(_ledger(args, policy), policy), out)
    return EXIT_PAY


def cmd_report(args: argparse.Namespace, out: TextIO) -> int:
    policy = _policy(args)
    ledger = _ledger(args, policy)
    data = report(ledger, policy)
    if args.distribution:
        data["score_distribution"] = score_distribution(ledger)
    if args.verify_chain:
        data["chain_problems"] = verify_chain(ledger.read())
    _emit(data, out)
    return EXIT_PAY


def cmd_feedback(args: argparse.Namespace, out: TextIO) -> int:
    ledger = _ledger(args)
    matches = ledger.find_by_decision_id(args.decision_id)
    if not matches:
        raise InputError(f"no ledger event for decision {args.decision_id}")
    target = matches[-1]
    event = ledger.append(
        "human_feedback",
        decision_id=args.decision_id,
        candidate_id=str(target.get("candidate_id")),
        request_id=target.get("request_id"),
        payload={"label": args.label, "note": args.note, "source": "guard feedback"},
    )
    _emit({"event_id": event["event_id"], "decision_id": args.decision_id, "label": args.label}, out)
    return EXIT_PAY


def cmd_sample(args: argparse.Namespace, out: TextIO) -> int:
    result = sample(
        _ledger(args), since=args.since, pay_rate=args.pay_rate, seed=args.seed
    )
    if args.json:
        _emit(result, out)
        return EXIT_PAY
    out.write(
        f"{result['selected']} to label "
        f"(PAY {result['selected_by_decision']['PAY']}, "
        f"HOLD {result['selected_by_decision']['HOLD']}, "
        f"REVIEW {result['selected_by_decision']['REVIEW']})\n\n"
    )
    for item in result["items"]:
        out.write(
            f"{item['decision_id']}  {item['decision']:6}  "
            f"{item['provider_id']}/{item['service_id']}/{item['route_id']}\n"
            f"    purpose: {item['task_purpose']}\n"
            f"    reasons: {', '.join(item['reason_codes'])}\n"
            f"    payment: {item['payment_status']}\n"
        )
    return EXIT_PAY


def cmd_verify(args: argparse.Namespace, out: TextIO) -> int:
    ledger = _ledger(args)
    events = ledger.read()
    problems = verify_chain(events)
    _emit(
        {
            "backend": args.backend or _policy(args).ledger.get("backend", "jsonl"),
            "events": len(events),
            "chain_intact": not problems,
            "problems": problems,
        },
        out,
    )
    return EXIT_PAY if not problems else EXIT_LEDGER_ERROR


def cmd_export(args: argparse.Namespace, out: TextIO) -> int:
    """Write the ledger out as JSONL, byte-identical in meaning to the source."""
    ledger = _ledger(args)
    destination = Path(args.out)
    destination.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with destination.open("w", encoding="utf-8") as handle:
        for event in ledger.iter_events():
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
            count += 1
    _emit({"exported": count, "out": str(destination)}, out)
    return EXIT_PAY


def cmd_import(args: argparse.Namespace, out: TextIO) -> int:
    """Load an existing JSONL ledger into the target backend.

    The source chain is verified before anything is written: importing a
    ledger that is already broken would bake the break into the new store,
    and the whole point of the chain is to notice that.
    """
    events = [event for event in _load_jsonl(args.source) if isinstance(event, dict)]
    problems = verify_chain(events)
    if problems and not args.allow_broken_chain:
        _emit(
            {
                "error": "source_chain_broken",
                "problems": problems[:10],
                "hint": "pass --allow-broken-chain to import anyway",
            },
            out,
        )
        return EXIT_LEDGER_ERROR

    ledger = _ledger(args)
    if ledger.latest_event_hash() is not None and not args.append:
        raise InputError("the target ledger is not empty; pass --append to add to it")

    imported = 0
    for event in events:
        # Re-sealed by the target backend so the imported run chains onto
        # whatever is already there rather than carrying stale links.
        ledger.append_prepared(
            prepare_event(
                event["event_type"],
                decision_id=event.get("decision_id"),
                candidate_id=event.get("candidate_id"),
                request_id=event.get("request_id"),
                payload=event.get("payload") or {},
                occurred_at=event.get("occurred_at"),
                event_id=event.get("event_id"),
            )
        )
        imported += 1
    _emit({"imported": imported, "source_chain_problems": problems}, out)
    return EXIT_PAY


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="guard", description="x402 Spend Guard (shadow mode)")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="policy JSON file")
    parser.add_argument("--ledger", help="ledger path (jsonl backend); defaults to the policy")
    parser.add_argument(
        "--backend", choices=("jsonl", "postgres"), help="override ledger.backend from the policy"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    evaluate = sub.add_parser("evaluate", help="evaluate one candidate or a JSONL stream")
    evaluate.add_argument("--input", required=True)
    evaluate.add_argument("--json", action="store_true", help="emit the full decision record")
    evaluate.add_argument("--jsonl", action="store_true", help="treat input as one candidate per line")
    evaluate.add_argument("--judge", choices=("jev", "fixture"), default="jev")
    evaluate.add_argument("--fixture", help="recorded answers for --judge fixture")
    evaluate.add_argument("--dry-run", action="store_true", help="never call Jev; implies fixture")
    evaluate.set_defaults(handler=cmd_evaluate)

    replay_cmd = sub.add_parser("replay", help="re-aggregate recorded judgments under a policy")
    replay_cmd.add_argument(
        "--policy", dest="policy_override",
        help="policy to re-decide under; defaults to the one the ledger was written with",
    )
    replay_cmd.set_defaults(handler=cmd_replay)

    report_cmd = sub.add_parser("report", help="shadow-mode metrics")
    report_cmd.add_argument("--distribution", action="store_true", help="include raw score spread")
    report_cmd.add_argument("--verify-chain", action="store_true")
    report_cmd.add_argument("--policy", dest="policy_override")
    report_cmd.set_defaults(handler=cmd_report)

    feedback = sub.add_parser("feedback", help="append a human label to a decision")
    feedback.add_argument("--decision-id", required=True)
    feedback.add_argument("--label", required=True, choices=FEEDBACK_LABELS)
    feedback.add_argument("--note")
    feedback.set_defaults(handler=cmd_feedback)

    sample_cmd = sub.add_parser("sample", help="pick the decisions a person should label")
    sample_cmd.add_argument("--since", help="ISO 8601 timestamp; ignore decisions older than this")
    sample_cmd.add_argument("--pay-rate", type=float, default=0.2, help="share of PAY to sample")
    sample_cmd.add_argument("--seed", type=int, help="make the selection reproducible")
    sample_cmd.add_argument("--json", action="store_true")
    sample_cmd.set_defaults(handler=cmd_sample)

    verify = sub.add_parser("verify", help="check the hash chain")
    verify.set_defaults(handler=cmd_verify)

    export = sub.add_parser("export", help="write the ledger out as JSONL")
    export.add_argument("--out", required=True)
    export.set_defaults(handler=cmd_export)

    import_cmd = sub.add_parser("import", help="load a JSONL ledger into the target backend")
    import_cmd.add_argument("--from", dest="source", required=True)
    import_cmd.add_argument("--append", action="store_true", help="allow a non-empty target")
    import_cmd.add_argument(
        "--allow-broken-chain", action="store_true", help="import even if the source fails to verify"
    )
    import_cmd.set_defaults(handler=cmd_import)
    return parser


def main(argv: Sequence[str] | None = None, out: TextIO | None = None, err: TextIO | None = None) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args, out)
    except InputError as exc:
        _emit({"error": "input_error", "message": str(exc)}, err)
        return EXIT_INPUT_ERROR
    except ProviderError as exc:
        # Provider detail stays out of the machine-readable output, as the
        # ledger rule in chapter 9 requires of anything provider-shaped.
        _emit({"error": "provider_error", "kind": exc.kind}, err)
        return EXIT_PROVIDER_ERROR
    except LedgerError as exc:
        _emit({"error": "ledger_error", "message": str(exc)}, err)
        return EXIT_LEDGER_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
