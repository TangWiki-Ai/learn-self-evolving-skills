"""Zero-cost static safety gate for candidate Skill artifacts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ses.contracts import SkillArtifactManifest
from ses.skills.installer import (
    SkillInstallError,
    load_skill_manifest,
    normalized_skill_sha256,
)

SUPPORTED_TOOLS = frozenset(
    {
        "mcp__shop__get_order",
        "mcp__shop__get_policies",
        "mcp__shop__process_return",
    }
)
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


@dataclass(frozen=True, slots=True)
class StaticGatePolicy:
    """Domain policy injected into the shared zero-cost Static Gate."""

    supported_tools: frozenset[str] = SUPPORTED_TOOLS
    identifier_patterns: tuple[re.Pattern[str], ...] = _IDENTIFIER_PATTERNS
    fixed_answer_pattern: re.Pattern[str] = _FIXED_ANSWER
    eval_content_pattern: re.Pattern[str] = _EVAL_CONTENT
    dangerous_pattern: re.Pattern[str] = _DANGEROUS
    description_pattern: re.Pattern[str] | None = None
    forbidden_content_patterns: tuple[re.Pattern[str], ...] = ()


DEFAULT_STATIC_GATE_POLICY = StaticGatePolicy()


def _parse_skill_front_matter(content: str) -> dict[str, str] | None:
    match = re.match(r"\A---\n(?P<header>.*?)\n---\n", content, flags=re.DOTALL)
    if match is None:
        return None
    metadata: dict[str, str] = {}
    for line in match.group("header").splitlines():
        if ":" not in line:
            return None
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip()
    return metadata


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
    policy: StaticGatePolicy = DEFAULT_STATIC_GATE_POLICY,
) -> StaticGateReport:
    """Inspect every gate condition without installing or calling a model."""

    try:
        skill_content = (source / "SKILL.md").read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        skill_content = ""
    metadata = _parse_skill_front_matter(skill_content)
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
        bool(tools)
        and len(tools) == len(set(tools))
        and set(tools) <= policy.supported_tools
    )
    frontmatter_ok = metadata is not None and set(metadata) <= _NATIVE_FRONTMATTER

    declared: set[str] = set()
    manifest_ok = False
    skill_hash: str | None = None
    installable_content = skill_content
    manifest: SkillArtifactManifest | None = None
    try:
        manifest = load_skill_manifest(source)
        declared = {item.path for item in manifest.files}
        installable_content = "\n".join(
            (source / item.path).read_text(encoding="utf-8") for item in manifest.files
        )
        manifest_ok = True
        skill_hash = normalized_skill_sha256(source)
    except (OSError, UnicodeError, SkillInstallError, ValueError):
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
    identifier_ok = not any(
        pattern.search(installable_content) for pattern in policy.identifier_patterns
    )
    fixed_ok = policy.fixed_answer_pattern.search(installable_content) is None
    eval_ok = policy.eval_content_pattern.search(installable_content) is None
    dangerous_ok = policy.dangerous_pattern.search(installable_content) is None
    length_ok = 0 < len(installable_content) <= max_characters
    checks: tuple[StaticCheck, ...] = (
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
    if policy.description_pattern is not None:
        description = metadata.get("description", "") if metadata is not None else ""
        checks += (
            _check(
                "domain_description",
                policy.description_pattern.search(description) is not None,
                "description matches the configured domain",
                "description does not match the configured domain",
            ),
        )
    if policy.forbidden_content_patterns:
        domain_content_ok = not any(
            pattern.search(installable_content)
            for pattern in policy.forbidden_content_patterns
        )
        checks += (
            _check(
                "domain_forbidden_content",
                domain_content_ok,
                "no domain-specific leakage or action bypass found",
                "candidate contains domain leakage or an action bypass",
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
