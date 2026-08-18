from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ses.contracts import (
    EvolutionPipelineSummary,
    FailureEvidenceFixture,
    artifact_json_bytes,
)
from ses.evolution.gate import (
    FixedGateAdapter,
    GateRequest,
    default_gate_policy,
    run_candidate_gate,
)
from ses.evolution.registry import RegistryError, SkillRegistry
from ses.evolution.updater import FakeUpdater
from ses.evolution.workflow import run_evolution_workflow

ROOT = Path(__file__).parents[2]
PARENT = ROOT / "course/ch07-create-v0/artifacts/skill/v0"
SEED_EVIDENCE = ROOT / "course/ch07-create-v0/artifacts/summary.json"
FAILURE_EVIDENCE = ROOT / "tests/fixtures/evolution/synthetic-failure-evidence.json"
SELECTION_LOCK = ROOT / "data/testset/protected/selection-manifest.json"
NOW = datetime(2026, 8, 18, 9, tzinfo=UTC)


def _initialized(tmp_path: Path) -> SkillRegistry:
    registry = SkillRegistry(tmp_path / "registry")
    registry.initialize(
        command_id="command-candidate-persistence-initialize",
        accepted_skill=PARENT,
        evidence_paths=(SEED_EVIDENCE,),
        occurred_at=NOW,
    )
    return registry


def _candidate(
    tmp_path: Path,
    *,
    evidence: Path = FAILURE_EVIDENCE,
) -> Path:
    bundle = tmp_path / "candidate"
    run_evolution_workflow(
        parent_dir=PARENT,
        evidence_path=evidence,
        output_root=bundle,
        updater=FakeUpdater(),
        mode="fixed",
    )
    return bundle


def _registered_snapshot(registry: SkillRegistry) -> Path:
    event = registry.audit().events[1]
    assert event.candidate is not None
    return registry.root / event.candidate.path


def test_registry_audit_uses_a_content_addressed_candidate_evidence_snapshot(
    tmp_path: Path,
) -> None:
    registry = _initialized(tmp_path)
    bundle = _candidate(tmp_path)
    registered = registry.register_candidate(
        command_id="command-register-persistent-candidate",
        candidate_bundle=bundle,
        occurred_at=NOW + timedelta(seconds=1),
    )

    snapshot_record = _registered_snapshot(registry)
    assert snapshot_record == (
        registry.root
        / "objects"
        / "candidates"
        / registered.version_sha256
        / "candidate.json"
    )
    assert {
        path.name for path in snapshot_record.parent.iterdir() if path.is_file()
    } == {
        "candidate.json",
        "failure-cards.json",
        "failure-evidence.json",
        "patch.json",
        "summary.json",
    }
    assert {Path(reference.path).name for reference in registered.evidence} == {
        "failure-cards.json",
        "failure-evidence.json",
        "patch.json",
        "summary.json",
    }

    shutil.rmtree(bundle)

    assert registry.audit().versions[registered.version_sha256].candidate is not None


def test_registration_event_commits_every_candidate_audit_sidecar(
    tmp_path: Path,
) -> None:
    registry = _initialized(tmp_path)
    bundle = _candidate(tmp_path)
    registered = registry.register_candidate(
        command_id="command-register-committed-sidecars",
        candidate_bundle=bundle,
        occurred_at=NOW + timedelta(seconds=1),
    )
    cards_ref = next(
        reference
        for reference in registered.evidence
        if reference.path.endswith("/failure-cards.json")
    )
    cards_path = registry.root / cards_ref.path
    cards_path.chmod(0o644)
    cards_path.write_bytes(cards_path.read_bytes() + b"\n")

    with pytest.raises(RegistryError, match="hash mismatch"):
        registry.audit()


