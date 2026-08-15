from __future__ import annotations

import json
import sys
from hashlib import sha256
from pathlib import Path

import pytest

from ses.testset.acquisition import UrlFetcher
from ses.testset.difficulty import aggregate_tau_difficulty
from ses.testset.manifest import load_manifest, verify_manifest_files
from ses.testset.profiles import expected_counts_for_profile, load_mining_inputs
from ses.testset.scrub import scrub_abcd
from ses.testset.sources import (
    filter_abcd_product_defect,
    filter_state_return_items,
    match_state_trajectories,
)

ROOT = Path(__file__).resolve().parents[2]
UPSTREAM_ROOT = ROOT / "data" / "upstream"
sys.path.insert(0, str(ROOT / "scripts"))

import prepare_data  # noqa: E402


def test_repository_manifest_and_fixture_profile_are_self_consistent() -> None:
    manifest = load_manifest(UPSTREAM_ROOT / "manifest.json")

    verify_manifest_files(manifest, UPSTREAM_ROOT)
    inputs = load_mining_inputs(manifest, UPSTREAM_ROOT, profile="fixture")
    expected = expected_counts_for_profile("fixture")

    assert len(inputs.state_tasks) == expected.state_tasks
    assert len(inputs.state_trajectories) == expected.state_trajectories
    assert len(inputs.abcd_conversations) == expected.abcd_conversations
    assert len(inputs.tau_tasks) == expected.tau_tasks
    assert len(inputs.tau_result_documents) == expected.tau_result_files
    assert (
        manifest.manifest_sha256
        == sha256((UPSTREAM_ROOT / "manifest.json").read_bytes()).hexdigest()
    )
    assert inputs.upstream_manifest_sha256 == manifest.manifest_sha256
    assert len(inputs.input_sha256) == 8
    assert manifest.source("abcd").assets[0].sha256 == (
        "2bdf53ac359543dcdc38d55bc6513e78df120363f8f44870716e909f4606de15"
    )
    assert manifest.source("tau2").slice["reward_json_path"] == "reward_info.reward"
    tau_assets = {asset.name: asset for asset in manifest.source("tau2").assets}
    assert tau_assets["tau2_result_gpt_4_1_mini"].generation_commit == (
        "ade39493be54aad326a4c65295f77fe09780329b"
    )
    assert set(inputs.tau_result_documents) == {
        "tau2_result_claude",
        "tau2_result_gpt_4_1",
        "tau2_result_gpt_4_1_mini",
        "tau2_result_o4_mini",
    }

    return_tasks = filter_state_return_items(inputs.state_tasks)
    product_defect = filter_abcd_product_defect(inputs.abcd_conversations)
    difficulty = aggregate_tau_difficulty(inputs.tau_tasks, inputs.tau_result_documents)
    assert len(return_tasks) == 2
    assert len(match_state_trajectories(return_tasks, inputs.state_trajectories)) == 1
    assert len(product_defect) == 2
    scrubbed = scrub_abcd(product_defect).records
    assert [record.source_split for record in scrubbed] == ["train", "train"]
    assert all(":train:" in record.source_id for record in scrubbed)
    assert [summary.task_id for summary in difficulty] == ["27", "53"]
    assert [summary.run_count for summary in difficulty] == [16, 16]
    assert [summary.success_count for summary in difficulty] == [5, 15]
    assert [summary.difficulty_bucket for summary in difficulty] == ["medium", "easy"]


def test_verify_only_never_constructs_a_network_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = 0

    def fail_if_called(self: UrlFetcher, url: str, timeout: float) -> object:
        del self, url, timeout
        nonlocal calls
        calls += 1
        raise AssertionError("network must remain disabled")

    monkeypatch.setattr(UrlFetcher, "open", fail_if_called)

    result = prepare_data.main(
        [
            "--verify-only",
            "--manifest",
            str(UPSTREAM_ROOT / "manifest.json"),
            "--data-root",
            str(UPSTREAM_ROOT),
        ]
    )

    assert result == 0
    assert calls == 0
    output = json.loads(capsys.readouterr().out)
    assert output["network_used"] is False


def test_fixture_cli_runs_real_local_adapters_without_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_if_called(self: UrlFetcher, url: str, timeout: float) -> object:
        del self, url, timeout
        raise AssertionError("fixture mining must remain offline")

    monkeypatch.setattr(UrlFetcher, "open", fail_if_called)
    output_dir = tmp_path / "fixture-bundle"

    result = prepare_data.main(
        [
            "--profile",
            "fixture",
            "--clusters",
            "2",
            "--manifest",
            str(UPSTREAM_ROOT / "manifest.json"),
            "--data-root",
            str(UPSTREAM_ROOT),
            "--output",
            str(output_dir),
        ]
    )

    assert result == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "ok"
    assert summary["profile"] == "fixture"
    assert summary["output"] == str(output_dir)
    assert (output_dir / "artifact-manifest.json").is_file()
    assert (output_dir / "cluster-summaries.jsonl").is_file()
    assert (output_dir / "candidate-list.jsonl").is_file()


def test_full_download_requires_explicit_network_before_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fail_if_called(self: UrlFetcher, url: str, timeout: float) -> object:
        del self, url, timeout
        nonlocal calls
        calls += 1
        raise AssertionError("network must remain disabled")

    monkeypatch.setattr(UrlFetcher, "open", fail_if_called)

    with pytest.raises(SystemExit):
        prepare_data.main(["--download-full", "--download-only"])

    assert calls == 0


def test_verify_only_rejects_download_flags_before_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def fail_if_called(self: UrlFetcher, url: str, timeout: float) -> object:
        del self, url, timeout
        nonlocal calls
        calls += 1
        raise AssertionError("network must remain disabled")

    monkeypatch.setattr(UrlFetcher, "open", fail_if_called)

    with pytest.raises(SystemExit):
        prepare_data.main(["--verify-only", "--download-full", "--allow-network"])

    assert calls == 0
