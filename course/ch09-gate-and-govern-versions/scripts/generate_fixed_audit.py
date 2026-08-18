"""Generate the Lesson 9 fixed/offline audit references without network access."""

from __future__ import annotations

import argparse
import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType

from ses.evolution.gate import FixedGateScenario
from ses.evolution.registry import SkillRegistry
from ses.evolution.updater import FakeUpdater
from ses.evolution.workflow import run_evolution_workflow

LESSON = Path(__file__).resolve().parents[1]
ROOT = LESSON.parents[1]
PARENT = ROOT / "course/ch07-create-v0/artifacts/skill/v0"
INITIAL_EVIDENCE = ROOT / "course/ch07-create-v0/artifacts/summary.json"
FAILURE_EVIDENCE = ROOT / "tests/fixtures/evolution/synthetic-failure-evidence.json"
SELECTION_LOCK = ROOT / "data/testset/protected/selection-manifest.json"
NOW = datetime(2026, 8, 18, 10, tzinfo=UTC)


def _solution() -> ModuleType:
    path = LESSON / "solution/governance.py"
    spec = importlib.util.spec_from_file_location("lesson_09_solution", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Lesson 9 solution cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _candidate(root: Path, name: str) -> Path:
    output = root / name
    run_evolution_workflow(
        parent_dir=PARENT,
        evidence_path=FAILURE_EVIDENCE,
        output_root=output,
        updater=FakeUpdater(),
        mode="fixed",
    )
    return output


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-root",
        type=Path,
        default=LESSON / "artifacts",
        help="Empty directory that will receive both fixed Registry bundles.",
    )
    return parser


def main() -> None:
    artifacts = _parser().parse_args().output_root.resolve(strict=False)
    acceptance = artifacts / "fixed-accept-promote-rollback"
    rejection = artifacts / "fixed-rejection"
    outputs = (
        acceptance,
        rejection,
        artifacts / "fixed-accept-promote-rollback.checkpoint.json",
        artifacts / "fixed-rejection.checkpoint.json",
    )
    if any(path.exists() for path in outputs):
        raise RuntimeError("fixed audit output already exists")

    solution = _solution()
    with TemporaryDirectory(prefix="ses-lesson-09-") as temporary:
        temporary_root = Path(temporary).resolve(strict=True)
        solution.govern(
            project_root=ROOT,
            governance_root=acceptance,
            accepted_skill=PARENT,
            initial_evidence=INITIAL_EVIDENCE,
            candidate_bundle=_candidate(temporary_root, "candidate-accept"),
            selection_lock=SELECTION_LOCK,
            scenario=FixedGateScenario.ACCEPT,
            gate_id="gate-reference-accept",
            occurred_at=NOW,
            rollback_after_promote=True,
        )
        solution.govern(
            project_root=ROOT,
            governance_root=rejection,
            accepted_skill=PARENT,
            initial_evidence=INITIAL_EVIDENCE,
            candidate_bundle=_candidate(temporary_root, "candidate-reject"),
            selection_lock=SELECTION_LOCK,
            scenario=FixedGateScenario.TIE,
            gate_id="gate-reference-reject",
            occurred_at=NOW,
        )

    for root in (acceptance, rejection):
        state = SkillRegistry(root).audit()
        decisions = tuple((root / "gates").glob("gate-*/gate-decision.json"))
        if len(decisions) != 1:
            raise RuntimeError("fixed audit must contain exactly one GateDecision")
        payload = decisions[0].read_text(encoding="utf-8")
        if (
            '"measurement_kind":"synthetic_offline"' not in payload
            or '"network_used":false' not in payload
        ):
            raise RuntimeError("fixed audit provenance is mislabeled")
        if not state.events:
            raise RuntimeError("fixed audit Registry is empty")


if __name__ == "__main__":
    main()
