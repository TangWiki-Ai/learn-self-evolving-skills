from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ses.contracts import TextDeltaPayload, Usage, UsagePayload
from ses.engines.fake import FakeEngine, FakeFixture, FakeStep
from ses.foundation.config import LockedModel
from ses.foundation.workspace import WorkspaceFactory
from ses.testset.curation import (
    CurationBundle,
    CurationResponseSource,
    FixedCurationModel,
    LiveCurationModel,
    LiveModelBinding,
    RubricDraft,
    curate_sources,
)

ROOT = Path(__file__).parents[2]
TICKET = ROOT / "data" / "testset" / "ticket07"
SOURCE = ROOT / "data" / "upstream" / "abcd" / "fixture" / "conversations.json"
RESPONSES = TICKET / "curation-responses.json"
SOURCE_IDS = (
    "abcd:6b8700ce67c6b37b062dd7a60abc76d7ef832a97:train:3592",
    "abcd:6b8700ce67c6b37b062dd7a60abc76d7ef832a97:train:9489",
)


def _run(response_path: Path = RESPONSES) -> CurationBundle:
    return asyncio.run(
        curate_sources(
            source_ids=SOURCE_IDS,
            source_path=SOURCE,
            model=FixedCurationModel.from_path(response_path),
        )
    )


def test_fixed_curation_replays_full_source_screening_without_network() -> None:
    bundle = _run()

    selected = bundle.by_source_id[SOURCE_IDS[0]]
    rejected = bundle.by_source_id[SOURCE_IDS[1]]
    assert selected.selected is True
    assert selected.triage.intent == "initiate_return"
    assert selected.rubric_draft is not None
    assert selected.rubric_draft.public_request_template.count("{order_id}") == 1
    assert rejected.selected is False
    assert rejected.triage.intent == "check_refund_status"
    assert rejected.rubric_draft is None
    assert bundle.network_used is False
    assert bundle.live_provider_used is False
    assert {item.triage_invocation.response_source for item in bundle.sources} == {
        CurationResponseSource.FIXED_RESPONSE
    }


def test_deterministic_environment_gate_overrides_model_approval(
    tmp_path: Path,
) -> None:
    fixture = json.loads(RESPONSES.read_text(encoding="utf-8"))
    fixture["responses"][SOURCE_IDS[1]]["triage"]["mappable"] = True
    path = tmp_path / "responses.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")

    rejected = _run(path).by_source_id[SOURCE_IDS[1]]

    assert rejected.triage.mappable is True
    assert rejected.signals.environment_supported is False
    assert rejected.selected is False
    assert rejected.selection_reason.startswith("deterministic environment gate")


def test_triage_must_cite_an_exact_source_turn(tmp_path: Path) -> None:
    fixture = json.loads(RESPONSES.read_text(encoding="utf-8"))
    fixture["responses"][SOURCE_IDS[0]]["triage"]["evidence_spans"][0]["text"] = (
        "invented evidence"
    )
    path = tmp_path / "responses.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")

    with pytest.raises(ValueError, match="does not match source evidence"):
        _run(path)


def test_rubric_draft_rejects_private_oracle_content() -> None:
    with pytest.raises(ValueError, match="private oracle"):
        RubricDraft.model_validate_json(
            json.dumps(
                {
                    "public_request_template": "Return {order_id} because it {reason}.",
                    "criteria": [
                        {
                            "criterion_id": "response-quality",
                            "criterion": "Repeat the oracle amount_minor.",
                            "evidence_scope": ["key_messages"],
                            "required": True,
                        }
                    ],
                    "reviewer_notes": "Reject leaked answers.",
                }
            )
        )


def test_rubric_draft_rejects_invented_monetary_answers() -> None:
    with pytest.raises(ValueError, match="monetary answers"):
        RubricDraft.model_validate_json(
            json.dumps(
                {
                    "public_request_template": "Return {order_id} because it {reason}.",
                    "criteria": [
                        {
                            "criterion_id": "response-quality",
                            "criterion": "Tell the customer that the refund is $19.00.",
                            "evidence_scope": ["key_messages"],
                            "required": True,
                        }
                    ],
                    "reviewer_notes": "Reject invented amounts.",
                }
            )
        )


def _live_binding(tmp_path: Path, name: str, *steps: FakeStep) -> LiveModelBinding:
    workspace = WorkspaceFactory(tmp_path).create(
        run_id="test-curation",
        case_id=name,
        iteration_id="0",
    )
    return LiveModelBinding(
        engine=FakeEngine(FakeFixture(events=steps)),
        model=LockedModel(
            model_id=f"test/{name}",
            base_url="https://provider.example/v1/",
        ),
        workspace=workspace,
    )


def test_live_adapter_requires_provider_reported_usage(tmp_path: Path) -> None:
    text = '{"intent":"return"}'
    triage = _live_binding(
        tmp_path,
        "triage",
        FakeStep(payload=TextDeltaPayload(message_id="message", text=text)),
    )
    rubric = _live_binding(
        tmp_path,
        "rubric",
        FakeStep(payload=TextDeltaPayload(message_id="message", text=text)),
    )
    model = LiveCurationModel(triage=triage, rubric=rubric)

    try:
        with pytest.raises(ValueError, match="did not report usage"):
            asyncio.run(
                model.invoke(
                    stage="triage",
                    source_id=SOURCE_IDS[0],
                    prompt="Return JSON.",
                    prompt_version="test-v1",
                )
            )
    finally:
        model.close()


def test_live_adapter_records_locked_model_and_measured_usage(tmp_path: Path) -> None:
    text = '{"intent":"return"}'
    steps = (
        FakeStep(payload=TextDeltaPayload(message_id="message", text=text)),
        FakeStep(payload=UsagePayload(usage=Usage(input_tokens=12, output_tokens=4))),
    )
    model = LiveCurationModel(
        triage=_live_binding(tmp_path, "triage", *steps),
        rubric=_live_binding(tmp_path, "rubric", *steps),
    )

    try:
        response = asyncio.run(
            model.invoke(
                stage="triage",
                source_id=SOURCE_IDS[0],
                prompt="Return JSON.",
                prompt_version="test-v1",
            )
        )
    finally:
        model.close()

    assert response.text == text
    assert response.invocation.response_source is CurationResponseSource.LIVE_ENGINE
    assert response.invocation.model_id == "test/triage"
    assert response.invocation.provider_host == "provider.example"
    assert response.invocation.usage.input_tokens == 12
    assert len(response.invocation.output_schema_sha256) == 64
    assert response.invocation.live_model_measured is True
