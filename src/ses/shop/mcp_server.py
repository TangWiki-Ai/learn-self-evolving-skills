"""Minimal stdio MCP server for the pinned deterministic shop case."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

from ses.contracts import ToolResult, ToolResultStatus
from ses.shop.artifacts import SnapshotArtifactWriter
from ses.shop.environment import CASE_ID, CaseEnvironment
from ses.shop.fixture import PINNED_CASE_FIXTURE, ReturnCaseFixture


def _send(payload: Mapping[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _result(request_id: object, result: Mapping[str, object]) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "result": result})


def _error(request_id: object, code: int, message: str) -> None:
    _send(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }
    )


def _mcp_tool_result(tool_result: ToolResult) -> dict[str, object]:
    structured = tool_result.model_dump(mode="json")
    result: dict[str, object] = {
        "content": [
            {
                "type": "text",
                "text": json.dumps(structured, sort_keys=True, separators=(",", ":")),
            }
        ],
        "structuredContent": structured,
    }
    if tool_result.status is ToolResultStatus.ERROR:
        result["isError"] = True
    return result


def _handle(
    environment: CaseEnvironment,
    message: Mapping[str, object],
    artifact_writer: SnapshotArtifactWriter | None,
) -> None:
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params", {})

    if method == "initialize":
        supplied = (
            params.get("protocolVersion") if isinstance(params, Mapping) else None
        )
        protocol_version = supplied if isinstance(supplied, str) else "2025-06-18"
        _result(
            request_id,
            {
                "protocolVersion": protocol_version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "ses-shop", "version": "0.1.0"},
            },
        )
        return
    if method == "notifications/initialized":
        return
    if method == "ping":
        _result(request_id, {})
        return
    if method == "tools/list":
        _result(request_id, {"tools": list(environment.available_tools())})
        return
    if method == "tools/call":
        if not isinstance(params, Mapping):
            _result(
                request_id,
                {
                    "content": [
                        {"type": "text", "text": "tools/call params must be an object"}
                    ],
                    "isError": True,
                },
            )
            return
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str):
            _result(
                request_id,
                {
                    "content": [{"type": "text", "text": "tool name must be a string"}],
                    "isError": True,
                },
            )
            return
        tool_result = environment.execute(name, arguments)
        if artifact_writer is not None:
            artifact_writer.write_after(environment.snapshot())
        _result(request_id, _mcp_tool_result(tool_result))
        return
    if request_id is not None:
        _error(request_id, -32601, f"method not found: {method!r}")


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the pinned SES shop MCP server.")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--case", choices=[CASE_ID])
    source.add_argument(
        "--fixture",
        type=Path,
        help="Evaluator-provided ReturnCaseFixture JSON for an isolated run.",
    )
    parser.add_argument(
        "--artifact-root",
        type=Path,
        help=(
            "Trusted evaluator workspace. Writes only shop/before.json and "
            "shop/after.json beneath this root."
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    fixture = (
        ReturnCaseFixture.model_validate_json(args.fixture.read_text(encoding="utf-8"))
        if args.fixture is not None
        else None
    )
    environment = CaseEnvironment(PINNED_CASE_FIXTURE if fixture is None else fixture)
    artifact_writer = (
        None
        if args.artifact_root is None
        else SnapshotArtifactWriter(args.artifact_root)
    )
    if artifact_writer is not None:
        initial = environment.snapshot()
        artifact_writer.write_before(initial)
        artifact_writer.write_after(initial)
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
                if not isinstance(message, Mapping):
                    raise ValueError("message must be an object")
                _handle(environment, message, artifact_writer)
            except (json.JSONDecodeError, ValueError) as exc:
                _error(None, -32700, str(exc))
    finally:
        environment.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
