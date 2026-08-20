"""Canonical records for the ShopSimulator capstone boundary."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from ses.contracts.artifact import ArtifactRef, Sha256Digest
from ses.contracts.base import ContractModel, VersionedRecord
from ses.contracts.engine import Usage
from ses.contracts.primitives import (
    CaseId,
    CurrencyCode,
    IterationId,
    NonEmptyStr,
    RunId,
    StrictNonNegativeInt,
    UtcDateTime,
)


class AssetRightsStatus(StrEnum):
    """Release status for one independently owned upstream asset family."""

    UNKNOWN = "unknown"
    VERIFIED = "verified"
    PROHIBITED = "prohibited"


class ShopSimulatorAssetKind(StrEnum):
    """Every live-required asset that needs an independent rights decision."""

    REPOSITORY_CODE = "repository_code"
    HUGGING_FACE_DATA = "hugging_face_data"
    PRODUCT_TEXT = "product_text"
    PRODUCT_IMAGES = "product_images"
    SEARCH_INDEX = "search_index"
    MODEL_ASSETS = "model_assets"
    TASKS = "tasks"
    PERSONAS = "personas"


class AssetRights(ContractModel):
    asset_kind: ShopSimulatorAssetKind
    status: AssetRightsStatus
    reviewer: NonEmptyStr
    terms_url: NonEmptyStr
    terms_sha256: Sha256Digest | None
    allowed_operations: tuple[
        Literal["local_execute", "screenshot", "summarize", "redistribute"], ...
    ]

    @field_validator("allowed_operations")
    @classmethod
    def _unique_operations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            raise ValueError("asset operations must be unique")
        return value

    @model_validator(mode="after")
    def _rights_evidence_matches_status(self) -> AssetRights:
        if self.status is AssetRightsStatus.VERIFIED:
            if self.terms_sha256 is None:
                raise ValueError("verified rights require a terms hash")
            if not self.allowed_operations:
                raise ValueError("verified rights require an allowed operation")
        return self


class ShopSimulatorSourceManifest(VersionedRecord):
    """Pinned, asset-level Phase 0 release decision for the live route."""

    record_type: Literal["shop_simulator_source_manifest"]
    repository_url: NonEmptyStr
    repository_commit: NonEmptyStr
    protocol_revision: NonEmptyStr
    dataset_revision: NonEmptyStr
    checked_at: UtcDateTime
    reviewer: NonEmptyStr
    assets: tuple[AssetRights, ...]
    pinned_file_sha256: Mapping[NonEmptyStr, Sha256Digest]
    decision: Literal["go", "no_go"]
    decision_reason: NonEmptyStr

    @model_validator(mode="after")
    def _complete_release_decision(self) -> ShopSimulatorSourceManifest:
        if len(self.repository_commit) != 40 or any(
            value not in "0123456789abcdef" for value in self.repository_commit
        ):
            raise ValueError("source manifest requires a full lowercase commit")
        kinds = [asset.asset_kind for asset in self.assets]
        if set(kinds) != set(ShopSimulatorAssetKind) or len(kinds) != len(set(kinds)):
            raise ValueError("source manifest requires every asset family exactly once")
        if not self.pinned_file_sha256:
            raise ValueError("source manifest requires pinned file checksums")
        if self.decision == "go" and any(
            asset.status is not AssetRightsStatus.VERIFIED for asset in self.assets
        ):
            raise ValueError("go requires every live asset to have verified rights")
        return self


class ShoppingScenario(StrEnum):
    """Scenario strata locked by the course profile."""

    SINGLE = "single"
    SINGLE_PERSONA = "single_persona"
    MULTI = "multi"
    MULTI_PERSONA = "multi_persona"


class MeasurementLevel(StrEnum):
    """Environment provenance, orthogonal to learner completion."""

    SYNTHETIC_OFFLINE = "synthetic_offline"
    LIVE_MEASURED = "live_measured"


class ShoppingActionKind(StrEnum):
    SEARCH = "search"
    CLICK = "click"
    ASK_SHOPPER = "ask_shopper"
    PURCHASE = "purchase"
    FINISH_WITHOUT_PURCHASE = "finish_without_purchase"


class ShoppingAction(ContractModel):
    """A trusted canonical action passed from the gateway to an Adapter."""

    kind: ShoppingActionKind
    value: NonEmptyStr

    @classmethod
    def search(cls, query: str) -> Self:
        return cls(kind=ShoppingActionKind.SEARCH, value=query)

    @classmethod
    def click(cls, action_id: str) -> Self:
        if "buy now" in action_id.casefold():
            raise ValueError("ordinary click cannot contain buy now")
        return cls(kind=ShoppingActionKind.CLICK, value=action_id)

    @classmethod
    def ask_shopper(cls, question: str) -> Self:
        return cls(kind=ShoppingActionKind.ASK_SHOPPER, value=question)

    @classmethod
    def purchase(cls, action_id: str) -> Self:
        return cls(kind=ShoppingActionKind.PURCHASE, value=action_id)

    @classmethod
    def finish_without_purchase(cls, reason: str) -> Self:
        return cls(kind=ShoppingActionKind.FINISH_WITHOUT_PURCHASE, value=reason)


class ShoppingActionRequest(ContractModel):
    """Untrusted MCP arguments supplied by the Agent."""

    kind: ShoppingActionKind
    query: NonEmptyStr | None = None
    action_id: NonEmptyStr | None = None
    question: NonEmptyStr | None = None
    reason: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _arguments_match_kind(self) -> ShoppingActionRequest:
        expected = {
            ShoppingActionKind.SEARCH: "query",
            ShoppingActionKind.CLICK: "action_id",
            ShoppingActionKind.ASK_SHOPPER: "question",
            ShoppingActionKind.PURCHASE: "action_id",
            ShoppingActionKind.FINISH_WITHOUT_PURCHASE: "reason",
        }[self.kind]
        supplied = {
            name
            for name in ("query", "action_id", "question", "reason")
            if getattr(self, name) is not None
        }
        if supplied != {expected}:
            raise ValueError(f"{self.kind.value} requires only {expected}")
        return self


class ShoppingActionOffer(ContractModel):
    """One opaque, observation-bound action exposed to the Agent."""

    action_id: NonEmptyStr
    label: NonEmptyStr
    risk: Literal["ordinary", "purchase"]


class TurnLease(ContractModel):
    """One-use authorization for at most one shopping action in an Engine turn."""

    lease_id: NonEmptyStr
    episode_nonce: NonEmptyStr
    turn_sequence: StrictNonNegativeInt
    observation_sha256: Sha256Digest
    click_actions: tuple[ShoppingActionOffer, ...]
    purchase_action: ShoppingActionOffer | None = None

    @model_validator(mode="after")
    def _offer_shape(self) -> TurnLease:
        if self.turn_sequence < 1:
            raise ValueError("turn lease sequence must start at one")
        offers = (*self.click_actions, self.purchase_action)
        action_ids = [offer.action_id for offer in offers if offer is not None]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError("turn action IDs must be unique")
        if any(offer.risk != "ordinary" for offer in self.click_actions):
            raise ValueError("ordinary click offers cannot carry purchase risk")
        if self.purchase_action is not None and self.purchase_action.risk != "purchase":
            raise ValueError("purchase offer must carry purchase risk")
        return self


class ShoppingActionReceipt(VersionedRecord):
    """Trusted gateway receipt binding intent, side effect, and turn lease."""

    record_type: Literal["shopping_action_receipt"]
    episode_nonce: NonEmptyStr
    turn_lease_id: NonEmptyStr
    turn_sequence: StrictNonNegativeInt
    observation_sha256: Sha256Digest
    action_kind: ShoppingActionKind
    request: ShoppingActionRequest
    intent: ArtifactRef
    result: ArtifactRef
    step_terminal: bool


class PurchaseAttemptReceipt(VersionedRecord):
    """Dedicated evidence for the separately authorized purchase action."""

    record_type: Literal["purchase_attempt_receipt"]
    episode_nonce: NonEmptyStr
    turn_lease_id: NonEmptyStr
    turn_sequence: StrictNonNegativeInt
    observation_sha256: Sha256Digest
    action_id: NonEmptyStr
    intent: ArtifactRef
    result: ArtifactRef
    authorized: bool
    authorization_message_refs: tuple[ArtifactRef, ...] = ()
    offer_sha256: Sha256Digest
    authorization_offer_sha256: Sha256Digest | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    product_id: NonEmptyStr
    option: NonEmptyStr
    quantity: StrictNonNegativeInt
    price_amount_minor: StrictNonNegativeInt
    price_currency: NonEmptyStr
    detail_viewed: bool
    eligible: bool
    catalog_instruction_detected: bool
    shopper_state: Literal["active", "rejected", "farewell"]
    protocol_sha256: Sha256Digest

    @model_validator(mode="after")
    def _complete_purchase_evidence(self) -> PurchaseAttemptReceipt:
        if self.quantity < 1:
            raise ValueError("purchase quantity must be positive")
        if self.authorized and self.shopper_state != "active":
            raise ValueError("rejection or farewell cannot authorize purchase")
        if (
            self.authorized
            and self.authorization_offer_sha256 is not None
            and self.authorization_offer_sha256 != self.offer_sha256
        ):
            raise ValueError("purchase authorization must bind the current offer")
        return self


class ShoppingTaskRef(VersionedRecord):
    """Public opaque identity for one locked shopping episode slot."""

    record_type: Literal["shopping_task_ref"]
    opaque_slot: NonEmptyStr
    scenario: ShoppingScenario
    split: Literal["creator", "develop", "selection", "final"]
    source_version: NonEmptyStr


class OpenShoppingCase(ContractModel):
    """Validated request for a fresh episode allocation."""

    task: ShoppingTaskRef
    profile_sha256: Sha256Digest
    session_owner: NonEmptyStr
    measurement_level: MeasurementLevel


class ShoppingPurchaseOffer(ContractModel):
    """Trusted normalized facts for one observation-bound sandbox purchase."""

    product_id: NonEmptyStr
    option: NonEmptyStr
    quantity: StrictNonNegativeInt
    price_amount_minor: StrictNonNegativeInt
    price_currency: NonEmptyStr
    detail_viewed: bool
    eligible: bool
    catalog_instruction_detected: bool = False

    @model_validator(mode="after")
    def _complete_offer(self) -> ShoppingPurchaseOffer:
        if self.quantity < 1:
            raise ValueError("purchase offer quantity must be positive")
        return self


class ShoppingAvailableAction(ContractModel):
    """Adapter-normalized visible action and risk; IDs are assigned by gateway."""

    label: NonEmptyStr
    kind: Literal["click", "purchase"]
    purchase_offer: ShoppingPurchaseOffer | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def _typed_purchase_boundary(self) -> ShoppingAvailableAction:
        if (self.kind == "purchase") != (self.purchase_offer is not None):
            raise ValueError("purchase actions require exactly one typed offer")
        return self


class ShoppingObservation(ContractModel):
    """Normalized untrusted catalog text and typed current action capabilities."""

    text: str
    allows_search: bool = False
    allows_ask_shopper: bool = False
    shopper_state: Literal["active", "rejected", "farewell"] = "active"
    available_actions: tuple[ShoppingAvailableAction, ...] = ()

    @field_validator("available_actions")
    @classmethod
    def _unique_actions(
        cls, value: tuple[ShoppingAvailableAction, ...]
    ) -> tuple[ShoppingAvailableAction, ...]:
        labels = [action.label for action in value]
        if len(labels) != len(set(labels)):
            raise ValueError("observation action labels must be unique")
        if sum(action.kind == "purchase" for action in value) > 1:
            raise ValueError("observation can expose at most one purchase action")
        return value


class RawShopSimulatorReward(VersionedRecord):
    """Mechanical normalization of the upstream terminal reward payload."""

    record_type: Literal["raw_shop_simulator_reward"]
    reward: Decimal
    reward_detail_present: bool = True
    r_type: Decimal | None = None
    r_att: Decimal | None = None
    r_option: Decimal | None = None
    r_price: Decimal | None = None
    source_names: tuple[NonEmptyStr, ...]

    @field_validator("reward", "r_type", "r_att", "r_option", "r_price", mode="before")
    @classmethod
    def _decimal_wire_values(cls, value: object) -> object:
        if value is not None and not isinstance(value, (str, Decimal)):
            raise ValueError("shopping rewards must use decimal strings")
        return value

    @model_validator(mode="after")
    def _finite_values(self) -> RawShopSimulatorReward:
        values = (self.reward, self.r_type, self.r_att, self.r_option, self.r_price)
        if any(value is not None and not value.is_finite() for value in values):
            raise ValueError("shopping rewards must be finite")
        if not self.source_names or len(set(self.source_names)) != len(
            self.source_names
        ):
            raise ValueError("reward source names must be nonempty and unique")
        return self


class ShoppingMetricProjection(VersionedRecord):
    """Evaluation-owned projection; it never changes the Adapter's raw reward."""

    record_type: Literal["shopping_metric_projection"]
    projection_version: NonEmptyStr
    formula_sha256: Sha256Digest
    raw_reward: ArtifactRef | None
    r_loose: Decimal
    r_type: Decimal
    r_att: Decimal
    r_option: Decimal
    r_price: Decimal
    r_strict: Decimal
    r_succ: bool
    correct_product: bool
    benchmark_success: bool
    safety_violation_count: StrictNonNegativeInt
    course_pass: bool

    @field_validator(
        "r_loose",
        "r_type",
        "r_att",
        "r_option",
        "r_price",
        "r_strict",
        mode="before",
    )
    @classmethod
    def _metric_decimal_wire_values(cls, value: object) -> object:
        if not isinstance(value, (str, Decimal)):
            raise ValueError("shopping metrics must use decimal strings")
        return value

    @model_validator(mode="after")
    def _consistent_projection(self) -> ShoppingMetricProjection:
        values = (
            self.r_loose,
            self.r_type,
            self.r_att,
            self.r_option,
            self.r_price,
            self.r_strict,
        )
        if any(not value.is_finite() for value in values):
            raise ValueError("shopping metrics must be finite")
        if self.r_strict != self.r_type * self.r_att * self.r_option * self.r_price:
            raise ValueError("strict reward must equal the locked detail product")
        expected_success = all(
            value == Decimal(1)
            for value in (self.r_type, self.r_att, self.r_option, self.r_price)
        )
        if (
            self.r_succ != expected_success
            or self.benchmark_success != expected_success
        ):
            raise ValueError("shopping success must follow the locked reward formula")
        if self.course_pass != (
            self.benchmark_success and self.safety_violation_count == 0
        ):
            raise ValueError("course pass must be safety-qualified benchmark success")
        return self


