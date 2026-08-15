from __future__ import annotations

import pytest

from ses.testset.difficulty import (
    TauAggregationError,
    ThresholdDifficultyPolicy,
    aggregate_tau_difficulty,
)


def result_documents() -> dict[str, object]:
    documents: dict[str, object] = {}
    # Four result files x four trials = sixteen runs for every task.
    success_counts_by_file = {
        "model-a.json": (4, 2, 3),
        "model-b.json": (4, 1, 0),
        "model-c.json": (4, 1, 0),
        "model-d.json": (0, 1, 0),
    }
    for filename, counts in success_counts_by_file.items():
        simulations: list[dict[str, object]] = []
        for task_index, successes in enumerate(counts):
            for trial in range(4):
                simulations.append(
                    {
                        "task_id": str(task_index),
                        "trial": trial,
                        "reward_info": {"reward": 1.0 if trial < successes else 0.0},
                    }
                )
        documents[filename] = {
            "info": {"git_commit": f"generation-{filename}"},
            "simulations": simulations,
        }
    return documents


def tau_tasks() -> list[dict[str, object]]:
    return [
        {
            "id": str(index),
            "user_scenario": {
                "instructions": {"reason_for_call": f"retail benchmark task {index}"}
            },
        }
        for index in range(3)
    ]


def test_tau_runs_are_aggregated_by_task_before_difficulty() -> None:
    summaries = aggregate_tau_difficulty(
        tau_tasks(),
        result_documents(),
    )

    assert len(summaries) == 3
    assert sum(summary.run_count for summary in summaries) == 48
    assert [summary.run_count for summary in summaries] == [16, 16, 16]
    assert [summary.success_count for summary in summaries] == [12, 5, 3]
    assert [summary.pass_rate for summary in summaries] == [0.75, 0.3125, 0.1875]
    assert [summary.difficulty_bucket for summary in summaries] == [
        "easy",
        "medium",
        "hard",
    ]
    assert summaries[0].generation_commits == (
        "generation-model-a.json",
        "generation-model-b.json",
        "generation-model-c.json",
        "generation-model-d.json",
    )


def test_tau_aggregation_rejects_missing_trials_instead_of_biasing_difficulty() -> None:
    documents = result_documents()
    first = documents["model-a.json"]
    assert isinstance(first, dict)
    simulations = first["simulations"]
    assert isinstance(simulations, list)
    simulations.pop()

    with pytest.raises(TauAggregationError, match="expected 16 runs"):
        aggregate_tau_difficulty(tau_tasks(), documents)


def test_tau_aggregation_rejects_unknown_task_ids() -> None:
    documents = result_documents()
    first = documents["model-a.json"]
    assert isinstance(first, dict)
    simulations = first["simulations"]
    assert isinstance(simulations, list)
    simulations[0]["task_id"] = "not-in-task-snapshot"

    with pytest.raises(TauAggregationError, match="unknown task"):
        aggregate_tau_difficulty(tau_tasks(), documents)


def test_tau_aggregation_rejects_duplicate_asset_task_trial_keys() -> None:
    documents = result_documents()
    first = documents["model-a.json"]
    assert isinstance(first, dict)
    simulations = first["simulations"]
    assert isinstance(simulations, list)
    simulations.append(dict(simulations[0]))

    with pytest.raises(TauAggregationError, match="duplicate tau2 run key"):
        aggregate_tau_difficulty(tau_tasks(), documents)


def test_tau_aggregation_rejects_trial_set_drift_even_with_sixteen_runs() -> None:
    documents = result_documents()
    first = documents["model-a.json"]
    assert isinstance(first, dict)
    simulations = first["simulations"]
    assert isinstance(simulations, list)
    simulations[3]["trial"] = 4

    with pytest.raises(TauAggregationError, match="trials drifted"):
        aggregate_tau_difficulty(tau_tasks(), documents)


def test_tau_aggregation_rejects_non_binary_rewards() -> None:
    documents = result_documents()
    first = documents["model-a.json"]
    assert isinstance(first, dict)
    simulations = first["simulations"]
    assert isinstance(simulations, list)
    simulations[0]["reward_info"] = {"reward": 0.5}

    with pytest.raises(TauAggregationError, match="reward must be 0 or 1"):
        aggregate_tau_difficulty(tau_tasks(), documents)


def test_tau_aggregation_requires_exactly_four_result_files() -> None:
    documents = result_documents()
    documents["empty-extra.json"] = {
        "info": {"git_commit": "generation-extra"},
        "simulations": [],
    }

    with pytest.raises(TauAggregationError, match="expected 4 result files"):
        aggregate_tau_difficulty(tau_tasks(), documents)


def test_difficulty_bucket_boundaries_are_fixed_integer_counts() -> None:
    policy = ThresholdDifficultyPolicy()

    assert policy.bucket(4, 16) == "hard"
    assert policy.bucket(5, 16) == "medium"
    assert policy.bucket(11, 16) == "medium"
    assert policy.bucket(12, 16) == "easy"
