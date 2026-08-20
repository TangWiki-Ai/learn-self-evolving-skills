from __future__ import annotations

from collections.abc import Sequence

import pytest

from ses.cli import shopping_capstone
from ses.cli.app import main


def test_capstone_index_routes_to_the_shopping_completion_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[str] = []

    def _capture(argv: Sequence[str]) -> int:
        received.extend(argv)
        return 0

    monkeypatch.setattr(shopping_capstone, "capstone_index_main", _capture)

    exit_code = main(
        [
            "capstone-index",
            "--profile",
            "fixed-v1.json",
            "--experiment-root",
            "experiment",
            "--output",
            "experiment/capstone-index.json",
        ]
    )

    assert exit_code == 0
    assert received == [
        "--profile",
        "fixed-v1.json",
        "--experiment-root",
        "experiment",
        "--output",
        "experiment/capstone-index.json",
    ]
