"""Task-level tau2 aggregation and deterministic difficulty buckets."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import cast

from ses.testset.sources import TAU2_COMMIT

TAU_RUNS_PER_TASK = 16
TAU_TRIALS_PER_ASSET = 4
TAU_RESULT_FILES = 4


class TauAggregationError(ValueError):
    """tau2 runs cannot form the required complete task-level aggregates."""


@dataclass(frozen=True)
class ThresholdDifficultyPolicy:
    easy_min: Decimal | float | str = Decimal("0.75")
    hard_max: Decimal | float | str = Decimal("0.25")

    def __post_init__(self) -> None:
        easy = Decimal(str(self.easy_min))
        hard = Decimal(str(self.hard_max))
        if not Decimal(0) <= hard < easy <= Decimal(1):
            raise ValueError("difficulty thresholds must satisfy 0 <= hard < easy <= 1")
        object.__setattr__(self, "easy_min", easy)
        object.__setattr__(self, "hard_max", hard)

    def bucket(self, success_count: int, run_count: int) -> str:
        rate = Decimal(success_count) / Decimal(run_count)
        hard = cast(Decimal, self.hard_max)
        easy = cast(Decimal, self.easy_min)
        if rate <= hard:
            return "hard"
        if rate >= easy:
            return "easy"
        return "medium"


@dataclass(frozen=True)
class TauRun:
    result_asset_id: str
    task_id: str
    trial: int
    reward: Decimal
    simulation_id: str | None
    generation_commit: str | None


@dataclass(frozen=True)
class PerAssetDifficulty:
    result_asset_id: str
    success_count: int
    run_count: int


@dataclass(frozen=True)
class TauDifficulty:
    source_id: str
    task_id: str
    task_text: str
    run_count: int
    success_count: int
    pass_rate: float
    pass_rate_decimal: str
    mean_reward: float
    difficulty_score: float
    difficulty_bucket: str
    per_asset: tuple[PerAssetDifficulty, ...]
    generation_commits: tuple[str, ...]


def _task_text(task: Mapping[str, object]) -> str:
    scenario = task.get("user_scenario")
    if not isinstance(scenario, Mapping):
        return ""
    instructions = scenario.get("instructions")
    if not isinstance(instructions, Mapping):
        return ""
    reason = instructions.get("reason_for_call")
    return reason if isinstance(reason, str) else ""


def _parse_reward(value: object, context: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise TauAggregationError(f"{context} reward must be 0 or 1")
    try:
        reward = Decimal(str(value))
    except InvalidOperation as exc:
        raise TauAggregationError(f"{context} reward must be 0 or 1") from exc
    if not reward.is_finite() or reward not in {Decimal(0), Decimal(1)}:
        raise TauAggregationError(f"{context} reward must be 0 or 1")
    return reward


def _parse_result_document(
    result_asset_id: str, document: object
) -> tuple[TauRun, ...]:
    if not isinstance(document, Mapping):
        raise TauAggregationError(f"{result_asset_id} must be a JSON object")
    info = document.get("info")
    generation_commit: str | None = None
    if isinstance(info, Mapping):
        raw_commit = info.get("git_commit")
        if isinstance(raw_commit, str):
            generation_commit = raw_commit
    simulations = document.get("simulations")
    if not isinstance(simulations, Sequence) or isinstance(
        simulations, (str, bytes, bytearray)
    ):
        raise TauAggregationError(f"{result_asset_id} simulations must be a list")
    runs: list[TauRun] = []
    for index, raw in enumerate(simulations):
        context = f"{result_asset_id}.simulations[{index}]"
        if not isinstance(raw, Mapping):
            raise TauAggregationError(f"{context} must be an object")
        task_id = raw.get("task_id")
        trial = raw.get("trial")
        reward_info = raw.get("reward_info")
        if not isinstance(task_id, str) or not task_id:
            raise TauAggregationError(f"{context} has no task_id")
        if not isinstance(trial, int) or isinstance(trial, bool):
            raise TauAggregationError(f"{context} has no integer trial")
        if not isinstance(reward_info, Mapping):
            raise TauAggregationError(f"{context} has no reward_info")
        simulation_id = raw.get("id")
        if simulation_id is not None and not isinstance(simulation_id, str):
            raise TauAggregationError(f"{context} simulation id must be a string")
        runs.append(
            TauRun(
                result_asset_id=result_asset_id,
                task_id=task_id,
                trial=trial,
                reward=_parse_reward(reward_info.get("reward"), context),
                simulation_id=simulation_id,
                generation_commit=generation_commit,
            )
        )
    return tuple(runs)


def _task_sort_key(task_id: str) -> tuple[int, int | str]:
    if task_id.isdigit():
        return (0, int(task_id))
    return (1, task_id)


def aggregate_tau_difficulty(
    tasks: Sequence[Mapping[str, object]],
    result_documents: Mapping[str, object],
) -> tuple[TauDifficulty, ...]:
    """Group every run by task, validate 4x4 provenance, then compute difficulty."""

    policy = ThresholdDifficultyPolicy()
    if len(result_documents) != TAU_RESULT_FILES:
        raise TauAggregationError(
            f"tau2 expected {TAU_RESULT_FILES} result files, got {len(result_documents)}"
        )
    task_by_id: dict[str, Mapping[str, object]] = {}
    for task in tasks:
        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise TauAggregationError("tau2 task has no string id")
        if task_id in task_by_id:
            raise TauAggregationError(f"duplicate tau2 task id: {task_id}")
        task_by_id[task_id] = task
    if not task_by_id:
        raise TauAggregationError("tau2 task snapshot is empty")

    runs_by_task: dict[str, list[TauRun]] = {task_id: [] for task_id in task_by_id}
    seen_keys: set[tuple[str, str, int]] = set()
    for result_asset_id, document in result_documents.items():
        for run in _parse_result_document(result_asset_id, document):
            if run.task_id not in task_by_id:
                raise TauAggregationError(
                    f"{result_asset_id} references unknown task {run.task_id}"
                )
            key = (run.result_asset_id, run.task_id, run.trial)
            if key in seen_keys:
                raise TauAggregationError(f"duplicate tau2 run key: {key}")
            seen_keys.add(key)
            runs_by_task[run.task_id].append(run)

    expected_trials = set(range(TAU_TRIALS_PER_ASSET))
    summaries: list[TauDifficulty] = []
    for task_id in sorted(task_by_id, key=_task_sort_key):
        runs = runs_by_task[task_id]
        if len(runs) != TAU_RUNS_PER_TASK:
            raise TauAggregationError(
                f"task {task_id} expected {TAU_RUNS_PER_TASK} runs, got {len(runs)}"
            )
        by_asset: dict[str, list[TauRun]] = {}
        for run in runs:
            by_asset.setdefault(run.result_asset_id, []).append(run)
        per_asset: list[PerAssetDifficulty] = []
        for asset_id in sorted(by_asset):
            asset_runs = by_asset[asset_id]
            trials = {run.trial for run in asset_runs}
            if trials != expected_trials:
                raise TauAggregationError(
                    f"task {task_id} in {asset_id} trials drifted: {sorted(trials)}"
                )
            per_asset.append(
                PerAssetDifficulty(
                    result_asset_id=asset_id,
                    success_count=sum(run.reward == Decimal(1) for run in asset_runs),
                    run_count=len(asset_runs),
                )
            )
        success_count = sum(run.reward == Decimal(1) for run in runs)
        pass_rate_decimal = Decimal(success_count) / Decimal(len(runs))
        mean_reward = sum((run.reward for run in runs), Decimal(0)) / Decimal(len(runs))
        generation_commits = tuple(
            sorted(
                {
                    run.generation_commit
                    for run in runs
                    if run.generation_commit is not None
                }
            )
        )
        summaries.append(
            TauDifficulty(
                source_id=f"tau2:{TAU2_COMMIT}:{task_id}",
                task_id=task_id,
                task_text=_task_text(task_by_id[task_id]),
                run_count=len(runs),
                success_count=success_count,
                pass_rate=float(pass_rate_decimal),
                pass_rate_decimal=str(pass_rate_decimal),
                mean_reward=float(mean_reward),
                difficulty_score=float(Decimal(1) - pass_rate_decimal),
                difficulty_bucket=policy.bucket(success_count, len(runs)),
                per_asset=tuple(per_asset),
                generation_commits=generation_commits,
            )
        )
    return tuple(summaries)
