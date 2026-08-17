"""Single-pass rubric LLM Judge over a constrained evidence packet."""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from pydantic import JsonValue, model_validator

from ses.contracts import (
    ArtifactRef,
    AssertionResult,
    CompletedPayload,
    ContractModel,
    EngineExitStatus,
    EngineRequest,
    ErrorPayload,
    EvidenceRef,
    GradeStatus,
    JsonPointer,
    JudgeKind,
    RecordType,
    SchemaVersion,
    Sha256Digest,
    TextDeltaPayload,
    ToolCallPayload,
)
from ses.engines.base import Engine
from ses.engines.claude_code import ClaudeCodeEngine
from ses.engines.fake import FakeEngine
from ses.evaluation.evidence_extractor import (
    EvidenceBundle,
    evidence_json_bytes,
    evidence_sha256,
)
from ses.foundation.config import LockedModel
from ses.foundation.credentials import ProviderCredentials
from ses.foundation.workspace import CaseWorkspace, WorkspaceFactory

RUBRIC_PROMPT_VERSION = "rubric-prompt-v3"
MODEL_PROTOCOL_VERSION = "llm-assertion-json-v2"
_MODEL_PROTOCOL = (
    "ses.evaluation.llm-judge/llm-assertion-json-v2|"
    "single-pass-rubric-rating|"
    "assertion_id:string|status:pass,fail,not_evaluated|"
    "reason:string|evidence_references:allowed-evidence-json-pointer[]|"
    "strict-json:no-markdown:no-retry"
)
MODEL_PROTOCOL_SHA256: Sha256Digest = hashlib.sha256(
    _MODEL_PROTOCOL.encode("utf-8")
).hexdigest()
_ALLOWED_EVIDENCE_ROOTS = frozenset(
    {
        "state_diff_facts",
        "tool_timeline",
        "amount_reconciliation",
        "key_messages",
    }
)


class Rubric(ContractModel):
    """One versioned semantic criterion evaluated as one assertion."""

    rubric_id: str
    rubric_version: str
    assertion_id: str
    required: bool = True
    criterion: str


class JudgeResponseSource(StrEnum):
    """How the response consumed by a Judge was actually obtained."""

    LIVE_ENGINE = "live_engine"
    FIXED_RESPONSE = "fixed_response"


class JudgeModelConfig(ContractModel):
    """Engine-attested response identity and settings that affect judgment."""

    model_id: str
    model_lock_version: str
    model_parameters: Mapping[str, JsonValue]
    response_source: JudgeResponseSource

    @property
    def sha256(self) -> Sha256Digest:
        return _sha256_json(self.model_dump(mode="json"))


class ModelDecision(ContractModel):
    """Strict LLM wire response before canonical result construction."""

    assertion_id: str
    status: GradeStatus
    reason: str
    evidence_references: tuple[JsonPointer, ...]

    @model_validator(mode="after")
    def _validate_decision(self) -> ModelDecision:
        if self.status is GradeStatus.ERROR:
            raise ValueError("models cannot declare judge error")
        if not self.reason.strip():
            raise ValueError("reason must not be blank")
        if self.status in {GradeStatus.PASS, GradeStatus.FAIL} and not (
            self.evidence_references
        ):
            raise ValueError("pass and fail require evidence references")
        if len(set(self.evidence_references)) != len(self.evidence_references):
            raise ValueError("evidence references must be unique")
        return self


class JudgeProtocolMetadata(ContractModel):
    """Stable identities required to reproduce a model judgment."""

    judge: JudgeKind
    rubric_version: str
    rubric_sha256: Sha256Digest
    prompt_version: str
    prompt_sha256: Sha256Digest
    extractor_version: str
    extractor_sha256: Sha256Digest
    evidence_sha256: Sha256Digest
    judge_model_id: str
    model_lock_version: str
    response_source: JudgeResponseSource
    model_config_sha256: Sha256Digest
    model_protocol_version: str
    model_protocol_sha256: Sha256Digest
    protocol_sha256: Sha256Digest


class ModelJudgeRun(ContractModel):
    """Canonical assertion plus the exact protocol and Engine request used."""

    assertion: AssertionResult
    protocol: JudgeProtocolMetadata
    request: EngineRequest
    raw_response: str


@dataclass(frozen=True)
class _EngineOutput:
    text: str
    error: str | None = None


def _sha256_text(value: str) -> Sha256Digest:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: object) -> Sha256Digest:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


