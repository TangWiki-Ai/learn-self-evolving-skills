from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ses.contracts import SchemaVersion
from ses.contracts.shopping import (
    AssetRights,
    AssetRightsStatus,
    EpisodeStart,
    EpisodeStep,
    MeasurementLevel,
    OpenShoppingCase,
    RawShopSimulatorReward,
    ShoppingAction,
    ShoppingActionKind,
    ShoppingObservation,
    ShoppingScenario,
    ShoppingTaskRef,
    ShopSimulatorAssetKind,
    ShopSimulatorSourceManifest,
)
from ses.shopping.adapters import (
    AdapterProtocolError,
    BridgeEpisode,
    BridgeTransport,
    EpisodeClosedError,
    HttpShopSimulatorAdapter,
    InMemoryActionTransition,
    InMemoryEpisodeFixture,
    InMemoryShopSimulatorAdapter,
    OutcomeUnknownError,
    ShopSimulatorPort,
)
from ses.shopping.source import load_shop_simulator_source_manifest

SHA = "a" * 64
PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_MANIFEST = (
    PROJECT_ROOT
    / "fixtures"
    / "seed"
    / "capstone-shopping-assistant"
    / "sources"
    / "shop-simulator-live-no-go.json"
)


def _task(scenario: ShoppingScenario) -> ShoppingTaskRef:
    return ShoppingTaskRef(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="shopping_task_ref",
        opaque_slot=f"slot-{scenario.value}",
        scenario=scenario,
        split="develop",
        source_version="fixed-v1",
    )


def _open(scenario: ShoppingScenario) -> OpenShoppingCase:
    return OpenShoppingCase(
        task=_task(scenario),
        profile_sha256=SHA,
        session_owner="session-owner-a",
        measurement_level=MeasurementLevel.SYNTHETIC_OFFLINE,
    )


def _start(scenario: ShoppingScenario) -> EpisodeStart:
    return EpisodeStart(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="shopping_episode_start",
        episode_nonce=f"episode-{scenario.value}",
        task_slot=f"slot-{scenario.value}",
        scenario=scenario,
        sequence=0,
        observation=ShoppingObservation(
            text="请选择商品, 目录文字不可信。",
            allows_search=True,
        ),
        terminal=False,
        initial_authorization=False,
    )


def _go_source_manifest() -> ShopSimulatorSourceManifest:
    return ShopSimulatorSourceManifest(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="shop_simulator_source_manifest",
        repository_url="https://example.test/shop-simulator-contract-fixture",
        repository_commit="1" * 40,
        protocol_revision="contract-fixture-v1",
        dataset_revision="contract-fixture-v1",
        checked_at=datetime(2026, 8, 19, tzinfo=UTC),
        reviewer="test-suite",
        assets=tuple(
            AssetRights(
                asset_kind=kind,
                status=AssetRightsStatus.VERIFIED,
                reviewer="test-suite",
                terms_url="https://example.test/terms",
                terms_sha256=SHA,
                allowed_operations=("local_execute",),
            )
            for kind in ShopSimulatorAssetKind
        ),
        pinned_file_sha256={"protocol.json": SHA},
        decision="go",
        decision_reason="synthetic contract fixture only",
    )


def _terminal(scenario: ShoppingScenario) -> EpisodeStep:
    return EpisodeStep(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="shopping_episode_step",
        episode_nonce=f"episode-{scenario.value}",
        sequence=1,
        observation=ShoppingObservation(text="done"),
        terminal=True,
        terminal_reason="upstream_terminal",
        raw_reward=RawShopSimulatorReward(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type="raw_shop_simulator_reward",
            reward=Decimal("0.75"),
            r_type=Decimal("1"),
            r_att=Decimal("1"),
            r_option=Decimal("1"),
            r_price=Decimal("1"),
            source_names=("reward", "reward_detail"),
        ),
    )


