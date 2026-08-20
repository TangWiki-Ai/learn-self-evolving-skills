"""Install a packaged Skill into an agent workspace."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from ses.shopping.profile import load_shopping_profile
from ses.shopping.registry import open_shopping_registry
from ses.skills.installer import SkillInstallError
from ses.skills.release import AcceptedSkillReleaseError, install_current_accepted
from ses.skills.shopping import (
    SHOPPING_ASSISTANT_NAME,
    install_shopping_assistant_skill,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ses skill-install",
        description="Install a packaged Skill into a workspace.",
    )
    parser.add_argument("name", nargs="?", choices=(SHOPPING_ASSISTANT_NAME,))
    parser.add_argument(
        "--accepted-package",
        type=Path,
        help="Accepted release-manifest.json produced from Registry current.",
    )
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--experiment-root", type=Path)
    parser.add_argument(
        "--destination",
        type=Path,
        help=("Empty destination directory. Defaults to .claude/skills/<name>."),
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    if (args.name is None) == (args.accepted_package is None):
        print(
            "skill_install_error: choose a packaged name or --accepted-package",
            file=sys.stderr,
        )
        return 1
    accepted = args.accepted_package is not None
    destination = args.destination or (
        Path(".claude/skills") if accepted else Path(".claude/skills") / args.name
    )
    try:
        if accepted:
            assert args.accepted_package is not None
            if (args.profile is None) != (args.experiment_root is None):
                raise ValueError(
                    "--profile and --experiment-root must be used together"
                )
            registry = None
            expected_profile_sha256 = None
            if args.profile is not None:
                profile = load_shopping_profile(args.profile)
                if profile.profile.mode != "fixed":
                    raise ValueError("live shopping installation is no_go")
                workspace = args.experiment_root.resolve(strict=True)
                expected_manifest = workspace / "package" / "release-manifest.json"
                if args.accepted_package.resolve(strict=True) != expected_manifest:
                    raise ValueError(
                        "accepted shopping package must belong to its experiment"
                    )
                registry = open_shopping_registry(workspace / "registry")
                expected_profile_sha256 = profile.profile_sha256
            else:
                workspace = args.accepted_package.absolute().parent.parent
            result = install_current_accepted(
                workspace_root=workspace,
                release_manifest=args.accepted_package,
                destination=destination,
                registry=registry,
                expected_profile_sha256=expected_profile_sha256,
            )
        else:
            result = install_shopping_assistant_skill(destination)
    except (
        AcceptedSkillReleaseError,
        OSError,
        SkillInstallError,
        ValueError,
    ) as exc:
        print(f"skill_install_error: {exc}", file=sys.stderr)
        return 1
    payload = {
        "destination": result.destination.as_posix(),
        "installed_files": list(result.installed_files),
        "name": result.name,
        "source_kind": "registry_accepted" if accepted else "reference_fallback",
        "sha256": result.sha256,
        "version": result.version,
    }
    if args.as_json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(f"installed {result.name}@{result.version}")
        print(f"destination={result.destination}")
        print(f"sha256={result.sha256}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