class BoundJudgeEngine:
    """An exact Engine paired with provenance derived from its execution source."""

    __slots__ = ("_engine", "_model", "_workspace")
    _engine: Engine
    _model: JudgeModelConfig
    _workspace: CaseWorkspace | None

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("use BoundJudgeEngine.from_fake or .production")

    @classmethod
    def _create(
        cls,
        *,
        engine: Engine,
        workspace: CaseWorkspace | None,
    ) -> BoundJudgeEngine:
        if workspace is not None:
            if workspace.mcp_config is not None:
                raise ValueError("Judge workspace must not contain MCP configuration")
            if any(workspace.root.iterdir()):
                raise ValueError("Judge workspace must start empty")
        if type(engine) is FakeEngine:
            digest = _sha256_json(engine.fixture.model_dump(mode="json"))
            model = JudgeModelConfig(
                model_id="ses/fixed-response-fixture",
                model_lock_version=f"fake-fixture:sha256:{digest}",
                model_parameters={},
                response_source=JudgeResponseSource.FIXED_RESPONSE,
            )
        elif type(engine) is ClaudeCodeEngine:
            if workspace is None or engine.workspace != workspace:
                raise ValueError("Claude Judge must use its bound workspace")
            digest = _sha256_json(engine.model.model_dump(mode="json"))
            model = JudgeModelConfig(
                model_id=engine.model.model_id,
                model_lock_version=f"locked-model:sha256:{digest}",
                model_parameters={},
                response_source=JudgeResponseSource.LIVE_ENGINE,
            )
        else:
            raise TypeError("Judge Engine must be created by a supported factory")
        instance = object.__new__(cls)
        instance._engine = engine
        instance._model = model
        instance._workspace = workspace
        return instance

    @classmethod
    def from_fake(cls, engine: FakeEngine) -> BoundJudgeEngine:
        """Bind a replay to a provenance identity derived from its exact fixture."""

        return cls._from_fake_in_workspace(engine, workspace=None)

    @classmethod
    def _from_fake_in_workspace(
        cls,
        engine: FakeEngine,
        *,
        workspace: CaseWorkspace | None,
    ) -> BoundJudgeEngine:
        if type(engine) is not FakeEngine:
            raise TypeError("fixed Judge responses require an exact FakeEngine")
        return cls._create(
            engine=engine,
            workspace=workspace,
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
        system_prompt: str | None = None,
        run_id: str = "llm-judge",
        case_id: str = "read-only-evidence",
        iteration_id: str = "0",
        output_json_schema: Mapping[str, object] | None = None,
    ) -> BoundJudgeEngine:
        """Create a live Claude Judge and derive provenance from its locked model."""

        factory = workspace_factory or WorkspaceFactory()
        workspace = factory.create(
            run_id=run_id,
            case_id=case_id,
            iteration_id=iteration_id,
        )
        engine = ClaudeCodeEngine(
            model=model,
            credentials=credentials,
            workspace=workspace,
            executable=executable,
            environ=environ,
            system_prompt=system_prompt,
            output_json_schema=output_json_schema,
        )
        return cls._create(
            engine=engine,
            workspace=workspace,
        )

    @property
    def engine(self) -> Engine:
        return self._engine

    @property
    def model(self) -> JudgeModelConfig:
        return self._model

    @property
    def workspace(self) -> CaseWorkspace | None:
        return self._workspace

    def close(self) -> None:
        """Remove only the workspace minted by the Judge factory."""

        cleanup_root = self._workspace.cleanup_root if self._workspace else None
        if cleanup_root is not None and cleanup_root.exists():
            shutil.rmtree(cleanup_root)


def _rubric_sha256(rubric: Rubric) -> Sha256Digest:
    return _sha256_json(rubric.model_dump(mode="json"))


