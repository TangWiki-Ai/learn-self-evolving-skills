"""Build the locked STATE-Bench selection and final holdout assets."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from ses.testset.holdout import (
    build_holdout_bundle,
    read_external_ranking_key,
    read_external_semantic_group_map,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive",
        type=Path,
        required=True,
        help="checksum-pinned STATE-Bench source.tar.gz",
    )
    parser.add_argument(
        "--creator-seed-manifest",
        type=Path,
        required=True,
        help="existing creator seed manifest used for source exclusion",
    )
    parser.add_argument(
        "--develop-manifest",
        type=Path,
        required=True,
        help="existing develop catalog used for source exclusion",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="new or empty output directory",
    )
    parser.add_argument(
        "--ranking-key-file",
        type=Path,
        required=True,
        help="external file containing at least 32 bytes of secret ranking key",
    )
    parser.add_argument(
        "--semantic-group-map-file",
        type=Path,
        required=True,
        help="external owner-only protected semantic-group mapping",
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    ranking_key = read_external_ranking_key(args.ranking_key_file)
    semantic_groups = read_external_semantic_group_map(args.semantic_group_map_file)
    result = build_holdout_bundle(
        archive_path=args.archive,
        creator_seed_manifest=args.creator_seed_manifest,
        develop_manifest=args.develop_manifest,
        output_root=args.output,
        ranking_key=ranking_key,
        semantic_groups=semantic_groups,
    )
    print(json.dumps(asdict(result), sort_keys=True))


if __name__ == "__main__":
    main()
