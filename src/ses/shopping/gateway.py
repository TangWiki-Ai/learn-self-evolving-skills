"""Case-local MCP gateway and one-use turn lease enforcement."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Literal

from ses.contracts.artifact import ArtifactRef, ArtifactRoot
from ses.contracts.primitives import SchemaVersion
from ses.contracts.serialization import artifact_json_bytes, content_sha256
from ses.contracts.shopping import (
    EpisodeStep,
    PurchaseAttemptReceipt,
    ShoppingAction,
    ShoppingActionKind,
    ShoppingActionOffer,
    ShoppingActionReceipt,
    ShoppingActionRequest,
    ShoppingObservation,
    ShoppingPurchaseOffer,
    ShoppingScenario,
    TurnLease,
)
from ses.shopping.adapters import ShoppingEpisode


class ShoppingGatewayError(ValueError):
    """Stable Agent protocol failure with no hidden bridge details."""


class TurnLeaseConsumedError(ShoppingGatewayError):
    """A turn attempted more than one shopping tool action."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _observation_sha256(observation: ShoppingObservation) -> str:
    return hashlib.sha256(
        _canonical_bytes(observation.model_dump(mode="json"))
    ).hexdigest()


def _write_once(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    return hashlib.sha256(payload).hexdigest()


class ShoppingMCPGateway:
    """The sole executor of environment actions for one shopping episode."""

    def __init__(self, *, episode: ShoppingEpisode, artifact_root: Path) -> None:
        self._episode = episode
        self._artifact_root = artifact_root.resolve()
        self._observation = episode.start.observation
        self._scenario = episode.start.scenario
        self._active_lease: TurnLease | None = None
        self._consumed: set[str] = set()
        self._click_labels: dict[str, str] = {}
        self._purchase_labels: dict[str, str] = {}
        self._purchase_offers: dict[str, ShoppingPurchaseOffer] = {}
        self._receipt: ShoppingActionReceipt | None = None
        self._receipt_ref: ArtifactRef | None = None
        self._receipts: list[tuple[ShoppingActionReceipt, ArtifactRef]] = []
        self._purchase_attempts: list[tuple[PurchaseAttemptReceipt, ArtifactRef]] = []
        self._last_step: EpisodeStep | None = None
        self._last_turn_sequence = 0
        self._purchase_authorized = episode.start.initial_authorization
        self._authorization_offer_sha256: str | None = None
        self._authorization_message_refs: tuple[ArtifactRef, ...] = ()
        self.violation_reason: str | None = None

    @property
    def receipt(self) -> ShoppingActionReceipt | None:
        return self._receipt

    @property
    def receipt_ref(self) -> ArtifactRef | None:
        return self._receipt_ref

    @property
    def receipts(self) -> tuple[tuple[ShoppingActionReceipt, ArtifactRef], ...]:
        return tuple(self._receipts)

    @property
    def purchase_attempts(
        self,
    ) -> tuple[tuple[PurchaseAttemptReceipt, ArtifactRef], ...]:
        return tuple(self._purchase_attempts)

    @property
    def last_step(self) -> EpisodeStep | None:
        return self._last_step

    @property
    def current_observation(self) -> ShoppingObservation:
        return self._observation

    @property
    def initial_authorization(self) -> bool:
        return self._episode.start.initial_authorization

    def issue_turn(self, *, turn_sequence: int) -> TurnLease:
        if self._active_lease is not None and (
            self._active_lease.lease_id not in self._consumed
        ):
            raise ShoppingGatewayError("previous turn lease has no shopping action")
        if turn_sequence != self._last_turn_sequence + 1:
            raise ShoppingGatewayError("turn lease sequence must be contiguous")
        observation_sha256 = _observation_sha256(self._observation)
        lease_id = hashlib.sha256(
            f"{self._episode.start.episode_nonce}:{turn_sequence}:{observation_sha256}".encode()
        ).hexdigest()
        self._click_labels = {}
        self._purchase_labels = {}
        self._purchase_offers = {}
        click_offers: list[ShoppingActionOffer] = []
        purchase_offer: ShoppingActionOffer | None = None
        for visible in self._observation.available_actions:
            label = visible.label
            risk: Literal["ordinary", "purchase"] = (
                "purchase" if visible.kind == "purchase" else "ordinary"
            )
            action_id = self._action_id(
                turn_sequence=turn_sequence,
                observation_sha256=observation_sha256,
                label=label,
                risk=risk,
            )
            offer = ShoppingActionOffer(
                action_id=action_id,
                label=label,
                risk=risk,
            )
            if risk == "purchase":
                if visible.purchase_offer is None:
                    raise ShoppingGatewayError(
                        "purchase action is missing trusted offer details"
                    )
                if purchase_offer is not None:
                    raise ShoppingGatewayError(
                        "observation exposes multiple purchase actions"
                    )
                purchase_offer = offer
                self._purchase_labels[action_id] = label
                self._purchase_offers[action_id] = visible.purchase_offer
            else:
                click_offers.append(offer)
                self._click_labels[action_id] = label
        lease = TurnLease(
            lease_id=f"lease-{lease_id}",
            episode_nonce=self._episode.start.episode_nonce,
            turn_sequence=turn_sequence,
            observation_sha256=observation_sha256,
            click_actions=tuple(click_offers),
            purchase_action=purchase_offer,
        )
        self._active_lease = lease
        self._last_turn_sequence = turn_sequence
        self._receipt = None
        self._receipt_ref = None
        self.violation_reason = None
        return lease

    def execute(
        self, lease: TurnLease, request: ShoppingActionRequest
    ) -> ShoppingActionReceipt:
        if self._active_lease is None or lease.lease_id != self._active_lease.lease_id:
            raise ShoppingGatewayError("turn lease does not match current observation")
        if lease.lease_id in self._consumed:
            self.violation_reason = "multiple_actions_in_turn"
            raise TurnLeaseConsumedError(
                "one turn lease permits only one shopping action"
            )
        self._consumed.add(lease.lease_id)
        try:
            action = self._trusted_action(request)
        except ShoppingGatewayError:
            if self.violation_reason is None:
                self.violation_reason = "action_not_bound_to_current_observation"
            raise

        relative_intent = (
            Path("action-intents") / f"turn-{lease.turn_sequence:04d}.json"
        )
        intent_payload = _canonical_bytes(
            {
                "schema_version": "v1alpha1",
                "record_type": "shopping_action_intent",
                "episode_nonce": lease.episode_nonce,
                "turn_lease_id": lease.lease_id,
                "turn_sequence": lease.turn_sequence,
                "observation_sha256": lease.observation_sha256,
                "request": request.model_dump(mode="json"),
            }
        )
        intent = ArtifactRef(
            root=ArtifactRoot.WORKSPACE,
            path=relative_intent.as_posix(),
            sha256=_write_once(self._artifact_root / relative_intent, intent_payload),
        )

        purchase_offer_at_action = (
            self._purchase_offers.get(request.action_id)
            if request.kind is ShoppingActionKind.PURCHASE
            and request.action_id is not None
            else None
        )
        offer_sha256_at_action = (
            content_sha256(purchase_offer_at_action)
            if purchase_offer_at_action is not None
            else None
        )
        shopper_state_at_action = self._observation.shopper_state
        authorized_at_action = self._purchase_authorized and (
            self._authorization_offer_sha256 is None
            or self._authorization_offer_sha256 == offer_sha256_at_action
        )
        if shopper_state_at_action != "active":
            authorized_at_action = False
        authorization_refs_at_action = self._authorization_message_refs
        authorization_offer_at_action = self._authorization_offer_sha256
        step = self._episode.step(action)
        relative_result = (
            Path("action-results") / f"turn-{lease.turn_sequence:04d}.json"
        )
        step_payload = step.model_dump(mode="json", exclude={"raw_reward"})
        step_payload["raw_reward_present"] = step.raw_reward is not None
        result_payload = _canonical_bytes(step_payload)
        result = ArtifactRef(
            root=ArtifactRoot.WORKSPACE,
            path=relative_result.as_posix(),
            sha256=_write_once(self._artifact_root / relative_result, result_payload),
        )
        self._observation = step.observation
        if step.authorization_update is not None:
            self._purchase_authorized = step.authorization_update.state == "granted"
            self._authorization_offer_sha256 = (
                step.authorization_update.offer_sha256
                if step.authorization_update.state == "granted"
                else None
            )
            self._authorization_message_refs = (step.authorization_update.evidence,)
        receipt = ShoppingActionReceipt(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type="shopping_action_receipt",
            episode_nonce=lease.episode_nonce,
            turn_lease_id=lease.lease_id,
            turn_sequence=lease.turn_sequence,
            observation_sha256=lease.observation_sha256,
            action_kind=request.kind,
            request=request,
            intent=intent,
            result=result,
            step_terminal=step.terminal,
        )
        relative_receipt = (
            Path("action-receipts") / f"turn-{lease.turn_sequence:04d}.json"
        )
        receipt_ref = ArtifactRef(
            root=ArtifactRoot.WORKSPACE,
            path=relative_receipt.as_posix(),
            sha256=_write_once(
                self._artifact_root / relative_receipt,
                artifact_json_bytes(receipt),
            ),
        )
        self._receipt = receipt
        self._receipt_ref = receipt_ref
        self._receipts.append((receipt, receipt_ref))
        if request.kind is ShoppingActionKind.PURCHASE:
            assert request.action_id is not None
            assert purchase_offer_at_action is not None
            assert offer_sha256_at_action is not None
            purchase_attempt = PurchaseAttemptReceipt(
                schema_version=SchemaVersion.V1ALPHA1,
                record_type="purchase_attempt_receipt",
                episode_nonce=lease.episode_nonce,
                turn_lease_id=lease.lease_id,
                turn_sequence=lease.turn_sequence,
                observation_sha256=lease.observation_sha256,
                action_id=request.action_id,
                intent=intent,
                result=result,
                authorized=authorized_at_action,
                authorization_message_refs=authorization_refs_at_action,
                offer_sha256=offer_sha256_at_action,
                authorization_offer_sha256=authorization_offer_at_action,
                product_id=purchase_offer_at_action.product_id,
                option=purchase_offer_at_action.option,
                quantity=purchase_offer_at_action.quantity,
                price_amount_minor=purchase_offer_at_action.price_amount_minor,
                price_currency=purchase_offer_at_action.price_currency,
                detail_viewed=purchase_offer_at_action.detail_viewed,
                eligible=purchase_offer_at_action.eligible,
                catalog_instruction_detected=(
                    purchase_offer_at_action.catalog_instruction_detected
                ),
                shopper_state=shopper_state_at_action,
                protocol_sha256=hashlib.sha256(b"ses-shopping-purchase-v1").hexdigest(),
            )
            relative_purchase = (
                Path("purchase-attempts") / f"turn-{lease.turn_sequence:04d}.json"
            )
            purchase_ref = ArtifactRef(
                root=ArtifactRoot.WORKSPACE,
                path=relative_purchase.as_posix(),
                sha256=_write_once(
                    self._artifact_root / relative_purchase,
                    artifact_json_bytes(purchase_attempt),
                ),
            )
            self._purchase_attempts.append((purchase_attempt, purchase_ref))
        self._last_step = step
        return receipt

    def _trusted_action(self, request: ShoppingActionRequest) -> ShoppingAction:
        if request.kind is ShoppingActionKind.SEARCH:
            if not self._observation.allows_search:
                self.violation_reason = "search_not_allowed"
                raise ShoppingGatewayError(
                    "search is not allowed by the current observation"
                )
            assert request.query is not None
            return ShoppingAction.search(request.query)
        if request.kind is ShoppingActionKind.CLICK:
            assert request.action_id is not None
            if request.action_id in self._purchase_labels:
                self.violation_reason = "purchase_requires_purchase_action"
                raise ShoppingGatewayError(
                    "purchase action cannot execute through ordinary click"
                )
            label = self._click_labels.get(request.action_id)
            if label is None:
                raise ShoppingGatewayError(
                    "click action is not bound to the current observation"
                )
            return ShoppingAction.click(label)
        if request.kind is ShoppingActionKind.PURCHASE:
            assert request.action_id is not None
            label = self._purchase_labels.get(request.action_id)
            if label is None:
                raise ShoppingGatewayError(
                    "purchase action is not bound to the current observation"
                )
            return ShoppingAction.purchase(label)
        if request.kind is ShoppingActionKind.ASK_SHOPPER:
            if self._scenario not in {
                ShoppingScenario.MULTI,
                ShoppingScenario.MULTI_PERSONA,
            }:
                self.violation_reason = "ask_shopper_not_allowed"
                raise ShoppingGatewayError(
                    "ask_shopper is unavailable in a single scenario"
                )
            if not self._observation.allows_ask_shopper:
                self.violation_reason = "ask_shopper_not_allowed"
                raise ShoppingGatewayError(
                    "ask_shopper is not allowed by the current observation"
                )
            assert request.question is not None
            return ShoppingAction.ask_shopper(request.question)
        assert request.reason is not None
        return ShoppingAction.finish_without_purchase(request.reason)

    def _action_id(
        self,
        *,
        turn_sequence: int,
        observation_sha256: str,
        label: str,
        risk: str,
    ) -> str:
        digest = hashlib.sha256(
            _canonical_bytes(
                {
                    "episode_nonce": self._episode.start.episode_nonce,
                    "step_sequence": turn_sequence,
                    "observation_sha256": observation_sha256,
                    "label": label,
                    "risk": risk,
                }
            )
        ).hexdigest()
        return f"action-{digest}"
