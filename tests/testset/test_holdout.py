from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest

import ses.testset.holdout as holdout
import ses.testset.split_guard as split_guard
from ses.testset.holdout import (
    FINAL_COUNT,
    SELECTION_COUNT,
    SEMANTIC_GROUP_MAP_PATH,
    STATE_BENCH_ARCHIVE_SHA256,
    HoldoutManifest,
    PrivateFixture,
    ProtectedSemanticGroups,
    SemanticGroupDefinition,
    build_holdout_bundle,
    read_external_semantic_group_map,
    scan_external_holdout_leaks,
    select_holdout_sources,
    validate_holdout_bundle,
    validate_public_holdout_bundle,
)
from ses.testset.sources import STATE_BENCH_COMMIT
from ses.testset.split_guard import (
    DevelopSplitIdentity,
    ExternalHoldoutSplitVerifier,
    SplitIdentityDimension,
    SplitValidationStatus,
)

ROOT = Path(__file__).parents[2]
PROTECTED = ROOT / "data" / "testset" / "protected"
RANKING_KEY = bytes(range(32))


def _empty_semantic_groups() -> ProtectedSemanticGroups:
    return ProtectedSemanticGroups(
        schema_version="v1alpha1",
        record_type="protected_semantic_groups",
        source_commit=STATE_BENCH_COMMIT,
        groups=(),
    )


def _write_semantic_group_map(path: Path) -> None:
    path.write_text(
        json.dumps(_empty_semantic_groups().model_dump(mode="json")),
        encoding="utf-8",
    )
    path.chmod(0o600)


def test_committed_holdout_exposes_only_opaque_locks() -> None:
    public_summary = validate_public_holdout_bundle(PROTECTED)
    assert public_summary.selection_count == SELECTION_COUNT
    assert public_summary.final_count == FINAL_COUNT

    selection = HoldoutManifest.model_validate_json(
        (PROTECTED / "selection-manifest.json").read_text(encoding="utf-8")
    )
    final = HoldoutManifest.model_validate_json(
        (PROTECTED / "final-manifest.json").read_text(encoding="utf-8")
    )
    assert list(selection.slots) == [
        f"slot-{index:03d}" for index in range(1, SELECTION_COUNT + 1)
    ]
    assert list(final.slots) == [
        f"final-slot-{index:03d}" for index in range(1, FINAL_COUNT + 1)
    ]
    assert selection.feedback_policy == "aggregate_gate_only"
    assert final.feedback_policy == "none_until_release"
    assert final.run_policy == "once_after_auto_evolution"


def test_public_holdout_locks_do_not_expose_case_material() -> None:
    public_paths = [
        PROTECTED / "holdout-commitments.json",
        PROTECTED / "selection-manifest.json",
        PROTECTED / "final-manifest.json",
    ]
    payload = "\n".join(path.read_text(encoding="utf-8") for path in public_paths)
    for forbidden in (
        '"records"',
        '"semantic_group_id"',
        '"content_hash"',
        '"public_case"',
        '"user_prompt"',
        '"source_id"',
        '"environment"',
        '"expected_value"',
        '"oracle"',
        '"rubric"',
        '"state_requirements"',
        '"task_requirements"',
        '"user_simulator"',
    ):
        assert forbidden not in payload
    assert "human_reviewed" not in "\n".join(
        path.read_text(encoding="utf-8") for path in public_paths
    )


def test_public_validator_rejects_per_case_fields(tmp_path: Path) -> None:
    bundle = tmp_path / "protected"
    bundle.mkdir()
    for relative in (
        "selection-manifest.json",
        "final-manifest.json",
        "holdout-commitments.json",
    ):
        destination = bundle / relative
        destination.write_bytes((PROTECTED / relative).read_bytes())
    target = bundle / "selection-manifest.json"
    payload = json.loads(target.read_text())
    payload["records"] = []
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        validate_public_holdout_bundle(bundle)


def test_source_selection_requires_a_secret_key_and_is_order_independent() -> None:
    sources = [f"synthetic-independent-{index:03d}" for index in range(25)]
    semantic_groups = _empty_semantic_groups()
    first = select_holdout_sources(
        sources,
        {sources[0]},
        ranking_key=RANKING_KEY,
        semantic_groups=semantic_groups,
    )
    second = select_holdout_sources(
        reversed(sources),
        {sources[0]},
        ranking_key=RANKING_KEY,
        semantic_groups=semantic_groups,
    )

    assert first == second
    assert len(first) == SELECTION_COUNT + FINAL_COUNT
    assert sources[0] not in {item.source_id for item in first}
    assert len({item.semantic_group_id for item in first}) == len(first)

    with pytest.raises(ValueError, match="at least 32 bytes"):
        select_holdout_sources(
            sources,
            set(),
            ranking_key=b"too-short",
            semantic_groups=semantic_groups,
        )

    changed = select_holdout_sources(
        sources,
        {sources[0]},
        ranking_key=bytes(reversed(range(32))),
        semantic_groups=semantic_groups,
    )
    assert [item.source_id for item in changed] != [item.source_id for item in first]


