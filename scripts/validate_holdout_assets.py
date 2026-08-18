"""Validate locked holdout checksums, privacy, provenance, and split isolation."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from ses.testset.holdout import validate_holdout_bundle


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--creator-protected-manifest", type=Path, required=True)
    parser.add_argument("--creator-seed-manifest", type=Path, required=True)
    parser.add_argument("--develop-manifest", type=Path, required=True)
    parser.add_argument("--candidate-seeds", type=Path, required=True)
    parser.add_argument(
        "--archive",
        type=Path,
        help="optional pinned archive for a full source-to-artifact check",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    result = validate_holdout_bundle(
        bundle_root=args.bundle,
        creator_protected_manifest=args.creator_protected_manifest,
        creator_seed_manifest=args.creator_seed_manifest,
        develop_manifest=args.develop_manifest,
        candidate_seeds=args.candidate_seeds,
        archive_path=args.archive,
    )
    print(json.dumps(asdict(result), sort_keys=True))


if __name__ == "__main__":
    main()
