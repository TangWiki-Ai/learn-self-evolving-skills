from __future__ import annotations

import io
import subprocess
import tarfile
from pathlib import Path
from subprocess import CompletedProcess

import pytest

import ses.release.clean_room as clean_room
from ses.release.clean_room import (
    DocumentedShellBlock,
    _extract_tracked_archive,
    _materialize_head,
    _safe_environment,
    _skip_reason,
    documented_shell_blocks,
    evidence_exit_code,
    run_clean_room,
    write_evidence,
)
from ses.release.validator import (
    CheckStatus,
    DocumentedCommand,
    _check_command_evidence,
)

_ROOT = Path(__file__).resolve().parents[2]


def test_lesson_nine_commands_share_one_shell_session() -> None:
    blocks = documented_shell_blocks(_ROOT)
    registry_block = next(
        block for block in blocks if "SES_CANDIDATE_ID=" in block.script
    )
    command_text = [command.command for command in registry_block.commands]

    assert registry_block.lesson == 9
    assert any(command.startswith("SES_CANDIDATE_ID=") for command in command_text)
    assert any("registry promote" in command for command in command_text)
    assert any('"$SES_CANDIDATE_ID"' in command for command in command_text)


def test_root_readme_commands_have_unique_root_ids(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text(
        """# Root

```bash
uv sync --all-extras --locked
uv run ses doctor
```
""",
        encoding="utf-8",
    )

    blocks = documented_shell_blocks(tmp_path)

    assert len(blocks) == 1
    assert [command.command_id.split(":", 1)[0] for command in blocks[0].commands] == [
        "root",
        "root",
    ]


def test_clean_room_environment_drops_credentials() -> None:
    environment = _safe_environment(
        {
            "PATH": "/usr/bin",
            "SILICONFLOW_API_KEY": "do-not-forward",
            "UV_CACHE_DIR": "/tmp/cache",
        }
    )

    assert environment == {
        "PATH": "/usr/bin",
        "PYTHONNOUSERSITE": "1",
        "UV_CACHE_DIR": "/tmp/cache",
    }


def test_network_download_block_becomes_explicit_deviation() -> None:
    command = DocumentedCommand(
        lesson=5,
        readme="course/ch05-example/README.md",
        line=1,
        command="uv run python scripts/prepare_data.py --download-full --allow-network",
    )
    block = DocumentedShellBlock(
        lesson=5,
        readme=command.readme,
        start_line=1,
        script=command.command + "\n",
        commands=(command,),
    )

    assert _skip_reason(block) == (
        "network acquisition was not rerun; pinned local assets were verified separately"
    )


def test_uncommitted_full_asset_block_becomes_explicit_deviation() -> None:
    command = DocumentedCommand(
        lesson=5,
        readme="course/ch05-example/README.md",
        line=1,
        command="uv run python scripts/prepare_data.py --profile full",
    )
    block = DocumentedShellBlock(
        lesson=5,
        readme=command.readme,
        start_line=1,
        script=command.command + "\n",
        commands=(command,),
    )

    assert _skip_reason(block) == (
        "full pinned assets are not committed; verified via two explicit full bundles"
    )


def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        check=True,
    )


