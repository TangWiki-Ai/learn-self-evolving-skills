from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from ses.contracts import (
    ArtifactRef,
    ArtifactRoot,
    EngineEvent,
    EngineRequest,
    GradeStatus,
    JudgeKind,
    RecordType,
    SchemaVersion,
    StateChange,
    StateDiff,
)
from ses.engines.fake import FakeEngine, load_fake_fixture
from ses.evaluation import build_trace
from ses.evaluation.evidence_extractor import (
    EvidenceBundle,
    evidence_json_bytes,
    extract_evidence,
)
from ses.evaluation.judges.llm import (
    BoundJudgeEngine,
    JudgeResponseSource,
    Rubric,
    judge_llm,
)
from ses.foundation.config import LockedModel
from ses.foundation.credentials import ProviderCredentials
from ses.foundation.workspace import WorkspaceFactory

FIXTURES = Path(__file__).parents[1] / "fixtures"


def _engine(fixture_name: str) -> BoundJudgeEngine:
    fixture = load_fake_fixture(FIXTURES / "judges" / fixture_name)
    return BoundJudgeEngine.from_fake(FakeEngine(fixture))


def _evidence() -> EvidenceBundle:
    request = EngineRequest(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.ENGINE_REQUEST,
        request_id="request-1",
        prompt="Return the order.",
        allowed_tools=("preview_return", "confirm_return"),
        timeout_seconds=30,
    )
    events = tuple(
        EngineEvent.model_validate_json(line)
        for line in (FIXTURES / "stream_json" / "normal_flow.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    trace = build_trace(
        events,
        request=request,
        run_id="run-1",
        case_id="case-1",
        iteration_id="iteration-0",
    )
    diff = StateDiff(
        schema_version=SchemaVersion.V1ALPHA1,
        record_type=RecordType.STATE_DIFF,
        diff_id="diff-1",
        before_snapshot_id="before",
        after_snapshot_id="after",
        added={"/return/id": "return-1"},
        changed={
            "/refund/amount_minor": StateChange(before=0, after=1299),
            "/status": StateChange(before="shipped", after="returned"),
        },
    )
    return extract_evidence(trace, diff)


def _artifact(evidence: EvidenceBundle) -> ArtifactRef:
    payload = evidence_json_bytes(evidence)
    return ArtifactRef(
        root=ArtifactRoot.RUN,
        path="evidence/case-1.json",
        sha256=hashlib.sha256(payload).hexdigest(),
    )


def _rubric() -> Rubric:
    return Rubric(
        rubric_id="customer-response-v1",
        rubric_version="1.0.0",
        assertion_id="response-quality",
        required=True,
        criterion="The final message accurately and clearly explains the observed outcome.",
    )


@pytest.mark.parametrize(
    ("fixture_name", "expected_status"),
    [
        ("pass.json", GradeStatus.PASS),
        ("fail.json", GradeStatus.FAIL),
        ("not_evaluated.json", GradeStatus.NOT_EVALUATED),
    ],
)
def test_llm_judge_returns_canonical_assertion_results(
    fixture_name: str,
    expected_status: GradeStatus,
) -> None:
    evidence = _evidence()
    run = asyncio.run(
        judge_llm(
            _engine(fixture_name),
            rubric=_rubric(),
            evidence=evidence,
            evidence_artifact=_artifact(evidence),
        )
    )

    assert run.assertion.status is expected_status
    assert run.assertion.judge is JudgeKind.LLM
    assert run.assertion.assertion_id == "response-quality"
    if expected_status in {GradeStatus.PASS, GradeStatus.FAIL}:
        assert run.assertion.evidence
        assert all(
            ref.artifact == _artifact(evidence) for ref in run.assertion.evidence
        )
    assert run.request.allowed_tools == ()


@pytest.mark.parametrize("fixture_name", ["malformed.json", "invalid_reference.json"])
def test_llm_judge_records_protocol_failures_as_judge_error(
    fixture_name: str,
) -> None:
    evidence = _evidence()

    run = asyncio.run(
        judge_llm(
            _engine(fixture_name),
            rubric=_rubric(),
            evidence=evidence,
            evidence_artifact=_artifact(evidence),
        )
    )

    assert run.assertion.status is GradeStatus.ERROR
    assert run.assertion.evidence == ()
    assert "judge error" in run.assertion.reason.lower()


def test_llm_judge_records_all_protocol_versions_and_hashes() -> None:
    evidence = _evidence()
    run = asyncio.run(
        judge_llm(
            _engine("pass.json"),
            rubric=_rubric(),
            evidence=evidence,
            evidence_artifact=_artifact(evidence),
        )
    )

    assert run.protocol.rubric_version == "1.0.0"
    assert run.protocol.prompt_version == "rubric-prompt-v3"
    assert run.protocol.extractor_version == "evidence-extractor-v2"
    assert run.protocol.model_protocol_version == "llm-assertion-json-v2"
    assert run.protocol.judge_model_id == "ses/fixed-response-fixture"
    assert run.protocol.model_lock_version.startswith("fake-fixture:sha256:")
    assert run.protocol.response_source is JudgeResponseSource.FIXED_RESPONSE
    for digest in (
        run.protocol.rubric_sha256,
        run.protocol.prompt_sha256,
        run.protocol.extractor_sha256,
        run.protocol.model_protocol_sha256,
        run.protocol.model_config_sha256,
        run.protocol.protocol_sha256,
    ):
        assert len(digest) == 64
        int(digest, 16)


def test_metadata_reference_is_rejected_even_when_it_exists() -> None:
    evidence = _evidence()
    fixture = load_fake_fixture(FIXTURES / "judges" / "metadata_reference.json")

    run = asyncio.run(
        judge_llm(
            BoundJudgeEngine.from_fake(FakeEngine(fixture)),
            rubric=_rubric(),
            evidence=evidence,
            evidence_artifact=_artifact(evidence),
        )
    )

    assert run.assertion.status is GradeStatus.ERROR
    assert "invalid evidence reference" in run.assertion.reason


def test_fixed_response_change_changes_protocol_hash() -> None:
    evidence = _evidence()
    first = asyncio.run(
        judge_llm(
            _engine("pass.json"),
            rubric=_rubric(),
            evidence=evidence,
            evidence_artifact=_artifact(evidence),
        )
    )
    second = asyncio.run(
        judge_llm(
            _engine("fail.json"),
            rubric=_rubric(),
            evidence=evidence,
            evidence_artifact=_artifact(evidence),
        )
    )

    assert first.protocol.protocol_sha256 != second.protocol.protocol_sha256


def test_llm_judge_rejects_an_unbound_engine() -> None:
    evidence = _evidence()

    with pytest.raises(TypeError, match="BoundJudgeEngine"):
        asyncio.run(
            judge_llm(
                FakeEngine(load_fake_fixture(FIXTURES / "judges" / "pass.json")),  # type: ignore[arg-type]
                rubric=_rubric(),
                evidence=evidence,
                evidence_artifact=_artifact(evidence),
            )
        )


def test_live_binding_derives_identity_from_the_actual_claude_engine(
    tmp_path: Path,
) -> None:
    model = LockedModel(
        model_id="provider/model-v1",
        base_url="https://provider.example/",
    )
    binding = BoundJudgeEngine.production(
        model=model,
        credentials=ProviderCredentials(api_key="test-secret-value"),
        workspace_factory=WorkspaceFactory(tmp_path),
        environ={},
    )
    cleanup_root = binding.workspace.cleanup_root if binding.workspace else None

    assert binding.model.model_id == model.model_id
    assert binding.model.response_source is JudgeResponseSource.LIVE_ENGINE
    assert binding.model.model_lock_version.startswith("locked-model:sha256:")
    binding.close()
    assert cleanup_root is not None
    assert not cleanup_root.exists()
