from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ses.journey.course import JourneyCourseError, run_station_6
from ses.skills.installer import (
    load_skill_manifest,
    normalized_skill_sha256,
    write_skill_manifest,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SEED_SKILL = PROJECT_ROOT / "fixtures/seed/skill/v0"


def _copy_skill(destination: Path, *, addition: str | None = None) -> str:
    shutil.copytree(SEED_SKILL, destination)
    if addition is not None:
        manifest = load_skill_manifest(destination)
        skill_path = destination / "SKILL.md"
        skill_path.write_text(
            skill_path.read_text(encoding="utf-8").rstrip() + f"\n\n{addition}\n",
            encoding="utf-8",
        )
        (destination / "skill-manifest.json").unlink()
        write_skill_manifest(
            destination,
            name=manifest.name,
            version="candidate",
            files=tuple(item.path for item in manifest.files),
            source_version="journey-release-test",
            provider_compatibility=manifest.provider_compatibility,
        )
    return normalized_skill_sha256(destination)


def _write_release_state(
    workspace: Path,
    *,
    current_candidate: Path,
    current_hash: str,
    gate_candidate_hash: str,
    parent_hash: str,
    gate_parent_hash: str | None = None,
) -> None:
    journey_root = workspace / ".ses"
    (journey_root / "evidence").mkdir(parents=True, exist_ok=True)
    (journey_root / "current-candidate.json").write_text(
        json.dumps(
            {
                "candidate_path": current_candidate.relative_to(workspace).as_posix(),
                "candidate_skill_sha256": current_hash,
                "parent_skill_sha256": parent_hash,
            }
        ),
        encoding="utf-8",
    )
    gate_path = journey_root / "evidence/gate.json"
    gate_path.write_text(
        json.dumps(
            {
                "candidate_skill_sha256": gate_candidate_hash,
                "outcome": "accepted",
                "parent_skill_sha256": gate_parent_hash or parent_hash,
            }
        ),
        encoding="utf-8",
    )
    (journey_root / "evidence/station-5.json").write_text(
        json.dumps({"gate_path": gate_path.relative_to(workspace).as_posix()}),
        encoding="utf-8",
    )


def test_release_rejects_a_current_candidate_not_accepted_by_the_gate(
    tmp_path: Path,
) -> None:
    journey_root = tmp_path / ".ses"
    parent_hash = _copy_skill(journey_root / "versions/v0")
    candidate_a = journey_root / "candidates/candidate-a"
    candidate_a_hash = _copy_skill(candidate_a, addition="Candidate A rule.")
    candidate_b = journey_root / "candidates/candidate-b"
    candidate_b_hash = _copy_skill(candidate_b, addition="Candidate B rule.")
    _write_release_state(
        tmp_path,
        current_candidate=candidate_b,
        current_hash=candidate_b_hash,
        gate_candidate_hash=candidate_a_hash,
        parent_hash=parent_hash,
    )

    with pytest.raises(JourneyCourseError, match="does not match"):
        run_station_6(workspace=tmp_path, action="release")

    assert not (journey_root / "versions/v1").exists()


def test_release_rejects_a_candidate_with_no_runtime_change(tmp_path: Path) -> None:
    journey_root = tmp_path / ".ses"
    parent_hash = _copy_skill(journey_root / "versions/v0")
    candidate = journey_root / "candidates/candidate-no-change"
    candidate_hash = _copy_skill(candidate)
    assert candidate_hash == parent_hash
    _write_release_state(
        tmp_path,
        current_candidate=candidate,
        current_hash=candidate_hash,
        gate_candidate_hash=candidate_hash,
        parent_hash=parent_hash,
    )

    with pytest.raises(JourneyCourseError, match="no runtime change"):
        run_station_6(workspace=tmp_path, action="release")

    assert not (journey_root / "versions/v1").exists()


def test_release_requires_the_gate_to_bind_the_same_parent(tmp_path: Path) -> None:
    journey_root = tmp_path / ".ses"
    parent_hash = _copy_skill(journey_root / "versions/v0")
    candidate = journey_root / "candidates/candidate-a"
    candidate_hash = _copy_skill(candidate, addition="Candidate A rule.")
    _write_release_state(
        tmp_path,
        current_candidate=candidate,
        current_hash=candidate_hash,
        gate_candidate_hash=candidate_hash,
        parent_hash=parent_hash,
        gate_parent_hash="f" * 64,
    )

    with pytest.raises(JourneyCourseError, match="parent"):
        run_station_6(workspace=tmp_path, action="release")

    assert not (journey_root / "versions/v1").exists()


def test_release_copies_the_exact_gate_accepted_candidate(tmp_path: Path) -> None:
    journey_root = tmp_path / ".ses"
    parent_hash = _copy_skill(journey_root / "versions/v0")
    candidate = journey_root / "candidates/candidate-a"
    candidate_hash = _copy_skill(candidate, addition="Candidate A rule.")
    _write_release_state(
        tmp_path,
        current_candidate=candidate,
        current_hash=candidate_hash,
        gate_candidate_hash=candidate_hash,
        parent_hash=parent_hash,
    )

    result = run_station_6(workspace=tmp_path, action="release")

    assert result.status == "completed"
    assert normalized_skill_sha256(journey_root / "versions/v1") == candidate_hash