def test_build_cli_requires_an_external_ranking_key_file(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "scripts/build_holdout_assets.py",
            "--archive",
            str(tmp_path / "source.tar.gz"),
            "--creator-seed-manifest",
            str(tmp_path / "creator.json"),
            "--develop-manifest",
            str(tmp_path / "develop.json"),
            "--output",
            str(tmp_path / "bundle"),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "--ranking-key-file" in result.stderr
    assert not (tmp_path / "bundle").exists()


def test_build_cli_requires_an_external_semantic_group_map(tmp_path: Path) -> None:
    ranking_key_path = tmp_path / "ranking.key"
    ranking_key_path.write_bytes(RANKING_KEY)
    ranking_key_path.chmod(0o600)

    result = _run_build_cli(
        tmp_path,
        ranking_key_path,
        include_semantic_group_map=False,
    )

    assert result.returncode == 2
    assert "--semantic-group-map-file" in result.stderr
    assert not (tmp_path / "bundle").exists()


def test_holdout_source_contains_no_embedded_semantic_family_identities() -> None:
    source = (ROOT / "src/ses/testset/holdout.py").read_text(encoding="utf-8")

    assert "_EXPLICIT_SEMANTIC_FAMILIES" not in source


def _run_build_cli(
    tmp_path: Path,
    ranking_key_path: Path,
    *,
    include_semantic_group_map: bool = True,
) -> subprocess.CompletedProcess[str]:
    arguments = [
        sys.executable,
        "scripts/build_holdout_assets.py",
        "--archive",
        str(tmp_path / "missing-source.tar.gz"),
        "--creator-seed-manifest",
        str(tmp_path / "missing-creator.json"),
        "--develop-manifest",
        str(tmp_path / "missing-develop.json"),
        "--output",
        str(tmp_path / "bundle"),
        "--ranking-key-file",
        str(ranking_key_path),
    ]
    if include_semantic_group_map:
        semantic_group_map = tmp_path / "semantic-groups.json"
        _write_semantic_group_map(semantic_group_map)
        arguments.extend(["--semantic-group-map-file", str(semantic_group_map)])
    return subprocess.run(
        arguments,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_build_cli_rejects_a_ranking_key_visible_to_group_or_other(
    tmp_path: Path,
) -> None:
    ranking_key_path = tmp_path / "ranking.key"
    ranking_key_path.write_bytes(RANKING_KEY)
    ranking_key_path.chmod(0o640)

    result = _run_build_cli(tmp_path, ranking_key_path)

    assert result.returncode != 0
    assert "ranking key permissions must deny group/other access" in result.stderr
    assert not (tmp_path / "bundle").exists()


def test_build_cli_rejects_a_ranking_key_beneath_a_symlinked_ancestor(
    tmp_path: Path,
) -> None:
    real_parent = tmp_path / "real-key-parent"
    real_parent.mkdir(mode=0o700)
    ranking_key_path = real_parent / "ranking.key"
    ranking_key_path.write_bytes(RANKING_KEY)
    ranking_key_path.chmod(0o600)
    linked_parent = tmp_path / "linked-key-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    result = _run_build_cli(tmp_path, linked_parent / ranking_key_path.name)

    assert result.returncode != 0
    assert "ranking key path has a symlink ancestor" in result.stderr
    assert not (tmp_path / "bundle").exists()


def test_external_ranking_key_reader_binds_ancestor_before_a_symlink_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted_parent = tmp_path / "trusted-key-switch"
    trusted_parent.mkdir(mode=0o700)
    trusted_key = trusted_parent / "ranking.key"
    trusted_key.write_bytes(RANKING_KEY)
    trusted_key.chmod(0o600)
    attacker_parent = tmp_path / "attacker-key-parent"
    attacker_parent.mkdir(mode=0o700)
    attacker_key = attacker_parent / trusted_key.name
    attacker_key.write_bytes(bytes(reversed(RANKING_KEY)))
    attacker_key.chmod(0o600)
    backup = tmp_path / "trusted-key-switch-original"
    original_os_open = os.open
    original_io_open = io.open
    swapped = False

    def swap_ancestor() -> None:
        nonlocal swapped
        trusted_parent.rename(backup)
        trusted_parent.symlink_to(attacker_parent, target_is_directory=True)
        swapped = True

    def swapping_os_open(
        path: str | bytes | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        path_text = os.fsdecode(path)
        if not swapped and dir_fd is not None and path_text == trusted_parent.name:
            descriptor = original_os_open(path, flags, mode, dir_fd=dir_fd)
            swap_ancestor()
            return descriptor
        return original_os_open(path, flags, mode, dir_fd=dir_fd)

    def swapping_io_open(
        file: int | str | bytes | os.PathLike[str] | os.PathLike[bytes],
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if (
            not swapped
            and not isinstance(file, int)
            and Path(os.fsdecode(file)) == trusted_key
        ):
            swap_ancestor()
        return original_io_open(file, *args, **kwargs)

    monkeypatch.setattr(os, "open", swapping_os_open)
    monkeypatch.setattr(io, "open", swapping_io_open)

    assert holdout.read_external_ranking_key(trusted_key) == RANKING_KEY
    assert swapped is True


def test_external_semantic_group_map_requires_mode_0600(tmp_path: Path) -> None:
    semantic_group_map = tmp_path / "semantic-groups.json"
    _write_semantic_group_map(semantic_group_map)
    semantic_group_map.chmod(0o640)

    with pytest.raises(ValueError, match="permissions must be 0600"):
        read_external_semantic_group_map(semantic_group_map)


def test_external_semantic_group_map_rejects_a_symlinked_ancestor(
    tmp_path: Path,
) -> None:
    real_parent = tmp_path / "real-map-parent"
    real_parent.mkdir(mode=0o700)
    semantic_group_map = real_parent / "semantic-groups.json"
    _write_semantic_group_map(semantic_group_map)
    linked_parent = tmp_path / "linked-map-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(ValueError, match="semantic group map path has a symlink"):
        read_external_semantic_group_map(linked_parent / semantic_group_map.name)


def test_semantic_group_map_excludes_an_entire_used_family() -> None:
    sources = [f"synthetic-independent-{index:03d}" for index in range(25)]
    semantic_groups = ProtectedSemanticGroups(
        schema_version="v1alpha1",
        record_type="protected_semantic_groups",
        source_commit=STATE_BENCH_COMMIT,
        groups=(
            SemanticGroupDefinition(
                name="synthetic-related-family",
                source_ids=(sources[0], sources[1]),
            ),
        ),
    )

    selected = select_holdout_sources(
        sources,
        {sources[0]},
        ranking_key=RANKING_KEY,
        semantic_groups=semantic_groups,
    )

    assert not {sources[0], sources[1]} & {item.source_id for item in selected}


def test_semantic_group_map_rejects_members_outside_the_pinned_pool() -> None:
    sources = [f"synthetic-independent-{index:03d}" for index in range(25)]
    protected_outside_source = "protected-outside-pool"
    semantic_groups = ProtectedSemanticGroups(
        schema_version="v1alpha1",
        record_type="protected_semantic_groups",
        source_commit=STATE_BENCH_COMMIT,
        groups=(
            SemanticGroupDefinition(
                name="synthetic-invalid-family",
                source_ids=(sources[0], protected_outside_source),
            ),
        ),
    )

    with pytest.raises(ValueError) as captured:
        select_holdout_sources(
            sources,
            set(),
            ranking_key=RANKING_KEY,
            semantic_groups=semantic_groups,
        )

    assert str(captured.value) == (
        "semantic group map contains a source outside the pinned pool"
    )
    assert protected_outside_source not in str(captured.value)


def _add_json(archive: tarfile.TarFile, path: str, value: object) -> None:
    payload = json.dumps(value, sort_keys=True).encode("utf-8")
    member = tarfile.TarInfo(path)
    member.size = len(payload)
    member.mtime = 0
    archive.addfile(member, io.BytesIO(payload))


def _synthetic_archive(path: Path, count: int = 21) -> tuple[str, list[str]]:
    root = f"STATE-Bench-{STATE_BENCH_COMMIT}"
    source_ids = [f"synthetic-holdout-{index:03d}" for index in range(count)]
    with tarfile.open(path, mode="w:gz") as archive:
        for index, source_id in enumerate(source_ids):
            task_path = (
                f"{root}/state_bench/domains/customer_support/tasks/{source_id}.json"
            )
            fixture_path = (
                f"{root}/state_bench/domains/customer_support/task_envs/"
                f"{source_id}.json"
            )
            _add_json(
                archive,
                task_path,
                {
                    "task_id": source_id,
                    "task_type": "return_item",
                    "task_env_path": (
                        "state_bench/domains/customer_support/task_envs/"
                        f"{source_id}.json"
                    ),
                    "opening_message": (
                        f'Synthetic public request "{index}".\nContinue safely.'
                    ),
                    "user_id": f"customer-{index}",
                    "now": "2026-08-18T12:00:00",
                    "user_simulator": {"task_rules": ["Stay in scope."]},
                    "state_requirements": [
                        {
                            "entity_type": "orders",
                            "record_key": f"order-{index}",
                            "field": "status",
                            "expected_value": "returned",
                        }
                    ],
                    "task_requirements": [
                        {
                            "id": f"requirement-{index}",
                            "kind": "must",
                            "requirement": "Complete the requested return.",
                            "evidence": "tool_calls",
                        }
                    ],
                },
            )
            _add_json(
                archive,
                fixture_path,
                {"orders": [{"order_id": f"order-{index}", "status": "delivered"}]},
            )
    return hashlib.sha256(path.read_bytes()).hexdigest(), source_ids


def _synthetic_build_inputs(tmp_path: Path) -> tuple[Path, str, Path, Path, int]:
    archive = tmp_path / "source.tar.gz"
    archive_sha256, source_ids = _synthetic_archive(archive)
    creator_seed = tmp_path / "creator-seed.json"
    creator_seed.write_text(
        json.dumps(
            {"records": [{"seed_id": "creator-case", "source_id": source_ids[0]}]}
        ),
        encoding="utf-8",
    )
    develop = tmp_path / "develop.json"
    develop.write_text(json.dumps({"cases": []}), encoding="utf-8")
    return archive, archive_sha256, creator_seed, develop, len(source_ids)


def _synthetic_holdout_bundle(tmp_path: Path) -> tuple[Path, Path]:
    archive, archive_sha256, creator_seed, develop, source_count = (
        _synthetic_build_inputs(tmp_path)
    )
    bundle = tmp_path / "bundle"
    build_holdout_bundle(
        archive_path=archive,
        creator_seed_manifest=creator_seed,
        develop_manifest=develop,
        output_root=bundle,
        ranking_key=RANKING_KEY,
        semantic_groups=_empty_semantic_groups(),
        expected_archive_sha256=archive_sha256,
        expected_task_count=source_count,
        expected_return_count=source_count,
    )
    inventory_path = bundle / "private/holdout-inventory.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    inventory["upstream_archive_sha256"] = STATE_BENCH_ARCHIVE_SHA256
    inventory_path.write_text(
        json.dumps(inventory, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    inventory_sha256 = hashlib.sha256(inventory_path.read_bytes()).hexdigest()
    manifest_sha256: dict[str, str] = {}
    for split in ("selection", "final"):
        manifest_path = bundle / f"{split}-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["upstream_archive_sha256"] = STATE_BENCH_ARCHIVE_SHA256
        manifest["inventory_commitment_sha256"] = inventory_sha256
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        manifest_sha256[split] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    commitments_path = bundle / "holdout-commitments.json"
    commitments = json.loads(commitments_path.read_text(encoding="utf-8"))
    commitments["selection_manifest_sha256"] = manifest_sha256["selection"]
    commitments["final_manifest_sha256"] = manifest_sha256["final"]
    commitments_path.write_text(
        json.dumps(commitments, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    public_locks = tmp_path / "public-locks"
    public_locks.mkdir()
    for name in (
        "selection-manifest.json",
        "final-manifest.json",
        "holdout-commitments.json",
    ):
        (public_locks / name).write_bytes((bundle / name).read_bytes())
    return bundle, public_locks


@pytest.mark.parametrize(
    ("field", "dimension"),
    [
        ("source_id", SplitIdentityDimension.SOURCE_ID),
        ("semantic_group_id", SplitIdentityDimension.SEMANTIC_GROUP_ID),
        ("case_id", SplitIdentityDimension.CASE_ID),
        ("content_hash", SplitIdentityDimension.CONTENT_HASH),
    ],
)
def test_external_split_verifier_checks_committed_inventory_without_exposing_it(
    tmp_path: Path, field: str, dimension: SplitIdentityDimension
) -> None:
    bundle, public_locks = _synthetic_holdout_bundle(tmp_path)
    inventory = json.loads(
        (bundle / "private/holdout-inventory.json").read_text(encoding="utf-8")
    )
    protected = inventory["records"][0]
    values = {
        "source_id": "develop-source",
        "semantic_group_id": "develop-semantic",
        "case_id": "develop-case",
        "content_hash": "f" * 64,
    }
    values[field] = protected[field]

    verifier = ExternalHoldoutSplitVerifier.from_bundle(
        bundle_root=bundle,
        public_lock_root=public_locks,
    )

    assert (
        verifier.status is SplitValidationStatus.EXTERNAL_INVENTORY_COMMITMENT_VERIFIED
    )
    assert (
        verifier.provenance_sha256
        == hashlib.sha256(
            (bundle / "private/holdout-inventory.json").read_bytes()
        ).hexdigest()
    )
    assert verifier.conflict_dimension(DevelopSplitIdentity(**values)) is dimension
    assert protected["source_id"] not in repr(verifier)


def test_external_holdout_leak_scan_returns_only_matching_paths(
    tmp_path: Path,
) -> None:
    bundle, public_locks = _synthetic_holdout_bundle(tmp_path)
    inventory = json.loads(
        (bundle / "private/holdout-inventory.json").read_text(encoding="utf-8")
    )
    protected = inventory["records"][0]
    public_case = json.loads(
        (bundle / protected["public_case"]["path"]).read_text(encoding="utf-8")
    )
    candidates = {
        "docs/source.md": f"prefix {protected['source_id']} suffix",
        "docs/prompt.md": json.dumps(
            {"user_prompt": public_case["user_prompt"]},
            ensure_ascii=False,
        ),
        "docs/hash.md": protected["content_hash"],
        "docs/safe.md": "No protected material here.",
    }

    result = scan_external_holdout_leaks(
        bundle_root=bundle,
        public_lock_root=public_locks,
        candidate_documents=candidates,
    )

    assert result.status == "external_holdout_snapshot_verified"
    assert result.matched_relative_paths == (
        "docs/hash.md",
        "docs/prompt.md",
        "docs/source.md",
    )
    assert protected["source_id"] not in repr(result)
    assert public_case["user_prompt"] not in repr(result)
    assert protected["content_hash"] not in repr(result)


def test_external_split_verifier_rejects_a_symlinked_root_ancestor(
    tmp_path: Path,
) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    bundle, public_locks = _synthetic_holdout_bundle(real_parent)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink ancestor"):
        ExternalHoldoutSplitVerifier.from_bundle(
            bundle_root=linked_parent / bundle.name,
            public_lock_root=public_locks,
        )


def test_regular_file_reader_binds_each_ancestor_before_a_symlink_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    trusted_parent = tmp_path / "trusted-switch"
    trusted_root = trusted_parent / "root"
    trusted_root.mkdir(parents=True)
    (trusted_root / "lock.json").write_bytes(b"trusted")
    attacker_parent = tmp_path / "attacker"
    attacker_root = attacker_parent / "root"
    attacker_root.mkdir(parents=True)
    (attacker_root / "lock.json").write_bytes(b"attacker")
    backup = tmp_path / "trusted-switch-original"
    original_open = os.open
    swapped = False

    def swapping_open(
        path: str | bytes | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        path_text = os.fsdecode(path)
        if (
            not swapped
            and dir_fd is None
            and Path(path_text) == trusted_root / "lock.json"
        ):
            trusted_parent.rename(backup)
            trusted_parent.symlink_to(attacker_parent, target_is_directory=True)
            swapped = True
        elif not swapped and dir_fd is not None and path_text == trusted_parent.name:
            descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
            trusted_parent.rename(backup)
            trusted_parent.symlink_to(attacker_parent, target_is_directory=True)
            swapped = True
            return descriptor
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swapping_open)

    assert split_guard._regular_file_bytes(trusted_root, "lock.json") == b"trusted"
    assert swapped is True


def test_external_split_verifier_snapshots_each_public_lock_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, public_locks = _synthetic_holdout_bundle(tmp_path)
    original = split_guard._regular_file_bytes
    reads: dict[str, int] = {}

    def read_once(root: Path, relative: str) -> bytes:
        if root == public_locks:
            reads[relative] = reads.get(relative, 0) + 1
            if reads[relative] > 1:
                raise AssertionError("public lock was re-read after its snapshot")
        return original(root, relative)

    monkeypatch.setattr(split_guard, "_regular_file_bytes", read_once)

    ExternalHoldoutSplitVerifier.from_bundle(
        bundle_root=bundle,
        public_lock_root=public_locks,
    )

    assert reads == {
        "selection-manifest.json": 1,
        "final-manifest.json": 1,
        "holdout-commitments.json": 1,
    }


@pytest.mark.parametrize("target", ["opaque-lock", "inventory-commitment"])
def test_external_split_verifier_rejects_uncommitted_identity_inputs(
    tmp_path: Path, target: str
) -> None:
    bundle, public_locks = _synthetic_holdout_bundle(tmp_path)
    if target == "opaque-lock":
        path = bundle / "selection-manifest.json"
        path.write_bytes(path.read_bytes() + b" ")
        message = "locks differ"
    else:
        path = bundle / "private/holdout-inventory.json"
        path.write_bytes(path.read_bytes() + b" ")
        message = "inventory differs"

    with pytest.raises(ValueError, match=message):
        ExternalHoldoutSplitVerifier.from_bundle(
            bundle_root=bundle,
            public_lock_root=public_locks,
        )


def test_full_validator_requires_private_modes_for_the_entire_bundle(
    tmp_path: Path,
) -> None:
    bundle, _ = _synthetic_holdout_bundle(tmp_path)
    creator_protected = tmp_path / "creator-protected.json"
    creator_protected.write_text(
        json.dumps(
            {
                "locked": True,
                "records": [
                    {
                        "case_id": "creator-case",
                        "semantic_group_id": "creator-semantic",
                        "content_hash": "a" * 64,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    candidate_seeds = tmp_path / "candidate-seeds.jsonl"
    candidate_seeds.write_text("", encoding="utf-8")
    cases = (
        (bundle, 0o750, 0o700, "bundle root permissions must be 0700"),
        (
            bundle / "private" / "selection",
            0o750,
            0o700,
            "bundle directory permissions must be 0700",
        ),
        (
            bundle / "selection-manifest.json",
            0o640,
            0o600,
            "bundle file permissions must be 0600",
        ),
    )
    for path, unsafe_mode, restored_mode, message in cases:
        path.chmod(unsafe_mode)
        with pytest.raises(ValueError, match=message):
            validate_holdout_bundle(
                bundle_root=bundle,
                creator_protected_manifest=creator_protected,
                creator_seed_manifest=tmp_path / "creator-seed.json",
                develop_manifest=tmp_path / "develop.json",
                candidate_seeds=candidate_seeds,
            )
        path.chmod(restored_mode)


def test_full_validator_rejects_a_wide_permission_root_swapped_after_mode_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, _ = _synthetic_holdout_bundle(tmp_path)
    creator_protected = tmp_path / "creator-protected.json"
    creator_protected.write_text(
        json.dumps(
            {
                "locked": True,
                "records": [
                    {
                        "case_id": "creator-case",
                        "semantic_group_id": "creator-semantic",
                        "content_hash": "a" * 64,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    candidate_seeds = tmp_path / "candidate-seeds.jsonl"
    candidate_seeds.write_text("", encoding="utf-8")
    replacement = tmp_path / "replacement-bundle"
    shutil.copytree(bundle, replacement)
    replacement.chmod(0o755)
    (replacement / "selection-manifest.json").chmod(0o644)
    original_bundle = tmp_path / "original-bundle"
    original_validate = HoldoutManifest.model_validate_json
    swapped = False

    def swap_before_first_lock_parse(
        data: str | bytes, **kwargs: Any
    ) -> HoldoutManifest:
        nonlocal swapped
        if not swapped:
            bundle.rename(original_bundle)
            replacement.rename(bundle)
            swapped = True
        return original_validate(data, **kwargs)

    monkeypatch.setattr(
        HoldoutManifest,
        "model_validate_json",
        swap_before_first_lock_parse,
    )

    with pytest.raises(ValueError, match="validation root path changed"):
        validate_holdout_bundle(
            bundle_root=bundle,
            creator_protected_manifest=creator_protected,
            creator_seed_manifest=tmp_path / "creator-seed.json",
            develop_manifest=tmp_path / "develop.json",
            candidate_seeds=candidate_seeds,
        )

    assert swapped is True


def test_full_validator_rejects_a_pointer_parent_swapped_after_tree_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle, _ = _synthetic_holdout_bundle(tmp_path)
    creator_protected = tmp_path / "creator-protected.json"
    creator_protected.write_text(
        json.dumps(
            {
                "locked": True,
                "records": [
                    {
                        "case_id": "creator-case",
                        "semantic_group_id": "creator-semantic",
                        "content_hash": "a" * 64,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    candidate_seeds = tmp_path / "candidate-seeds.jsonl"
    candidate_seeds.write_text("", encoding="utf-8")
    pointer_parent = bundle / "private" / "selection" / "slot-001"
    replacement = tmp_path / "replacement-pointer-parent"
    shutil.copytree(pointer_parent, replacement)
    replacement.chmod(0o755)
    for path in replacement.iterdir():
        path.chmod(0o644)
    (replacement / "oracle.json").write_text("{}", encoding="utf-8")
    original_pointer_parent = tmp_path / "original-pointer-parent"
    original_validate = PrivateFixture.model_validate_json
    swapped = False

    def swap_before_first_fixture_parse(
        data: str | bytes, **kwargs: Any
    ) -> PrivateFixture:
        nonlocal swapped
        if not swapped:
            pointer_parent.rename(original_pointer_parent)
            replacement.rename(pointer_parent)
            swapped = True
        return original_validate(data, **kwargs)

    monkeypatch.setattr(
        PrivateFixture,
        "model_validate_json",
        swap_before_first_fixture_parse,
    )

    with pytest.raises(ValueError, match="validation directory tree changed"):
        validate_holdout_bundle(
            bundle_root=bundle,
            creator_protected_manifest=creator_protected,
            creator_seed_manifest=tmp_path / "creator-seed.json",
            develop_manifest=tmp_path / "develop.json",
            candidate_seeds=candidate_seeds,
        )

    assert swapped is True


@pytest.mark.parametrize("attack", ["root-symlink", "ancestor-symlink", "parent"])
def test_builder_rejects_unsafe_output_roots(tmp_path: Path, attack: str) -> None:
    archive, archive_sha256, creator_seed, develop, source_count = (
        _synthetic_build_inputs(tmp_path)
    )
    if attack == "root-symlink":
        real_output = tmp_path / "real-output"
        real_output.mkdir()
        output = tmp_path / "bundle"
        output.symlink_to(real_output, target_is_directory=True)
        message = "symlink"
    elif attack == "ancestor-symlink":
        real_parent = tmp_path / "real-parent"
        real_parent.mkdir()
        linked_parent = tmp_path / "linked-parent"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        output = linked_parent / "bundle"
        message = "symlink"
    else:
        output = tmp_path / "container" / ".." / "escaped"
        message = "parent traversal"

    with pytest.raises(ValueError, match=message):
        build_holdout_bundle(
            archive_path=archive,
            creator_seed_manifest=creator_seed,
            develop_manifest=develop,
            output_root=output,
            ranking_key=RANKING_KEY,
            semantic_groups=_empty_semantic_groups(),
            expected_archive_sha256=archive_sha256,
            expected_task_count=source_count,
            expected_return_count=source_count,
        )


def test_builder_keeps_all_writes_on_one_root_descriptor_during_ancestor_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive, archive_sha256, creator_seed, develop, source_count = (
        _synthetic_build_inputs(tmp_path)
    )
    trusted_parent = tmp_path / "trusted-output-switch"
    trusted_parent.mkdir(mode=0o700)
    output = trusted_parent / "bundle"
    attacker_parent = tmp_path / "attacker-output-parent"
    attacker_bundle = attacker_parent / output.name
    (attacker_bundle / "private" / "selection" / "slot-001").mkdir(
        parents=True,
        mode=0o700,
    )
    backup = tmp_path / "trusted-output-switch-original"
    original_open = os.open
    swapped = False

    def swap_ancestor() -> None:
        nonlocal swapped
        trusted_parent.rename(backup)
        trusted_parent.symlink_to(attacker_parent, target_is_directory=True)
        swapped = True

    def swapping_open(
        path: str | bytes | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        path_text = os.fsdecode(path)
        path_value = Path(path_text)
        if (
            not swapped
            and dir_fd is None
            and path_value.is_absolute()
            and output in path_value.parents
        ):
            swap_ancestor()
        elif not swapped and dir_fd is not None and path_text == trusted_parent.name:
            descriptor = original_open(path, flags, mode, dir_fd=dir_fd)
            swap_ancestor()
            return descriptor
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", swapping_open)

    try:
        build_holdout_bundle(
            archive_path=archive,
            creator_seed_manifest=creator_seed,
            develop_manifest=develop,
            output_root=output,
            ranking_key=RANKING_KEY,
            semantic_groups=_empty_semantic_groups(),
            expected_archive_sha256=archive_sha256,
            expected_task_count=source_count,
            expected_return_count=source_count,
        )
    except ValueError:
        pass

    assert swapped is True
    assert not any(path.is_file() for path in attacker_bundle.rglob("*"))


def test_synthetic_builder_and_validator_detect_private_tail_tampering(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "source.tar.gz"
    archive_sha256, source_ids = _synthetic_archive(archive)
    creator_seed = tmp_path / "creator-seed.json"
    creator_seed.write_text(
        json.dumps(
            {"records": [{"seed_id": "creator-case", "source_id": source_ids[0]}]}
        ),
        encoding="utf-8",
    )
    creator_protected = tmp_path / "creator-protected.json"
    creator_protected.write_text(
        json.dumps(
            {
                "locked": True,
                "records": [
                    {
                        "case_id": "creator-case",
                        "semantic_group_id": "creator-semantic",
                        "content_hash": "a" * 64,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    develop = tmp_path / "develop.json"
    develop.write_text(json.dumps({"cases": []}), encoding="utf-8")
    candidate_seeds = tmp_path / "candidate-seeds.jsonl"
    candidate_seeds.write_text("", encoding="utf-8")
    bundle = tmp_path / "bundle"

    build_holdout_bundle(
        archive_path=archive,
        creator_seed_manifest=creator_seed,
        develop_manifest=develop,
        output_root=bundle,
        ranking_key=RANKING_KEY,
        semantic_groups=_empty_semantic_groups(),
        expected_archive_sha256=archive_sha256,
        expected_task_count=len(source_ids),
        expected_return_count=len(source_ids),
    )
    assert stat.S_IMODE(bundle.stat().st_mode) == 0o700
    for path in bundle.rglob("*"):
        expected_mode = 0o700 if path.is_dir() else 0o600
        assert stat.S_IMODE(path.stat().st_mode) == expected_mode
    ranking_key_path = bundle / "private/holdout-ranking.key"
    assert ranking_key_path.read_bytes() == RANKING_KEY
    assert stat.S_IMODE(ranking_key_path.stat().st_mode) == 0o600
    semantic_group_map_path = bundle / SEMANTIC_GROUP_MAP_PATH
    assert stat.S_IMODE(semantic_group_map_path.stat().st_mode) == 0o600
    semantic_group_map = read_external_semantic_group_map(semantic_group_map_path)
    inventory = json.loads(
        (bundle / "private/holdout-inventory.json").read_text(encoding="utf-8")
    )
    assert inventory["selection_algorithm"]["semantic_group_map"] == {
        "path": SEMANTIC_GROUP_MAP_PATH,
        "sha256": hashlib.sha256(semantic_group_map_path.read_bytes()).hexdigest(),
    }
    assert semantic_group_map == _empty_semantic_groups()
    for split, count, prefix in (
        ("selection", SELECTION_COUNT, "slot"),
        ("final", FINAL_COUNT, "final-slot"),
    ):
        lock = json.loads((bundle / f"{split}-manifest.json").read_text())
        assert "records" not in lock
        assert "construction" not in lock
        assert lock["case_count"] == count
        assert lock["slots"] == [
            f"{prefix}-{index:03d}" for index in range(1, count + 1)
        ]
    assert len(tuple((bundle / "public/selection").glob("*.json"))) == 6
    assert len(tuple((bundle / "public/final").glob("*.json"))) == 12
    ranking_key_path.chmod(0o644)
    with pytest.raises(ValueError, match="ranking key permissions"):
        validate_holdout_bundle(
            bundle_root=bundle,
            creator_protected_manifest=creator_protected,
            creator_seed_manifest=creator_seed,
            develop_manifest=develop,
            candidate_seeds=candidate_seeds,
            expected_archive_sha256=archive_sha256,
        )
    ranking_key_path.chmod(0o600)
    validate_holdout_bundle(
        bundle_root=bundle,
        creator_protected_manifest=creator_protected,
        creator_seed_manifest=creator_seed,
        develop_manifest=develop,
        candidate_seeds=candidate_seeds,
        archive_path=archive,
        expected_archive_sha256=archive_sha256,
        expected_task_count=len(source_ids),
        expected_return_count=len(source_ids),
    )

    semantic_group_map_path = bundle / SEMANTIC_GROUP_MAP_PATH
    semantic_group_map_bytes = semantic_group_map_path.read_bytes()
    semantic_group_map_path.write_bytes(semantic_group_map_bytes + b" ")
    with pytest.raises(ValueError, match="checksum mismatch"):
        validate_holdout_bundle(
            bundle_root=bundle,
            creator_protected_manifest=creator_protected,
            creator_seed_manifest=creator_seed,
            develop_manifest=develop,
            candidate_seeds=candidate_seeds,
            expected_archive_sha256=archive_sha256,
        )
    semantic_group_map_path.write_bytes(semantic_group_map_bytes)

    private_fixture = next((bundle / "private" / "final").rglob("fixture.json"))
    private_fixture.write_bytes(private_fixture.read_bytes() + b" ")
    with pytest.raises(ValueError, match="checksum mismatch"):
        validate_holdout_bundle(
            bundle_root=bundle,
            creator_protected_manifest=creator_protected,
            creator_seed_manifest=creator_seed,
            develop_manifest=develop,
            candidate_seeds=candidate_seeds,
            expected_archive_sha256=archive_sha256,
        )


@pytest.mark.parametrize(
    ("conflict", "dimension"),
    [("source_id", "source_ids"), ("content_hash", "content_hashes")],
)
def test_full_validator_does_not_echo_protected_identity_on_split_conflict(
    tmp_path: Path,
    conflict: str,
    dimension: str,
) -> None:
    archive, archive_sha256, original_creator_seed, develop, source_count = (
        _synthetic_build_inputs(tmp_path)
    )
    bundle = tmp_path / "bundle"
    build_holdout_bundle(
        archive_path=archive,
        creator_seed_manifest=original_creator_seed,
        develop_manifest=develop,
        output_root=bundle,
        ranking_key=RANKING_KEY,
        semantic_groups=_empty_semantic_groups(),
        expected_archive_sha256=archive_sha256,
        expected_task_count=source_count,
        expected_return_count=source_count,
    )
    inventory = json.loads(
        (bundle / "private/holdout-inventory.json").read_text(encoding="utf-8")
    )
    protected_record = inventory["records"][0]
    original_creator = json.loads(original_creator_seed.read_text(encoding="utf-8"))
    creator_source_id = original_creator["records"][0]["source_id"]
    protected_value = protected_record[conflict]

    creator_seed = tmp_path / "conflicting-creator-seed.json"
    creator_seed.write_text(
        json.dumps(
            {
                "records": [
                    {
                        "seed_id": "creator-conflict",
                        "source_id": (
                            protected_value
                            if conflict == "source_id"
                            else creator_source_id
                        ),
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    creator_protected = tmp_path / "creator-protected.json"
    creator_protected.write_text(
        json.dumps(
            {
                "locked": True,
                "records": [
                    {
                        "case_id": "creator-conflict",
                        "semantic_group_id": "creator-semantic",
                        "content_hash": (
                            protected_value if conflict == "content_hash" else "a" * 64
                        ),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    candidate_seeds = tmp_path / "candidate-seeds.jsonl"
    candidate_seeds.write_text("", encoding="utf-8")

    with pytest.raises(ValueError) as captured:
        validate_holdout_bundle(
            bundle_root=bundle,
            creator_protected_manifest=creator_protected,
            creator_seed_manifest=creator_seed,
            develop_manifest=develop,
            candidate_seeds=candidate_seeds,
            expected_archive_sha256=archive_sha256,
        )

    assert str(protected_value) not in str(captured.value)
    assert str(captured.value) == (
        f"split identity overlap: creator/selection: {dimension}"
    )


def test_full_validator_does_not_echo_missing_protected_archive_source(
    tmp_path: Path,
) -> None:
    archive, archive_sha256, creator_seed, develop, source_count = (
        _synthetic_build_inputs(tmp_path)
    )
    bundle = tmp_path / "bundle"
    build_holdout_bundle(
        archive_path=archive,
        creator_seed_manifest=creator_seed,
        develop_manifest=develop,
        output_root=bundle,
        ranking_key=RANKING_KEY,
        semantic_groups=_empty_semantic_groups(),
        expected_archive_sha256=archive_sha256,
        expected_task_count=source_count,
        expected_return_count=source_count,
    )
    inventory_path = bundle / "private/holdout-inventory.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    protected_source_id = inventory["records"][0]["source_id"]

    missing_source_archive = tmp_path / "source-with-missing-protected-task.tar.gz"
    with (
        tarfile.open(archive, mode="r:gz") as source_archive,
        tarfile.open(missing_source_archive, mode="w:gz") as target_archive,
    ):
        for member in source_archive.getmembers():
            if Path(member.name).stem == protected_source_id:
                continue
            stream = source_archive.extractfile(member)
            target_archive.addfile(member, stream)
    missing_archive_sha256 = hashlib.sha256(
        missing_source_archive.read_bytes()
    ).hexdigest()

    inventory["upstream_archive_sha256"] = missing_archive_sha256
    inventory_path.write_text(
        json.dumps(inventory, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    inventory_sha256 = hashlib.sha256(inventory_path.read_bytes()).hexdigest()
    manifest_hashes: dict[str, str] = {}
    for split in ("selection", "final"):
        manifest_path = bundle / f"{split}-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["upstream_archive_sha256"] = missing_archive_sha256
        manifest["inventory_commitment_sha256"] = inventory_sha256
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        manifest_hashes[split] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    commitments_path = bundle / "holdout-commitments.json"
    commitments = json.loads(commitments_path.read_text(encoding="utf-8"))
    commitments["selection_manifest_sha256"] = manifest_hashes["selection"]
    commitments["final_manifest_sha256"] = manifest_hashes["final"]
    commitments_path.write_text(
        json.dumps(commitments, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    creator_protected = tmp_path / "creator-protected.json"
    creator_protected.write_text(
        json.dumps(
            {
                "locked": True,
                "records": [
                    {
                        "case_id": "creator-case",
                        "semantic_group_id": "creator-semantic",
                        "content_hash": "a" * 64,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    candidate_seeds = tmp_path / "candidate-seeds.jsonl"
    candidate_seeds.write_text("", encoding="utf-8")

    with pytest.raises(ValueError) as captured:
        validate_holdout_bundle(
            bundle_root=bundle,
            creator_protected_manifest=creator_protected,
            creator_seed_manifest=creator_seed,
            develop_manifest=develop,
            candidate_seeds=candidate_seeds,
            archive_path=missing_source_archive,
            expected_archive_sha256=missing_archive_sha256,
            expected_task_count=source_count - 1,
            expected_return_count=source_count - 1,
        )

    assert protected_source_id not in str(captured.value)
    assert str(captured.value) == "holdout source is absent from pinned archive"
