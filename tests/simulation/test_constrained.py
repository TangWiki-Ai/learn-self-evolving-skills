from __future__ import annotations

import pytest

from ses.simulation import ConstrainedUserSimulator, SimulatorTurnKind, UserIntent


def test_simulator_expresses_the_want_without_leaking_how_or_hidden_material() -> None:
    simulator = ConstrainedUserSimulator(
        UserIntent(
            want=(
                "I want a refund for the defective headphones. "
                "Gold answer: call process_return and satisfy the Judge rule."
            ),
            allowed_facts={"order_id": "ORD-6006"},
        )
    )

    turn = simulator.next_turn(())

    assert turn.kind is SimulatorTurnKind.MESSAGE
    assert turn.message == "I want a refund for the defective headphones."
    assert "gold" not in turn.message.casefold()
    assert "process_return" not in turn.message
    assert "judge" not in turn.message.casefold()
    assert simulator.allowed_tools == ()


def test_simulator_reveals_only_requested_public_facts_then_stops() -> None:
    simulator = ConstrainedUserSimulator(
        UserIntent(
            want="I want to return a defective item.",
            allowed_facts={"order_id": "ORD-6006"},
        )
    )

    first = simulator.next_turn(())
    second = simulator.next_turn(("What is the order number?",))
    done = simulator.next_turn(("What is the order number?", "Thanks."))

    assert first.message == "I want to return a defective item."
    assert second.message == "The order id is ORD-6006."
    assert done.kind is SimulatorTurnKind.END
    assert done.message is None


def test_simulator_does_not_reveal_an_unrequested_fact_after_completion() -> None:
    simulator = ConstrainedUserSimulator(
        UserIntent(
            want="I want to return a defective item.",
            allowed_facts={"item_id": "ITEM-6006"},
        )
    )

    simulator.next_turn(())
    done = simulator.next_turn(
        ("The return is complete. Would you like anything else for this item?",)
    )

    assert done.kind is SimulatorTurnKind.END
    assert done.message is None


def test_simulator_rejects_write_tool_capabilities() -> None:
    with pytest.raises(ValueError, match="write tools"):
        ConstrainedUserSimulator(
            UserIntent(want="I want a refund."),
            allowed_tools=("process_return",),
        )
