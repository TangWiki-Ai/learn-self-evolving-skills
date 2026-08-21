# ruff: noqa: RUF001 -- The parser matches punctuation in the Chinese review packet.
"""Activate the reviewed develop catalog and v0 Skill for live validation."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ses.runner.fake import load_develop_catalog
from ses.skills.installer import load_skill_manifest, normalized_skill_sha256


class ActivationError(ValueError):
    """The review packet or release assets are not ready for activation."""


_COMMIT_RE = re.compile(r"[0-9a-fA-F]{40}")
_APPROVED_SOURCE_RE = re.compile(r"-approved@([0-9a-f]{40})$")
_ACTIVATION_HEADING = "## C. 资产激活决定"


def _completed_value(text: str, label: str) -> str:
    match = re.search(rf"^- {re.escape(label)}：(.+)$", text, re.MULTILINE)
    if match is None:
        raise ActivationError(f"review packet is missing {label}")
    value = match.group(1).strip()
    if not value or not value.strip("_ "):
        raise ActivationError(f"review packet field is incomplete: {label}")
    return value


def _review_approval(packet_path: Path) -> tuple[str, str]:
    text = packet_path.read_text(encoding="utf-8")
    heading_at = text.find(_ACTIVATION_HEADING)
    if heading_at < 0:
        raise ActivationError("review packet is missing the asset activation section")
    if "[ ]" in text[:heading_at]:
        raise ActivationError("asset review contains unchecked required items")

    _completed_value(text, "审核人")
    _completed_value(text, "审核日期（UTC）")
    review_commit = _completed_value(text, "审核 commit").lower()
    _completed_value(text, "审核环境与 Provider")
    if _COMMIT_RE.fullmatch(review_commit) is None:
        raise ActivationError("审核 commit must be a full 40-character Git SHA")

    section = text[heading_at:]
    if re.search(r"^- \[[xX]\] 允许激活：", section, re.MULTILINE) is None:
        raise ActivationError("asset activation approval is not checked")
    if re.search(r"^- \[[xX]\] 暂不激活", section, re.MULTILINE) is not None:
        raise ActivationError("review packet also selects do not activate")
    signature = re.search(
        r"^资产复核签名：(.+?)　时间（UTC）：(.+)$", section, re.MULTILINE
    )
    if signature is None or any(
        not value.strip() or not value.strip("_ ") for value in signature.groups()
    ):
        raise ActivationError("asset activation signature or time is incomplete")

    reviewed_prefix = text[: heading_at + signature.end()]
    review_sha256 = hashlib.sha256(reviewed_prefix.encode("utf-8")).hexdigest()
    return review_commit, review_sha256


def _read_object(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ActivationError(f"{label} must be a regular file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ActivationError(f"invalid {label}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ActivationError(f"{label} must contain one JSON object")
    return payload


def _canonical_json(payload: dict[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _activated_manifests(
    *,
    catalog: dict[str, Any],
    skill: dict[str, Any],
    review_commit: str,
    review_sha256: str,
) -> tuple[bytes, bytes]:
    catalog["review_status"] = "human_approved"
    catalog["intended_use"] = "fixed_and_live_journey"
    catalog["review_commit"] = review_commit
    catalog["asset_review_sha256"] = review_sha256
    version_body = dict(catalog)
    version_body.pop("data_version", None)
    catalog["data_version"] = hashlib.sha256(
        json.dumps(
            version_body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    source_version = skill.get("source_version")
    if not isinstance(source_version, str):
        raise ActivationError("v0 Skill manifest has no source_version")
    if source_version.endswith("-pending"):
        skill["source_version"] = (
            source_version.removesuffix("-pending") + f"-approved@{review_commit}"
        )
    else:
        approved = _APPROVED_SOURCE_RE.search(source_version)
        if approved is None or approved.group(1) != review_commit:
            raise ActivationError(
                "v0 Skill source is neither pending nor approved by this review commit"
            )
    return _canonical_json(catalog), _canonical_json(skill)


def _stage(path: Path, payload: bytes) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        return temporary
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _replace_pair(
    first: Path,
    first_payload: bytes,
    second: Path,
    second_payload: bytes,
    *,
    validate: Callable[[], None],
) -> None:
    original_first = first.read_bytes()
    original_second = second.read_bytes()
    staged_first: Path | None = None
    staged_second: Path | None = None
    first_replaced = False
    second_replaced = False
    try:
        staged_first = _stage(first, first_payload)
        staged_second = _stage(second, second_payload)
        os.replace(staged_first, first)
        first_replaced = True
        os.replace(staged_second, second)
        second_replaced = True
        validate()
    except BaseException:
        if first_replaced:
            restore_first = _stage(first, original_first)
            os.replace(restore_first, first)
        if second_replaced:
            restore_second = _stage(second, original_second)
            os.replace(restore_second, second)
        raise
    finally:
        if staged_first is not None:
            staged_first.unlink(missing_ok=True)
        if staged_second is not None:
            staged_second.unlink(missing_ok=True)


def activate_reviewed_assets(root: Path) -> tuple[Path, Path]:
    """Validate the signed asset review and activate its two reviewed inputs."""

    root = root.resolve(strict=True)
    packet_path = root / "docs/release/human-review-packet.md"
    catalog_path = root / "data/testset/ticket07/generated/develop-manifest.json"
    skill_path = root / "fixtures/seed/skill/v0/skill-manifest.json"
    review_commit, review_sha256 = _review_approval(packet_path)
    load_develop_catalog(catalog_path, mode="fixed")
    load_skill_manifest(skill_path.parent)
    normalized_skill_sha256(skill_path.parent)
    catalog = _read_object(catalog_path, label="develop manifest")
    skill = _read_object(skill_path, label="v0 Skill manifest")
    catalog_payload, skill_payload = _activated_manifests(
        catalog=catalog,
        skill=skill,
        review_commit=review_commit,
        review_sha256=review_sha256,
    )

    def validate_activated() -> None:
        load_develop_catalog(catalog_path, mode="live")
        manifest = load_skill_manifest(skill_path.parent)
        if _APPROVED_SOURCE_RE.search(manifest.source_version) is None:
            raise ActivationError("activated v0 Skill has no review binding")
        normalized_skill_sha256(skill_path.parent)

    _replace_pair(
        catalog_path,
        catalog_payload,
        skill_path,
        skill_payload,
        validate=validate_activated,
    )
    return catalog_path, skill_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Activate human-reviewed Journey assets for live validation."
    )
    parser.add_argument(
        "--confirm-signed-asset-review",
        action="store_true",
        help="confirm that the packet's asset review section was signed",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not args.confirm_signed_asset_review:
        print(
            "refusing activation without --confirm-signed-asset-review",
            file=sys.stderr,
        )
        return 2
    try:
        catalog_path, skill_path = activate_reviewed_assets(args.root)
    except (ActivationError, OSError) as exc:
        print(f"asset activation failed: {exc}", file=sys.stderr)
        return 1
    print(f"activated: {catalog_path}")
    print(f"activated: {skill_path}")
    print(
        "next: run both Provider live smokes, complete the release checklist, "
        "then sign the final decision"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
