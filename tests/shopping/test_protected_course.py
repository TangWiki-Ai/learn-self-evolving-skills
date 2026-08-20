from __future__ import annotations

import hashlib
import json
import shutil
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from ses.automation.capstone import OpaqueSplitLockPaths, write_opaque_split_locks
from ses.automation.orchestrator import FinalProtocolLock
from ses.contracts import GatePolicy, MeasurementKind, PairedComparison, Trace
from ses.contracts.shopping import ShoppingScenario, ShopSimulatorEpisodeResult
from ses.shopping.course_workflow import (
    ShoppingCreateStageResult,
    run_shopping_create_stage,
)
from ses.shopping.fixed_course import fixed_public_source_groups
from ses.shopping.gate import (
    FixedShoppingEpisodeGateAdapter,
    shopping_gate_policy,
)
from ses.shopping.profile import (
    LoadedShoppingProfile,
    load_shopping_profile,
    shopping_experiment_id,
)
from ses.shopping.protected_course import FixedShoppingFinalAdapter
from ses.shopping.rollout import FixedShoppingRolloutAdapter
from ses.skills.installer import (
    load_skill_manifest,
    normalized_skill_sha256,
    write_skill_manifest,
)

ROOT = Path(__file__).resolve().parents[2]
CAPSTONE = ROOT / "course/capstone-shopping-assistant"
NOW = datetime(2026, 8, 20, tzinfo=UTC)


def _variant(source: Path, target: Path, addition: str) -> tuple[Path, str]:
    shutil.copytree(source, target)
    manifest = load_skill_manifest(target)
    skill = target / "SKILL.md"
    skill.write_text(
        skill.read_text(encoding="utf-8").rstrip() + f"\n\n{addition}\n",
        encoding="utf-8",
    )
    (target / "skill-manifest.json").unlink()
    write_skill_manifest(
        target,
        name=manifest.name,
        version=manifest.version,
        files=tuple(item.path for item in manifest.files),
        source_version=manifest.source_version,
        provider_compatibility=manifest.provider_compatibility,
        source_kind=manifest.source_kind,
        tool_protocol_sha256=manifest.tool_protocol_sha256,
    )
    return target, normalized_skill_sha256(target)


def _course(
    tmp_path: Path,
) -> tuple[
    LoadedShoppingProfile,
    Path,
    ShoppingCreateStageResult,
    Path,
    str,
    Path,
    str,
    Path,
    str,
    OpaqueSplitLockPaths,
    GatePolicy,
]:
    profile = load_shopping_profile(CAPSTONE / "profiles/fixed-v1.json")
    experiment = (tmp_path / "experiment").resolve()
    created = run_shopping_create_stage(
        profile=profile,
        projection_root=CAPSTONE / "fixtures/creator-projections",
        experiment_root=experiment,
    )
    manual, manual_sha = _variant(
        created.skill_source,
        tmp_path / "manual-skill",
        "拒绝、撤销或告别后禁止购买，告别不构成授权。",  # noqa: RUF001
    )
    auto, auto_sha = _variant(
        manual,
        tmp_path / "auto-skill",
        "Before any purchase, re-check current authorization and the exact offer.",
    )
    repeat, repeat_sha = _variant(
        auto,
        tmp_path / "repeat-skill",
        "Repeat the already active exact-offer check.",
    )
    locks = write_opaque_split_locks(
        experiment_root=experiment,
        experiment_id=shopping_experiment_id(profile),
        profile_sha256=profile.profile_sha256,
        mode="fixed",
        selection_case_count=8,
        selection_commitment_sha256=(
            profile.profile.protected_split_commitments["selection"]
        ),
        final_commitment_sha256=profile.profile.protected_split_commitments["final"],
        generated_at=NOW,
    )
    policy = shopping_gate_policy(
        profile,
        selection_lock=locks.selection,
        experiment_id=shopping_experiment_id(profile),
    )
    return (
        profile,
        experiment,
        created,
        manual,
        manual_sha,
        auto,
        auto_sha,
        repeat,
        repeat_sha,
        locks,
        policy,
    )


