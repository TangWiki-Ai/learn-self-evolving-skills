#!/usr/bin/env python3
"""Run the locked model Judge over a pending creator seed review packet."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from ses.contracts import (  # noqa: E402
    ArtifactRef,
    ArtifactRoot,
    GradeStatus,
    StateDiff,
    Trace,
    artifact_json_bytes,
)
from ses.evaluation import aggregate_case_grade  # noqa: E402
from ses.evaluation.evidence_extractor import (  # noqa: E402
    EXTRACTOR_VERSION,
    evidence_json_bytes,
    extract_evidence,
)
from ses.evaluation.judges.llm import (  # noqa: E402
    RUBRIC_PROMPT_VERSION,
    BoundJudgeEngine,
    ModelDecision,
    Rubric,
    judge_llm,
)
from ses.foundation.config import (  # noqa: E402
    ModelRole,
    load_model_lock,
    load_runtime_config,
)
from ses.foundation.credentials import read_siliconflow_credentials  # noqa: E402
from ses.foundation.workspace import WorkspaceFactory  # noqa: E402

RUBRIC_ID = "creator-source-grounding-v2"
RUBRIC_VERSION = "2.0.0"


def _write(path: Path, value: object) -> bytes:
    payload = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return payload


def _ref(root: Path, path: Path, payload: bytes) -> dict[str, str]:
    return ArtifactRef(
        root=ArtifactRoot.RUN,
        path=path.relative_to(root).as_posix(),
        sha256=hashlib.sha256(payload).hexdigest(),
    ).model_dump(mode="json")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--project-root", type=Path, default=ROOT)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()
    root = args.packet.parent
    packet = json.loads(args.packet.read_text(encoding="utf-8"))
    runtime = load_runtime_config(args.project_root / "ses.json")
    lock = load_model_lock(args.project_root / runtime.models_lock)
    credentials = read_siliconflow_credentials(os.environ)
    for index, row in enumerate(packet["records"], 1):
        prior_judge = row.get("model_judge")
        if (
            isinstance(prior_judge, dict)
            and prior_judge.get("status") == "pass"
            and prior_judge.get("prompt_version") == RUBRIC_PROMPT_VERSION
            and prior_judge.get("extractor_version") == EXTRACTOR_VERSION
            and prior_judge.get("rubric_version") == RUBRIC_VERSION
        ):
            refs = tuple(
                ArtifactRef.model_validate(prior_judge[name])
                for name in ("evidence", "run", "grade")
            )
            for ref in refs:
                ref.verify_bytes((root / ref.path).read_bytes())
            continue
        trace_ref = ArtifactRef.model_validate(row["trace"])
        diff_ref = ArtifactRef.model_validate(row["state_diff"])
        trace = Trace.model_validate_json((root / trace_ref.path).read_text())
        diff = StateDiff.model_validate_json((root / diff_ref.path).read_text())
        evidence = extract_evidence(trace, diff)
        evidence_path = (
            root / f"private/judges/model/evidence/evidence-{index:03d}.json"
        )
        evidence_payload = evidence_json_bytes(evidence)
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        evidence_path.write_bytes(evidence_payload)
        evidence_ref = ArtifactRef.model_validate(
            _ref(root, evidence_path, evidence_payload)
        )
        rubric = Rubric(
            rubric_id=RUBRIC_ID,
            rubric_version=RUBRIC_VERSION,
            assertion_id=f"{row['seed_id']}-trajectory-quality",
            criterion=(
                "The trajectory grounds its actions in tool evidence, places a matching "
                "preview before every confirming mutation, and describes the terminal "
                "state without inventing facts. The initial user request may authorize "
                "the operation; do not require an intervening assistant message between "
                "preview and confirm. A later refund-method update supersedes an earlier "
                "return result, so evaluate the final observed state rather than treating "
                "that intermediate value as a contradiction. An insufficient amount "
                "reconciliation is not itself a failure when tool and state evidence "
                "directly establish the amounts."
            ),
        )
        run = asyncio.run(
            judge_llm(
                BoundJudgeEngine.production(
                    model=lock.roles[ModelRole.JUDGE],
                    credentials=credentials,
                    workspace_factory=WorkspaceFactory(
                        root / "private/judges/model/workspaces"
                    ),
                    executable=runtime.claude_executable,
                    environ=os.environ,
                    run_id="creator-seed-model-judge",
                    case_id=row["seed_id"],
                    iteration_id="0",
                    output_json_schema=ModelDecision.model_json_schema(),
                ),
                rubric=rubric,
                evidence=evidence,
                evidence_artifact=evidence_ref,
                timeout_seconds=args.timeout,
            )
        )
        grade = aggregate_case_grade(
            (run.assertion,),
            run_id="run-creator-seed-audit",
            case_id=row["seed_id"],
            iteration_id="iteration-0",
        )
        run_path = root / f"private/judges/model/judge-runs/run-{index:03d}.json"
        run_payload = _write(run_path, run.model_dump(mode="json"))
        grade_path = root / f"private/judges/model/grade-{index:03d}.json"
        grade_payload = artifact_json_bytes(grade)
        grade_path.parent.mkdir(parents=True, exist_ok=True)
        grade_path.write_bytes(grade_payload)
        row["model_judge"] = {
            "status": grade.status.value,
            "model_id": run.protocol.judge_model_id,
            "rubric_version": run.protocol.rubric_version,
            "prompt_version": run.protocol.prompt_version,
            "extractor_version": run.protocol.extractor_version,
            "response_source": run.protocol.response_source.value,
            "protocol_sha256": run.protocol.protocol_sha256,
            "evidence": _ref(root, evidence_path, evidence_payload),
            "run": _ref(root, run_path, run_payload),
            "grade": _ref(root, grade_path, grade_payload),
        }
        if grade.status is not GradeStatus.PASS:
            _write(args.packet, packet)
            raise RuntimeError(f"model Judge did not pass {row['seed_id']}")
    _write(args.packet, packet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
