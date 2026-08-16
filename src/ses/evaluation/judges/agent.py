"""Evidence-planning Agent Judge in a dedicated zero-capability workspace."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Literal

from pydantic import JsonValue, model_validator

from ses.contracts import ArtifactRef, ContractModel, GradeStatus, JudgeKind
from ses.engines.base import Engine
from ses.engines.fake import FakeEngine
from ses.evaluation.evidence_extractor import EvidenceBundle, evidence_json_bytes
from ses.foundation.config import LockedModel
from ses.foundation.credentials import ProviderCredentials
from ses.foundation.workspace import CaseWorkspace, WorkspaceFactory

from .llm import (
    BoundJudgeEngine,
    JudgeModelConfig,
    ModelJudgeRun,
    Rubric,
    _artifact_matches,
    _assertion,
    _collect_engine_output,
    _error_run,
    _metadata,
    _request,
    _validated_refs,
)

AGENT_PROMPT_VERSION = "evidence-agent-prompt-v2"
AGENT_MODEL_PROTOCOL_VERSION = "agent-evidence-plan-json-v2"
_AGENT_MODEL_PROTOCOL = (
    "ses.evaluation.agent-judge/agent-evidence-plan-json-v2|"
    "inspect-sections-before-conclusion|completed_checks:section[]|"
    "completed-checks:matching-evidence-reference-required-for-pass-fail|"
    "assertion_id:string|status:pass,fail,not_evaluated|"
    "reason:string|evidence_references:allowed-evidence-json-pointer[]|"
    "zero-tools:dedicated-workspace:strict-json:no-retry"
)
AGENT_MODEL_PROTOCOL_SHA256 = hashlib.sha256(
    _AGENT_MODEL_PROTOCOL.encode("utf-8")
).hexdigest()
EvidenceSection = Literal[
    "state_diff_facts",
    "tool_timeline",
    "amount_reconciliation",
    "key_messages",
]


class AgentDecision(ContractModel):
    """Agent-specific wire result with an auditable evidence inspection plan."""

    assertion_id: str
    status: GradeStatus
    reason: str
    completed_checks: tuple[EvidenceSection, ...]
    evidence_references: tuple[str, ...]

    @model_validator(mode="after")
    def _validate_agent_reasoning_shape(self) -> AgentDecision:
        if self.status is GradeStatus.ERROR:
            raise ValueError("models cannot declare judge error")
        if not self.reason.strip():
            raise ValueError("reason must not be blank")
        if not self.completed_checks:
            raise ValueError("agent must report at least one completed evidence check")
        if len(set(self.completed_checks)) != len(self.completed_checks):
            raise ValueError("completed evidence checks must be unique")
        if len(set(self.evidence_references)) != len(self.evidence_references):
            raise ValueError("evidence references must be unique")
        if self.status in {GradeStatus.PASS, GradeStatus.FAIL}:
            if not self.evidence_references:
                raise ValueError("pass and fail require evidence references")
            checked = set(self.completed_checks)
            if not checked - {"key_messages"}:
                raise ValueError(
                    "agent pass and fail require a deterministic evidence check"
                )
            referenced = {
                section
                for section in checked
                if any(
                    pointer == f"/{section}" or pointer.startswith(f"/{section}/")
                    for pointer in self.evidence_references
                )
            }
            missing = checked - referenced
            if missing:
                raise ValueError(
                    "completed evidence checks require matching references: "
                    + ", ".join(sorted(missing))
                )
        return self


class AgentJudgeEngine:
    """A source-bound Judge Engine minted with a fresh zero-capability workspace."""

    __slots__ = ("_binding",)
    _binding: BoundJudgeEngine

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("use AgentJudgeEngine.from_fake or .production")

    @classmethod
    def _create(cls, binding: BoundJudgeEngine) -> AgentJudgeEngine:
        if type(binding) is not BoundJudgeEngine or binding.workspace is None:
            raise TypeError("Agent Judge requires a workspace-bound Judge Engine")
        instance = object.__new__(cls)
        instance._binding = binding
        return instance

    @classmethod
    def from_fake(
        cls,
        engine: FakeEngine,
        *,
        workspace_factory: WorkspaceFactory | None = None,
    ) -> AgentJudgeEngine:
        """Create the fixed-response protocol in the same empty workspace shape."""

        factory = workspace_factory or WorkspaceFactory()
        workspace = factory.create(
            run_id="offline-judge",
            case_id="fixed-response",
            iteration_id="0",
        )
        return cls._create(
            BoundJudgeEngine._from_fake_in_workspace(engine, workspace=workspace)
        )

    @classmethod
    def production(
        cls,
        *,
        model: LockedModel,
        credentials: ProviderCredentials,
        workspace_factory: WorkspaceFactory | None = None,
        executable: str = "claude",
        environ: Mapping[str, str] | None = None,
    ) -> AgentJudgeEngine:
        """Build Claude Code internally so a case engine can never be supplied."""

        binding = BoundJudgeEngine.production(
            model=model,
            credentials=credentials,
            workspace_factory=workspace_factory,
            executable=executable,
            environ=environ,
            system_prompt=(
                "You are an evidence judge. You have no tools. Read only the "
                "evidence in the user prompt and return the requested JSON."
            ),
            run_id="agent-judge",
            case_id="read-only-evidence",
            iteration_id="0",
        )
        return cls._create(binding)

    @property
    def engine(self) -> Engine:
        return self._binding.engine

    @property
    def model(self) -> JudgeModelConfig:
        return self._binding.model

    @property
    def workspace(self) -> CaseWorkspace:
        workspace = self._binding.workspace
        assert workspace is not None
        return workspace

    def close(self) -> None:
        """Remove only the factory-created temporary boundary."""

        self._binding.close()


def _render_agent_prompt(*, rubric: Rubric, evidence: EvidenceBundle) -> str:
    rubric_json = json.dumps(
        rubric.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    evidence_json = evidence_json_bytes(evidence).decode("utf-8")
    return (
        f"Protocol: {AGENT_PROMPT_VERSION}. Work as a read-only evidence analyst. "
        "First inspect the relevant deterministic sections: state_diff_facts, "
        "tool_timeline, amount_reconciliation, and key_messages. Check explicit "
        "amount relations; never assume fees, discounts, prices, and refunds must "
        "all be equal. Then return exactly one JSON object with assertion_id, "
        "status, reason, completed_checks, and evidence_references. A pass or fail "
        "must include a deterministic check in addition to any message reading. "
        "You have no tools and may use only the supplied evidence.\n"
        f"RUBRIC={rubric_json}\nEVIDENCE={evidence_json}"
    )


async def judge_agent(
    engine: AgentJudgeEngine,
    *,
    rubric: Rubric,
    evidence: EvidenceBundle,
    evidence_artifact: ArtifactRef,
    timeout_seconds: float = 30,
) -> ModelJudgeRun:
    """Run the evidence-planning protocol in a dedicated no-MCP workspace."""

    if type(engine) is not AgentJudgeEngine:
        raise TypeError("Agent Judge requires a dedicated AgentJudgeEngine")
    prompt = _render_agent_prompt(rubric=rubric, evidence=evidence)
    metadata = _metadata(
        judge=JudgeKind.AGENT,
        prompt_version=AGENT_PROMPT_VERSION,
        prompt=prompt,
        rubric=rubric,
        evidence=evidence,
        model=engine.model,
        model_protocol_version=AGENT_MODEL_PROTOCOL_VERSION,
        model_protocol_sha256=AGENT_MODEL_PROTOCOL_SHA256,
    )
    request = _request(
        judge=JudgeKind.AGENT,
        prompt=prompt,
        metadata=metadata,
        timeout_seconds=timeout_seconds,
    )
    try:
        if engine.workspace.mcp_config is not None:
            return _error_run(
                rubric=rubric,
                judge=JudgeKind.AGENT,
                metadata=metadata,
                request=request,
                reason="Agent Judge workspace contains MCP configuration",
            )
        if not _artifact_matches(evidence, evidence_artifact):
            return _error_run(
                rubric=rubric,
                judge=JudgeKind.AGENT,
                metadata=metadata,
                request=request,
                reason="evidence artifact checksum does not match the supplied evidence",
            )
        output = await _collect_engine_output(engine.engine, request)
        if output.error is not None:
            return _error_run(
                rubric=rubric,
                judge=JudgeKind.AGENT,
                metadata=metadata,
                request=request,
                reason=output.error,
                raw_response=output.text,
            )
        try:
            raw: JsonValue = json.loads(output.text)
            decision = AgentDecision.model_validate(raw)
            if decision.assertion_id != rubric.assertion_id:
                raise ValueError("model output assertion_id does not match the rubric")
            refs = _validated_refs(
                evidence=evidence,
                evidence_artifact=evidence_artifact,
                pointers=decision.evidence_references,
            )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            reason = (
                str(exc)
                if str(exc).startswith("invalid evidence")
                else "malformed agent output"
            )
            return _error_run(
                rubric=rubric,
                judge=JudgeKind.AGENT,
                metadata=metadata,
                request=request,
                reason=reason,
                raw_response=output.text,
            )
        return ModelJudgeRun(
            assertion=_assertion(
                rubric=rubric,
                judge=JudgeKind.AGENT,
                metadata=metadata,
                status=decision.status,
                reason=decision.reason,
                evidence=refs,
            ),
            protocol=metadata,
            request=request,
            raw_response=output.text,
        )
    finally:
        engine.close()