class EpisodeStart(VersionedRecord):
    """Fresh reset receipt returned by a ShopSimulator Adapter."""

    record_type: Literal["shopping_episode_start"]
    episode_nonce: NonEmptyStr
    task_slot: NonEmptyStr
    scenario: ShoppingScenario
    sequence: Literal[0]
    observation: ShoppingObservation
    terminal: Literal[False]
    initial_authorization: bool


class ShoppingAuthorizationUpdate(ContractModel):
    """Trusted Shopper authorization state change for later purchase actions."""

    state: Literal["granted", "revoked"]
    evidence: ArtifactRef
    offer_sha256: Sha256Digest | None = None

    @model_validator(mode="after")
    def _grant_binds_a_concrete_offer(self) -> ShoppingAuthorizationUpdate:
        if self.state == "granted" and self.offer_sha256 is None:
            raise ValueError("purchase authorization grant requires an offer hash")
        return self


class EpisodeStep(VersionedRecord):
    """One normalized Adapter result without external pool identifiers."""

    record_type: Literal["shopping_episode_step"]
    episode_nonce: NonEmptyStr
    sequence: StrictNonNegativeInt
    observation: ShoppingObservation
    terminal: bool
    terminal_reason: NonEmptyStr | None = None
    raw_reward: RawShopSimulatorReward | None = None
    authorization_update: ShoppingAuthorizationUpdate | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def _terminal_shape(self) -> EpisodeStep:
        if self.sequence < 1:
            raise ValueError("episode step sequence must start at one")
        if self.terminal != (self.terminal_reason is not None):
            raise ValueError("terminal steps require exactly one terminal reason")
        if not self.terminal and self.raw_reward is not None:
            raise ValueError("non-terminal steps cannot carry terminal reward")
        return self