@pytest.mark.parametrize("damage", ["missing", "tampered"])
def test_registry_audit_rejects_missing_or_tampered_candidate_sidecars(
    tmp_path: Path,
    damage: str,
) -> None:
    registry = _initialized(tmp_path)
    bundle = _candidate(tmp_path)
    registry.register_candidate(
        command_id=f"command-register-sidecar-{damage}",
        candidate_bundle=bundle,
        occurred_at=NOW + timedelta(seconds=1),
    )
    sidecar = _registered_snapshot(registry).parent / "failure-evidence.json"
    if damage == "missing":
        sidecar.unlink()
    else:
        sidecar.chmod(0o644)
        sidecar.write_bytes(sidecar.read_bytes() + b"\n")

    with pytest.raises(
        RegistryError,
        match=r"artifact|candidate|evidence|sidecar|snapshot",
    ):
        registry.audit()


def test_registry_rejects_a_candidate_bundle_with_a_symlinked_sidecar(
    tmp_path: Path,
) -> None:
    registry = _initialized(tmp_path)
    bundle = _candidate(tmp_path)
    evidence = bundle / "failure-evidence.json"
    captured = tmp_path / "captured-evidence.json"
    captured.write_bytes(evidence.read_bytes())
    evidence.unlink()
    evidence.symlink_to(captured)

    with pytest.raises(RegistryError, match=r"symlink|regular|bundle"):
        registry.register_candidate(
            command_id="command-register-symlinked-sidecar",
            candidate_bundle=bundle,
            occurred_at=NOW + timedelta(seconds=1),
        )


def test_registry_rejects_a_candidate_bundle_reached_through_a_symlink_ancestor(
    tmp_path: Path,
) -> None:
    registry = _initialized(tmp_path)
    real_parent = tmp_path / "real-parent"
    bundle = _candidate(real_parent)
    alias_parent = tmp_path / "alias-parent"
    alias_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(RegistryError, match="symlink ancestors"):
        registry.register_candidate(
            command_id="command-register-symlink-ancestor",
            candidate_bundle=alias_parent / bundle.name,
            occurred_at=NOW + timedelta(seconds=1),
        )


def test_registry_rejects_a_lexically_traversing_candidate_bundle_path(
    tmp_path: Path,
) -> None:
    registry = _initialized(tmp_path)
    real_parent = tmp_path / "real-parent"
    bundle = _candidate(real_parent)
    detour = tmp_path / "detour"
    detour.mkdir()

    with pytest.raises(RegistryError, match="canonical"):
        registry.register_candidate(
            command_id="command-register-lexical-traversal",
            candidate_bundle=detour / ".." / real_parent.name / bundle.name,
            occurred_at=NOW + timedelta(seconds=1),
        )


def test_registry_rejects_undeclared_candidate_bundle_files(tmp_path: Path) -> None:
    registry = _initialized(tmp_path)
    bundle = _candidate(tmp_path)
    (bundle / "undeclared-audit-material.json").write_text(
        '{"record_type":"shadow"}',
        encoding="utf-8",
    )

    with pytest.raises(RegistryError, match="undeclared"):
        registry.register_candidate(
            command_id="command-register-undeclared-sidecar",
            candidate_bundle=bundle,
            occurred_at=NOW + timedelta(seconds=1),
        )


def test_registry_rejects_candidate_sidecars_containing_an_environment_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "candidate-registry-secret-value-20260819"
    source = FailureEvidenceFixture.model_validate_json(FAILURE_EVIDENCE.read_bytes())
    first = source.cases[0].model_copy(
        update={"observation": f"The redacted observation leaked {secret}."}
    )
    forged = source.model_copy(update={"cases": (first, *source.cases[1:])})
    evidence = tmp_path / "secret-evidence.json"
    evidence.write_bytes(artifact_json_bytes(forged))
    bundle = _candidate(tmp_path / "secret", evidence=evidence)
    registry = _initialized(tmp_path / "secret-registry")
    monkeypatch.setenv("CANDIDATE_REGISTRY_SECRET", secret)

    with pytest.raises(RegistryError, match="credential"):
        registry.register_candidate(
            command_id="command-register-secret-sidecar",
            candidate_bundle=bundle,
            occurred_at=NOW + timedelta(seconds=1),
        )


