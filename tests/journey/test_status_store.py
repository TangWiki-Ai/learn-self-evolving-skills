from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from ses.contracts import ArtifactRef, ArtifactRoot, Usage
from ses.foundation.config import ProviderId
from ses.journey import (
    DEFAULT_STATION_COMMANDS,
    ExperimentCostSource,
    ExperimentMode,
    JourneyProgressStatus,
    JourneyStateError,
    JourneyStatus,
    JourneyStatusStore,
)

START = datetime(2026, 8, 20, 8, tzinfo=UTC)
SHA_A = "a" * 64
SHA_B = "b" * 64


def _ref(path: str, sha256: str = SHA_A) -> ArtifactRef:
    return ArtifactRef(root=ArtifactRoot.WORKSPACE, path=path, sha256=sha256)


def test_initialize_writes_private_canonical_status_and_recovers(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    store = JourneyStatusStore(workspace)

    initial = store.initialize(now=START)

    assert store.status_path == workspace / ".ses" / "status.json"
    assert initial.status is JourneyProgressStatus.PENDING
    assert initial.experiment_mode is ExperimentMode.LIVE
    assert initial.experiment_provider is ProviderId.SILICONFLOW
    assert initial.model_lock_sha256 == "0" * 64
    assert initial.cost_source is ExperimentCostSource.CLAUDE_CODE_ESTIMATE
    assert initial.current_station == 0
    assert tuple(station.number for station in initial.stations) == tuple(range(8))
    assert tuple(station.command for station in initial.stations) == (
        DEFAULT_STATION_COMMANDS
    )
    assert all(
        station.status is JourneyProgressStatus.PENDING for station in initial.stations
    )
    assert stat.S_IMODE(store.status_path.stat().st_mode) == 0o600
    assert store.status_path.read_bytes().endswith(b"\n")
    assert (
        json.loads(store.status_path.read_text(encoding="utf-8"))["record_type"]
        == "journey_status"
    )

    orphan = workspace / ".ses" / ".status.json.crashed.tmp"
    orphan.write_text("partial", encoding="utf-8")
    recovered = store.initialize(now=START + timedelta(hours=1))

    assert recovered == initial
    assert store.load() == initial


def test_experiment_mode_is_persisted_and_validated(tmp_path: Path) -> None:
    store = JourneyStatusStore(tmp_path)

    fixed = store.initialize(experiment_mode=ExperimentMode.FIXED, now=START)

    assert fixed.experiment_mode is ExperimentMode.FIXED
    assert fixed.experiment_provider is None
    assert fixed.model_lock_sha256 is None
    assert fixed.cost_source is ExperimentCostSource.SYNTHETIC_CI
    assert store.load().experiment_mode is ExperimentMode.FIXED
    payload = json.loads(store.status_path.read_text(encoding="utf-8"))
    assert payload["experiment_mode"] == "fixed"
    assert payload["experiment_provider"] is None
    assert (
        store.initialize(experiment_mode="fixed", now=START + timedelta(minutes=1))
        == fixed
    )

    with pytest.raises(JourneyStateError, match=r"live.*fixed"):
        JourneyStatusStore(tmp_path / "invalid").initialize(
            experiment_mode="replay",
            now=START,
        )


def test_live_provider_and_model_lock_are_persisted_and_cannot_change(
    tmp_path: Path,
) -> None:
    store = JourneyStatusStore(tmp_path)
    lock_sha256 = "c" * 64

    initial = store.initialize(
        experiment_provider=ProviderId.CHATANYWHERE,
        model_lock_sha256=lock_sha256,
        cost_currency="CNY",
        cost_source=ExperimentCostSource.UNAVAILABLE,
        now=START,
    )

    assert initial.experiment_provider is ProviderId.CHATANYWHERE
    assert initial.model_lock_sha256 == lock_sha256
    assert initial.cost_source is ExperimentCostSource.UNAVAILABLE
    assert initial.experiment_usage.cost_complete is False
    with pytest.raises(JourneyStateError, match="provider"):
        store.initialize(
            experiment_provider=ProviderId.SILICONFLOW,
            model_lock_sha256=lock_sha256,
            cost_currency="CNY",
            cost_source=ExperimentCostSource.UNAVAILABLE,
            now=START + timedelta(minutes=1),
        )
    with pytest.raises(JourneyStateError, match="model lock"):
        store.initialize(
            experiment_provider=ProviderId.CHATANYWHERE,
            model_lock_sha256="d" * 64,
            cost_currency="CNY",
            cost_source=ExperimentCostSource.UNAVAILABLE,
            now=START + timedelta(minutes=1),
        )


def test_stations_can_run_out_of_order_and_completed_stations_can_reopen(
    tmp_path: Path,
) -> None:
    store = JourneyStatusStore(tmp_path)
    store.initialize(now=START)

    station_7 = store.start_station(7, now=START + timedelta(minutes=1))
    assert station_7.status is JourneyProgressStatus.RUNNING
    assert station_7.current_station == 7

    early_summary = store.complete_station(
        7,
        artifact_refs=(_ref(".ses/artifacts/summary.html"),),
        now=START + timedelta(minutes=2),
    )
    assert early_summary.status is JourneyProgressStatus.RUNNING
    assert early_summary.current_station == 0
    assert early_summary.stations[7].status is JourneyProgressStatus.COMPLETED

    reopened = store.start_station(7, now=START + timedelta(minutes=3))
    assert reopened.stations[7].status is JourneyProgressStatus.RUNNING
    assert reopened.stations[7].completed_at is None
    assert reopened.stations[7].artifact_refs == (_ref(".ses/artifacts/summary.html"),)


def test_switching_stations_preserves_interruption_for_resume(tmp_path: Path) -> None:
    store = JourneyStatusStore(tmp_path)
    store.initialize(now=START)
    store.start_station(4, now=START + timedelta(minutes=1))

    state = store.start_station(7, now=START + timedelta(minutes=2))

    assert state.current_station == 7
    assert state.status is JourneyProgressStatus.RUNNING
    assert state.stations[4].status is JourneyProgressStatus.NEEDS_ATTENTION
    assert state.stations[4].attention_reason is not None
    assert state.stations[7].status is JourneyProgressStatus.RUNNING


def test_gate_loop_reopens_stations_and_replaces_refs_by_logical_path(
    tmp_path: Path,
) -> None:
    store = JourneyStatusStore(tmp_path)
    store.initialize(now=START)
    first_patch = _ref(".ses/decisions/station-4.json", SHA_A)

    store.start_station(4, now=START + timedelta(minutes=1))
    store.complete_station(
        4,
        decision_refs=(first_patch,),
        now=START + timedelta(minutes=2),
    )
    store.start_station(5, now=START + timedelta(minutes=3))
    store.mark_needs_attention(
        5,
        "Gate rejected the candidate.",
        now=START + timedelta(minutes=4),
    )

    reopened = store.start_station(4, now=START + timedelta(minutes=5))
    assert reopened.stations[4].decision_refs == (first_patch,)
    second_patch = _ref(".ses/decisions/station-4.json", SHA_B)
    completed = store.complete_station(
        4,
        decision_refs=(second_patch,),
        now=START + timedelta(minutes=6),
    )

    assert completed.stations[4].decision_refs == (second_patch,)
    assert completed.current_station == 5
    assert completed.status is JourneyProgressStatus.NEEDS_ATTENTION


def test_all_stations_must_complete_before_journey_completes(tmp_path: Path) -> None:
    store = JourneyStatusStore(tmp_path)
    store.initialize(now=START)

    for number in range(8):
        minute = number * 2 + 1
        store.start_station(number, now=START + timedelta(minutes=minute))
        state = store.complete_station(
            number,
            now=START + timedelta(minutes=minute + 1),
        )

    assert state.status is JourneyProgressStatus.COMPLETED
    assert state.current_station == 7
    assert all(
        station.status is JourneyProgressStatus.COMPLETED for station in state.stations
    )


def test_usage_accumulates_decimal_cost_and_marks_missing_cost(
    tmp_path: Path,
) -> None:
    store = JourneyStatusStore(tmp_path)
    store.initialize(now=START)

    state = store.record_usage(
        Usage(
            input_tokens=10,
            output_tokens=4,
            cost_amount=Decimal("0.0123"),
            cost_currency="CNY",
        ),
        now=START + timedelta(minutes=1),
    )
    state = store.record_usage(
        Usage(input_tokens=3, output_tokens=2),
        now=START + timedelta(minutes=2),
    )

    assert state.experiment_usage.input_tokens == 13
    assert state.experiment_usage.output_tokens == 6
    assert state.experiment_usage.cost_amount == Decimal("0.0123")
    assert state.experiment_usage.cost_currency == "CNY"
    assert state.experiment_usage.cost_complete is False
    with pytest.raises(JourneyStateError, match="usage is invalid"):
        store.record_usage(
            Usage(
                input_tokens=1,
                output_tokens=1,
                cost_amount=Decimal("1"),
                cost_currency="USD",
            ),
            now=START + timedelta(minutes=3),
        )


def test_replace_usage_is_idempotent_and_does_not_double_count(tmp_path: Path) -> None:
    store = JourneyStatusStore(tmp_path)
    store.initialize(now=START)
    cumulative = Usage(
        input_tokens=120,
        output_tokens=30,
        cost_amount=Decimal("0.45"),
        cost_currency="CNY",
    )

    first = store.replace_usage(cumulative, now=START + timedelta(minutes=1))
    first_bytes = store.status_path.read_bytes()
    repeated = store.replace_usage(cumulative, now=START + timedelta(minutes=2))

    assert repeated == first
    assert store.status_path.read_bytes() == first_bytes
    assert repeated.experiment_usage.input_tokens == 120
    assert repeated.experiment_usage.output_tokens == 30
    assert repeated.experiment_usage.cost_amount == Decimal("0.45")

    recalculated = store.replace_usage(
        Usage(
            input_tokens=150,
            output_tokens=40,
            cost_amount=Decimal("0.60"),
            cost_currency="CNY",
        ),
        now=START + timedelta(minutes=3),
    )
    assert recalculated.experiment_usage.input_tokens == 150
    assert recalculated.experiment_usage.output_tokens == 40
    assert recalculated.experiment_usage.cost_amount == Decimal("0.60")


def test_relative_artifact_paths_are_enforced() -> None:
    with pytest.raises(ValidationError, match="relative"):
        _ref("/tmp/report.html")
    with pytest.raises(ValidationError, match="traverse"):
        _ref("reports/../secret.txt")


def test_workspace_and_status_symlinks_are_rejected(tmp_path: Path) -> None:
    real_workspace = tmp_path / "real"
    real_workspace.mkdir()
    linked_workspace = tmp_path / "linked"
    linked_workspace.symlink_to(real_workspace, target_is_directory=True)
    with pytest.raises(JourneyStateError, match="symlink"):
        JourneyStatusStore(linked_workspace)

    status_directory = real_workspace / ".ses"
    status_directory.mkdir()
    target = tmp_path / "outside.json"
    target.write_text("{}", encoding="utf-8")
    (status_directory / "status.json").symlink_to(target)
    with pytest.raises(JourneyStateError, match="symlink"):
        JourneyStatusStore(real_workspace)


def test_environment_credential_cannot_be_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = 'opaque"provider\\value-should-never-persist'
    monkeypatch.setenv("SILICONFLOW_API_KEY", secret)
    commands = list(DEFAULT_STATION_COMMANDS)
    commands[0] = f"runner --opaque-value {secret}"
    store = JourneyStatusStore(tmp_path)

    with pytest.raises(JourneyStateError, match="credential") as error:
        store.initialize(commands, now=START)

    assert secret not in str(error.value)
    assert not store.status_path.exists()


def test_key_shaped_attention_reason_is_rejected_without_echo(
    tmp_path: Path,
) -> None:
    secret = "sk-sensitive-value-12345678"
    store = JourneyStatusStore(tmp_path)
    store.initialize(now=START)
    store.start_station(0, now=START + timedelta(minutes=1))

    with pytest.raises(JourneyStateError) as error:
        store.mark_needs_attention(
            0,
            f"provider returned {secret}",
            now=START + timedelta(minutes=2),
        )

    assert secret not in str(error.value)
    assert secret not in store.status_path.read_text(encoding="utf-8")


def test_failed_atomic_replace_keeps_previous_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JourneyStatusStore(tmp_path)
    initial = store.initialize(now=START)

    def fail_replace(
        source: str,
        destination: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
    ) -> None:
        del source, destination, src_dir_fd, dst_dir_fd
        raise OSError("simulated interruption")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(JourneyStateError, match="atomically"):
        store.start_station(0, now=START + timedelta(minutes=1))

    assert store.load() == initial
    assert not list((tmp_path / ".ses").glob(".status.json.*.tmp"))


def test_load_rejects_noncanonical_or_future_status(tmp_path: Path) -> None:
    store = JourneyStatusStore(tmp_path)
    store.initialize(now=START)
    payload = json.loads(store.status_path.read_text(encoding="utf-8"))
    payload["schema_version"] = "v9"
    store.status_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(JourneyStateError, match="invalid"):
        store.load()


def test_journey_model_rejects_missing_station() -> None:
    # Producer validation prevents a dashboard from silently rendering seven stations.
    with pytest.raises(ValidationError, match="at least 8"):
        JourneyStatus.model_validate(
            {
                "schema_version": "v1alpha1",
                "record_type": "journey_status",
                "status": "pending",
                "current_station": 0,
                "stations": (),
                "experiment_usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cost_amount": "0",
                    "cost_currency": "CNY",
                    "cost_complete": True,
                },
                "created_at": START,
                "updated_at": START,
            }
        )