def test_head_materialization_is_exact_and_excludes_ignored_secret_bait(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init")
    (source / ".gitignore").write_text(
        ".env\n.env.*\ndata/upstream/downloads/\nruns/\nartifacts/cache/\n",
        encoding="utf-8",
    )
    (source / "README.md").write_text("committed\n", encoding="utf-8")
    script = source / "scripts/check.sh"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    _git(source, "add", ".gitignore", "README.md", "scripts/check.sh")
    _git(
        source,
        "-c",
        "user.name=Release Test",
        "-c",
        "user.email=release@example.invalid",
        "commit",
        "-m",
        "fixture",
    )

    (source / "README.md").write_text("dirty working copy\n", encoding="utf-8")
    for bait in (
        source / ".env",
        source / ".env.local",
        source / "data/upstream/downloads/full-secret.json",
        source / "runs/local.json",
        source / "artifacts/cache/provider-response.json",
    ):
        bait.parent.mkdir(parents=True, exist_ok=True)
        bait.write_text("secret-bait\n", encoding="utf-8")
    (source / "untracked.txt").write_text("local-only\n", encoding="utf-8")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _materialize_head(source, workspace)

    tracked = {
        item.decode()
        for item in _git(
            source, "ls-tree", "-r", "-z", "--name-only", "HEAD"
        ).stdout.split(b"\0")
        if item
    }
    actual = {
        path.relative_to(workspace).as_posix()
        for path in workspace.rglob("*")
        if path.is_file()
    }
    assert actual == tracked
    assert (workspace / "README.md").read_text(encoding="utf-8") == "committed\n"
    assert (workspace / "scripts/check.sh").stat().st_mode & 0o111
    for relative in (
        ".env",
        ".env.local",
        "data/upstream/downloads/full-secret.json",
        "runs/local.json",
        "artifacts/cache/provider-response.json",
        "untracked.txt",
    ):
        assert not (workspace / relative).exists()


@pytest.mark.parametrize("kind", ["escape", "symlink"])
def test_tracked_archive_rejects_unsafe_members(tmp_path: Path, kind: str) -> None:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as archive:
        if kind == "escape":
            member = tarfile.TarInfo("../escape.txt")
            data = b"escape\n"
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        else:
            member = tarfile.TarInfo("link")
            member.type = tarfile.SYMTYPE
            member.linkname = "../escape.txt"
            archive.addfile(member)
    payload.seek(0)

    with pytest.raises(ValueError, match=r"unsafe path|non-regular file"):
        _extract_tracked_archive(
            payload,
            tmp_path / "workspace",
            expected_files=frozenset(),
        )


def test_evidence_exit_code_distinguishes_failure_and_deviation() -> None:
    base: dict[str, object] = {
        "source_clean": True,
        "locked_sync": {"status": "passed"},
        "commands": [{"status": "passed"}],
    }
    assert evidence_exit_code(base) == 0

    base["commands"] = [{"status": "deviation"}]
    assert evidence_exit_code(base) == 2

    base["commands"] = [{"status": "failed"}]
    assert evidence_exit_code(base) == 1


def test_runner_evidence_keys_duplicate_command_text_by_document_location(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    workspace = tmp_path / "workspace"
    root_command = DocumentedCommand(
        lesson=0,
        readme="README.md",
        line=4,
        command="uv run ses doctor",
    )
    lesson_command = DocumentedCommand(
        lesson=1,
        readme="course/ch01-example/README.md",
        line=8,
        command=root_command.command,
    )
    blocks = (
        DocumentedShellBlock(
            lesson=0,
            readme=root_command.readme,
            start_line=root_command.line,
            script=root_command.command + "\n",
            commands=(root_command,),
        ),
        DocumentedShellBlock(
            lesson=1,
            readme=lesson_command.readme,
            start_line=lesson_command.line,
            script=lesson_command.command + "\n",
            commands=(lesson_command,),
        ),
    )
    revision = "a" * 40

    def fake_git_source(_root: Path) -> tuple[str, bool]:
        return revision, True

    monkeypatch.setattr(clean_room, "_git_source", fake_git_source)

    def fake_materialize(
        _source: Path,
        destination: Path,
    ) -> None:
        Path(destination).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(clean_room, "_materialize_head", fake_materialize)

    def fake_blocks(_root: Path) -> tuple[DocumentedShellBlock, ...]:
        return blocks

    monkeypatch.setattr(clean_room, "documented_shell_blocks", fake_blocks)

    def fake_run(*_args: object, **_kwargs: object) -> CompletedProcess[str]:
        return CompletedProcess(
            args=[],
            returncode=0,
            stdout="ok\n",
            stderr="",
        )

    monkeypatch.setattr("ses.release.clean_room.subprocess.run", fake_run)

    payload = run_clean_room(source, workspace)
    evidence = tmp_path / "evidence.json"
    write_evidence(evidence, payload)

    check = _check_command_evidence(
        [root_command, lesson_command],
        evidence,
        expected_repository_commit=revision,
    )

    assert check.status is CheckStatus.PASS

    records = payload["commands"]
    assert isinstance(records, list)
    assert len(records) == 2
    assert all(isinstance(record, dict) for record in records)
    root_record = records[0]
    lesson_record = records[1]
    assert isinstance(root_record, dict)
    assert isinstance(lesson_record, dict)
    assert root_record["command_sha256"] == lesson_record["command_sha256"]
    assert root_record["command_id"] != lesson_record["command_id"]

    original_id = lesson_record["command_id"]
    assert isinstance(original_id, str)
    lesson_record["command_id"] = original_id.replace("lesson-01", "lesson-02")
    write_evidence(evidence, payload)
    assert (
        _check_command_evidence(
            [root_command, lesson_command],
            evidence,
            expected_repository_commit=revision,
        ).status
        is CheckStatus.FAIL
    )

    lesson_record["command_id"] = original_id
    original_hash = lesson_record["command_sha256"]
    lesson_record["command_sha256"] = "0" * 64
    write_evidence(evidence, payload)
    assert (
        _check_command_evidence(
            [root_command, lesson_command],
            evidence,
            expected_repository_commit=revision,
        ).status
        is CheckStatus.FAIL
    )

    lesson_record["command_sha256"] = original_hash
    records.append(dict(root_record))
    write_evidence(evidence, payload)
    assert (
        _check_command_evidence(
            [root_command, lesson_command],
            evidence,
            expected_repository_commit=revision,
        ).status
        is CheckStatus.FAIL
    )