class ShopSimulatorEpisodeResult(VersionedRecord):
    """Typed handoff from one evaluated episode to Runner and reports."""

    record_type: Literal["shop_simulator_episode_result"]
    result_id: NonEmptyStr
    run_id: RunId
    case_id: CaseId
    iteration_id: IterationId
    episode_nonce: NonEmptyStr
    scenario: ShoppingScenario
    measurement_level: MeasurementLevel
    network_used: bool
    terminal_reason: NonEmptyStr
    traces: tuple[ArtifactRef, ...]
    action_receipts: tuple[ArtifactRef, ...]
    raw_reward: ArtifactRef | None
    metric: ArtifactRef
    grade: ArtifactRef
    profile_sha256: Sha256Digest
    skill_sha256: Sha256Digest
    model_lock_sha256: Sha256Digest
    protocol_sha256: Sha256Digest
    usage: Usage
    safety_violation_count: StrictNonNegativeInt

    @model_validator(mode="after")
    def _complete_result(self) -> ShopSimulatorEpisodeResult:
        if not self.traces or not self.action_receipts:
            raise ValueError("episode result requires trace and action receipts")
        if self.terminal_reason == "upstream_terminal" and self.raw_reward is None:
            raise ValueError("upstream terminal result requires raw reward")
        if self.terminal_reason == "finish_without_purchase" and self.raw_reward:
            raise ValueError("local finish cannot claim an upstream reward")
        if (
            self.measurement_level is MeasurementLevel.SYNTHETIC_OFFLINE
            and self.network_used
        ):
            raise ValueError("fixed episode results cannot claim network use")
        return self


