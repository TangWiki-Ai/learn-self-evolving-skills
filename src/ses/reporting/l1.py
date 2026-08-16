"""Minimal L1 JSON and terminal views for one evaluated case."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from pydantic import JsonValue

from ses.contracts import ArtifactRef, CaseGrade, StateDiff, Trace
from ses.contracts.security import validate_public_data
from ses.evaluation import trace_messages, trace_tool_calls

_RUN_ID_PATTERN = re.compile(r"^run-[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CASE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


def _plain_json(value: object) -> JsonValue:
    if isinstance(value, Mapping):
        return {str(key): _plain_json(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(child) for child in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"L1 result contains a non-JSON value: {type(value).__name__}")


def build_l1_result(
    *,
    trace: Trace,
    state_diff: StateDiff,
    grade: CaseGrade,
    outcome: str,
    artifacts: Mapping[str, ArtifactRef],
) -> dict[str, JsonValue]:
    """Project canonical records into the smallest useful learner-facing view."""
    messages: list[JsonValue] = [
        {"role": "user", "message_id": None, "content": trace.request.prompt}
    ]
    messages.extend(
        {
            "role": "assistant",
            "message_id": message.message_id,
            "content": message.text,
        }
        for message in trace_messages(trace)
    )
    tool_calls: list[JsonValue] = []
    for call in trace_tool_calls(trace):
        tool_calls.append(
            {
                "tool_call_id": call.tool_call_id,
                "tool_name": call.tool_name,
                "input": _plain_json(call.arguments),
                "output": (
                    None if call.result is None else _plain_json(call.result.content)
                ),
                "is_error": None if call.result is None else call.result.is_error,
            }
        )
    usage: dict[str, JsonValue] = {
        "input_tokens": None,
        "output_tokens": None,
        "cost_amount": None,
        "cost_currency": None,
    }
    if trace.usage is not None:
        usage = cast(
            dict[str, JsonValue],
            trace.usage.model_dump(mode="json", round_trip=True),
        )
    result: dict[str, JsonValue] = {
        "schema_version": "v1alpha1",
        "record_type": "l1_case_result",
        "run_id": trace.run_id,
        "case_id": trace.case_id,
        "iteration_id": trace.iteration_id,
        "outcome": outcome,
        "messages": messages,
        "tool_calls": tool_calls,
        "state_diff": cast(
            JsonValue, state_diff.model_dump(mode="json", round_trip=True)
        ),
        "assertions": cast(
            JsonValue,
            [
                assertion.model_dump(mode="json", round_trip=True)
                for assertion in grade.assertions
            ],
        ),
        "case_grade": cast(JsonValue, grade.model_dump(mode="json", round_trip=True)),
        "usage": usage,
        "skill": {
            "version": trace.skill_version,
            "sha256": trace.skill_sha256,
        },
        "artifacts": {
            name: reference.model_dump(mode="json", round_trip=True)
            for name, reference in sorted(artifacts.items())
        },
    }
    validate_public_data(result)
    return result


def l1_json_bytes(result: Mapping[str, JsonValue]) -> bytes:
    """Serialize the L1 view deterministically without adding private fields."""
    validate_public_data(result)
    return json.dumps(
        result,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _safe_identifier(value: str, label: str) -> str:
    pattern = _RUN_ID_PATTERN if label == "run_id" else _CASE_ID_PATTERN
    if not pattern.fullmatch(value):
        raise ValueError(f"invalid {label}")
    return value


def _verify_artifacts(run_dir: Path, result: Mapping[str, JsonValue]) -> None:
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, Mapping) or not artifacts:
        raise ValueError("run result is missing artifact references")
    for value in artifacts.values():
        reference = ArtifactRef.model_validate(value)
        path = run_dir / reference.path
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise ValueError(f"run artifact is unavailable: {reference.path}") from exc
        try:
            reference.verify_bytes(payload)
        except ValueError as exc:
            raise ValueError(f"run artifact checksum failed: {reference.path}") from exc


def load_l1_result(
    output_root: Path,
    run_id: str,
    case_id: str | None = None,
) -> dict[str, JsonValue]:
    """Load one result through a run-relative path and verify its identity."""
    safe_run_id = _safe_identifier(run_id, "run_id")
    run_dir = output_root.resolve() / safe_run_id
    path = run_dir / "result.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"run result does not exist: {safe_run_id}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"run result is not valid JSON: {safe_run_id}") from exc
    if not isinstance(value, dict):
        raise ValueError("run result must be a JSON object")
    result = cast(dict[str, JsonValue], value)
    validate_public_data(result)
    if result.get("run_id") != safe_run_id:
        raise ValueError("run result identity does not match its directory")
    if case_id is not None:
        safe_case_id = _safe_identifier(case_id, "case_id")
        if result.get("case_id") != safe_case_id:
            raise ValueError("case_id does not match the run result")
    _verify_artifacts(run_dir, result)
    return result


def render_l1_text(result: Mapping[str, JsonValue]) -> str:
    """Render a compact terminal report without re-running any judge."""
    usage = result.get("usage")
    usage_map = usage if isinstance(usage, Mapping) else {}
    lines = [
        f"Run: {result.get('run_id')}",
        f"Case: {result.get('case_id')}",
        f"Outcome: {result.get('outcome')}",
        (
            "Usage: "
            f"input={usage_map.get('input_tokens')}, "
            f"output={usage_map.get('output_tokens')}, "
            f"cost={usage_map.get('cost_amount')} "
            f"{usage_map.get('cost_currency') or ''}"
        ).rstrip(),
        "",
        "Messages:",
    ]
    messages = result.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if isinstance(message, Mapping):
                lines.append(f"- {message.get('role')}: {message.get('content')}")
    lines.extend(("", "Tools:"))
    tool_calls = result.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            if isinstance(call, Mapping):
                state = "error" if call.get("is_error") else "ok"
                lines.append(f"- {call.get('tool_name')}: {state}")
    state_diff = result.get("state_diff")
    summary = state_diff.get("summary") if isinstance(state_diff, Mapping) else None
    lines.extend(("", f"StateDiff: {summary}"))
    assertions = result.get("assertions")
    if isinstance(assertions, list):
        lines.append("Assertions:")
        for assertion in assertions:
            if isinstance(assertion, Mapping):
                lines.append(
                    f"- {assertion.get('status')} {assertion.get('assertion_id')}"
                )
    return "\n".join(lines)