def test_protected_selection_runs_fresh_skill_driven_pairs(tmp_path: Path) -> None:
    (
        profile,
        experiment,
        created,
        manual,
        manual_sha,
        auto,
        auto_sha,
        repeat,
        repeat_sha,
        locks,
        policy,
    ) = _course(tmp_path)
    cases = (
        (
            "gate-shopping-manual",
            created.skill_source,
            created.receipt.skill_sha256,
            manual,
            manual_sha,
            (3, 4),
        ),
        (
            "gate-auto-r001",
            manual,
            manual_sha,
            auto,
            auto_sha,
            (4, 5),
        ),
        (
            "gate-auto-r002",
            auto,
            auto_sha,
            repeat,
            repeat_sha,
            (5, 5),
        ),
    )
    for gate_id, accepted, accepted_sha, candidate, candidate_sha, expected in cases:
        result = FixedShoppingEpisodeGateAdapter(
            profile=profile,
            experiment_root=experiment,
            selection_lock=locks.selection,
            accepted_skill_source=accepted,
            candidate_skill_source=candidate,
            scenario="accept" if gate_id != "gate-auto-r002" else "tie",
        ).run_selection(
            gate_id=gate_id,
            evaluation_nonce=f"nonce-{gate_id}",
            accepted_skill_sha256=accepted_sha,
            candidate_skill_sha256=candidate_sha,
            policy=policy,
            measured_at=NOW,
        )
        pair = result.pair
        assert pair.domain_evidence_kind == "episode_results"
        assert len(pair.accepted_episode_results) == 8
        assert len(pair.candidate_episode_results) == 8
        assert (
            sum(row.accepted_full_success is True for row in pair.cases),
            sum(row.candidate_full_success is True for row in pair.cases),
        ) == expected
        private_runs = experiment / "protected/selection-runs" / gate_id
        accepted_result_paths = tuple(
            private_runs.glob(
                "run-*-accepted-shopping/artifacts/*/iteration-0/attempt-0/"
                "episode-result.json"
            )
        )
        candidate_result_paths = tuple(
            private_runs.glob(
                "run-*-candidate-shopping/artifacts/*/iteration-0/attempt-0/"
                "episode-result.json"
            )
        )
        accepted_episodes = tuple(
            ShopSimulatorEpisodeResult.model_validate_json(path.read_bytes())
            for path in accepted_result_paths
        )
        candidate_episodes = tuple(
            ShopSimulatorEpisodeResult.model_validate_json(path.read_bytes())
            for path in candidate_result_paths
        )
        assert len(accepted_episodes) == len(candidate_episodes) == 8
        assert {row.episode_nonce for row in accepted_episodes}.isdisjoint(
            row.episode_nonce for row in candidate_episodes
        )
        mapping_rows = json.loads(
            (experiment / "protected/private/selection-mapping.json").read_bytes()
        )["rows"]
        opaque_slots = {row["case_slot"]: row["opaque_slot"] for row in mapping_rows}
        for side, episodes in (
            ("accepted", accepted_episodes),
            ("candidate", candidate_episodes),
        ):
            assert {row.episode_nonce for row in episodes} == {
                "episode-"
                + hashlib.sha256(
                    (
                        f"fixed-protected-selection-{side}-"
                        f"{opaque_slots[row.case_id]}:attempt-0:"
                        f"{int(row.case_id.rsplit('-', 1)[1])}"
                    ).encode()
                ).hexdigest()[:32]
                for row in episodes
            }
        accepted_trace_paths = tuple(
            private_runs.glob(
                "run-*-accepted-shopping/artifacts/*/iteration-0/attempt-0/"
                "trace-turn-0001.json"
            )
        )
        candidate_trace_paths = tuple(
            private_runs.glob(
                "run-*-candidate-shopping/artifacts/*/iteration-0/attempt-0/"
                "trace-turn-0001.json"
            )
        )
        accepted_traces = tuple(
            Trace.model_validate_json(path.read_bytes())
            for path in accepted_trace_paths
        )
        candidate_traces = tuple(
            Trace.model_validate_json(path.read_bytes())
            for path in candidate_trace_paths
        )
        assert len(accepted_traces) == len(candidate_traces) == 8
        assert {row.trace_id for row in accepted_traces}.isdisjoint(
            row.trace_id for row in candidate_traces
        )
        assert {row.session_id for row in accepted_traces}.isdisjoint(
            row.session_id for row in candidate_traces
        )
        assert None not in {row.session_id for row in accepted_traces}
        assert None not in {row.session_id for row in candidate_traces}

        accepted_workspaces = {
            path.resolve()
            for path in private_runs.glob(
                "run-*-accepted-shopping/artifacts/*/iteration-0/attempt-0/workspace"
            )
        }
        candidate_workspaces = {
            path.resolve()
            for path in private_runs.glob(
                "run-*-candidate-shopping/artifacts/*/iteration-0/attempt-0/workspace"
            )
        }
        assert len(accepted_workspaces) == len(candidate_workspaces) == 8
        assert accepted_workspaces.isdisjoint(candidate_workspaces)

    mapping = experiment / "protected/private/selection-mapping.json"
    assert mapping.stat().st_mode & 0o077 == 0
    assert b"fixed-selection-group" not in mapping.read_bytes()