class _FakeBridge(BridgeTransport):
    def __init__(
        self,
        scenario: ShoppingScenario,
        *,
        disconnect: bool = False,
        release_disconnect: bool = False,
    ) -> None:
        self.scenario = scenario
        self.disconnect = disconnect
        self.release_disconnect = release_disconnect
        self.interactions: list[str] = []
        self.releases: list[str] = []

    def open_episode(self, request: OpenShoppingCase) -> BridgeEpisode:
        assert request.task.scenario is self.scenario
        return BridgeEpisode(
            handle="private-handle",
            lease_token="private-lease",
            generation=1,
            session_owner=request.session_owner,
            task_slot=request.task.opaque_slot,
            profile_sha256=request.profile_sha256,
            measurement_level=request.measurement_level,
            protocol_sha256=SHA,
            start=_start(self.scenario),
        )

    def interact(
        self, handle: str, lease_token: str, generation: int, action: str
    ) -> EpisodeStep:
        assert handle == "private-handle"
        assert lease_token == "private-lease"
        assert generation == 1
        self.interactions.append(action)
        if self.disconnect:
            raise TimeoutError("connection state is unknown")
        return _terminal(self.scenario)

    def release_one(
        self,
        handle: str,
        lease_token: str,
        generation: int,
        session_owner: str,
    ) -> None:
        assert lease_token == "private-lease"
        assert generation == 1
        assert session_owner == "session-owner-a"
        self.releases.append(handle)
        if self.release_disconnect:
            raise TimeoutError("release outcome is unknown")


@pytest.mark.parametrize("scenario", list(ShoppingScenario))
@pytest.mark.parametrize("adapter_kind", ["memory", "http"])
def test_both_adapters_follow_the_same_four_scenario_contract(
    scenario: ShoppingScenario,
    adapter_kind: str,
) -> None:
    terminal = _terminal(scenario)
    adapter: ShopSimulatorPort
    if adapter_kind == "memory":
        adapter = InMemoryShopSimulatorAdapter(
            {
                f"slot-{scenario.value}": InMemoryEpisodeFixture(
                    start=_start(scenario),
                    steps=(terminal,),
                )
            }
        )
    else:
        adapter = HttpShopSimulatorAdapter(
            _FakeBridge(scenario),
            expected_protocol_sha256=SHA,
            source_manifest=_go_source_manifest(),
        )

    with adapter.open_episode(_open(scenario)) as episode:
        assert episode.start.scenario is scenario
        step = episode.step(ShoppingAction.search("降噪耳机"))
        assert step.terminal is True
        assert step.raw_reward == terminal.raw_reward
        with pytest.raises(EpisodeClosedError, match="terminal"):
            episode.step(ShoppingAction.search("不能继续"))


@pytest.mark.parametrize("adapter_kind", ["memory", "http"])
@pytest.mark.parametrize(
    ("scenario", "action", "expected_upstream"),
    [
        (
            ShoppingScenario.SINGLE_PERSONA,
            ShoppingAction.search("降噪耳机"),
            "search[降噪耳机]",
        ),
        (
            ShoppingScenario.SINGLE_PERSONA,
            ShoppingAction.click("offer-details"),
            "click[offer-details]",
        ),
        (
            ShoppingScenario.MULTI_PERSONA,
            ShoppingAction.ask_shopper("请确认预算"),
            "ask_shopper[请确认预算]",
        ),
        (
            ShoppingScenario.MULTI_PERSONA,
            ShoppingAction.purchase("authorized-offer"),
            "click[buy now]",
        ),
        (
            ShoppingScenario.MULTI_PERSONA,
            ShoppingAction.finish_without_purchase("没有合格商品"),
            None,
        ),
    ],
    ids=["search", "click", "ask", "purchase", "finish"],
)
def test_common_adapter_action_suite_covers_persona_scenarios(
    adapter_kind: str,
    scenario: ShoppingScenario,
    action: ShoppingAction,
    expected_upstream: str | None,
) -> None:
    terminal = _terminal(scenario)
    bridge: _FakeBridge | None = None
    adapter: ShopSimulatorPort
    if adapter_kind == "memory":
        adapter = InMemoryShopSimulatorAdapter(
            {
                f"slot-{scenario.value}": InMemoryEpisodeFixture(
                    start=_start(scenario),
                    steps=(),
                    transitions=(
                        InMemoryActionTransition(expected=action, step=terminal),
                    ),
                )
            }
        )
    else:
        bridge = _FakeBridge(scenario)
        adapter = HttpShopSimulatorAdapter(
            bridge,
            expected_protocol_sha256=SHA,
            source_manifest=_go_source_manifest(),
        )

    with adapter.open_episode(_open(scenario)) as episode:
        step = episode.step(action)

    if expected_upstream is None:
        assert step.terminal_reason == "finish_without_purchase"
        assert step.raw_reward is None
        expected_release_count = 1
    else:
        assert step.raw_reward == terminal.raw_reward
        expected_release_count = 0
    if bridge is None:
        assert isinstance(adapter, InMemoryShopSimulatorAdapter)
        assert adapter.release_count == expected_release_count
    else:
        assert bridge.interactions == (
            [] if expected_upstream is None else [expected_upstream]
        )
        assert len(bridge.releases) == expected_release_count


