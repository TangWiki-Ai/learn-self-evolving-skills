"""Build and validate locked STATE-Bench selection and final holdouts."""

from __future__ import annotations

import hashlib
import hmac
import json
import tarfile
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal, cast

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    field_validator,
    model_validator,
)

from ses.contracts import CaseDefinition, CaseSplit, RecordType, SchemaVersion
from ses.testset.secure_files import (
    SecureDirectorySnapshot,
    SecureDirectoryWriter,
    read_regular_file_snapshot,
)
from ses.testset.sources import STATE_BENCH_COMMIT

STATE_BENCH_ARCHIVE_SHA256 = (
    "746646f24ab0ebd713ae28f0e96c1cc81cdbe9598171a2f7ed37953ce7a0b96a"
)
HOLDOUT_BUILD_VERSION = "ses-state-bench-holdout-v3"
HOLDOUT_RANKING_KEY_PATH = "private/holdout-ranking.key"
SEMANTIC_GROUP_MAP_PATH = "private/semantic-groups.json"
SELECTION_COUNT: Literal[6] = 6
FINAL_COUNT: Literal[12] = 12
TOTAL_HOLDOUT_COUNT = SELECTION_COUNT + FINAL_COUNT
HOLDOUT_REQUIRED_TOOLS = ("get_order", "get_policies", "process_return")

HashValue = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
NonEmptyText = Annotated[str, Field(min_length=1)]
HoldoutSplit = Literal["selection", "final"]


class _PrivateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


class ArtifactPointer(_PrivateModel):
    path: NonEmptyText
    sha256: HashValue

    @field_validator("path")
    @classmethod
    def _relative_file_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or value.endswith("/"):
            raise ValueError("artifact path must be a relative file path")
        return value


class SemanticGroupDefinition(_PrivateModel):
    name: NonEmptyText
    source_ids: tuple[NonEmptyText, ...]

    @model_validator(mode="after")
    def _validate_members(self) -> SemanticGroupDefinition:
        if len(self.source_ids) < 2:
            raise ValueError("semantic groups must contain at least two source IDs")
        if len(set(self.source_ids)) != len(self.source_ids):
            raise ValueError("semantic group source IDs must be unique")
        return self


class ProtectedSemanticGroups(_PrivateModel):
    schema_version: Literal["v1alpha1"]
    record_type: Literal["protected_semantic_groups"]
    source_commit: NonEmptyText
    groups: tuple[SemanticGroupDefinition, ...]

    @model_validator(mode="after")
    def _validate_groups(self) -> ProtectedSemanticGroups:
        if self.source_commit != STATE_BENCH_COMMIT:
            raise ValueError("semantic group map source commit is not pinned")
        names = [group.name for group in self.groups]
        if len(set(names)) != len(names):
            raise ValueError("semantic group names must be unique")
        members = [source_id for group in self.groups for source_id in group.source_ids]
        if len(set(members)) != len(members):
            raise ValueError("a source ID cannot belong to multiple semantic groups")
        return self


class HoldoutConstruction(_PrivateModel):
    algorithm: Literal["hmac-sha256-ranked-semantic-groups-v3"]
    semantic_group_policy: Literal["external-protected-connected-components-v1"]
    semantic_group_map: ArtifactPointer
    ranking_key_sha256: HashValue
    current_skill_results_used: Literal[False]

    @model_validator(mode="after")
    def _fixed_semantic_group_path(self) -> HoldoutConstruction:
        if self.semantic_group_map.path != SEMANTIC_GROUP_MAP_PATH:
            raise ValueError("semantic group map must use the protected fixed path")
        return self


class HoldoutManifest(_PrivateModel):
    schema_version: Literal["v1alpha1"]
    record_type: Literal["protected_holdout_manifest"]
    split: HoldoutSplit
    locked: Literal[True]
    source_name: Literal["STATE-Bench"]
    source_commit: NonEmptyText
    upstream_archive_sha256: HashValue
    inventory_commitment_sha256: HashValue
    feedback_policy: Literal["aggregate_gate_only", "none_until_release"]
    run_policy: Literal["paired_gate_evaluation", "once_after_auto_evolution"]
    case_count: int = Field(gt=0)
    slots: tuple[NonEmptyText, ...]

    @model_validator(mode="after")
    def _policy_matches_split(self) -> HoldoutManifest:
        if self.source_commit != STATE_BENCH_COMMIT:
            raise ValueError("holdout source commit is not pinned")
        expected = (
            ("aggregate_gate_only", "paired_gate_evaluation")
            if self.split == "selection"
            else ("none_until_release", "once_after_auto_evolution")
        )
        if (self.feedback_policy, self.run_policy) != expected:
            raise ValueError("holdout policy does not match split")
        expected_count = SELECTION_COUNT if self.split == "selection" else FINAL_COUNT
        prefix = "slot" if self.split == "selection" else "final-slot"
        expected_slots = tuple(
            f"{prefix}-{index:03d}" for index in range(1, expected_count + 1)
        )
        if self.case_count != expected_count or self.slots != expected_slots:
            raise ValueError("holdout lock must contain only the generic split slots")
        return self


class StateRequirement(_PrivateModel):
    entity_type: NonEmptyText
    record_key: NonEmptyText
    field: NonEmptyText
    expected_value: JsonValue


class RubricCriterion(_PrivateModel):
    id: NonEmptyText
    kind: Literal["must", "must_not"]
    requirement: NonEmptyText
    evidence: NonEmptyText


class PrivateFixture(_PrivateModel):
    schema_version: Literal["v1alpha1"]
    record_type: Literal["holdout_private_fixture"]
    case_id: NonEmptyText
    source_id: NonEmptyText
    user_id: NonEmptyText
    now: NonEmptyText
    environment: dict[str, JsonValue]
    user_simulator: dict[str, JsonValue]


class DeterministicOracle(_PrivateModel):
    schema_version: Literal["v1alpha1"]
    record_type: Literal["holdout_deterministic_oracle"]
    case_id: NonEmptyText
    evaluator: Literal["exact_state_requirements_v1"]
    state_requirements: tuple[StateRequirement, ...]

    @field_validator("state_requirements")
    @classmethod
    def _nonempty_requirements(
        cls, value: tuple[StateRequirement, ...]
    ) -> tuple[StateRequirement, ...]:
        if not value:
            raise ValueError("oracle needs at least one state requirement")
        return value


class PrivateRubric(_PrivateModel):
    schema_version: Literal["v1alpha1"]
    record_type: Literal["holdout_private_rubric"]
    case_id: NonEmptyText
    scoring: Literal["all_must_and_no_must_not_v1"]
    criteria: tuple[RubricCriterion, ...]

    @field_validator("criteria")
    @classmethod
    def _nonempty_criteria(
        cls, value: tuple[RubricCriterion, ...]
    ) -> tuple[RubricCriterion, ...]:
        if not value:
            raise ValueError("rubric needs at least one criterion")
        return value


