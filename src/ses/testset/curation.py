"""LLM-assisted source triage and rubric drafting for verified cases."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from time import monotonic
from typing import Final, Literal, Protocol, cast
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

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
from ses.engines.base import Engine
from ses.engines.claude_code import ClaudeCodeEngine
from ses.foundation.config import LockedModel
from ses.foundation.credentials import ProviderCredentials
from ses.foundation.workspace import CaseWorkspace, WorkspaceFactory
from ses.testset.scrub import ScrubbedConversation, scrub_abcd

CURATION_VERSION: Final[Literal["ses-llm-assisted-curation-v1"]] = (
    "ses-llm-assisted-curation-v1"
)
TRIAGE_PROMPT_VERSION: Final = "ses-source-triage-v1"
RUBRIC_PROMPT_VERSION: Final = "ses-rubric-draft-v1"
SOURCE_KIND: Final[Literal["benchmark_proxy"]] = "benchmark_proxy"
_SUPPORTED_SOURCE_LABELS = frozenset({("product_defect", "return_size")})


class CurationRecord(BaseModel):
    """Strict immutable private curation data."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class SourceTurn(CurationRecord):
    """One delexed source turn that a reviewer can locate exactly."""

    turn_index: int = Field(ge=0)
    speaker: str
    text: str


class SourceEvidence(CurationRecord):
    """A pinned benchmark excerpt supplied to the screening model."""

    source_id: str
    source_kind: Literal["benchmark_proxy"] = SOURCE_KIND
    source_version: str
    flow: str
    subflow: str
    turns: tuple[SourceTurn, ...]

    @model_validator(mode="after")
    def _require_turns(self) -> SourceEvidence:
        if not self.turns:
            raise ValueError("source evidence must contain turns")
        if tuple(turn.turn_index for turn in self.turns) != tuple(
            range(len(self.turns))
        ):
            raise ValueError("source turn indexes must be contiguous")
        return self


class DeterministicSignals(CurationRecord):
    """Cheap, explainable signals extracted before any model call."""

    source_id: str
    environment_supported: bool
    customer_markers: tuple[str, ...]
    risk_flags: tuple[str, ...]


class EvidenceSpan(CurationRecord):
    """One exact source turn cited by an LLM triage decision."""

    turn_index: int = Field(ge=0)
    speaker: str
    text: str


class TriageDecision(CurationRecord):
    """Strict model output for source-to-environment mappability."""

    intent: str
    failure_type: str
    mappable: bool
    severity: Literal["low", "medium", "high"]
    evidence_spans: tuple[EvidenceSpan, ...]
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str

    @model_validator(mode="after")
    def _require_evidence_and_reason(self) -> TriageDecision:
        if not self.intent.strip() or not self.failure_type.strip():
            raise ValueError("triage intent and failure_type must not be blank")
        if not self.reason.strip() or not self.evidence_spans:
            raise ValueError("triage requires a reason and source evidence")
        indexes = tuple(span.turn_index for span in self.evidence_spans)
        if len(set(indexes)) != len(indexes):
            raise ValueError("triage evidence spans must be unique")
        return self


class RubricCriterionDraft(CurationRecord):
    """One model-scored criterion proposed for later human approval."""

    criterion_id: str
    criterion: str
    evidence_scope: tuple[Literal["tool_timeline", "key_messages"], ...]
    required: bool = True

    @model_validator(mode="after")
    def _validate_criterion(self) -> RubricCriterionDraft:
        if not self.criterion_id.strip() or not self.criterion.strip():
            raise ValueError("rubric criterion fields must not be blank")
        if not self.evidence_scope or len(set(self.evidence_scope)) != len(
            self.evidence_scope
        ):
            raise ValueError("rubric evidence scope must be nonempty and unique")
        return self