@pytest.mark.parametrize(
    "factory,release_count",
    [
        (
            lambda scenario: InMemoryShopSimulatorAdapter(
                {
                    f"slot-{scenario.value}": InMemoryEpisodeFixture(
                        start=_start(scenario),
                        steps=(_terminal(scenario),),
                    )
                }
            ),
            lambda adapter: adapter.release_count,
        ),
        (
            lambda scenario: HttpShopSimulatorAdapter(
                _FakeBridge(scenario),
                expected_protocol_sha256=SHA,
                source_manifest=_go_source_manifest(),
            ),
            lambda adapter: len(adapter.transport.releases),
        ),
    ],
)
def test_close_releases_only_a_non_terminal_episode_still_owned_by_the_session(
    factory: Callable[[ShoppingScenario], ShopSimulatorPort],
    release_count: Callable[[ShopSimulatorPort], int],
) -> None:
    scenario = ShoppingScenario.SINGLE
    unfinished = factory(scenario)
    with unfinished.open_episode(_open(scenario)):
        pass
    assert release_count(unfinished) == 1

    terminal = factory(scenario)
    with terminal.open_episode(_open(scenario)) as episode:
        episode.step(ShoppingAction.search("耳机"))
        episode.close()
        episode.close()
    assert release_count(terminal) == 0


@pytest.mark.parametrize(
    "factory,release_count",
    [
        (
            lambda scenario: InMemoryShopSimulatorAdapter(
                {
                    f"slot-{scenario.value}": InMemoryEpisodeFixture(
                        start=_start(scenario),
                        steps=(_terminal(scenario),),
                    )
                }
            ),
            lambda adapter: adapter.release_count,
        ),
        (
            lambda scenario: HttpShopSimulatorAdapter(
                _FakeBridge(scenario),
                expected_protocol_sha256=SHA,
                source_manifest=_go_source_manifest(),
            ),
            lambda adapter: len(adapter.transport.releases),
        ),
    ],
    ids=["memory", "http"],
)
def test_finish_releases_the_owned_episode_once_before_close(
    factory: Callable[[ShoppingScenario], ShopSimulatorPort],
    release_count: Callable[[ShopSimulatorPort], int],
) -> None:
    scenario = ShoppingScenario.SINGLE
    adapter = factory(scenario)

    with adapter.open_episode(_open(scenario)) as episode:
        step = episode.step(
            ShoppingAction.finish_without_purchase("没有满足全部约束的商品")
        )
        assert step.terminal_reason == "finish_without_purchase"
        assert release_count(adapter) == 1
        episode.close()
        episode.close()

    assert release_count(adapter) == 1


def test_http_adapter_does_not_retry_or_release_an_outcome_unknown_action() -> None:
    bridge = _FakeBridge(ShoppingScenario.SINGLE, disconnect=True)
    adapter = HttpShopSimulatorAdapter(
        bridge,
        expected_protocol_sha256=SHA,
        source_manifest=_go_source_manifest(),
    )

    with pytest.raises(OutcomeUnknownError, match="outcome_unknown"):
        with adapter.open_episode(_open(ShoppingScenario.SINGLE)) as episode:
            episode.step(ShoppingAction.purchase("opaque-buy-action"))

    assert bridge.interactions == ["click[buy now]"]
    assert bridge.releases == []


