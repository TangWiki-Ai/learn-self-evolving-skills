# ruff: noqa: RUF001 -- Test data mirrors the Chinese review packet exactly.
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

import ses.journey.asset_activation as activation
from ses.runner.fake import load_develop_catalog
from ses.skills.installer import load_skill_manifest, normalized_skill_sha256

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT_ROOT / "scripts/activate_reviewed_assets.py"


def _packet(*, signed: bool, review_commit: str) -> str:
    mark = "x" if signed else " "
    signature = "Tang" if signed else "________________"
    signed_at = "2026-08-22T12:00:00Z" if signed else "________________"
    return f"""# 首发集中人工复核包

- 审核人：Tang
- 审核日期（UTC）：2026-08-22
- 审核 commit：{review_commit}
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


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _workspace(tmp_path: Path, *, signed: bool) -> tuple[Path, str]:
    generated = tmp_path / "data/testset/ticket07/generated"
    generated.parent.mkdir(parents=True)
    shutil.copytree(
        PROJECT_ROOT / "data/testset/ticket07/generated",
        generated,
    )
    skill = tmp_path / "fixtures/seed/skill/v0"
    skill.parent.mkdir(parents=True)
    shutil.copytree(PROJECT_ROOT / "fixtures/seed/skill/v0", skill)
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "add", "data", "fixtures")
    _git(
        tmp_path,
        "-c",
        "user.name=Asset Review Test",
        "-c",
        "user.email=asset-review@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "reviewed assets",
    )
    review_commit = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()

    packet = tmp_path / "docs/release/human-review-packet.md"
    packet.parent.mkdir(parents=True)
    packet.write_text(
        _packet(signed=signed, review_commit=review_commit), encoding="utf-8"
    )
    return tmp_path, review_commit


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
    root, _ = _workspace(tmp_path, signed=False)
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
    root, review_commit = _workspace(tmp_path, signed=True)
    catalog_path = root / "data/testset/ticket07/generated/develop-manifest.json"
    skill_root = root / "fixtures/seed/skill/v0"

    completed = _run(root)
    assert completed.returncode == 0, completed.stderr
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    assert catalog["review_status"] == "human_approved"
    assert catalog["intended_use"] == "fixed_and_live_journey"
    assert catalog["review_commit"] == review_commit
    assert len(catalog["asset_review_sha256"]) == 64
    load_develop_catalog(catalog_path, mode="live")
    manifest = load_skill_manifest(skill_root)
    assert manifest.source_version.endswith(f"-approved@{review_commit}")
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


@pytest.mark.parametrize("asset", ["catalog", "skill"])
def test_activation_rejects_assets_changed_after_review(
    tmp_path: Path, asset: str
) -> None:
    root, _ = _workspace(tmp_path, signed=True)
    catalog = root / "data/testset/ticket07/generated/develop-manifest.json"
    skill = root / "fixtures/seed/skill/v0/skill-manifest.json"
    before = (catalog.read_bytes(), skill.read_bytes())
    target = catalog if asset == "catalog" else skill
    target.write_bytes(target.read_bytes() + b"\n")

    completed = _run(root)

    assert completed.returncode == 1
    assert "differs from the reviewed commit" in completed.stderr
    expected = list(before)
    expected[0 if asset == "catalog" else 1] += b"\n"
    assert (catalog.read_bytes(), skill.read_bytes()) == tuple(expected)


def test_activation_rejects_unknown_review_commit(tmp_path: Path) -> None:
    root, review_commit = _workspace(tmp_path, signed=True)
    packet = root / "docs/release/human-review-packet.md"
    packet.write_text(
        packet.read_text(encoding="utf-8").replace(review_commit, "a" * 40),
        encoding="utf-8",
    )
    catalog = root / "data/testset/ticket07/generated/develop-manifest.json"
    skill = root / "fixtures/seed/skill/v0/skill-manifest.json"
    before = (catalog.read_bytes(), skill.read_bytes())

    completed = _run(root)

    assert completed.returncode == 1
    assert "not available" in completed.stderr
    assert (catalog.read_bytes(), skill.read_bytes()) == before


@pytest.mark.parametrize("failure", ["stage", "replace"])
def test_pair_update_rolls_back_and_removes_temporary_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_bytes(b"first-original")
    second.write_bytes(b"second-original")

    if failure == "stage":
        original_stage: Any = activation._stage
        calls = 0

        def fail_second_stage(path: Path, payload: bytes) -> Path:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("staging failed")
            return cast(Path, original_stage(path, payload))

        monkeypatch.setattr(activation, "_stage", fail_second_stage)
    else:
        original_replace: Any = activation._replace
        calls = 0

        def fail_second_replace(source: Path, destination: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("replace failed")
            original_replace(source, destination)

        monkeypatch.setattr(activation, "_replace", fail_second_replace)

    replace_pair: Any = activation._replace_pair
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
