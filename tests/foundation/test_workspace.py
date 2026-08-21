from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from ses.foundation.workspace import WorkspaceError, WorkspaceFactory


def test_default_workspace_is_outside_repo_and_cases_do_not_share_a_parent(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "gold.json").write_text("hidden gold", encoding="utf-8")
    personal_claude = tmp_path / "home" / ".claude"
    personal_claude.mkdir(parents=True)
    (personal_claude / "settings.json").write_text("private", encoding="utf-8")

    first = WorkspaceFactory().create(
        run_id="run", case_id="case-1", iteration_id="iteration"
    )
    second = WorkspaceFactory().create(
        run_id="run", case_id="case-2", iteration_id="iteration"
    )
    assert first.cleanup_root is not None
    assert second.cleanup_root is not None
    first_cleanup = first.cleanup_root
    second_cleanup = second.cleanup_root
    request.addfinalizer(lambda: shutil.rmtree(first_cleanup, ignore_errors=True))
    request.addfinalizer(lambda: shutil.rmtree(second_cleanup, ignore_errors=True))

    temp_root = Path(tempfile.gettempdir()).resolve()
    assert temp_root in first.root.resolve().parents
    assert repository.resolve() not in first.root.resolve().parents
    assert Path.cwd().resolve() not in first.root.resolve().parents
    assert first.root.parent != second.root.parent
    assert not (first.root / "../gold.json").resolve().is_file()
    assert not (first.root / f"../{second.cleanup_root.name}").resolve().exists()
    assert not (first.root / "../.claude/settings.json").resolve().is_file()


def test_each_case_gets_unique_allowlist_only_workspace(tmp_path: Path) -> None:
    personal = tmp_path / "personal"
    personal.mkdir()
    (personal / "settings.json").write_text("private settings", encoding="utf-8")
    allowed = tmp_path / "task.txt"
    allowed.write_text("public task", encoding="utf-8")
    skill = tmp_path / "SKILL.md"
    skill.write_text("course skill", encoding="utf-8")
    factory = WorkspaceFactory(tmp_path / "workspaces")

    first = factory.create(
        run_id="run-1",
        case_id="case-1",
        iteration_id="iteration-1",
        files=((allowed, "inputs/task.txt"),),
        skill_files=((skill, "return-helper/SKILL.md"),),
    )
    second = factory.create(
        run_id="run-1",
        case_id="case-1",
        iteration_id="iteration-1",
    )

    assert first.root != second.root
    assert first.claude_config_dir != second.claude_config_dir
    assert (first.root / "inputs/task.txt").read_text() == "public task"
    assert (first.root / ".claude/skills/return-helper/SKILL.md").is_file()
    visible = {path.name for path in first.root.rglob("*")}
    assert "settings.json" not in visible
    assert "memory" not in visible


def test_mcp_config_cannot_receive_credentials(tmp_path: Path) -> None:
    workspace = WorkspaceFactory(tmp_path / "workspaces").create(
        run_id="run",
        case_id="case",
        iteration_id="iteration",
        mcp_servers={
            "shop": {
                "command": "python",
                "args": ["server.py"],
                "env": {
                    "SHOP_FIXTURE": "case.json",
                    "SILICONFLOW_API_KEY": "must-not-survive",
                    "CHATANYWHERE_API_KEY": "chatanywhere-must-not-survive",
                    "SHOP_API_KEY": "shop-secret-must-not-survive",
                },
            }
        },
    )

    assert workspace.mcp_config is not None
    rendered = workspace.mcp_config.read_text(encoding="utf-8")
    config = json.loads(rendered)
    env = config["mcpServers"]["shop"]["env"]
    assert "must-not-survive" not in rendered
    assert "chatanywhere-must-not-survive" not in rendered
    assert "shop-secret-must-not-survive" not in rendered
    assert "SHOP_API_KEY" not in env
    assert env["SILICONFLOW_API_KEY"] == ""
    assert env["CHATANYWHERE_API_KEY"] == ""
    assert env["ANTHROPIC_API_KEY"] == ""
    assert env["SHOP_FIXTURE"] == "case.json"


@pytest.mark.parametrize("destination", ["../gold.json", "/tmp/gold.json", ""])
def test_workspace_rejects_unsafe_destinations(
    tmp_path: Path, destination: str
) -> None:
    source = tmp_path / "input"
    source.write_text("value", encoding="utf-8")

    with pytest.raises(WorkspaceError):
        WorkspaceFactory(tmp_path / "workspaces").create(
            run_id="run",
            case_id="case",
            iteration_id="iteration",
            files=((source, destination),),
        )