class RubricDraft(CurationRecord):
    """Model-authored wording and semantic checks, never numeric gold."""

    public_request_template: str
    criteria: tuple[RubricCriterionDraft, ...]
    reviewer_notes: str

    @model_validator(mode="after")
    def _validate_safe_draft(self) -> RubricDraft:
        if self.public_request_template.count("{order_id}") != 1:
            raise ValueError("public request template requires one {order_id}")
        if self.public_request_template.count("{reason}") != 1:
            raise ValueError("public request template requires one {reason}")
        if "{" in self.public_request_template.replace("{order_id}", "").replace(
            "{reason}", ""
        ):
            raise ValueError("public request template contains an unknown placeholder")
        if not self.criteria or len(
            {item.criterion_id for item in self.criteria}
        ) != len(self.criteria):
            raise ValueError("rubric criteria must be nonempty and unique")
        serialized = json.dumps(self.model_dump(mode="json"), ensure_ascii=False)
        forbidden = ("amount_minor", "expected_terminal", "gold_hash", "oracle")
        if any(token in serialized.casefold() for token in forbidden):
            raise ValueError("rubric draft must not contain private oracle data")
        if re.search(
            r"(?:[$€£¥]\s*\d|\b\d+(?:\.\d+)?\s*(?:usd|cny|dollars?)\b)",
            serialized,
            re.IGNORECASE,
        ):
            raise ValueError("rubric draft must not invent monetary answers")
        return self


class CurationResponseSource(StrEnum):
    """How a curation response was obtained."""

    FIXED_RESPONSE = "fixed_response"
    LIVE_ENGINE = "live_engine"


class ModelInvocation(CurationRecord):
    """Auditable identity and measured usage for one structured response."""

    stage: Literal["triage", "rubric_draft"]
    source_id: str
    response_source: CurationResponseSource
    provider_host: str
    model_id: str
    model_lock_version: str
    prompt_version: str
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    usage: Usage
    latency_ms: int = Field(ge=0)
    network_used: bool
    live_model_measured: bool

    @model_validator(mode="after")
    def _validate_execution_source(self) -> ModelInvocation:
        if not all(
            value.strip()
            for value in (
                self.source_id,
                self.provider_host,
                self.model_id,
                self.model_lock_version,
                self.prompt_version,
            )
        ):
            raise ValueError("model invocation identity fields must not be blank")
        expected_live = self.response_source is CurationResponseSource.LIVE_ENGINE
        if (
            self.network_used != expected_live
            or self.live_model_measured != expected_live
        ):
            raise ValueError("model invocation source flags are inconsistent")
        return self


class CuratedSource(CurationRecord):
    """One source after deterministic checks and model assistance."""

    source: SourceEvidence
    signals: DeterministicSignals
    triage: TriageDecision
    triage_invocation: ModelInvocation
    selected: bool
    selection_reason: str
    rubric_draft: RubricDraft | None = None
    rubric_invocation: ModelInvocation | None = None

    @model_validator(mode="after")
    def _validate_selection(self) -> CuratedSource:
        source_id = self.source.source_id
        if self.signals.source_id != source_id:
            raise ValueError("deterministic signals do not match their source")
        if (
            self.triage_invocation.source_id != source_id
            or self.triage_invocation.stage != "triage"
        ):
            raise ValueError("triage invocation does not match its source or stage")
        if self.selected != (
            self.signals.environment_supported and self.triage.mappable
        ):
            raise ValueError(
                "source selection must combine deterministic and LLM gates"
            )
        if self.selected != (self.rubric_draft is not None):
            raise ValueError("selected sources require exactly one rubric draft")
        if (self.rubric_draft is None) != (self.rubric_invocation is None):
            raise ValueError("rubric draft and invocation must be present together")
        if self.rubric_invocation is not None and (
            self.rubric_invocation.source_id != source_id
            or self.rubric_invocation.stage != "rubric_draft"
        ):
            raise ValueError("rubric invocation does not match its source or stage")
        return self


