from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import phase0_check  # noqa: E402


class StreamJsonTests(unittest.TestCase):
    def test_accepts_model_mcp_and_result_events(self) -> None:
        events = [
            {"type": "system", "subtype": "init"},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": phase0_check.MCP_TOOL_NAME,
                            "input": {"value": phase0_check.PING_VALUE},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "content": phase0_check.PING_RESULT,
                        }
                    ]
                },
            },
            {"type": "result", "is_error": False},
        ]
        stdout = "\n".join(json.dumps(event) for event in events)

        evidence = phase0_check.parse_stream_json(stdout)

        self.assertEqual(evidence.event_count, 4)
        self.assertTrue(evidence.has_model_response)
        self.assertTrue(evidence.has_tool_call)
        self.assertTrue(evidence.has_tool_result)
        self.assertTrue(evidence.has_success_result)

    def test_rejects_stream_without_tool_result(self) -> None:
        events = [
            {"type": "assistant", "message": {"content": []}},
            {"type": "result", "is_error": False},
        ]
        stdout = "\n".join(json.dumps(event) for event in events)

        with self.assertRaisesRegex(phase0_check.SmokeError, "MCP tool call"):
            phase0_check.parse_stream_json(stdout)

    def test_rejects_non_json_output(self) -> None:
        with self.assertRaisesRegex(phase0_check.SmokeError, "不是有效 JSON"):
            phase0_check.parse_stream_json("not-json")


class CredentialTests(unittest.TestCase):
    def test_child_environment_overrides_global_provider_without_copying_alias(self) -> None:
        source = {
            "ANTHROPIC_AUTH_TOKEN": "old-provider-token",
            "ANTHROPIC_BASE_URL": "https://old-provider.example/",
            "SILICONFLOW_API_KEY": "new-key",
            "CLAUDE_CODE_USE_VERTEX": "1",
            "PATH": "/usr/bin",
        }

        env = phase0_check.build_claude_env(
            source, "new-key", "example-model", Path("/tmp/isolated")
        )

        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
        self.assertNotIn("SILICONFLOW_API_KEY", env)
        self.assertNotIn("CLAUDE_CODE_USE_VERTEX", env)
        self.assertEqual(env["ANTHROPIC_API_KEY"], "new-key")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], phase0_check.SILICONFLOW_BASE_URL)
        self.assertEqual(env["ANTHROPIC_MODEL"], "example-model")
        self.assertEqual(env["PATH"], "/usr/bin")

    def test_key_is_not_part_of_command_line(self) -> None:
        command = phase0_check.build_claude_command(
            "/usr/bin/claude", "example-model", Path("/tmp/mcp.json")
        )
        self.assertNotIn("secret-key", " ".join(command))

    def test_redacts_explicit_and_key_shaped_values(self) -> None:
        text = "token=exact-secret x-api-key: sk-example123456789"
        redacted = phase0_check.redact(text, ["exact-secret"])
        self.assertNotIn("exact-secret", redacted)
        self.assertNotIn("sk-example123456789", redacted)
        self.assertIn("[REDACTED]", redacted)


class McpServerTests(unittest.TestCase):
    def test_minimal_json_rpc_round_trip(self) -> None:
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18"},
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "phase0_ping",
                    "arguments": {"value": phase0_check.PING_VALUE},
                },
            },
        ]
        stdin = "\n".join(json.dumps(message) for message in messages) + "\n"

        completed = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "phase0_mcp_server.py")],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        responses = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual([response["id"] for response in responses], [1, 2, 3])
        self.assertEqual(
            responses[1]["result"]["tools"][0]["name"], "phase0_ping"
        )
        self.assertEqual(
            responses[2]["result"]["content"][0]["text"],
            phase0_check.PING_RESULT,
        )


if __name__ == "__main__":
    unittest.main()
