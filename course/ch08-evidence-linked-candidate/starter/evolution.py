"""Lesson 8 starter: implement the evidence-linked candidate seams."""

from __future__ import annotations

from pathlib import Path


def analyze(evidence: Path) -> object:
    del evidence
    raise NotImplementedError("Lesson 8: implement ordered failure attribution")


def create_candidate(*args: object, **kwargs: object) -> object:
    del args, kwargs
    raise NotImplementedError("Lesson 8: implement atomic candidate creation")


def evolve(*args: object, **kwargs: object) -> object:
    del args, kwargs
    raise NotImplementedError("Lesson 8: connect analysis, Updater, and candidate")