def test_registry_rejects_a_preexisting_cross_candidate_hash_collision(
    tmp_path: Path,
) -> None:
    registry = _initialized(tmp_path)
    bundle = _candidate(tmp_path)
    candidate = json.loads((bundle / "candidate.json").read_text(encoding="utf-8"))
    collision = registry.root / "objects" / "candidates" / candidate["content_sha256"]
    collision.mkdir(parents=True)
    (collision / "candidate.json").write_text("{}", encoding="utf-8")

    with pytest.raises(RegistryError, match=r"collision|snapshot"):
        registry.register_candidate(
            command_id="command-register-candidate-collision",
            candidate_bundle=bundle,
            occurred_at=NOW + timedelta(seconds=1),
        )


def test_gate_snapshots_candidate_sidecars_before_the_source_bundle_disappears(
    tmp_path: Path,
) -> None:
    registry = _initialized(tmp_path)
    bundle = _candidate(tmp_path)
    registry.register_candidate(
        command_id="command-register-before-gate-snapshot",
        candidate_bundle=bundle,
        occurred_at=NOW + timedelta(seconds=1),
    )
    gate_id = "gate-candidate-evidence-snapshot"
    decision = run_candidate_gate(
        GateRequest(
            gate_id=gate_id,
            lineage_id=registry.audit().lineage_id,
            workspace_root=registry.root,
            accepted_skill=PARENT,
            candidate_bundle=bundle,
            selection_lock=SELECTION_LOCK,
            policy=default_gate_policy(ROOT, SELECTION_LOCK),
            mode="fixed",
            measured_at=NOW + timedelta(seconds=2),
        ),
        adapter=FixedGateAdapter(),
    )
    gate_root = registry.root / "gates" / gate_id
    assert (gate_root / "failure-evidence.json").is_file()
    assert (gate_root / "failure-cards.json").is_file()
    assert (gate_root / "patch.json").is_file()

    shutil.rmtree(bundle)
    registry.record_decision(
        command_id="command-record-after-source-disappeared",
        decision_path=gate_root / "gate-decision.json",
        occurred_at=NOW + timedelta(seconds=3),
    )

    assert registry.audit().events[-1].gate_decision is not None
    assert decision.candidate.path.endswith("/candidate.json")


def test_registry_rejects_a_gate_candidate_snapshot_that_differs_from_registration(
    tmp_path: Path,
) -> None:
    registry = _initialized(tmp_path)
    bundle = _candidate(tmp_path)
    registry.register_candidate(
        command_id="command-register-before-gate-sidecar-drift",
        candidate_bundle=bundle,
        occurred_at=NOW + timedelta(seconds=1),
    )
    gate_id = "gate-candidate-sidecar-drift"
    run_candidate_gate(
        GateRequest(
            gate_id=gate_id,
            lineage_id=registry.audit().lineage_id,
            workspace_root=registry.root,
            accepted_skill=PARENT,
            candidate_bundle=bundle,
            selection_lock=SELECTION_LOCK,
            policy=default_gate_policy(ROOT, SELECTION_LOCK),
            mode="fixed",
            measured_at=NOW + timedelta(seconds=2),
        ),
        adapter=FixedGateAdapter(),
    )
    gate_root = registry.root / "gates" / gate_id
    summary_path = gate_root / "summary.json"
    summary = EvolutionPipelineSummary.model_validate_json(summary_path.read_bytes())
    summary_path.write_bytes(
        artifact_json_bytes(summary.model_copy(update={"updater_latency_ms": 1}))
    )

    with pytest.raises(RegistryError, match="snapshot differs"):
        registry.record_decision(
            command_id="command-record-gate-sidecar-drift",
            decision_path=gate_root / "gate-decision.json",
            occurred_at=NOW + timedelta(seconds=3),
        )
