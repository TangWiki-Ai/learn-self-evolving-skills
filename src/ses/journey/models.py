"""Canonical, credential-free progress models for the eight-station journey."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from ses.contracts.artifact import ArtifactRef
from ses.contracts.base import ContractModel, VersionedRecord
from ses.contracts.engine import Usage
from ses.contracts.primitives import (
    CurrencyCode,
    NonEmptyStr,
    SchemaVersion,
    StrictNonNegativeInt,
    UtcDateTime,
)
from ses.foundation.config import ProviderId
from ses.foundation.credentials import is_sensitive_name, redact

STATION_COUNT = 8
STATION_NUMBERS = tuple(range(STATION_COUNT))
DEFAULT_STATION_COMMANDS = tuple(
    f"uv run ses journey station {number}" for number in STATION_NUMBERS
)

StationNumber = Annotated[StrictInt, Field(ge=0, le=STATION_COUNT - 1)]


class JourneyProgressStatus(StrEnum):
    """Visible progress states shared by the journey and its stations."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    NEEDS_ATTENTION = "needs_attention"


class ExperimentMode(StrEnum):
    """Execution provenance kept distinct from visible station progress."""

    LIVE = "live"
    FIXED = "fixed"


class ExperimentCostSource(StrEnum):
    """What the displayed experiment cost actually represents."""

    SYNTHETIC_CI = "synthetic_ci"
    CLAUDE_CODE_ESTIMATE = "claude_code_estimate"
    UNAVAILABLE = "unavailable"


def _reject_sensitive_text(value: str) -> str:
    if redact(value) != value:
        raise ValueError("journey text must not contain credential material")
    return value


class JourneyModel(ContractModel):
    """Strict base that also keeps rejected input out of validation messages."""

    model_config = ConfigDict(hide_input_in_errors=True)


class ExperimentUsage(JourneyModel):
    """Cumulative paid-engine usage displayed throughout the journey."""

    input_tokens: StrictNonNegativeInt = 0
    output_tokens: StrictNonNegativeInt = 0
    cost_amount: Decimal = Decimal(0)
    cost_currency: CurrencyCode = "CNY"
    cost_complete: StrictBool = True

    @field_validator("cost_amount", mode="before")
    @classmethod
    def _decimal_wire_value(cls, value: object) -> object:
        if not isinstance(value, (str, Decimal)):
            raise ValueError("cost_amount must use a decimal string")
        return value

    @model_validator(mode="after")
    def _valid_cost(self) -> ExperimentUsage:
        if not self.cost_amount.is_finite() or self.cost_amount < 0:
            raise ValueError("cost_amount must be finite and nonnegative")
        return self

    def add(self, usage: Usage) -> ExperimentUsage:
        """Return totals with one normalized engine usage observation added."""

        if (
            usage.cost_currency is not None
            and usage.cost_currency != self.cost_currency
        ):
            raise ValueError("engine usage currency differs from journey currency")
        return self.model_copy(
            update={
                "input_tokens": self.input_tokens + usage.input_tokens,
                "output_tokens": self.output_tokens + usage.output_tokens,
                "cost_amount": self.cost_amount
                + (usage.cost_amount if usage.cost_amount is not None else Decimal(0)),
                "cost_complete": self.cost_complete and usage.cost_amount is not None,
            }
        )

    @classmethod
    def from_usage(
        cls,
        usage: Usage,
        *,
        cost_currency: str,
    ) -> ExperimentUsage:
        """Project one evidence-derived cumulative usage into dashboard totals."""

        if usage.cost_currency is not None and usage.cost_currency != cost_currency:
            raise ValueError("engine usage currency differs from journey currency")
        return cls(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_amount=(
                usage.cost_amount if usage.cost_amount is not None else Decimal(0)
            ),
            cost_currency=cost_currency,
            cost_complete=usage.cost_amount is not None,
        )


class StationProgress(JourneyModel):
    """Current projection for one course station."""

    number: StationNumber
    status: JourneyProgressStatus
    command: NonEmptyStr
    decision_refs: tuple[ArtifactRef, ...] = ()
    artifact_refs: tuple[ArtifactRef, ...] = ()
    started_at: UtcDateTime | None = None
    completed_at: UtcDateTime | None = None
    updated_at: UtcDateTime
    attention_reason: NonEmptyStr | None = None

    @field_validator("command", "attention_reason")
    @classmethod
    def _credential_free_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _reject_sensitive_text(value)

    @field_validator("decision_refs", "artifact_refs")
    @classmethod
    def _safe_unique_refs(
        cls, value: tuple[ArtifactRef, ...]
    ) -> tuple[ArtifactRef, ...]:
        identities: set[tuple[str, str]] = set()
        for reference in value:
            _reject_sensitive_text(reference.path)
            if is_sensitive_name(reference.path):
                raise ValueError(
                    "station reference cannot identify credential material"
                )
            identity = (reference.root.value, reference.path)
            if identity in identities:
                raise ValueError("station references must use unique root/path pairs")
            identities.add(identity)
        return value

    @model_validator(mode="after")
    def _valid_lifecycle(self) -> StationProgress:
        if self.started_at is not None and self.started_at > self.updated_at:
            raise ValueError("station started_at cannot follow updated_at")
        if self.completed_at is not None and self.completed_at > self.updated_at:
            raise ValueError("station completed_at cannot follow updated_at")
        if (
            self.started_at is not None
            and self.completed_at is not None
            and self.completed_at < self.started_at
        ):
            raise ValueError("station completed_at cannot precede started_at")

        if self.status is JourneyProgressStatus.PENDING:
            if (
                self.started_at is not None
                or self.completed_at is not None
                or self.attention_reason is not None
                or self.decision_refs
                or self.artifact_refs
            ):
                raise ValueError("pending station cannot contain run state")
        elif self.status is JourneyProgressStatus.RUNNING:
            if self.started_at is None or self.completed_at is not None:
                raise ValueError("running station requires only started_at")
            if self.attention_reason is not None:
                raise ValueError("running station cannot have an attention reason")
        elif self.status is JourneyProgressStatus.COMPLETED:
            if self.started_at is None or self.completed_at is None:
                raise ValueError("completed station requires both run timestamps")
            if self.attention_reason is not None:
                raise ValueError("completed station cannot have an attention reason")
        elif self.status is JourneyProgressStatus.NEEDS_ATTENTION:
            if self.started_at is None or self.completed_at is not None:
                raise ValueError("attention station requires only started_at")
            if self.attention_reason is None:
                raise ValueError("attention station requires a reason")
        return self


