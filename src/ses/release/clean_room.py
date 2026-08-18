"""Execute documented lesson shell blocks in a fresh temporary repository copy."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tarfile
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from ses.foundation.credentials import is_sensitive_name
from ses.release.validator import (
    DocumentedCommand,
    _documented_commands,
    _lesson_dirs,
    _logical_commands,
)

_NETWORK_MARKERS = ("--allow-network", "--download-full")
_LIVE_MARKERS = ("--curation-mode live", "--mode live")


@dataclass(frozen=True, slots=True)
class DocumentedShellBlock:
    """One fenced shell block; all contained commands share one shell process."""

    lesson: int
    readme: str
    start_line: int
    script: str
    commands: tuple[DocumentedCommand, ...]

    @property
    def block_id(self) -> str:
        digest = hashlib.sha256(self.script.encode()).hexdigest()[:12]
        return f"lesson-{self.lesson:02d}:block-{self.start_line}:{digest}"


def _fenced_ranges(text: str) -> tuple[tuple[int, int, str], ...]:
    ranges: list[tuple[int, int, str]] = []
    start: int | None = None
    lines: list[str] = []
    for line_number, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if start is None:
            if stripped in {"```bash", "```sh", "```shell"}:
                start = line_number + 1
                lines = []
            continue
        if stripped == "```":
            ranges.append((start, line_number, "\n".join(lines) + "\n"))
            start = None
            lines = []
        else:
            lines.append(raw)
    return tuple(ranges)


def documented_shell_blocks(root: Path) -> tuple[DocumentedShellBlock, ...]:
    """Map documented logical commands back to their fenced shell sessions."""

    lessons = _lesson_dirs(root)
    all_commands = _documented_commands(root, lessons)
    blocks: list[DocumentedShellBlock] = []
    assigned: set[str] = set()
    readmes = [(0, root / "README.md"), *sorted(lessons.items())]
    for lesson_number, readme_or_lesson in readmes:
        readme_path = (
            readme_or_lesson if lesson_number == 0 else readme_or_lesson / "README.md"
        )
        text = readme_path.read_text(encoding="utf-8")
        readme = readme_path.relative_to(root).as_posix()
        lesson_commands = _logical_commands(
            text,
            readme=readme,
            lesson=lesson_number,
        )
        for start, end, script in _fenced_ranges(text):
            commands = tuple(
                command for command in lesson_commands if start <= command.line < end
            )
            if not commands:
                continue
            blocks.append(
                DocumentedShellBlock(
                    lesson=lesson_number,
                    readme=readme,
                    start_line=start,
                    script=script,
                    commands=commands,
                )
            )
            assigned.update(command.command_id for command in commands)

    for command in all_commands:
        if command.command_id in assigned:
            continue
        blocks.append(
            DocumentedShellBlock(
                lesson=command.lesson,
                readme=command.readme,
                start_line=command.line,
                script=command.command + "\n",
                commands=(command,),
            )
        )
    return tuple(sorted(blocks, key=lambda item: (item.lesson, item.start_line)))


def _safe_member_path(name: str) -> PurePosixPath:
    if not name or "\\" in name or "\0" in name:
        raise ValueError("tracked archive contains an unsafe path")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("tracked archive contains an unsafe path")
    return path


def _extract_tracked_archive(
    stream: BinaryIO,
    workspace: Path,
    *,
    expected_files: frozenset[str],
) -> None:
    """Extract regular HEAD files after validating the complete tar inventory."""

    with tarfile.open(fileobj=stream, mode="r:") as archive:
        members = archive.getmembers()
        seen: set[str] = set()
        files: set[str] = set()
        for member in members:
            relative = _safe_member_path(member.name).as_posix()
            if relative in seen:
                raise ValueError("tracked archive contains a duplicate path")
            seen.add(relative)
            if member.isfile():
                files.add(relative)
            elif not member.isdir():
                raise ValueError("tracked archive contains a non-regular file")
        if files != expected_files:
            raise ValueError("tracked archive inventory differs from HEAD")

        directory_modes: list[tuple[Path, int]] = []
        for member in members:
            relative_path = _safe_member_path(member.name)
            destination = workspace.joinpath(*relative_path.parts)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                directory_modes.append((destination, member.mode & 0o777))
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise ValueError("tracked archive regular file has no payload")
            with source, destination.open("wb") as output:
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)
            destination.chmod(member.mode & 0o777)
        for directory, mode in reversed(directory_modes):
            directory.chmod(mode)


def _materialize_head(source_root: Path, workspace: Path) -> None:
    """Create an exact regular-file projection of the committed HEAD tree."""

    inventory = subprocess.run(
        ["git", "ls-tree", "-r", "-z", "--name-only", "HEAD"],
        cwd=source_root,
        capture_output=True,
        check=True,
    ).stdout
    try:
        expected_files = frozenset(
            item.decode("utf-8") for item in inventory.split(b"\0") if item
        )
    except UnicodeDecodeError as exc:
        raise ValueError("tracked HEAD contains a non-UTF-8 path") from exc

    with tempfile.TemporaryFile(mode="w+b") as stream:
        archived = subprocess.run(
            ["git", "archive", "--format=tar", "HEAD"],
            cwd=source_root,
            stdout=stream,
            stderr=subprocess.PIPE,
            check=False,
        )
        if archived.returncode != 0:
            raise RuntimeError("cannot materialize tracked HEAD archive")
        stream.flush()
        stream.seek(0)
        _extract_tracked_archive(
            stream,
            workspace,
            expected_files=expected_files,
        )


def _safe_environment(source: Mapping[str, str]) -> dict[str, str]:
    allowed = {
        "LANG",
        "LC_ALL",
        "PATH",
        "SSL_CERT_FILE",
        "TMPDIR",
        "UV_CACHE_DIR",
    }
    environment = {
        name: value
        for name, value in source.items()
        if name in allowed and not is_sensitive_name(name)
    }
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def _git_source(root: Path) -> tuple[str, bool]:
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return revision, not bool(status.strip())


def _output_digest(completed: subprocess.CompletedProcess[str]) -> dict[str, str]:
    return {
        "stdout_sha256": hashlib.sha256(completed.stdout.encode()).hexdigest(),
        "stderr_sha256": hashlib.sha256(completed.stderr.encode()).hexdigest(),
    }


def _skip_reason(block: DocumentedShellBlock) -> str | None:
    normalized = " ".join(block.script.split())
    if any(marker in normalized for marker in _LIVE_MARKERS) or re.search(
        r"(?:^|\s)--live(?:\s|$)", normalized
    ):
        return "live Provider or human-gated command was not authorized in clean-room"
    if any(marker in normalized for marker in _NETWORK_MARKERS):
        return "network acquisition was not rerun; pinned local assets were verified separately"
    if "--profile full" in normalized:
        return "full pinned assets are not committed; verified via two explicit full bundles"
    return None


def _command_records(
    block: DocumentedShellBlock,
    *,
    status: str,
    exit_code: int | None,
    reason: str | None = None,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for command in block.commands:
        record: dict[str, object] = {
            "block_id": block.block_id,
            "command_id": command.command_id,
            "command_sha256": command.sha256,
            "exit_code": exit_code,
            "line": command.line,
            "readme": command.readme,
            "status": status,
        }
        if reason is not None:
            record["reason"] = reason
        records.append(record)
    return records


def run_clean_room(
    source_root: Path,
    workspace: Path,
    *,
    allow_dirty_source: bool = False,
    environment: Mapping[str, str] | None = None,
    shell: str = "/bin/sh",
) -> dict[str, object]:
    """Materialize committed HEAD and execute README blocks without credentials."""

    source_root = source_root.resolve(strict=True)
    workspace = workspace.resolve(strict=False)
    if workspace.exists() and any(workspace.iterdir()):
        raise FileExistsError(f"clean-room workspace is not empty: {workspace}")
    revision, source_clean = _git_source(source_root)
    if not source_clean and not allow_dirty_source:
        raise RuntimeError("source worktree is dirty; commit before release evidence")
    workspace.mkdir(parents=True, exist_ok=True)
    _materialize_head(source_root, workspace)

    clean_environment = _safe_environment(environment or os.environ)
    clean_environment.setdefault("UV_CACHE_DIR", str(workspace / ".uv-cache"))
    sync = subprocess.run(
        ["uv", "sync", "--all-extras", "--locked"],
        cwd=workspace,
        env=clean_environment,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    records: list[dict[str, object]] = []
    block_results: list[dict[str, object]] = []
    if sync.returncode == 0:
        for block in documented_shell_blocks(workspace):
            reason = _skip_reason(block)
            if reason is not None:
                records.extend(
                    _command_records(
                        block,
                        status="deviation",
                        exit_code=None,
                        reason=reason,
                    )
                )
                block_results.append(
                    {
                        "block_id": block.block_id,
                        "exit_code": None,
                        "reason": reason,
                        "status": "deviation",
                    }
                )
                continue
            completed = subprocess.run(
                [shell, "-eu", "-c", block.script],
                cwd=workspace,
                env=clean_environment,
                capture_output=True,
                text=True,
                timeout=900,
                check=False,
            )
            block_status = "passed" if completed.returncode == 0 else "failed"
            records.extend(
                _command_records(
                    block,
                    status=block_status,
                    exit_code=completed.returncode,
                )
            )
            block_results.append(
                {
                    "block_id": block.block_id,
                    "exit_code": completed.returncode,
                    "status": block_status,
                    **_output_digest(completed),
                }
            )

    return {
        "schema_version": "v1alpha1",
        "record_type": "clean_room_command_evidence",
        "environment_kind": "fresh_temporary_copy",
        "repository_commit": revision,
        "source_clean": source_clean,
        "source_materialization": "git_archive_head_regular_files",
        "shell_grouping": "readme_fenced_blocks",
        "credential_environment_names": [],
        "locked_sync": {
            "command": "uv sync --all-extras --locked",
            "status": "passed" if sync.returncode == 0 else "failed",
            "exit_code": sync.returncode,
            **_output_digest(sync),
        },
        "commands": records,
        "blocks": block_results,
    }


def write_evidence(path: Path, payload: Mapping[str, object]) -> None:
    """Write canonical command evidence after all subprocesses finish."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )


def evidence_exit_code(payload: Mapping[str, object]) -> int:
    """Return 1 for failures, 2 for deviations, and 0 for a complete pass."""

    locked_sync = payload.get("locked_sync")
    if not isinstance(locked_sync, Mapping) or locked_sync.get("status") != "passed":
        return 1
    commands = payload.get("commands")
    if not isinstance(commands, Sequence):
        return 1
    statuses = {
        record.get("status") for record in commands if isinstance(record, Mapping)
    }
    if "failed" in statuses:
        return 1
    if "deviation" in statuses or payload.get("source_clean") is not True:
        return 2
    return 0


__all__ = [
    "DocumentedShellBlock",
    "documented_shell_blocks",
    "evidence_exit_code",
    "run_clean_room",
    "write_evidence",
]
