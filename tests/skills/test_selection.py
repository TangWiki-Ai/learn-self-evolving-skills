from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ses.skills.creator import FakeCreator
from ses.skills.selection import CandidateMode, select_demo_skill


def _write_manifest(
    source: Path, files: list[str], *, name: str = "custom-return"
) -> None:
    payload = {
        "schema_version": "v1alpha1",
        "record_type": "skill_artifact_manifest",
        "name": name,
        "version": "candidate-v1",
        "files": [
            {
                "path": relative,
                "sha256": hashlib.sha256((source / relative).read_bytes()).hexdigest(),
            }
            for relative in files
        ],
    }
    (source / "skill-manifest.json").write_text(json.dumps(payload), encoding="utf-8")


def _candidate(tmp_path: Path, body: str) -> Path:
    source = tmp_path / "candidate"
    source.mkdir()
    (source / "SKILL.md").write_text(body, encoding="utf-8")
    _write_manifest(source, ["SKILL.md"])
    return source


def test_learner_can_select_a_generated_candidate(tmp_path: Path) -> None:
    selected = select_demo_skill(
        tmp_path / "selection",
        mode=CandidateMode.GENERATE,
        creator=FakeCreator(),
    )

    assert selected.source_label == "generated"
    assert selected.fallback_reason is None
    assert selected.manifest.version == "demo-v1"


def test_learner_can_supply_a_candidate_path(tmp_path: Path) -> None:
    candidate = _candidate(
        tmp_path,
        """---
name: custom-return
description: Use for customer return requests.
version: candidate-v1
---
# Safe return workflow
Inspect the order and policy. Preview the return. Confirm only after checking
the amount and requested item. Verify the final state and report tool evidence.
""",
    )

    selected = select_demo_skill(
        tmp_path / "selection",
        mode=CandidateMode.CANDIDATE,
        candidate_source=candidate,
    )

    assert selected.source == candidate
    assert selected.source_label == "candidate"
    assert selected.fallback_reason is None


def test_learner_can_explicitly_use_the_packaged_reference(tmp_path: Path) -> None:
    selected = select_demo_skill(
        tmp_path / "selection",
        mode=CandidateMode.REFERENCE,
    )

    assert selected.source_label == "reference"
    assert selected.fallback_reason is None
    assert selected.source.is_relative_to(tmp_path)
    assert selected.source.joinpath("skill-manifest.json").is_file()


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("# Tiny\nDo returns.\n", "weak_content"),
        (
            "# Missing front matter\n" + "Inspect preview confirm verify. " * 12,
            "invalid_structure",
        ),
    ],
)
def test_weak_or_structurally_invalid_candidate_records_reference_fallback(
    tmp_path: Path, body: str, reason: str
) -> None:
    candidate = _candidate(tmp_path, body)

    selected = select_demo_skill(
        tmp_path / "selection",
        mode=CandidateMode.CANDIDATE,
        candidate_source=candidate,
    )

    assert selected.source_label == "reference_fallback"
    assert selected.fallback_reason is not None
    assert selected.fallback_reason.startswith(reason + ":")


def test_uninstallable_candidate_records_reference_fallback(tmp_path: Path) -> None:
    candidate = _candidate(
        tmp_path,
        """---
name: custom-return
description: Use for customer return requests.
version: candidate-v1
---
Inspect the order and policy. Preview the return. Confirm it after checking the
amount and item. Verify the final state and report the tool evidence clearly.
""",
    )
    (candidate / "SKILL.md").write_text("tampered after manifest", encoding="utf-8")

    selected = select_demo_skill(
        tmp_path / "selection",
        mode=CandidateMode.CANDIDATE,
        candidate_source=candidate,
    )

    assert selected.source_label == "reference_fallback"
    assert selected.fallback_reason is not None
    assert selected.fallback_reason.startswith("uninstallable:")
