"""Create Skill v0 from the isolated, audited creator seed pack."""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from ses.contracts import (
    CompletedPayload,
    EngineExitStatus,
    EngineRequest,
    ErrorPayload,
    RecordType,
    SchemaVersion,
    TextDeltaPayload,
    ToolCallPayload,
    Usage,
    UsagePayload,
)
from ses.engines.claude_code import ClaudeCodeEngine
from ses.foundation.config import LockedModel
from ses.foundation.credentials import ProviderCredentials
from ses.foundation.workspace import CaseWorkspace, WorkspaceFactory
from ses.skills.creator import SkillCandidate
from ses.skills.installer import normalized_skill_sha256, write_skill_manifest

CREATOR_SAFE_TOOLS: tuple[str, ...] = ()
_SKILL_SPEC = """# Skill v0 output contract

Write a generic Claude Code Skill for product returns. Include YAML front matter
with name, description, version, and allowed-tools. Declare only the exact native
tool names mcp__shop__get_order, mcp__shop__get_policies, and
mcp__shop__process_return. Explain inspect, preview, confirm, and verify.
Never copy identifiers, monetary answers, eval data, gold, traces, or credentials.
"""


@dataclass(frozen=True, slots=True)
class V0CreatorRequest:
    workspace: Path
    visible_files: tuple[str, ...]
    seed_files: tuple[Path, ...]
    allowed_tools: tuple[str, ...]
    source_version: str


class FakeV0Creator:
    """Deterministic Creator used by default tests, CLI, and course artifacts."""

    def __init__(self) -> None:
        self.last_request: V0CreatorRequest | None = None

    def create(self, request: V0CreatorRequest, output_dir: Path) -> SkillCandidate:
        self.last_request = request
        if output_dir.exists():
            raise ValueError("v0 output directory already exists")
        output_dir.mkdir(parents=True)
        (output_dir / "references").mkdir()
        (output_dir / "SKILL.md").write_text(
            """---
name: resolve-product-returns
description: Use for product return requests that require policy checks and safe state changes.
allowed-tools: mcp__shop__get_order, mcp__shop__get_policies, mcp__shop__process_return
---

# Resolve product returns

1. Inspect the order with `get_order`. Identify only the item the customer wants to return.
2. Read the current rules with `get_policies`; do not guess eligibility, fees, or timing.
3. Call `process_return` in preview mode. Explain the tool-produced result and ask for consent.
4. Confirm with the same item and reason only after the customer approves the preview.
5. Verify the returned terminal state. Report only actions and amounts supported by tool evidence.

Do not expose internal terminology, invent a fixed answer, or change unrelated items.
""",
            encoding="utf-8",
        )
        (output_dir / "references" / "return-workflow.md").write_text(
            """# Return workflow

- Inspect the current order and select the requested item.
- Read policy facts before promising an outcome.
- Preview before confirm; preserve arguments between both calls.
- Verify the terminal state and ground the customer message in tool evidence.
""",
            encoding="utf-8",
        )
        write_skill_manifest(
            output_dir,
            name="resolve-product-returns",
            version="v0",
            files=("SKILL.md", "references/return-workflow.md"),
            source_version=request.source_version,
            provider_compatibility=("claude-code-native",),
        )
        return SkillCandidate(
            source=output_dir,
            version="v0",
            sha256=normalized_skill_sha256(output_dir),
        )


class V0Creator(Protocol):
    def create(self, request: V0CreatorRequest, output_dir: Path) -> SkillCandidate: ...


class V0SeedPack(Protocol):
    """The narrow projection-only input seam shared by Creator domains."""

    @property
    def projections(self) -> tuple[Path, ...]: ...

    @property
    def source_version(self) -> str: ...


class _LiveCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    skill_md: str = Field(min_length=200, max_length=10_000)
    reference_md: str = Field(min_length=80, max_length=4_000)


