from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

import pytest

from ses.contracts import CapstoneMilestonePolicyCheck, artifact_json_bytes
from ses.release.capstone import (
    CAPSTONE_RELATIVE_ROOT,
    FIXED_CLEAN_ROOM_COMMANDS,
    TARGET_COMMAND_IDS,
    TARGET_COMMANDS,
    CapstoneReleaseReport,
    CheckStatus,
    ReleaseCheck,
    capstone_evidence_exit_code,
    materialize_worktree,
    run_capstone_clean_room,
    validate_capstone_course,
)

_ROOT = Path(__file__).resolve().parents[2]


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode()


def _checks(report: CapstoneReleaseReport) -> dict[str, ReleaseCheck]:
    return {check.check_id: check for check in report.checks}


def test_milestone_executor_blocks_starter_and_runs_reference_solution(
    tmp_path: Path,
) -> None:
    script = _ROOT / "scripts/execute_capstone_milestone.py"
    receipt = tmp_path / "test-milestone-policy-receipt.json"
    command = [
        sys.executable,
        str(script),
        "--root",
        str(_ROOT),
        "--milestone",
        "create",
        "--command-id",
        "doctor.fixed",
        "--policy-receipt",
        str(receipt),
        "--",
        sys.executable,
        "-c",
        "print('milestone-target-ran')",
    ]

    starter = subprocess.run(
        [*command[:6], "--variant", "starter", *command[6:]],
        capture_output=True,
        text=True,
        check=False,
    )
    solution = subprocess.run(
        [*command[:6], "--variant", "solution", *command[6:]],
        capture_output=True,
        text=True,
        check=False,
    )

    assert starter.returncode != 0
    assert "milestone-target-ran" not in starter.stdout
    assert solution.returncode == 0
    assert solution.stdout.strip() == "milestone-target-ran"
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    record = CapstoneMilestonePolicyCheck.model_validate(payload)
    assert receipt.read_bytes() == artifact_json_bytes(record)
    assert payload["record_type"] == "capstone_milestone_policy_check"
    assert record.milestone == "create"
    assert record.command_id == "doctor.fixed"
    assert payload["status"] == "passed"
    assert len(payload["fixture_sha256"]) == 64
    assert len(payload["implementation_sha256"]) == 64
    assert len(payload["policy_result_sha256"]) == 64
    receipt.unlink()


