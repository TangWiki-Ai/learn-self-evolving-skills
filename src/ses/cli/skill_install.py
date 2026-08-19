"""Install a packaged Skill into an agent workspace."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from ses.skills.installer import SkillInstallError
from ses.skills.shopping import (
    SHOPPING_ASSISTANT_NAME,
    install_shopping_assistant_skill,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ses skill-install",
        description="Install a packaged Skill into a workspace.",
    )
    parser.add_argument("name", choices=(SHOPPING_ASSISTANT_NAME,))
    parser.add_argument(
        "--destination",
        type=Path,
        help=("Empty destination directory. Defaults to .claude/skills/<name>."),
    )
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    destination = args.destination or Path(".claude/skills") / args.name
    try:
        result = install_shopping_assistant_skill(destination)
    except (OSError, SkillInstallError, ValueError) as exc:
        print(f"skill_install_error: {exc}", file=sys.stderr)
        return 1
    payload = {
        "destination": result.destination.as_posix(),
        "installed_files": list(result.installed_files),
        "name": result.name,
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
