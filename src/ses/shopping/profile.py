"""Locked shopping course profiles and trusted source-group expansion."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal
from unicodedata import normalize

from ses.contracts.primitives import SchemaVersion
from ses.contracts.serialization import content_sha256
from ses.contracts.shopping import (
    ShoppingProfile,
    ShoppingScenario,
    ShoppingTaskRef,
)

ShoppingSplit = Literal["creator", "develop", "selection", "final"]
_SPLITS: tuple[ShoppingSplit, ...] = (
    "creator",
    "develop",
    "selection",
    "final",
)


class ShoppingProfileError(ValueError):
    """A profile or its trusted source-group mapping violates the locked policy."""


@dataclass(frozen=True, slots=True)
class LoadedShoppingProfile:
    """A validated profile and its canonical semantic digest."""

    profile: ShoppingProfile
    profile_sha256: str


@dataclass(frozen=True, slots=True)
class ShoppingExperimentBinding:
    """Profile identity bound to one experiment root and Registry lineage."""

    profile_id: str
    mode: Literal["fixed", "live"]
    profile_sha256: str
    experiment_root: Path
    lineage_id: str


@dataclass(frozen=True, slots=True)
class ShoppingSourceGroup:
    """Trusted private identity assigned to a split before scenario expansion."""

    source_group_id: str
    semantic_family_id: str
    split: ShoppingSplit

    def __post_init__(self) -> None:
        if not self.source_group_id.strip() or not self.semantic_family_id.strip():
            raise ShoppingProfileError("source-group identities must not be blank")
        if self.split not in _SPLITS:
            raise ShoppingProfileError(f"unknown shopping split: {self.split!r}")


@dataclass(frozen=True, slots=True)
class ShoppingEpisodeSlot:
    """Private expanded episode identity held by a trusted runner."""

    source_group_id: str
    episode_slot: str
    split: ShoppingSplit
    scenario: ShoppingScenario
    source_version: str


def shopping_experiment_id(loaded: LoadedShoppingProfile) -> str:
    """Return the stable experiment identity for one profile revision."""

    return f"experiment-shopping-{loaded.profile.mode}-{loaded.profile_sha256[:16]}"


def shopping_lineage_id(loaded: LoadedShoppingProfile) -> str:
    """Return the stable Registry lineage identity for one profile revision."""

    return f"lineage-shopping-{loaded.profile.mode}-{loaded.profile_sha256[:16]}"


def load_shopping_profile(path: Path) -> LoadedShoppingProfile:
    """Load one strict public profile and compute its canonical content hash."""

    profile = ShoppingProfile.model_validate_json(path.read_bytes())
    return LoadedShoppingProfile(
        profile=profile,
        profile_sha256=content_sha256(profile),
    )


def bind_shopping_experiment(
    loaded: LoadedShoppingProfile,
    *,
    experiment_root: Path,
    lineage_id: str,
) -> ShoppingExperimentBinding:
    """Bind a validated profile to explicit experiment and lineage identities."""

    if not lineage_id.strip():
        raise ShoppingProfileError("lineage_id must not be blank")
    return ShoppingExperimentBinding(
        profile_id=loaded.profile.profile_id,
        mode=loaded.profile.mode,
        profile_sha256=loaded.profile_sha256,
        experiment_root=experiment_root.resolve(strict=False),
        lineage_id=lineage_id,
    )


def validate_profile_isolation(
    first: ShoppingExperimentBinding,
    second: ShoppingExperimentBinding,
) -> None:
    """Reject fixed/live bindings that do not form one cross-mode pair."""

    if {first.mode, second.mode} != {"fixed", "live"}:
        raise ShoppingProfileError("isolation requires one fixed and one live profile")
    if first.profile_sha256 == second.profile_sha256:
        raise ShoppingProfileError("fixed and live must use different profile hashes")
    if first.experiment_root == second.experiment_root:
        raise ShoppingProfileError("fixed and live must use different experiment roots")
    if first.lineage_id == second.lineage_id:
        raise ShoppingProfileError("fixed and live must use different lineage IDs")


def expand_source_groups(
    profile: ShoppingProfile,
    source_groups: tuple[ShoppingSourceGroup, ...],
) -> tuple[ShoppingEpisodeSlot, ...]:
    """Validate private groups before expanding every group into four scenarios."""

    return _expand_source_groups(
        profile,
        source_groups,
        expected_group_counts=dict(profile.source_group_counts),
        expected_slot_counts=dict(profile.episode_slot_counts),
    )


def expand_public_source_groups(
    profile: ShoppingProfile,
    source_groups: tuple[ShoppingSourceGroup, ...],
) -> tuple[ShoppingEpisodeSlot, ...]:
    """Expand only learner-visible creator/develop groups."""

    public_splits: tuple[ShoppingSplit, ...] = ("creator", "develop")
    if any(group.split not in public_splits for group in source_groups):
        raise ShoppingProfileError(
            "public source groups cannot reveal protected splits"
        )
    return _expand_source_groups(
        profile,
        source_groups,
        expected_group_counts={
            split: profile.source_group_counts[split] for split in public_splits
        },
        expected_slot_counts={
            split: profile.episode_slot_counts[split] for split in public_splits
        },
    )


def _expand_source_groups(
    profile: ShoppingProfile,
    source_groups: tuple[ShoppingSourceGroup, ...],
    *,
    expected_group_counts: dict[ShoppingSplit, int],
    expected_slot_counts: dict[ShoppingSplit, int],
) -> tuple[ShoppingEpisodeSlot, ...]:
    """Validate one trusted group inventory and expand each group once per scenario."""

    groups = tuple(source_groups)
    group_ids = [group.source_group_id for group in groups]
    if len(group_ids) != len(set(group_ids)):
        raise ShoppingProfileError("source_group_id must be unique before split")

    semantic_families = [
        normalize("NFKC", group.semantic_family_id).strip().casefold()
        for group in groups
    ]
    if len(semantic_families) != len(set(semantic_families)):
        raise ShoppingProfileError("semantic family must be grouped before split")

    actual_counts = Counter(group.split for group in groups)
    if dict(actual_counts) != expected_group_counts:
        raise ShoppingProfileError(
            "source-group counts do not match the locked profile"
        )

    slots = tuple(
        ShoppingEpisodeSlot(
            source_group_id=group.source_group_id,
            episode_slot=_opaque_slot(profile, group, scenario),
            split=group.split,
            scenario=scenario,
            source_version=profile.source_version,
        )
        for group in groups
        for scenario in profile.scenarios
    )
    slot_counts = Counter(slot.split for slot in slots)
    if dict(slot_counts) != expected_slot_counts:
        raise ShoppingProfileError("expanded slots do not match the locked profile")
    return slots


def public_task_refs(
    slots: tuple[ShoppingEpisodeSlot, ...],
) -> tuple[ShoppingTaskRef, ...]:
    """Project only learner-visible creator/develop slots into public records."""

    return tuple(
        ShoppingTaskRef(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type="shopping_task_ref",
            opaque_slot=slot.episode_slot,
            scenario=slot.scenario,
            split=slot.split,
            source_version=slot.source_version,
        )
        for slot in slots
        if slot.split in {"creator", "develop"}
    )


def _opaque_slot(
    profile: ShoppingProfile,
    group: ShoppingSourceGroup,
    scenario: ShoppingScenario,
) -> str:
    material = (
        f"{profile.profile_id}\0{group.source_group_id}\0{scenario.value}"
    ).encode()
    return f"slot-{sha256(material).hexdigest()[:24]}"