class InventoryRecord(_PrivateModel):
    split: HoldoutSplit
    case_id: NonEmptyText
    source_id: NonEmptyText
    semantic_group_basis: NonEmptyText
    semantic_group_id: NonEmptyText
    rank_sha256: HashValue
    content_hash: HashValue
    source_task_path: NonEmptyText
    source_task_sha256: HashValue
    upstream_fixture_path: NonEmptyText
    upstream_fixture_sha256: HashValue
    public_case: ArtifactPointer
    fixture: ArtifactPointer
    oracle: ArtifactPointer
    rubric: ArtifactPointer


class HoldoutInventory(_PrivateModel):
    schema_version: Literal["v1alpha1"]
    record_type: Literal["protected_holdout_inventory"]
    source_name: Literal["STATE-Bench"]
    source_commit: NonEmptyText
    upstream_archive_sha256: HashValue
    builder_version: NonEmptyText
    selection_algorithm: HoldoutConstruction
    records: tuple[InventoryRecord, ...]

    @model_validator(mode="after")
    def _validate_versions(self) -> HoldoutInventory:
        if self.source_commit != STATE_BENCH_COMMIT:
            raise ValueError("holdout source commit is not pinned")
        if self.builder_version != HOLDOUT_BUILD_VERSION:
            raise ValueError("holdout builder version is not supported")
        return self


class HoldoutCommitments(_PrivateModel):
    """Public split hashes available before either holdout is executed."""

    schema_version: Literal["v1alpha1"]
    record_type: Literal["protected_holdout_commitments"]
    selection_manifest_sha256: HashValue
    final_manifest_sha256: HashValue
    selection_case_count: Literal[6]
    final_case_count: Literal[12]


@dataclass(frozen=True, slots=True)
class SourceDocument:
    source_id: str
    task_path: str
    task_bytes: bytes
    task: Mapping[str, JsonValue]
    fixture_path: str
    fixture_bytes: bytes
    fixture: Mapping[str, JsonValue]


@dataclass(frozen=True, slots=True)
class HoldoutAssignment:
    split: HoldoutSplit
    case_id: str
    source_id: str
    semantic_group_basis: str
    semantic_group_id: str
    rank_sha256: str


@dataclass(frozen=True, slots=True)
class HoldoutSummary:
    selection_count: int
    final_count: int
    inventory_sha256: str
    selection_manifest_sha256: str
    final_manifest_sha256: str


@dataclass(frozen=True, slots=True)
class HoldoutLeakScanResult:
    status: Literal["external_holdout_snapshot_verified"]
    matched_relative_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _SplitIdentity:
    source_ids: frozenset[str]
    semantic_group_ids: frozenset[str]
    case_ids: frozenset[str]
    content_hashes: frozenset[str]


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _json_file_bytes(value: object) -> bytes:
    return _canonical_bytes(value) + b"\n"


def _sha_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha_text(value: str) -> str:
    return _sha_bytes(value.encode("utf-8"))


def _load_json_object_bytes(payload: bytes, label: str) -> dict[str, JsonValue]:
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} must contain a JSON object")
    return cast(dict[str, JsonValue], value)


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} is not valid UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain a JSON object")
    return cast(dict[str, Any], value)


def _normalized_semantic_groups(
    value: ProtectedSemanticGroups,
) -> ProtectedSemanticGroups:
    return ProtectedSemanticGroups(
        schema_version=value.schema_version,
        record_type=value.record_type,
        source_commit=value.source_commit,
        groups=tuple(
            SemanticGroupDefinition(
                name=group.name,
                source_ids=tuple(sorted(group.source_ids)),
            )
            for group in sorted(value.groups, key=lambda item: item.name)
        ),
    )


def _semantic_group_bytes(value: ProtectedSemanticGroups) -> bytes:
    normalized = _normalized_semantic_groups(value)
    return _json_file_bytes(normalized.model_dump(mode="json"))


def _semantic_group_basis(
    source_id: str,
    semantic_groups: ProtectedSemanticGroups,
) -> str:
    for group in semantic_groups.groups:
        if source_id in group.source_ids:
            return group.name
    return f"upstream-independent-task:{source_id}"


def _semantic_group_id(basis: str) -> str:
    return f"state-semantic:{_sha_text(basis)}"


def _validated_ranking_key(ranking_key: bytes) -> bytes:
    if len(ranking_key) < 32:
        raise ValueError("holdout ranking key must contain at least 32 bytes")
    return ranking_key


def read_external_ranking_key(path: Path) -> bytes:
    """Read an owner-only ranking key through descriptor-anchored ancestors."""

    lexical = path if path.is_absolute() else Path.cwd() / path
    try:
        snapshot = read_regular_file_snapshot(lexical.parent, lexical.name)
    except ValueError as exc:
        raise ValueError(
            "ranking key path has a symlink ancestor or is not an external regular file"
        ) from exc
    if snapshot.mode & 0o077:
        raise ValueError("ranking key permissions must deny group/other access")
    return _validated_ranking_key(snapshot.data)


def read_external_semantic_group_map(path: Path) -> ProtectedSemanticGroups:
    """Read an owner-only semantic map through descriptor-anchored ancestors."""

    lexical = path if path.is_absolute() else Path.cwd() / path
    try:
        snapshot = read_regular_file_snapshot(lexical.parent, lexical.name)
    except ValueError as exc:
        raise ValueError(
            "semantic group map path has a symlink ancestor or is not an external "
            "regular file"
        ) from exc
    if snapshot.mode != 0o600:
        raise ValueError("semantic group map permissions must be 0600")
    try:
        value = ProtectedSemanticGroups.model_validate_json(snapshot.data)
    except ValueError:
        raise ValueError("semantic group map is invalid") from None
    return _normalized_semantic_groups(value)


def _validate_semantic_group_sources(
    semantic_groups: ProtectedSemanticGroups,
    source_ids: set[str],
) -> None:
    mapped = {
        source_id for group in semantic_groups.groups for source_id in group.source_ids
    }
    if not mapped <= source_ids:
        raise ValueError("semantic group map contains a source outside the pinned pool")
    singleton_bases = {
        f"upstream-independent-task:{source_id}" for source_id in source_ids
    }
    if any(group.name in singleton_bases for group in semantic_groups.groups):
        raise ValueError("semantic group name collides with an independent-task basis")


def _rank(ranking_key: bytes, kind: str, value: str) -> str:
    return hmac.new(
        _validated_ranking_key(ranking_key),
        f"{kind}\0{value}".encode(),
        hashlib.sha256,
    ).hexdigest()


