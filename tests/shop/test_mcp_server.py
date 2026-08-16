from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Generator, Mapping
from contextlib import contextmanager


class _McpClient:
    def __init__(self, role: str = "agent") -> None:
        self._process = subprocess.Popen(
            [sys.executable, "-m", "ses.shop.mcp_server", "--role", role],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def request(
        self, request_id: int, method: str, params: object = None
    ) -> Mapping[str, object]:
        assert self._process.stdin is not None
        assert self._process.stdout is not None
        message: dict[str, object] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            message["params"] = params
        self._process.stdin.write(json.dumps(message) + "\n")
        self._process.stdin.flush()
        response = self._process.stdout.readline()
        assert response
        decoded = json.loads(response)
        assert isinstance(decoded, Mapping)
        return decoded

    def close(self) -> None:
        self._process.terminate()
        _, stderr = self._process.communicate(timeout=10)
        assert self._process.returncode is not None
        assert stderr == ""


@contextmanager
def _mcp_client(role: str = "agent") -> Generator[_McpClient, None, None]:
    client = _McpClient(role)
    try:
        yield client
    finally:
        client.close()


def _result(response: Mapping[str, object]) -> Mapping[str, object]:
    value = response["result"]
    assert isinstance(value, Mapping)
    return value


def _structured(response: Mapping[str, object]) -> Mapping[str, object]:
    value = _result(response)["structuredContent"]
    assert isinstance(value, Mapping)
    return value


def _mapping(value: object) -> Mapping[str, object]:
    assert isinstance(value, Mapping)
    return value


def test_mcp_lists_and_calls_only_the_pinned_case_tools() -> None:
    with _mcp_client() as client:
        initialized = _result(
            client.request(1, "initialize", {"protocolVersion": "2025-06-18"})
        )
        listed = _result(client.request(2, "tools/list"))
        tools = listed["tools"]

        assert initialized["serverInfo"] == {"name": "ses-shop", "version": "0.1.0"}
        assert isinstance(tools, list)
        tool_records = [_mapping(tool) for tool in tools]
        assert [tool["name"] for tool in tool_records] == [
            "get_order",
            "get_policies",
            "process_return",
        ]
        return_schema = _mapping(tool_records[2]["inputSchema"])
        properties = _mapping(return_schema["properties"])
        assert properties["amount_minor"] == {"type": "integer"}

        order = _structured(
            client.request(
                3,
                "tools/call",
                {"name": "get_order", "arguments": {"order_id": "ORD-6006"}},
            )
        )
        policy = _structured(
            client.request(
                4,
                "tools/call",
                {"name": "get_policies", "arguments": {"topic": "return"}},
            )
        )
        preview = _structured(
            client.request(
                5,
                "tools/call",
                {
                    "name": "process_return",
                    "arguments": {"item_id": "ITEM-9050", "reason": "defective"},
                },
            )
        )
        confirmed = _structured(
            client.request(
                6,
                "tools/call",
                {
                    "name": "process_return",
                    "arguments": {
                        "item_id": "ITEM-9050",
                        "reason": "defective",
                        "confirm": True,
                        "amount_minor": 129_900,
                    },
                },
            )
        )

        order_data = _mapping(order["data"])
        assert _mapping(order_data["order"])["order_id"] == "ORD-6006"
        assert policy["status"] == "success"
        assert _mapping(preview["data"])["status"] == "preview"
        assert _mapping(confirmed["data"])["status"] == "returned"


def test_mcp_enforces_role_permissions() -> None:
    with _mcp_client("judge") as client:
        listed = _result(client.request(1, "tools/list"))
        denied = _result(
            client.request(
                2,
                "tools/call",
                {
                    "name": "process_return",
                    "arguments": {"item_id": "ITEM-9050", "reason": "defective"},
                },
            )
        )

        assert listed["tools"] == []
        assert denied["isError"] is True
        structured = denied["structuredContent"]
        assert isinstance(structured, Mapping)
        assert structured["error_code"] == "permission_denied"
