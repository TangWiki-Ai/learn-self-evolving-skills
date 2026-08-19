"""Fail-closed checks for the ten-lesson course release."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from html.parser import HTMLParser
from pathlib import Path

from ses.foundation.credentials import credential_values, is_sensitive_name
from ses.testset.holdout import (
    scan_external_holdout_leaks,
    validate_holdout_bundle,
    validate_public_holdout_bundle,
)

_LESSON_PATTERN = re.compile(r"^ch(?P<number>\d{2})-")
_INLINE_COMMAND_PATTERN = re.compile(r"`(uv run\s+[^`]+)`")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ABSOLUTE_PATH_PATTERNS = (
    re.compile(r"/Users/[A-Za-z0-9._-]+(?:/[^\s\"'`<>)]*)?"),
    re.compile(r"/home/[A-Za-z0-9._-]+(?:/[^\s\"'`<>)]*)?"),
    re.compile(r"[A-Za-z]:\\Users\\[A-Za-z0-9._-]+(?:\\[^\s\"'`<>)]*)?"),
)
_LITERAL_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{16,}\b", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_-]?key|access[_-]?token)\s*[:=]\s*[\"']([^\"']{12,})[\"']",
        re.IGNORECASE,
    ),
)
_KNOWN_TEST_CREDENTIAL_FIXTURE_PARTS = (
    ("exact-", "process-secret"),
    ("test-", "secret-value"),
    ("sk-", "leaksecret123456"),
    ("fixture-", "secret-value"),
    ("sk-", "example123456789"),
    ("sk-", "supersecret123456"),
)
_KNOWN_TEST_CREDENTIAL_FIXTURES = tuple(
    "".join(parts) for parts in _KNOWN_TEST_CREDENTIAL_FIXTURE_PARTS
)
_ALLOWED_ABSOLUTE_TEST_FIXTURES = {
    "tests/reporting/test_l3_report.py": ("/Us" + "ers/example/project/private.json",),
    "tests/testset/test_pipeline.py": ("C:/Us" + "ers/alice/source.json",),
}
_ALLOWED_RELEASE_BINARY_PATHS: frozenset[str] = frozenset()
_FALLBACK_SCAN_EXCLUDED_DIRS = frozenset(
    {".git", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".venv", "__pycache__"}
)
_PRIVATE_HOLDOUT_RECORD_TYPES = (
    "holdout_" + "private_fixture",
    "holdout_" + "deterministic_oracle",
    "holdout_" + "private_rubric",
    "protected_" + "holdout_inventory",
)
_PRIVATE_HOLDOUT_KEYS = (
    "source_" + "task_path",
    "state_" + "requirements",
    "upstream_" + "fixture_path",
    "user_" + "simulator",
)
_PRIVATE_HOLDOUT_SCORING = "all_must_" + "and_no_must_not_v1"
_REPORT_LIMIT_BYTES = 2_000_000
_REQUIRED_REPORTS = {
    "L1": Path(
        "course/ch06-verify-develop-cases/artifacts/run-ticket07-expanded/l1.html"
    ),
    "L2": Path("course/ch07-create-v0/artifacts/l2.html"),
    "L3": Path(
        "course/ch10-auto-evolve-and-portfolio/artifacts/fixed-reference/l3.html"
    ),
}
_REVIEW_PACKET = Path("docs/release/human-review-packet.md")
_FULL_DATA_OUTPUTS = {
    "candidate-list.jsonl",
    "cluster-assignments.jsonl",
    "cluster-summaries.jsonl",
    "funnel-counts.json",
    "label-metrics.json",
    "scrubbed-abcd.jsonl",
    "tau2-difficulty.jsonl",
}
_EXPECTED_FULL_OUTPUT_RECORDS = {
    "candidate-list.jsonl": 1_070,
    "cluster-assignments.jsonl": 1_070,
    "cluster-summaries.jsonl": 12,
    "funnel-counts.json": 1,
    "label-metrics.json": 1,
    "scrubbed-abcd.jsonl": 1_070,
    "tau2-difficulty.jsonl": 114,
}
_EXPECTED_ABCD_SUMMARY = {
    "aligned_original_delexed_records": 1_070,
    "delexed_turns": 28_535,
    "exact_product_defect": 1_070,
    "flow_counts": {"product_defect": 1_070},
    "original_turns": 28_535,
    "partition_counts": {"dev": 102, "test": 105, "train": 863},
    "records_with_delexed": 1_070,
    "records_with_original": 1_070,
    "source_conversations": 10_042,
    "subflow_counts": {
        "refund_initiate": 176,
        "refund_status": 179,
        "refund_update": 177,
        "return_color": 180,
        "return_size": 191,
        "return_stain": 167,
    },
}
_EXPECTED_TAU2_SUMMARY = {
    "difficulty_buckets": {"easy": 70, "hard": 10, "medium": 34},
    "runs_per_task": 16,
    "runs_removed_as_separate_candidate_units": 1_710,
    "source_tasks": 114,
    "task_aggregates": 114,
    "trajectory_runs": 1_824,
    "usage": ["deduplication_signal", "difficulty_signal"],
}
_EXPECTED_FULL_FUNNEL = {
    "abcd": {
        "candidate_cap_removed": 0,
        "candidate_pool": 1_070,
        "candidates": 1_070,
        "clustered": 1_070,
        "dropped_duplicates": 0,
        "dropped_empty": 0,
        "dropped_encoding": 0,
        "dropped_invalid": 0,
        "dropped_misaligned": 0,
        "exact_product_defect": 1_070,
        "scrubbed_unique": 1_070,
        "semantic_duplicates_removed": 0,
        "source_conversations": 10_042,
    },
    "profile": "full",
    "record_type": "candidate_mining_funnel",
    "schema_version": "v1alpha1",
    "state": {
        "return_item_tasks": 33,
        "return_item_trajectories": 21,
        "source_tasks": 150,
        "source_trajectories": 100,
    },
    "tau": {
        "easy_tasks": 70,
        "hard_tasks": 10,
        "medium_tasks": 34,
        "result_files": 4,
        "source_tasks": 114,
        "task_aggregates": 114,
        "trajectory_runs": 1_824,
    },
}
_EXPECTED_LESSON_ARTIFACTS: Mapping[int, tuple[str, ...]] = {
    1: ("comparison-artifact.json",),
    2: ("baseline-results.json",),
    3: ("agreement-experiment.json",),
    4: ("baseline-comparison.json",),
    5: ("full-funnel-reference.json",),
    6: ("qualification-funnel.json", "expanded-baseline.json"),
    7: ("artifacts/l2.html", "artifacts/summary.json"),
    8: ("artifacts/evidence-linked-patch-list.json",),
    9: (
        "artifacts/fixed-accept-promote-rollback/events.jsonl",
        "artifacts/fixed-rejection/events.jsonl",
    ),
    10: (
        "artifacts/fixed-reference/l3.html",
        "artifacts/fixed-reference/final-aggregate.json",
        "artifacts/fixed-reference/manifest.json",
    ),
}


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


@dataclass(frozen=True, slots=True)
class ReleaseReport:
    """Deterministic aggregate; it contains no local absolute root."""

    checks: tuple[ReleaseCheck, ...]
    lesson_count: int
    documented_command_count: int

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
            "record_type": "release_validation_report",
            "status": self.status.value,
            "repository": "repository_root",
            "lesson_count": self.lesson_count,
            "documented_command_count": self.documented_command_count,
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


@dataclass(frozen=True, slots=True)
class DocumentedCommand:
    lesson: int
    readme: str
    line: int
    command: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.command.encode()).hexdigest()

    @property
    def command_id(self) -> str:
        scope = "root" if self.lesson == 0 else f"lesson-{self.lesson:02d}"
        return f"{scope}:line-{self.line}:{self.sha256[:12]}"


def _relative(root: Path, path: Path) -> str:
    return path.resolve(strict=False).relative_to(root.resolve(strict=True)).as_posix()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _lesson_dirs(root: Path) -> dict[int, Path]:
    lessons: dict[int, Path] = {}
    course = root / "course"
    if not course.is_dir():
        return lessons
    for path in sorted(course.iterdir()):
        match = _LESSON_PATTERN.match(path.name)
        if not path.is_dir() or match is None:
            continue
        number = int(match.group("number"))
        if number in lessons:
            raise ValueError(f"duplicate lesson number: {number}")
        lessons[number] = path
    return lessons


def _check_course_structure(root: Path, lessons: Mapping[int, Path]) -> ReleaseCheck:
    failures: list[str] = []
    expected = set(range(1, 11))
    actual = set(lessons)
    if actual != expected:
        failures.append(
            f"lesson inventory differs; missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}"
        )
    for number, lesson in sorted(lessons.items()):
        required_dirs = ("starter", "solution", "tests")
        if not (lesson / "README.md").is_file():
            failures.append(f"lesson-{number:02d}: missing README.md")
        for directory in required_dirs:
            path = lesson / directory
            if not path.is_dir() or not any(path.glob("*.py")):
                failures.append(
                    f"lesson-{number:02d}: missing {directory} Python files"
                )
        tests_root = lesson / "tests"
        if tests_root.is_dir():
            test_parts: list[str] = []
            for test_path in sorted(tests_root.rglob("*.py")):
                test_text = _read_text(test_path)
                if test_text is None:
                    failures.append(
                        f"lesson-{number:02d}: unreadable test {test_path.name}"
                    )
                else:
                    test_parts.append(test_text)
            test_source = "\n".join(test_parts)
            for variant in ("starter", "solution"):
                if variant not in test_source:
                    failures.append(
                        f"lesson-{number:02d}: tests do not exercise {variant}"
                    )
        for artifact in _EXPECTED_LESSON_ARTIFACTS.get(number, ()):
            if not (lesson / artifact).is_file():
                failures.append(f"lesson-{number:02d}: missing artifact {artifact}")
        readme_path = lesson / "README.md"
        if readme_path.is_file():
            text = readme_path.read_text(encoding="utf-8")
            required_topics = {
                "starter": ("Starter", "starter"),
                "solution": ("solution", "Solution"),
                "tests": ("测试", "pytest"),
                "comparison": ("对照", "参考结果", "参考产物"),
                "cost": ("预算", "成本", "费用"),
                "reading": ("拓展阅读", "扩展阅读"),
            }
            for topic, markers in required_topics.items():
                if not any(marker in text for marker in markers):
                    failures.append(f"lesson-{number:02d}: README lacks {topic}")
    if failures:
        return ReleaseCheck(
            "course.structure",
            CheckStatus.FAIL,
            "Ten-lesson structure or required teaching material is incomplete.",
            tuple(failures),
        )
    return ReleaseCheck(
        "course.structure",
        CheckStatus.PASS,
        "All ten lessons contain README, starter, solution, tests, and reference artifacts.",
        tuple(
            f"lesson-{number:02d}:{path.name}"
            for number, path in sorted(lessons.items())
        ),
    )


def _tree_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    for file_path in sorted(item for item in path.rglob("*") if item.is_file()):
        relative = file_path.relative_to(path).as_posix().encode()
        payload = file_path.read_bytes()
        digest.update(relative)
        digest.update(b"\0")
        digest.update(str(len(payload)).encode("ascii"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
    return digest.hexdigest()


def _check_transitions(root: Path, lessons: Mapping[int, Path]) -> ReleaseCheck:
    manifest_path = root / "course" / "transition-manifest.json"
    transitions = tuple(
        f"lesson-{number:02d}->lesson-{number + 1:02d}" for number in range(1, 10)
    )
    if not manifest_path.is_file():
        return ReleaseCheck(
            "course.transitions",
            CheckStatus.DEVIATION,
            "The repository has independent exercise wrappers, not a mechanically proven cumulative solution-to-starter chain.",
            transitions,
        )
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        records = payload["records"]
        if not isinstance(records, list) or len(records) != 9:
            raise ValueError("transition manifest must contain nine records")
        failures: list[str] = []
        for number, record in enumerate(records, 1):
            if not isinstance(record, dict):
                raise ValueError("transition record must be an object")
            previous = lessons[number] / "solution"
            following = lessons[number + 1] / "starter"
            if record.get("from_solution_tree_sha256") != _tree_sha256(previous):
                failures.append(f"lesson-{number:02d}: solution hash drifted")
            if record.get("to_starter_tree_sha256") != _tree_sha256(following):
                failures.append(f"lesson-{number + 1:02d}: starter hash drifted")
            if record.get("mechanism") != "generated_cumulative_workspace":
                failures.append(
                    f"lesson-{number:02d}: transition mechanism is not cumulative generation"
                )
        if failures:
            return ReleaseCheck(
                "course.transitions",
                CheckStatus.FAIL,
                "The transition manifest exists but does not prove the current chain.",
                tuple(failures),
            )
    except (KeyError, OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return ReleaseCheck(
            "course.transitions",
            CheckStatus.FAIL,
            "The transition manifest is invalid.",
            (type(exc).__name__,),
        )
    return ReleaseCheck(
        "course.transitions",
        CheckStatus.PASS,
        "All nine cumulative solution-to-starter transitions match their locked tree hashes.",
        transitions,
    )


def _logical_commands(
    text: str, *, readme: str, lesson: int
) -> tuple[DocumentedCommand, ...]:
    commands: list[DocumentedCommand] = []
    in_bash = False
    fragments: list[str] = []
    start_line = 0
    for line_number, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not in_bash:
            if stripped in {"```bash", "```sh", "```shell"}:
                in_bash = True
                continue
            commands.extend(
                DocumentedCommand(
                    lesson,
                    readme,
                    line_number,
                    match.group(1).strip(),
                )
                for match in _INLINE_COMMAND_PATTERN.finditer(raw)
            )
            continue
        if stripped == "```":
            if fragments:
                commands.append(
                    DocumentedCommand(lesson, readme, start_line, " ".join(fragments))
                )
                fragments = []
            in_bash = False
            continue
        if not stripped or stripped.startswith("#"):
            continue
        if not fragments:
            start_line = line_number
        continuation = stripped.endswith("\\")
        fragments.append(stripped[:-1].rstrip() if continuation else stripped)
        if not continuation:
            commands.append(
                DocumentedCommand(lesson, readme, start_line, " ".join(fragments))
            )
            fragments = []
    return tuple(commands)


def _documented_commands(
    root: Path, lessons: Mapping[int, Path]
) -> tuple[DocumentedCommand, ...]:
    commands: list[DocumentedCommand] = []
    root_readme = root / "README.md"
    if root_readme.is_file():
        commands.extend(
            _logical_commands(
                root_readme.read_text(encoding="utf-8"),
                readme="README.md",
                lesson=0,
            )
        )
    for number, lesson in sorted(lessons.items()):
        readme = lesson / "README.md"
        if readme.is_file():
            commands.extend(
                _logical_commands(
                    readme.read_text(encoding="utf-8"),
                    readme=readme.relative_to(lesson.parents[1]).as_posix(),
                    lesson=number,
                )
            )
    return tuple(commands)


def _check_command_syntax(
    root: Path, commands: Sequence[DocumentedCommand]
) -> ReleaseCheck:
    failures: list[str] = []
    app_source = (root / "src/ses/cli/app.py").read_text(encoding="utf-8")
    for command in commands:
        value = command.command
        if (
            any(marker in value for marker in ("./my-skill", "YOUR_API_KEY", "=..."))
            or re.search(r"<[A-Za-z0-9_.-]+>", value) is not None
            or re.search(r"\b[A-Z][A-Z0-9_]*(?:KEY|TOKEN)\s*=", value) is not None
        ):
            failures.append(f"{command.command_id}: command contains a placeholder")
        candidate_match = re.search(r"--candidate\s+([^\s]+)", value)
        if candidate_match is not None:
            candidate = candidate_match.group(1).strip("'\"")
            if not candidate.startswith(("$", "/")) and not (root / candidate).exists():
                failures.append(f"{command.command_id}: candidate path is missing")
        for output_match in re.finditer(
            r"(?:--out|--output|--output-root)\s+([^\s]+)", value
        ):
            destination = output_match.group(1).strip("'\"")
            if destination.startswith(("course/", "data/")):
                failures.append(
                    f"{command.command_id}: generator writes into checked release data"
                )
        if value.startswith("uv sync "):
            if "--all-extras" not in value or "--locked" not in value:
                failures.append(
                    f"{command.command_id}: dependency sync is not fully locked"
                )
        elif value.startswith("python3 "):
            match = re.match(r"python3\s+([^\s]+)", value)
            if match is None or not (root / match.group(1)).is_file():
                failures.append(f"{command.command_id}: Python script is missing")
        elif "uv run pytest" in value:
            match = re.search(r"uv run pytest\s+([^\s]+)", value)
            if match is None or not (root / match.group(1)).exists():
                failures.append(f"{command.command_id}: pytest target is missing")
        elif "uv run python" in value:
            match = re.search(r"uv run python\s+([^\s]+)", value)
            if match is None:
                failures.append(f"{command.command_id}: Python invocation is malformed")
            elif match.group(1) == "-m":
                module_match = re.search(
                    r"uv run python\s+-m\s+([A-Za-z0-9_.]+)", value
                )
                if module_match is None:
                    failures.append(
                        f"{command.command_id}: Python module invocation is malformed"
                    )
                else:
                    module = Path(*module_match.group(1).split("."))
                    module_file = root / "src" / module.with_suffix(".py")
                    module_package = root / "src" / module / "__init__.py"
                    if not module_file.is_file() and not module_package.is_file():
                        failures.append(
                            f"{command.command_id}: Python module is missing"
                        )
            elif match.group(1) != "-c" and not (root / match.group(1)).is_file():
                failures.append(f"{command.command_id}: Python script is missing")
        elif "uv run ses" in value:
            match = re.search(r"uv run ses\s+([a-z0-9-]+)", value)
            if match is None or f'"{match.group(1)}"' not in app_source:
                failures.append(f"{command.command_id}: ses command is not registered")
        elif "$(uv run python" in value or re.match(r"^[A-Z][A-Z0-9_]+=", value):
            if "uv run" not in value:
                failures.append(
                    f"{command.command_id}: shell assignment has no executable"
                )
        else:
            failures.append(
                f"{command.command_id}: unsupported documented shell command"
            )
    if failures:
        return ReleaseCheck(
            "docs.command_syntax",
            CheckStatus.FAIL,
            "One or more documented commands cannot be resolved mechanically.",
            tuple(failures),
        )
    return ReleaseCheck(
        "docs.command_syntax",
        CheckStatus.PASS,
        f"All {len(commands)} documented root and lesson commands resolve to registered CLI, scripts, or test paths.",
        tuple(command.command_id for command in commands),
    )


def _check_command_evidence(
    commands: Sequence[DocumentedCommand],
    evidence_path: Path | None,
    *,
    expected_repository_commit: str,
) -> ReleaseCheck:
    if evidence_path is None:
        return ReleaseCheck(
            "docs.command_execution",
            CheckStatus.DEVIATION,
            "No clean-room command evidence was supplied; syntax checks do not prove execution.",
            tuple(command.command_id for command in commands),
        )
    try:
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("command evidence must be an object")
        if (
            payload.get("schema_version") != "v1alpha1"
            or payload.get("record_type") != "clean_room_command_evidence"
        ):
            raise ValueError("command evidence has the wrong record type")
        if payload.get("environment_kind") not in {
            "fresh_clone",
            "fresh_temporary_copy",
        }:
            raise ValueError("command evidence does not identify a clean environment")
        repository_commit = payload.get("repository_commit")
        if not isinstance(repository_commit, str) or not re.fullmatch(
            r"[0-9a-f]{40}", repository_commit
        ):
            raise ValueError("command evidence needs a full repository commit")
        if repository_commit != expected_repository_commit:
            raise ValueError(
                "command evidence belongs to a different repository commit"
            )
        if payload.get("source_clean") is not True:
            raise ValueError("command evidence must come from a clean source commit")
        if payload.get("source_materialization") != "git_archive_head_regular_files":
            raise ValueError("clean-room source must contain only regular HEAD files")
        if payload.get("shell_grouping") != "readme_fenced_blocks":
            raise ValueError("README commands must preserve fenced shell sessions")
        if payload.get("credential_environment_names") != []:
            raise ValueError(
                "clean-room commands must not receive credential variables"
            )
        locked_sync = payload.get("locked_sync")
        if not isinstance(locked_sync, dict):
            raise ValueError("command evidence needs a successful locked sync")
        allowed_sync_keys = {
            "command",
            "exit_code",
            "status",
            "stderr_sha256",
            "stdout_sha256",
        }
        if (
            not set(locked_sync).issubset(allowed_sync_keys)
            or locked_sync.get("command") != "uv sync --all-extras --locked"
            or locked_sync.get("status") != "passed"
            or locked_sync.get("exit_code", 0) != 0
            or any(
                key in locked_sync
                and (
                    not isinstance(locked_sync[key], str)
                    or _SHA256_PATTERN.fullmatch(locked_sync[key]) is None
                )
                for key in ("stderr_sha256", "stdout_sha256")
            )
        ):
            raise ValueError("command evidence needs a successful locked sync")
        records = payload["commands"]
        if not isinstance(records, list):
            raise ValueError("commands must be a list")
        by_id: dict[str, Mapping[str, object]] = {}
        for row in records:
            if not isinstance(row, dict):
                raise ValueError("command evidence records must be objects")
            command_id = row.get("command_id")
            command_sha256 = row.get("command_sha256")
            if (
                not isinstance(command_id, str)
                or not isinstance(command_sha256, str)
                or _SHA256_PATTERN.fullmatch(command_sha256) is None
            ):
                raise ValueError("command evidence needs a command ID and SHA256")
            if command_id in by_id:
                raise ValueError("command evidence IDs must be unique")
            by_id[command_id] = row
            status = row.get("status")
            exit_code = row.get("exit_code")
            reason = row.get("reason")
            if status == "passed" and exit_code != 0:
                raise ValueError("passed command evidence needs exit code 0")
            if status == "failed" and (
                not isinstance(exit_code, int) or exit_code == 0
            ):
                raise ValueError("failed command evidence needs a nonzero exit code")
            if status == "deviation" and not isinstance(reason, str):
                raise ValueError("deviation command evidence needs a reason")
    except (KeyError, OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return ReleaseCheck(
            "docs.command_execution",
            CheckStatus.FAIL,
            "Clean-room command evidence is invalid.",
            (type(exc).__name__,),
        )
    expected_by_id = {command.command_id: command for command in commands}
    if len(expected_by_id) != len(commands):
        return ReleaseCheck(
            "docs.command_execution",
            CheckStatus.FAIL,
            "Documented command IDs are not unique.",
        )
    missing = [command_id for command_id in expected_by_id if command_id not in by_id]
    failed = [
        command_id
        for command_id in expected_by_id
        if command_id in by_id and by_id[command_id].get("status") == "failed"
    ]
    deviated = [
        command_id
        for command_id in expected_by_id
        if command_id in by_id and by_id[command_id].get("status") == "deviation"
    ]
    invalid = [
        command_id
        for command_id, command in expected_by_id.items()
        if command_id in by_id
        and (
            by_id[command_id].get("status") not in {"passed", "failed", "deviation"}
            or by_id[command_id].get("command_sha256") != command.sha256
            or by_id[command_id].get("readme") != command.readme
            or by_id[command_id].get("line") != command.line
        )
    ]
    invalid.extend(
        f"unknown:{value}" for value in sorted(set(by_id) - set(expected_by_id))
    )
    if failed or invalid:
        return ReleaseCheck(
            "docs.command_execution",
            CheckStatus.FAIL,
            "Clean-room evidence records a failed or invalid documented command.",
            tuple([*failed, *invalid]),
        )
    if missing or deviated:
        return ReleaseCheck(
            "docs.command_execution",
            CheckStatus.DEVIATION,
            "Some documented commands were not run or ended with an explicit deviation.",
            tuple(
                [
                    *(f"missing:{item}" for item in missing),
                    *(f"deviation:{item}" for item in deviated),
                ]
            ),
        )
    return ReleaseCheck(
        "docs.command_execution",
        CheckStatus.PASS,
        f"Clean-room evidence records successful execution for all {len(commands)} commands.",
    )


def _run_course_tests(
    root: Path, lessons: Mapping[int, Path], *, enabled: bool
) -> ReleaseCheck:
    if not enabled:
        return ReleaseCheck(
            "course.tests",
            CheckStatus.DEVIATION,
            "Course tests were not executed by this validator invocation.",
            tuple(f"lesson-{number:02d}" for number in sorted(lessons)),
        )
    failures: list[str] = []
    passed: list[str] = []
    secrets = credential_values(os.environ)
    test_environment = {
        name: value for name, value in os.environ.items() if not is_sensitive_name(name)
    }
    for number, lesson in sorted(lessons.items()):
        target = lesson / "tests"
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", _relative(root, target)],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
            env=test_environment,
        )
        combined_output = completed.stdout + completed.stderr
        count_summary = _pytest_count_summary(combined_output)
        if completed.returncode == 0:
            passed.append(f"lesson-{number:02d}:{count_summary}")
            continue
        output = combined_output.replace(str(root), "<repo>")
        for secret in secrets:
            output = output.replace(secret, "<redacted>")
        compact = " ".join(output.split())[-500:]
        failures.append(
            f"lesson-{number:02d}:{count_summary}:exit={completed.returncode}:{compact}"
        )
    if failures:
        return ReleaseCheck(
            "course.tests",
            CheckStatus.FAIL,
            "At least one independent lesson test suite failed.",
            tuple([*passed, *failures]),
        )
    return ReleaseCheck(
        "course.tests",
        CheckStatus.PASS,
        "All ten lesson test suites passed independently.",
        tuple(passed),
    )


def _pytest_count_summary(output: str) -> str:
    for line in reversed(output.splitlines()):
        normalized = line.strip().strip("=").strip()
        if " in " not in normalized:
            continue
        counts = normalized.rsplit(" in ", 1)[0]
        if re.search(
            r"\b\d+ (?:passed|failed|skipped|xfailed|xpassed|errors?)\b", counts
        ):
            return counts
    return "summary-unavailable"


def _check_data_manifest(root: Path) -> tuple[ReleaseCheck, ReleaseCheck]:
    manifest_path = root / "data/upstream/manifest.json"
    failures: list[str] = []
    full_present: list[str] = []
    full_missing: list[str] = []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("record_type") != "upstream_manifest":
            raise ValueError("upstream manifest has the wrong record type")
        entrypoint = manifest["transformation"]["entrypoint"]
        if not (root / entrypoint).is_file():
            failures.append("transformation entrypoint is missing")
        for source in manifest["sources"]:
            name = source["name"]
            commit = source["commit"]
            if not isinstance(commit, str) or len(commit) != 40:
                failures.append(f"{name}: source commit is not full length")
            license_row = source["license"]
            license_path = root / "data/upstream" / license_row["path"]
            _check_declared_file(license_path, license_row, f"{name}:license", failures)
            source_doc = root / "data/upstream" / name / "SOURCE.md"
            if not source_doc.is_file() or commit not in source_doc.read_text(
                encoding="utf-8"
            ):
                failures.append(f"{name}: SOURCE.md does not bind the commit")
            for fixture in source.get("fixture_files", []):
                fixture_path = root / "data/upstream" / fixture["path"]
                _check_declared_file(
                    fixture_path, fixture, f"{name}:{fixture['role']}", failures
                )
            for asset in source.get("assets", []):
                asset_path = root / "data/upstream" / asset["destination"]
                label = f"{name}:{asset['name']}"
                if asset_path.is_file():
                    full_present.append(label)
                    _check_declared_file(asset_path, asset, label, failures)
                else:
                    full_missing.append(label)
    except (
        KeyError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as exc:
        failures.append(f"manifest:{type(exc).__name__}")
    provenance = ReleaseCheck(
        "data.provenance",
        CheckStatus.FAIL if failures else CheckStatus.PASS,
        "Dataset versions, licenses, committed fixtures, and declared checksums "
        + ("contain errors." if failures else "are internally consistent."),
        tuple(failures) if failures else ("STATE-Bench", "ABCD", "tau2"),
    )
    if full_missing:
        full = ReleaseCheck(
            "data.full_assets",
            CheckStatus.DEVIATION,
            "Full ignored upstream assets are optional in a fixture-only clone and were not all available for checksum verification.",
            tuple(
                [
                    *(f"verified:{item}" for item in full_present),
                    *(f"missing:{item}" for item in full_missing),
                ]
            ),
        )
    else:
        full = ReleaseCheck(
            "data.full_assets",
            CheckStatus.FAIL if failures else CheckStatus.PASS,
            "Every pinned full upstream asset present in this workspace matches its manifest.",
            tuple(full_present),
        )
    return provenance, full


def _check_declared_file(
    path: Path,
    declaration: Mapping[str, object],
    label: str,
    failures: list[str],
) -> None:
    if not path.is_file() or path.is_symlink():
        failures.append(f"{label}: file is missing")
        return
    expected_hash = declaration.get("sha256")
    expected_bytes = declaration.get("bytes")
    if not isinstance(expected_hash, str) or not _SHA256_PATTERN.fullmatch(
        expected_hash
    ):
        failures.append(f"{label}: declared SHA256 is invalid")
    elif _sha256(path) != expected_hash:
        failures.append(f"{label}: SHA256 mismatch")
    if not isinstance(expected_bytes, int) or path.stat().st_size != expected_bytes:
        failures.append(f"{label}: byte count mismatch")


def _check_full_data_regeneration(
    root: Path, bundle_paths: Sequence[Path]
) -> ReleaseCheck:
    if not bundle_paths:
        return ReleaseCheck(
            "data.full_regeneration",
            CheckStatus.DEVIATION,
            "No repeated full data bundles were supplied for byte comparison.",
            (
                "run scripts/prepare_data.py --profile full twice in separate temporary directories",
            ),
        )
    failures: list[str] = []
    bundle_hashes: dict[str, list[str]] = {
        relative: [] for relative in [*_FULL_DATA_OUTPUTS, "artifact-manifest.json"]
    }
    resolved_bundles: list[Path] = []
    try:
        reference = _load_json(
            root / "course/ch05-mine-benchmark-data/full-funnel-reference.json"
        )
        if not isinstance(reference, dict):
            raise TypeError("full reference must be an object")
        if (
            reference.get("schema_version") != "v1alpha1"
            or reference.get("record_type") != "lesson05_full_mining_reference"
            or reference.get("profile") != "full"
        ):
            failures.append("full reference identity is invalid")
        abcd_summary = reference.get("abcd")
        tau2_summary = reference.get("tau2")
        reference_funnel = reference.get("funnel")
        if not isinstance(abcd_summary, dict):
            raise TypeError("full reference ABCD summary must be an object")
        if not isinstance(tau2_summary, dict):
            raise TypeError("full reference tau2 summary must be an object")
        if not isinstance(reference_funnel, dict):
            raise TypeError("full reference funnel must be an object")
        if reference_funnel != _EXPECTED_FULL_FUNNEL:
            failures.append("full reference funnel facts drifted")
        for key, expected in _EXPECTED_ABCD_SUMMARY.items():
            if abcd_summary.get(key) != expected:
                failures.append(f"full reference ABCD fact drift:{key}")
        for key, expected in _EXPECTED_TAU2_SUMMARY.items():
            if tau2_summary.get(key) != expected:
                failures.append(f"full reference tau2 fact drift:{key}")
        pipeline = reference.get("pipeline")
        if not isinstance(pipeline, dict):
            raise TypeError("full reference pipeline must be an object")
        reference_rows = pipeline.get("outputs")
        if not isinstance(reference_rows, list):
            raise TypeError("full reference output inventory must be a list")
        reference_by_path = {
            row.get("path"): row
            for row in reference_rows
            if isinstance(row, dict) and isinstance(row.get("path"), str)
        }
        if set(reference_by_path) != _FULL_DATA_OUTPUTS or len(reference_rows) != len(
            _FULL_DATA_OUTPUTS
        ):
            failures.append("full reference has an incomplete output inventory")
        for relative, expected_records in _EXPECTED_FULL_OUTPUT_RECORDS.items():
            declaration = reference_by_path.get(relative)
            if (
                not isinstance(declaration, dict)
                or declaration.get("records") != expected_records
            ):
                failures.append(f"full reference record count drift:{relative}")
        for index, bundle_path in enumerate(bundle_paths, 1):
            bundle = bundle_path.resolve(strict=True)
            resolved_bundles.append(bundle)
            manifest_path = bundle / "artifact-manifest.json"
            manifest = _load_json(manifest_path)
            if not isinstance(manifest, dict):
                raise TypeError("full bundle manifest must be an object")
            if manifest.get("record_type") != "candidate_artifact_manifest":
                failures.append(f"run-{index}: artifact manifest record type")
            if manifest.get("profile") != "full":
                failures.append(f"run-{index}: artifact manifest is not full")
            if manifest.get("seed") != 0:
                failures.append(f"run-{index}: artifact manifest seed is not 0")
            if manifest.get("upstream_manifest_sha256") != _sha256(
                root / "data/upstream/manifest.json"
            ):
                failures.append(f"run-{index}: upstream manifest binding drift")
            if _load_json(bundle / "funnel-counts.json") != reference_funnel:
                failures.append(f"run-{index}: funnel summary drift")
            generated_rows = manifest.get("artifacts")
            if not isinstance(generated_rows, list):
                raise TypeError("full artifact inventory must be a list")
            generated_by_path: dict[str, Mapping[str, object]] = {}
            for row in generated_rows:
                if not isinstance(row, dict):
                    continue
                generated_path = row.get("path")
                if isinstance(generated_path, str):
                    generated_by_path[generated_path] = row
            if set(generated_by_path) != _FULL_DATA_OUTPUTS or len(
                generated_rows
            ) != len(_FULL_DATA_OUTPUTS):
                failures.append(f"run-{index}: incomplete output inventory")
            for relative, generated_declaration in sorted(generated_by_path.items()):
                if relative not in _FULL_DATA_OUTPUTS:
                    continue
                artifact_path = bundle / relative
                _check_declared_file(
                    artifact_path,
                    generated_declaration,
                    f"run-{index}:{relative}",
                    failures,
                )
                if artifact_path.is_file() and not artifact_path.is_symlink():
                    declared_records = generated_declaration.get("records")
                    actual_records = (
                        sum(
                            bool(line.strip())
                            for line in artifact_path.read_text(
                                encoding="utf-8"
                            ).splitlines()
                        )
                        if artifact_path.suffix == ".jsonl"
                        else 1
                    )
                    if declared_records != actual_records:
                        failures.append(f"run-{index}: record count drift:{relative}")
                if reference_by_path.get(relative) != generated_declaration:
                    failures.append(f"run-{index}: reference drift:{relative}")
                bundle_hashes.setdefault(relative, []).append(
                    _sha256(bundle / relative)
                )
            manifest_hash = _sha256(manifest_path)
            bundle_hashes["artifact-manifest.json"].append(manifest_hash)
            if pipeline.get("artifact_manifest_sha256") != manifest_hash:
                failures.append(f"run-{index}: reference drift:artifact-manifest.json")
        if len(set(resolved_bundles)) != len(resolved_bundles):
            failures.append("repeat runs must use distinct bundle directories")
        for relative, hashes in sorted(bundle_hashes.items()):
            if len(set(hashes)) > 1:
                failures.append(f"repeat drift:{relative}")
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as exc:
        failures.append(f"bundle:{type(exc).__name__}")
    if failures:
        return ReleaseCheck(
            "data.full_regeneration",
            CheckStatus.FAIL,
            "A fresh full data run does not byte-match its checked-in reference inventory.",
            tuple(failures),
        )
    if len(bundle_paths) < 2:
        return ReleaseCheck(
            "data.full_regeneration",
            CheckStatus.DEVIATION,
            "One full data bundle matches the reference, but repeatability was not tested.",
            ("provide --full-data-bundle twice with distinct fresh outputs",),
        )
    return ReleaseCheck(
        "data.full_regeneration",
        CheckStatus.PASS,
        "A fresh seed-0 full data run byte-matches all seven checked-in reference outputs.",
        (
            f"ABCD={_EXPECTED_ABCD_SUMMARY['exact_product_defect']}",
            f"ABCD_turns={_EXPECTED_ABCD_SUMMARY['original_turns']}/{_EXPECTED_ABCD_SUMMARY['delexed_turns']}",
            f"tau2_tasks={_EXPECTED_TAU2_SUMMARY['task_aggregates']}",
            f"tau2_runs={_EXPECTED_TAU2_SUMMARY['trajectory_runs']}",
            f"outputs={len(_FULL_DATA_OUTPUTS)}",
        ),
    )


def _holdout_archive(
    root: Path,
    protected: Path,
    explicit: Path | None,
) -> Path | None:
    if explicit is not None:
        return explicit
    candidates = (
        protected / "source.tar.gz",
        protected / "upstream/state_bench/source.tar.gz",
        root / "data/upstream/downloads/state_bench/source.tar.gz",
    )
    return next((path for path in candidates if path.is_file()), None)


def _check_holdout(
    root: Path,
    *,
    protected_holdout_root: Path | None = None,
    state_bench_archive: Path | None = None,
) -> ReleaseCheck:
    public = root / "data/testset/protected"
    try:
        public_summary = validate_public_holdout_bundle(public)
    except (OSError, UnicodeError, ValueError) as exc:
        return ReleaseCheck(
            "data.split_isolation",
            CheckStatus.FAIL,
            "Public holdout manifests, opaque locks, or commitments are invalid.",
            (type(exc).__name__, str(exc)[:300]),
        )
    public_details = (
        f"selection={public_summary.selection_count}",
        f"final={public_summary.final_count}",
        f"inventory_commitment_sha256={public_summary.inventory_sha256}",
        f"selection_manifest_sha256={public_summary.selection_manifest_sha256}",
        f"final_manifest_sha256={public_summary.final_manifest_sha256}",
    )
    if protected_holdout_root is None:
        return ReleaseCheck(
            "data.split_isolation",
            CheckStatus.DEVIATION,
            "Public holdout commitments are valid, but private four-way isolation was not revalidated.",
            (
                *public_details,
                "provide --protected-holdout-root with the external private bundle",
            ),
        )

    try:
        protected = protected_holdout_root.resolve(strict=True)
        if protected_holdout_root.is_symlink() or not protected.is_dir():
            raise ValueError("external protected holdout root must be a real directory")
        for name in (
            "selection-manifest.json",
            "final-manifest.json",
            "holdout-commitments.json",
        ):
            if (protected / name).read_bytes() != (public / name).read_bytes():
                raise ValueError(
                    "external protected bundle differs from the public commitment"
                )
        archive = _holdout_archive(root, protected, state_bench_archive)
        summary = validate_holdout_bundle(
            bundle_root=protected,
            creator_protected_manifest=public / "creator-manifest.json",
            creator_seed_manifest=root / "data/skill-v0/creator/seed-manifest.json",
            develop_manifest=root
            / "data/testset/ticket07/generated/develop-manifest.json",
            candidate_seeds=root / "data/testset/ticket07/candidate-seeds.jsonl",
            archive_path=archive,
        )
    except (OSError, UnicodeError, ValueError):
        return ReleaseCheck(
            "data.split_isolation",
            CheckStatus.FAIL,
            "Holdout checksum, privacy, provenance, or four-way isolation validation failed.",
            ("external_holdout_validation_failed",),
        )
    if archive is None:
        return ReleaseCheck(
            "data.split_isolation",
            CheckStatus.DEVIATION,
            "The external private holdout passed four-way isolation, but pinned source-tar reproduction was not rerun.",
            (
                *public_details,
                f"inventory_sha256={summary.inventory_sha256}",
                "provide --state-bench-archive with the pinned source tar",
            ),
        )
    return ReleaseCheck(
        "data.split_isolation",
        CheckStatus.PASS,
        "The external holdout proves four-way isolation and reproduces from the pinned STATE-Bench source tar.",
        (
            f"selection={summary.selection_count}",
            f"final={summary.final_count}",
            f"inventory_sha256={summary.inventory_sha256}",
            "archive_verified=true",
        ),
    )


@dataclass(frozen=True, slots=True)
class _ReleaseTextSnapshot:
    relative: str
    text: str | None


_INVALID_INDEX_PATH = "<git-index>"
_REGULAR_INDEX_MODES = frozenset({"100644", "100755"})
_GIT_OBJECT_ID_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")


def _invalid_index() -> tuple[tuple[str, bytes | None], ...]:
    return ((_INVALID_INDEX_PATH, None),)


def _git_metadata_present(root: Path) -> bool:
    metadata = root / ".git"
    return metadata.exists() or metadata.is_symlink()


def _batch_git_blobs(root: Path, object_ids: Sequence[str]) -> dict[str, bytes] | None:
    if not object_ids:
        return {}
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "cat-file", "--batch"],
            input=("\n".join(object_ids) + "\n").encode("ascii"),
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None

    stream = io.BytesIO(completed.stdout)
    blobs: dict[str, bytes] = {}
    for expected in object_ids:
        header = stream.readline()
        fields = header.rstrip(b"\n").split(b" ")
        if len(fields) != 3:
            return None
        try:
            observed = fields[0].decode("ascii")
            object_type = fields[1].decode("ascii")
            size = int(fields[2])
        except (UnicodeError, ValueError):
            return None
        if observed != expected or object_type != "blob" or size < 0:
            return None
        payload = stream.read(size)
        if len(payload) != size or stream.read(1) != b"\n":
            return None
        blobs[expected] = payload
    if stream.read(1):
        return None
    return blobs


def _git_index_file_bytes(
    root: Path,
) -> tuple[tuple[str, bytes | None], ...] | None:
    """Snapshot stage-0 index blobs without interpreting a path as a revision."""

    try:
        top_level_result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return _invalid_index() if _git_metadata_present(root) else None
    if top_level_result.returncode != 0:
        return _invalid_index() if _git_metadata_present(root) else None
    try:
        top_level = Path(top_level_result.stdout.decode("utf-8").strip())
        if top_level.resolve(strict=True) != root.resolve(strict=True):
            return None
        inventory_result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--stage", "-z"],
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return _invalid_index()
    if inventory_result.returncode != 0:
        return _invalid_index()

    entries: list[tuple[str, str, str]] = []
    for item in inventory_result.stdout.split(b"\0"):
        if not item:
            continue
        metadata, separator, encoded_path = item.partition(b"\t")
        fields = metadata.split(b" ")
        if not separator or len(fields) != 3:
            return _invalid_index()
        try:
            mode = fields[0].decode("ascii")
            object_id = fields[1].decode("ascii")
            stage = fields[2].decode("ascii")
            relative = encoded_path.decode("utf-8")
        except UnicodeError:
            return _invalid_index()
        relative_path = Path(relative)
        if (
            relative_path.is_absolute()
            or ".." in relative_path.parts
            or not relative_path.parts
            or not _GIT_OBJECT_ID_PATTERN.fullmatch(object_id)
        ):
            return _invalid_index()
        entries.append(
            (relative_path.as_posix(), mode, object_id if stage == "0" else "")
        )

    object_ids = tuple(
        dict.fromkeys(
            object_id
            for _, mode, object_id in entries
            if mode in _REGULAR_INDEX_MODES and object_id
        )
    )
    blobs = _batch_git_blobs(root, object_ids)
    if blobs is None:
        return _invalid_index()
    return tuple(
        (
            relative,
            blobs.get(object_id)
            if mode in _REGULAR_INDEX_MODES and object_id
            else None,
        )
        for relative, mode, object_id in entries
    )


def _iter_fallback_release_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        if any(
            part in _FALLBACK_SCAN_EXCLUDED_DIRS
            for part in path.relative_to(root).parts
        ):
            continue
        if path.is_file() or path.is_symlink():
            yield path


def _decode_release_text(payload: bytes | None) -> str | None:
    if payload is None:
        return None
    try:
        return payload.decode("utf-8")
    except UnicodeError:
        return None


def _iter_release_text_snapshots(root: Path) -> Iterable[_ReleaseTextSnapshot]:
    indexed = _git_index_file_bytes(root)
    if indexed is not None:
        for relative, index_payload in indexed:
            if relative in _ALLOWED_RELEASE_BINARY_PATHS:
                continue
            yield _ReleaseTextSnapshot(
                relative=relative,
                text=_decode_release_text(index_payload),
            )
            if relative == _INVALID_INDEX_PATH:
                continue
            worktree = root.joinpath(*Path(relative).parts)
            try:
                if worktree.is_symlink() or not worktree.is_file():
                    worktree_payload = None
                else:
                    worktree_payload = worktree.read_bytes()
            except OSError:
                worktree_payload = None
            if worktree_payload != index_payload:
                yield _ReleaseTextSnapshot(
                    relative=relative,
                    text=_decode_release_text(worktree_payload),
                )
        return

    for path in _iter_fallback_release_files(root):
        relative = path.relative_to(root).as_posix()
        if relative in _ALLOWED_RELEASE_BINARY_PATHS:
            continue
        yield _ReleaseTextSnapshot(relative=relative, text=_read_text(path))


def _read_text(path: Path) -> str | None:
    if path.is_symlink():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None


def _check_credentials(root: Path) -> ReleaseCheck:
    hits: set[str] = set()
    unreadable: set[str] = set()
    environment_values = credential_values(os.environ)
    for snapshot in _iter_release_text_snapshots(root):
        text = snapshot.text
        relative = snapshot.relative
        if text is None:
            unreadable.add(relative)
            continue
        if any(value and value in text for value in environment_values):
            hits.add(relative)
        scan_text = text
        if relative.startswith("tests/"):
            for fixture in _KNOWN_TEST_CREDENTIAL_FIXTURES:
                scan_text = scan_text.replace(fixture, "<known-test-fixture>")
        for pattern in _LITERAL_SECRET_PATTERNS:
            for match in pattern.finditer(scan_text):
                captured = match.group(1) if match.lastindex else match.group(0)
                normalized = captured.strip("\"'")
                if (
                    normalized == "..."
                    or normalized.startswith(("$", "<", "YOUR_", "example"))
                    or "os.environ" in normalized
                    or "getenv" in normalized
                ):
                    continue
                hits.add(relative)
    if hits or unreadable:
        return ReleaseCheck(
            "security.credentials",
            CheckStatus.FAIL,
            "Credential-like material appears, or a release text file cannot be scanned as UTF-8.",
            tuple(
                [
                    *(f"credential:{relative}" for relative in sorted(hits)),
                    *(f"unreadable:{relative}" for relative in sorted(unreadable)),
                ]
            ),
        )
    return ReleaseCheck(
        "security.credentials",
        CheckStatus.PASS,
        "No environment credential value or literal key pattern appears in release text files.",
    )


def _check_absolute_paths(root: Path) -> ReleaseCheck:
    hits: list[str] = []
    unreadable: list[str] = []
    for snapshot in _iter_release_text_snapshots(root):
        relative = snapshot.relative
        text = snapshot.text
        if text is None:
            unreadable.append(relative)
            continue
        for line_number, line in enumerate(text.splitlines(), 1):
            fixtures = _ALLOWED_ABSOLUTE_TEST_FIXTURES.get(relative, ())
            scan_line = line
            for fixture in fixtures:
                scan_line = scan_line.replace(fixture, "<known-test-fixture>")
            if any(pattern.search(scan_line) for pattern in _ABSOLUTE_PATH_PATTERNS):
                hits.append(f"{relative}:{line_number}")
    if hits or unreadable:
        return ReleaseCheck(
            "security.absolute_paths",
            CheckStatus.FAIL,
            "Local user-home paths appear, or a release text file cannot be scanned as UTF-8.",
            tuple(
                [
                    *(f"absolute:{hit}" for hit in hits),
                    *(f"unreadable:{relative}" for relative in unreadable),
                ]
            ),
        )
    return ReleaseCheck(
        "security.absolute_paths",
        CheckStatus.PASS,
        "No local user-home absolute path appears in release files.",
    )


def _private_gold_markers() -> tuple[str, ...]:
    record_type_key = "record_" + "type"
    scoring_key = "scor" + "ing"
    quotes = ('"', "'")
    markers = [
        f"{key_quote}{record_type_key}{key_quote}:{value_quote}{value}{value_quote}"
        for value in _PRIVATE_HOLDOUT_RECORD_TYPES
        for key_quote in quotes
        for value_quote in quotes
    ]
    markers.extend(
        f"{quote}{key}{quote}:" for key in _PRIVATE_HOLDOUT_KEYS for quote in quotes
    )
    markers.extend(
        f"{key_quote}{scoring_key}{key_quote}:"
        f"{value_quote}{_PRIVATE_HOLDOUT_SCORING}{value_quote}"
        for key_quote in quotes
        for value_quote in quotes
    )
    return tuple(markers)


def _compact_text(value: str) -> str:
    return "".join(value.split())


def _allowed_private_fixture_lines(relative: str) -> tuple[str, ...]:
    record_type_key = "record_" + "type"
    source_task_path, state_requirements, upstream_fixture_path, user_simulator = (
        _PRIVATE_HOLDOUT_KEYS
    )
    if relative == "src/ses/testset/holdout.py":
        return (
            *(
                f'"{record_type_key}": "{value}",'
                for value in _PRIVATE_HOLDOUT_RECORD_TYPES
            ),
            f'"{user_simulator}": _task_object(source.task, "{user_simulator}"),',
            f'"{state_requirements}": _task_sequence(source.task, "{state_requirements}"),',
            f'"{source_task_path}": source.task_path,',
            f'"{upstream_fixture_path}": source.fixture_path,',
            f'"scoring": "{_PRIVATE_HOLDOUT_SCORING}",',
        )
    if relative == "tests/testset/test_holdout.py":
        return (
            f'"{user_simulator}": {{"task_rules": ["Stay in scope."]}},',
            f'"{state_requirements}": [',
        )
    return ()


def _contains_private_gold_structure(relative: str, text: str) -> bool:
    remaining_allowances = [
        _compact_text(line) for line in _allowed_private_fixture_lines(relative)
    ]
    scan_lines: list[str] = []
    for line in text.splitlines():
        compact_line = _compact_text(line)
        if compact_line in remaining_allowances:
            remaining_allowances.remove(compact_line)
        else:
            scan_lines.append(line)
    compact = _compact_text("".join(scan_lines))
    return any(marker in compact for marker in _private_gold_markers())


def _check_public_holdout_leak(
    root: Path,
    *,
    protected_holdout_root: Path | None = None,
) -> ReleaseCheck:
    hits: set[str] = set()
    candidate_documents: dict[str, str] = {}
    candidate_paths: dict[str, str] = {}
    for index, snapshot in enumerate(_iter_release_text_snapshots(root)):
        relative = snapshot.relative
        text = snapshot.text
        if text is None:
            hits.add(f"unreadable:{relative}")
            continue
        candidate_key = f"tracked-release-document/{index:08d}"
        candidate_documents[candidate_key] = text
        candidate_paths[candidate_key] = relative
        if _contains_private_gold_structure(relative, text):
            hits.add(relative)

    private_scan_complete = False
    if protected_holdout_root is not None:
        try:
            external_scan = scan_external_holdout_leaks(
                bundle_root=protected_holdout_root,
                public_lock_root=root / "data/testset/protected",
                candidate_documents=candidate_documents,
            )
            if external_scan.status != "external_holdout_snapshot_verified":
                raise ValueError("unexpected external holdout scan status")
            hits.update(
                candidate_paths[candidate_key]
                for candidate_key in external_scan.matched_relative_paths
            )
            private_scan_complete = True
        except Exception:
            return ReleaseCheck(
                "security.public_holdout_leak",
                CheckStatus.FAIL,
                "The trusted external holdout leakage scan could not be completed.",
                ("external_holdout_leak_scan_failed",),
            )
    if hits:
        return ReleaseCheck(
            "security.public_holdout_leak",
            CheckStatus.FAIL,
            "A tracked release file exposes private final/selection material.",
            tuple(sorted(set(hits))),
        )
    if not private_scan_complete:
        return ReleaseCheck(
            "security.public_holdout_leak",
            CheckStatus.DEVIATION,
            "Tracked release files contain no private gold structures, but source-ID leakage needs the external inventory.",
            ("provide --protected-holdout-root for source-identity scanning",),
        )
    return ReleaseCheck(
        "security.public_holdout_leak",
        CheckStatus.PASS,
        "Tracked release files contain no private holdout source identities or gold structures.",
    )


class _StrictHtmlParser(HTMLParser):
    pass


def _check_reports(root: Path) -> ReleaseCheck:
    failures: list[str] = []
    details: list[str] = []
    for level, relative in _REQUIRED_REPORTS.items():
        path = root / relative
        if not path.is_file():
            failures.append(f"{level}: missing {relative.as_posix()}")
            continue
        payload = path.read_bytes()
        if len(payload) >= _REPORT_LIMIT_BYTES:
            failures.append(f"{level}: report is {len(payload)} bytes")
            continue
        try:
            text = payload.decode("utf-8")
            parser = _StrictHtmlParser()
            parser.feed(text)
            parser.close()
        except (UnicodeError, ValueError) as exc:
            failures.append(f"{level}: invalid HTML/UTF-8:{type(exc).__name__}")
            continue
        lowered = text.casefold()
        forbidden = ("http://", "https://", "<script src=", "<link rel=")
        if any(marker in lowered for marker in forbidden):
            failures.append(f"{level}: report contains an external resource marker")
        if "<html" not in lowered or "</html>" not in lowered:
            failures.append(f"{level}: report lacks a complete html root")
        if level in {"L2", "L3"} and 'type="application/json"' not in lowered:
            failures.append(f"{level}: report lacks embedded JSON")
        details.append(f"{level}:{relative.as_posix()}:{len(payload)}")
    if failures:
        return ReleaseCheck(
            "reports.self_contained",
            CheckStatus.FAIL,
            "L1, L2, or L3 is missing, externally linked, malformed, or over 2 MB.",
            tuple(failures),
        )
    return ReleaseCheck(
        "reports.self_contained",
        CheckStatus.PASS,
        "L1, L2, and L3 are self-contained UTF-8 HTML files under 2 MB.",
        tuple(details),
    )


def _check_cost_classification(root: Path, lessons: Mapping[int, Path]) -> ReleaseCheck:
    failures: list[str] = []
    for number, lesson in sorted(lessons.items()):
        text = (lesson / "README.md").read_text(encoding="utf-8")
        if not any(marker in text for marker in ("预算", "成本", "费用")):
            failures.append(f"lesson-{number:02d}: no cost or budget statement")
    release_report = root / "docs/release/release-report.md"
    release_text = (
        release_report.read_text(encoding="utf-8").casefold()
        if release_report.is_file()
        else ""
    )
    required = {
        "measured": ("measured", "实测"),
        "fixed": ("fixed", "固定"),
        "estimated": ("estimated", "估算", "预计"),
        "noncanonical": ("noncanonical", "非 canonical", "非canonical"),
    }
    for label, markers in required.items():
        if not any(marker.casefold() in release_text for marker in markers):
            failures.append(f"release report does not identify {label} cost")
    if failures:
        return ReleaseCheck(
            "cost.classification",
            CheckStatus.FAIL,
            "The release report does not fully distinguish measured, fixed, estimated, and noncanonical values.",
            tuple(failures),
        )
    return ReleaseCheck(
        "cost.classification",
        CheckStatus.PASS,
        "The release report distinguishes measured, fixed, estimated, and noncanonical costs, and every lesson gives a budget statement.",
    )


def _check_public_wording(root: Path, lessons: Mapping[int, Path]) -> ReleaseCheck:
    paths = [
        root / "README.md",
        *(lesson / "README.md" for lesson in lessons.values()),
        *sorted((root / "docs").rglob("*.md")),
    ]
    patterns = (
        re.compile(r"(?:来自|使用|基于).{0,12}(?:真实|实际).{0,8}生产(?:日志|数据)"),
        re.compile(r"\b(?:real|actual) production (?:logs?|data)\b", re.IGNORECASE),
        re.compile(r"\bactual customer logs?\b", re.IGNORECASE),
    )
    hits: list[str] = []
    for path in paths:
        if not path.is_file():
            continue
        negative_section = False
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                heading = stripped.lstrip("#").strip().casefold()
                negative_section = any(
                    marker in heading
                    for marker in ("out of scope", "non-goal", "非目标", "不做")
                )
            explicit_negation = any(
                marker in line.casefold()
                for marker in (
                    "不称",
                    "不能称",
                    "不包装",
                    "不使用",
                    "不把",
                    "不是",
                    "避免",
                    "禁止",
                    "不得",
                    "不能",
                    "not ",
                    "never ",
                )
            )
            if (
                not negative_section
                and not explicit_negation
                and any(pattern.search(line) for pattern in patterns)
            ):
                hits.append(f"{_relative(root, path)}:{line_number}")
    if hits:
        return ReleaseCheck(
            "docs.data_wording",
            CheckStatus.FAIL,
            "Public course documentation claims benchmark or role-play material is production data.",
            tuple(hits),
        )
    return ReleaseCheck(
        "docs.data_wording",
        CheckStatus.PASS,
        "Public course documentation does not claim benchmark or role-play material is production data.",
    )


def _check_historical_live_provenance(root: Path) -> ReleaseCheck:
    path = root / "docs/phase0-validation.md"
    release_report = root / "docs/release/release-report.md"
    try:
        text = path.read_text(encoding="utf-8")
        release_text = release_report.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return ReleaseCheck(
            "docs.live_provenance",
            CheckStatus.FAIL,
            "The historical live smoke record cannot be audited.",
            (type(exc).__name__,),
        )
    required_claims = {
        "dated historical scope": "历史 smoke 记录",
        "current release scope": "本轮 release",
        "current release not rerun": "本轮未复测",
        "not current canonical evidence": "不能把本页",
        "explicit release deviation": "live_not_rerun",
    }
    missing = [label for label, marker in required_claims.items() if marker not in text]
    if "## 当前结论" in text:
        missing.append("historical section is still labeled current")
    if not any(marker in release_text for marker in ("live_not_rerun", "本轮未复测")):
        missing.append("release report lacks the current live-not-rerun deviation")
    if missing:
        return ReleaseCheck(
            "docs.live_provenance",
            CheckStatus.FAIL,
            "Historical Provider smoke results are not clearly separated from the current release.",
            tuple(missing),
        )
    return ReleaseCheck(
        "docs.live_provenance",
        CheckStatus.PASS,
        "The 2026-08-16 Provider smoke table is labeled historical and not reused as current release evidence.",
    )


def _local_markdown_links(path: Path) -> Iterable[tuple[int, str]]:
    pattern = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
    text = path.read_text(encoding="utf-8")
    for line_number, line in enumerate(text.splitlines(), 1):
        for match in pattern.finditer(line):
            target = match.group(1).strip().split()[0].strip("<>")
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            yield line_number, target.split("#", 1)[0]


def _check_local_links(root: Path, lessons: Mapping[int, Path]) -> ReleaseCheck:
    failures: list[str] = []
    paths = [
        root / "README.md",
        *(lesson / "README.md" for lesson in lessons.values()),
        root / "docs/release/README.md",
        root / _REVIEW_PACKET,
    ]
    for path in paths:
        if not path.is_file():
            continue
        for line_number, target in _local_markdown_links(path):
            if target and not (path.parent / target).resolve(strict=False).exists():
                failures.append(f"{_relative(root, path)}:{line_number}:{target}")
    if failures:
        return ReleaseCheck(
            "docs.local_links",
            CheckStatus.FAIL,
            "A local documentation link target is missing.",
            tuple(failures),
        )
    return ReleaseCheck(
        "docs.local_links",
        CheckStatus.PASS,
        "All local links in the root, lesson, and release documentation resolve.",
    )


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


_REVIEW_SCAN_EXCLUDED_PARTS = frozenset({"scripts", "solution", "starter", "tests"})
_DIRECT_REVIEW_KEYS = frozenset(
    {"current_review", "human_reviewed", "reviewed_at", "reviewer"}
)
_REVIEW_DECISION_KEYS = frozenset(
    {
        "approval_status",
        "human_review",
        "human_review_status",
        "review_decision",
        "review_status",
    }
)
_UNSIGNED_REVIEW_DECISIONS = frozenset({"approved", "human_reviewed", "rejected"})


def _iter_checked_review_artifacts(root: Path) -> Iterable[Path]:
    """Yield checked data/course artifacts, excluding executable test fixtures."""

    bases = (
        root / "data/skill-v0",
        root / "data/testset",
        root / "course",
    )
    for base in bases:
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if (
                not path.is_file()
                or path.is_symlink()
                or path.suffix.casefold() not in {".html", ".json", ".jsonl"}
            ):
                continue
            relative_parts = path.relative_to(base).parts
            if base.name == "course" and any(
                part in _REVIEW_SCAN_EXCLUDED_PARTS for part in relative_parts
            ):
                continue
            if any(part.startswith(".") for part in relative_parts):
                continue
            yield path


def _has_unsigned_review_decision(
    value: object,
    *,
    review_context: bool = False,
) -> bool:
    """Detect approval claims without treating Registry outcomes as human review."""

    if isinstance(value, list):
        return any(
            _has_unsigned_review_decision(item, review_context=review_context)
            for item in value
        )
    if not isinstance(value, dict):
        return False

    normalized_keys = {str(key).casefold(): item for key, item in value.items()}
    local_review_context = review_context or any(
        "review" in key or "approval" in key for key in normalized_keys
    )
    for key, item in normalized_keys.items():
        if key in _DIRECT_REVIEW_KEYS:
            return True
        if isinstance(item, str):
            normalized_value = item.casefold()
            if normalized_value in _UNSIGNED_REVIEW_DECISIONS and (
                key in _REVIEW_DECISION_KEYS
                or (key in {"decision", "outcome", "status"} and local_review_context)
            ):
                return True
        if _has_unsigned_review_decision(
            item,
            review_context=local_review_context or "review" in key,
        ):
            return True
    return False


def _artifact_has_invalid_review_claim(path: Path, text: str) -> bool:
    casefolded = text.casefold()
    if "ticket-owner" in casefolded or "delegated to codex" in casefolded:
        return True
    if re.search(
        r'"(?:current_review|human_reviewed|reviewed_at|reviewer)"\s*:',
        text,
        flags=re.IGNORECASE,
    ):
        return True
    if re.search(r'[:[,]\s*"human_reviewed"\s*[,}\]]', text, re.IGNORECASE):
        return True
    if path.suffix.casefold() == ".html":
        return bool(
            re.search(
                r'"(?:approval_status|human_review(?:_status)?|review_decision|review_status)"\s*:\s*"(?:approved|rejected)"',
                text,
                flags=re.IGNORECASE,
            )
        )

    try:
        if path.suffix.casefold() == ".jsonl":
            records = [json.loads(line) for line in text.splitlines() if line.strip()]
            review_context = "review" in path.name.casefold()
            return any(
                _has_unsigned_review_decision(
                    record,
                    review_context=review_context,
                )
                for record in records
            )
        return _has_unsigned_review_decision(
            json.loads(text),
            review_context="review" in path.name.casefold(),
        )
    except json.JSONDecodeError:
        return True


def _legacy_develop_review_claims(root: Path) -> tuple[str, ...]:
    """Return every checked artifact with a legacy or unsigned review claim."""

    hits: list[str] = []
    for path in _iter_checked_review_artifacts(root):
        text = _read_text(path)
        if text is None:
            hits.append(f"unreadable:{_relative(root, path)}")
            continue
        if _artifact_has_invalid_review_claim(path, text):
            hits.append(_relative(root, path))
    return tuple(sorted(set(hits)))


def _check_review_packet(root: Path) -> tuple[ReleaseCheck, ReleaseCheck]:
    packet = root / _REVIEW_PACKET
    coverage_failures: list[str] = []
    pending: list[str] = []
    calibration: dict[str, object] | None = None
    creator_ids: list[str] = []
    develop_ids: list[str] = []
    try:
        text = packet.read_text(encoding="utf-8")
        calibration_value = _load_json(
            root / "course/ch03-calibrate-judges/agreement-experiment.json"
        )
        creator = _load_json(root / "data/skill-v0/creator/seed-manifest.json")
        develop = _load_json(
            root / "data/testset/ticket07/generated/develop-manifest.json"
        )
        if not isinstance(calibration_value, dict):
            raise TypeError("calibration artifact must be an object")
        if not isinstance(creator, dict):
            raise TypeError("creator manifest must be an object")
        if not isinstance(develop, dict):
            raise TypeError("develop manifest must be an object")
        calibration = calibration_value
        calibration_rows = calibration.get("measurements")
        creator_rows = creator.get("records")
        develop_rows = develop.get("cases")
        if not isinstance(calibration_rows, list):
            raise TypeError("calibration measurements must be a list")
        if not isinstance(creator_rows, list):
            raise TypeError("creator records must be a list")
        if not isinstance(develop_rows, list):
            raise TypeError("develop cases must be a list")
        calibration_ids = sorted(
            {
                str(row["case_id"])
                for row in calibration_rows
                if isinstance(row, dict) and "case_id" in row
            }
        )
        creator_ids = [
            str(row["seed_id"])
            for row in creator_rows
            if isinstance(row, dict) and "seed_id" in row
        ]
        develop_ids = [
            str(row["case_id"])
            for row in develop_rows
            if isinstance(row, dict) and "case_id" in row
        ]
        expected_counts = {
            "Lesson 3 calibration": (
                len(calibration_ids),
                len(set(calibration_ids)),
                4,
            ),
            "creator": (len(creator_ids), len(set(creator_ids)), 9),
            "develop": (len(develop_ids), len(set(develop_ids)), 15),
        }
        for label, (count, unique_count, expected) in expected_counts.items():
            if count != expected or unique_count != expected:
                coverage_failures.append(
                    f"{label} inventory is {count}/{unique_count}, expected {expected} unique"
                )
        for identifier in [*calibration_ids, *creator_ids, *develop_ids]:
            if identifier not in text:
                coverage_failures.append(f"packet lacks {identifier}")
        for number in range(1, 13):
            if f"PRD-{number:02d}" not in text:
                coverage_failures.append(f"packet lacks PRD-{number:02d}")
        if "[x]" in text.casefold():
            coverage_failures.append("packet contains a pre-checked decision")
    except (
        KeyError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        TypeError,
    ) as exc:
        coverage_failures.append(f"packet:{type(exc).__name__}")

    legacy_develop_reviews = _legacy_develop_review_claims(root)
    if legacy_develop_reviews:
        pending.extend(
            f"invalid legacy or unsigned review claim:{path}"
            for path in legacy_develop_reviews
        )
    pending.extend(
        f"{identifier} awaits direct reviewer confirmation"
        for identifier in creator_ids
    )
    pending.extend(
        f"{identifier} awaits direct reviewer confirmation"
        for identifier in develop_ids
    )
    if (
        calibration is not None
        and calibration.get("label_review_status")
        == "course_authored_pending_human_review"
    ):
        pending.append("Lesson 3 labels await direct reviewer confirmation")
    review_packet = ReleaseCheck(
        "manual.packet_coverage",
        CheckStatus.FAIL if coverage_failures else CheckStatus.PASS,
        "The consolidated unsigned review packet "
        + (
            "is incomplete."
            if coverage_failures
            else "covers Lesson 3, nine creator seeds, fifteen develop cases, and all twelve PRD launch items."
        ),
        tuple(coverage_failures),
    )
    if legacy_develop_reviews:
        manual_state = ReleaseCheck(
            "manual.review_state",
            CheckStatus.FAIL,
            "A checked artifact makes a legacy, delegated, or unsigned direct-review claim.",
            tuple(pending),
        )
    else:
        manual_state = ReleaseCheck(
            "manual.review_state",
            CheckStatus.DEVIATION if pending else CheckStatus.PASS,
            "Required direct review remains pending."
            if pending
            else "All required direct review evidence is present.",
            tuple(pending),
        )
    return review_packet, manual_state


def _check_prd_release_items() -> ReleaseCheck:
    return ReleaseCheck(
        "prd.prelaunch_checklist",
        CheckStatus.DEVIATION,
        "The twelve PRD prelaunch items require the consolidated packet and live/human evidence; automated checks alone cannot close them.",
        tuple(f"PRD-{number:02d}:pending packet decision" for number in range(1, 13)),
    )


def _repository_head(root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    )
    revision = completed.stdout.strip()
    if re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise ValueError("repository HEAD is not a full commit")
    return revision


def validate_release(
    root: Path,
    *,
    run_course_tests: bool = False,
    command_evidence: Path | None = None,
    full_data_bundles: Sequence[Path] = (),
    protected_holdout_root: Path | None = None,
    state_bench_archive: Path | None = None,
) -> ReleaseReport:
    """Run deterministic checks without changing repository state."""

    repository = root.resolve(strict=True)
    if not (repository / "pyproject.toml").is_file():
        raise ValueError("release root does not contain pyproject.toml")
    lessons = _lesson_dirs(repository)
    commands = _documented_commands(repository, lessons)
    expected_command_commit = (
        _repository_head(repository) if command_evidence is not None else "0" * 40
    )
    data_checks = _check_data_manifest(repository)
    review_checks = _check_review_packet(repository)
    checks = (
        _check_course_structure(repository, lessons),
        _check_transitions(repository, lessons),
        _check_command_syntax(repository, commands),
        _check_command_evidence(
            commands,
            command_evidence,
            expected_repository_commit=expected_command_commit,
        ),
        _run_course_tests(repository, lessons, enabled=run_course_tests),
        *data_checks,
        _check_full_data_regeneration(repository, full_data_bundles),
        _check_holdout(
            repository,
            protected_holdout_root=protected_holdout_root,
            state_bench_archive=state_bench_archive,
        ),
        _check_credentials(repository),
        _check_absolute_paths(repository),
        _check_public_holdout_leak(
            repository,
            protected_holdout_root=protected_holdout_root,
        ),
        _check_reports(repository),
        _check_cost_classification(repository, lessons),
        _check_public_wording(repository, lessons),
        _check_historical_live_provenance(repository),
        _check_local_links(repository, lessons),
        *review_checks,
        _check_prd_release_items(),
    )
    return ReleaseReport(
        checks=checks,
        lesson_count=len(lessons),
        documented_command_count=len(commands),
    )


__all__ = [
    "CheckStatus",
    "DocumentedCommand",
    "ReleaseCheck",
    "ReleaseReport",
    "validate_release",
]