def select_holdout_sources(
    source_ids: Iterable[str],
    used_source_ids: Iterable[str],
    *,
    ranking_key: bytes,
    semantic_groups: ProtectedSemanticGroups,
) -> tuple[HoldoutAssignment, ...]:
    """Select source IDs without accepting Skill outputs or performance scores."""

    source_values = tuple(source_ids)
    available = set(source_values)
    used = set(used_source_ids)
    if len(available) != len(source_values):
        raise ValueError("source IDs must be unique")
    _validate_semantic_group_sources(semantic_groups, available)

    grouped: dict[str, list[str]] = defaultdict(list)
    for source_id in available:
        grouped[_semantic_group_basis(source_id, semantic_groups)].append(source_id)

    eligible: list[tuple[str, str, str]] = []
    for basis, members in grouped.items():
        if set(members) & used:
            continue
        representative = min(
            members, key=lambda item: _rank(ranking_key, "source", item)
        )
        eligible.append((_rank(ranking_key, "group", basis), basis, representative))
    eligible.sort()
    if len(eligible) < TOTAL_HOLDOUT_COUNT:
        raise ValueError(
            f"need {TOTAL_HOLDOUT_COUNT} unused semantic groups; got {len(eligible)}"
        )

    assignments: list[HoldoutAssignment] = []
    for index, (rank_sha256, basis, source_id) in enumerate(
        eligible[:TOTAL_HOLDOUT_COUNT]
    ):
        if index < SELECTION_COUNT:
            split: HoldoutSplit = "selection"
            case_id = f"slot-{index + 1:03d}"
        else:
            split = "final"
            case_id = f"final-slot-{index - SELECTION_COUNT + 1:03d}"
        assignments.append(
            HoldoutAssignment(
                split=split,
                case_id=case_id,
                source_id=source_id,
                semantic_group_basis=basis,
                semantic_group_id=_semantic_group_id(basis),
                rank_sha256=rank_sha256,
            )
        )
    return tuple(assignments)


def _archive_members(
    archive_path: Path,
    *,
    expected_archive_sha256: str,
) -> tuple[dict[str, tuple[str, bytes]], dict[str, tuple[str, bytes]]]:
    archive_bytes = archive_path.read_bytes()
    actual = _sha_bytes(archive_bytes)
    if actual != expected_archive_sha256:
        raise ValueError(
            f"STATE-Bench archive checksum mismatch: expected "
            f"{expected_archive_sha256}, got {actual}"
        )

    expected_root = f"STATE-Bench-{STATE_BENCH_COMMIT}"
    tasks: dict[str, tuple[str, bytes]] = {}
    fixtures: dict[str, tuple[str, bytes]] = {}
    with tarfile.open(archive_path, mode="r:gz") as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("STATE-Bench archive contains an unsafe path")
            if not path.parts or path.parts[0] != expected_root:
                raise ValueError(
                    "STATE-Bench archive root does not match pinned commit"
                )
            if not member.isfile() or path.suffix != ".json":
                continue
            if path.parent.parent.name != "customer_support":
                continue
            role: dict[str, tuple[str, bytes]] | None = None
            if path.parent.name == "tasks":
                role = tasks
            elif path.parent.name == "task_envs":
                role = fixtures
            if role is None:
                continue
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError("cannot read STATE-Bench archive member")
            source_id = path.stem
            if source_id in role:
                raise ValueError("duplicate STATE-Bench archive member")
            role[source_id] = (path.as_posix(), stream.read())
    return tasks, fixtures


def read_state_bench_archive(
    archive_path: Path,
    *,
    expected_archive_sha256: str = STATE_BENCH_ARCHIVE_SHA256,
    expected_task_count: int | None = 150,
    expected_return_count: int | None = 33,
) -> Mapping[str, SourceDocument]:
    """Read pinned tasks and fixtures without extracting archive paths."""

    task_members, fixture_members = _archive_members(
        archive_path, expected_archive_sha256=expected_archive_sha256
    )
    if expected_task_count is not None and len(task_members) != expected_task_count:
        raise ValueError(
            f"expected {expected_task_count} STATE-Bench tasks; got {len(task_members)}"
        )

    selected: dict[str, SourceDocument] = {}
    for source_id, (task_path, task_bytes) in task_members.items():
        task = _load_json_object_bytes(task_bytes, "STATE-Bench task")
        if task.get("task_id") != source_id:
            raise ValueError("STATE-Bench task ID does not match its filename")
        if task.get("task_type") != "return_item":
            continue
        fixture_member = fixture_members.get(source_id)
        if fixture_member is None:
            raise ValueError("STATE-Bench task environment is missing")
        fixture_path, fixture_bytes = fixture_member
        expected_suffix = f"/task_envs/{source_id}.json"
        if not str(task.get("task_env_path", "")).endswith(expected_suffix):
            raise ValueError("STATE-Bench task environment path does not match")
        fixture = _load_json_object_bytes(fixture_bytes, "STATE-Bench task environment")
        selected[source_id] = SourceDocument(
            source_id=source_id,
            task_path=task_path,
            task_bytes=task_bytes,
            task=task,
            fixture_path=fixture_path,
            fixture_bytes=fixture_bytes,
            fixture=fixture,
        )
    if expected_return_count is not None and len(selected) != expected_return_count:
        raise ValueError(
            f"expected {expected_return_count} return_item tasks; got {len(selected)}"
        )
    return selected


def _creator_source_ids(path: Path) -> set[str]:
    manifest = _load_json_object(path, "creator seed manifest")
    records = manifest.get("records")
    if not isinstance(records, list):
        raise ValueError("creator seed manifest records must be a list")
    values: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("source_id"), str):
            raise ValueError("creator seed record needs source_id")
        values.add(record["source_id"])
    return values


def _develop_source_ids(path: Path) -> set[str]:
    manifest = _load_json_object(path, "develop manifest")
    cases = manifest.get("cases")
    if not isinstance(cases, list):
        raise ValueError("develop manifest cases must be a list")
    values: set[str] = set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("develop manifest case must be an object")
        curation = case.get("curation")
        if not isinstance(curation, dict) or not isinstance(
            curation.get("source_id"), str
        ):
            raise ValueError("develop case needs curation.source_id")
        values.add(curation["source_id"])
    return values


def _task_sequence(task: Mapping[str, JsonValue], key: str) -> list[JsonValue]:
    value = task.get(key)
    if not isinstance(value, list) or not value:
        raise ValueError(f"STATE-Bench task needs nonempty {key}")
    return value


def _task_object(task: Mapping[str, JsonValue], key: str) -> dict[str, JsonValue]:
    value = task.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"STATE-Bench task needs object {key}")
    return value