def _render_llm_prompt(*, rubric: Rubric, evidence: EvidenceBundle) -> str:
    rubric_json = json.dumps(
        rubric.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    evidence_json = evidence_json_bytes(evidence).decode("utf-8")
    allowed_references = [
        *(
            f"/state_diff_facts/{index}"
            for index in range(len(evidence.state_diff_facts))
        ),
        *(
            f"/tool_timeline/{index}/{field}"
            for index in range(len(evidence.tool_timeline))
            for field in ("tool_name", "arguments", "result_content")
        ),
        *(f"/key_messages/{index}/text" for index in range(len(evidence.key_messages))),
        "/amount_reconciliation",
    ]
    allowed_json = json.dumps(allowed_references, separators=(",", ":"))
    return (
        f"Protocol: {RUBRIC_PROMPT_VERSION}. Rate the rubric criterion in one "
        "single pass from the supplied evidence. Do not call tools or use outside "
        "information. Return exactly one JSON object with assertion_id, status, "
        "reason, and evidence_references. status must be pass, fail, or "
        "not_evaluated. Evidence references may point only into "
        "state_diff_facts, tool_timeline, amount_reconciliation, or key_messages.\n"
        "Copy evidence_references verbatim from ALLOWED_EVIDENCE_REFERENCES; do "
        "not invent deeper paths or substitute record labels.\n"
        f"ALLOWED_EVIDENCE_REFERENCES={allowed_json}\n"
        f"RUBRIC={rubric_json}\nEVIDENCE={evidence_json}"
    )


def _metadata(
    *,
    judge: JudgeKind,
    prompt_version: str,
    prompt: str,
    rubric: Rubric,
    evidence: EvidenceBundle,
    model: JudgeModelConfig,
    model_protocol_version: str,
    model_protocol_sha256: Sha256Digest,
) -> JudgeProtocolMetadata:
    values = {
        "judge": judge.value,
        "rubric_version": rubric.rubric_version,
        "rubric_sha256": _rubric_sha256(rubric),
        "prompt_version": prompt_version,
        "prompt_sha256": _sha256_text(prompt),
        "extractor_version": evidence.extractor_version,
        "extractor_sha256": evidence.extractor_sha256,
        "evidence_sha256": evidence_sha256(evidence),
        "judge_model_id": model.model_id,
        "model_lock_version": model.model_lock_version,
        "response_source": model.response_source.value,
        "model_config_sha256": model.sha256,
        "model_protocol_version": model_protocol_version,
        "model_protocol_sha256": model_protocol_sha256,
    }
    return JudgeProtocolMetadata(
        judge=judge,
        rubric_version=rubric.rubric_version,
        rubric_sha256=values["rubric_sha256"],
        prompt_version=prompt_version,
        prompt_sha256=values["prompt_sha256"],
        extractor_version=evidence.extractor_version,
        extractor_sha256=evidence.extractor_sha256,
        evidence_sha256=values["evidence_sha256"],
        judge_model_id=model.model_id,
        model_lock_version=model.model_lock_version,
        response_source=model.response_source,
        model_config_sha256=values["model_config_sha256"],
        model_protocol_version=model_protocol_version,
        model_protocol_sha256=model_protocol_sha256,
        protocol_sha256=_sha256_json(values),
    )


def _decode_pointer(pointer: str) -> tuple[str, ...]:
    return tuple(
        token.replace("~1", "/").replace("~0", "~") for token in pointer[1:].split("/")
    )


def _pointer_exists(evidence: EvidenceBundle, pointer: str) -> bool:
    tokens = _decode_pointer(pointer)
    if not tokens or tokens[0] not in _ALLOWED_EVIDENCE_ROOTS:
        return False
    current: object = evidence.model_dump(mode="json")
    for token in tokens:
        if isinstance(current, Mapping):
            if token not in current:
                return False
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit() or (token != "0" and token.startswith("0")):
                return False
            index = int(token)
            if index >= len(current):
                return False
            current = current[index]
        else:
            return False
    return True


def _validated_refs(
    *,
    evidence: EvidenceBundle,
    evidence_artifact: ArtifactRef,
    pointers: tuple[JsonPointer, ...],
) -> tuple[EvidenceRef, ...]:
    invalid = next(
        (pointer for pointer in pointers if not _pointer_exists(evidence, pointer)),
        None,
    )
    if invalid is not None:
        raise ValueError(f"invalid evidence reference: {invalid}")
    return tuple(
        EvidenceRef(artifact=evidence_artifact, json_pointer=pointer)
        for pointer in pointers
    )


def _assertion(
    *,
    rubric: Rubric,
    judge: JudgeKind,
    metadata: JudgeProtocolMetadata,
    status: GradeStatus,
    reason: str,
    evidence: tuple[EvidenceRef, ...] = (),
) -> AssertionResult:
    return AssertionResult(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.ASSERTION_RESULT,
        assertion_id=rubric.assertion_id,
        judge=judge,
        judge_version=f"{judge.value}-v2:{metadata.protocol_sha256[:16]}",
        required=rubric.required,
        status=status,
        reason=reason,
        evidence=evidence,
    )


def _error_run(
    *,
    rubric: Rubric,
    judge: JudgeKind,
    metadata: JudgeProtocolMetadata,
    request: EngineRequest,
    reason: str,
    raw_response: str = "",
) -> ModelJudgeRun:
    return ModelJudgeRun(
        assertion=_assertion(
            rubric=rubric,
            judge=judge,
            metadata=metadata,
            status=GradeStatus.ERROR,
            reason=f"Judge error: {reason}",
        ),
        protocol=metadata,
        request=request,
        raw_response=raw_response,
    )


async def _collect_engine_output(
    engine: Engine, request: EngineRequest
) -> _EngineOutput:
    text_parts: list[str] = []
    terminal_status: EngineExitStatus | None = None
    engine_error = False
    unauthorized_tools: list[str] = []
    try:
        async for event in engine.stream(request):
            payload = event.payload
            if isinstance(payload, TextDeltaPayload):
                text_parts.append(payload.text)
            elif isinstance(payload, ToolCallPayload):
                unauthorized_tools.append(payload.tool_name)
            elif isinstance(payload, ErrorPayload):
                engine_error = True
            elif isinstance(payload, CompletedPayload):
                terminal_status = payload.exit_status
    except Exception:
        return _EngineOutput(text="".join(text_parts), error="engine execution failed")
    text = "".join(text_parts)
    if unauthorized_tools:
        return _EngineOutput(
            text=text, error=f"unauthorized tool request: {unauthorized_tools[0]}"
        )
    if engine_error or terminal_status is not EngineExitStatus.SUCCESS:
        return _EngineOutput(text=text, error="engine did not complete successfully")
    return _EngineOutput(text=text)


def _request(
    *,
    judge: JudgeKind,
    prompt: str,
    metadata: JudgeProtocolMetadata,
    timeout_seconds: float,
) -> EngineRequest:
    return EngineRequest(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.ENGINE_REQUEST,
        request_id=(
            f"judge-{judge.value}-{metadata.evidence_sha256[:12]}-"
            f"{metadata.rubric_sha256[:12]}"
        ),
        prompt=prompt,
        allowed_tools=(),
        timeout_seconds=timeout_seconds,
    )


def _artifact_matches(evidence: EvidenceBundle, artifact: ArtifactRef) -> bool:
    return artifact.sha256 == hashlib.sha256(evidence_json_bytes(evidence)).hexdigest()


async def _judge_llm(
    engine: BoundJudgeEngine,
    *,
    rubric: Rubric,
    evidence: EvidenceBundle,
    evidence_artifact: ArtifactRef,
    timeout_seconds: float = 30,
) -> ModelJudgeRun:
    prompt = _render_llm_prompt(rubric=rubric, evidence=evidence)
    metadata = _metadata(
        judge=JudgeKind.LLM,
        prompt_version=RUBRIC_PROMPT_VERSION,
        prompt=prompt,
        rubric=rubric,
        evidence=evidence,
        model=engine.model,
        model_protocol_version=MODEL_PROTOCOL_VERSION,
        model_protocol_sha256=MODEL_PROTOCOL_SHA256,
    )
    request = _request(
        judge=JudgeKind.LLM,
        prompt=prompt,
        metadata=metadata,
        timeout_seconds=timeout_seconds,
    )
    if not _artifact_matches(evidence, evidence_artifact):
        return _error_run(
            rubric=rubric,
            judge=JudgeKind.LLM,
            metadata=metadata,
            request=request,
            reason="evidence artifact checksum does not match the supplied evidence",
        )
    output = await _collect_engine_output(engine.engine, request)
    if output.error is not None:
        return _error_run(
            rubric=rubric,
            judge=JudgeKind.LLM,
            metadata=metadata,
            request=request,
            reason=output.error,
            raw_response=output.text,
        )
    try:
        raw: JsonValue = json.loads(output.text)
        decision = ModelDecision.model_validate(raw)
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
            else "malformed model output"
        )
        return _error_run(
            rubric=rubric,
            judge=JudgeKind.LLM,
            metadata=metadata,
            request=request,
            reason=reason,
            raw_response=output.text,
        )
    return ModelJudgeRun(
        assertion=_assertion(
            rubric=rubric,
            judge=JudgeKind.LLM,
            metadata=metadata,
            status=decision.status,
            reason=decision.reason,
            evidence=refs,
        ),
        protocol=metadata,
        request=request,
        raw_response=output.text,
    )


async def judge_llm(
    engine: BoundJudgeEngine,
    *,
    rubric: Rubric,
    evidence: EvidenceBundle,
    evidence_artifact: ArtifactRef,
    timeout_seconds: float = 30,
) -> ModelJudgeRun:
    """Rate one rubric through an Engine with source-bound provenance."""

    if type(engine) is not BoundJudgeEngine:
        raise TypeError("LLM Judge requires a BoundJudgeEngine")
    try:
        return await _judge_llm(
            engine,
            rubric=rubric,
            evidence=evidence,
            evidence_artifact=evidence_artifact,
            timeout_seconds=timeout_seconds,
        )
    finally:
        engine.close()
