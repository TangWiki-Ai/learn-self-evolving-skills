from __future__ import annotations

from copy import deepcopy

import pytest

from ses.testset.scrub import ScrubError, scrub_abcd
from ses.testset.sources import (
    ABCD_COMMIT,
    SourceShapeError,
    filter_abcd_product_defect,
    filter_state_return_items,
    flatten_abcd_document,
    match_state_trajectories,
)


def abcd_record(
    convo_id: int,
    *,
    flow: str = "product_defect",
    subflow: str = "return_size",
    customer_text: str = "The screen flickers.",
) -> dict[str, object]:
    return {
        "convo_id": convo_id,
        "source_split": "fixture",
        "scenario": {"flow": flow, "subflow": subflow},
        "original": [
            ["customer", customer_text],
            ["agent", "I can help with that. 👍"],
        ],
        "delexed": [
            {"speaker": "customer", "text": customer_text, "turn_count": 1},
            {
                "speaker": "agent",
                "text": "i can help with that. 👍",
                "turn_count": 2,
            },
        ],
    }


def test_filters_use_exact_json_values() -> None:
    state_tasks = [
        {"task_id": "keep", "task_type": "return_item"},
        {"task_id": "wrong-case", "task_type": "Return_Item"},
        {"task_id": "wrong-spacing", "task_type": "return_item "},
        {"task_id": "wrong-field", "type": "return_item"},
    ]
    conversations = [
        abcd_record(1),
        abcd_record(2, flow="Product_Defect"),
        abcd_record(3, flow="product_defect "),
        {"convo_id": 4, "flow": "product_defect", "scenario": {}},
    ]

    assert [item["task_id"] for item in filter_state_return_items(state_tasks)] == [
        "keep"
    ]
    assert [item["convo_id"] for item in filter_abcd_product_defect(conversations)] == [
        1
    ]


def test_profiles_do_not_silently_mix_sample_and_full_abcd_shapes() -> None:
    with pytest.raises(SourceShapeError, match="top-level list"):
        flatten_abcd_document({"train": [], "dev": [], "test": []}, profile="fixture")
    with pytest.raises(SourceShapeError, match="train/dev/test"):
        flatten_abcd_document([], profile="full")


def test_state_trajectory_join_uses_filename_stem_after_json_filter() -> None:
    tasks = [
        {"task_id": "return-task", "task_type": "return_item"},
        {"task_id": "other-task", "task_type": "cancel_order"},
    ]
    trajectories: dict[str, dict[str, object]] = {
        "return-task": {"conversation": []},
        "other-task": {"conversation": []},
    }

    matches = match_state_trajectories(filter_state_return_items(tasks), trajectories)

    assert len(matches) == 1
    assert matches[0][0]["task_id"] == "return-task"


def test_scrub_preserves_original_delexed_pair_and_intent() -> None:
    raw = abcd_record(
        3592,
        subflow="return_size",
        customer_text="I need the café-size item returned.\u00a0",
    )
    before = deepcopy(raw)

    result = scrub_abcd([raw])

    assert raw == before
    assert result.funnel.input_records == 1
    assert result.funnel.output_records == 1
    record = result.records[0]
    assert record.source_id == f"abcd:{ABCD_COMMIT}:fixture:3592"
    assert record.upstream_id == "3592"
    assert record.flow == "product_defect"
    assert record.subflow == "return_size"
    assert record.original[0].text == "I need the café-size item returned.\u00a0"
    assert record.delexed[0].text == "I need the café-size item returned.\u00a0"
    assert "café-size" in record.normalized_text
    assert len(record.original) == len(record.delexed)


def test_stable_ids_and_deduplication_do_not_depend_on_input_order() -> None:
    first = abcd_record(10, customer_text="The screen flickers.\u00a0")
    duplicate = abcd_record(11, customer_text="  The   screen flickers. ")
    label_conflict = abcd_record(
        12,
        subflow="refund_status",
        customer_text="The screen flickers.",
    )

    forward = scrub_abcd([first, duplicate, label_conflict])
    reverse = scrub_abcd([label_conflict, duplicate, first])

    assert [record.source_id for record in forward.records] == [
        record.source_id for record in reverse.records
    ]
    prefix = f"abcd:{ABCD_COMMIT}:fixture:"
    assert [record.source_id for record in forward.records] == [
        f"{prefix}10",
        f"{prefix}12",
    ]
    assert forward.records[0].duplicate_source_ids == (f"{prefix}11",)
    assert forward.funnel.dropped_duplicates == 1
    # An identical utterance with a different upstream intent label remains auditable.
    assert {record.subflow for record in forward.records} == {
        "return_size",
        "refund_status",
    }


def test_scrub_counts_empty_and_misaligned_records() -> None:
    empty = abcd_record(20)
    empty["original"] = []
    misaligned = abcd_record(21)
    assert isinstance(misaligned["delexed"], list)
    misaligned["delexed"].pop()

    result = scrub_abcd([empty, misaligned, abcd_record(22)])

    assert [record.source_id for record in result.records] == [
        f"abcd:{ABCD_COMMIT}:fixture:22"
    ]
    assert result.funnel.dropped_empty == 1
    assert result.funnel.dropped_misaligned == 1
    assert result.funnel.output_records == 1


def test_scrub_counts_null_and_invalid_utf8_without_rewriting_other_records() -> None:
    null_intent = abcd_record(30)
    null_intent["scenario"] = {"flow": "product_defect", "subflow": None}
    invalid_utf8 = abcd_record(31, customer_text="broken surrogate \ud800")
    valid = abcd_record(32, customer_text="Keep this exact intent.")

    result = scrub_abcd([null_intent, invalid_utf8, valid])

    assert result.funnel.dropped_invalid == 1
    assert result.funnel.dropped_encoding == 1
    assert len(result.records) == 1
    assert result.records[0].original[0].text == "Keep this exact intent."


def test_duplicate_source_identity_with_changed_intent_is_rejected() -> None:
    first = abcd_record(40, subflow="return_size")
    conflicting = abcd_record(40, subflow="refund_status")

    with pytest.raises(ScrubError, match="source identity conflict"):
        scrub_abcd([first, conflicting])