def _task_text(task: Mapping[str, JsonValue], key: str) -> str:
    value = task.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"STATE-Bench task needs nonempty {key}")
    return value


def _fixture_payload(
    assignment: HoldoutAssignment, source: SourceDocument
) -> dict[str, object]:
    return {
        "schema_version": "v1alpha1",
        "record_type": "holdout_private_fixture",
        "case_id": assignment.case_id,
        "source_id": source.source_id,
        "user_id": _task_text(source.task, "user_id"),
        "now": _task_text(source.task, "now"),
        "environment": source.fixture,
        "user_simulator": _task_object(source.task, "user_simulator"),
    }


def _oracle_payload(
    assignment: HoldoutAssignment, source: SourceDocument
) -> dict[str, object]:
    return {
        "schema_version": "v1alpha1",
        "record_type": "holdout_deterministic_oracle",
        "case_id": assignment.case_id,
        "evaluator": "exact_state_requirements_v1",
        "state_requirements": _task_sequence(source.task, "state_requirements"),
    }


def _rubric_payload(
    assignment: HoldoutAssignment, source: SourceDocument
) -> dict[str, object]:
    return {
        "schema_version": "v1alpha1",
        "record_type": "holdout_private_rubric",
        "case_id": assignment.case_id,
        "scoring": "all_must_and_no_must_not_v1",
        "criteria": _task_sequence(source.task, "task_requirements"),
    }


def _construction(
    ranking_key: bytes,
    semantic_group_map_sha256: str,
) -> dict[str, object]:
    return {
        "algorithm": "hmac-sha256-ranked-semantic-groups-v3",
        "semantic_group_policy": "external-protected-connected-components-v1",
        "semantic_group_map": {
            "path": SEMANTIC_GROUP_MAP_PATH,
            "sha256": semantic_group_map_sha256,
        },
        "ranking_key_sha256": _sha_bytes(_validated_ranking_key(ranking_key)),
        "current_skill_results_used": False,
    }


def build_holdout_bundle(
    *,
    archive_path: Path,
    creator_seed_manifest: Path,
    develop_manifest: Path,
    output_root: Path,
    ranking_key: bytes,
    semantic_groups: ProtectedSemanticGroups,
    expected_archive_sha256: str = STATE_BENCH_ARCHIVE_SHA256,
    expected_task_count: int | None = 150,
    expected_return_count: int | None = 33,
) -> HoldoutSummary:
    """Build a new bundle; refuse to overwrite an existing directory."""

    ranking_key = _validated_ranking_key(ranking_key)
    semantic_groups = _normalized_semantic_groups(semantic_groups)
    sources = read_state_bench_archive(
        archive_path,
        expected_archive_sha256=expected_archive_sha256,
        expected_task_count=expected_task_count,
        expected_return_count=expected_return_count,
    )
    _validate_semantic_group_sources(semantic_groups, set(sources))
    used_sources = _creator_source_ids(creator_seed_manifest)
    used_sources.update(_develop_source_ids(develop_manifest))
    assignments = select_holdout_sources(
        sources,
        used_sources,
        ranking_key=ranking_key,
        semantic_groups=semantic_groups,
    )
    with SecureDirectoryWriter.create(output_root) as writer:
        semantic_group_map_sha256 = writer.write_bytes(
            SEMANTIC_GROUP_MAP_PATH,
            _semantic_group_bytes(semantic_groups),
        )

        inventory_records: list[dict[str, object]] = []
        for assignment in assignments:
            source = sources[assignment.source_id]
            private_prefix = f"private/{assignment.split}/{assignment.case_id}"
            fixture_relative = f"{private_prefix}/fixture.json"
            oracle_relative = f"{private_prefix}/oracle.json"
            rubric_relative = f"{private_prefix}/rubric.json"
            fixture = _fixture_payload(assignment, source)
            oracle = _oracle_payload(assignment, source)
            rubric = _rubric_payload(assignment, source)
            try:
                PrivateFixture.model_validate(fixture)
                DeterministicOracle.model_validate(oracle)
                PrivateRubric.model_validate(rubric)
            except ValueError:
                raise ValueError(
                    "generated holdout private material is invalid"
                ) from None
            fixture_sha256 = writer.write_bytes(
                fixture_relative,
                _json_file_bytes(fixture),
            )
            oracle_sha256 = writer.write_bytes(
                oracle_relative,
                _json_file_bytes(oracle),
            )
            rubric_sha256 = writer.write_bytes(
                rubric_relative,
                _json_file_bytes(rubric),
            )

            prompt = _task_text(source.task, "opening_message")
            try:
                public_case = CaseDefinition(
                    schema_version=SchemaVersion.V1ALPHA1,
                    record_type=RecordType.CASE_DEFINITION,
                    case_id=assignment.case_id,
                    source_id=(
                        "protected-source:"
                        f"{_rank(ranking_key, 'source-commitment', source.source_id)}"
                    ),
                    source_version=STATE_BENCH_COMMIT,
                    transformation_version=HOLDOUT_BUILD_VERSION,
                    split=CaseSplit(assignment.split),
                    user_prompt=prompt,
                    fixture_id=f"protected-fixture:{fixture_sha256}",
                    required_tools=HOLDOUT_REQUIRED_TOOLS,
                )
            except ValueError:
                raise ValueError("generated holdout public case is invalid") from None
            public_relative = f"public/{assignment.split}/{assignment.case_id}.json"
            public_sha256 = writer.write_bytes(
                public_relative,
                _json_file_bytes(public_case.model_dump(mode="json")),
            )
            inventory_records.append(
                {
                    "split": assignment.split,
                    "case_id": assignment.case_id,
                    "source_id": source.source_id,
                    "semantic_group_basis": assignment.semantic_group_basis,
                    "semantic_group_id": assignment.semantic_group_id,
                    "rank_sha256": assignment.rank_sha256,
                    "content_hash": _sha_text(prompt),
                    "source_task_path": source.task_path,
                    "source_task_sha256": _sha_bytes(source.task_bytes),
                    "upstream_fixture_path": source.fixture_path,
                    "upstream_fixture_sha256": _sha_bytes(source.fixture_bytes),
                    "public_case": {
                        "path": public_relative,
                        "sha256": public_sha256,
                    },
                    "fixture": {
                        "path": fixture_relative,
                        "sha256": fixture_sha256,
                    },
                    "oracle": {
                        "path": oracle_relative,
                        "sha256": oracle_sha256,
                    },
                    "rubric": {
                        "path": rubric_relative,
                        "sha256": rubric_sha256,
                    },
                }
            )

        inventory = {
            "schema_version": "v1alpha1",
            "record_type": "protected_holdout_inventory",
            "source_name": "STATE-Bench",
            "source_commit": STATE_BENCH_COMMIT,
            "upstream_archive_sha256": expected_archive_sha256,
            "builder_version": HOLDOUT_BUILD_VERSION,
            "selection_algorithm": _construction(
                ranking_key,
                semantic_group_map_sha256,
            ),
            "records": inventory_records,
        }
        try:
            HoldoutInventory.model_validate(inventory)
        except ValueError:
            raise ValueError("generated private holdout inventory is invalid") from None
        inventory_sha256 = writer.write_bytes(
            "private/holdout-inventory.json",
            _json_file_bytes(inventory),
        )
        writer.write_bytes(HOLDOUT_RANKING_KEY_PATH, ranking_key)

        manifest_relatives: dict[HoldoutSplit, str] = {
            "selection": "selection-manifest.json",
            "final": "final-manifest.json",
        }
        manifest_sha256: dict[HoldoutSplit, str] = {}
        for split, relative in manifest_relatives.items():
            manifest = {
                "schema_version": "v1alpha1",
                "record_type": "protected_holdout_manifest",
                "split": split,
                "locked": True,
                "source_name": "STATE-Bench",
                "source_commit": STATE_BENCH_COMMIT,
                "upstream_archive_sha256": expected_archive_sha256,
                "inventory_commitment_sha256": inventory_sha256,
                "feedback_policy": (
                    "aggregate_gate_only"
                    if split == "selection"
                    else "none_until_release"
                ),
                "run_policy": (
                    "paired_gate_evaluation"
                    if split == "selection"
                    else "once_after_auto_evolution"
                ),
                "case_count": (
                    SELECTION_COUNT if split == "selection" else FINAL_COUNT
                ),
                "slots": [
                    (
                        f"slot-{index:03d}"
                        if split == "selection"
                        else f"final-slot-{index:03d}"
                    )
                    for index in range(
                        1,
                        (SELECTION_COUNT if split == "selection" else FINAL_COUNT) + 1,
                    )
                ],
            }
            HoldoutManifest.model_validate(manifest)
            manifest_sha256[split] = writer.write_bytes(
                relative,
                _json_file_bytes(manifest),
            )

        commitments = HoldoutCommitments(
            schema_version="v1alpha1",
            record_type="protected_holdout_commitments",
            selection_manifest_sha256=manifest_sha256["selection"],
            final_manifest_sha256=manifest_sha256["final"],
            selection_case_count=SELECTION_COUNT,
            final_case_count=FINAL_COUNT,
        )
        writer.write_bytes(
            "holdout-commitments.json",
            _json_file_bytes(commitments.model_dump(mode="json")),
        )

        return HoldoutSummary(
            selection_count=SELECTION_COUNT,
            final_count=FINAL_COUNT,
            inventory_sha256=inventory_sha256,
            selection_manifest_sha256=manifest_sha256["selection"],
            final_manifest_sha256=manifest_sha256["final"],
        )


