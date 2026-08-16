#!/usr/bin/env python3
"""Compatibility entrypoint for the package-owned doctor."""

from ses.foundation.doctor import (
    ABCD_COMMIT,
    DEFAULT_MODEL,
    MCP_SERVER_NAME,
    MCP_TOOL_NAME,
    PING_RESULT,
    PING_VALUE,
    SILICONFLOW_BASE_URL,
    STATE_COMMIT,
    TAU2_COMMIT,
    CheckResult,
    SmokeError,
    StreamEvidence,
    build_claude_command,
    build_claude_env,
    check_claude,
    check_claude_isolation,
    check_local_data,
    check_python,
    main,
    parse_stream_json,
    redact,
    run_checks,
    run_doctor,
)

__all__ = [
    "ABCD_COMMIT",
    "DEFAULT_MODEL",
    "MCP_SERVER_NAME",
    "MCP_TOOL_NAME",
    "PING_RESULT",
    "PING_VALUE",
    "SILICONFLOW_BASE_URL",
    "STATE_COMMIT",
    "TAU2_COMMIT",
    "CheckResult",
    "SmokeError",
    "StreamEvidence",
    "build_claude_command",
    "build_claude_env",
    "check_claude",
    "check_claude_isolation",
    "check_local_data",
    "check_python",
    "main",
    "parse_stream_json",
    "redact",
    "run_checks",
    "run_doctor",
]


if __name__ == "__main__":
    raise SystemExit(main())
