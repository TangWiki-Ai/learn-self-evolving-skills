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
from ses.evaluation.judges.agent import AgentJudgeEngine, judge_agent
from ses.evaluation.judges.llm import JudgeModelConfig, Rubric
from ses.foundation.workspace import WorkspaceFactory
from ses.shop import CaseEnvironment

FIXTURES = Path(__file__).parents[1] / "fixtures"
MODEL = JudgeModelConfig(
    model_id="Qwen/Qwen3.6-35B-A3B",
    model_lock_version="v1alpha1:843b2fc6",
    model_parameters={"temperature": 0, "top_p": 1},
)


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
        ("agent_pass.json", GradeStatus.PASS),
        ("agent_fail.json", GradeStatus.FAIL),
        ("agent_not_evaluated.json", GradeStatus.NOT_EVALUATED),
    ],
)
def test_agent_judge_receives_only_inline_read_only_evidence(
    fixture_name: str,
    expected_status: GradeStatus,
) -> None:
    rubric, evidence, artifact = _inputs()

    run = asyncio.run(
        judge_agent(
            AgentJudgeEngine.from_fake(
                FakeEngine(load_fake_fixture(FIXTURES / "judges" / fixture_name))
            ),
            rubric=rubric,
            evidence=evidence,
            evidence_artifact=artifact,
            model=MODEL,
        )
    )

    assert run.assertion.status is expected_status
    assert run.assertion.judge is JudgeKind.AGENT
    assert run.request.allowed_tools == ()
    assert "confirm_return" in run.request.prompt
    assert "hidden_gold" not in run.request.prompt
    assert "reference_trace" not in run.request.prompt
    assert "skill_source" not in run.request.prompt
    assert run.protocol.prompt_version == "evidence-agent-prompt-v2"


def test_agent_judge_rejects_a_shop_write_tool_attempt() -> None:
    rubric, evidence, artifact = _inputs()

    run = asyncio.run(
        judge_agent(
            AgentJudgeEngine.from_fake(
                FakeEngine(
                    load_fake_fixture(FIXTURES / "judges" / "permission_attempt.json")
                )
            ),
            rubric=rubric,
            evidence=evidence,
            evidence_artifact=artifact,
            model=MODEL,
        )
    )

    assert run.assertion.status is GradeStatus.ERROR
    assert run.assertion.evidence == ()
    assert "unauthorized tool" in run.assertion.reason.lower()


def test_agent_judge_marks_malformed_output_as_judge_error() -> None:
    rubric, evidence, artifact = _inputs()

    run = asyncio.run(
        judge_agent(
            AgentJudgeEngine.from_fake(
                FakeEngine(load_fake_fixture(FIXTURES / "judges" / "malformed.json"))
            ),
            rubric=rubric,
            evidence=evidence,
            evidence_artifact=artifact,
            model=MODEL,
        )
    )

    assert run.assertion.status is GradeStatus.ERROR
    assert "judge error" in run.assertion.reason.lower()


def test_agent_judge_rejects_an_unattested_case_engine() -> None:
    rubric, evidence, artifact = _inputs()
    raw_engine = FakeEngine(load_fake_fixture(FIXTURES / "judges" / "agent_pass.json"))

    with pytest.raises(TypeError, match="dedicated AgentJudgeEngine"):
        asyncio.run(
            judge_agent(
                raw_engine,  # type: ignore[arg-type]
                rubric=rubric,
                evidence=evidence,
                evidence_artifact=artifact,
                model=MODEL,
            )
        )


def test_agent_judge_wrapper_rejects_a_case_mcp_workspace(tmp_path: Path) -> None:
    raw_engine = FakeEngine(load_fake_fixture(FIXTURES / "judges" / "agent_pass.json"))
    case_workspace = WorkspaceFactory(tmp_path).create(
        run_id="case-run",
        case_id="case-with-shop",
        iteration_id="0",
        mcp_servers={
            "shop": {"command": "python", "args": ["-m", "ses.shop.mcp_server"]}
        },
    )

    with pytest.raises(ValueError, match="must not contain MCP"):
        AgentJudgeEngine(raw_engine, case_workspace, _validated=True)


def test_tool_attempt_cannot_change_shop_snapshot() -> None:
    rubric, evidence, artifact = _inputs()
    shop = CaseEnvironment()
    before = shop.snapshot()
    isolated = AgentJudgeEngine.from_fake(
        FakeEngine(load_fake_fixture(FIXTURES / "judges" / "permission_attempt.json"))
    )
    assert isolated.workspace.mcp_config is None
    assert tuple(isolated.workspace.root.iterdir()) == ()

    run = asyncio.run(
        judge_agent(
            isolated,
            rubric=rubric,
            evidence=evidence,
            evidence_artifact=artifact,
            model=MODEL,
        )
    )
    after = shop.snapshot()
    shop.close()

    assert run.assertion.status is GradeStatus.ERROR
    assert before.model_copy(update={"snapshot_id": after.snapshot_id}) == after
