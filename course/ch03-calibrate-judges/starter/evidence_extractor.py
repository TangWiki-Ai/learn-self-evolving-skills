"""Lesson 3 starter: extract deterministic evidence before model judgment."""

from __future__ import annotations

from ses.contracts import StateDiff, Trace
from ses.evaluation.evidence_extractor import EvidenceBundle


def extract_evidence(trace: Trace, state_diff: StateDiff) -> EvidenceBundle:
    """Build state, timeline, named amount, and message evidence."""

    raise NotImplementedError("Lesson 3: implement the evidence extractor")