def _verified_pointer(
    snapshot: SecureDirectorySnapshot,
    pointer: ArtifactPointer,
) -> bytes:
    try:
        payload = snapshot.file(pointer.path).data
    except ValueError:
        raise ValueError("holdout artifact is missing or unsafe") from None
    if _sha_bytes(payload) != pointer.sha256:
        raise ValueError("holdout artifact checksum mismatch")
    return payload


def _manifest(payload: bytes) -> HoldoutManifest:
    return HoldoutManifest.model_validate_json(payload)


def _inventory(payload: bytes) -> HoldoutInventory:
    try:
        return HoldoutInventory.model_validate_json(payload)
    except ValueError:
        raise ValueError("private holdout inventory is invalid") from None


def _semantic_groups(payload: bytes) -> ProtectedSemanticGroups:
    try:
        value = ProtectedSemanticGroups.model_validate_json(payload)
    except ValueError:
        raise ValueError("private semantic group map is invalid") from None
    if payload != _semantic_group_bytes(value):
        raise ValueError("private semantic group map is not canonical")
    return _normalized_semantic_groups(value)


def _read_bundle_ranking_key(snapshot: SecureDirectorySnapshot) -> bytes:
    key = snapshot.file(HOLDOUT_RANKING_KEY_PATH)
    if key.mode & 0o077:
        raise ValueError("holdout ranking key permissions must deny group/other access")
    return _validated_ranking_key(key.data)


def validate_public_holdout_bundle(bundle_root: Path) -> HoldoutSummary:
    """Validate the three publishable opaque locks without reading hidden gold."""

    with SecureDirectorySnapshot.open(bundle_root) as snapshot:
        selection_bytes = snapshot.file("selection-manifest.json").data
        final_bytes = snapshot.file("final-manifest.json").data
        commitments_bytes = snapshot.file("holdout-commitments.json").data
        selection = _manifest(selection_bytes)
        final = _manifest(final_bytes)
        commitments = HoldoutCommitments.model_validate_json(commitments_bytes)
        if selection.split != "selection" or final.split != "final":
            raise ValueError("holdout manifests identify the wrong split")
        if any(
            manifest.upstream_archive_sha256 != STATE_BENCH_ARCHIVE_SHA256
            for manifest in (selection, final)
        ):
            raise ValueError("holdout manifests do not identify the pinned archive")
        if selection.inventory_commitment_sha256 != final.inventory_commitment_sha256:
            raise ValueError("holdout manifests disagree on the private inventory")
        selection_sha256 = _sha_bytes(selection_bytes)
        final_sha256 = _sha_bytes(final_bytes)
        if (
            commitments.selection_manifest_sha256 != selection_sha256
            or commitments.final_manifest_sha256 != final_sha256
        ):
            raise ValueError("public holdout commitments do not match the manifests")

        return HoldoutSummary(
            selection_count=selection.case_count,
            final_count=final.case_count,
            inventory_sha256=selection.inventory_commitment_sha256,
            selection_manifest_sha256=selection_sha256,
            final_manifest_sha256=final_sha256,
        )


