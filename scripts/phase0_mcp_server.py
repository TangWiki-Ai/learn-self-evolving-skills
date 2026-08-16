#!/usr/bin/env python3
# ruff: noqa: I001
"""Compatibility entrypoint for the package-owned doctor MCP server."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ses.foundation.phase0_mcp import main


if __name__ == "__main__":
    raise SystemExit(main())