def test_unauthorized_gate_and_final_safety_come_from_episode_receipts(
    tmp_path: Path,
) -> None:
    (
        profile,
        experiment,
        _created,
        manual,
        manual_sha,
        auto,
        auto_sha,
        _repeat,
        _repeat_sha,
        locks,
        policy,
    ) = _course(tmp_path)
    pair = (
        FixedShoppingEpisodeGateAdapter(
            profile=profile,
            experiment_root=experiment,
            selection_lock=locks.selection,
            accepted_skill_source=manual,
            candidate_skill_source=auto,
            scenario="unauthorized",
        )
        .run_selection(
            gate_id="gate-shopping-unauthorized",
            evaluation_nonce="nonce-shopping-unauthorized",
            accepted_skill_sha256=manual_sha,
            candidate_skill_sha256=auto_sha,
            policy=policy,
            measured_at=NOW,
        )
        .pair
    )
    assert sum(row.candidate_safety_violation_count or 0 for row in pair.cases) == 1
    assert tuple(
        experiment.glob(
            "protected/selection-runs/gate-shopping-unauthorized/"
            "run-*-candidate-shopping/artifacts/*/iteration-0/attempt-0/"
            "purchase-attempts/turn-0001.json"
        )
    )

    protocol = FinalProtocolLock(
        engine_id="engine-fixed",
        simulator_id="simulator-fixed",
        judge_id="judge-fixed",
        provider_id="provider-fixed",
        model_lock_sha256=policy.model_lock_sha256,
        evaluation_protocol_sha256=policy.evaluation_protocol_sha256,
        report_protocol_sha256="f" * 64,
    )
    final = FixedShoppingFinalAdapter(
        profile=profile,
        experiment_root=experiment,
        final_lock=locks.final,
        scenario="unauthorized",
    ).run(
        experiment_id=shopping_experiment_id(profile),
        subject_skill=auto,
        subject_skill_sha256=auto_sha,
        final_manifest=locks.final,
        executed_at=NOW,
        protocol=protocol,
    )
    assert len(final.case_passes) == 12
    assert sum(final.case_passes) == 10
    assert final.safety_violation_count == 1
    assert final.measurement_kind is MeasurementKind.SYNTHETIC_OFFLINE
    assert final.scenario_metrics is not None
    assert {row.scenario for row in final.scenario_metrics} == set(ShoppingScenario)
    assert sum(row.safety_violation_count for row in final.scenario_metrics) == 1
    episode_results = final.private_payload["episode_results"]
    assert isinstance(episode_results, list)
    assert len(episode_results) == 12
    assert tuple(
        experiment.glob(
            "final/protected-evaluation/run-shopping-final-current-fixed/"
            "artifacts/*/iteration-0/attempt-0/purchase-attempts/turn-0001.json"
        )
    )

    protected_group_counts: dict[str, int] = {}
    protected_group_tokens: dict[str, set[str]] = {}
    protected_slot_count = 0
    public_bytes = (CAPSTONE / "profiles/fixed-v1.json").read_bytes()
    for split, expected_groups, lock_path in (
        ("selection", 2, locks.selection),
        ("final", 3, locks.final),
    ):
        mapping = json.loads(
            (experiment / f"protected/private/{split}-mapping.json").read_bytes()
        )
        rows = mapping["rows"]
        protected_slot_count += len(rows)
        group_tokens = {str(row["source_group_token"]) for row in rows}
        protected_group_tokens[split] = group_tokens
        protected_group_counts[split] = len(group_tokens)
        assert len(group_tokens) == expected_groups
        assert all(
            len(token) == 38 and token.startswith("group-") for token in group_tokens
        )
        assert Counter(str(row["source_group_token"]) for row in rows) == {
            token: 4 for token in group_tokens
        }
        assert {
            (str(row["source_group_token"]), str(row["scenario"])) for row in rows
        } == {
            (token, scenario.value)
            for token in group_tokens
            for scenario in ShoppingScenario
        }
        for token in group_tokens:
            encoded = token.encode()
            assert encoded not in public_bytes
            assert encoded not in lock_path.read_bytes()

    assert protected_group_counts == {"selection": 2, "final": 3}
    assert protected_group_tokens["selection"].isdisjoint(
        protected_group_tokens["final"]
    )
    assert protected_slot_count == 20
    assert (
        len(fixed_public_source_groups()) + sum(protected_group_counts.values()) == 10
    )
    assert len(fixed_public_source_groups()) * 4 + protected_slot_count == 40


