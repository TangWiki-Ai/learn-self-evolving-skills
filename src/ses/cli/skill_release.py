"""CLI adapter for Registry-current accepted Skill packaging."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from ses.evolution.registry import RegistryError, SkillRegistry
from ses.shopping.profile import load_shopping_profile
from ses.shopping.registry import open_shopping_registry
from ses.skills.release import AcceptedSkillReleaseError, package_current_accepted


def package_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ses skill package",
        description="Package only the Registry current accepted Skill.",
    )
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--experiment-root", type=Path)
    parser.add_argument("--current-accepted", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def package_main(argv: Sequence[str]) -> int:
    args = package_parser().parse_args(argv)
    try:
        if (args.profile is None) != (args.experiment_root is None):
            raise ValueError("--profile and --experiment-root must be used together")
        expected_profile_sha256: str | None = None
        if args.profile is not None:
            profile = load_shopping_profile(args.profile)
            if profile.profile.mode != "fixed":
                raise ValueError("live shopping packaging is no_go")
            workspace = args.experiment_root.resolve(strict=True)
            if args.registry.resolve(strict=True) != workspace / "registry":
                raise ValueError("shopping Registry must be inside its experiment root")
            registry = open_shopping_registry(args.registry)
            expected_profile_sha256 = profile.profile_sha256
        else:
            registry = SkillRegistry(args.registry)
            workspace = registry.root.parent
        final_receipt = workspace / "final" / "capstone-final-receipt.json"
        release = package_current_accepted(
            workspace_root=workspace,
            registry=registry,
            capstone_final_receipt=final_receipt,
            output=args.output,
            released_at=datetime.now(UTC),
            expected_profile_sha256=expected_profile_sha256,
        )
    except (AcceptedSkillReleaseError, OSError, RegistryError, ValueError) as exc:
        print(f"skill_package_error:{type(exc).__name__}", file=sys.stderr)
        return 1
    payload = release.model_dump(mode="json")
    if args.as_json:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    else:
        print(f"release_id={release.release_id}")
        print(f"accepted_skill_sha256={release.accepted_skill_sha256}")
        print(f"release_manifest={args.output / 'release-manifest.json'}")
    return 0


__all__ = ["package_main", "package_parser"]
