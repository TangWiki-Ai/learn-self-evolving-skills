from __future__ import annotations

import json
from pathlib import Path

from ses.skills.installer import install_skill, load_skill_manifest
from ses.skills.seeds import load_creator_seed_pack
from ses.skills.v0 import CREATOR_SAFE_TOOLS, FakeV0Creator, create_skill_v0

ROOT = Path(__file__).parents[2]


def test_fake_v0_creator_sees_only_safe_projections_and_skill_spec(
    tmp_path: Path,
) -> None:
    pack = load_creator_seed_pack(
        ROOT / "data" / "skill-v0" / "creator" / "seed-manifest.json"
    )
    creator = FakeV0Creator()

    result = create_skill_v0(
        seed_pack=pack,
        output_dir=tmp_path / "v0",
        creator=creator,
        workspace_root=tmp_path / "creator-workspaces",
    )

    assert creator.last_request is not None
    assert creator.last_request.allowed_tools == CREATOR_SAFE_TOOLS == ()
    assert len(creator.last_request.seed_files) == 9
    visible = set(creator.last_request.visible_files)
    assert visible == {"skill-spec.md"} | {
        f"seeds/seed-{index:03d}.json" for index in range(1, 10)
    }
    assert not any(
        token in path
        for path in visible
        for token in ("develop", "selection", "final", "gold", "private", "src/")
    )
    assert result.source == tmp_path / "v0"
    manifest = load_skill_manifest(result.source)
    assert manifest.source_version == (
        "state-bench:5644b1838d96bc4483da29642d058ecaa6f80f7f:creator-audit-v3"
    )
    assert manifest.content_sha256 == result.sha256
    assert manifest.provider_compatibility == ("claude-code-native",)


def test_v0_install_excludes_creator_audit_and_decoy_private_files(
    tmp_path: Path,
) -> None:
    pack = load_creator_seed_pack(
        ROOT / "data" / "skill-v0" / "creator" / "seed-manifest.json"
    )
    result = create_skill_v0(
        seed_pack=pack,
        output_dir=tmp_path / "v0",
        creator=FakeV0Creator(),
        workspace_root=tmp_path / "creator-workspaces",
    )
    (result.source / "eval" / "gold").mkdir(parents=True)
    (result.source / "eval" / "gold" / "answer.json").write_text(
        json.dumps({"decoy": True}), encoding="utf-8"
    )

    installed = install_skill(
        result.source, tmp_path / "agent" / ".claude" / "skills" / "returns"
    )

    assert installed.installed_files == (
        "SKILL.md",
        "references/return-workflow.md",
    )
    assert not (installed.destination / "eval").exists()
    assert not (installed.destination / "skill-manifest.json").exists()
