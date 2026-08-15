from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, localcontext
from typing import Literal

import pytest
from pydantic import JsonValue, ValidationError

from ses.contracts import (
    ArtifactRef,
    ArtifactRoot,
    ContractModel,
    RunId,
    SchemaVersion,
    Usage,
    UtcDateTime,
    VersionedRecord,
    canonical_json,
    canonical_sha256,
)

ARTIFACT_SHA256 = "c7c5c1d70c5dec4416ab6158afd0b223ef40c29b1dc1f97ed9428b94d4cadb1c"


class ExampleContract(ContractModel):
    run_id: RunId
    occurred_at: UtcDateTime
    payload: dict[str, JsonValue]


class ExampleRecord(VersionedRecord):
    record_type: Literal["example"] = "example"


def test_contracts_are_frozen_and_reject_unknown_fields() -> None:
    contract = ExampleContract(
        run_id="run-1",
        occurred_at=datetime(2026, 8, 16, tzinfo=UTC),
        payload={},
    )

    attribute = "run_id"
    with pytest.raises(ValidationError, match="frozen"):
        setattr(contract, attribute, "run-2")

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ExampleContract.model_validate(
            {
                "run_id": "run-1",
                "occurred_at": "2026-08-16T00:00:00Z",
                "payload": {},
                "unexpected": True,
            }
        )


def test_versioned_records_reject_unsupported_versions() -> None:
    assert ExampleRecord().schema_version is SchemaVersion.V1ALPHA1

    with pytest.raises(ValidationError, match="unsupported schema_version"):
        ExampleRecord.model_validate({"schema_version": "v2", "record_type": "example"})


def test_utc_datetime_is_normalized_and_serialized_as_rfc3339_z() -> None:
    contract = ExampleContract.model_validate(
        {
            "run_id": "run-1",
            "occurred_at": "2026-08-16T12:30:00+08:00",
            "payload": {},
        }
    )

    assert contract.occurred_at == datetime(2026, 8, 16, 4, 30, tzinfo=UTC)
    assert contract.model_dump(mode="json")["occurred_at"] == "2026-08-16T04:30:00Z"


@pytest.mark.parametrize(
    "occurred_at",
    [
        datetime(2026, 8, 16, 4, 30),
        "2026-08-16T04:30:00",
        1_776_314_600,
        "1776314600",
        b"2026-08-16T04:30:00Z",
        Decimal("1776314600"),
        "2026-08-16T04:30:00-00:00",
    ],
)
def test_utc_datetime_rejects_naive_and_epoch_values(occurred_at: object) -> None:
    with pytest.raises(ValidationError):
        ExampleContract.model_validate(
            {"run_id": "run-1", "occurred_at": occurred_at, "payload": {}}
        )


def test_canonical_json_has_a_stable_known_digest() -> None:
    first: JsonValue = {"b": 2, "a": 1}
    second: JsonValue = {"a": 1, "b": 2}

    assert canonical_json(first) == b'{"a":1,"b":2}'
    assert canonical_sha256(first) == canonical_sha256(second)
    assert (
        canonical_sha256(first)
        == "43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777"
    )
    assert canonical_sha256({"a": 1, "b": 3}) != canonical_sha256(first)


def test_decimal_canonical_json_is_independent_of_decimal_context() -> None:
    usage = Usage(
        input_tokens=1,
        output_tokens=1,
        cost_amount=Decimal("1.2345678901234567890123456789012345"),
        cost_currency="USD",
    )
    hashes: set[str] = set()

    for precision in (10, 28, 50):
        with localcontext() as context:
            context.prec = precision
            hashes.add(usage.canonical_sha256())

    assert len(hashes) == 1
    assert b"1.2345678901234567890123456789012345" in usage.canonical_json()


def test_decimal_canonical_json_does_not_expand_large_exponents() -> None:
    usage = Usage(
        input_tokens=1,
        output_tokens=1,
        cost_amount=Decimal("1E+999999999"),
        cost_currency="USD",
    )

    assert b'"1E+999999999"' in usage.canonical_json()


@pytest.mark.parametrize(
    "path",
    [
        "",
        ".",
        "../trace.json",
        "runs/../trace.json",
        "/tmp/trace.json",
        "C:/trace.json",
        r"runs\trace.json",
        "runs//trace.json",
        "runs/./trace.json",
        "runs/trace.json/",
        "~/trace.json",
        "//server/share/trace.json",
        "runs/trace\x00.json",
    ],
)
def test_artifact_reference_rejects_noncanonical_relative_posix_paths(
    path: str,
) -> None:
    with pytest.raises(ValidationError):
        ArtifactRef(root=ArtifactRoot.RUN, path=path, sha256=ARTIFACT_SHA256)


def test_artifact_reference_verifies_content_checksum() -> None:
    reference = ArtifactRef(
        root=ArtifactRoot.RUN,
        path="traces/trace.json",
        sha256=ARTIFACT_SHA256,
    )

    reference.verify_bytes(b"artifact")
    with pytest.raises(ValueError, match="checksum mismatch"):
        reference.verify_bytes(b"different")


