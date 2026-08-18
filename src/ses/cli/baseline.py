"""Standalone baseline CLI; the integration owner may register it in ``ses``."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ses.reporting.html_l1 import write_l1_html
from ses.runner import (
    BaselineRunner,
    BudgetLimits,
    DevelopCatalogEvaluator,
    develop_catalog_sha256,
    load_develop_catalog,
)

_EMPTY_SKILL_HASH = hashlib.sha256(b"").hexdigest()


def _decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("cost must be a decimal") from exc
    if not parsed.is_finite() or parsed < 0:
        raise argparse.ArgumentTypeError("cost must be finite and nonnegative")
    return parsed


def _new_run_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"run-baseline-{timestamp}-{uuid.uuid4().hex[:8]}"


def _safe_error(exc: Exception) -> str:
    message = str(exc)
    return type(exc).__name__ if "/" in message or "\\" in message else message


def build_parser() -> argparse.ArgumentParser:
    """Build the standalone parser without touching credentials or the network."""
    parser = argparse.ArgumentParser(
        prog="python -m ses.cli.baseline",
        description="Run the executable develop catalog through the offline L1 pipeline.",
    )
    parser.add_argument("--output-root", type=Path, default=Path(".ses/baselines"))
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--case", action="append", dest="cases")
    parser.add_argument("--iterations", type=int, default=1)
    parser.add_argument("--max-cases", type=int, default=1000)
    parser.add_argument("--max-turns", type=int, default=3)
    parser.add_argument("--max-input-tokens", type=int)
    parser.add_argument("--max-output-tokens", type=int)
    parser.add_argument("--max-cost", type=_decimal)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--rerun", action="append", default=[])
    parser.add_argument("--html", type=Path)
    parser.add_argument("--json", action="store_true", dest="as_json")
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--skill-hash", default=_EMPTY_SKILL_HASH)
    parser.add_argument("--protocol-version", default="ses-runner-v1")
    parser.add_argument(
        "--catalog-manifest",
        type=Path,
        help=(
            "Executable fixed/offline pending-review develop manifest; "
            "defaults to the Ticket 07 generated catalog."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the offline baseline and return 2 only for a clean budget stop."""
    args = build_parser().parse_args(argv)
    try:
        catalog = load_develop_catalog(args.catalog_manifest)
    except (OSError, TypeError, ValueError) as exc:
        print(f"baseline_error: {_safe_error(exc)}", file=sys.stderr)
        return 1
    case_ids = tuple(args.cases or catalog)
    unknown = sorted(set(case_ids) - set(catalog))
    if unknown:
        print(
            f"baseline_error: cases are not executable develop cases: {unknown}",
            file=sys.stderr,
        )
        return 1
    run_id = args.run_id or _new_run_id()
    try:
        budgets = BudgetLimits(
            max_cases=args.max_cases,
            max_turns_per_case=args.max_turns,
            max_input_tokens=args.max_input_tokens,
            max_output_tokens=args.max_output_tokens,
            max_cost=args.max_cost,
        )
        project_root = args.project_root.resolve()
        model_lock = project_root / "models.lock.json"
        data_version = develop_catalog_sha256(catalog)
        model_lock_hash = hashlib.sha256(model_lock.read_bytes()).hexdigest()
        completed = BaselineRunner(
            args.output_root, DevelopCatalogEvaluator(catalog)
        ).run(
            run_id=run_id,
            case_ids=case_ids,
            iterations=args.iterations,
            budgets=budgets,
            resume=args.resume,
            rerun_case_ids=tuple(args.rerun),
            data_version=data_version,
            model_lock_hash=model_lock_hash,
            skill_hash=args.skill_hash,
            protocol_version=args.protocol_version,
        )
        html_path = args.html or completed.run_dir / "l1.html"
        write_l1_html(completed.events_path, html_path)
    except (OSError, TypeError, ValueError) as exc:
        print(f"baseline_error: {_safe_error(exc)}", file=sys.stderr)
        return 1
    payload = {
        "run_id": completed.run_id,
        "metrics": completed.metrics,
        "stop_reason": completed.stop_reason,
        "events": str(completed.events_path),
        "html": str(html_path),
    }
    if args.as_json:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    else:
        print(f"run_id={completed.run_id}")
        print(f"events={completed.events_path}")
        print(f"html={html_path}")
        if completed.stop_reason:
            print(f"stop_reason={completed.stop_reason}")
    return 2 if completed.stop_reason else 0


if __name__ == "__main__":
    raise SystemExit(main())