def test_milestone_executor_rejects_execute_once_without_policy_validation(
    tmp_path: Path,
) -> None:
    capstone = tmp_path / CAPSTONE_RELATIVE_ROOT
    capstone.parent.mkdir(parents=True)
    shutil.copytree(_ROOT / CAPSTONE_RELATIVE_ROOT, capstone)
    (capstone / "starter/create.py").write_text(
        "from collections.abc import Callable, Mapping\n"
        "def execute_target(command_id: str, probe: Mapping[str, object], "
        "validate_policy: Callable[[object], str], "
        "execute_once: Callable[[], int]) -> int:\n"
        "    return execute_once()\n",
        encoding="utf-8",
    )
    receipt = tmp_path / "policy-check.json"

    completed = subprocess.run(
        [
            sys.executable,
            str(_ROOT / "scripts/execute_capstone_milestone.py"),
            "--root",
            str(tmp_path),
            "--variant",
            "starter",
            "--milestone",
            "create",
            "--command-id",
            "doctor.fixed",
            "--policy-receipt",
            str(receipt),
            "--",
            sys.executable,
            "-c",
            "print('target-must-not-run')",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "target-must-not-run" not in completed.stdout
    assert not receipt.exists()


def test_capstone_is_an_independent_five_milestone_course() -> None:
    report = validate_capstone_course(_ROOT)
    checks = _checks(report)

    assert report.milestone_count == 5
    assert checks["capstone.identity"].status is CheckStatus.PASS
    assert checks["capstone.milestones"].status is CheckStatus.PASS
    assert checks["capstone.assets"].status is CheckStatus.PASS
    assert checks["capstone.live_fail_closed"].status is CheckStatus.PASS
    assert checks["capstone.target_commands"].status is CheckStatus.DEVIATION
    assert set(checks["capstone.target_commands"].details) == set(TARGET_COMMAND_IDS)
    assert report.status is CheckStatus.DEVIATION


def test_target_commands_match_the_complete_fixed_cli_contract() -> None:
    commands = {row.command_id: row.command for row in TARGET_COMMANDS}

    assert tuple(
        command_id for command_id in commands if ".inspect_" in command_id
    ) == (
        "eval.inspect_paired_trace",
        "evolve.inspect_failure_evidence",
        "evolve.inspect_failure_card",
        "gate.inspect_rejected_decision",
        "gate.inspect_registry_history",
    )
    assert commands["eval.inspect_paired_trace"] == (
        'uv run --offline --frozen ses inspect paired-trace "$PAIRED_TRACE" '
        '--profile "$PROFILE" --experiment-root "$ROOT"'
    )
    assert commands["gate.inspect_rejected_decision"] == (
        "uv run --offline --frozen ses inspect gate-decision "
        '"$REJECTED_GATE_DECISION" --profile "$PROFILE" '
        '--experiment-root "$ROOT"'
    )
    assert commands["gate.inspect_registry_history"] == (
        "uv run --offline --frozen ses inspect registry-history "
        '"$REGISTRY/events.jsonl" --profile "$PROFILE" '
        '--experiment-root "$ROOT"'
    )
    assert all(
        '--profile "$PROFILE" --experiment-root "$ROOT"' in commands[command_id]
        for command_id in (
            "automation.l3",
            "automation.portfolio",
            "automation.package",
            "automation.install",
            "automation.capstone_index",
        )
    )
    assert commands["automation.capstone_index"] == (
        'uv run --offline --frozen ses capstone-index --profile "$PROFILE" '
        '--experiment-root "$ROOT" --output "$ROOT/capstone-index.json"'
    )


def test_capstone_validator_rejects_lesson_eleven_wording(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    target = root / CAPSTONE_RELATIVE_ROOT
    target.parent.mkdir(parents=True)
    shutil.copytree(_ROOT / CAPSTONE_RELATIVE_ROOT, target)
    (target / "README.md").write_text("这是第 11 课。\n", encoding="utf-8")

    check = _checks(validate_capstone_course(root))["capstone.identity"]

    assert check.status is CheckStatus.FAIL
    assert "lesson_11_wording" in check.details


def test_capstone_validator_rejects_executable_live_shell_blocks(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repository"
    target = root / CAPSTONE_RELATIVE_ROOT
    target.parent.mkdir(parents=True)
    shutil.copytree(_ROOT / CAPSTONE_RELATIVE_ROOT, target)
    (target / "LIVE_SETUP.md").write_text(
        "```bash\nuv run ses doctor --profile profiles/live-v1.json --live\n```\n",
        encoding="utf-8",
    )

    check = _checks(validate_capstone_course(root))["capstone.live_fail_closed"]

    assert check.status is CheckStatus.FAIL
    assert any("LIVE_SETUP.md" in detail for detail in check.details)


def test_worktree_materialization_uses_current_bytes_and_excludes_local_state(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("current dirty bytes\n", encoding="utf-8")
    (source / "untracked.txt").write_text("development file\n", encoding="utf-8")
    (source / ".env").write_text("API_KEY=secret\n", encoding="utf-8")
    (source / ".git").mkdir()
    (source / ".git" / "index").write_bytes(b"git-state")
    workspace = tmp_path / "workspace"

    digest = materialize_worktree(source, workspace)

    assert len(digest) == 64
    assert (workspace / "README.md").read_text(encoding="utf-8") == (
        "current dirty bytes\n"
    )
    assert (workspace / "untracked.txt").is_file()
    assert not (workspace / ".env").exists()
    assert not (workspace / ".git").exists()


def test_git_worktree_materialization_excludes_ignored_files_but_keeps_untracked(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "--quiet", source], check=True)
    (source / ".gitignore").write_text("ignored-private.json\n", encoding="utf-8")
    (source / "tracked.txt").write_text("working bytes\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", source, "add", ".gitignore", "tracked.txt"], check=True
    )
    (source / "tracked.txt").write_text("dirty current bytes\n", encoding="utf-8")
    (source / "untracked.txt").write_text("phase 8 work\n", encoding="utf-8")
    (source / "ignored-private.json").write_text("private\n", encoding="utf-8")

    materialize_worktree(source, tmp_path / "workspace")

    assert (tmp_path / "workspace/tracked.txt").read_text(encoding="utf-8") == (
        "dirty current bytes\n"
    )
    assert (tmp_path / "workspace/untracked.txt").is_file()
    assert not (tmp_path / "workspace/ignored-private.json").exists()


class _RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], Mapping[str, str]]] = []

    def __call__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        capture_output: bool,
        text: bool,
        timeout: int,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        del capture_output, text, timeout, check
        self.calls.append((tuple(command), dict(env)))
        if (
            "scripts/execute_capstone_milestone.py" in command
            and "--policy-receipt" in command
        ):
            root = Path(command[command.index("--root") + 1])
            variant = command[command.index("--variant") + 1]
            milestone = command[command.index("--milestone") + 1]
            command_id = command[command.index("--command-id") + 1]
            receipt = Path(command[command.index("--policy-receipt") + 1])
            fixture_path = (
                root / CAPSTONE_RELATIVE_ROOT / "fixtures/milestone-policy-v1.json"
            )
            fixture_content = fixture_path.read_bytes()
            fixture = json.loads(fixture_content)
            result_content = _canonical_json_bytes(
                fixture["milestones"][milestone]["expected"]
            )
            implementation = root / CAPSTONE_RELATIVE_ROOT / variant / f"{milestone}.py"
            receipt.parent.mkdir(parents=True, exist_ok=True)
            receipt_record = CapstoneMilestonePolicyCheck.model_validate(
                {
                    "schema_version": "v1alpha1",
                    "record_type": "capstone_milestone_policy_check",
                    "milestone": milestone,
                    "command_id": command_id,
                    "implementation_variant": variant,
                    "implementation_path": implementation.relative_to(root).as_posix(),
                    "implementation_sha256": hashlib.sha256(
                        implementation.read_bytes()
                    ).hexdigest(),
                    "fixture_path": (
                        "fixtures/seed/capstone-shopping-assistant/fixtures/"
                        "milestone-policy-v1.json"
                    ),
                    "fixture_sha256": hashlib.sha256(fixture_content).hexdigest(),
                    "policy_result_sha256": hashlib.sha256(result_content).hexdigest(),
                    "status": "passed",
                    "target_exit_code": 0,
                }
            )
            receipt.write_bytes(artifact_json_bytes(receipt_record))
        if "capstone-index" in command and "--output" in command:
            output = Path(command[command.index("--output") + 1])
            if not output.is_absolute():
                output = cwd / output
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(
                json.dumps(
                    {
                        "record_type": "capstone_index",
                        "learning_completion": "workflow_complete",
                        "measurement_kind": "synthetic_offline",
                    }
                ),
                encoding="utf-8",
            )
        stdout = (
            "outcome=accepted\ncandidate_id=candidate-test\n"
            if tuple(command[:4]) == ("uv", "run", "--offline", "--frozen")
            and "gate" in command
            and "candidate" in command
            else "passed\n"
        )
        return subprocess.CompletedProcess(command, 0, stdout, "")


