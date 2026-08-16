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
from ses.evaluation.judges.agent import judge_agent
from ses.evaluation.judges.llm import Rubric

FIXTURES = Path(__file__).parents[1] / "fixtures"


def _inputs() -> tuple[Rubric, EvidenceBundle, ArtifactRef]:
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
    evidence = extract_evidence(trace, diff)
    artifact = ArtifactRef(
        root=ArtifactRoot.RUN,
        path="evidence/case-1.json",
        sha256=hashlib.sha256(evidence_json_bytes(evidence)).hexdigest(),
    )
    rubric = Rubric(
        rubric_id="customer-response-v1",
        rubric_version="1.0.0",
        assertion_id="response-quality",
        required=True,
        criterion="The final message accurately and clearly explains the observed outcome.",
    )
    return rubric, evidence, artifact


@pytest.mark.parametrize(
    ("fixture_name", "expected_status"),
    [
        ("pass.json", GradeStatus.PASS),
        ("fail.json", GradeStatus.FAIL),
        ("not_evaluated.json", GradeStatus.NOT_EVALUATED),
    ],
)
def test_agent_judge_receives_only_inline_read_only_evidence(
    fixture_name: str,
    expected_status: GradeStatus,
) -> None:
    rubric, evidence, artifact = _inputs()

    run = asyncio.run(
        judge_agent(
            FakeEngine(load_fake_fixture(FIXTURES / "judges" / fixture_name)),
            rubric=rubric,
            evidence=evidence,
            evidence_artifact=artifact,
        )
    )

    assert run.assertion.status is expected_status
    assert run.assertion.judge is JudgeKind.AGENT
    assert run.request.allowed_tools == ()
    assert "confirm_return" in run.request.prompt
    assert "hidden_gold" not in run.request.prompt
    assert "reference_trace" not in run.request.prompt
    assert "skill_source" not in run.request.prompt
    assert run.protocol.prompt_version == "evidence-agent-prompt-v1"


def test_agent_judge_rejects_a_shop_write_tool_attempt() -> None:
    rubric, evidence, artifact = _inputs()

    run = asyncio.run(
        judge_agent(
            FakeEngine(
                load_fake_fixture(FIXTURES / "judges" / "permission_attempt.json")
            ),
            rubric=rubric,
            evidence=evidence,
            evidence_artifact=artifact,
        )
    )

    assert run.assertion.status is GradeStatus.ERROR
    assert run.assertion.evidence == ()
    assert "unauthorized tool" in run.assertion.reason.lower()


def test_agent_judge_marks_malformed_output_as_judge_error() -> None:
    rubric, evidence, artifact = _inputs()

    run = asyncio.run(
        judge_agent(
            FakeEngine(load_fake_fixture(FIXTURES / "judges" / "malformed.json")),
            rubric=rubric,
            evidence=evidence,
            evidence_artifact=artifact,
        )
    )

    assert run.assertion.status is GradeStatus.ERROR
    assert "judge error" in run.assertion.reason.lower()
