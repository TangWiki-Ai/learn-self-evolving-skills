#!/usr/bin/env python3
# ruff: noqa: I001
"""Compatibility entrypoint for the package-owned doctor MCP server."""

from ses.foundation.phase0_mcp import main


if __name__ == "__main__":
    raise SystemExit(main())