def scan_external_holdout_leaks(
    *,
    bundle_root: Path,
    public_lock_root: Path,
    candidate_documents: Mapping[str, str],
) -> HoldoutLeakScanResult:
    """Return only paths whose text exposes committed selected-case material."""

    for relative in candidate_documents:
        path = PurePosixPath(relative)
        if path.is_absolute() or not path.parts or ".." in path.parts:
            raise ValueError("leak scan candidate path is unsafe")

    lock_names = (
        "selection-manifest.json",
        "final-manifest.json",
        "holdout-commitments.json",
    )
    with SecureDirectorySnapshot.open(public_lock_root) as public_snapshot:
        public_locks = tuple(public_snapshot.file(name).data for name in lock_names)

    with SecureDirectorySnapshot.open(bundle_root) as snapshot:
        snapshot.require_private_modes()
        external_locks = tuple(snapshot.file(name).data for name in lock_names)
        if external_locks != public_locks:
            raise ValueError("external holdout locks differ from public commitments")

        selection = _manifest(external_locks[0])
        final = _manifest(external_locks[1])
        commitments = HoldoutCommitments.model_validate_json(external_locks[2])
        inventory_bytes = snapshot.file("private/holdout-inventory.json").data
        inventory = _inventory(inventory_bytes)
        inventory_sha256 = _sha_bytes(inventory_bytes)
        if {
            selection.inventory_commitment_sha256,
            final.inventory_commitment_sha256,
        } != {inventory_sha256}:
            raise ValueError(
                "external holdout inventory differs from public commitment"
            )
        if commitments.selection_manifest_sha256 != _sha_bytes(
            external_locks[0]
        ) or commitments.final_manifest_sha256 != _sha_bytes(external_locks[1]):
            raise ValueError("public holdout commitments do not match the manifests")
        if len(inventory.records) != TOTAL_HOLDOUT_COUNT:
            raise ValueError("external holdout inventory has the wrong case count")

        protected_values: set[str] = set()
        for record in inventory.records:
            public_payload = _verified_pointer(snapshot, record.public_case)
            try:
                public_case = CaseDefinition.model_validate_json(public_payload)
            except ValueError:
                raise ValueError("external holdout public case is invalid") from None
            if _sha_text(public_case.user_prompt) != record.content_hash:
                raise ValueError("external holdout public request commitment drifted")
            protected_values.update(
                {
                    record.source_id,
                    record.content_hash,
                    public_case.user_prompt,
                    json.dumps(public_case.user_prompt, ensure_ascii=False)[1:-1],
                }
            )

        matches = tuple(
            sorted(
                relative
                for relative, text in candidate_documents.items()
                if any(value in text for value in protected_values)
            )
        )
        return HoldoutLeakScanResult(
            status="external_holdout_snapshot_verified",
            matched_relative_paths=matches,
        )


def _creator_identity(
    protected_manifest: Path,
    creator_seed_manifest: Path,
    semantic_groups: ProtectedSemanticGroups,
) -> _SplitIdentity:
    protected = _load_json_object(protected_manifest, "creator protected manifest")
    records = protected.get("records")
    if protected.get("locked") is not True or not isinstance(records, list):
        raise ValueError("creator protected manifest must be locked with records")
    seed = _load_json_object(creator_seed_manifest, "creator seed manifest")
    seed_records = seed.get("records")
    if not isinstance(seed_records, list):
        raise ValueError("creator seed manifest records must be a list")
    source_by_case: dict[str, str] = {}
    prompt_hash_by_case: dict[str, str] = {}
    for item in seed_records:
        if not isinstance(item, dict):
            raise ValueError("creator seed record must be an object")
        case_id = item.get("seed_id")
        source_id = item.get("source_id")
        if not isinstance(case_id, str) or not isinstance(source_id, str):
            raise ValueError("creator seed record needs seed_id and source_id")
        source_by_case[case_id] = source_id
        trace = item.get("trace")
        if isinstance(trace, dict):
            trace_path = trace.get("path")
            trace_sha256 = trace.get("sha256")
            if not isinstance(trace_path, str) or not isinstance(trace_sha256, str):
                raise ValueError("creator trace reference is incomplete")
            relative = PurePosixPath(trace_path)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("creator trace reference is unsafe")
            resolved = creator_seed_manifest.parent.joinpath(*relative.parts)
            payload = resolved.read_bytes()
            if _sha_bytes(payload) != trace_sha256:
                raise ValueError("creator trace checksum drifted")
            trace_record = _load_json_object_bytes(payload, "creator trace")
            request = trace_record.get("request")
            prompt = request.get("prompt") if isinstance(request, dict) else None
            if not isinstance(prompt, str):
                raise ValueError("creator trace has no public request")
            prompt_hash_by_case[case_id] = _sha_text(prompt)
    sources: set[str] = set()
    semantics: set[str] = set()
    cases: set[str] = set()
    hashes: set[str] = set()
    for item in records:
        if not isinstance(item, dict):
            raise ValueError("creator protected record must be an object")
        case_id = item.get("case_id")
        semantic = item.get("semantic_group_id")
        content_hash = item.get("content_hash")
        if not all(
            isinstance(value, str) for value in (case_id, semantic, content_hash)
        ):
            raise ValueError("creator protected record is incomplete")
        assert isinstance(case_id, str)
        if case_id not in source_by_case:
            raise ValueError("creator protected case has no source")
        cases.add(case_id)
        sources.add(source_by_case[case_id])
        semantics.add(
            _semantic_group_id(
                _semantic_group_basis(source_by_case[case_id], semantic_groups)
            )
        )
        hashes.add(prompt_hash_by_case.get(case_id, cast(str, content_hash)))
    return _SplitIdentity(
        source_ids=frozenset(sources),
        semantic_group_ids=frozenset(semantics),
        case_ids=frozenset(cases),
        content_hashes=frozenset(hashes),
    )


