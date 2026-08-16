"""Rubric LLM Judge built on the repository's provider-neutral Engine seam."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

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
from ses.evaluation.evidence_extractor import (
    EvidenceBundle,
    evidence_json_bytes,
    evidence_sha256,
)

RUBRIC_PROMPT_VERSION = "rubric-prompt-v1"
MODEL_PROTOCOL_VERSION = "assertion-json-v1"
_MODEL_PROTOCOL = (
    "ses.evaluation.model-judge/assertion-json-v1|"
    "assertion_id:string|status:pass,fail,not_evaluated|"
    "reason:string|evidence_references:json-pointer[]|"
    "strict-json:no-markdown:no-retry"
)
MODEL_PROTOCOL_SHA256: Sha256Digest = hashlib.sha256(
    _MODEL_PROTOCOL.encode("utf-8")
).hexdigest()


class Rubric(ContractModel):
    """One versioned semantic criterion evaluated as one assertion."""

    rubric_id: str
    rubric_version: str
    assertion_id: str
    required: bool = True
    criterion: str


class ModelDecision(ContractModel):
    """Strict model wire response before canonical result construction."""

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
    model_protocol_version: str
    model_protocol_sha256: Sha256Digest
    protocol_sha256: Sha256Digest


class ModelJudgeRun(ContractModel):
    """Canonical assertion plus the exact protocol and Engine request used."""

    assertion: AssertionResult
    protocol: JudgeProtocolMetadata
    request: EngineRequest


def _sha256_text(value: str) -> Sha256Digest:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _rubric_sha256(rubric: Rubric) -> Sha256Digest:
    return hashlib.sha256(
        json.dumps(
            rubric.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _render_prompt(
    *,
    judge: JudgeKind,
    prompt_version: str,
    rubric: Rubric,
    evidence: EvidenceBundle,
) -> str:
    role = (
        "Apply the rubric to the supplied evidence."
        if judge is JudgeKind.LLM
        else "Reason only over the supplied read-only evidence."
    )
    rubric_json = json.dumps(
        rubric.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    evidence_json = evidence_json_bytes(evidence).decode("utf-8")
    return (
        f"Protocol: {prompt_version}. {role} "
        "Do not call tools or use information outside this prompt. "
        "Return exactly one JSON object with assertion_id, status, reason, and "
        "evidence_references. status must be pass, fail, or not_evaluated. "
        "Each evidence reference must be a JSON Pointer into EVIDENCE.\n"
        f"RUBRIC={rubric_json}\nEVIDENCE={evidence_json}"
    )


def _metadata(
    *,
    judge: JudgeKind,
    prompt_version: str,
    prompt: str,
    rubric: Rubric,
    evidence: EvidenceBundle,
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
        "model_protocol_version": MODEL_PROTOCOL_VERSION,
        "model_protocol_sha256": MODEL_PROTOCOL_SHA256,
    }
    protocol_sha256 = hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return JudgeProtocolMetadata(
        judge=judge,
        rubric_version=rubric.rubric_version,
        rubric_sha256=values["rubric_sha256"],
        prompt_version=prompt_version,
        prompt_sha256=values["prompt_sha256"],
        extractor_version=evidence.extractor_version,
        extractor_sha256=evidence.extractor_sha256,
        evidence_sha256=values["evidence_sha256"],
        model_protocol_version=MODEL_PROTOCOL_VERSION,
        model_protocol_sha256=MODEL_PROTOCOL_SHA256,
        protocol_sha256=protocol_sha256,
    )


def _decode_pointer(pointer: str) -> tuple[str, ...]:
    return tuple(
        token.replace("~1", "/").replace("~0", "~") for token in pointer[1:].split("/")
    )


def _pointer_exists(evidence: EvidenceBundle, pointer: str) -> bool:
    current: object = evidence.model_dump(mode="json")
    for token in _decode_pointer(pointer):
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
        judge_version=f"{judge.value}-v1:{metadata.protocol_sha256[:16]}",
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
    )


async def _run_model_judge(
    engine: Engine,
    *,
    judge: JudgeKind,
    prompt_version: str,
    rubric: Rubric,
    evidence: EvidenceBundle,
    evidence_artifact: ArtifactRef,
    timeout_seconds: float,
) -> ModelJudgeRun:
    prompt = _render_prompt(
        judge=judge,
        prompt_version=prompt_version,
        rubric=rubric,
        evidence=evidence,
    )
    metadata = _metadata(
        judge=judge,
        prompt_version=prompt_version,
        prompt=prompt,
        rubric=rubric,
        evidence=evidence,
    )
    request = EngineRequest(
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
    actual_artifact_sha256 = hashlib.sha256(evidence_json_bytes(evidence)).hexdigest()
    if evidence_artifact.sha256 != actual_artifact_sha256:
        return _error_run(
            rubric=rubric,
            judge=judge,
            metadata=metadata,
            request=request,
            reason="evidence artifact checksum does not match the supplied evidence",
        )

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
        return _error_run(
            rubric=rubric,
            judge=judge,
            metadata=metadata,
            request=request,
            reason="engine execution failed",
        )
    if unauthorized_tools:
        return _error_run(
            rubric=rubric,
            judge=judge,
            metadata=metadata,
            request=request,
            reason=f"unauthorized tool request: {unauthorized_tools[0]}",
        )
    if engine_error or terminal_status is not EngineExitStatus.SUCCESS:
        return _error_run(
            rubric=rubric,
            judge=judge,
            metadata=metadata,
            request=request,
            reason="engine did not complete successfully",
        )
    try:
        raw: JsonValue = json.loads("".join(text_parts))
        decision = ModelDecision.model_validate(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return _error_run(
            rubric=rubric,
            judge=judge,
            metadata=metadata,
            request=request,
            reason="malformed model output",
        )
    if decision.assertion_id != rubric.assertion_id:
        return _error_run(
            rubric=rubric,
            judge=judge,
            metadata=metadata,
            request=request,
            reason="model output assertion_id does not match the rubric",
        )
    invalid = next(
        (
            pointer
            for pointer in decision.evidence_references
            if not _pointer_exists(evidence, pointer)
        ),
        None,
    )
    if invalid is not None:
        return _error_run(
            rubric=rubric,
            judge=judge,
            metadata=metadata,
            request=request,
            reason=f"invalid evidence reference: {invalid}",
        )
    refs = tuple(
        EvidenceRef(artifact=evidence_artifact, json_pointer=pointer)
        for pointer in decision.evidence_references
    )
    return ModelJudgeRun(
        assertion=_assertion(
            rubric=rubric,
            judge=judge,
            metadata=metadata,
            status=decision.status,
            reason=decision.reason,
            evidence=refs,
        ),
        protocol=metadata,
        request=request,
    )


async def judge_llm(
    engine: Engine,
    *,
    rubric: Rubric,
    evidence: EvidenceBundle,
    evidence_artifact: ArtifactRef,
    timeout_seconds: float = 30,
) -> ModelJudgeRun:
    """Evaluate one rubric with one Engine call and no retries."""

    return await _run_model_judge(
        engine,
        judge=JudgeKind.LLM,
        prompt_version=RUBRIC_PROMPT_VERSION,
        rubric=rubric,
        evidence=evidence,
        evidence_artifact=evidence_artifact,
        timeout_seconds=timeout_seconds,
    )
