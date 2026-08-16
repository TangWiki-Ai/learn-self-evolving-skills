from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ses.skills.seeds import CreatorSeedError, load_creator_seed_pack


def _write_pack(root: Path, *, count: int = 9, split: str = "creator") -> Path:
    projections = root / "projections"
    projections.mkdir(parents=True)
    records = []
    for index in range(count):
        source = root / "private" / "traces" / f"trace-{index + 1:03d}.json"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text('{"outcome":"success"}', encoding="utf-8")
        path = projections / f"seed-{index + 1:03d}.json"
        path.write_text(
            json.dumps(
                {
                    "scenario": "defective item return",
                    "reusable_steps": ["inspect", "preview", "confirm", "verify"],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        records.append(
            {
                "seed_id": f"creator-seed-{index + 1:03d}",
                "split": split,
                "source_id": f"state-bench-trace-{index + 1:03d}",
                "source": source.relative_to(root).as_posix(),
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "projection": path.relative_to(root).as_posix(),
                "projection_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "state_judge": "pass",
                "model_judge": "pass",
                "human_review": "approved",
            }
        )
    manifest = root / "seed-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "v1alpha1",
                "record_type": "creator_seed_manifest",
                "source_version": "state-bench:5644b183",
                "records": records,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return manifest


def test_creator_seed_pack_requires_exactly_nine_triply_approved_creator_traces(
    tmp_path: Path,
) -> None:
    manifest = _write_pack(tmp_path)

    pack = load_creator_seed_pack(manifest)

    assert len(pack.records) == 9
    assert {record.split for record in pack.records} == {"creator"}
    assert all(record.state_judge == "pass" for record in pack.records)
    assert all(record.model_judge == "pass" for record in pack.records)
    assert all(record.human_review == "approved" for record in pack.records)
    assert len(pack.projections) == 9


@pytest.mark.parametrize("count", [8, 10, 15])
def test_creator_seed_pack_rejects_wrong_size(tmp_path: Path, count: int) -> None:
    with pytest.raises(CreatorSeedError, match="exactly 9"):
        load_creator_seed_pack(_write_pack(tmp_path, count=count))


def test_creator_seed_pack_rejects_develop_split_leakage(tmp_path: Path) -> None:
    with pytest.raises(CreatorSeedError, match="creator split"):
        load_creator_seed_pack(_write_pack(tmp_path, split="develop"))


def test_creator_seed_pack_rejects_projection_tampering(tmp_path: Path) -> None:
    manifest = _write_pack(tmp_path)
    (tmp_path / "projections" / "seed-001.json").write_text(
        '{"hidden_gold":"leaked"}', encoding="utf-8"
    )

    with pytest.raises(CreatorSeedError, match="projection hash"):
        load_creator_seed_pack(manifest)
