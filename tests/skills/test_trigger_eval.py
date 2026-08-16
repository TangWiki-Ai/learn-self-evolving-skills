from __future__ import annotations

from collections import deque

from ses.skills.trigger_eval import (
    TRIGGER_PROMPTS,
    DiscoveryObservation,
    DiscoveryStatus,
    evaluate_triggers,
)


class QueueDiscovery:
    def __init__(self, statuses: list[DiscoveryStatus]) -> None:
        self.statuses = deque(statuses)

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
        discovery=backend,
    )

    assert result.indeterminate_count == 1
    assert result.tp == 9
    assert result.fn == 0
    assert result.recall == 1.0
    assert result.prompts[0].actual is DiscoveryStatus.INDETERMINATE