class CurationBundle(CurationRecord):
    """Complete source-screening result consumed by qualification."""

    curation_version: Literal["ses-llm-assisted-curation-v1"] = CURATION_VERSION
    sources: tuple[CuratedSource, ...]

    @model_validator(mode="after")
    def _require_unique_sources(self) -> CurationBundle:
        source_ids = tuple(item.source.source_id for item in self.sources)
        if not source_ids or len(set(source_ids)) != len(source_ids):
            raise ValueError("curation sources must be nonempty and unique")
        return self

    @property
    def by_source_id(self) -> Mapping[str, CuratedSource]:
        return {item.source.source_id: item for item in self.sources}

    @property
    def live_provider_used(self) -> bool:
        return any(
            invocation.live_model_measured
            for item in self.sources
            for invocation in (item.triage_invocation, item.rubric_invocation)
            if invocation is not None
        )

    @property
    def network_used(self) -> bool:
        return any(
            invocation.network_used
            for item in self.sources
            for invocation in (item.triage_invocation, item.rubric_invocation)
            if invocation is not None
        )


class FixedSourceResponses(CurationRecord):
    """Raw fixture objects replayed through the production validators."""

    triage: Mapping[str, JsonValue]
    rubric_draft: Mapping[str, JsonValue] | None = None
    triage_usage: Usage
    rubric_usage: Usage | None = None


class FixedCurationFixture(CurationRecord):
    """Checked offline responses for deterministic CI and course playback."""

    schema_version: Literal["v1alpha1"]
    fixture_version: str
    model_id: str
    triage_prompt_version: Literal["ses-source-triage-v1"]
    rubric_prompt_version: Literal["ses-rubric-draft-v1"]
    responses: Mapping[str, FixedSourceResponses]


@dataclass(frozen=True, slots=True)
class RawModelResponse:
    text: str
    invocation: ModelInvocation


class CurationModel(Protocol):
    """Narrow source-aware interface shared by fixed and live responses."""

    async def invoke(
        self,
        *,
        stage: Literal["triage", "rubric_draft"],
        source_id: str,
        prompt: str,
        prompt_version: str,
    ) -> RawModelResponse: ...

    def close(self) -> None: ...


def _canonical_bytes(value: object) -> bytes:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _source_evidence(record: ScrubbedConversation) -> SourceEvidence:
    return SourceEvidence(
        source_id=record.source_id,
        source_version=record.source_commit,
        flow=record.flow,
        subflow=record.subflow,
        turns=tuple(
            SourceTurn(
                turn_index=index,
                speaker=turn.speaker,
                text=turn.text,
            )
            for index, turn in enumerate(record.delexed)
        ),
    )


def load_source_evidence(
    path: Path, source_ids: Sequence[str]
) -> Mapping[str, SourceEvidence]:
    """Load exact delexed excerpts for candidate IDs from the pinned ABCD slice."""

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("ABCD source evidence must be a JSON list")
    records = scrub_abcd(tuple(cast(Mapping[str, object], item) for item in raw))
    available = {
        record.source_id: _source_evidence(record) for record in records.records
    }
    missing = sorted(set(source_ids) - set(available))
    if missing:
        raise ValueError(f"candidate source evidence is missing:{missing[0]}")
    return {source_id: available[source_id] for source_id in source_ids}


def extract_deterministic_signals(source: SourceEvidence) -> DeterministicSignals:
    """Extract cheap markers without deciding semantic mappability for the model."""

    customer_text = " ".join(
        turn.text.casefold() for turn in source.turns if turn.speaker == "customer"
    )
    markers: list[str] = []
    for marker, phrases in (
        ("return_request", ("return an item", "reason for the return")),
        ("refund_status", ("status of a refund", "till it is refunded")),
        ("wrong_size", ("wrong size",)),
    ):
        if any(phrase in customer_text for phrase in phrases):
            markers.append(marker)
    supported = (source.flow, source.subflow) in _SUPPORTED_SOURCE_LABELS
    risks = [] if supported else ["unsupported_shop_intent"]
    if not markers:
        risks.append("no_known_customer_marker")
    return DeterministicSignals(
        source_id=source.source_id,
        environment_supported=supported,
        customer_markers=tuple(markers),
        risk_flags=tuple(risks),
    )


