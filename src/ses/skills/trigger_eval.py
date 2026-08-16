"""Measure native Skill discovery on a fixed positive/negative prompt set."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from ses.contracts import (
    CompletedPayload,
    EngineExitStatus,
    EngineRequest,
    ErrorPayload,
    RecordType,
    SchemaVersion,
    ToolCallPayload,
    Usage,
    UsagePayload,
)
from ses.engines.claude_code import ClaudeCodeEngine
from ses.foundation.config import LockedModel
from ses.foundation.credentials import ProviderCredentials
from ses.foundation.workspace import WorkspaceFactory
from ses.skills.installer import load_skill_manifest


class DiscoveryStatus(StrEnum):
    TRIGGERED = "triggered"
    NOT_TRIGGERED = "not_triggered"
    INDETERMINATE = "indeterminate"


class TriggerPrompt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    prompt_id: str
    prompt: str
    expected_trigger: bool


TRIGGER_PROMPTS = tuple(
    TriggerPrompt(prompt_id=f"positive-{index:02d}", prompt=text, expected_trigger=True)
    for index, text in enumerate(
        (
            "I need to return a defective laptop from my recent order.",
            "Can you help me send back shoes that arrived in the wrong size?",
            "Please process a return for an unopened item I bought last week.",
            "The screen flickers and I want to return the product.",
            "I received the wrong color and would like to send it back.",
            "Help me check the policy and return one item from an order.",
            "I want a return, but please preview any fee before confirming.",
            "Can you verify whether my damaged item can be returned?",
            "Start a policy-compliant product return after I approve the preview.",
            "Please inspect my purchase and complete the eligible return safely.",
        ),
        1,
    )
) + tuple(
    TriggerPrompt(
        prompt_id=f"negative-{index:02d}", prompt=text, expected_trigger=False
    )
    for index, text in enumerate(
        (
            "What is the shipping status of my package?",
            "Please update the email address on my account.",
            "Can you recommend a laptop for photo editing?",
            "I forgot my password and cannot sign in.",
            "Where can I find the store opening hours?",
            "Please explain how loyalty points work.",
            "I want to buy two more units of this product.",
            "Can you cancel a newsletter subscription?",
            "What colors are available for this shirt?",
            "Please summarize this meeting transcript.",
        ),
        1,
    )
)


class DiscoveryObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: DiscoveryStatus
    evidence: str = Field(min_length=1)


class DiscoveryBackend(Protocol):
    """Observe the product's native discovery result for one prompt."""

    def observe(self, prompt: str) -> DiscoveryObservation: ...


class TriggerPromptResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    prompt_id: str
    prompt: str
    expected_trigger: bool
    actual: DiscoveryStatus
    evidence: str


class TriggerEvalResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: str = "v1alpha1"
    record_type: str = "trigger_eval_result"
    skill_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    engine_version: str = Field(min_length=1)
    tp: int = Field(ge=0)
    fp: int = Field(ge=0)
    tn: int = Field(ge=0)
    fn: int = Field(ge=0)
    precision: float = Field(ge=0, le=1)
    recall: float = Field(ge=0, le=1)
    indeterminate_count: int = Field(ge=0)
    prompts: tuple[TriggerPromptResult, ...]


class FixedNativeDiscovery:
    """Deterministic product-shaped discovery fixture for offline CI."""

    def __init__(self, expected: dict[str, bool] | None = None) -> None:
        self._expected = expected or {
            item.prompt: item.expected_trigger for item in TRIGGER_PROMPTS
        }

    def observe(self, prompt: str) -> DiscoveryObservation:
        if prompt not in self._expected:
            return DiscoveryObservation(
                status=DiscoveryStatus.INDETERMINATE,
                evidence="fixed native discovery fixture has no observation",
            )
        triggered = self._expected[prompt]
        return DiscoveryObservation(
            status=(
                DiscoveryStatus.TRIGGERED
                if triggered
                else DiscoveryStatus.NOT_TRIGGERED
            ),
            evidence="fixed Claude Code native discovery observation",
        )