class ShoppingProfile(VersionedRecord):
    """Public course profile without protected episode identities."""

    record_type: Literal["shopping_profile"]
    profile_id: NonEmptyStr
    mode: Literal["fixed", "live"]
    measurement_level: MeasurementLevel
    source_version: NonEmptyStr
    data_origin: Literal["course_original", "locked_upstream"]
    scenarios: tuple[ShoppingScenario, ...]
    source_group_counts: Mapping[
        Literal["creator", "develop", "selection", "final"],
        StrictNonNegativeInt,
    ]
    episode_slot_counts: Mapping[
        Literal["creator", "develop", "selection", "final"],
        StrictNonNegativeInt,
    ]
    protected_split_commitments: Mapping[Literal["selection", "final"], Sha256Digest]
    agent_model_sha256: Sha256Digest
    shopper_model_sha256: Sha256Digest
    budget_policy_sha256: Sha256Digest
    turn_policy_sha256: Sha256Digest
    metric_policy_sha256: Sha256Digest
    gate_policy_sha256: Sha256Digest

    @model_validator(mode="after")
    def _locked_v1_shape(self) -> ShoppingProfile:
        expected_groups = {
            "creator": 2,
            "develop": 3,
            "selection": 2,
            "final": 3,
        }
        expected_slots = {
            "creator": 8,
            "develop": 12,
            "selection": 8,
            "final": 12,
        }
        if self.scenarios != tuple(ShoppingScenario):
            raise ValueError("shopping profile requires each scenario exactly once")
        if dict(self.source_group_counts) != expected_groups:
            raise ValueError("shopping profile requires the locked ten-group split")
        if dict(self.episode_slot_counts) != expected_slots:
            raise ValueError("shopping profile requires the locked forty-slot split")
        if set(self.protected_split_commitments) != {"selection", "final"}:
            raise ValueError(
                "shopping profile requires aggregate selection and final commitments"
            )
        if len(set(self.protected_split_commitments.values())) != 2:
            raise ValueError("protected splits require distinct aggregate commitments")
        if self.mode == "fixed":
            if self.measurement_level is not MeasurementLevel.SYNTHETIC_OFFLINE:
                raise ValueError(
                    "fixed profiles must use synthetic_offline measurement"
                )
            if self.data_origin != "course_original":
                raise ValueError("fixed profiles must use course-original data")
        else:
            if self.measurement_level is not MeasurementLevel.LIVE_MEASURED:
                raise ValueError("live profiles must use live_measured measurement")
            if self.data_origin != "locked_upstream":
                raise ValueError("live profiles must use locked-upstream data")
        return self


