"""Zero-cost static safety gate for candidate Skill artifacts."""

from __future__ import annotations

import json
import re
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ses.skills.applicability import parse_skill_front_matter
from ses.skills.installer import (
    SkillInstallError,
    load_skill_manifest,
    normalized_skill_sha256,
)

SUPPORTED_TOOLS = frozenset({"get_order", "get_policies", "process_return"})
_NATIVE_FRONTMATTER = frozenset(
    {
        "name",
        "description",
        "when_to_use",
        "argument-hint",
        "arguments",
        "disable-model-invocation",
        "user-invocable",
        "allowed-tools",
        "disallowed-tools",
        "model",
        "effort",
        "context",
        "agent",
        "background",
        "hooks",
        "paths",
        "shell",
        "metadata",
        "license",
        "compatibility",
    }
)
_IDENTIFIER_PATTERNS = (
    re.compile(r"\bORD-[A-Z0-9-]+\b", re.IGNORECASE),
    re.compile(r"\bCUST(?:OMER)?-[A-Z0-9-]*\d[A-Z0-9-]*\b", re.IGNORECASE),
    re.compile(
        r"\b(?:creator-seed|develop-return|selection-case|final-case)-[A-Z0-9-]+\b",
        re.IGNORECASE,
    ),
)
_FIXED_ANSWER = re.compile(
    r"(?:\b(?:always|exactly)\s+(?:refund|return)\b.{0,30}(?:[$€£¥]\s*\d+|\b\d+(?:\.\d+)?\s*(?:usd|cny|dollars?)\b)|"
    r"(?:[$€£¥]\s*\d+|\b\d+(?:\.\d+)?\s*(?:usd|cny|dollars?)\b).{0,30}\b(?:always|exactly)\b)",
    re.IGNORECASE,
)
_EVAL_CONTENT = re.compile(
    r"(?:^|[/\\])(?:evals?|gold|traces?|selection|final)(?:[/\\]|\b)|"
    r"\b(?:hidden gold|reference answer)\b",
    re.IGNORECASE,
)
_DANGEROUS = re.compile(
    r"\b(?:disable|bypass|ignore)\s+(?:all\s+)?(?:safeguards?|permissions?|security|policy)|"
    r"\b(?:run|execute)\s+(?:arbitrary\s+)?(?:shell|terminal|bash)\b|"
    r"\b(?:reveal|print|exfiltrate)\s+(?:credentials?|secrets?|api keys?)\b",
    re.IGNORECASE,
)


class StaticGateStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"


class StaticCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    check_id: str
    passed: bool
    detail: str


class StaticGateReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = "v1alpha1"
    record_type: str = "skill_static_gate_report"
    status: StaticGateStatus
    skill_sha256: str | None
    checks: tuple[StaticCheck, ...]


def _check(check_id: str, passed: bool, success: str, failure: str) -> StaticCheck:
    return StaticCheck(
        check_id=check_id,
        passed=passed,
        detail=success if passed else failure,
    )


def _actual_files(source: Path) -> set[str]:
    return {
        path.relative_to(source).as_posix()
        for path in source.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def run_static_gate(
    source: Path,
    *,
    audit_path: Path | None = None,
    max_characters: int = 12_000,
) -> StaticGateReport:
    """Inspect every gate condition without installing or calling a model."""

    try:
        content = (source / "SKILL.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        content = ""
    metadata = parse_skill_front_matter(content)
    required = {"name", "description", "allowed-tools"}
    metadata_ok = (
        metadata is not None
        and required.issubset(metadata)
        and all(metadata[key].strip() for key in required)
    )
    raw_tools = metadata.get("allowed-tools", "") if metadata is not None else ""
    if raw_tools.startswith("[") and raw_tools.endswith("]"):
        raw_tools = raw_tools[1:-1]
    tools = tuple(part.strip() for part in raw_tools.split(",") if part.strip())
    tools_ok = (
        bool(tools) and len(tools) == len(set(tools)) and set(tools) <= SUPPORTED_TOOLS
    )
    frontmatter_ok = metadata is not None and set(metadata) <= _NATIVE_FRONTMATTER

    declared: set[str] = set()
    manifest_ok = False
    skill_hash: str | None = None
    try:
        manifest = load_skill_manifest(source)
        declared = {item.path for item in manifest.files}
        manifest_ok = True
        skill_hash = normalized_skill_sha256(source)
    except (OSError, SkillInstallError, ValueError):
        try:
            raw = json.loads((source / "skill-manifest.json").read_text("utf-8"))
            declared = {
                row["path"]
                for row in raw.get("files", [])
                if isinstance(row, dict) and isinstance(row.get("path"), str)
            }
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
            declared = set()
    actual = _actual_files(source)
    inventory_ok = actual == declared | {"skill-manifest.json"}
    identifier_ok = not any(pattern.search(content) for pattern in _IDENTIFIER_PATTERNS)
    fixed_ok = _FIXED_ANSWER.search(content) is None
    eval_ok = _EVAL_CONTENT.search(content) is None
    dangerous_ok = _DANGEROUS.search(content) is None
    length_ok = 0 < len(content) <= max_characters
    checks = (
        _check(
            "required_metadata",
            metadata_ok,
            "required metadata is present",
            "required metadata is missing or invalid",
        ),
        _check(
            "native_frontmatter",
            frontmatter_ok,
            "frontmatter uses Claude Code native fields",
            "frontmatter contains an unsupported Claude Code field",
        ),
        _check(
            "manifest_integrity",
            manifest_ok,
            "manifest and file hashes are valid",
            "manifest or declared file hash is invalid",
        ),
        _check(
            "file_inventory",
            inventory_ok,
            "only manifest-declared runtime files exist",
            "artifact contains undeclared or illegal files",
        ),
        _check(
            "supported_tools",
            tools_ok,
            "all declared tools are supported",
            "tool list is empty, duplicated, unknown, or unsupported",
        ),
        _check(
            "forbidden_identifiers",
            identifier_ok,
            "no order, customer, or case identifiers found",
            "candidate contains a forbidden order, customer, or case identifier",
        ),
        _check(
            "fixed_answers",
            fixed_ok,
            "no case-specific fixed answer found",
            "candidate contains a case-specific fixed monetary answer",
        ),
        _check(
            "eval_content",
            eval_ok,
            "no evaluation or hidden-answer content found",
            "candidate refers to evaluation, trace, gold, selection, or final content",
        ),
        _check(
            "dangerous_instructions",
            dangerous_ok,
            "no dangerous instruction found",
            "candidate contains dangerous or permission-bypassing instructions",
        ),
        _check(
            "content_length",
            length_ok,
            "content length is within the configured limit",
            "content is empty or exceeds the configured limit",
        ),
    )
    report = StaticGateReport(
        status=(
            StaticGateStatus.PASS
            if all(item.passed for item in checks)
            else StaticGateStatus.FAIL
        ),
        skill_sha256=skill_hash,
        checks=checks,
    )
    if audit_path is not None:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(
            json.dumps(report.model_dump(mode="json"), sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
    return report
