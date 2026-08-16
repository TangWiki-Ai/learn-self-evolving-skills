"""Lesson 4 solution: connect Evaluator, Runner, and L1 reporting."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence

AgentTurn = Callable[[str, str | None], tuple[str, str]]
Judge = Callable[[Sequence[Mapping[str, object]]], str]
Evaluate = Callable[[str], Mapping[str, object]]
EVALUATED = frozenset(
    {"pass", "agent_fail", "simulator_error", "judge_error", "infrastructure_error"}
)


def evaluate_case(
    case_id: str,
    user_turns: Sequence[str],
    agent_turn: AgentTurn,
    judge: Judge,
) -> Mapping[str, object]:
    """Resume one session across turns, then attach the final Judge decision."""
    session_id: str | None = None
    transcript: list[Mapping[str, object]] = []
    for user_message in user_turns:
        assistant_message, returned_session = agent_turn(user_message, session_id)
        if session_id is not None and returned_session != session_id:
            raise ValueError("agent changed session within one case")
        session_id = returned_session
        transcript.extend(
            (
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": assistant_message},
            )
        )
    status = judge(transcript)
    if status not in EVALUATED:
        raise ValueError("judge returned an unsupported status")
    return {
        "case_id": case_id,
        "status": status,
        "turn_count": len(user_turns),
        "session_id": session_id,
        "transcript": transcript,
    }


def run_baseline(
    case_ids: Sequence[str], evaluate: Evaluate
) -> list[Mapping[str, object]]:
    """Run each unique planned case through the evaluator."""
    if not case_ids or len(set(case_ids)) != len(case_ids):
        raise ValueError("case plan must be nonempty and unique")
    return [evaluate(case_id) for case_id in case_ids]


def _iteration(record: Mapping[str, object]) -> int:
    value = record.get("iteration", 0)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("iteration must be an integer")
    return value


def _reliability(
    records: Sequence[Mapping[str, object]], k: int
) -> tuple[int, int, float, float]:
    if k < 1:
        raise ValueError("k must be at least one")
    grouped: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    for record in records:
        case_id = record.get("case_id")
        if isinstance(case_id, str) and record.get("status") in EVALUATED:
            grouped[case_id].append(record)
    if not grouped:
        raise ValueError("baseline requires at least one evaluated case")
    for values in grouped.values():
        values.sort(key=_iteration)
    first_passes = sum(values[0].get("status") == "pass" for values in grouped.values())
    reliable = sum(
        len(values) >= k and all(value.get("status") == "pass" for value in values[:k])
        for values in grouped.values()
    )
    total = len(grouped)
    return first_passes, total, first_passes / total, reliable / total


def build_l1_report(
    records: Sequence[Mapping[str, object]], k: int
) -> Mapping[str, object]:
    """Build L1 metrics and preserve per-case evidence records."""
    passed, total, pass_at_1, pass_power_k = _reliability(records, k)
    return {
        "metrics": {
            "first_passes": passed,
            "sample_size": total,
            "pass_at_1": pass_at_1,
            "pass_power_k": pass_power_k,
            "k": k,
        },
        "records": list(records),
    }