class LiveV0Creator:
    """Generate v0 through the locked ClaudeCLI Creator role with no tools."""

    def __init__(
        self,
        *,
        model: LockedModel,
        credentials: ProviderCredentials,
        executable: str,
        environ: Mapping[str, str],
        timeout_seconds: float = 120,
    ) -> None:
        self._model = model
        self._credentials = credentials
        self._executable = executable
        self._environ = environ
        self._timeout_seconds = timeout_seconds
        self.last_request: V0CreatorRequest | None = None
        self.usage = Usage(input_tokens=0, output_tokens=0)
        self.latency_ms = 0

    def create(self, request: V0CreatorRequest, output_dir: Path) -> SkillCandidate:
        self.last_request = request
        workspace = CaseWorkspace(
            root=request.workspace,
            claude_config_dir=request.workspace.parent / "claude-config",
            cleanup_root=request.workspace.parent,
        )
        schema = _LiveCandidate.model_json_schema()
        engine = ClaudeCodeEngine(
            model=self._model,
            credentials=self._credentials,
            workspace=workspace,
            executable=self._executable,
            environ=self._environ,
            system_prompt=(
                "You are an isolated Skill Creator. Use only the supplied projection "
                "JSON and specification. Return the requested JSON. Do not use tools "
                "or outside knowledge."
            ),
            output_json_schema=schema,
        )
        inputs = {
            path.name: json.loads(path.read_text(encoding="utf-8"))
            for path in request.seed_files
        }
        prompt = (
            (request.workspace / "skill-spec.md").read_text(encoding="utf-8")
            + "\nReturn JSON fields skill_md and reference_md. skill_md must contain "
            "the exact metadata name resolve-product-returns and allowed-tools "
            "mcp__shop__get_order, mcp__shop__get_policies, "
            "mcp__shop__process_return. Do not put version in "
            "SKILL.md frontmatter; the artifact manifest owns versioning.\n"
            + json.dumps(inputs, ensure_ascii=False, sort_keys=True)
        )
        started = monotonic()
        candidate = asyncio.run(self._invoke(engine, prompt))
        self.latency_ms = round((monotonic() - started) * 1000)
        if output_dir.exists():
            raise ValueError("v0 output directory already exists")
        output_dir.mkdir(parents=True)
        (output_dir / "references").mkdir()
        skill_md = (
            "\n".join(
                line
                for line in candidate.skill_md.splitlines()
                if not line.casefold().startswith("version:")
            ).rstrip()
            + "\n"
        )
        (output_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
        (output_dir / "references" / "return-workflow.md").write_text(
            candidate.reference_md, encoding="utf-8"
        )
        write_skill_manifest(
            output_dir,
            name="resolve-product-returns",
            version="v0",
            files=("SKILL.md", "references/return-workflow.md"),
            source_version=request.source_version,
            provider_compatibility=("claude-code-native",),
        )
        return SkillCandidate(
            source=output_dir,
            version="v0",
            sha256=normalized_skill_sha256(output_dir),
        )

    async def _invoke(self, engine: ClaudeCodeEngine, prompt: str) -> _LiveCandidate:
        request = EngineRequest(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type=RecordType.ENGINE_REQUEST,
            request_id="skill-v0-live-creator",
            prompt=prompt,
            allowed_tools=(),
            timeout_seconds=self._timeout_seconds,
        )
        text: list[str] = []
        terminal: EngineExitStatus | None = None
        failed = False
        error_codes: list[str] = []
        usage_seen = False
        async for event in engine.stream(request):
            payload = event.payload
            if isinstance(payload, TextDeltaPayload):
                text.append(payload.text)
            elif isinstance(payload, UsagePayload):
                self.usage = payload.usage
                usage_seen = True
            elif isinstance(payload, ToolCallPayload):
                raise ValueError("live Creator attempted an unauthorized tool")
            elif isinstance(payload, ErrorPayload):
                failed = True
                error_codes.append(payload.error_code)
            elif isinstance(payload, CompletedPayload):
                terminal = payload.exit_status
        if failed or terminal is not EngineExitStatus.SUCCESS or not usage_seen:
            terminal_value = "missing" if terminal is None else terminal.value
            codes = ",".join(sorted(set(error_codes))) or "none"
            raise ValueError(
                "live Creator did not complete with measured usage "
                f"(terminal={terminal_value}, usage_seen={str(usage_seen).lower()}, "
                f"error_codes={codes})"
            )
        return _LiveCandidate.model_validate_json("".join(text))


def create_skill_v0(
    *,
    seed_pack: V0SeedPack,
    output_dir: Path,
    creator: V0Creator,
    workspace_root: Path,
    skill_spec: str = _SKILL_SPEC,
) -> SkillCandidate:
    """Expose only projections and the Skill spec, then materialize one candidate."""

    if not seed_pack.projections or len(set(seed_pack.projections)) != len(
        seed_pack.projections
    ):
        raise ValueError("Creator projections must be nonempty and unique")
    spec = workspace_root / "inputs" / "skill-spec.md"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text(skill_spec, encoding="utf-8")
    files = [(spec, "skill-spec.md")]
    files.extend(
        (path, f"seeds/seed-{index:03d}.json")
        for index, path in enumerate(seed_pack.projections, 1)
    )
    workspace = WorkspaceFactory(workspace_root).create(
        run_id="skill-v0",
        case_id="creator",
        iteration_id="v0",
        files=files,
    )
    visible = tuple(
        sorted(
            path.relative_to(workspace.root).as_posix()
            for path in workspace.root.rglob("*")
            if path.is_file()
        )
    )
    request = V0CreatorRequest(
        workspace=workspace.root,
        visible_files=visible,
        seed_files=tuple(
            workspace.root / f"seeds/seed-{index:03d}.json"
            for index in range(1, len(seed_pack.projections) + 1)
        ),
        allowed_tools=CREATOR_SAFE_TOOLS,
        source_version=seed_pack.source_version,
    )
    try:
        return creator.create(request, output_dir)
    finally:
        if workspace.cleanup_root is not None and workspace.cleanup_root.exists():
            shutil.rmtree(workspace.cleanup_root)
