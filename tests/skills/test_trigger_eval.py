from __future__ import annotations

import json
from collections import deque
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

import ses.cli.skill_v0 as skill_cli
from ses.contracts import DiscoveryStatus, MeasurementKind, Usage
from ses.skills.seeds import load_creator_seed_pack
from ses.skills.trigger_eval import (
    TRIGGER_PROMPTS,
    DiscoveryObservation,
    SyntheticDiscoveryFixture,
    evaluate_triggers,
)
from ses.skills.v0 import FakeV0Creator


class QueueDiscovery:
    def __init__(self, statuses: list[DiscoveryStatus]) -> None:
        self.statuses = deque(statuses)
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost_amount: Decimal | None = None
        self.cost_currency: str | None = None

    def observe(self, prompt: str) -> DiscoveryObservation:
        del prompt
        status = self.statuses.popleft()
        return DiscoveryObservation(status=status, evidence=f"native:{status.value}")


def test_trigger_eval_reports_matrix_precision_recall_and_evidence() -> None:
    assert len(TRIGGER_PROMPTS) == 20
    assert sum(prompt.expected_trigger for prompt in TRIGGER_PROMPTS) == 10
    backend = QueueDiscovery(
        [DiscoveryStatus.TRIGGERED] * 9
        + [DiscoveryStatus.NOT_TRIGGERED]
        + [DiscoveryStatus.TRIGGERED]
        + [DiscoveryStatus.NOT_TRIGGERED] * 9
    )

    result = evaluate_triggers(
        skill_sha256="a" * 64,
        engine_version="claude-code:2.1.220",
        model_id="fixture-model",
        measurement_kind=MeasurementKind.SYNTHETIC_OFFLINE,
        measured_at=datetime(2026, 8, 17, tzinfo=UTC),
        discovery=backend,
    )

    assert (result.tp, result.fp, result.tn, result.fn) == (9, 1, 9, 1)
    assert result.precision == 0.9
    assert result.recall == 0.9
    assert result.indeterminate_count == 0
    assert len(result.prompts) == 20
    assert all(row.evidence.startswith("native:") for row in result.prompts)


def test_trigger_eval_preserves_indeterminate_without_coercing_it() -> None:
    backend = QueueDiscovery(
        [DiscoveryStatus.INDETERMINATE]
        + [DiscoveryStatus.TRIGGERED] * 9
        + [DiscoveryStatus.NOT_TRIGGERED] * 10
    )

    result = evaluate_triggers(
        skill_sha256="b" * 64,
        engine_version="claude-code:2.1.220",
        model_id="fixture-model",
        measurement_kind=MeasurementKind.SYNTHETIC_OFFLINE,
        measured_at=datetime(2026, 8, 17, tzinfo=UTC),
        discovery=backend,
    )

    assert result.indeterminate_count == 1
    assert result.tp == 9
    assert result.fn == 0
    assert result.recall == 1.0
    assert result.prompts[0].actual is DiscoveryStatus.INDETERMINATE


def test_trigger_eval_preserves_unavailable_provider_cost() -> None:
    backend = QueueDiscovery(
        [DiscoveryStatus.TRIGGERED] * 10 + [DiscoveryStatus.NOT_TRIGGERED] * 10
    )
    backend.input_tokens = 34
    backend.output_tokens = 12

    result = evaluate_triggers(
        skill_sha256="c" * 64,
        engine_version="claude-code:2.1.220",
        model_id="claude-sonnet-4-6",
        measurement_kind=MeasurementKind.LIVE_MEASURED,
        measured_at=datetime(2026, 8, 20, tzinfo=UTC),
        discovery=backend,
    )

    assert result.usage.input_tokens == 34
    assert result.usage.output_tokens == 12
    assert result.usage.cost_amount is None
    assert result.usage.cost_currency is None


def test_trigger_cli_serializes_unavailable_live_cost_as_null(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class UnpricedDiscovery(SyntheticDiscoveryFixture):
        def __init__(self, **_kwargs: object) -> None:
            super().__init__()
            self.input_tokens = 34
            self.output_tokens = 12
            self.cost_amount = None
            self.cost_currency = None

    root = Path(__file__).parents[2]
    monkeypatch.setattr(skill_cli, "ClaudeNativeDiscovery", UnpricedDiscovery)
    monkeypatch.setenv("CHATANYWHERE_API_KEY", "chatanywhere-test-secret")

    code = skill_cli.trigger_main(
        [
            "--skill",
            str(root / "fixtures/seed/skill/v0"),
            "--mode",
            "live",
            "--provider",
            "chatanywhere",
            "--project-root",
            str(root),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["cost_amount"] is None
    assert payload["usage"]["cost_amount"] is None
    assert payload["usage"]["cost_currency"] is None


def test_create_cli_serializes_unavailable_live_cost_as_null(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    original_loader = load_creator_seed_pack

    class UnpricedCreator(FakeV0Creator):
        def __init__(self, **_kwargs: object) -> None:
            super().__init__()
            self.usage = Usage(input_tokens=55, output_tokens=21)
            self.latency_ms = 9

    monkeypatch.setattr(skill_cli, "LiveV0Creator", UnpricedCreator)
    monkeypatch.setattr(
        skill_cli,
        "load_creator_seed_pack",
        lambda path, *, mode: original_loader(path, mode="fixed"),
    )
    monkeypatch.setenv("CHATANYWHERE_API_KEY", "chatanywhere-test-secret")
    root = Path(__file__).parents[2]

    code = skill_cli.skill_main(
        [
            "create-v0",
            "--out",
            str(tmp_path / "v0"),
            "--mode",
            "live",
            "--provider",
            "chatanywhere",
            "--project-root",
            str(root),
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 0
    assert payload["input_tokens"] == 55
    assert payload["output_tokens"] == 21
    assert payload["cost_amount"] is None