def test_capstone_clean_room_executes_the_fixed_cli_route_and_blocks_live(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    capstone = source / CAPSTONE_RELATIVE_ROOT
    capstone.parent.mkdir(parents=True)
    shutil.copytree(_ROOT / CAPSTONE_RELATIVE_ROOT, capstone)
    (source / "pyproject.toml").write_text(
        "[project]\nname='fixture'\n", encoding="utf-8"
    )
    (source / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (source / "untracked-phase8.txt").write_text("present\n", encoding="utf-8")
    runner = _RecordingRunner()

    evidence = run_capstone_clean_room(
        source,
        tmp_path / "workspace",
        environment={"PATH": "/usr/bin", "PROVIDER_API_KEY": "do-not-forward"},
        runner=runner,
        implementation_variant="solution",
    )

    executed = [" ".join(command) for command, _ in runner.calls]
    assert all(
        "live-v1" not in command and "--live" not in command for command in executed
    )
    assert all("PROVIDER_API_KEY" not in environment for _, environment in runner.calls)
    assert (tmp_path / "workspace" / "untracked-phase8.txt").is_file()
    assert evidence["source_materialization"] == "working_tree_regular_files"
    live_records = cast(list[dict[str, object]], evidence["live_commands"])
    assert {row["command_id"] for row in live_records} == {"live.full_workflow"}
    assert {row["status"] for row in live_records} == {"blocked"}
    target_records = cast(list[dict[str, object]], evidence["target_commands"])
    assert [row["command_id"] for row in target_records] == list(TARGET_COMMAND_IDS)
    assert {row["status"] for row in target_records} == {"passed"}
    assert evidence["implementation_variant"] == "solution"
    assert evidence["learning_completion"] == "workflow_complete"
    assert isinstance(evidence["capstone_index_sha256"], str)
    milestone_records = cast(
        list[dict[str, object]], evidence["milestone_implementations"]
    )
    assert [row["milestone"] for row in milestone_records] == [
        "create",
        "eval",
        "evolve",
        "gate",
        "automation",
    ]
    assert {row["status"] for row in milestone_records} == {"passed"}
    assert all(
        len(cast(str, row["implementation_sha256"])) == 64 for row in milestone_records
    )
    assert all(
        len(cast(str, row["policy_check_summary_sha256"])) == 64
        for row in milestone_records
    )
    hashes_by_milestone = {
        str(row["milestone"]): row["implementation_sha256"] for row in milestone_records
    }
    for row in target_records:
        milestone = row["milestone"]
        assert isinstance(milestone, str)
        assert row["implementation_sha256"] == hashes_by_milestone[milestone]
    assert all(
        len(cast(str, row["policy_check_sha256"])) == 64
        and len(cast(str, row["policy_result_sha256"])) == 64
        for row in target_records
    )
    target_calls = runner.calls[1 + len(FIXED_CLEAN_ROOM_COMMANDS) :]
    assert len(target_calls) == len(TARGET_COMMANDS)
    assert all(
        "$" not in argument for command, _ in target_calls for argument in command
    )
    assert any(
        "--candidate-id" in command and "candidate-test" in command
        for command, _ in target_calls
    )
    assert capstone_evidence_exit_code(evidence) == 0

    tampered = json.loads(json.dumps(evidence))
    tampered["milestone_implementations"][0]["implementation_sha256"] = "f" * 64
    assert capstone_evidence_exit_code(tampered) == 1
    evidence_path = tmp_path / "tampered-evidence.json"
    evidence_path.write_text(json.dumps(tampered), encoding="utf-8")
    clean_check = _checks(
        validate_capstone_course(source, command_evidence=evidence_path)
    )["capstone.clean_room_evidence"]
    assert clean_check.status is CheckStatus.FAIL
    assert clean_check.details == ("milestone_implementation_evidence_invalid",)

    tampered_policy = json.loads(json.dumps(evidence))
    tampered_policy["target_commands"][0]["policy_check_sha256"] = "e" * 64
    assert capstone_evidence_exit_code(tampered_policy) == 1


class _FailingTargetRunner(_RecordingRunner):
    def __call__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        capture_output: bool,
        text: bool,
        timeout: int,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        completed = super().__call__(
            command,
            cwd=cwd,
            env=env,
            capture_output=capture_output,
            text=text,
            timeout=timeout,
            check=check,
        )
        if "paired-comparison" in command:
            receipt = Path(command[command.index("--policy-receipt") + 1])
            record = CapstoneMilestonePolicyCheck.model_validate_json(
                receipt.read_bytes()
            )
            receipt.write_bytes(
                artifact_json_bytes(record.model_copy(update={"target_exit_code": 9}))
            )
            return subprocess.CompletedProcess(command, 9, "", "pair failed\n")
        return completed


class _StarterAwareRunner(_RecordingRunner):
    def __call__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        capture_output: bool,
        text: bool,
        timeout: int,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        if (
            "scripts/execute_capstone_milestone.py" in command
            and "--variant" in command
            and command[command.index("--variant") + 1] == "starter"
        ):
            self.calls.append((tuple(command), dict(env)))
            return subprocess.CompletedProcess(
                command,
                1,
                "",
                "NotImplementedError: learner milestone remains open\n",
            )
        return super().__call__(
            command,
            cwd=cwd,
            env=env,
            capture_output=capture_output,
            text=text,
            timeout=timeout,
            check=check,
        )


def test_capstone_clean_room_default_starter_cannot_claim_workflow_complete(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    capstone = source / CAPSTONE_RELATIVE_ROOT
    capstone.parent.mkdir(parents=True)
    shutil.copytree(_ROOT / CAPSTONE_RELATIVE_ROOT, capstone)
    (source / "pyproject.toml").write_text("[project]\nname='fixture'\n")
    (source / "uv.lock").write_text("version = 1\n")
    runner = _StarterAwareRunner()

    evidence = run_capstone_clean_room(
        source,
        tmp_path / "workspace",
        environment={"PATH": "/usr/bin"},
        runner=runner,
    )

    records = cast(list[dict[str, object]], evidence["target_commands"])
    milestones = cast(list[dict[str, object]], evidence["milestone_implementations"])
    assert evidence["implementation_variant"] == "starter"
    assert evidence["learning_completion"] == "incomplete"
    assert evidence["capstone_index_sha256"] is None
    assert records[0]["status"] == "failed"
    assert {row["status"] for row in records[1:]} == {"not_executed"}
    assert milestones[0]["status"] == "failed"
    assert {row["status"] for row in milestones[1:]} == {"not_executed"}
    assert capstone_evidence_exit_code(evidence) == 1

    evidence_path = tmp_path / "starter-evidence.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    clean_check = _checks(
        validate_capstone_course(source, command_evidence=evidence_path)
    )["capstone.clean_room_evidence"]
    assert clean_check.status is CheckStatus.FAIL
    assert clean_check.details == ("learner_workflow_incomplete",)


def test_capstone_clean_room_stops_target_route_after_the_first_failure(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    capstone = source / CAPSTONE_RELATIVE_ROOT
    capstone.parent.mkdir(parents=True)
    shutil.copytree(_ROOT / CAPSTONE_RELATIVE_ROOT, capstone)
    (source / "pyproject.toml").write_text("[project]\nname='fixture'\n")
    (source / "uv.lock").write_text("version = 1\n")
    runner = _FailingTargetRunner()

    evidence = run_capstone_clean_room(
        source,
        tmp_path / "workspace",
        environment={"PATH": "/usr/bin"},
        runner=runner,
        implementation_variant="solution",
    )

    records = cast(list[dict[str, object]], evidence["target_commands"])
    statuses = {str(row["command_id"]): row["status"] for row in records}
    assert statuses["eval.paired"] == "failed"
    assert statuses["eval.inspect_paired_trace"] == "not_executed"
    assert statuses["automation.install"] == "not_executed"
    assert capstone_evidence_exit_code(evidence) == 1


class _MissingCandidateRunner(_RecordingRunner):
    def __call__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        capture_output: bool,
        text: bool,
        timeout: int,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        completed = super().__call__(
            command,
            cwd=cwd,
            env=env,
            capture_output=capture_output,
            text=text,
            timeout=timeout,
            check=check,
        )
        if "gate" in command and "candidate" in command:
            return subprocess.CompletedProcess(command, 0, "outcome=accepted\n", "")
        return completed


def test_capstone_clean_room_requires_the_gate_candidate_id_before_promote(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    capstone = source / CAPSTONE_RELATIVE_ROOT
    capstone.parent.mkdir(parents=True)
    shutil.copytree(_ROOT / CAPSTONE_RELATIVE_ROOT, capstone)
    (source / "pyproject.toml").write_text("[project]\nname='fixture'\n")
    (source / "uv.lock").write_text("version = 1\n")
    runner = _MissingCandidateRunner()

    evidence = run_capstone_clean_room(
        source,
        tmp_path / "workspace",
        environment={"PATH": "/usr/bin"},
        runner=runner,
        implementation_variant="solution",
    )

    records = cast(list[dict[str, object]], evidence["target_commands"])
    by_id = {str(row["command_id"]): row for row in records}
    assert by_id["gate.candidate"]["status"] == "failed"
    assert by_id["gate.candidate"]["reason"] == "accepted_candidate_id_missing"
    assert by_id["gate.promote_accepted"]["status"] == "not_executed"
    assert not any(
        "registry" in command and "promote" in command for command, _ in runner.calls
    )
    assert capstone_evidence_exit_code(evidence) == 1


def test_capstone_evidence_fails_if_live_was_executed() -> None:
    evidence: dict[str, object] = {
        "locked_sync": {"status": "passed"},
        "fixed_commands": [{"status": "passed"}],
        "target_commands": [{"status": "passed"}],
        "live_commands": [{"status": "passed"}],
    }

    assert capstone_evidence_exit_code(evidence) == 1


def test_capstone_evidence_rejects_duplicate_target_command_records(
    tmp_path: Path,
) -> None:
    target_records = [
        {
            "command_id": row.command_id,
            "command_sha256": hashlib.sha256(row.command.encode()).hexdigest(),
            "status": "passed",
            "exit_code": 0,
        }
        for row in TARGET_COMMANDS
    ]
    evidence: dict[str, object] = {
        "schema_version": "v1alpha1",
        "record_type": "shopping_capstone_clean_room_evidence",
        "course_kind": "independent_capstone",
        "source_materialization": "working_tree_regular_files",
        "source_tree_sha256": "a" * 64,
        "credential_environment_names": [],
        "locked_sync": {
            "command": "uv sync --all-extras --locked --offline",
            "status": "passed",
        },
        "fixed_commands": [
            {
                "command_id": command_id,
                "command_sha256": hashlib.sha256(command.encode()).hexdigest(),
                "status": "passed",
                "exit_code": 0,
            }
            for command_id, command in FIXED_CLEAN_ROOM_COMMANDS
        ],
        "target_commands": [*target_records, target_records[0]],
        "live_commands": [
            {
                "command_id": "live.full_workflow",
                "command_sha256": None,
                "status": "blocked",
                "exit_code": None,
            }
        ],
    }
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    assert capstone_evidence_exit_code(evidence) == 1
    target_check = _checks(
        validate_capstone_course(_ROOT, command_evidence=evidence_path)
    )["capstone.target_commands"]
    assert target_check.status is CheckStatus.FAIL


def test_clean_room_rejects_manifest_command_drift_before_subprocess(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    capstone = source / CAPSTONE_RELATIVE_ROOT
    capstone.parent.mkdir(parents=True)
    shutil.copytree(_ROOT / CAPSTONE_RELATIVE_ROOT, capstone)
    manifest_path = capstone / "course-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["clean_room_commands"][0]["command"] = "python unexpected.py"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    runner = _RecordingRunner()

    with pytest.raises(ValueError, match="clean-room command contract"):
        run_capstone_clean_room(
            source,
            tmp_path / "workspace",
            environment={"PATH": "/usr/bin"},
            runner=runner,
        )

    assert runner.calls == []


def test_clean_room_rejects_target_command_drift_before_subprocess(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    capstone = source / CAPSTONE_RELATIVE_ROOT
    capstone.parent.mkdir(parents=True)
    shutil.copytree(_ROOT / CAPSTONE_RELATIVE_ROOT, capstone)
    manifest_path = capstone / "course-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["target_commands"][0]["command"] = "uv run unexpected"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    runner = _RecordingRunner()

    with pytest.raises(ValueError, match="target command contract"):
        run_capstone_clean_room(
            source,
            tmp_path / "workspace",
            environment={"PATH": "/usr/bin"},
            runner=runner,
        )

    assert runner.calls == []


def test_clean_room_rejects_milestone_execution_contract_drift_before_subprocess(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    capstone = source / CAPSTONE_RELATIVE_ROOT
    capstone.parent.mkdir(parents=True)
    shutil.copytree(_ROOT / CAPSTONE_RELATIVE_ROOT, capstone)
    manifest_path = capstone / "course-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["milestone_execution"]["policy_validation"] = "after_target"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    runner = _RecordingRunner()

    with pytest.raises(ValueError, match="milestone execution contract"):
        run_capstone_clean_room(
            source,
            tmp_path / "workspace",
            environment={"PATH": "/usr/bin"},
            runner=runner,
        )

    assert runner.calls == []


def test_structure_validator_accepts_a_completed_starter_only_in_learner_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "repository"
    capstone = root / CAPSTONE_RELATIVE_ROOT
    capstone.parent.mkdir(parents=True)
    shutil.copytree(_ROOT / CAPSTONE_RELATIVE_ROOT, capstone)
    for milestone in ("create", "eval", "evolve", "gate", "automation"):
        shutil.copy2(
            capstone / "solution" / f"{milestone}.py",
            capstone / "starter" / f"{milestone}.py",
        )

    normal = _checks(validate_capstone_course(root))["capstone.milestones"]
    monkeypatch.setenv("SES_CAPSTONE_IMPLEMENTATION_VARIANT", "starter")
    learner = _checks(validate_capstone_course(root))["capstone.milestones"]

    assert normal.status is CheckStatus.FAIL
    assert learner.status is CheckStatus.PASS

    monkeypatch.delenv("SES_CAPSTONE_IMPLEMENTATION_VARIANT")
    evidence = run_capstone_clean_room(
        root,
        tmp_path / "workspace",
        environment={"PATH": "/usr/bin"},
        runner=_RecordingRunner(),
    )
    evidence_path = tmp_path / "completed-starter-evidence.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    checks = _checks(validate_capstone_course(root, command_evidence=evidence_path))

    assert evidence["learning_completion"] == "workflow_complete"
    assert checks["capstone.milestones"].status is CheckStatus.PASS
    assert checks["capstone.clean_room_evidence"].status is CheckStatus.PASS


def test_validator_rejects_clean_room_evidence_after_worktree_changes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    capstone = source / CAPSTONE_RELATIVE_ROOT
    capstone.parent.mkdir(parents=True)
    shutil.copytree(_ROOT / CAPSTONE_RELATIVE_ROOT, capstone)
    runner = _RecordingRunner()
    evidence = run_capstone_clean_room(
        source,
        tmp_path / "workspace",
        environment={"PATH": "/usr/bin"},
        runner=runner,
        implementation_variant="solution",
    )
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    manifest = capstone / "course-manifest.json"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + "\n",
        encoding="utf-8",
    )

    clean_check = _checks(
        validate_capstone_course(source, command_evidence=evidence_path)
    )["capstone.clean_room_evidence"]

    assert clean_check.status is CheckStatus.FAIL
    assert clean_check.details == ("source_tree_sha256_mismatch",)


def test_course_manifest_is_canonical_json() -> None:
    path = _ROOT / CAPSTONE_RELATIVE_ROOT / "course-manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["course_kind"] == "independent_capstone"
    assert [row["id"] for row in payload["milestones"]] == [
        "create",
        "eval",
        "evolve",
        "gate",
        "automation",
    ]
