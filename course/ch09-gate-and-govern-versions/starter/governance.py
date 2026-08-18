"""Lesson 9 starter: implement conservative gating and version governance."""

from __future__ import annotations


def gate_candidate(*args: object, **kwargs: object) -> object:
    del args, kwargs
    raise NotImplementedError("Lesson 9: implement the ordered candidate Gate")


def record_and_maybe_promote(*args: object, **kwargs: object) -> object:
    del args, kwargs
    raise NotImplementedError("Lesson 9: record the decision before promotion")


def rollback(*args: object, **kwargs: object) -> object:
    del args, kwargs
    raise NotImplementedError("Lesson 9: rollback only to verified history")


def govern(*args: object, **kwargs: object) -> object:
    del args, kwargs
    raise NotImplementedError("Lesson 9: connect Gate, Registry, and rollback")