@pytest.mark.parametrize("missing", ["raw_reward", "reward_detail"])
def test_http_purchase_terminal_requires_present_reward_detail(missing: str) -> None:
    class _MissingRewardDetailBridge(_FakeBridge):
        def interact(
            self,
            handle: str,
            lease_token: str,
            generation: int,
            action: str,
        ) -> EpisodeStep:
            step = super().interact(handle, lease_token, generation, action)
            assert step.raw_reward is not None
            return step.model_copy(
                update={
                    "raw_reward": (
                        None
                        if missing == "raw_reward"
                        else step.raw_reward.model_copy(
                            update={"reward_detail_present": False}
                        )
                    )
                }
            )

    bridge = _MissingRewardDetailBridge(ShoppingScenario.SINGLE)
    adapter = HttpShopSimulatorAdapter(
        bridge,
        expected_protocol_sha256=SHA,
        source_manifest=_go_source_manifest(),
    )

    expected_error = "raw reward" if missing == "raw_reward" else "reward detail"
    with pytest.raises(AdapterProtocolError, match=expected_error):
        with adapter.open_episode(_open(ShoppingScenario.SINGLE)) as episode:
            episode.step(ShoppingAction.purchase("opaque-buy-action"))

    assert bridge.interactions == ["click[buy now]"]
    assert bridge.releases == []


def test_http_nonpurchase_terminal_can_end_without_reward_detail() -> None:
    class _StepLimitBridge(_FakeBridge):
        def interact(
            self,
            handle: str,
            lease_token: str,
            generation: int,
            action: str,
        ) -> EpisodeStep:
            super().interact(handle, lease_token, generation, action)
            return EpisodeStep(
                schema_version=SchemaVersion.V1ALPHA1,
                record_type="shopping_episode_step",
                episode_nonce=_start(self.scenario).episode_nonce,
                sequence=1,
                observation=ShoppingObservation(text="step limit reached"),
                terminal=True,
                terminal_reason="step_limit_without_purchase",
                raw_reward=RawShopSimulatorReward(
                    schema_version=SchemaVersion.V1ALPHA1,
                    record_type="raw_shop_simulator_reward",
                    reward=Decimal("0"),
                    reward_detail_present=False,
                    source_names=("reward",),
                ),
            )

    bridge = _StepLimitBridge(ShoppingScenario.SINGLE)
    adapter = HttpShopSimulatorAdapter(
        bridge,
        expected_protocol_sha256=SHA,
        source_manifest=_go_source_manifest(),
    )

    with adapter.open_episode(_open(ShoppingScenario.SINGLE)) as episode:
        step = episode.step(ShoppingAction.search("没有匹配商品"))

    assert step.terminal_reason == "step_limit_without_purchase"
    assert step.raw_reward is not None
    assert step.raw_reward.reward_detail_present is False
    assert bridge.releases == []


def test_http_upstream_terminal_without_raw_reward_is_protocol_error() -> None:
    class _MissingRawRewardBridge(_FakeBridge):
        def interact(
            self,
            handle: str,
            lease_token: str,
            generation: int,
            action: str,
        ) -> EpisodeStep:
            step = super().interact(handle, lease_token, generation, action)
            return step.model_copy(update={"raw_reward": None})

    bridge = _MissingRawRewardBridge(ShoppingScenario.SINGLE)
    adapter = HttpShopSimulatorAdapter(
        bridge,
        expected_protocol_sha256=SHA,
        source_manifest=_go_source_manifest(),
    )

    with pytest.raises(AdapterProtocolError, match="terminal is missing raw reward"):
        with adapter.open_episode(_open(ShoppingScenario.SINGLE)) as episode:
            episode.step(ShoppingAction.search("没有匹配商品"))

    assert bridge.releases == []


def test_in_memory_fixture_rejects_upstream_terminal_without_raw_reward() -> None:
    start = _start(ShoppingScenario.SINGLE)
    terminal = _terminal(ShoppingScenario.SINGLE).model_copy(
        update={"raw_reward": None}
    )

    with pytest.raises(ValueError, match="terminal requires raw reward"):
        InMemoryEpisodeFixture(start=start, steps=(terminal,))


def test_http_finish_release_failure_is_outcome_unknown_and_never_retried() -> None:
    bridge = _FakeBridge(
        ShoppingScenario.SINGLE,
        release_disconnect=True,
    )
    adapter = HttpShopSimulatorAdapter(
        bridge,
        expected_protocol_sha256=SHA,
        source_manifest=_go_source_manifest(),
    )

    with pytest.raises(OutcomeUnknownError, match="outcome_unknown"):
        with adapter.open_episode(_open(ShoppingScenario.SINGLE)) as episode:
            episode.step(ShoppingAction.finish_without_purchase("安全停止"))

    assert bridge.interactions == []
    assert bridge.releases == ["private-handle"]
    with pytest.raises(EpisodeClosedError, match="closed"):
        episode.step(ShoppingAction.search("不能恢复未知 lease"))


