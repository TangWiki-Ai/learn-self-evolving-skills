"""Allowlist-only, per-case Claude workspaces."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ses.foundation.credentials import is_sensitive_name

_CREDENTIAL_NAMES = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_CUSTOM_HEADERS",
    "SILICONFLOW_API_KEY",
)


class WorkspaceError(ValueError):
    """An allowlisted workspace input is unsafe or invalid."""


@dataclass(frozen=True)
class CaseWorkspace:
    """Internal workspace paths for one isolated case execution."""

    root: Path
    claude_config_dir: Path
    mcp_config: Path | None = None
    cleanup_root: Path | None = None


def _safe_relative_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or not value.strip() or ".." in path.parts:
        raise WorkspaceError(f"unsafe workspace path: {value!r}")
    return path


class WorkspaceFactory:
    """Create unique case directories by copying only explicit inputs."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root
        if self._root is not None:
            self._root.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        *,
        run_id: str,
        case_id: str,
        iteration_id: str,
        files: Iterable[tuple[Path, str]] = (),
        skill_files: Iterable[tuple[Path, str]] = (),
        mcp_servers: Mapping[str, Mapping[str, object]] | None = None,
    ) -> CaseWorkspace:
        identity = "\0".join((run_id, case_id, iteration_id)).encode()
        prefix = "case-" + hashlib.sha256(identity).hexdigest()[:12] + "-"
        boundary = Path(
            tempfile.mkdtemp(
                prefix=prefix,
                dir=self._root,
            )
        )
        boundary.chmod(0o700)
        root = boundary / "workspace"
        root.mkdir(mode=0o700)
        config_dir = boundary / "claude-config"
        config_dir.mkdir(mode=0o700)

        for source, destination in files:
            self._copy_allowed(source, root / _safe_relative_path(destination), root)
        for source, destination in skill_files:
            relative = _safe_relative_path(destination)
            self._copy_allowed(
                source,
                root / ".claude" / "skills" / relative,
                root,
            )

        mcp_path: Path | None = None
        if mcp_servers is not None:
            scrubbed = self._scrub_mcp_servers(mcp_servers)
            mcp_path = root / "mcp.json"
            mcp_path.write_text(
                json.dumps(
                    {"mcpServers": scrubbed}, sort_keys=True, separators=(",", ":")
                ),
                encoding="utf-8",
            )
        return CaseWorkspace(
            root=root,
            claude_config_dir=config_dir,
            mcp_config=mcp_path,
            cleanup_root=boundary,
        )

    @staticmethod
    def _copy_allowed(source: Path, destination: Path, root: Path) -> None:
        if not source.is_file() or source.is_symlink():
            raise WorkspaceError(f"allowlisted input must be a regular file: {source}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        resolved_parent = destination.parent.resolve()
        if root.resolve() not in (resolved_parent, *resolved_parent.parents):
            raise WorkspaceError(f"workspace destination escapes root: {destination}")
        shutil.copyfile(source, destination, follow_symlinks=False)

    @staticmethod
    def _scrub_mcp_servers(
        servers: Mapping[str, Mapping[str, object]],
    ) -> dict[str, dict[str, object]]:
        scrubbed: dict[str, dict[str, object]] = {}
        for name, config in servers.items():
            if not name.strip():
                raise WorkspaceError("MCP server names must not be blank")
            unknown = set(config) - {"command", "args", "env"}
            if unknown:
                raise WorkspaceError(f"unknown MCP server fields: {sorted(unknown)}")
            command = config.get("command")
            args = config.get("args", [])
            env = config.get("env", {})
            if not isinstance(command, str) or not command:
                raise WorkspaceError("MCP command must be a non-empty string")
            if not isinstance(args, (list, tuple)) or not all(
                isinstance(arg, str) for arg in args
            ):
                raise WorkspaceError("MCP args must contain only strings")
            if not isinstance(env, Mapping) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in env.items()
            ):
                raise WorkspaceError("MCP env must map strings to strings")
            clean_env = {
                key: value for key, value in env.items() if not is_sensitive_name(key)
            }
            clean_env.update({key: "" for key in _CREDENTIAL_NAMES})
            scrubbed[name] = {
                "command": command,
                "args": list(args),
                "env": clean_env,
            }
        return scrubbed
