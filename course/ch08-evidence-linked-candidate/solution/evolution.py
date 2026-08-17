"""Lesson 8 solution delegates to the production evolution protocol."""

from __future__ import annotations

from pathlib import Path

from ses.contracts import FailureCard, Patch
from ses.evolution.candidate import create_candidate as _create_candidate
from ses.evolution.diagnosis import analyze_failure_evidence
from ses.evolution.updater import FakeUpdater
from ses.evolution.workflow import run_evolution_workflow


def analyze(evidence: Path) -> object:
    return analyze_failure_evidence(evidence)


def create_candidate(
    *,
    parent: Path,
    patch: Patch,
    cards: tuple[FailureCard, ...],
    evidence: Path,
    output: Path,
) -> object:
    return _create_candidate(
        parent_dir=parent,
        patch=patch,
        cards=cards,
        evidence_path=evidence,
        output_dir=output,
    )


def evolve(*, parent: Path, evidence: Path, output: Path) -> object:
    return run_evolution_workflow(
        parent_dir=parent,
        evidence_path=evidence,
        output_root=output,
        updater=FakeUpdater(),
        mode="fixed",
    )
