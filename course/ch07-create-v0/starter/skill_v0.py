"""Lesson 7 starter: implement the four Skill v0 evaluation seams."""

from pathlib import Path


def load_seeds(manifest: Path) -> object:
    del manifest
    raise NotImplementedError("Lesson 7: validate exactly nine creator seeds")


def static_gate(skill: Path) -> object:
    del skill
    raise NotImplementedError("Lesson 7: implement the zero-cost Static Gate")


def trigger_eval(skill_hash: str, discovery: object) -> object:
    del skill_hash, discovery
    raise NotImplementedError("Lesson 7: measure native trigger precision and recall")


def paired_compare(skill: Path, output: Path, project_root: Path) -> object:
    del skill, output, project_root
    raise NotImplementedError("Lesson 7: run fresh paired develop cases")
