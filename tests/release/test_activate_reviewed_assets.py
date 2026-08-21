# ruff: noqa: RUF001 -- Test data mirrors the Chinese review packet exactly.
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from ses.runner.fake import load_develop_catalog
from ses.skills.installer import load_skill_manifest, normalized_skill_sha256

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT_ROOT / "scripts/activate_reviewed_assets.py"
REVIEW_COMMIT = "a" * 40


def _load_script() -> Any:
    spec = importlib.util.spec_from_file_location("activate_reviewed_assets", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _packet(*, signed: bool) -> str:
    mark = "x" if signed else " "
    signature = "Tang" if signed else "________________"
    signed_at = "2026-08-22T12:00:00Z" if signed else "________________"
    return f"""# 首发集中人工复核包

- 审核人：Tang
- 审核日期（UTC）：2026-08-22
- 审核 commit：{REVIEW_COMMIT}
- 审核环境与 Provider：macOS; SiliconFlow and ChatAnywhere

## A. v0 Skill 复核

- [{mark}] 所有 v0 来源均已核对。

## B. develop 用例复核

- [{mark}] 所有 develop 用例均已核对。

## C. 资产激活决定

- [{mark}] 允许激活：A、B 的必需项均已核对，没有未处理的拒绝项。
- [ ] 暂不激活：我已写明原因。

资产复核签名：{signature}　时间（UTC）：{signed_at}

## D. 发布检查
"""


def _workspace(tmp_path: Path, *, signed: bool) -> Path:
    generated = tmp_path / "data/testset/ticket07/generated"
    generated.parent.mkdir(parents=True)
    shutil.copytree(
        PROJECT_ROOT / "data/testset/ticket07/generated",
        generated,
    )
    skill = tmp_path / "fixtures/seed/skill/v0"
    skill.parent.mkdir(parents=True)
    shutil.copytree(PROJECT_ROOT / "fixtures/seed/skill/v0", skill)
    packet = tmp_path / "docs/release/human-review-packet.md"
    packet.parent.mkdir(parents=True)
    packet.write_text(_packet(signed=signed), encoding="utf-8")
    return tmp_path


def _run(root: Path, *, confirm: bool = True) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(SCRIPT), "--root", str(root)]
    if confirm:
        command.append("--confirm-signed-asset-review")
    return subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_activation_requires_confirmation_and_signed_review(tmp_path: Path) -> None:
    root = _workspace(tmp_path, signed=False)
    catalog = root / "data/testset/ticket07/generated/develop-manifest.json"
    skill = root / "fixtures/seed/skill/v0/skill-manifest.json"
    before = (catalog.read_bytes(), skill.read_bytes())

    without_confirmation = _run(root, confirm=False)
    assert without_confirmation.returncode == 2
    unsigned = _run(root)
    assert unsigned.returncode == 1
    assert "unchecked required items" in unsigned.stderr
    assert (catalog.read_bytes(), skill.read_bytes()) == before


def test_activation_is_valid_and_idempotent(tmp_path: Path) -> None:
    root = _workspace(tmp_path, signed=True)
    catalog_path = root / "data/testset/ticket07/generated/develop-manifest.json"
    skill_root = root / "fixtures/seed/skill/v0"

    completed = _run(root)
    assert completed.returncode == 0, completed.stderr
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    assert catalog["review_status"] == "human_approved"
    assert catalog["intended_use"] == "fixed_and_live_journey"
    assert catalog["review_commit"] == REVIEW_COMMIT
    assert len(catalog["asset_review_sha256"]) == 64
    load_develop_catalog(catalog_path, mode="live")
    manifest = load_skill_manifest(skill_root)
    assert manifest.source_version.endswith(f"-approved@{REVIEW_COMMIT}")
    normalized_skill_sha256(skill_root)

    activated = (
        catalog_path.read_bytes(),
        (skill_root / "skill-manifest.json").read_bytes(),
    )
    repeated = _run(root)
    assert repeated.returncode == 0, repeated.stderr
    assert (
        catalog_path.read_bytes(),
        (skill_root / "skill-manifest.json").read_bytes(),
    ) == activated


@pytest.mark.parametrize("failure", ["stage", "replace"])
def test_pair_update_rolls_back_and_removes_temporary_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    module = _load_script()
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_bytes(b"first-original")
    second.write_bytes(b"second-original")

    if failure == "stage":
        original_stage: Any = module._stage
        calls = 0

        def fail_second_stage(path: Path, payload: bytes) -> Path:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("staging failed")
            return cast(Path, original_stage(path, payload))

        monkeypatch.setattr(module, "_stage", fail_second_stage)
    else:
        original_replace: Any = module.os.replace
        calls = 0

        def fail_second_replace(source: Path, destination: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("replace failed")
            original_replace(source, destination)

        monkeypatch.setattr(module.os, "replace", fail_second_replace)

    replace_pair: Any = module._replace_pair
    with pytest.raises(OSError):
        replace_pair(
            first,
            b"first-new",
            second,
            b"second-new",
            validate=lambda: None,
        )

    assert first.read_bytes() == b"first-original"
    assert second.read_bytes() == b"second-original"
    assert not list(tmp_path.glob("*.tmp"))