@pytest.mark.parametrize(
    "sha256",
    [ARTIFACT_SHA256.upper(), ARTIFACT_SHA256[:-1], "g" * 64],
)
def test_artifact_reference_requires_lowercase_sha256(sha256: str) -> None:
    with pytest.raises(ValidationError):
        ArtifactRef(root=ArtifactRoot.WORKSPACE, path="trace.json", sha256=sha256)


@pytest.mark.parametrize(
    "payload",
    [
        {"api_key": "not-a-real-value"},
        {"ANTHROPIC_API_KEY": "not-a-real-value"},
        {"accessToken": "not-a-real-value"},
        {"nested": [{"Authorization": "not-a-real-value"}]},
        {"request-headers": {"x-api-key": "not-a-real-value"}},
        {"hidden_gold": {"answer": "private"}},
        {"api__key": "not-a-real-value"},
        {"api/key": "not-a-real-value"},
        {"api:key": "not-a-real-value"},
        {"secretKey": "not-a-real-value"},
        {"apiToken": "not-a-real-value"},
        {"httpHeaders": "not-a-real-value"},
        {"response_headers": "not-a-real-value"},
        {"proxyAuthorization": "not-a-real-value"},
        {"selection_gold": "private"},
        {"final_gold": "private"},
        {"goldAnswer": "private"},
        {"hidden__gold": "private"},
        {"api\u200b_key": "not-a-real-value"},
        {"api_key_value": "not-a-real-value"},
        {"x_api_key_id": "not-a-real-value"},
        {"authorizationHeader": "not-a-real-value"},
        {"headers_map": "not-a-real-value"},
        {"response_headers_raw": "not-a-real-value"},
        {"clientSecretValue": "not-a-real-value"},
        {"hiddenGoldValue": "private"},
        {"selectionAnswerText": "private"},
        {"final_answer_text": "private"},
        {"secret_key_material": "not-a-real-value"},
        {"myAPIKeyValue": "not-a-real-value"},
    ],
)
def test_contract_payloads_reject_credential_and_hidden_fields(
    payload: dict[str, JsonValue],
) -> None:
    with pytest.raises(ValidationError, match="forbidden field"):
        ExampleContract.model_validate(
            {
                "run_id": "run-1",
                "occurred_at": "2026-08-16T04:30:00Z",
                "payload": payload,
            }
        )


def test_usage_token_field_names_are_not_mistaken_for_credentials() -> None:
    contract = ExampleContract.model_validate(
        {
            "run_id": "run-1",
            "occurred_at": "2026-08-16T04:30:00Z",
            "payload": {"input_tokens": 1, "output_tokens": 2},
        }
    )

    assert contract.payload == {"input_tokens": 1, "output_tokens": 2}


def test_mutated_nested_payload_cannot_serialize_a_credential_field() -> None:
    contract = ExampleContract.model_validate(
        {
            "run_id": "run-1",
            "occurred_at": "2026-08-16T04:30:00Z",
            "payload": {},
        }
    )
    with pytest.raises(TypeError, match="frozen"):
        contract.payload["api_key"] = "not-a-real-value"


def test_nested_json_arrays_are_frozen() -> None:
    contract = ExampleContract.model_validate(
        {
            "run_id": "run-1",
            "occurred_at": "2026-08-16T04:30:00Z",
            "payload": {"items": [{"id": "item-1"}]},
        }
    )
    items = contract.payload["items"]

    assert isinstance(items, list)
    with pytest.raises(TypeError, match="frozen"):
        items.append({"id": "item-2"})


def test_model_copy_revalidates_updates_and_preserves_deep_freezing() -> None:
    contract = ExampleContract.model_validate(
        {
            "run_id": "run-1",
            "occurred_at": "2026-08-16T04:30:00Z",
            "payload": {"items": [1]},
        }
    )

    copied = contract.model_copy(update={"payload": {"items": [2]}})
    deep_copy = contract.model_copy(deep=True)

    copied_items = copied.payload["items"]
    assert isinstance(copied_items, list)
    with pytest.raises(TypeError, match="frozen"):
        copied_items.append(3)
    assert deep_copy == contract

    with pytest.raises(ValidationError, match="forbidden field"):
        contract.model_copy(update={"payload": {"api_key_value": "not-a-real-value"}})


@pytest.mark.parametrize(
    "payload",
    [
        {"value": "\ud800"},
        {"\ud800": "value"},
    ],
)
def test_contracts_reject_strings_that_are_not_valid_utf8(
    payload: dict[str, JsonValue],
) -> None:
    with pytest.raises(ValidationError, match="UTF-8"):
        ExampleContract.model_validate(
            {
                "run_id": "run-1",
                "occurred_at": "2026-08-16T04:30:00Z",
                "payload": payload,
            }
        )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_canonical_json_rejects_non_finite_floats(value: float) -> None:
    with pytest.raises(ValueError):
        canonical_json({"value": value})