def test_action_contract_keeps_purchase_and_finish_explicit() -> None:
    assert ShoppingAction.purchase("buy-action").kind is ShoppingActionKind.PURCHASE
    assert (
        ShoppingAction.finish_without_purchase("没有满足全部约束的商品").kind
        is ShoppingActionKind.FINISH_WITHOUT_PURCHASE
    )
    with pytest.raises(ValueError, match="buy now"):
        ShoppingAction.click("buy now")


def test_source_manifest_requires_asset_level_rights_and_fails_closed() -> None:
    assets = tuple(
        AssetRights(
            asset_kind=kind,
            status=(
                AssetRightsStatus.UNKNOWN
                if kind is ShopSimulatorAssetKind.PRODUCT_IMAGES
                else AssetRightsStatus.VERIFIED
            ),
            reviewer="course-maintainer",
            terms_url="https://github.com/ShopAgent-Team/ShopSimulator",
            terms_sha256=SHA,
            allowed_operations=("local_execute", "summarize"),
        )
        for kind in ShopSimulatorAssetKind
    )

    manifest = ShopSimulatorSourceManifest(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type="shop_simulator_source_manifest",
        repository_url="https://github.com/ShopAgent-Team/ShopSimulator",
        repository_commit="51bb26012cee31aea7ac26177c5ffe807026ac07",
        protocol_revision="bridge-v1",
        dataset_revision="unverified",
        checked_at=datetime(2026, 8, 19, tzinfo=UTC),
        reviewer="course-maintainer",
        assets=assets,
        pinned_file_sha256={"get_score.py": SHA},
        decision="no_go",
        decision_reason="product image rights remain unknown",
    )
    assert manifest.decision == "no_go"

    with pytest.raises(ValueError, match="every live asset"):
        manifest.model_copy(update={"decision": "go"})


def test_checked_in_source_manifest_mechanically_keeps_live_disabled() -> None:
    loaded = load_shop_simulator_source_manifest(SOURCE_MANIFEST)

    assert loaded.manifest.decision == "no_go"
    assert loaded.live_enabled is False
    assert all(
        asset.status is not AssetRightsStatus.VERIFIED
        for asset in loaded.manifest.assets
    )
    assert any(asset.terms_sha256 is None for asset in loaded.manifest.assets)
    with pytest.raises(AdapterProtocolError, match="no_go"):
        HttpShopSimulatorAdapter(
            _FakeBridge(ShoppingScenario.SINGLE),
            expected_protocol_sha256=SHA,
            source_manifest=loaded.manifest,
        )


def test_verified_rights_require_a_terms_hash() -> None:
    with pytest.raises(ValueError, match="verified rights require"):
        AssetRights(
            asset_kind=ShopSimulatorAssetKind.REPOSITORY_CODE,
            status=AssetRightsStatus.VERIFIED,
            reviewer="course-maintainer",
            terms_url="https://github.com/ShopAgent-Team/ShopSimulator",
            terms_sha256=None,
            allowed_operations=("local_execute",),
        )