class JourneyStatus(VersionedRecord):
    """Producer-owned snapshot consumed by the local read-only dashboard."""

    model_config = ConfigDict(hide_input_in_errors=True)

    record_type: Literal["journey_status"]
    experiment_mode: ExperimentMode = ExperimentMode.LIVE
    experiment_provider: ProviderId | None = ProviderId.SILICONFLOW
    model_lock_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = (
        "0" * 64
    )
    cost_source: ExperimentCostSource = ExperimentCostSource.CLAUDE_CODE_ESTIMATE
    status: JourneyProgressStatus
    current_station: StationNumber
    stations: Annotated[
        tuple[StationProgress, ...],
        Field(min_length=STATION_COUNT, max_length=STATION_COUNT),
    ]
    experiment_usage: ExperimentUsage
    created_at: UtcDateTime
    updated_at: UtcDateTime

    @model_validator(mode="after")
    def _valid_projection(self) -> JourneyStatus:
        if self.experiment_mode is ExperimentMode.FIXED:
            if (
                self.experiment_provider is not None
                or self.model_lock_sha256 is not None
            ):
                raise ValueError(
                    "fixed journey cannot bind a live provider or model lock"
                )
            if self.cost_source is not ExperimentCostSource.SYNTHETIC_CI:
                raise ValueError("fixed journey must label cost as synthetic CI")
        else:
            if self.experiment_provider is None or self.model_lock_sha256 is None:
                raise ValueError("live journey requires a provider and model lock")
            if self.cost_source is ExperimentCostSource.SYNTHETIC_CI:
                raise ValueError("live journey cannot label cost as synthetic CI")
        numbers = tuple(station.number for station in self.stations)
        if numbers != STATION_NUMBERS:
            raise ValueError("stations must be ordered exactly from 0 through 7")
        if self.updated_at < self.created_at:
            raise ValueError("journey updated_at cannot precede created_at")
        if any(station.updated_at > self.updated_at for station in self.stations):
            raise ValueError("station updated_at cannot follow journey updated_at")

        all_pending = all(
            station.status is JourneyProgressStatus.PENDING for station in self.stations
        )
        all_completed = all(
            station.status is JourneyProgressStatus.COMPLETED
            for station in self.stations
        )
        current_status = self.stations[self.current_station].status

        if self.status is JourneyProgressStatus.PENDING:
            if not all_pending or self.current_station != 0:
                raise ValueError("pending journey must be untouched at station 0")
        elif self.status is JourneyProgressStatus.COMPLETED:
            if not all_completed or self.current_station != STATION_COUNT - 1:
                raise ValueError("completed journey requires all eight stations")
        elif self.status is JourneyProgressStatus.NEEDS_ATTENTION:
            if current_status is not JourneyProgressStatus.NEEDS_ATTENTION:
                raise ValueError("current station must need attention")
        elif self.status is JourneyProgressStatus.RUNNING:
            if all_pending or all_completed:
                raise ValueError("running journey must contain recorded progress")
            if current_status is JourneyProgressStatus.NEEDS_ATTENTION:
                raise ValueError("attention at current station must be visible")
        return self


def initial_journey_status(
    *,
    commands: tuple[str, ...],
    now: datetime,
    cost_currency: str,
    experiment_mode: ExperimentMode = ExperimentMode.LIVE,
    experiment_provider: ProviderId | None = ProviderId.SILICONFLOW,
    model_lock_sha256: str | None = "0" * 64,
    cost_source: ExperimentCostSource = ExperimentCostSource.CLAUDE_CODE_ESTIMATE,
) -> JourneyStatus:
    """Build an untouched eight-station status snapshot."""

    if len(commands) != STATION_COUNT:
        raise ValueError("journey requires exactly eight station commands")
    stations = tuple(
        StationProgress(
            number=number,
            status=JourneyProgressStatus.PENDING,
            command=command,
            updated_at=now,
        )
        for number, command in enumerate(commands)
    )
    return JourneyStatus(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="journey_status",
        experiment_mode=experiment_mode,
        experiment_provider=experiment_provider,
        model_lock_sha256=model_lock_sha256,
        cost_source=cost_source,
        status=JourneyProgressStatus.PENDING,
        current_station=0,
        stations=stations,
        experiment_usage=ExperimentUsage(
            cost_amount=Decimal(0),
            cost_currency=cost_currency,
            cost_complete=cost_source is not ExperimentCostSource.UNAVAILABLE,
        ),
        created_at=now,
        updated_at=now,
    )


__all__ = [
    "DEFAULT_STATION_COMMANDS",
    "STATION_COUNT",
    "STATION_NUMBERS",
    "ExperimentCostSource",
    "ExperimentMode",
    "ExperimentUsage",
    "JourneyProgressStatus",
    "JourneyStatus",
    "StationNumber",
    "StationProgress",
    "initial_journey_status",
]