def _develop_identity(develop_manifest: Path, candidate_seeds: Path) -> _SplitIdentity:
    candidate_semantics: dict[str, str] = {}
    for line in candidate_seeds.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if not isinstance(item, dict):
            raise ValueError("candidate seed must be an object")
        source_id = item.get("source_id")
        semantic = item.get("semantic_group_id")
        if not isinstance(source_id, str) or not isinstance(semantic, str):
            raise ValueError("candidate seed needs source_id and semantic_group_id")
        candidate_semantics[source_id] = semantic
    manifest = _load_json_object(develop_manifest, "develop manifest")
    records = manifest.get("cases")
    if not isinstance(records, list):
        raise ValueError("develop manifest cases must be a list")
    sources: set[str] = set()
    semantics: set[str] = set()
    cases: set[str] = set()
    hashes: set[str] = set()
    for item in records:
        if not isinstance(item, dict):
            raise ValueError("develop case must be an object")
        case_id = item.get("case_id")
        curation = item.get("curation")
        public_case = item.get("public_case")
        if (
            not isinstance(case_id, str)
            or not isinstance(curation, dict)
            or not isinstance(curation.get("source_id"), str)
            or not isinstance(public_case, dict)
            or not isinstance(public_case.get("sha256"), str)
        ):
            raise ValueError("develop case identity is incomplete")
        source_id = cast(str, curation["source_id"])
        semantic = candidate_semantics.get(source_id)
        if semantic is None:
            raise ValueError("develop source has no semantic group")
        sources.add(source_id)
        semantics.add(semantic)
        cases.add(case_id)
        public_path = public_case.get("path")
        if not isinstance(public_path, str):
            raise ValueError("develop public case reference needs a path")
        relative = PurePosixPath(public_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("develop public case reference is unsafe")
        resolved = develop_manifest.parent.joinpath(*relative.parts)
        payload = resolved.read_bytes()
        if _sha_bytes(payload) != public_case["sha256"]:
            raise ValueError("develop public case checksum drifted")
        try:
            definition = CaseDefinition.model_validate_json(payload)
        except ValueError:
            raise ValueError("develop public case is invalid") from None
        hashes.add(_sha_text(definition.user_prompt))
    return _SplitIdentity(
        source_ids=frozenset(sources),
        semantic_group_ids=frozenset(semantics),
        case_ids=frozenset(cases),
        content_hashes=frozenset(hashes),
    )


def _holdout_identity(
    records: Sequence[InventoryRecord], split: HoldoutSplit
) -> _SplitIdentity:
    selected = [record for record in records if record.split == split]
    return _SplitIdentity(
        source_ids=frozenset(record.source_id for record in selected),
        semantic_group_ids=frozenset(record.semantic_group_id for record in selected),
        case_ids=frozenset(record.case_id for record in selected),
        content_hashes=frozenset(record.content_hash for record in selected),
    )


def _assert_pairwise_disjoint(identities: Mapping[str, _SplitIdentity]) -> None:
    fields = (
        "source_ids",
        "semantic_group_ids",
        "case_ids",
        "content_hashes",
    )
    names = tuple(identities)
    for index, left_name in enumerate(names):
        for right_name in names[index + 1 :]:
            left = identities[left_name]
            right = identities[right_name]
            for field in fields:
                overlap = getattr(left, field) & getattr(right, field)
                if overlap:
                    raise ValueError(
                        f"split identity overlap: {left_name}/{right_name}: {field}"
                    )


def _assert_no_public_leak(
    public_case: CaseDefinition,
    source_id: str,
    public_payload: Mapping[str, object],
    ranking_key: bytes,
) -> None:
    serialized = _canonical_bytes(public_payload).decode("utf-8")
    forbidden_keys = {
        "environment",
        "expected_value",
        "oracle",
        "rubric",
        "state_requirements",
        "task_requirements",
        "user_simulator",
    }
    if forbidden_keys & set(public_payload):
        raise ValueError("public case exposes a private holdout field")
    if source_id in serialized:
        raise ValueError("public case exposes its upstream source ID")
    expected_source = (
        f"protected-source:{_rank(ranking_key, 'source-commitment', source_id)}"
    )
    if public_case.source_id != expected_source:
        raise ValueError("public source commitment does not match private inventory")


def _verify_against_archive(
    records: Sequence[InventoryRecord],
    snapshot: SecureDirectorySnapshot,
    sources: Mapping[str, SourceDocument],
) -> None:
    for record in records:
        source = sources.get(record.source_id)
        if source is None:
            raise ValueError("holdout source is absent from pinned archive")
        if record.source_task_path != source.task_path or (
            record.source_task_sha256 != _sha_bytes(source.task_bytes)
        ):
            raise ValueError("holdout source task provenance drifted")
        if record.upstream_fixture_path != source.fixture_path or (
            record.upstream_fixture_sha256 != _sha_bytes(source.fixture_bytes)
        ):
            raise ValueError("holdout source fixture provenance drifted")
        assignment = HoldoutAssignment(
            split=record.split,
            case_id=record.case_id,
            source_id=record.source_id,
            semantic_group_basis=record.semantic_group_basis,
            semantic_group_id=record.semantic_group_id,
            rank_sha256=record.rank_sha256,
        )
        expected_values = (
            (record.fixture, _fixture_payload(assignment, source)),
            (record.oracle, _oracle_payload(assignment, source)),
            (record.rubric, _rubric_payload(assignment, source)),
        )
        for pointer, expected in expected_values:
            if _verified_pointer(snapshot, pointer) != _json_file_bytes(expected):
                raise ValueError("holdout private derivation drifted")
        public_payload = _verified_pointer(snapshot, record.public_case)
        try:
            public_case = CaseDefinition.model_validate_json(public_payload)
        except ValueError:
            raise ValueError("holdout public case is invalid") from None
        if public_case.user_prompt != _task_text(source.task, "opening_message"):
            raise ValueError("holdout public request drifted")


def _validate_holdout_bundle_snapshot(
    *,
    snapshot: SecureDirectorySnapshot,
    creator_protected_manifest: Path,
    creator_seed_manifest: Path,
    develop_manifest: Path,
    candidate_seeds: Path,
    archive_path: Path | None = None,
    expected_archive_sha256: str = STATE_BENCH_ARCHIVE_SHA256,
    expected_task_count: int | None = 150,
    expected_return_count: int | None = 33,
) -> HoldoutSummary:
    """Validate checksums, privacy, provenance, counts, and four-way isolation."""

    ranking_key = _read_bundle_ranking_key(snapshot)
    snapshot.require_private_modes()
    selection_bytes = snapshot.file("selection-manifest.json").data
    final_bytes = snapshot.file("final-manifest.json").data
    inventory_bytes = snapshot.file("private/holdout-inventory.json").data
    commitments_bytes = snapshot.file("holdout-commitments.json").data
    selection = _manifest(selection_bytes)
    final = _manifest(final_bytes)
    inventory = _inventory(inventory_bytes)
    semantic_group_bytes = _verified_pointer(
        snapshot,
        inventory.selection_algorithm.semantic_group_map,
    )
    semantic_groups = _semantic_groups(semantic_group_bytes)
    commitments = HoldoutCommitments.model_validate_json(commitments_bytes)
    if selection.split != "selection" or final.split != "final":
        raise ValueError("holdout manifests identify the wrong split")
    if len(inventory.records) != TOTAL_HOLDOUT_COUNT:
        raise ValueError(f"private inventory must contain {TOTAL_HOLDOUT_COUNT} cases")
    if any(
        value != expected_archive_sha256
        for value in (
            selection.upstream_archive_sha256,
            final.upstream_archive_sha256,
            inventory.upstream_archive_sha256,
        )
    ):
        raise ValueError("holdout bundle does not identify the expected pinned archive")
    inventory_sha256 = _sha_bytes(inventory_bytes)
    if {
        selection.inventory_commitment_sha256,
        final.inventory_commitment_sha256,
    } != {inventory_sha256}:
        raise ValueError("public manifest inventory commitment does not match")
    if inventory.selection_algorithm.ranking_key_sha256 != _sha_bytes(ranking_key):
        raise ValueError("private inventory does not match the ranking key")
    if commitments.selection_manifest_sha256 != _sha_bytes(
        selection_bytes
    ) or commitments.final_manifest_sha256 != _sha_bytes(final_bytes):
        raise ValueError("public holdout commitments do not match the manifests")

    inventory_by_case = {record.case_id: record for record in inventory.records}
    if len(inventory_by_case) != len(inventory.records):
        raise ValueError("private inventory case IDs must be unique")
    expected_files = {
        "selection-manifest.json",
        "final-manifest.json",
        "holdout-commitments.json",
        "private/holdout-inventory.json",
        HOLDOUT_RANKING_KEY_PATH,
        SEMANTIC_GROUP_MAP_PATH,
    }
    for manifest in (selection, final):
        expected_feedback = (
            "aggregate_gate_only"
            if manifest.split == "selection"
            else "none_until_release"
        )
        if manifest.feedback_policy != expected_feedback:
            raise ValueError("holdout feedback policy drifted")
        observed_slots = tuple(
            record.case_id
            for record in inventory.records
            if record.split == manifest.split
        )
        if observed_slots != manifest.slots:
            raise ValueError(f"{manifest.split} lock slots differ from inventory")

    for record in inventory.records:
        expected_basis = _semantic_group_basis(record.source_id, semantic_groups)
        if (
            record.semantic_group_basis != expected_basis
            or record.semantic_group_id != _semantic_group_id(expected_basis)
            or record.rank_sha256 != _rank(ranking_key, "group", expected_basis)
        ):
            raise ValueError("holdout selection construction drifted")
        public_bytes = _verified_pointer(snapshot, record.public_case)
        fixture_bytes = _verified_pointer(snapshot, record.fixture)
        oracle_bytes = _verified_pointer(snapshot, record.oracle)
        rubric_bytes = _verified_pointer(snapshot, record.rubric)
        expected_files.update(
            {
                record.public_case.path,
                record.fixture.path,
                record.oracle.path,
                record.rubric.path,
            }
        )
        try:
            public_payload = _load_json_object_bytes(
                public_bytes,
                "public holdout case",
            )
            public_case = CaseDefinition.model_validate(public_payload)
            fixture = PrivateFixture.model_validate_json(fixture_bytes)
            oracle = DeterministicOracle.model_validate_json(oracle_bytes)
            rubric = PrivateRubric.model_validate_json(rubric_bytes)
        except ValueError:
            raise ValueError("holdout case material is invalid") from None
        if not (
            public_case.case_id
            == fixture.case_id
            == oracle.case_id
            == rubric.case_id
            == record.case_id
        ):
            raise ValueError("holdout case identity mismatch")
        if public_case.split.value != record.split:
            raise ValueError("holdout split mismatch")
        if public_case.required_tools != HOLDOUT_REQUIRED_TOOLS:
            raise ValueError("holdout tool inventory mismatch")
        if public_case.fixture_id != f"protected-fixture:{record.fixture.sha256}":
            raise ValueError("holdout fixture commitment mismatch")
        if _sha_text(public_case.user_prompt) != record.content_hash:
            raise ValueError("holdout public request content hash mismatch")
        if fixture.source_id != record.source_id:
            raise ValueError("holdout private source identity mismatch")
        _assert_no_public_leak(
            public_case, record.source_id, public_payload, ranking_key
        )

    managed_files = {
        relative
        for relative in snapshot.file_paths
        if (
            PurePosixPath(relative).name
            in {
                "selection-manifest.json",
                "final-manifest.json",
                "holdout-commitments.json",
            }
            or PurePosixPath(relative).parts[:2]
            in {
                ("public", "selection"),
                ("public", "final"),
                ("private", "selection"),
                ("private", "final"),
            }
            or relative == "private/holdout-inventory.json"
            or relative == HOLDOUT_RANKING_KEY_PATH
            or relative == SEMANTIC_GROUP_MAP_PATH
        )
    }
    if managed_files != expected_files:
        raise ValueError("holdout file inventory mismatch")

    identities = {
        "creator": _creator_identity(
            creator_protected_manifest,
            creator_seed_manifest,
            semantic_groups,
        ),
        "develop": _develop_identity(develop_manifest, candidate_seeds),
        "selection": _holdout_identity(inventory.records, "selection"),
        "final": _holdout_identity(inventory.records, "final"),
    }
    _assert_pairwise_disjoint(identities)
    if any(
        len(getattr(identities[split], field)) != expected
        for split, expected in (("selection", SELECTION_COUNT), ("final", FINAL_COUNT))
        for field in (
            "source_ids",
            "semantic_group_ids",
            "case_ids",
            "content_hashes",
        )
    ):
        raise ValueError("selection/final identities must be unique within each split")

    if archive_path is not None:
        sources = read_state_bench_archive(
            archive_path,
            expected_archive_sha256=expected_archive_sha256,
            expected_task_count=expected_task_count,
            expected_return_count=expected_return_count,
        )
        _validate_semantic_group_sources(semantic_groups, set(sources))
        _verify_against_archive(inventory.records, snapshot, sources)
        used = identities["creator"].source_ids | identities["develop"].source_ids
        expected_assignments = select_holdout_sources(
            sources,
            used,
            ranking_key=ranking_key,
            semantic_groups=semantic_groups,
        )
        observed = tuple(
            (
                record.split,
                record.case_id,
                record.source_id,
                record.semantic_group_id,
                record.rank_sha256,
            )
            for record in inventory.records
        )
        expected = tuple(
            (
                item.split,
                item.case_id,
                item.source_id,
                item.semantic_group_id,
                item.rank_sha256,
            )
            for item in expected_assignments
        )
        if observed != expected:
            raise ValueError("holdout source selection does not reproduce")

    return HoldoutSummary(
        selection_count=selection.case_count,
        final_count=final.case_count,
        inventory_sha256=inventory_sha256,
        selection_manifest_sha256=_sha_bytes(selection_bytes),
        final_manifest_sha256=_sha_bytes(final_bytes),
    )


def validate_holdout_bundle(
    *,
    bundle_root: Path,
    creator_protected_manifest: Path,
    creator_seed_manifest: Path,
    develop_manifest: Path,
    candidate_seeds: Path,
    archive_path: Path | None = None,
    expected_archive_sha256: str = STATE_BENCH_ARCHIVE_SHA256,
    expected_task_count: int | None = 150,
    expected_return_count: int | None = 33,
) -> HoldoutSummary:
    """Validate one descriptor-bound bundle lifecycle."""

    with SecureDirectorySnapshot.open(bundle_root) as snapshot:
        return _validate_holdout_bundle_snapshot(
            snapshot=snapshot,
            creator_protected_manifest=creator_protected_manifest,
            creator_seed_manifest=creator_seed_manifest,
            develop_manifest=develop_manifest,
            candidate_seeds=candidate_seeds,
            archive_path=archive_path,
            expected_archive_sha256=expected_archive_sha256,
            expected_task_count=expected_task_count,
            expected_return_count=expected_return_count,
        )
