"""`guard` command line (chapter 10).

Exit codes keep semantic uncertainty and system failure apart:

    0   PAY                 2   invalid local input
    10  HOLD                4   Jev provider error
    20  REVIEW              5   ledger write error

The ledger-error code applies to CLI use only. On the payment path chapter 6
takes over and no exception leaves the guard.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterator, Sequence, TextIO

from .engine import ShadowEngine
from .errors import (
    EXIT_INPUT_ERROR,
    EXIT_LEDGER_ERROR,
    EXIT_PAY,
    EXIT_PROVIDER_ERROR,
    DECISION_EXIT_CODES,
    InputError,
    LedgerError,
    ProviderError,
)
from .judges import FixtureProcurementJudge, JevProcurementJudge
from .judges.base import ProcurementJudge
from .ledger import Ledger, verify_chain
from .models import SEMANTIC_OK
from .policy import Policy
from .report import FEEDBACK_LABELS, replay, report, score_distribution

DEFAULT_POLICY = Path(__file__).resolve().parents[2] / "policies" / "shadow-v1.json"
DEFAULT_LEDGER = "./var/spend-guard/ledger.jsonl"


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


def _judge(args: argparse.Namespace) -> ProcurementJudge:
    """Pick the judge. `--dry-run` never reaches the network."""
    if args.dry_run or args.judge == "fixture":
        if args.fixture:
            return FixtureProcurementJudge.from_file(args.fixture)
        raise InputError("--judge fixture (and --dry-run) require --fixture")
    return JevProcurementJudge()


def _engine(args: argparse.Namespace) -> ShadowEngine:
    return ShadowEngine(Policy.load(args.policy), _judge(args), Ledger(args.ledger))


def cmd_evaluate(args: argparse.Namespace, out: TextIO) -> int:
    engine = _engine(args)
    if args.jsonl:
        results: list[dict[str, Any]] = []
        worst = EXIT_PAY
        for raw in _load_jsonl(args.input):
            decision = engine.process(raw)
            if decision is None:
                results.append({"skipped": "already_evaluated"})
                continue
            results.append(decision.as_dict())
            worst = max(worst, DECISION_EXIT_CODES[decision.decision])
        for item in results:
            out.write(json.dumps(item, ensure_ascii=False) + "\n")
        return worst

    decision = engine.process(_load_json(args.input))
    if decision is None:
        _emit({"skipped": "already_evaluated"}, out)
        return EXIT_PAY
    _emit(decision.as_dict() if args.json else {"decision": decision.decision,
                                                "reason_codes": list(decision.reason_codes)}, out)
    # A provider failure is an operational problem even though the shadow
    # decision (REVIEW) is a perfectly ordinary outcome, so it gets its own code.
    if decision.semantic_judgments.status not in (SEMANTIC_OK, "SKIPPED"):
        return EXIT_PROVIDER_ERROR
    return DECISION_EXIT_CODES[decision.decision]


def cmd_replay(args: argparse.Namespace, out: TextIO) -> int:
    _emit(replay(Ledger(args.ledger), Policy.load(args.policy)), out)
    return EXIT_PAY


def cmd_report(args: argparse.Namespace, out: TextIO) -> int:
    ledger = Ledger(args.ledger)
    data = report(ledger)
    if args.distribution:
        data["score_distribution"] = score_distribution(ledger)
    if args.verify_chain:
        data["chain_problems"] = verify_chain(ledger.read())
    _emit(data, out)
    return EXIT_PAY


def cmd_feedback(args: argparse.Namespace, out: TextIO) -> int:
    ledger = Ledger(args.ledger)
    target = None
    for event in ledger.iter_events():
        if event.get("decision_id") == args.decision_id:
            target = event
    if target is None:
        raise InputError(f"no ledger event for decision {args.decision_id}")
    event = ledger.append(
        "human_feedback",
        decision_id=args.decision_id,
        candidate_id=str(target.get("candidate_id")),
        request_id=target.get("request_id"),
        payload={"label": args.label, "note": args.note, "source": "guard feedback"},
    )
    _emit({"event_id": event["event_id"], "decision_id": args.decision_id, "label": args.label}, out)
    return EXIT_PAY


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="guard", description="x402 Spend Guard (shadow mode)")
    parser.add_argument("--policy", default=str(DEFAULT_POLICY), help="policy JSON file")
    parser.add_argument("--ledger", default=DEFAULT_LEDGER, help="append-only ledger path")
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
    replay_cmd.set_defaults(handler=cmd_replay)

    report_cmd = sub.add_parser("report", help="shadow-mode metrics")
    report_cmd.add_argument("--distribution", action="store_true", help="include raw score spread")
    report_cmd.add_argument("--verify-chain", action="store_true")
    report_cmd.set_defaults(handler=cmd_report)

    feedback = sub.add_parser("feedback", help="append a human label to a decision")
    feedback.add_argument("--decision-id", required=True)
    feedback.add_argument("--label", required=True, choices=FEEDBACK_LABELS)
    feedback.add_argument("--note")
    feedback.set_defaults(handler=cmd_feedback)
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
