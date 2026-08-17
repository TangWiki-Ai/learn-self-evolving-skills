from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from ses.skills.seeds import CreatorSeedError, load_creator_seed_pack

ROOT = Path(__file__).parents[2]
SOURCE_PACK = ROOT / "data" / "skill-v0" / "creator"


def _copy_pack(tmp_path: Path) -> Path:
    destination = tmp_path / "creator"
    shutil.copytree(SOURCE_PACK, destination)
    return destination / "seed-manifest.json"


def _read(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _write(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _record(manifest: dict[str, object], index: int = 0) -> dict[str, object]:
    records = manifest["records"]
    assert isinstance(records, list)
    value = records[index]
    assert isinstance(value, dict)
    return value


def _update_review_binding(
    root: Path,
    manifest: dict[str, object],
    *,
    field: str,
    digest: str,
) -> None:
    record = _record(manifest)
    review_ref = record["human_review"]
    assert isinstance(review_ref, dict)
    review_path = root / str(review_ref["path"])
    review = _read(review_path)
    review[field] = digest
    _write(review_path, review)
    review_ref["sha256"] = _sha(review_path)


def test_creator_seed_pack_requires_exactly_nine_fully_audited_creator_traces(
    tmp_path: Path,
) -> None:
    pack = load_creator_seed_pack(_copy_pack(tmp_path))

    assert len(pack.records) == 9
    assert {record.split for record in pack.records} == {"creator"}
    assert len(pack.projections) == 9
    assert pack.manifest.source_version.endswith(":creator-audit-v3")


@pytest.mark.parametrize("count", [8, 10, 15])
def test_creator_seed_pack_rejects_wrong_size(tmp_path: Path, count: int) -> None:
    manifest_path = _copy_pack(tmp_path)
    manifest = _read(manifest_path)
    records = manifest["records"]
    assert isinstance(records, list)
    if count <= 9:
        manifest["records"] = records[:count]
    else:
        manifest["records"] = records + [dict(records[-1]) for _ in range(count - 9)]
    _write(manifest_path, manifest)

    with pytest.raises(CreatorSeedError, match="exactly 9"):
        load_creator_seed_pack(manifest_path)


def test_creator_seed_pack_rejects_develop_split_leakage(tmp_path: Path) -> None:
    manifest_path = _copy_pack(tmp_path)
    manifest = _read(manifest_path)
    _record(manifest)["split"] = "develop"
    _write(manifest_path, manifest)

    with pytest.raises(CreatorSeedError, match="creator split"):
        load_creator_seed_pack(manifest_path)


def test_creator_seed_pack_rejects_projection_tampering(tmp_path: Path) -> None:
    manifest_path = _copy_pack(tmp_path)
    root = manifest_path.parent
    projection = root / "projections" / "seed-001.json"
    projection.write_text('{"hidden_gold":"leaked"}', encoding="utf-8")

    with pytest.raises(CreatorSeedError, match="artifact hash"):
        load_creator_seed_pack(manifest_path)


def test_creator_seed_pack_rejects_hash_valid_noncanonical_trace(
    tmp_path: Path,
) -> None:
    manifest_path = _copy_pack(tmp_path)
    root = manifest_path.parent
    source = root / "private" / "traces" / "trace-001.json"
    source.write_text('{"outcome":"success"}', encoding="utf-8")
    manifest = _read(manifest_path)
    trace_ref = _record(manifest)["trace"]
    assert isinstance(trace_ref, dict)
    trace_ref["sha256"] = _sha(source)
    _write(manifest_path, manifest)

    with pytest.raises(CreatorSeedError, match="canonical Trace"):
        load_creator_seed_pack(manifest_path)


@pytest.mark.parametrize(
    "leak",
    [
        {"hidden_gold": "approve"},
        {"scenario": "selection-case-009"},
        {"scenario": "use api_key credential-value"},
    ],
)
def test_creator_seed_pack_rejects_hash_valid_projection_leakage(
    tmp_path: Path, leak: dict[str, str]
) -> None:
    manifest_path = _copy_pack(tmp_path)
    root = manifest_path.parent
    projection = root / "projections" / "seed-001.json"
    value = _read(projection)
    value.update(leak)
    _write(projection, value)
    manifest = _read(manifest_path)
    projection_ref = _record(manifest)["projection"]
    assert isinstance(projection_ref, dict)
    projection_ref["sha256"] = _sha(projection)
    _update_review_binding(
        root,
        manifest,
        field="reviewed_projection_sha256",
        digest=_sha(projection),
    )
    _write(manifest_path, manifest)

    with pytest.raises(CreatorSeedError, match="safe schema"):
        load_creator_seed_pack(manifest_path)


def test_creator_seed_pack_rejects_review_of_different_evidence(
    tmp_path: Path,
) -> None:
    manifest_path = _copy_pack(tmp_path)
    root = manifest_path.parent
    manifest = _read(manifest_path)
    _update_review_binding(
        root,
        manifest,
        field="reviewed_trace_sha256",
        digest="0" * 64,
    )
    _write(manifest_path, manifest)

    with pytest.raises(CreatorSeedError, match="exact evidence"):
        load_creator_seed_pack(manifest_path)


def test_creator_seed_pack_rejects_state_grade_with_unrelated_evidence(
    tmp_path: Path,
) -> None:
    manifest_path = _copy_pack(tmp_path)
    root = manifest_path.parent
    grade = root / "private" / "judges" / "state" / "state-grade-001.json"
    value = _read(grade)
    assertions = value["assertions"]
    assert isinstance(assertions, list)
    assertion = assertions[0]
    assert isinstance(assertion, dict)
    evidence = assertion["evidence"]
    assert isinstance(evidence, list)
    evidence_row = evidence[0]
    assert isinstance(evidence_row, dict)
    manifest = _read(manifest_path)
    evidence_row["artifact"] = _record(manifest)["trace"]
    _write(grade, value)
    grade_ref = _record(manifest)["state_grade"]
    assert isinstance(grade_ref, dict)
    grade_ref["sha256"] = _sha(grade)
    _write(manifest_path, manifest)

    with pytest.raises(CreatorSeedError, match="grade evidence"):
        load_creator_seed_pack(manifest_path)


def test_creator_seed_pack_rejects_model_run_hash_tampering(tmp_path: Path) -> None:
    manifest_path = _copy_pack(tmp_path)
    root = manifest_path.parent
    run = root / "private" / "judges" / "model" / "judge-runs" / "run-001.json"
    run.write_text(run.read_text(encoding="utf-8") + " ", encoding="utf-8")

    with pytest.raises(CreatorSeedError, match="artifact hash"):
        load_creator_seed_pack(manifest_path)


def test_creator_seed_pack_rejects_replay_source_mismatch(tmp_path: Path) -> None:
    manifest_path = _copy_pack(tmp_path)
    root = manifest_path.parent
    replay = root / "private" / "replays" / "replay-001.json"
    value = _read(replay)
    value["seed_id"] = "creator-seed-999"
    _write(replay, value)
    manifest = _read(manifest_path)
    replay_ref = _record(manifest)["replay"]
    assert isinstance(replay_ref, dict)
    replay_ref["sha256"] = _sha(replay)
    _write(manifest_path, manifest)

    with pytest.raises(CreatorSeedError, match="pinned source"):
        load_creator_seed_pack(manifest_path)
