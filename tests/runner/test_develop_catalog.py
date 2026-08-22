from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ses.runner.fake import (
    ExecutableDevelopCase,
    develop_catalog_sha256,
    load_develop_catalog,
)


def test_catalog_hash_covers_the_actual_executable_fixture() -> None:
    catalog = load_develop_catalog()
    case_id, case = next(iter(catalog.items()))
    changed_fixture = case.fixture.model_copy(
        update={"user_prompt": case.fixture.user_prompt + " Please hurry."}
    )
    changed = {
        case_id: ExecutableDevelopCase(
            changed_fixture,
            case.expected_actions,
            case.qualification_hash,
            case.manifest_data_version,
        )
    }

    assert develop_catalog_sha256(changed) != develop_catalog_sha256(catalog)


def test_catalog_rejects_data_version_drift(tmp_path: Path) -> None:
    source = Path(__file__).parents[2] / "data" / "testset" / "ticket07" / "generated"
    copied = tmp_path / "generated"
    shutil.copytree(source, copied)
    manifest_path = copied / "develop-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["cases"][0]["expected_actions"][0]["tool_name"] = "changed_tool"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="data_version"):
        load_develop_catalog(manifest_path)


def test_catalog_rejects_tampered_curation_evidence(tmp_path: Path) -> None:
    source = Path(__file__).parents[2] / "data" / "testset" / "ticket07" / "generated"
    copied = tmp_path / "generated"
    shutil.copytree(source, copied)
    curation = json.loads((copied / "curation-manifest.json").read_text())
    evidence_path = (
        copied / curation["sources"][0]["artifacts"]["source_evidence"]["path"]
    )
    evidence_path.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="artifact checksum mismatch"):
        load_develop_catalog(copied / "develop-manifest.json")


def test_pending_course_catalog_is_fixed_only() -> None:
    with pytest.raises(ValueError, match="signed human review packet"):
        load_develop_catalog(mode="live")


def test_approved_catalog_requires_asset_review_binding(tmp_path: Path) -> None:
    source = Path(__file__).parents[2] / "data" / "testset" / "ticket07" / "generated"
    copied = tmp_path / "generated"
    shutil.copytree(source, copied)
    manifest_path = copied / "develop-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["review_status"] = "human_approved"
    manifest["intended_use"] = "fixed_and_live_journey"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="asset review binding"):
        load_develop_catalog(manifest_path, mode="live")
