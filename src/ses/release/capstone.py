"""Fail-closed release checks for the independent shopping capstone."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from string import Template
from typing import Literal, Protocol

from ses.contracts.capstone import CapstoneMilestonePolicyCheck
from ses.contracts.serialization import artifact_json_bytes
from ses.foundation.credentials import is_sensitive_name

CAPSTONE_RELATIVE_ROOT = Path("fixtures/seed/capstone-shopping-assistant")
MILESTONE_IDS = ("create", "eval", "evolve", "gate", "automation")
MilestoneImplementationVariant = Literal["starter", "solution"]
MILESTONE_POLICY_FIXTURE = Path(
    "fixtures/seed/capstone-shopping-assistant/fixtures/milestone-policy-v1.json"
)
MILESTONE_EXECUTION_CONTRACT = {
    "default_variant": "starter",
    "reference_variant": "solution",
    "entrypoint": "execute_target",
    "policy_fixture": "fixtures/milestone-policy-v1.json",
    "policy_validation": "before_target_exactly_once",
    "target_execution": "exactly_once",
}


@dataclass(frozen=True, slots=True)
class CapstoneTargetCommand:
    """One learner-visible command required by the capstone contract."""

    command_id: str
    milestone: str
    command: str


class CheckStatus(StrEnum):
    """Release gate status; deviations never silently become passes."""

    PASS = "pass"
    DEVIATION = "deviation"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class ReleaseCheck:
    """One stable release assertion with path-safe evidence."""

    check_id: str
    status: CheckStatus
    summary: str
    details: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "check_id": self.check_id,
            "status": self.status.value,
            "summary": self.summary,
            "details": list(self.details),
        }


class _Digest(Protocol):
    def update(self, value: bytes) -> None: ...


TARGET_COMMANDS = (
    CapstoneTargetCommand(
        "doctor.fixed",
        "create",
        'uv run --offline --frozen ses doctor --profile "$PROFILE"',
    ),
    CapstoneTargetCommand(
        "create.v0",
        "create",
        'uv run --offline --frozen ses skill create-v0 --profile "$PROFILE" '
        '--experiment-root "$ROOT"',
    ),
    CapstoneTargetCommand(
        "create.static",
        "create",
        'uv run --offline --frozen ses skill static-gate --profile "$PROFILE" '
        '--experiment-root "$ROOT"',
    ),
    CapstoneTargetCommand(
        "create.trigger",
        "create",
        'uv run --offline --frozen ses trigger-eval --profile "$PROFILE" '
        '--experiment-root "$ROOT"',
    ),
    CapstoneTargetCommand(
        "eval.paired",
        "eval",
        'uv run --offline --frozen ses paired-comparison --profile "$PROFILE" '
        '--experiment-root "$ROOT"',
    ),
    CapstoneTargetCommand(
        "eval.inspect_paired_trace",
        "eval",
        'uv run --offline --frozen ses inspect paired-trace "$PAIRED_TRACE" '
        '--profile "$PROFILE" --experiment-root "$ROOT"',
    ),
    CapstoneTargetCommand(
        "gate.registry_init",
        "gate",
        'uv run --offline --frozen ses registry init --profile "$PROFILE" '
        '--registry "$REGISTRY" '
        '--experiment-root "$ROOT" --initial-skill "$ROOT/skill/v0" '
        '--initial-evidence "$ROOT/v0-pipeline-summary.json"',
    ),
    CapstoneTargetCommand(
        "evolve.manual",
        "evolve",
        'uv run --offline --frozen ses evolve --profile "$PROFILE" '
        '--experiment-root "$ROOT"',
    ),
    CapstoneTargetCommand(
        "evolve.inspect_failure_evidence",
        "evolve",
        "uv run --offline --frozen ses inspect failure-evidence "
        '"$ROOT/failure-evidence.json" --profile "$PROFILE" '
        '--experiment-root "$ROOT"',
    ),
    CapstoneTargetCommand(
        "evolve.inspect_failure_card",
        "evolve",
        "uv run --offline --frozen ses inspect failure-card "
        '"$ROOT/manual-evolution/failure-cards.json" --profile "$PROFILE" '
        '--experiment-root "$ROOT"',
    ),
    CapstoneTargetCommand(
        "gate.registry_register",
        "gate",
        'uv run --offline --frozen ses registry register --profile "$PROFILE" '
        '--experiment-root "$ROOT" --registry "$REGISTRY" '
        '--candidate "$ROOT/manual-evolution"',
    ),
    CapstoneTargetCommand(
        "gate.candidate",
        "gate",
        'uv run --offline --frozen ses gate candidate --profile "$PROFILE" '
        '--experiment-root "$ROOT"',
    ),
    CapstoneTargetCommand(
        "gate.promote_accepted",
        "gate",
        'uv run --offline --frozen ses registry promote --profile "$PROFILE" '
        '--experiment-root "$ROOT" --registry "$REGISTRY" '
        '--candidate-id "$ACCEPTED_CANDIDATE_ID" '
        '--gate-decision "$MANUAL_GATE_DECISION"',
    ),
    CapstoneTargetCommand(
        "automation.auto_evolve",
        "automation",
        'uv run --offline --frozen ses auto-evolve --profile "$PROFILE" '
        '--experiment-root "$ROOT"',
    ),
    CapstoneTargetCommand(
        "gate.inspect_rejected_decision",
        "gate",
        "uv run --offline --frozen ses inspect gate-decision "
        '"$REJECTED_GATE_DECISION" --profile "$PROFILE" '
        '--experiment-root "$ROOT"',
    ),
    CapstoneTargetCommand(
        "gate.inspect_registry_history",
        "gate",
        "uv run --offline --frozen ses inspect registry-history "
        '"$REGISTRY/events.jsonl" --profile "$PROFILE" '
        '--experiment-root "$ROOT"',
    ),
    CapstoneTargetCommand(
        "automation.final",
        "automation",
        'uv run --offline --frozen ses final --profile "$PROFILE" '
        '--experiment-root "$ROOT"',
    ),
    CapstoneTargetCommand(
        "automation.l3",
        "automation",
        'uv run --offline --frozen ses l3-render --profile "$PROFILE" '
        '--experiment-root "$ROOT" --output "$ROOT/l3.html"',
    ),
    CapstoneTargetCommand(
        "automation.portfolio",
        "automation",
        'uv run --offline --frozen ses portfolio-export --profile "$PROFILE" '
        '--experiment-root "$ROOT" --output "$ROOT/portfolio"',
    ),
    CapstoneTargetCommand(
        "automation.package",
        "automation",
        'uv run --offline --frozen ses skill package --profile "$PROFILE" '
        '--experiment-root "$ROOT" --registry "$REGISTRY" --current-accepted '
        '--output "$ROOT/package"',
    ),
    CapstoneTargetCommand(
        "automation.capstone_index",
        "automation",
        'uv run --offline --frozen ses capstone-index --profile "$PROFILE" '
        '--experiment-root "$ROOT" --output "$ROOT/capstone-index.json"',
    ),
    CapstoneTargetCommand(
        "automation.install",
        "automation",
        "uv run --offline --frozen ses skill-install "
        '--accepted-package "$ROOT/package/release-manifest.json" '
        '--profile "$PROFILE" --experiment-root "$ROOT" '
        '--destination "$INSTALL_ROOT"',
    ),
)
TARGET_COMMAND_IDS = tuple(command.command_id for command in TARGET_COMMANDS)
FIXED_CLEAN_ROOM_COMMANDS = (
    (
        "fixed.course_tests",
        "uv run --offline --frozen pytest -q "
        "fixtures/seed/capstone-shopping-assistant/tests",
    ),
    (
        "fixed.structure_validator",
        "uv run --offline --frozen python scripts/validate_capstone.py --root . "
        "--structure-only --json",
    ),
)

_REQUIRED_ASSETS = (
    "course-manifest.json",
    "profiles/fixed-v1.json",
    "profiles/live-v1.json",
    "fixtures/milestone-policy-v1.json",
    "sources/shop-simulator-live-no-go.json",
)
_EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".ses",
        ".tox",
        ".uv-cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
    }
)
_EXCLUDED_PATH_PREFIXES = (
    Path("artifacts/cache"),
    Path("data/upstream/downloads"),
    Path("runs"),
)
_LIVE_COMMAND_PATTERN = re.compile(
    r"(?:profiles/live-v1\.json|--live(?:\s|$)|--mode\s+live)", re.IGNORECASE
)
_LESSON_ELEVEN_PATTERN = re.compile(
    r"(?:第\s*11\s*课|lesson\s*11|ch11(?:\b|-))", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class CapstoneReleaseReport:
    """Deterministic validation report kept separate from the ten-lesson report."""

    checks: tuple[ReleaseCheck, ...]
    milestone_count: int
    target_command_count: int

    @property
    def status(self) -> CheckStatus:
        statuses = {check.status for check in self.checks}
        if CheckStatus.FAIL in statuses:
            return CheckStatus.FAIL
        if CheckStatus.DEVIATION in statuses:
            return CheckStatus.DEVIATION
        return CheckStatus.PASS

    def as_dict(self) -> dict[str, object]:
        counts = {
            status.value: sum(check.status is status for check in self.checks)
            for status in CheckStatus
        }
        return {
            "schema_version": "v1alpha1",
            "record_type": "shopping_capstone_release_validation_report",
            "course_kind": "independent_capstone",
            "status": self.status.value,
            "milestone_count": self.milestone_count,
            "target_command_count": self.target_command_count,
            "check_counts": counts,
            "checks": [check.as_dict() for check in self.checks],
        }

    def json_bytes(self) -> bytes:
        return (
            json.dumps(
                self.as_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )


def _load_json(path: Path) -> Mapping[str, object] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, Mapping) else None


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )


def _safe_environment(source: Mapping[str, str]) -> dict[str, str]:
    allowed = {
        "LANG",
        "LC_ALL",
        "PATH",
        "SSL_CERT_FILE",
        "TMPDIR",
        "UV_CACHE_DIR",
    }
    environment = {
        name: value
        for name, value in source.items()
        if name in allowed and not is_sensitive_name(name)
    }
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def _output_digest(
    completed: subprocess.CompletedProcess[str],
) -> dict[str, str]:
    return {
        "stdout_sha256": hashlib.sha256(completed.stdout.encode()).hexdigest(),
        "stderr_sha256": hashlib.sha256(completed.stderr.encode()).hexdigest(),
    }


def _policy_fixture(root: Path) -> tuple[Mapping[str, object], str] | None:
    path = root / MILESTONE_POLICY_FIXTURE
    try:
        content = path.read_bytes()
        value = json.loads(content)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if (
        not isinstance(value, Mapping)
        or value.get("schema_version") != "v1alpha1"
        or value.get("record_type") != "shopping_capstone_milestone_policy_fixture"
    ):
        return None
    milestones = value.get("milestones")
    if not isinstance(milestones, Mapping) or tuple(milestones) != MILESTONE_IDS:
        return None
    if any(
        not isinstance(milestones.get(milestone), Mapping)
        or not isinstance(milestones[milestone].get("probe"), Mapping)
        or not isinstance(milestones[milestone].get("expected"), Mapping)
        for milestone in MILESTONE_IDS
    ):
        return None
    return value, hashlib.sha256(content).hexdigest()


def _manifest(root: Path) -> Mapping[str, object] | None:
    return _load_json(root / CAPSTONE_RELATIVE_ROOT / "course-manifest.json")


def _check_identity(
    capstone: Path, manifest: Mapping[str, object] | None
) -> ReleaseCheck:
    details: list[str] = []
    if manifest is None:
        details.append("invalid_course_manifest")
    else:
        if manifest.get("record_type") != "shopping_capstone_course_manifest":
            details.append("invalid_manifest_identity")
        if manifest.get("course_id") != "capstone-shopping-assistant":
            details.append("invalid_capstone_id")
        if manifest.get("course_kind") != "independent_capstone":
            details.append("course_kind_must_be_independent_capstone")
    try:
        readme = (capstone / "README.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        readme = ""
    if _LESSON_ELEVEN_PATTERN.search(readme):
        details.append("lesson_11_wording")
    if details:
        return ReleaseCheck(
            "capstone.identity",
            CheckStatus.FAIL,
            "The shopping project must remain an independent capstone.",
            tuple(details),
        )
    return ReleaseCheck(
        "capstone.identity",
        CheckStatus.PASS,
        "The fixed shopping assets retain their independent capstone identity.",
    )


def _check_milestones(
    capstone: Path,
    manifest: Mapping[str, object] | None,
    *,
    implementation_variant: str | None,
) -> ReleaseCheck:
    failures: list[str] = []
    rows = manifest.get("milestones") if manifest is not None else None
    if not isinstance(rows, list):
        rows = []
        failures.append("milestones_missing")
    ids = tuple(row.get("id") for row in rows if isinstance(row, Mapping))
    if ids != MILESTONE_IDS:
        failures.append("milestone_order_must_be_create_eval_evolve_gate_automation")
    clean_rows = manifest.get("clean_room_commands") if manifest is not None else None
    actual_clean_commands = (
        tuple(
            (row.get("id"), row.get("command"))
            for row in clean_rows
            if isinstance(row, Mapping)
        )
        if isinstance(clean_rows, list)
        else ()
    )
    if actual_clean_commands != FIXED_CLEAN_ROOM_COMMANDS:
        failures.append("fixed_clean_room_command_contract_drift")
    execution = manifest.get("milestone_execution") if manifest is not None else None
    if execution != MILESTONE_EXECUTION_CONTRACT:
        failures.append("milestone_execution_contract_drift")
    if _policy_fixture(capstone.parents[2]) is None:
        failures.append("milestone_policy_fixture_invalid")
    for milestone in MILESTONE_IDS:
        for variant in ("starter", "solution"):
            relative = f"{variant}/{milestone}.py"
            path = capstone / relative
            if not path.is_file() or path.is_symlink():
                failures.append(f"missing:{relative}")
        test_path = capstone / "tests/test_milestones.py"
        if not test_path.is_file() or test_path.is_symlink():
            failures.append("missing:tests/test_milestones.py")
    for milestone in MILESTONE_IDS:
        starter = capstone / "starter" / f"{milestone}.py"
        solution = capstone / "solution" / f"{milestone}.py"
        try:
            starter_text = starter.read_text(encoding="utf-8")
            if implementation_variant == "starter":
                if "NotImplementedError" in starter_text:
                    failures.append(f"starter_still_open:{milestone}")
            elif "NotImplementedError" not in starter_text:
                failures.append(f"starter_not_open:{milestone}")
            solution_text = solution.read_text(encoding="utf-8")
            if (
                "from ses." not in solution_text
                or "NotImplementedError" in solution_text
            ):
                failures.append(f"solution_not_delegated:{milestone}")
        except (OSError, UnicodeError):
            continue
    if failures:
        return ReleaseCheck(
            "capstone.milestones",
            CheckStatus.FAIL,
            "The five milestone starter/solution contract is incomplete.",
            tuple(sorted(set(failures))),
        )
    return ReleaseCheck(
        "capstone.milestones",
        CheckStatus.PASS,
        "All five milestone policy and execution seams match the course contract.",
    )


def _check_assets(capstone: Path) -> ReleaseCheck:
    failures = [name for name in _REQUIRED_ASSETS if not (capstone / name).is_file()]
    if failures:
        return ReleaseCheck(
            "capstone.assets",
            CheckStatus.FAIL,
            "Required fixed shopping assets are missing.",
            tuple(failures),
        )
    return ReleaseCheck(
        "capstone.assets",
        CheckStatus.PASS,
        "The fixed profiles, policy fixture, source decision, and manifest are present.",
    )


def _shell_blocks(path: Path) -> tuple[tuple[int, str], ...]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ()
    blocks: list[tuple[int, str]] = []
    start: int | None = None
    lines: list[str] = []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip().casefold()
        if start is None:
            if stripped in {"```bash", "```sh", "```shell"}:
                start = number + 1
                lines = []
        elif stripped == "```":
            blocks.append((start, "\n".join(lines)))
            start = None
            lines = []
        else:
            lines.append(line)
    return tuple(blocks)


def _check_live_fail_closed(
    capstone: Path,
    manifest: Mapping[str, object] | None,
    evidence: Mapping[str, object] | None,
) -> ReleaseCheck:
    failures: list[str] = []
    source = _load_json(capstone / "sources/shop-simulator-live-no-go.json")
    if source is None or source.get("decision") != "no_go":
        failures.append("phase0_source_decision_must_be_no_go")
    assets = source.get("assets") if source is not None else None
    if not isinstance(assets, list) or not assets:
        failures.append("phase0_asset_decisions_missing")
    elif not any(
        isinstance(row, Mapping) and row.get("status") in {"unknown", "prohibited"}
        for row in assets
    ):
        failures.append("no_go_requires_unknown_or_prohibited_asset")
    if manifest is None or manifest.get("live_execution_policy") != (
        "blocked_phase0_no_go"
    ):
        failures.append("manifest_live_policy_not_blocked")
    for path in sorted(capstone.rglob("*.md")):
        for line, script in _shell_blocks(path):
            if _LIVE_COMMAND_PATTERN.search(script):
                failures.append(
                    f"executable_live_command:{path.relative_to(capstone).as_posix()}:{line}"
                )
    if evidence is not None:
        records = evidence.get("live_commands")
        if not _command_records_match(
            records,
            (("live.full_workflow", None),),
            allowed_statuses=frozenset({"blocked"}),
        ):
            failures.append("live_command_was_not_fail_closed")
    if failures:
        return ReleaseCheck(
            "capstone.live_fail_closed",
            CheckStatus.FAIL,
            "Phase 0 is no-go, so no live command may execute.",
            tuple(failures),
        )
    return ReleaseCheck(
        "capstone.live_fail_closed",
        CheckStatus.PASS,
        "Phase 0 no-go is explicit and every live route remains blocked.",
    )


def _normalized(value: str) -> str:
    return " ".join(value.replace("\\\n", " ").split())


def _expected_command_sha256(command: str | None) -> str | None:
    return hashlib.sha256(command.encode()).hexdigest() if command is not None else None


def _command_records_match(
    value: object,
    expected: Sequence[tuple[str, str | None]],
    *,
    allowed_statuses: frozenset[str],
) -> bool:
    if not isinstance(value, list) or len(value) != len(expected):
        return False
    for row, (command_id, command) in zip(value, expected, strict=True):
        if not isinstance(row, Mapping):
            return False
        if row.get("command_id") != command_id or row.get(
            "command_sha256"
        ) != _expected_command_sha256(command):
            return False
        status_value = row.get("status")
        if status_value not in allowed_statuses:
            return False
        exit_code = row.get("exit_code")
        if status_value == "passed" and exit_code != 0:
            return False
        if status_value in {"blocked", "not_executed"} and exit_code is not None:
            return False
        if status_value == "failed" and (type(exit_code) is not int or exit_code == 0):
            return False
    return True


def _milestone_implementation_evidence_valid(
    evidence: Mapping[str, object],
    *,
    source_root: Path | None = None,
) -> bool:
    variant = evidence.get("implementation_variant")
    if variant not in {"starter", "solution"}:
        return False
    milestone_rows = evidence.get("milestone_implementations")
    target_rows = evidence.get("target_commands")
    if (
        not isinstance(milestone_rows, list)
        or len(milestone_rows) != len(MILESTONE_IDS)
        or not isinstance(target_rows, list)
        or len(target_rows) != len(TARGET_COMMANDS)
    ):
        return False
    target_by_id = {
        row.get("command_id"): row
        for row in target_rows
        if isinstance(row, Mapping) and isinstance(row.get("command_id"), str)
    }
    if len(target_by_id) != len(TARGET_COMMANDS):
        return False
    for milestone, row in zip(MILESTONE_IDS, milestone_rows, strict=True):
        if not isinstance(row, Mapping) or row.get("milestone") != milestone:
            return False
        relative = (
            CAPSTONE_RELATIVE_ROOT / str(variant) / f"{milestone}.py"
        ).as_posix()
        implementation_sha256 = row.get("implementation_sha256")
        fixture_sha256 = row.get("policy_fixture_sha256")
        expected_result_sha256 = row.get("policy_expected_result_sha256")
        if (
            row.get("implementation_variant") != variant
            or row.get("implementation_path") != relative
            or not isinstance(implementation_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", implementation_sha256) is None
            or row.get("policy_fixture_path") != MILESTONE_POLICY_FIXTURE.as_posix()
            or not isinstance(fixture_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", fixture_sha256) is None
            or not isinstance(expected_result_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_result_sha256) is None
        ):
            return False
        if source_root is not None:
            path = source_root / relative
            if (
                not path.is_file()
                or path.is_symlink()
                or hashlib.sha256(path.read_bytes()).hexdigest()
                != implementation_sha256
            ):
                return False
            fixture = _policy_fixture(source_root)
            if fixture is None or fixture[1] != fixture_sha256:
                return False
            fixture_milestones = fixture[0].get("milestones")
            if not isinstance(fixture_milestones, Mapping):
                return False
            fixture_row = fixture_milestones.get(milestone)
            if (
                not isinstance(fixture_row, Mapping)
                or hashlib.sha256(
                    _canonical_json_bytes(fixture_row.get("expected"))
                ).hexdigest()
                != expected_result_sha256
            ):
                return False
        commands = [
            target.command_id
            for target in TARGET_COMMANDS
            if target.milestone == milestone
        ]
        if row.get("target_command_ids") != commands:
            return False
        statuses: list[object] = []
        policy_checks: list[dict[str, object]] = []
        for command_id in commands:
            target_row = target_by_id.get(command_id)
            if (
                target_row is None
                or target_row.get("milestone") != milestone
                or target_row.get("implementation_path") != relative
                or target_row.get("implementation_sha256") != implementation_sha256
                or target_row.get("implementation_variant") != variant
                or target_row.get("policy_fixture_path")
                != MILESTONE_POLICY_FIXTURE.as_posix()
                or target_row.get("policy_fixture_sha256") != fixture_sha256
                or target_row.get("policy_expected_result_sha256")
                != expected_result_sha256
            ):
                return False
            target_status = target_row.get("status")
            statuses.append(target_status)
            if target_status == "passed":
                policy_path = target_row.get("policy_check_path")
                policy_sha256 = target_row.get("policy_check_sha256")
                policy_result_sha256 = target_row.get("policy_result_sha256")
                if (
                    policy_path
                    != f".ses/capstone-clean-room-policy-checks/{command_id}.json"
                    or not isinstance(policy_sha256, str)
                    or re.fullmatch(r"[0-9a-f]{64}", policy_sha256) is None
                    or not isinstance(policy_result_sha256, str)
                    or re.fullmatch(r"[0-9a-f]{64}", policy_result_sha256) is None
                    or policy_result_sha256 != expected_result_sha256
                ):
                    return False
                expected_receipt = CapstoneMilestonePolicyCheck.model_validate(
                    {
                        "schema_version": "v1alpha1",
                        "record_type": "capstone_milestone_policy_check",
                        "milestone": milestone,
                        "command_id": command_id,
                        "implementation_variant": variant,
                        "implementation_path": relative,
                        "implementation_sha256": implementation_sha256,
                        "fixture_path": MILESTONE_POLICY_FIXTURE.as_posix(),
                        "fixture_sha256": fixture_sha256,
                        "policy_result_sha256": policy_result_sha256,
                        "status": "passed",
                        "target_exit_code": 0,
                    }
                )
                if (
                    hashlib.sha256(artifact_json_bytes(expected_receipt)).hexdigest()
                    != policy_sha256
                ):
                    return False
                policy_checks.append(
                    {
                        "command_id": command_id,
                        "policy_check_sha256": policy_sha256,
                    }
                )
        expected_status = (
            "failed"
            if "failed" in statuses
            else "passed"
            if statuses and set(statuses) == {"passed"}
            else "not_executed"
        )
        if row.get("status") != expected_status:
            return False
        expected_policy_summary = (
            hashlib.sha256(_canonical_json_bytes(policy_checks)).hexdigest()
            if expected_status == "passed" and len(policy_checks) == len(commands)
            else None
        )
        if row.get("policy_check_summary_sha256") != expected_policy_summary:
            return False
    target_statuses = {
        row.get("status") for row in target_rows if isinstance(row, Mapping)
    }
    index_sha256 = evidence.get("capstone_index_sha256")
    index_hash_valid = (
        isinstance(index_sha256, str)
        and re.fullmatch(r"[0-9a-f]{64}", index_sha256) is not None
    )
    if target_by_id["automation.capstone_index"].get("status") == "passed" and not (
        index_hash_valid
    ):
        return False
    expected_completion = (
        "workflow_complete"
        if target_statuses == {"passed"} and index_hash_valid
        else "incomplete"
    )
    return evidence.get("learning_completion") == expected_completion


def _check_target_commands(
    manifest: Mapping[str, object] | None,
    evidence: Mapping[str, object] | None,
) -> ReleaseCheck:
    failures: list[str] = []
    manifest_rows = manifest.get("target_commands") if manifest is not None else None
    by_id: dict[str, Mapping[str, object]] = {}
    if isinstance(manifest_rows, list):
        by_id = {
            str(row.get("id")): row
            for row in manifest_rows
            if isinstance(row, Mapping) and isinstance(row.get("id"), str)
        }
    expected = {row.command_id: row for row in TARGET_COMMANDS}
    if set(by_id) != set(expected):
        failures.append("target_command_inventory_mismatch")
    for command_id, target in expected.items():
        row = by_id.get(command_id)
        if row is None:
            continue
        if row.get("milestone") != target.milestone or _normalized(
            str(row.get("command", ""))
        ) != _normalized(target.command):
            failures.append(f"target_command_drift:{command_id}")
    if failures:
        return ReleaseCheck(
            "capstone.target_commands",
            CheckStatus.FAIL,
            "The learner command contract differs from the capstone specification.",
            tuple(failures),
        )
    if evidence is None:
        return ReleaseCheck(
            "capstone.target_commands",
            CheckStatus.DEVIATION,
            "The target CLI contract is locked but has no clean-room execution evidence.",
            TARGET_COMMAND_IDS,
        )
    records = evidence.get("target_commands")
    expected_records = tuple(
        (target.command_id, target.command) for target in TARGET_COMMANDS
    )
    if not _command_records_match(
        records,
        expected_records,
        allowed_statuses=frozenset({"passed", "not_executed", "failed"}),
    ):
        return ReleaseCheck(
            "capstone.target_commands",
            CheckStatus.FAIL,
            "Target command evidence inventory, hash, status, or exit code is invalid.",
            ("target_command_evidence_inventory_mismatch",),
        )
    assert isinstance(records, list)
    status_by_id = {
        str(row.get("command_id")): row.get("status")
        for row in records
        if isinstance(row, Mapping)
    }
    failed = tuple(
        command_id
        for command_id in TARGET_COMMAND_IDS
        if status_by_id[command_id] == "failed"
    )
    if failed:
        return ReleaseCheck(
            "capstone.target_commands",
            CheckStatus.FAIL,
            "One or more learner-visible target commands failed.",
            failed,
        )
    pending = tuple(
        command_id
        for command_id in TARGET_COMMAND_IDS
        if status_by_id[command_id] != "passed"
    )
    if pending:
        return ReleaseCheck(
            "capstone.target_commands",
            CheckStatus.DEVIATION,
            "The target CLI contract remains explicitly unexecuted.",
            pending,
        )
    return ReleaseCheck(
        "capstone.target_commands",
        CheckStatus.PASS,
        "Every learner-visible target command passed in the fixed clean room.",
    )


def _check_clean_room_evidence(
    evidence: Mapping[str, object] | None,
    *,
    current_tree_sha256: str | None,
    source_root: Path,
) -> ReleaseCheck:
    if evidence is None:
        return ReleaseCheck(
            "capstone.clean_room_evidence",
            CheckStatus.DEVIATION,
            "No capstone clean-room evidence was supplied.",
            ("run scripts/run_capstone_clean_room.py",),
        )
    if (
        evidence.get("schema_version") != "v1alpha1"
        or evidence.get("record_type") != "shopping_capstone_clean_room_evidence"
        or evidence.get("course_kind") != "independent_capstone"
        or evidence.get("source_materialization") != "working_tree_regular_files"
        or not isinstance(evidence.get("source_tree_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", str(evidence.get("source_tree_sha256")))
        is None
    ):
        return ReleaseCheck(
            "capstone.clean_room_evidence",
            CheckStatus.FAIL,
            "Capstone evidence identity or current-worktree materialization is invalid.",
            ("invalid_clean_room_identity",),
        )
    if evidence.get("credential_environment_names") != []:
        return ReleaseCheck(
            "capstone.clean_room_evidence",
            CheckStatus.FAIL,
            "Clean-room evidence exposed credential environment names.",
            ("credential_environment_not_empty",),
        )
    if (
        current_tree_sha256 is None
        or evidence.get("source_tree_sha256") != current_tree_sha256
    ):
        return ReleaseCheck(
            "capstone.clean_room_evidence",
            CheckStatus.FAIL,
            "Clean-room evidence does not bind the current worktree bytes.",
            ("source_tree_sha256_mismatch",),
        )
    if not _milestone_implementation_evidence_valid(
        evidence,
        source_root=source_root,
    ):
        return ReleaseCheck(
            "capstone.clean_room_evidence",
            CheckStatus.FAIL,
            "Clean-room evidence is not bound to five executed milestone implementations.",
            ("milestone_implementation_evidence_invalid",),
        )
    sync = evidence.get("locked_sync")
    fixed = evidence.get("fixed_commands")
    if (
        not isinstance(sync, Mapping)
        or sync.get("command") != "uv sync --all-extras --locked --offline"
        or sync.get("status") != "passed"
        or sync.get("exit_code") != 0
        or not _command_records_match(
            fixed,
            FIXED_CLEAN_ROOM_COMMANDS,
            allowed_statuses=frozenset({"passed"}),
        )
    ):
        return ReleaseCheck(
            "capstone.clean_room_evidence",
            CheckStatus.FAIL,
            "Locked sync or fixed capstone checks did not pass.",
            ("fixed_clean_room_incomplete",),
        )
    if evidence.get("learning_completion") != "workflow_complete":
        return ReleaseCheck(
            "capstone.clean_room_evidence",
            CheckStatus.FAIL,
            "The learner implementation did not complete every graduation milestone.",
            ("learner_workflow_incomplete",),
        )
    return ReleaseCheck(
        "capstone.clean_room_evidence",
        CheckStatus.PASS,
        "The current worktree passed locked, credential-free fixed checks.",
    )


def _check_course_tests(
    root: Path,
    run_course_tests: bool,
    *,
    implementation_variant: str | None,
) -> ReleaseCheck:
    if not run_course_tests:
        return ReleaseCheck(
            "capstone.course_tests",
            CheckStatus.DEVIATION,
            "Capstone course tests were not requested by this validator run.",
            ("pass --run-course-tests",),
        )
    environment = _safe_environment(os.environ)
    if implementation_variant in {"starter", "solution"}:
        environment["SES_CAPSTONE_IMPLEMENTATION_VARIANT"] = implementation_variant
    completed = subprocess.run(
        [
            "uv",
            "run",
            "--offline",
            "--frozen",
            "pytest",
            "-q",
            "fixtures/seed/capstone-shopping-assistant/tests",
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if completed.returncode != 0:
        return ReleaseCheck(
            "capstone.course_tests",
            CheckStatus.FAIL,
            "Capstone course tests failed.",
            (f"exit_code:{completed.returncode}",),
        )
    return ReleaseCheck(
        "capstone.course_tests",
        CheckStatus.PASS,
        "Capstone course tests passed.",
    )


def validate_capstone_course(
    root: Path,
    *,
    command_evidence: Path | None = None,
    run_course_tests: bool = False,
) -> CapstoneReleaseReport:
    """Validate the capstone without adding it to the ten-lesson inventory."""

    root = root.resolve(strict=True)
    capstone = root / CAPSTONE_RELATIVE_ROOT
    manifest = _manifest(root)
    evidence = _load_json(command_evidence) if command_evidence is not None else None
    current_tree_sha256 = worktree_sha256(root) if evidence is not None else None
    evidence_variant = evidence.get("implementation_variant") if evidence else None
    implementation_variant = (
        evidence_variant
        if evidence_variant in {"starter", "solution"}
        else os.environ.get("SES_CAPSTONE_IMPLEMENTATION_VARIANT")
    )
    checks = (
        _check_identity(capstone, manifest),
        _check_milestones(
            capstone,
            manifest,
            implementation_variant=implementation_variant,
        ),
        _check_assets(capstone),
        _check_live_fail_closed(capstone, manifest, evidence),
        _check_target_commands(manifest, evidence),
        _check_clean_room_evidence(
            evidence,
            current_tree_sha256=current_tree_sha256,
            source_root=root,
        ),
        _check_course_tests(
            root,
            run_course_tests,
            implementation_variant=implementation_variant,
        ),
    )
    return CapstoneReleaseReport(
        checks=checks,
        milestone_count=len(MILESTONE_IDS),
        target_command_count=len(TARGET_COMMAND_IDS),
    )


def _excluded(relative: Path) -> bool:
    if any(part in _EXCLUDED_DIRECTORY_NAMES for part in relative.parts):
        return True
    if relative.name == ".env" or relative.name.startswith(".env."):
        return True
    if relative.name == ".DS_Store" or relative.suffix == ".pyc":
        return True
    return any(
        relative == prefix or prefix in relative.parents
        for prefix in _EXCLUDED_PATH_PREFIXES
    )


def _git_worktree_files(source: Path) -> tuple[Path, ...] | None:
    """Return tracked plus non-ignored untracked paths using current file bytes."""

    completed = subprocess.run(
        [
            "git",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
        ],
        cwd=source,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return None
    files: list[Path] = []
    for raw in completed.stdout.split(b"\0"):
        if not raw:
            continue
        try:
            value = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("worktree contains a non-UTF-8 path") from exc
        if "\\" in value:
            raise ValueError("worktree contains an unsafe path")
        relative = PurePosixPath(value)
        if relative.is_absolute() or any(
            part in {"", ".", ".."} for part in relative.parts
        ):
            raise ValueError("worktree contains an unsafe path")
        files.append(Path(*relative.parts))
    return tuple(sorted(set(files), key=lambda path: path.as_posix()))


def _fallback_worktree_files(source: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    for current, directory_names, file_names in os.walk(
        source, topdown=True, followlinks=False
    ):
        current_path = Path(current)
        relative_current = current_path.relative_to(source)
        kept_directories: list[str] = []
        for name in sorted(directory_names):
            path = current_path / name
            relative = relative_current / name
            if _excluded(relative):
                continue
            if path.is_symlink():
                raise ValueError(f"worktree contains symlink directory: {relative}")
            kept_directories.append(name)
        directory_names[:] = kept_directories
        files.extend(relative_current / name for name in sorted(file_names))
    return tuple(files)


def _worktree_files(source: Path) -> tuple[Path, ...]:
    relative_files = _git_worktree_files(source)
    return (
        _fallback_worktree_files(source) if relative_files is None else relative_files
    )


def _worktree_file(source: Path, relative: Path) -> tuple[bytes, int] | None:
    if _excluded(relative):
        return None
    source_file = source / relative
    if not source_file.exists() and not source_file.is_symlink():
        return None
    file_stat = source_file.lstat()
    if not stat.S_ISREG(file_stat.st_mode):
        raise ValueError(f"worktree contains non-regular file: {relative}")
    return source_file.read_bytes(), file_stat.st_mode & 0o777


def _update_tree_digest(
    digest: _Digest,
    *,
    relative: Path,
    content: bytes,
    mode: int,
) -> None:
    encoded = relative.as_posix().encode("utf-8")
    digest.update(len(encoded).to_bytes(8, "big"))
    digest.update(encoded)
    digest.update(mode.to_bytes(2, "big"))
    digest.update(hashlib.sha256(content).digest())


def worktree_sha256(source_root: Path) -> str:
    """Hash current tracked and non-ignored untracked regular-file bytes."""

    source = source_root.resolve(strict=True)
    digest = hashlib.sha256()
    for relative in _worktree_files(source):
        loaded = _worktree_file(source, relative)
        if loaded is None:
            continue
        content, mode = loaded
        _update_tree_digest(
            digest,
            relative=relative,
            content=content,
            mode=mode,
        )
    return digest.hexdigest()


def materialize_worktree(source_root: Path, workspace: Path) -> str:
    """Copy current regular-file bytes, including dirty and untracked development files."""

    source = source_root.resolve(strict=True)
    destination = workspace.resolve(strict=False)
    try:
        destination.relative_to(source)
    except ValueError:
        pass
    else:
        raise ValueError("clean-room workspace must be outside the source worktree")
    if destination.exists() and any(destination.iterdir()):
        raise FileExistsError(f"clean-room workspace is not empty: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    for relative in _worktree_files(source):
        loaded = _worktree_file(source, relative)
        if loaded is None:
            continue
        content, mode = loaded
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        target.chmod(mode)
        _update_tree_digest(
            digest,
            relative=relative,
            content=content,
            mode=mode,
        )
    return digest.hexdigest()


def _source_git_metadata(source_root: Path) -> tuple[str | None, bool | None]:
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=source_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if revision.returncode != 0:
        return None, None
    status_result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=source_root,
        capture_output=True,
        text=True,
        check=False,
    )
    source_dirty = None if status_result.returncode != 0 else bool(status_result.stdout)
    return revision.stdout.strip(), source_dirty


def _command_record(
    *,
    command_id: str,
    command: str | None,
    status_value: str,
    exit_code: int | None,
    reason: str | None = None,
    completed: subprocess.CompletedProcess[str] | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "command_id": command_id,
        "command_sha256": (
            hashlib.sha256(command.encode()).hexdigest()
            if command is not None
            else None
        ),
        "status": status_value,
        "exit_code": exit_code,
    }
    if reason is not None:
        record["reason"] = reason
    if completed is not None:
        record.update(_output_digest(completed))
    return record


def _target_variables(destination: Path) -> dict[str, str]:
    root = destination / ".ses" / "shopping-capstone"
    registry = root / "registry"
    return {
        "PROFILE": str(
            destination
            / "fixtures"
            / "seed"
            / "capstone-shopping-assistant"
            / "profiles"
            / "fixed-v1.json"
        ),
        "ROOT": str(root),
        "REGISTRY": str(registry),
        "PAIRED_TRACE": str(
            root
            / "run-shopping-develop-skill-v0-fixed"
            / "artifacts"
            / "shopping-develop-01"
            / "iteration-0"
            / "attempt-0"
            / "trace-turn-0001.json"
        ),
        "MANUAL_GATE_DECISION": str(
            registry / "gates" / "gate-shopping-manual" / "gate-decision.json"
        ),
        "REJECTED_GATE_DECISION": str(
            registry / "gates" / "gate-auto-r002" / "gate-decision.json"
        ),
        "INSTALL_ROOT": str(root / "installed-skill"),
    }


def _milestone_bindings(
    destination: Path,
    variant: MilestoneImplementationVariant,
) -> dict[str, dict[str, object]]:
    fixture = _policy_fixture(destination)
    if fixture is None:
        raise ValueError("milestone policy fixture is invalid")
    fixture_value, fixture_sha256 = fixture
    fixture_milestones = fixture_value["milestones"]
    assert isinstance(fixture_milestones, Mapping)
    bindings: dict[str, dict[str, object]] = {}
    for milestone in MILESTONE_IDS:
        relative = (CAPSTONE_RELATIVE_ROOT / variant / f"{milestone}.py").as_posix()
        path = destination / relative
        if not path.is_file() or path.is_symlink():
            raise ValueError(
                f"milestone implementation is not a regular file: {relative}"
            )
        fixture_row = fixture_milestones[milestone]
        assert isinstance(fixture_row, Mapping)
        expected_result = fixture_row["expected"]
        bindings[milestone] = {
            "milestone": milestone,
            "implementation_variant": variant,
            "implementation_path": relative,
            "implementation_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "policy_fixture_path": MILESTONE_POLICY_FIXTURE.as_posix(),
            "policy_fixture_sha256": fixture_sha256,
            "policy_expected_result_sha256": hashlib.sha256(
                _canonical_json_bytes(expected_result)
            ).hexdigest(),
        }
    return bindings


def _bound_target_command(
    *,
    destination: Path,
    variant: MilestoneImplementationVariant,
    target: CapstoneTargetCommand,
    command: Sequence[str],
    policy_receipt: Path,
) -> tuple[str, ...]:
    return (
        "uv",
        "run",
        "--offline",
        "--frozen",
        "python",
        "scripts/execute_capstone_milestone.py",
        "--root",
        str(destination),
        "--variant",
        variant,
        "--milestone",
        target.milestone,
        "--command-id",
        target.command_id,
        "--policy-receipt",
        str(policy_receipt),
        "--",
        *command,
    )


def _policy_receipt_path(
    destination: Path,
    target: CapstoneTargetCommand,
) -> Path:
    return (
        destination
        / ".ses"
        / "capstone-clean-room-policy-checks"
        / f"{target.command_id}.json"
    )


def _verified_policy_check(
    *,
    destination: Path,
    target: CapstoneTargetCommand,
    binding: Mapping[str, object],
    receipt_path: Path,
    target_exit_code: int,
) -> dict[str, object] | None:
    try:
        content = receipt_path.read_bytes()
        record = CapstoneMilestonePolicyCheck.model_validate_json(content)
    except (OSError, ValueError):
        return None
    if artifact_json_bytes(record) != content:
        return None
    value = record.model_dump(mode="json")
    expected = {
        "schema_version": "v1alpha1",
        "record_type": "capstone_milestone_policy_check",
        "milestone": target.milestone,
        "command_id": target.command_id,
        "implementation_variant": binding.get("implementation_variant"),
        "implementation_path": binding.get("implementation_path"),
        "implementation_sha256": binding.get("implementation_sha256"),
        "fixture_path": binding.get("policy_fixture_path"),
        "fixture_sha256": binding.get("policy_fixture_sha256"),
        "status": "passed",
        "target_exit_code": target_exit_code,
    }
    if any(
        value.get(name) != expected_value for name, expected_value in expected.items()
    ):
        return None
    result_sha256 = value.get("policy_result_sha256")
    if (
        not isinstance(result_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", result_sha256) is None
    ):
        return None
    if result_sha256 != binding.get("policy_expected_result_sha256"):
        return None
    try:
        relative = receipt_path.relative_to(destination).as_posix()
    except ValueError:
        return None
    return {
        "policy_check_path": relative,
        "policy_check_sha256": hashlib.sha256(content).hexdigest(),
        "policy_result_sha256": result_sha256,
    }


def _target_record(
    *,
    target: CapstoneTargetCommand,
    binding: Mapping[str, object],
    status_value: str,
    exit_code: int | None,
    reason: str | None = None,
    completed: subprocess.CompletedProcess[str] | None = None,
) -> dict[str, object]:
    record = _command_record(
        command_id=target.command_id,
        command=target.command,
        status_value=status_value,
        exit_code=exit_code,
        reason=reason,
        completed=completed,
    )
    record.update(binding)
    return record


def _milestone_execution_records(
    target_records: Sequence[Mapping[str, object]],
    bindings: Mapping[str, Mapping[str, object]],
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for milestone in MILESTONE_IDS:
        commands = tuple(
            row.command_id for row in TARGET_COMMANDS if row.milestone == milestone
        )
        statuses = {
            str(row.get("command_id")): row.get("status")
            for row in target_records
            if row.get("milestone") == milestone
        }
        if any(statuses.get(command_id) == "failed" for command_id in commands):
            status_value = "failed"
        elif all(statuses.get(command_id) == "passed" for command_id in commands):
            status_value = "passed"
        else:
            status_value = "not_executed"
        record = dict(bindings[milestone])
        policy_checks = [
            {
                "command_id": command_id,
                "policy_check_sha256": statuses_row.get("policy_check_sha256"),
            }
            for command_id in commands
            if (
                statuses_row := next(
                    (
                        row
                        for row in target_records
                        if row.get("command_id") == command_id
                    ),
                    {},
                )
            ).get("status")
            == "passed"
        ]
        policy_summary_sha256 = (
            hashlib.sha256(_canonical_json_bytes(policy_checks)).hexdigest()
            if len(policy_checks) == len(commands)
            and all(
                isinstance(row.get("policy_check_sha256"), str) for row in policy_checks
            )
            else None
        )
        record.update(
            {
                "target_command_ids": list(commands),
                "status": status_value,
                "policy_check_summary_sha256": policy_summary_sha256,
            }
        )
        records.append(record)
    return records


_CANDIDATE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}")


def _accepted_candidate_id(
    completed: subprocess.CompletedProcess[str],
    *,
    decision_path: Path,
) -> str | None:
    for line in completed.stdout.splitlines():
        name, separator, value = line.partition("=")
        if name == "candidate_id" and separator:
            return value if _CANDIDATE_ID_PATTERN.fullmatch(value) else None
    decision = _load_json(decision_path)
    candidate_value = decision.get("candidate_id") if decision is not None else None
    return (
        candidate_value
        if isinstance(candidate_value, str)
        and _CANDIDATE_ID_PATTERN.fullmatch(candidate_value)
        else None
    )


def run_capstone_clean_room(
    source_root: Path,
    workspace: Path,
    *,
    environment: Mapping[str, str] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    implementation_variant: MilestoneImplementationVariant = "starter",
) -> dict[str, object]:
    """Run fixed checks from current worktree bytes; never execute a live command."""

    source = source_root.resolve(strict=True)
    destination = workspace.resolve(strict=False)
    tree_sha256 = materialize_worktree(source, destination)
    manifest = _manifest(destination)
    if manifest is None:
        raise ValueError("capstone course manifest is missing or invalid")
    fixed_rows = manifest.get("clean_room_commands")
    actual_fixed_commands = (
        tuple(
            (row.get("id"), row.get("command"))
            for row in fixed_rows
            if isinstance(row, Mapping)
        )
        if isinstance(fixed_rows, list)
        else ()
    )
    if actual_fixed_commands != FIXED_CLEAN_ROOM_COMMANDS:
        raise ValueError("capstone clean-room command contract drifted")
    target_rows = manifest.get("target_commands")
    actual_target_commands = (
        tuple(
            (row.get("id"), row.get("milestone"), row.get("command"))
            for row in target_rows
            if isinstance(row, Mapping)
        )
        if isinstance(target_rows, list)
        else ()
    )
    expected_target_commands = tuple(
        (row.command_id, row.milestone, row.command) for row in TARGET_COMMANDS
    )
    if actual_target_commands != expected_target_commands:
        raise ValueError("capstone target command contract drifted")
    if manifest.get("milestone_execution") != MILESTONE_EXECUTION_CONTRACT:
        raise ValueError("capstone milestone execution contract drifted")
    bindings = _milestone_bindings(destination, implementation_variant)
    clean_environment = _safe_environment(environment or os.environ)
    clean_environment["SES_CAPSTONE_IMPLEMENTATION_VARIANT"] = implementation_variant
    sync_command = ("uv", "sync", "--all-extras", "--locked", "--offline")
    sync = runner(
        sync_command,
        cwd=destination,
        env=clean_environment,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    fixed_records: list[dict[str, object]] = []
    assert isinstance(fixed_rows, list)
    for row in fixed_rows:
        if not isinstance(row, Mapping):
            raise ValueError("invalid fixed clean-room command")
        command_id = row.get("id")
        command = row.get("command")
        if not isinstance(command_id, str) or not isinstance(command, str):
            raise ValueError("invalid fixed clean-room command fields")
        if _LIVE_COMMAND_PATTERN.search(command):
            raise ValueError("live command cannot enter the fixed clean room")
        if sync.returncode != 0:
            fixed_records.append(
                _command_record(
                    command_id=command_id,
                    command=command,
                    status_value="not_executed",
                    exit_code=None,
                    reason="locked_sync_failed",
                )
            )
            continue
        completed = runner(
            tuple(shlex.split(command)),
            cwd=destination,
            env=clean_environment,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        fixed_records.append(
            _command_record(
                command_id=command_id,
                command=command,
                status_value=("passed" if completed.returncode == 0 else "failed"),
                exit_code=completed.returncode,
                completed=completed,
            )
        )
    target_records: list[dict[str, object]] = []
    fixed_passed = sync.returncode == 0 and all(
        row.get("status") == "passed" for row in fixed_records
    )
    target_failed = not fixed_passed
    variables = _target_variables(destination)
    capstone_index_sha256: str | None = None
    for target in TARGET_COMMANDS:
        binding = bindings[target.milestone]
        if target_failed:
            target_records.append(
                _target_record(
                    target=target,
                    binding=binding,
                    status_value="not_executed",
                    exit_code=None,
                    reason=(
                        "fixed_checks_failed"
                        if not fixed_passed
                        else "prior_target_failed"
                    ),
                )
            )
            continue
        try:
            expanded = Template(target.command).substitute(variables)
            command = tuple(shlex.split(expanded))
        except (KeyError, ValueError):
            target_records.append(
                _target_record(
                    target=target,
                    binding=binding,
                    status_value="failed",
                    exit_code=1,
                    reason="target_variable_unavailable",
                )
            )
            target_failed = True
            continue
        if _LIVE_COMMAND_PATTERN.search(expanded):
            raise ValueError("live command cannot enter the fixed clean room")
        policy_receipt = _policy_receipt_path(destination, target)
        completed = runner(
            _bound_target_command(
                destination=destination,
                variant=implementation_variant,
                target=target,
                command=command,
                policy_receipt=policy_receipt,
            ),
            cwd=destination,
            env=clean_environment,
            capture_output=True,
            text=True,
            timeout=900,
            check=False,
        )
        policy_check = _verified_policy_check(
            destination=destination,
            target=target,
            binding=binding,
            receipt_path=policy_receipt,
            target_exit_code=completed.returncode,
        )
        passed = completed.returncode == 0 and policy_check is not None
        evidence_exit_code = completed.returncode if completed.returncode != 0 else 1
        target_records.append(
            _target_record(
                target=target,
                binding=binding,
                status_value="passed" if passed else "failed",
                exit_code=0 if passed else evidence_exit_code,
                reason=(
                    None
                    if policy_check is not None
                    else "milestone_policy_check_missing_or_invalid"
                ),
                completed=completed,
            )
        )
        if policy_check is not None:
            target_records[-1].update(policy_check)
        if not passed:
            target_failed = True
            continue
        assert policy_check is not None
        if target.command_id == "gate.candidate":
            candidate_id = _accepted_candidate_id(
                completed,
                decision_path=Path(variables["MANUAL_GATE_DECISION"]),
            )
            if candidate_id is None:
                target_records[-1] = _target_record(
                    target=target,
                    binding=binding,
                    status_value="failed",
                    exit_code=1,
                    reason="accepted_candidate_id_missing",
                    completed=completed,
                )
                target_records[-1].update(policy_check)
                target_failed = True
                continue
            variables["ACCEPTED_CANDIDATE_ID"] = candidate_id
        if target.command_id == "automation.capstone_index":
            index_path = Path(variables["ROOT"]) / "capstone-index.json"
            index = _load_json(index_path)
            if (
                index is None
                or index.get("record_type") != "capstone_index"
                or index.get("learning_completion") != "workflow_complete"
                or index.get("measurement_kind") != "synthetic_offline"
            ):
                target_records[-1] = _target_record(
                    target=target,
                    binding=binding,
                    status_value="failed",
                    exit_code=1,
                    reason="workflow_complete_index_missing",
                    completed=completed,
                )
                target_records[-1].update(policy_check)
                target_failed = True
                continue
            capstone_index_sha256 = hashlib.sha256(index_path.read_bytes()).hexdigest()
    live_records = [
        _command_record(
            command_id="live.full_workflow",
            command=None,
            status_value="blocked",
            exit_code=None,
            reason="phase0_no_go",
        )
    ]
    revision, source_dirty = _source_git_metadata(source)
    milestone_records = _milestone_execution_records(target_records, bindings)
    learning_completion = (
        "workflow_complete"
        if capstone_index_sha256 is not None
        and all(row.get("status") == "passed" for row in target_records)
        and all(row.get("status") == "passed" for row in milestone_records)
        else "incomplete"
    )
    return {
        "schema_version": "v1alpha1",
        "record_type": "shopping_capstone_clean_room_evidence",
        "course_kind": "independent_capstone",
        "environment_kind": "fresh_temporary_copy",
        "repository_commit": revision,
        "source_dirty": source_dirty,
        "source_materialization": "working_tree_regular_files",
        "source_tree_sha256": tree_sha256,
        "credential_environment_names": [],
        "implementation_variant": implementation_variant,
        "milestone_implementations": milestone_records,
        "learning_completion": learning_completion,
        "capstone_index_sha256": capstone_index_sha256,
        "locked_sync": {
            "command": " ".join(sync_command),
            "status": "passed" if sync.returncode == 0 else "failed",
            "exit_code": sync.returncode,
            **_output_digest(sync),
        },
        "fixed_commands": fixed_records,
        "target_commands": target_records,
        "live_commands": live_records,
    }


def capstone_evidence_exit_code(payload: Mapping[str, object]) -> int:
    """Return 1 for failure, 2 for explicit target gaps, and 0 for a full pass."""

    sync = payload.get("locked_sync")
    fixed = payload.get("fixed_commands")
    target = payload.get("target_commands")
    live = payload.get("live_commands")
    if (
        payload.get("schema_version") != "v1alpha1"
        or payload.get("record_type") != "shopping_capstone_clean_room_evidence"
        or payload.get("course_kind") != "independent_capstone"
        or payload.get("source_materialization") != "working_tree_regular_files"
        or not isinstance(sync, Mapping)
        or sync.get("command") != "uv sync --all-extras --locked --offline"
        or sync.get("status") != "passed"
        or sync.get("exit_code") != 0
    ):
        return 1
    if not _command_records_match(
        fixed,
        FIXED_CLEAN_ROOM_COMMANDS,
        allowed_statuses=frozenset({"passed"}),
    ):
        return 1
    if not _milestone_implementation_evidence_valid(payload):
        return 1
    if not _command_records_match(
        live,
        (("live.full_workflow", None),),
        allowed_statuses=frozenset({"blocked"}),
    ):
        return 1
    if not _command_records_match(
        target,
        tuple((row.command_id, row.command) for row in TARGET_COMMANDS),
        allowed_statuses=frozenset({"passed", "not_executed", "failed"}),
    ):
        return 1
    assert isinstance(target, list)
    statuses = {row.get("status") for row in target if isinstance(row, Mapping)}
    if len(statuses) == 0 or "failed" in statuses:
        return 1
    if statuses != {"passed"}:
        return 2
    return 0


def write_capstone_evidence(path: Path, payload: Mapping[str, object]) -> None:
    """Write canonical capstone evidence after subprocess execution."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )


__all__ = [
    "CAPSTONE_RELATIVE_ROOT",
    "FIXED_CLEAN_ROOM_COMMANDS",
    "MILESTONE_EXECUTION_CONTRACT",
    "MILESTONE_IDS",
    "MILESTONE_POLICY_FIXTURE",
    "TARGET_COMMANDS",
    "TARGET_COMMAND_IDS",
    "CapstoneReleaseReport",
    "CheckStatus",
    "MilestoneImplementationVariant",
    "ReleaseCheck",
    "capstone_evidence_exit_code",
    "materialize_worktree",
    "run_capstone_clean_room",
    "validate_capstone_course",
    "worktree_sha256",
    "write_capstone_evidence",
]
