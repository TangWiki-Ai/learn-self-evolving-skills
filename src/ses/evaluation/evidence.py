"""Small helpers for pointing assertions at persisted canonical records."""

from __future__ import annotations

from ses.contracts import ArtifactRef, EvidenceRef


def escape_json_pointer_token(token: str) -> str:
    """Escape one RFC 6901 JSON Pointer token."""

    return token.replace("~", "~0").replace("/", "~1")


def join_json_pointer(*tokens: str) -> str:
    """Build a JSON Pointer without accepting an already escaped token."""

    if not tokens:
        raise ValueError("a JSON Pointer needs at least one token")
    return "/" + "/".join(escape_json_pointer_token(token) for token in tokens)


def evidence_ref(artifact: ArtifactRef, pointer: str) -> EvidenceRef:
    """Create a validated evidence reference after the source was persisted."""

    return EvidenceRef(artifact=artifact, json_pointer=pointer)


def trace_event_evidence(
    artifact: ArtifactRef,
    event_index: int,
    *payload_tokens: str,
) -> EvidenceRef:
    """Point at an event or a field inside its payload."""

    if event_index < 0:
        raise ValueError("event_index must be nonnegative")
    pointer = join_json_pointer("events", str(event_index), *payload_tokens)
    return evidence_ref(artifact, pointer)


def timeline_evidence(artifact: ArtifactRef) -> EvidenceRef:
    """Point at the complete event timeline."""

    return evidence_ref(artifact, "/events")


def state_diff_evidence(
    artifact: ArtifactRef,
    bucket: str,
    path: str,
) -> EvidenceRef:
    """Point at one path in a persisted ``StateDiff`` bucket."""

    if bucket not in {"added", "removed", "changed"}:
        raise ValueError(f"unsupported StateDiff bucket: {bucket!r}")
    pointer = join_json_pointer(bucket) if not path else join_json_pointer(bucket, path)
    return evidence_ref(artifact, pointer)