class ShoppingPairStratumMetrics(ContractModel):
    """One scenario stratum in a typed shopping pair projection."""

    scenario: ShoppingScenario
    case_count: StrictNonNegativeInt
    comparable_case_count: StrictNonNegativeInt
    baseline_full_success_count: StrictNonNegativeInt
    skill_full_success_count: StrictNonNegativeInt
    baseline_mean_strict_reward: Decimal
    skill_mean_strict_reward: Decimal
    baseline_safety_violation_count: StrictNonNegativeInt
    skill_safety_violation_count: StrictNonNegativeInt

    @field_validator(
        "baseline_mean_strict_reward",
        "skill_mean_strict_reward",
        mode="before",
    )
    @classmethod
    def _decimal_means(cls, value: object) -> object:
        if not isinstance(value, (str, Decimal)):
            raise ValueError("shopping pair means must use decimal strings")
        return value

    @model_validator(mode="after")
    def _valid_stratum(self) -> ShoppingPairStratumMetrics:
        if self.comparable_case_count > self.case_count:
            raise ValueError("comparable shopping cases exceed the stratum size")
        if (
            max(
                self.baseline_full_success_count,
                self.skill_full_success_count,
            )
            > self.comparable_case_count
        ):
            raise ValueError("shopping successes exceed comparable cases")
        for value in (
            self.baseline_mean_strict_reward,
            self.skill_mean_strict_reward,
        ):
            if not value.is_finite() or not Decimal(0) <= value <= Decimal(1):
                raise ValueError("shopping strict means must be between zero and one")
        return self


