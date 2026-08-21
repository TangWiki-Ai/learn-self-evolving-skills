from __future__ import annotations

from ses.journey.course import evaluate_two_door_gate


def test_gate_rejects_net_improvement_when_one_prior_pass_regresses() -> None:
    result = evaluate_two_door_gate(
        accepted_cases={
            "target-a": "agent_fail",
            "target-b": "agent_fail",
            "old-pass": "pass",
        },
        target_statuses={"target-a": "pass", "target-b": "pass"},
        regression_statuses={
            "target-a": "pass",
            "target-b": "pass",
            "old-pass": "agent_fail",
        },
        target_case_ids=("target-a", "target-b"),
    )

    assert result.target_passed is True
    assert result.counts["fail-to-pass"] == 2
    assert result.counts["pass-to-fail"] == 1
    assert result.regression_passed is False
    assert result.accepted is False


def test_gate_accepts_exact_target_fix_with_zero_regressions() -> None:
    result = evaluate_two_door_gate(
        accepted_cases={"target": "agent_fail", "old-pass": "pass"},
        target_statuses={"target": "pass"},
        regression_statuses={"target": "pass", "old-pass": "pass"},
        target_case_ids=("target",),
    )

    assert result.target_passed is True
    assert result.target_pass_count == 1
    assert result.full_regression_ran is True
    assert result.regression_case_set_complete is True
    assert result.regression_case_count == 2
    assert result.candidate_pass_count == 2
    assert result.target_regression_pass_count == 1
    assert result.regression_passed is True
    assert result.counts == {
        "both-pass": 1,
        "both-fail": 0,
        "fail-to-pass": 1,
        "pass-to-fail": 0,
    }
    assert result.accepted is True


def test_gate_rejects_target_that_fails_again_in_full_regression() -> None:
    result = evaluate_two_door_gate(
        accepted_cases={"target": "agent_fail", "old-pass": "pass"},
        target_statuses={"target": "pass"},
        regression_statuses={"target": "agent_fail", "old-pass": "pass"},
        target_case_ids=("target",),
    )

    assert result.target_passed is True
    assert result.regression_passed is False
    assert result.accepted is False


def test_gate_rejects_an_incomplete_full_regression_case_set() -> None:
    result = evaluate_two_door_gate(
        accepted_cases={
            "target": "agent_fail",
            "old-pass": "pass",
            "old-fail": "agent_fail",
        },
        target_statuses={"target": "pass"},
        regression_statuses={"target": "pass", "old-pass": "pass"},
        target_case_ids=("target",),
    )

    assert result.full_regression_ran is True
    assert result.regression_case_set_complete is False
    assert result.regression_case_count == 2
    assert result.candidate_pass_count == 2
    assert result.regression_passed is False
    assert result.accepted is False


def test_gate_rejects_when_target_is_not_fixed_or_no_target_exists() -> None:
    missed = evaluate_two_door_gate(
        accepted_cases={"target": "agent_fail", "old-pass": "pass"},
        target_statuses={"target": "judge_error"},
        regression_statuses={},
        target_case_ids=("target",),
    )
    absent = evaluate_two_door_gate(
        accepted_cases={"old-pass": "pass"},
        target_statuses={},
        regression_statuses={"old-pass": "pass"},
        target_case_ids=(),
    )

    assert missed.target_passed is False
    assert missed.accepted is False
    assert absent.target_passed is False
    assert absent.regression_passed is True
    assert absent.accepted is False