def test_auto_rollout_uses_current_parent_episode_evidence(tmp_path: Path) -> None:
    (
        profile,
        experiment,
        _created,
        manual,
        manual_sha,
        auto,
        auto_sha,
        _repeat,
        _repeat_sha,
        _locks,
        _policy,
    ) = _course(tmp_path)
    adapter = FixedShoppingRolloutAdapter(
        profile=profile,
        experiment_root=experiment,
    )

    first = adapter.run(
        experiment_id=shopping_experiment_id(profile),
        round_number=1,
        parent_skill=manual,
        parent_skill_sha256=manual_sha,
        executed_at=NOW,
    )
    second = adapter.run(
        experiment_id=shopping_experiment_id(profile),
        round_number=2,
        parent_skill=auto,
        parent_skill_sha256=auto_sha,
        executed_at=NOW,
    )

    assert first.source_kind == second.source_kind == "fresh_fixed_execution"
    first_subcodes = {row.shopping_subcode for row in first.evidence.cases}
    second_subcodes = {row.shopping_subcode for row in second.evidence.cases}
    assert None not in first_subcodes | second_subcodes
    assert {value.value for value in first_subcodes if value is not None} == {
        "unauthorized_purchase",
        "option_mismatch",
    }
    assert {value.value for value in second_subcodes if value is not None} == {
        "option_mismatch"
    }
    assert first.evidence.source.skill_sha256 == manual_sha
    assert second.evidence.source.skill_sha256 == auto_sha
    assert first.evidence.source.comparison_sha256 != (
        second.evidence.source.comparison_sha256
    )
    assert first.evidence.source.skill_events_sha256 != (
        second.evidence.source.skill_events_sha256
    )
    assert first.usage.input_tokens > 0
    assert second.usage.input_tokens > 0
    round_episode_nonces: dict[int, set[str]] = {}
    round_session_ids: dict[int, set[str]] = {}
    round_workspaces: dict[int, set[Path]] = {}
    evidence_by_round = {1: first.evidence, 2: second.evidence}
    parent_hashes = {1: manual_sha, 2: auto_sha}
    parent_sources = {1: manual, 2: auto}
    for round_number in (1, 2):
        root = (
            experiment / "rounds" / f"round-{round_number:03d}" / "rollout-evaluation"
        )
        baseline_paths = tuple(
            root.glob("run-*-baseline/artifacts/*/*/*/episode-result.json")
        )
        skill_paths = tuple(
            root.glob("run-*-skill/artifacts/*/*/*/episode-result.json")
        )
        assert len(baseline_paths) == len(skill_paths) == 12
        comparison_path = root / "paired-comparison.json"
        comparison_bytes = comparison_path.read_bytes()
        comparison = PairedComparison.model_validate_json(comparison_bytes)
        evidence = evidence_by_round[round_number]
        assert (
            evidence.source.comparison_sha256
            == hashlib.sha256(comparison_bytes).hexdigest()
        )
        assert evidence.source.pair_execution_sha256 == (
            comparison.pair_execution_sha256
        )
        assert (
            evidence.source.baseline_events_sha256
            == hashlib.sha256(
                (root / comparison.baseline_events.path).read_bytes()
            ).hexdigest()
        )
        assert (
            evidence.source.skill_events_sha256
            == hashlib.sha256(
                (root / comparison.skill_events.path).read_bytes()
            ).hexdigest()
        )
        baseline_results = tuple(
            ShopSimulatorEpisodeResult.model_validate_json(path.read_bytes())
            for path in baseline_paths
        )
        skill_results = tuple(
            ShopSimulatorEpisodeResult.model_validate_json(path.read_bytes())
            for path in skill_paths
        )
        baseline_nonces = {row.episode_nonce for row in baseline_results}
        skill_nonces = {row.episode_nonce for row in skill_results}
        assert baseline_nonces.isdisjoint(skill_nonces)
        round_episode_nonces[round_number] = baseline_nonces | skill_nonces

        trace_paths = tuple(root.glob("run-*/artifacts/*/*/*/trace-turn-0001.json"))
        assert len(trace_paths) == 24
        traces = tuple(
            Trace.model_validate_json(path.read_bytes()) for path in trace_paths
        )
        assert all("受保护提示" not in trace.request.prompt for trace in traces)
        session_ids = {trace.session_id for trace in traces}
        assert None not in session_ids
        assert len(session_ids) == 24
        round_session_ids[round_number] = {value for value in session_ids if value}

        manifest = load_skill_manifest(parent_sources[round_number])
        expected_inventory = {item.path for item in manifest.files}
        workspaces = tuple(root.glob("run-*-skill/artifacts/*/*/*/workspace"))
        assert len(workspaces) == 12
        round_workspaces[round_number] = set(workspaces)
        for workspace in workspaces:
            installed = workspace / ".claude" / "skills" / manifest.name
            assert {
                path.relative_to(installed).as_posix()
                for path in installed.rglob("*")
                if path.is_file()
            } == expected_inventory
            assert {
                item.path: hashlib.sha256(
                    (installed / item.path).read_bytes()
                ).hexdigest()
                for item in manifest.files
            } == {item.path: item.sha256 for item in manifest.files}
            assert {row.skill_sha256 for row in skill_results} == {
                parent_hashes[round_number]
            }

    assert round_episode_nonces[1].isdisjoint(round_episode_nonces[2])
    assert round_session_ids[1].isdisjoint(round_session_ids[2])
    assert round_workspaces[1].isdisjoint(round_workspaces[2])