def test_http_adapter_rejects_a_bridge_allocation_owned_by_another_session() -> None:
    class _WrongOwnerBridge(_FakeBridge):
        def open_episode(self, request: OpenShoppingCase) -> BridgeEpisode:
            allocation = super().open_episode(request)
            return replace(allocation, session_owner="another-session")

    bridge = _WrongOwnerBridge(ShoppingScenario.SINGLE)
    adapter = HttpShopSimulatorAdapter(
        bridge,
        expected_protocol_sha256=SHA,
        source_manifest=_go_source_manifest(),
    )

    with pytest.raises(AdapterProtocolError, match="session owner"):
        with adapter.open_episode(_open(ShoppingScenario.SINGLE)):
            pass
    assert bridge.releases == []


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda allocation: replace(allocation, task_slot="slot-other"),
            "task slot",
        ),
        (
            lambda allocation: replace(
                allocation,
                start=allocation.start.model_copy(update={"task_slot": "slot-other"}),
            ),
            "start has a different task slot",
        ),
        (
            lambda allocation: replace(
                allocation,
                start=allocation.start.model_copy(
                    update={"scenario": ShoppingScenario.MULTI_PERSONA}
                ),
            ),
            "scenario",
        ),
        (
            lambda allocation: replace(allocation, profile_sha256="b" * 64),
            "profile",
        ),
        (
            lambda allocation: replace(
                allocation,
                measurement_level=MeasurementLevel.LIVE_MEASURED,
            ),
            "measurement",
        ),
        (
            lambda allocation: replace(allocation, protocol_sha256="b" * 64),
            "protocol lock",
        ),
    ],
    ids=[
        "task",
        "start-task",
        "start-scenario",
        "profile",
        "measurement",
        "protocol",
    ],
)
def test_http_adapter_releases_an_owned_nonterminal_allocation_rejected_by_validation(
    mutation: Callable[[BridgeEpisode], BridgeEpisode],
    message: str,
) -> None:
    class _InvalidAllocationBridge(_FakeBridge):
        def open_episode(self, request: OpenShoppingCase) -> BridgeEpisode:
            return mutation(super().open_episode(request))

    bridge = _InvalidAllocationBridge(ShoppingScenario.SINGLE)
    adapter = HttpShopSimulatorAdapter(
        bridge,
        expected_protocol_sha256=SHA,
        source_manifest=_go_source_manifest(),
    )

    with pytest.raises(AdapterProtocolError, match=message):
        with adapter.open_episode(_open(ShoppingScenario.SINGLE)):
            pass

    assert bridge.releases == ["private-handle"]


def test_http_validation_cleanup_with_unknown_release_outcome_is_not_retried() -> None:
    class _InvalidProfileBridge(_FakeBridge):
        def open_episode(self, request: OpenShoppingCase) -> BridgeEpisode:
            return replace(
                super().open_episode(request),
                profile_sha256="b" * 64,
            )

    bridge = _InvalidProfileBridge(
        ShoppingScenario.SINGLE,
        release_disconnect=True,
    )
    adapter = HttpShopSimulatorAdapter(
        bridge,
        expected_protocol_sha256=SHA,
        source_manifest=_go_source_manifest(),
    )

    with pytest.raises(OutcomeUnknownError, match="outcome_unknown"):
        with adapter.open_episode(_open(ShoppingScenario.SINGLE)):
            pass

    assert bridge.releases == ["private-handle"]


def test_in_memory_adapter_allocates_a_fresh_episode_nonce_on_every_open() -> None:
    scenario = ShoppingScenario.SINGLE
    adapter = InMemoryShopSimulatorAdapter(
        {
            f"slot-{scenario.value}": InMemoryEpisodeFixture(
                start=_start(scenario),
                steps=(_terminal(scenario),),
            )
        }
    )
    nonces: list[str] = []
    for _ in range(2):
        with adapter.open_episode(_open(scenario)) as episode:
            nonces.append(episode.start.episode_nonce)
            episode.step(ShoppingAction.search("耳机"))

    assert len(set(nonces)) == 2


def test_in_memory_adapter_can_match_actions_without_changing_the_task() -> None:
    scenario = ShoppingScenario.SINGLE
    success = _terminal(scenario)
    assert success.raw_reward is not None
    failure = success.model_copy(
        update={
            "raw_reward": success.raw_reward.model_copy(
                update={
                    "reward": Decimal("0"),
                    "r_type": Decimal("0"),
                    "r_att": Decimal("0"),
                    "r_option": Decimal("0"),
                    "r_price": Decimal("0"),
                }
            )
        }
    )
    adapter = InMemoryShopSimulatorAdapter(
        {
            f"slot-{scenario.value}": InMemoryEpisodeFixture(
                start=_start(scenario),
                steps=(),
                transitions=(
                    InMemoryActionTransition(
                        expected=ShoppingAction.search("精确约束"),
                        step=success,
                    ),
                    InMemoryActionTransition(
                        expected=ShoppingAction.search("模糊请求"),
                        step=failure,
                    ),
                ),
            )
        }
    )

    with adapter.open_episode(_open(scenario)) as episode:
        result = episode.step(ShoppingAction.search("精确约束"))
    assert result.raw_reward is not None
    assert result.raw_reward.reward == Decimal("0.75")

    with adapter.open_episode(_open(scenario)) as episode:
        result = episode.step(ShoppingAction.search("模糊请求"))
    assert result.raw_reward is not None
    assert result.raw_reward.reward == Decimal("0")
