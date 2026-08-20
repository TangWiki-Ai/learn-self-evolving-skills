from __future__ import annotations

from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from ses.shopping.fixed_course import fixed_public_source_groups
from ses.shopping.profile import (
    ShoppingProfileError,
    ShoppingSourceGroup,
    ShoppingSplit,
    bind_shopping_experiment,
    expand_public_source_groups,
    expand_source_groups,
    load_shopping_profile,
    public_task_refs,
    validate_profile_isolation,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROFILE_ROOT = PROJECT_ROOT / "course" / "capstone-shopping-assistant" / "profiles"


def _source_groups() -> tuple[ShoppingSourceGroup, ...]:
    split_counts: tuple[tuple[ShoppingSplit, int], ...] = (
        ("creator", 2),
        ("develop", 3),
        ("selection", 2),
        ("final", 3),
    )
    return tuple(
        ShoppingSourceGroup(
            source_group_id=f"private-{split}-{index:02d}",
            semantic_family_id=f"original-family-{split}-{index:02d}",
            split=split,
        )
        for split, count in split_counts
        for index in range(1, count + 1)
    )


def test_fixed_profile_expands_ten_groups_into_the_locked_forty_slot_matrix() -> None:
    loaded = load_shopping_profile(PROFILE_ROOT / "fixed-v1.json")

    slots = expand_source_groups(loaded.profile, _source_groups())

    assert len(_source_groups()) == 10
    assert len(slots) == 40
    assert Counter(slot.split for slot in slots) == {
        "creator": 8,
        "develop": 12,
        "selection": 8,
        "final": 12,
    }
    assert Counter(slot.scenario.value for slot in slots) == {
        "single": 10,
        "single_persona": 10,
        "multi": 10,
        "multi_persona": 10,
    }
    assert Counter((slot.split, slot.scenario.value) for slot in slots) == {
        ("creator", "single"): 2,
        ("creator", "single_persona"): 2,
        ("creator", "multi"): 2,
        ("creator", "multi_persona"): 2,
        ("develop", "single"): 3,
        ("develop", "single_persona"): 3,
        ("develop", "multi"): 3,
        ("develop", "multi_persona"): 3,
        ("selection", "single"): 2,
        ("selection", "single_persona"): 2,
        ("selection", "multi"): 2,
        ("selection", "multi_persona"): 2,
        ("final", "single"): 3,
        ("final", "single_persona"): 3,
        ("final", "multi"): 3,
        ("final", "multi_persona"): 3,
    }


def test_fixed_public_inventory_exposes_only_creator_and_develop_groups() -> None:
    loaded = load_shopping_profile(PROFILE_ROOT / "fixed-v1.json")

    groups = fixed_public_source_groups()
    slots = expand_public_source_groups(loaded.profile, groups)

    assert Counter(group.split for group in groups) == {
        "creator": 2,
        "develop": 3,
    }
    assert len(slots) == 20
    assert Counter(slot.split for slot in slots) == {
        "creator": 8,
        "develop": 12,
    }
    assert not {"selection", "final"} & {group.split for group in groups}


def test_semantic_family_is_grouped_before_assignment_to_a_split() -> None:
    loaded = load_shopping_profile(PROFILE_ROOT / "fixed-v1.json")
    groups = list(_source_groups())
    groups[0] = ShoppingSourceGroup(
        source_group_id=groups[0].source_group_id,
        semantic_family_id="shared-family",
        split="creator",
    )
    groups[5] = ShoppingSourceGroup(
        source_group_id=groups[5].source_group_id,
        semantic_family_id=" Shared-Family ",
        split="selection",
    )

    with pytest.raises(ShoppingProfileError, match="grouped before split"):
        expand_source_groups(loaded.profile, tuple(groups))


def test_public_projection_cannot_enumerate_selection_or_final_slots() -> None:
    loaded = load_shopping_profile(PROFILE_ROOT / "fixed-v1.json")
    slots = expand_source_groups(loaded.profile, _source_groups())

    public_refs = public_task_refs(slots)

    assert Counter(task.split for task in public_refs) == {
        "creator": 8,
        "develop": 12,
    }
    assert not {
        slot.episode_slot for slot in slots if slot.split in {"selection", "final"}
    } & {task.opaque_slot for task in public_refs}


def test_protected_splits_require_distinct_aggregate_commitments() -> None:
    loaded = load_shopping_profile(PROFILE_ROOT / "fixed-v1.json")
    shared_commitment = "a" * 64

    with pytest.raises(ValueError, match="distinct aggregate"):
        loaded.profile.model_copy(
            update={
                "protected_split_commitments": {
                    "selection": shared_commitment,
                    "final": shared_commitment,
                }
            }
        )


def test_fixed_and_live_bind_to_distinct_experiments(tmp_path: Path) -> None:
    fixed = load_shopping_profile(PROFILE_ROOT / "fixed-v1.json")
    live = load_shopping_profile(PROFILE_ROOT / "live-v1.json")
    fixed_binding = bind_shopping_experiment(
        fixed,
        experiment_root=tmp_path / "fixed",
        lineage_id="lineage-shopping-fixed-v1",
    )
    live_binding = bind_shopping_experiment(
        live,
        experiment_root=tmp_path / "live",
        lineage_id="lineage-shopping-live-v1",
    )

    validate_profile_isolation(fixed_binding, live_binding)

    assert fixed_binding.profile_sha256 != live_binding.profile_sha256
    assert fixed_binding.experiment_root != live_binding.experiment_root
    assert fixed_binding.lineage_id != live_binding.lineage_id


def test_fixed_and_live_cannot_share_an_experiment_root(tmp_path: Path) -> None:
    fixed = load_shopping_profile(PROFILE_ROOT / "fixed-v1.json")
    live = load_shopping_profile(PROFILE_ROOT / "live-v1.json")
    shared_root = tmp_path / "shared"
    fixed_binding = bind_shopping_experiment(
        fixed,
        experiment_root=shared_root,
        lineage_id="lineage-shopping-fixed-v1",
    )
    live_binding = bind_shopping_experiment(
        live,
        experiment_root=shared_root / ".." / "shared",
        lineage_id="lineage-shopping-live-v1",
    )

    with pytest.raises(ShoppingProfileError, match="experiment root"):
        validate_profile_isolation(fixed_binding, live_binding)


def test_fixed_and_live_cannot_share_a_registry_lineage(tmp_path: Path) -> None:
    fixed = load_shopping_profile(PROFILE_ROOT / "fixed-v1.json")
    live = load_shopping_profile(PROFILE_ROOT / "live-v1.json")
    fixed_binding = bind_shopping_experiment(
        fixed,
        experiment_root=tmp_path / "fixed",
        lineage_id="lineage-shopping-shared",
    )
    live_binding = bind_shopping_experiment(
        live,
        experiment_root=tmp_path / "live",
        lineage_id="lineage-shopping-shared",
    )

    with pytest.raises(ShoppingProfileError, match="lineage"):
        validate_profile_isolation(fixed_binding, live_binding)


def test_fixed_receipts_cannot_be_relabelled_as_live_profile_evidence(
    tmp_path: Path,
) -> None:
    fixed = load_shopping_profile(PROFILE_ROOT / "fixed-v1.json")
    live = load_shopping_profile(PROFILE_ROOT / "live-v1.json")
    fixed_binding = bind_shopping_experiment(
        fixed,
        experiment_root=tmp_path / "fixed",
        lineage_id="lineage-shopping-fixed-v1",
    )
    live_binding = bind_shopping_experiment(
        live,
        experiment_root=tmp_path / "live",
        lineage_id="lineage-shopping-live-v1",
    )
    relabelled_live = replace(
        live_binding,
        profile_sha256=fixed_binding.profile_sha256,
    )

    with pytest.raises(ShoppingProfileError, match="profile hash"):
        validate_profile_isolation(fixed_binding, relabelled_live)
