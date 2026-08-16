"""Lesson 6 starter: implement Verify, Calibrate, and Split."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path


def verify_variant(candidate: object, dimensions: object) -> object:
    del candidate, dimensions
    raise NotImplementedError("Lesson 6: verify a controlled policy variant")


def calibrate_case(case: object) -> object:
    del case
    raise NotImplementedError(
        "Lesson 6: calibrate correct, incorrect, and missing evidence"
    )


def protect_split(cases: Sequence[object], manifests: Sequence[Path]) -> None:
    del cases, manifests
    raise NotImplementedError("Lesson 6: protect locked split identities and content")
