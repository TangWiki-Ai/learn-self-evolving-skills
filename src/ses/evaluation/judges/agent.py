"""Read-only evidence Agent Judge with an explicit zero-tool capability set."""

from __future__ import annotations

from ses.contracts import ArtifactRef, JudgeKind
from ses.engines.base import Engine
from ses.evaluation.evidence_extractor import EvidenceBundle

from .llm import ModelJudgeRun, Rubric, _run_model_judge

AGENT_PROMPT_VERSION = "evidence-agent-prompt-v1"


async def judge_agent(
    engine: Engine,
    *,
    rubric: Rubric,
    evidence: EvidenceBundle,
    evidence_artifact: ArtifactRef,
    timeout_seconds: float = 30,
) -> ModelJudgeRun:
    """Judge one assertion from extracted evidence with no tool capability."""

    return await _run_model_judge(
        engine,
        judge=JudgeKind.AGENT,
        prompt_version=AGENT_PROMPT_VERSION,
        rubric=rubric,
        evidence=evidence,
        evidence_artifact=evidence_artifact,
        timeout_seconds=timeout_seconds,
    )