def _render_triage_prompt(source: SourceEvidence, signals: DeterministicSignals) -> str:
    return (
        f"Protocol: {TRIAGE_PROMPT_VERSION}. You screen benchmark-proxy customer "
        "support excerpts for an executable Shop return test. The environment can "
        "initiate an item return and compute policy outcomes, but cannot answer an "
        "existing refund-status query. Use only SOURCE and SIGNALS. Return exactly "
        "one JSON object with intent, failure_type, mappable, severity, "
        "evidence_spans, confidence, and reason. Each evidence span must copy one "
        "complete source turn with turn_index, speaker, and text. Do not return "
        "markdown.\n"
        f"SOURCE={_canonical_bytes(source).decode('utf-8')}\n"
        f"SIGNALS={_canonical_bytes(signals).decode('utf-8')}"
    )


def _render_rubric_prompt(source: SourceEvidence, triage: TriageDecision) -> str:
    return (
        f"Protocol: {RUBRIC_PROMPT_VERSION}. Draft the public wording and semantic "
        "rubric for a controlled executable variant. Return exactly one JSON object "
        "with public_request_template, criteria, and reviewer_notes. The template "
        "must contain exactly the placeholders {order_id} and {reason}. Each "
        "criterion has criterion_id, criterion, evidence_scope, and required. "
        "evidence_scope may contain only tool_timeline and key_messages. Do not "
        "invent an amount, terminal state, policy result, oracle, or gold answer; a "
        "deterministic policy engine owns those values. Do not return markdown.\n"
        f"SOURCE={_canonical_bytes(source).decode('utf-8')}\n"
        f"TRIAGE={_canonical_bytes(triage).decode('utf-8')}"
    )


def _validate_spans(source: SourceEvidence, triage: TriageDecision) -> None:
    turns = {turn.turn_index: turn for turn in source.turns}
    for span in triage.evidence_spans:
        expected = turns.get(span.turn_index)
        if expected is None or (
            expected.speaker != span.speaker or expected.text != span.text
        ):
            raise ValueError("triage evidence span does not match source evidence")
    if not any(span.speaker == "customer" for span in triage.evidence_spans):
        raise ValueError("triage must cite at least one customer source turn")