class ClaudeNativeDiscovery:
    """Observe Claude Code's native Skill tool call in a clean per-prompt workspace."""

    def __init__(
        self,
        *,
        skill_source: Path,
        model: LockedModel,
        credentials: ProviderCredentials,
        executable: str,
        environ: Mapping[str, str],
        workspace_root: Path,
        timeout_seconds: float = 120,
    ) -> None:
        manifest = load_skill_manifest(skill_source)
        self._skill_files = tuple(
            (
                skill_source / PurePosixPath(item.path),
                f"resolve-product-returns/{item.path}",
            )
            for item in manifest.files
        )
        self._model = model
        self._credentials = credentials
        self._executable = executable
        self._environ = environ
        self._workspace_root = workspace_root
        self._timeout_seconds = timeout_seconds
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost_amount = 0.0
        self.latency_ms = 0

    def observe(self, prompt: str) -> DiscoveryObservation:
        return self.observe_all((prompt,))[0]

    def observe_all(
        self, prompts: Sequence[str], *, max_concurrency: int = 4
    ) -> tuple[DiscoveryObservation, ...]:
        """Observe prompts concurrently while keeping one clean workspace per prompt."""
        if max_concurrency < 1:
            raise ValueError("native discovery concurrency must be positive")
        return asyncio.run(self._observe_all(tuple(prompts), max_concurrency))

    async def _observe_all(
        self, prompts: tuple[str, ...], max_concurrency: int
    ) -> tuple[DiscoveryObservation, ...]:
        semaphore = asyncio.Semaphore(max_concurrency)

        async def bounded(prompt: str) -> DiscoveryObservation:
            async with semaphore:
                return await self._observe_one(prompt)

        return tuple(await asyncio.gather(*(bounded(prompt) for prompt in prompts)))

    async def _observe_one(self, prompt: str) -> DiscoveryObservation:
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:16]
        workspace = WorkspaceFactory(self._workspace_root).create(
            run_id="ticket08-trigger-live",
            case_id=prompt_hash,
            iteration_id="native",
            skill_files=self._skill_files,
        )
        try:
            engine = ClaudeCodeEngine(
                model=self._model,
                credentials=self._credentials,
                workspace=workspace,
                executable=self._executable,
                environ=self._environ,
                system_prompt=(
                    "Handle the user's request naturally. Use the installed Skill "
                    "through Claude Code's native Skill mechanism only when it applies."
                ),
                native_skill_discovery=True,
            )
            return await self._observe(engine, prompt, prompt_hash)
        finally:
            if workspace.cleanup_root is not None and workspace.cleanup_root.exists():
                shutil.rmtree(workspace.cleanup_root)

    async def _observe(
        self, engine: ClaudeCodeEngine, prompt: str, prompt_hash: str
    ) -> DiscoveryObservation:
        request = EngineRequest(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type=RecordType.ENGINE_REQUEST,
            request_id=f"trigger-live-{prompt_hash}",
            prompt=prompt,
            allowed_tools=("Skill(resolve-product-returns)",),
            timeout_seconds=self._timeout_seconds,
        )
        triggered = False
        error_codes: list[str] = []
        terminal: EngineExitStatus | None = None
        usage = Usage(input_tokens=0, output_tokens=0)
        async for event in engine.stream(request):
            payload = event.payload
            if (
                isinstance(payload, ToolCallPayload)
                and payload.tool_name.casefold() == "skill"
            ):
                triggered = True
            elif isinstance(payload, UsagePayload):
                usage = payload.usage
            elif isinstance(payload, ErrorPayload):
                error_codes.append(payload.error_code)
            elif isinstance(payload, CompletedPayload):
                terminal = payload.exit_status
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cost_amount += float(usage.cost_amount or 0)
        if error_codes or terminal is not EngineExitStatus.SUCCESS:
            diagnostic = ",".join(sorted(set(error_codes))) or "missing_success"
            return DiscoveryObservation(
                status=DiscoveryStatus.INDETERMINATE,
                evidence=(
                    "Claude Code native discovery did not complete successfully "
                    f"({diagnostic})"
                ),
            )
        return DiscoveryObservation(
            status=(
                DiscoveryStatus.TRIGGERED
                if triggered
                else DiscoveryStatus.NOT_TRIGGERED
            ),
            evidence=(
                "Claude Code emitted a native Skill tool call"
                if triggered
                else "Claude Code completed without a native Skill tool call"
            ),
        )


def evaluate_triggers(
    *,
    skill_sha256: str,
    engine_version: str,
    discovery: DiscoveryBackend,
    prompts: tuple[TriggerPrompt, ...] = TRIGGER_PROMPTS,
) -> TriggerEvalResult:
    """Evaluate native observations; indeterminate rows remain outside the matrix."""

    if len(prompts) != 20 or sum(item.expected_trigger for item in prompts) != 10:
        raise ValueError(
            "trigger evaluation requires exactly 10 positive and 10 negative prompts"
        )
    rows: list[TriggerPromptResult] = []
    tp = fp = tn = fn = uncertain = 0
    observe_all = getattr(discovery, "observe_all", None)
    if callable(observe_all):
        observations = tuple(observe_all(tuple(item.prompt for item in prompts)))
    else:
        observations = tuple(discovery.observe(item.prompt) for item in prompts)
    if len(observations) != len(prompts):
        raise ValueError("native discovery returned the wrong observation count")
    for item, observation in zip(prompts, observations, strict=True):
        rows.append(
            TriggerPromptResult(
                prompt_id=item.prompt_id,
                prompt=item.prompt,
                expected_trigger=item.expected_trigger,
                actual=observation.status,
                evidence=observation.evidence,
            )
        )
        if observation.status is DiscoveryStatus.INDETERMINATE:
            uncertain += 1
        elif item.expected_trigger and observation.status is DiscoveryStatus.TRIGGERED:
            tp += 1
        elif item.expected_trigger:
            fn += 1
        elif observation.status is DiscoveryStatus.TRIGGERED:
            fp += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return TriggerEvalResult(
        skill_sha256=skill_sha256,
        engine_version=engine_version,
        tp=tp,
        fp=fp,
        tn=tn,
        fn=fn,
        precision=precision,
        recall=recall,
        indeterminate_count=uncertain,
        prompts=tuple(rows),
    )
