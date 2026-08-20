"""Lifecycle-safe ShopSimulator Port implementations."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Protocol

from ses.contracts.primitives import SchemaVersion
from ses.contracts.shopping import (
    EpisodeStart,
    EpisodeStep,
    MeasurementLevel,
    OpenShoppingCase,
    ShoppingAction,
    ShoppingActionKind,
    ShoppingObservation,
    ShopSimulatorSourceManifest,
)


class EpisodeClosedError(RuntimeError):
    """The caller attempted to use a closed or terminal episode."""


class OutcomeUnknownError(RuntimeError):
    """A non-idempotent external operation lost its authoritative outcome."""


class AdapterProtocolError(RuntimeError):
    """The external bridge violated the locked episode protocol."""


class ShoppingEpisode(Protocol):
    @property
    def start(self) -> EpisodeStart: ...

    def step(self, action: ShoppingAction) -> EpisodeStep: ...

    def close(self) -> None: ...


class ShopSimulatorPort(Protocol):
    def open_episode(
        self, request: OpenShoppingCase
    ) -> AbstractContextManager[ShoppingEpisode]: ...


@dataclass(frozen=True, slots=True)
class InMemoryActionTransition:
    """One expected action and deterministic result in an original fixture."""

    expected: ShoppingAction
    step: EpisodeStep


@dataclass(frozen=True, slots=True)
class InMemoryEpisodeFixture:
    start: EpisodeStart
    steps: tuple[EpisodeStep, ...]
    transitions: tuple[InMemoryActionTransition, ...] = ()

    def __post_init__(self) -> None:
        transition_steps = tuple(item.step for item in self.transitions)
        if self.steps and self.transitions:
            raise ValueError("fixture must use scripted steps or action transitions")
        if any(
            step.episode_nonce != self.start.episode_nonce
            for step in (*self.steps, *transition_steps)
        ):
            raise ValueError("fixture steps must belong to the start episode")
        if any(
            step.terminal
            and step.terminal_reason != "finish_without_purchase"
            and step.raw_reward is None
            for step in (*self.steps, *transition_steps)
        ):
            raise ValueError("in-memory upstream terminal requires raw reward")
        if any(
            item.expected.kind is ShoppingActionKind.PURCHASE
            and item.step.terminal
            and (
                item.step.raw_reward is None
                or not item.step.raw_reward.reward_detail_present
            )
            for item in self.transitions
        ):
            raise ValueError("in-memory purchase terminal requires reward detail")
        if tuple(step.sequence for step in self.steps) != tuple(
            range(1, len(self.steps) + 1)
        ):
            raise ValueError("fixture step sequences must be contiguous")
        if self.transitions:
            sequences = {item.step.sequence for item in self.transitions}
            if sequences != set(range(1, max(sequences) + 1)):
                raise ValueError("fixture transition sequences must be contiguous")
            keys = [
                (item.step.sequence, item.expected.kind, item.expected.value)
                for item in self.transitions
            ]
            if len(keys) != len(set(keys)):
                raise ValueError("fixture action transitions must be unique")


class _InMemoryEpisode:
    def __init__(
        self, adapter: InMemoryShopSimulatorAdapter, fixture: InMemoryEpisodeFixture
    ) -> None:
        self._adapter = adapter
        self._fixture = fixture
        self._index = 0
        self._terminal = False
        self._closed = False

    @property
    def start(self) -> EpisodeStart:
        return self._fixture.start

    def step(self, action: ShoppingAction) -> EpisodeStep:
        self._ensure_open()
        if action.kind is ShoppingActionKind.FINISH_WITHOUT_PURCHASE:
            self._adapter._release_owned_episode()
            step = EpisodeStep(
                schema_version=SchemaVersion.V1ALPHA1,
                record_type="shopping_episode_step",
                episode_nonce=self.start.episode_nonce,
                sequence=self._index + 1,
                observation=ShoppingObservation(text=""),
                terminal=True,
                terminal_reason="finish_without_purchase",
            )
        else:
            if self._fixture.transitions:
                expected_sequence = self._index + 1
                transition = next(
                    (
                        item
                        for item in self._fixture.transitions
                        if item.step.sequence == expected_sequence
                        and item.expected == action
                    ),
                    None,
                )
                if transition is None:
                    raise AdapterProtocolError(
                        "in-memory action is not valid for the current fixture state"
                    )
                step = transition.step
            else:
                if self._index >= len(self._fixture.steps):
                    raise AdapterProtocolError("in-memory episode has no scripted step")
                step = self._fixture.steps[self._index]
        self._index += 1
        self._terminal = step.terminal
        return step

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._terminal:
            self._adapter._release_owned_episode()

    def _ensure_open(self) -> None:
        if self._terminal:
            raise EpisodeClosedError("terminal episode cannot execute another step")
        if self._closed:
            raise EpisodeClosedError("closed episode cannot execute another step")


class InMemoryShopSimulatorAdapter:
    """Original deterministic fixtures used by CI and the fixed course profile."""

    def __init__(self, fixtures: Mapping[str, InMemoryEpisodeFixture]) -> None:
        self._fixtures = dict(fixtures)
        self.release_count = 0
        self._allocation_sequence = 0

    @contextmanager
    def open_episode(self, request: OpenShoppingCase) -> Iterator[_InMemoryEpisode]:
        try:
            fixture = self._fixtures[request.task.opaque_slot]
        except KeyError as exc:
            raise AdapterProtocolError("unknown fixed shopping task slot") from exc
        if fixture.start.scenario is not request.task.scenario:
            raise AdapterProtocolError("fixture scenario does not match task ref")
        self._allocation_sequence += 1
        nonce = (
            "episode-"
            + hashlib.sha256(
                (
                    f"{fixture.start.episode_nonce}:{request.session_owner}:"
                    f"{self._allocation_sequence}"
                ).encode()
            ).hexdigest()[:32]
        )
        allocated = InMemoryEpisodeFixture(
            start=fixture.start.model_copy(update={"episode_nonce": nonce}),
            steps=tuple(
                step.model_copy(update={"episode_nonce": nonce})
                for step in fixture.steps
            ),
            transitions=tuple(
                InMemoryActionTransition(
                    expected=item.expected,
                    step=item.step.model_copy(update={"episode_nonce": nonce}),
                )
                for item in fixture.transitions
            ),
        )
        episode = _InMemoryEpisode(self, allocated)
        try:
            yield episode
        finally:
            episode.close()

    def _release_owned_episode(self) -> None:
        self.release_count += 1


@dataclass(frozen=True, slots=True)
class BridgeEpisode:
    """Private bridge allocation; the handle never crosses the Adapter seam."""

    handle: str
    lease_token: str
    generation: int
    session_owner: str
    task_slot: str
    profile_sha256: str
    measurement_level: MeasurementLevel
    protocol_sha256: str
    start: EpisodeStart

    def __post_init__(self) -> None:
        if not self.handle or not self.lease_token:
            raise ValueError("bridge allocation requires opaque handle and lease token")
        if self.generation < 1:
            raise ValueError("bridge generation must be positive")


class BridgeTransport(Protocol):
    """Injected HTTP transport kept below the public ShopSimulator Port."""

    def open_episode(self, request: OpenShoppingCase) -> BridgeEpisode: ...

    def interact(
        self,
        handle: str,
        lease_token: str,
        generation: int,
        action: str,
    ) -> EpisodeStep: ...

    def release_one(
        self,
        handle: str,
        lease_token: str,
        generation: int,
        session_owner: str,
    ) -> None: ...


def _upstream_action(action: ShoppingAction) -> str:
    if action.kind is ShoppingActionKind.SEARCH:
        return f"search[{action.value}]"
    if action.kind is ShoppingActionKind.CLICK:
        return f"click[{action.value}]"
    if action.kind is ShoppingActionKind.ASK_SHOPPER:
        return f"ask_shopper[{action.value}]"
    if action.kind is ShoppingActionKind.PURCHASE:
        return "click[buy now]"
    raise AdapterProtocolError("local finish action must not reach the bridge")


class _HttpEpisode:
    def __init__(self, transport: BridgeTransport, allocation: BridgeEpisode) -> None:
        self._transport = transport
        self._allocation = allocation
        self._terminal = False
        self._closed = False
        self._outcome_unknown = False
        self._released = False
        self._sequence = 0

    @property
    def start(self) -> EpisodeStart:
        return self._allocation.start

    def step(self, action: ShoppingAction) -> EpisodeStep:
        self._ensure_open()
        self._sequence += 1
        if action.kind is ShoppingActionKind.FINISH_WITHOUT_PURCHASE:
            self._release_owned_episode()
            step = EpisodeStep(
                schema_version=SchemaVersion.V1ALPHA1,
                record_type="shopping_episode_step",
                episode_nonce=self.start.episode_nonce,
                sequence=self._sequence,
                observation=ShoppingObservation(text=""),
                terminal=True,
                terminal_reason="finish_without_purchase",
            )
            self._terminal = True
            return step
        try:
            step = self._transport.interact(
                self._allocation.handle,
                self._allocation.lease_token,
                self._allocation.generation,
                _upstream_action(action),
            )
        except Exception as exc:
            self._outcome_unknown = True
            self._closed = True
            raise OutcomeUnknownError(
                "outcome_unknown after non-idempotent bridge interaction"
            ) from exc
        if step.episode_nonce != self.start.episode_nonce:
            self._outcome_unknown = True
            self._closed = True
            raise AdapterProtocolError("bridge returned a different episode nonce")
        if step.sequence != self._sequence:
            self._outcome_unknown = True
            self._closed = True
            raise AdapterProtocolError("bridge step sequence drifted")
        if step.terminal and step.raw_reward is None:
            self._outcome_unknown = True
            self._closed = True
            raise AdapterProtocolError("upstream terminal is missing raw reward")
        if (
            action.kind is ShoppingActionKind.PURCHASE
            and step.terminal
            and step.raw_reward is not None
            and not step.raw_reward.reward_detail_present
        ):
            self._outcome_unknown = True
            self._closed = True
            raise AdapterProtocolError("purchase terminal is missing reward detail")
        self._terminal = step.terminal
        return step

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._terminal and not self._outcome_unknown and not self._released:
            self._release_owned_episode()

    def _release_owned_episode(self) -> None:
        try:
            self._transport.release_one(
                self._allocation.handle,
                self._allocation.lease_token,
                self._allocation.generation,
                self._allocation.session_owner,
            )
        except Exception as exc:
            self._outcome_unknown = True
            self._closed = True
            raise OutcomeUnknownError(
                "outcome_unknown after bridge episode release"
            ) from exc
        self._released = True

    def _ensure_open(self) -> None:
        if self._terminal:
            raise EpisodeClosedError("terminal episode cannot execute another step")
        if self._closed:
            raise EpisodeClosedError("closed episode cannot execute another step")


class HttpShopSimulatorAdapter:
    """ShopSimulator Port backed by a locked external episode bridge."""

    def __init__(
        self,
        transport: BridgeTransport,
        *,
        expected_protocol_sha256: str,
        source_manifest: ShopSimulatorSourceManifest,
    ) -> None:
        if source_manifest.decision != "go":
            raise AdapterProtocolError(
                "live source decision is no_go; HTTP adapter remains disabled"
            )
        self.transport = transport
        self._expected_protocol_sha256 = expected_protocol_sha256

    @contextmanager
    def open_episode(self, request: OpenShoppingCase) -> Iterator[_HttpEpisode]:
        allocation = self.transport.open_episode(request)
        if allocation.session_owner != request.session_owner:
            raise AdapterProtocolError(
                "bridge allocation has a different session owner"
            )
        if allocation.start.terminal:
            raise AdapterProtocolError("fresh bridge reset cannot already be terminal")
        episode = _HttpEpisode(self.transport, allocation)
        try:
            if allocation.task_slot != request.task.opaque_slot:
                raise AdapterProtocolError(
                    "bridge allocation has a different task slot"
                )
            if allocation.start.task_slot != request.task.opaque_slot:
                raise AdapterProtocolError("bridge start has a different task slot")
            if allocation.start.scenario is not request.task.scenario:
                raise AdapterProtocolError("bridge start has a different scenario")
            if allocation.profile_sha256 != request.profile_sha256:
                raise AdapterProtocolError("bridge allocation has a different profile")
            if allocation.measurement_level is not request.measurement_level:
                raise AdapterProtocolError(
                    "bridge allocation has a different measurement"
                )
            if allocation.protocol_sha256 != self._expected_protocol_sha256:
                raise AdapterProtocolError("bridge protocol lock does not match")
        except AdapterProtocolError:
            episode.close()
            raise
        try:
            yield episode
        finally:
            episode.close()
