"""Single-pass rubric LLM Judge over a constrained evidence packet."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

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

RUBRIC_PROMPT_VERSION = "rubric-prompt-v2"
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


class JudgeModelConfig(ContractModel):
    """Locked model identity and inference settings that affect judgment."""

    model_id: str
    model_lock_version: str
    model_parameters: Mapping[str, JsonValue]

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
    return (
        f"Protocol: {RUBRIC_PROMPT_VERSION}. Rate the rubric criterion in one "
        "single pass from the supplied evidence. Do not call tools or use outside "
        "information. Return exactly one JSON object with assertion_id, status, "
        "reason, and evidence_references. status must be pass, fail, or "
        "not_evaluated. Evidence references may point only into "
        "state_diff_facts, tool_timeline, amount_reconciliation, or key_messages.\n"
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


async def judge_llm(
    engine: Engine,
    *,
    rubric: Rubric,
    evidence: EvidenceBundle,
    evidence_artifact: ArtifactRef,
    model: JudgeModelConfig,
    timeout_seconds: float = 30,
) -> ModelJudgeRun:
    """Rate one rubric in a single model pass with no retries or tools."""

    prompt = _render_llm_prompt(rubric=rubric, evidence=evidence)
    metadata = _metadata(
        judge=JudgeKind.LLM,
        prompt_version=RUBRIC_PROMPT_VERSION,
        prompt=prompt,
        rubric=rubric,
        evidence=evidence,
        model=model,
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
    output = await _collect_engine_output(engine, request)
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
