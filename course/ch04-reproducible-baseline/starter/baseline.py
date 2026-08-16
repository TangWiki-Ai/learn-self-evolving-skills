"""Lesson 4 starter: connect Evaluator, Runner, and L1 reporting."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

AgentTurn = Callable[[str, str | None], tuple[str, str]]
Judge = Callable[[Sequence[Mapping[str, object]]], str]
Evaluate = Callable[[str], Mapping[str, object]]


def evaluate_case(
    case_id: str,
    user_turns: Sequence[str],
    agent_turn: AgentTurn,
    judge: Judge,
) -> Mapping[str, object]:
    """Resume one session across turns, then attach the final Judge decision."""
    del case_id, user_turns, agent_turn, judge
    raise NotImplementedError("Lesson 4: connect Simulator, Agent session, and Judge")


def run_baseline(
    case_ids: Sequence[str], evaluate: Evaluate
) -> list[Mapping[str, object]]:
    """Run each planned case through the evaluator."""
    del case_ids, evaluate
    raise NotImplementedError("Lesson 4: connect Evaluator to Runner")


def build_l1_report(
    records: Sequence[Mapping[str, object]], k: int
) -> Mapping[str, object]:
    """Build L1 metrics and preserve per-case evidence records."""
    del records, k
    raise NotImplementedError("Lesson 4: connect Runner records to L1")