class FixedCurationModel:
    """Replay checked responses while exercising the same strict parser and gates."""

    def __init__(self, fixture: FixedCurationFixture) -> None:
        self._fixture = fixture
        self._fixture_hash = _sha256(fixture)

    @classmethod
    def from_path(cls, path: Path) -> FixedCurationModel:
        return cls(FixedCurationFixture.model_validate_json(path.read_text("utf-8")))

    async def invoke(
        self,
        *,
        stage: Literal["triage", "rubric_draft"],
        source_id: str,
        prompt: str,
        prompt_version: str,
    ) -> RawModelResponse:
        response = self._fixture.responses.get(source_id)
        if response is None:
            raise ValueError(f"fixed curation response is missing:{source_id}")
        raw = response.triage if stage == "triage" else response.rubric_draft
        usage = response.triage_usage if stage == "triage" else response.rubric_usage
        if raw is None or usage is None:
            raise ValueError(f"fixed curation stage is missing:{source_id}:{stage}")
        expected_prompt_version = (
            self._fixture.triage_prompt_version
            if stage == "triage"
            else self._fixture.rubric_prompt_version
        )
        if prompt_version != expected_prompt_version:
            raise ValueError("fixed curation prompt version does not match its fixture")
        text = _canonical_bytes(raw).decode("utf-8")
        return RawModelResponse(
            text=text,
            invocation=ModelInvocation(
                stage=stage,
                source_id=source_id,
                response_source=CurationResponseSource.FIXED_RESPONSE,
                provider_host="offline-fixture",
                model_id=self._fixture.model_id,
                model_lock_version=f"fixture:sha256:{self._fixture_hash}",
                prompt_version=prompt_version,
                prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
                response_sha256=hashlib.sha256(text.encode()).hexdigest(),
                usage=usage,
                latency_ms=0,
                network_used=False,
                live_model_measured=False,
            ),
        )

    def close(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class LiveModelBinding:
    engine: Engine
    model: LockedModel
    workspace: CaseWorkspace


class LiveCurationModel:
    """Call locked ClaudeCLI models with no tools and strict JSON responses."""

    def __init__(
        self,
        *,
        triage: LiveModelBinding,
        rubric: LiveModelBinding,
        timeout_seconds: float = 120,
    ) -> None:
        self._bindings = {"triage": triage, "rubric_draft": rubric}
        self._timeout_seconds = timeout_seconds

    @classmethod
    def production(
        cls,
        *,
        triage_model: LockedModel,
        rubric_model: LockedModel,
        credentials: ProviderCredentials,
        executable: str,
        environ: Mapping[str, str],
        timeout_seconds: float = 120,
    ) -> LiveCurationModel:
        factory = WorkspaceFactory()

        def bind(stage: str, model: LockedModel) -> LiveModelBinding:
            workspace = factory.create(
                run_id="ticket07-curation",
                case_id=stage,
                iteration_id="live",
            )
            engine = ClaudeCodeEngine(
                model=model,
                credentials=credentials,
                workspace=workspace,
                executable=executable,
                environ=environ,
                system_prompt=(
                    "Return only the requested JSON. Do not call tools, read files, "
                    "or use outside information."
                ),
            )
            return LiveModelBinding(engine=engine, model=model, workspace=workspace)

        triage = bind("triage", triage_model)
        try:
            rubric = bind("rubric-draft", rubric_model)
        except Exception:
            cleanup = triage.workspace.cleanup_root
            if cleanup is not None and cleanup.exists():
                shutil.rmtree(cleanup)
            raise
        return cls(
            triage=triage,
            rubric=rubric,
            timeout_seconds=timeout_seconds,
        )

    async def invoke(
        self,
        *,
        stage: Literal["triage", "rubric_draft"],
        source_id: str,
        prompt: str,
        prompt_version: str,
    ) -> RawModelResponse:
        binding = self._bindings[stage]
        request = EngineRequest(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type=RecordType.ENGINE_REQUEST,
            request_id=f"curation-{stage}-{hashlib.sha256(source_id.encode()).hexdigest()[:16]}",
            prompt=prompt,
            allowed_tools=(),
            timeout_seconds=self._timeout_seconds,
        )
        started = monotonic()
        text_parts: list[str] = []
        usage = Usage(input_tokens=0, output_tokens=0)
        usage_seen = False
        terminal: EngineExitStatus | None = None
        failed = False
        async for event in binding.engine.stream(request):
            payload = event.payload
            if isinstance(payload, TextDeltaPayload):
                text_parts.append(payload.text)
            elif isinstance(payload, UsagePayload):
                usage = payload.usage
                usage_seen = True
            elif isinstance(payload, ToolCallPayload):
                raise ValueError("curation model attempted an unauthorized tool call")
            elif isinstance(payload, ErrorPayload):
                failed = True
            elif isinstance(payload, CompletedPayload):
                terminal = payload.exit_status
        if failed or terminal is not EngineExitStatus.SUCCESS:
            raise ValueError("curation model did not complete successfully")
        if not usage_seen:
            raise ValueError("curation model did not report usage")
        text = "".join(text_parts)
        model_hash = _sha256(binding.model)
        return RawModelResponse(
            text=text,
            invocation=ModelInvocation(
                stage=stage,
                source_id=source_id,
                response_source=CurationResponseSource.LIVE_ENGINE,
                provider_host=urlparse(binding.model.base_url).hostname or "unknown",
                model_id=binding.model.model_id,
                model_lock_version=f"locked-model:sha256:{model_hash}",
                prompt_version=prompt_version,
                prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
                response_sha256=hashlib.sha256(text.encode()).hexdigest(),
                usage=usage,
                latency_ms=round((monotonic() - started) * 1000),
                network_used=True,
                live_model_measured=True,
            ),
        )

    def close(self) -> None:
        for binding in self._bindings.values():
            cleanup = binding.workspace.cleanup_root
            if cleanup is not None and cleanup.exists():
                shutil.rmtree(cleanup)


def _parse_response(text: str, model: type[CurationRecord]) -> CurationRecord:
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("curation model returned malformed JSON") from exc
    try:
        return model.model_validate_json(text)
    except ValueError as exc:
        raise ValueError("curation model response failed its strict schema") from exc


async def curate_sources(
    *,
    source_ids: Sequence[str],
    source_path: Path,
    model: CurationModel,
) -> CurationBundle:
    """Run deterministic and model-assisted source screening before generation."""

    try:
        evidence = load_source_evidence(source_path, source_ids)
        curated: list[CuratedSource] = []
        for source_id in source_ids:
            source = evidence[source_id]
            signals = extract_deterministic_signals(source)
            triage_prompt = _render_triage_prompt(source, signals)
            triage_raw = await model.invoke(
                stage="triage",
                source_id=source_id,
                prompt=triage_prompt,
                prompt_version=TRIAGE_PROMPT_VERSION,
            )
            triage = cast(
                TriageDecision,
                _parse_response(triage_raw.text, TriageDecision),
            )
            _validate_spans(source, triage)
            selected = signals.environment_supported and triage.mappable
            if not signals.environment_supported:
                reason = "deterministic environment gate rejected the source"
            elif not triage.mappable:
                reason = "LLM triage rejected the source mapping"
            else:
                reason = "deterministic and LLM source gates passed"
            rubric: RubricDraft | None = None
            rubric_invocation: ModelInvocation | None = None
            if selected:
                rubric_prompt = _render_rubric_prompt(source, triage)
                rubric_raw = await model.invoke(
                    stage="rubric_draft",
                    source_id=source_id,
                    prompt=rubric_prompt,
                    prompt_version=RUBRIC_PROMPT_VERSION,
                )
                rubric = cast(
                    RubricDraft,
                    _parse_response(rubric_raw.text, RubricDraft),
                )
                rubric_invocation = rubric_raw.invocation
            curated.append(
                CuratedSource(
                    source=source,
                    signals=signals,
                    triage=triage,
                    triage_invocation=triage_raw.invocation,
                    selected=selected,
                    selection_reason=reason,
                    rubric_draft=rubric,
                    rubric_invocation=rubric_invocation,
                )
            )
    finally:
        model.close()
    return CurationBundle(sources=tuple(curated))


def invocation_cost(bundle: CurationBundle) -> tuple[int, int, Decimal, str | None]:
    """Sum model usage without claiming unknown provider pricing."""

    invocations = tuple(
        invocation
        for item in bundle.sources
        for invocation in (item.triage_invocation, item.rubric_invocation)
        if invocation is not None
    )
    costs = tuple(
        invocation.usage
        for invocation in invocations
        if invocation.usage.cost_amount is not None
    )
    currencies = {item.cost_currency for item in costs}
    if len(currencies) > 1:
        raise ValueError("curation invocations cannot combine cost currencies")
    return (
        sum(item.usage.input_tokens for item in invocations),
        sum(item.usage.output_tokens for item in invocations),
        sum((item.cost_amount or Decimal(0) for item in costs), Decimal(0)),
        next(iter(currencies)) if currencies else None,
    )
