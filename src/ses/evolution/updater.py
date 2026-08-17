"""Generate a small evidence-linked Patch from reviewed Failure Cards."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ses.contracts import (
    AddPatchOperation,
    CompletedPayload,
    DeletePatchOperation,
    EngineExitStatus,
    EngineRequest,
    ErrorPayload,
    EvidenceRef,
    FailureCard,
    FailureCategory,
    MeasurementKind,
    Patch,
    PatchOperation,
    RecordType,
    SchemaVersion,
    TextDeltaPayload,
    ToolCallPayload,
    UpdatePatchOperation,
    Usage,
    UsagePayload,
)
from ses.engines.claude_code import ClaudeCodeEngine
from ses.evolution.patches import (
    EMPTY_CONTENT_SHA256,
    file_content_sha256,
    validate_target,
)
from ses.foundation.config import LockedModel
from ses.foundation.credentials import ProviderCredentials
from ses.foundation.workspace import CaseWorkspace

UPDATER_SKILL_SPEC = """# Lesson 8 Updater contract

Propose one small patch to the accepted product-return Skill. Use at most three
operations. Each operation must be add, update, or delete; target only SKILL.md
or references/*.md; and name every supporting Failure Card. Do not change evals,
cases, gold, Judges, tools, or policy. Keep each operation within twelve changed
lines. Prefer the narrowest instruction that addresses the cited observations.
For a delete operation, return content as an empty string.
"""


class UpdaterError(ValueError):
    """The Updater could not produce a valid small proposal."""


class _ProposedOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    operation: Literal["add", "update", "delete"]
    target: str = Field(min_length=1)
    content: str
    failure_card_ids: tuple[str, ...] = Field(min_length=1)
    reason: str = Field(min_length=1)
    risk: str = Field(min_length=1)

    @field_validator("failure_card_ids", mode="before")
    @classmethod
    def _json_ids_to_tuple(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _content_matches_operation(self) -> _ProposedOperation:
        if self.operation == "delete" and self.content:
            raise ValueError("delete proposal content must be empty")
        if self.operation != "delete" and not self.content:
            raise ValueError("add and update proposals require content")
        if len(self.failure_card_ids) != len(set(self.failure_card_ids)):
            raise ValueError("proposal Failure Card IDs must be unique")
        return self


class _UpdaterProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    operations: tuple[_ProposedOperation, ...] = Field(min_length=1, max_length=3)

    @field_validator("operations", mode="before")
    @classmethod
    def _json_operations_to_tuple(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


@dataclass(frozen=True, slots=True)
class UpdaterRequest:
    """The complete allowlisted input visible to one Updater invocation."""

    workspace: Path
    visible_files: tuple[str, ...]
    cards: tuple[FailureCard, ...]
    parent_files: Mapping[str, str]
    parent_skill_sha256: str


class Updater(Protocol):
    measurement_kind: MeasurementKind
    usage: Usage
    latency_ms: int

    def propose(self, request: UpdaterRequest) -> Patch: ...


def _unique_evidence(
    cards: tuple[FailureCard, ...],
    attribute: Literal["trace_evidence", "assertion_evidence"],
) -> tuple[EvidenceRef, ...]:
    values: list[EvidenceRef] = []
    seen: set[tuple[str, str, str, str]] = set()
    for card in cards:
        for reference in getattr(card, attribute):
            key = (
                reference.artifact.root.value,
                reference.artifact.path,
                reference.artifact.sha256,
                reference.json_pointer,
            )
            if key not in seen:
                seen.add(key)
                values.append(reference)
    return tuple(values)


def build_patch(proposal: _UpdaterProposal, request: UpdaterRequest) -> Patch:
    """Bind an untrusted proposal to trusted hashes and reviewed evidence."""
    card_by_id = {card.failure_id: card for card in request.cards}
    operations: list[PatchOperation] = []
    for proposed in proposal.operations:
        validate_target(proposed.target)
        try:
            cards = tuple(card_by_id[item] for item in proposed.failure_card_ids)
        except KeyError as exc:
            raise UpdaterError("proposal references an unknown Failure Card") from exc
        exists = proposed.target in request.parent_files
        if proposed.operation == "add" and exists:
            raise UpdaterError("add proposal targets an existing file")
        if proposed.operation in {"update", "delete"} and not exists:
            raise UpdaterError("update or delete proposal targets a missing file")
        precondition = (
            file_content_sha256(request.parent_files[proposed.target])
            if exists
            else EMPTY_CONTENT_SHA256
        )
        trace_evidence = _unique_evidence(cards, "trace_evidence")
        assertion_evidence = _unique_evidence(cards, "assertion_evidence")
        if proposed.operation == "add":
            operations.append(
                AddPatchOperation(
                    operation="add",
                    target=proposed.target,
                    precondition_sha256=precondition,
                    content=proposed.content,
                    trace_evidence=trace_evidence,
                    assertion_evidence=assertion_evidence,
                    reason=proposed.reason,
                    risk=proposed.risk,
                    failure_card_ids=proposed.failure_card_ids,
                )
            )
        elif proposed.operation == "update":
            operations.append(
                UpdatePatchOperation(
                    operation="update",
                    target=proposed.target,
                    precondition_sha256=precondition,
                    content=proposed.content,
                    trace_evidence=trace_evidence,
                    assertion_evidence=assertion_evidence,
                    reason=proposed.reason,
                    risk=proposed.risk,
                    failure_card_ids=proposed.failure_card_ids,
                )
            )
        else:
            operations.append(
                DeletePatchOperation(
                    operation="delete",
                    target=proposed.target,
                    precondition_sha256=precondition,
                    trace_evidence=trace_evidence,
                    assertion_evidence=assertion_evidence,
                    reason=proposed.reason,
                    risk=proposed.risk,
                    failure_card_ids=proposed.failure_card_ids,
                )
            )
    operation_hash = hashlib.sha256(
        json.dumps(
            {
                "parent": request.parent_skill_sha256,
                "operations": [
                    operation.model_dump(mode="json") for operation in operations
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return Patch(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="skill_patch",
        patch_id=f"patch-{operation_hash[:16]}",
        parent_skill_sha256=request.parent_skill_sha256,
        operations=tuple(operations),
    )


class FakeUpdater:
    """Produce the fixed Lesson 8 patch without network or credentials."""

    def __init__(self) -> None:
        self.measurement_kind = MeasurementKind.SYNTHETIC_OFFLINE
        self.usage = Usage(input_tokens=0, output_tokens=0)
        self.latency_ms = 0
        self.last_request: UpdaterRequest | None = None

    def propose(self, request: UpdaterRequest) -> Patch:
        self.last_request = request
        by_category = {card.category: card for card in request.cards}
        required = {
            FailureCategory.PATTERN,
            FailureCategory.OVERLOAD,
            FailureCategory.TIMING,
            FailureCategory.SAFETY,
        }
        if not required <= set(by_category):
            raise UpdaterError("fixed Updater requires the Lesson 8 category fixture")
        proposal = _UpdaterProposal(
            operations=(
                _ProposedOperation(
                    operation="add",
                    target="references/safety-notes.md",
                    content=(
                        "# Safety notes\n\n"
                        "- Treat policy and tool results as the source of truth.\n"
                        "- Ask for consent before a confirming state change.\n"
                        "- Never expose internal identifiers or credentials.\n"
                    ),
                    failure_card_ids=(by_category[FailureCategory.SAFETY].failure_id,),
                    reason="Add the smallest safety reminder supported by the safety card.",
                    risk="The extra reminder may increase prompt length.",
                ),
                _ProposedOperation(
                    operation="update",
                    target="SKILL.md",
                    content=(
                        "---\n"
                        "name: resolve-product-returns\n"
                        "description: Use for product return requests that require policy checks and safe state changes.\n"
                        "allowed-tools: mcp__shop__get_order, mcp__shop__get_policies, mcp__shop__process_return\n"
                        "---\n\n"
                        "# Resolve product returns\n\n"
                        "1. Identify the requested item and state the request in customer-facing language before acting.\n"
                        "2. Inspect the order with `get_order`; select only that item.\n"
                        "3. Read the current rules with `get_policies`; do not guess eligibility, fees, or timing.\n"
                        "4. Call `process_return` in preview mode. Explain the tool-produced result and ask for consent.\n"
                        "5. Confirm with the same item and reason only after the customer approves the preview.\n"
                        "6. Verify the returned terminal state. Report only actions and amounts supported by tool evidence.\n\n"
                        "Do not expose internal terminology, invent a fixed answer, or change unrelated items.\n"
                    ),
                    failure_card_ids=(
                        by_category[FailureCategory.PATTERN].failure_id,
                        by_category[FailureCategory.TIMING].failure_id,
                    ),
                    reason="Clarify item matching before tool use and preserve action order.",
                    risk="The added wording could change trigger or timing behavior.",
                ),
                _ProposedOperation(
                    operation="delete",
                    target="references/return-workflow.md",
                    content="",
                    failure_card_ids=(
                        by_category[FailureCategory.OVERLOAD].failure_id,
                    ),
                    reason="Remove the redundant workflow after folding its guardrail into SKILL.md.",
                    risk="A downstream installer may expect the old reference path.",
                ),
            )
        )
        return build_patch(proposal, request)


class ClaudeCodeUpdater:
    """Use the locked ClaudeCLI model to propose operations, then bind them locally."""

    def __init__(
        self,
        *,
        model: LockedModel,
        credentials: ProviderCredentials,
        executable: str,
        environ: Mapping[str, str],
        timeout_seconds: float = 180,
    ) -> None:
        self.measurement_kind = MeasurementKind.LIVE_MEASURED
        self._model = model
        self._credentials = credentials
        self._executable = executable
        self._environ = environ
        self._timeout_seconds = timeout_seconds
        self.usage = Usage(input_tokens=0, output_tokens=0)
        self.latency_ms = 0
        self.last_request: UpdaterRequest | None = None

    def propose(self, request: UpdaterRequest) -> Patch:
        self.last_request = request
        workspace = CaseWorkspace(
            root=request.workspace,
            claude_config_dir=request.workspace.parent / "claude-config",
            cleanup_root=request.workspace.parent,
        )
        engine = ClaudeCodeEngine(
            model=self._model,
            credentials=self._credentials,
            workspace=workspace,
            executable=self._executable,
            environ=self._environ,
            system_prompt=(
                "You are an isolated Skill Updater. Use only the supplied Failure "
                "Cards, Skill files, and Updater contract. Return the requested JSON. "
                "Never infer hidden eval data or propose changes outside the Skill."
            ),
            output_json_schema=_UpdaterProposal.model_json_schema(),
        )
        prompt = json.dumps(
            {
                "contract": UPDATER_SKILL_SPEC,
                "failure_cards": [
                    card.model_dump(mode="json") for card in request.cards
                ],
                "parent_files": dict(request.parent_files),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        started = monotonic()
        proposal = asyncio.run(self._invoke(engine, prompt))
        self.latency_ms = round((monotonic() - started) * 1000)
        return build_patch(proposal, request)

    async def _invoke(self, engine: ClaudeCodeEngine, prompt: str) -> _UpdaterProposal:
        request = EngineRequest(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type=RecordType.ENGINE_REQUEST,
            request_id="lesson-08-live-updater",
            prompt=prompt,
            allowed_tools=(),
            timeout_seconds=self._timeout_seconds,
        )
        text: list[str] = []
        terminal: EngineExitStatus | None = None
        failed = False
        error_codes: list[str] = []
        error_details: list[str] = []
        usage_seen = False
        async for event in engine.stream(request):
            payload = event.payload
            if isinstance(payload, TextDeltaPayload):
                text.append(payload.text)
            elif isinstance(payload, UsagePayload):
                self.usage = payload.usage
                usage_seen = True
            elif isinstance(payload, ToolCallPayload):
                raise UpdaterError("live Updater attempted an unauthorized tool")
            elif isinstance(payload, ErrorPayload):
                failed = True
                error_codes.append(payload.error_code)
                error_details.append(payload.message)
            elif isinstance(payload, CompletedPayload):
                terminal = payload.exit_status
        if failed or terminal is not EngineExitStatus.SUCCESS or not usage_seen:
            terminal_value = "missing" if terminal is None else terminal.value
            codes = ",".join(sorted(set(error_codes))) or "none"
            details = _safe_engine_details(error_details)
            raise UpdaterError(
                "live Updater did not complete with measured usage "
                f"(terminal={terminal_value}, "
                f"usage_seen={str(usage_seen).lower()}, error_codes={codes}, "
                f"details={details})"
            )
        try:
            return _UpdaterProposal.model_validate_json("".join(text))
        except ValueError as exc:
            raise UpdaterError("live Updater returned an invalid proposal") from exc


def _safe_engine_details(messages: list[str]) -> str:
    """Keep short redacted diagnostics without exposing URLs or local paths."""
    if not messages:
        return "none"
    value = " | ".join(" ".join(message.split()) for message in messages)
    value = re.sub(r"https?://\S+", "<url>", value)
    value = re.sub(r"(?<!\w)/(?:[^\s,;:]+/?)+", "<path>", value)
    return value[:300]
