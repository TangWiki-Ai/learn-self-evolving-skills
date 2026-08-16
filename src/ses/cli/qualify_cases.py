"""Offline CLI for Ticket 07 case verification and develop admission."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from ses.foundation.config import ModelRole, load_model_lock, load_runtime_config
from ses.foundation.credentials import read_siliconflow_credentials
from ses.testset.curation import LiveCurationModel
from ses.testset.verified import qualify_cases, reject_protected_split_write


def build_parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[3]
    ticket = root / "data" / "testset" / "ticket07"
    parser = argparse.ArgumentParser(
        prog="ses qualify-cases",
        description=(
            "Triage source signals, verify controlled cases, and build the develop "
            "catalog."
        ),
    )
    parser.add_argument(
        "--candidates", type=Path, default=ticket / "candidate-seeds.jsonl"
    )
    parser.add_argument("--variants", type=Path, default=ticket / "variant-plan.json")
    parser.add_argument("--reviews", type=Path, default=ticket / "human-reviews.jsonl")
    parser.add_argument(
        "--protected-manifest",
        type=Path,
        action="append",
        dest="protected",
        default=None,
    )
    parser.add_argument(
        "--judge-fixture",
        type=Path,
        default=root / "tests" / "fixtures" / "judges" / "calibration.json",
    )
    parser.add_argument(
        "--source-evidence",
        type=Path,
        default=root / "data" / "upstream" / "abcd" / "fixture" / "conversations.json",
    )
    parser.add_argument(
        "--curation-fixture",
        type=Path,
        default=ticket / "curation-responses.json",
    )
    parser.add_argument(
        "--curation-mode",
        choices=("fixed", "live"),
        default="fixed",
        help="Replay checked responses by default; live explicitly calls ClaudeCLI.",
    )
    parser.add_argument("--config", type=Path, default=root / "ses.json")
    parser.add_argument("--curation-timeout", type=float, default=120)
    parser.add_argument("--output", type=Path, default=ticket / "generated")
    parser.add_argument(
        "--split", choices=("develop", "selection", "final"), default="develop"
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = Path(__file__).resolve().parents[3]
    protected = args.protected or [
        root / "data" / "testset" / "protected" / "creator-manifest.json",
        root / "data" / "testset" / "protected" / "selection-manifest.json",
        root / "data" / "testset" / "protected" / "final-manifest.json",
    ]
    curation_model: LiveCurationModel | None = None
    try:
        reject_protected_split_write(args.split)
        if args.curation_mode == "live":
            config = load_runtime_config(args.config)
            lock = load_model_lock(root / config.models_lock)
            credentials = read_siliconflow_credentials(os.environ)
            curation_model = LiveCurationModel.production(
                triage_model=lock.roles[ModelRole.JUDGE],
                rubric_model=lock.roles[ModelRole.CREATOR],
                credentials=credentials,
                executable=config.claude_executable,
                environ=os.environ,
                timeout_seconds=args.curation_timeout,
            )
        summary = qualify_cases(
            candidate_path=args.candidates,
            variant_plan_path=args.variants,
            reviews_path=args.reviews,
            protected_manifests=protected,
            model_calibration_fixture=args.judge_fixture,
            output=args.output,
            split=args.split,
            source_evidence_path=args.source_evidence,
            curation_fixture_path=args.curation_fixture,
            curation_model=curation_model,
        )
    except (OSError, PermissionError, RuntimeError, TypeError, ValueError) as exc:
        # Avoid echoing arbitrary paths or private oracle values from nested errors.
        reason = str(exc)
        if "/" in reason or "\\" in reason:
            reason = type(exc).__name__
        print(f"qualify_cases_error:{reason}", file=sys.stderr)
        return 1
    finally:
        if curation_model is not None:
            curation_model.close()
    payload = {
        "candidate_count": summary.candidate_count,
        "source_candidate_count": summary.source_candidate_count,
        "selected_source_count": summary.selected_source_count,
        "qualified_count": summary.qualified_count,
        "rejected_count": summary.rejected_count,
        "pending_count": summary.pending_count,
        "data_version": summary.data_version,
        "output": str(summary.output),
        "curation_response_source": summary.response_source,
        "curation_input_tokens": summary.input_tokens,
        "curation_output_tokens": summary.output_tokens,
        "network_used": summary.network_used,
        "live_provider_used": summary.live_provider_used,
    }
    if args.as_json:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    else:
        for key, value in payload.items():
            print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