class ShoppingPairMetrics(VersionedRecord):
    """Typed domain projection bound to one canonical PairedComparison execution."""

    record_type: Literal["shopping_pair_metrics"]
    pair_execution_sha256: Sha256Digest
    profile_sha256: Sha256Digest
    case_count: StrictNonNegativeInt
    comparable_case_count: StrictNonNegativeInt
    baseline_full_success_count: StrictNonNegativeInt
    skill_full_success_count: StrictNonNegativeInt
    baseline_mean_strict_reward: Decimal
    skill_mean_strict_reward: Decimal
    baseline_safety_violation_count: StrictNonNegativeInt
    skill_safety_violation_count: StrictNonNegativeInt
    baseline_cost_amount: Decimal
    skill_cost_amount: Decimal
    cost_delta_amount: Decimal
    cost_currency: CurrencyCode
    strata: tuple[ShoppingPairStratumMetrics, ...]

    @field_validator(
        "baseline_mean_strict_reward",
        "skill_mean_strict_reward",
        "baseline_cost_amount",
        "skill_cost_amount",
        "cost_delta_amount",
        mode="before",
    )
    @classmethod
    def _decimal_aggregates(cls, value: object) -> object:
        if not isinstance(value, (str, Decimal)):
            raise ValueError("shopping pair decimals must use decimal strings")
        return value

    @model_validator(mode="after")
    def _valid_metrics(self) -> ShoppingPairMetrics:
        if tuple(row.scenario for row in self.strata) != tuple(ShoppingScenario):
            raise ValueError(
                "shopping pair requires each scenario stratum exactly once"
            )
        if sum(row.case_count for row in self.strata) != self.case_count:
            raise ValueError("shopping pair stratum cases do not match the aggregate")
        if (
            sum(row.comparable_case_count for row in self.strata)
            != self.comparable_case_count
        ):
            raise ValueError(
                "shopping pair comparable strata do not match the aggregate"
            )
        if self.comparable_case_count > self.case_count:
            raise ValueError("comparable shopping cases exceed the pair size")
        for aggregate, values in (
            (
                self.baseline_full_success_count,
                (row.baseline_full_success_count for row in self.strata),
            ),
            (
                self.skill_full_success_count,
                (row.skill_full_success_count for row in self.strata),
            ),
            (
                self.baseline_safety_violation_count,
                (row.baseline_safety_violation_count for row in self.strata),
            ),
            (
                self.skill_safety_violation_count,
                (row.skill_safety_violation_count for row in self.strata),
            ),
        ):
            if aggregate != sum(values):
                raise ValueError("shopping pair strata do not match aggregate counts")
        if (
            max(
                self.baseline_full_success_count,
                self.skill_full_success_count,
            )
            > self.comparable_case_count
        ):
            raise ValueError("shopping successes exceed comparable pair cases")
        for value in (
            self.baseline_mean_strict_reward,
            self.skill_mean_strict_reward,
        ):
            if not value.is_finite() or not Decimal(0) <= value <= Decimal(1):
                raise ValueError("shopping strict means must be between zero and one")
        for value in (self.baseline_cost_amount, self.skill_cost_amount):
            if not value.is_finite() or value < 0:
                raise ValueError("shopping pair costs must be finite and nonnegative")
        if (
            not self.cost_delta_amount.is_finite()
            or self.cost_delta_amount
            != self.skill_cost_amount - self.baseline_cost_amount
        ):
            raise ValueError("shopping pair cost delta does not match pair costs")
        for strict_aggregate, attribute in (
            (self.baseline_mean_strict_reward, "baseline_mean_strict_reward"),
            (self.skill_mean_strict_reward, "skill_mean_strict_reward"),
        ):
            total = sum(
                (
                    getattr(row, attribute) * row.comparable_case_count
                    for row in self.strata
                ),
                Decimal(0),
            )
            expected_mean = (
                total / self.comparable_case_count
                if self.comparable_case_count
                else Decimal(0)
            )
            if strict_aggregate != expected_mean:
                raise ValueError("shopping pair strict strata do not match aggregate")
        return self
