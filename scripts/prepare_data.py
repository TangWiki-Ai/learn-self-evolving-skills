#!/usr/bin/env python3
"""Verify, acquire, and mine pinned benchmark data into candidate artifacts."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from ses.testset.acquisition import (  # noqa: E402
    AcquisitionError,
    acquire_full_manifest,
)
from ses.testset.cluster import (  # noqa: E402
    ClusterDependencyError,
    SklearnTfidfClusterAdapter,
)
from ses.testset.manifest import (  # noqa: E402
    ManifestDriftError,
    load_manifest,
    verify_manifest_files,
)
from ses.testset.pipeline import (  # noqa: E402
    MiningBundle,
    MiningConfig,
    SourceCountDriftError,
    mine_candidates,
    write_mining_bundle_atomic,
)
from ses.testset.profiles import (  # noqa: E402
    expected_counts_for_profile,
    load_mining_inputs,
)
from ses.testset.sources import SourceShapeError  # noqa: E402
from ses.testset.stratify import (  # noqa: E402
    CandidateCapacityError,
    SklearnCosineStratifyAdapter,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Mine auditable candidates from pinned benchmark and role-playing data."
        )
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=REPOSITORY_ROOT / "data" / "upstream" / "manifest.json",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=REPOSITORY_ROOT / "data" / "upstream",
        help="Root used for committed fixtures and ignored full downloads.",
    )
    parser.add_argument("--profile", choices=("fixture", "full"), default="fixture")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--download-full",
        action="store_true",
        help="Explicitly fetch every pinned full-profile asset.",
    )
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help="Required with --download-full; never implied by another option.",
    )
    parser.add_argument(
        "--download-only",
        action="store_true",
        help="Stop after verified full assets are atomically installed.",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Verify the manifest and committed fixture checksums, then stop.",
    )
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--clusters", type=int, default=12)
    parser.add_argument("--candidate-count", type=int)
    parser.add_argument("--seed", type=int, default=0)
    return parser


def _summary(bundle: MiningBundle, output: Path) -> str:
    funnel = bundle.funnel
    return json.dumps(
        {
            "status": "ok",
            "profile": bundle.profile,
            "output": str(output),
            "funnel": asdict(funnel),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.verify_only and (
        args.download_full or args.allow_network or args.download_only
    ):
        parser.error("--verify-only cannot be combined with download options")
    if args.download_full and not args.allow_network:
        parser.error("--download-full requires explicit --allow-network")
    if args.allow_network and not args.download_full:
        parser.error("--allow-network is only valid with --download-full")
    if args.download_only and not args.download_full:
        parser.error("--download-only requires --download-full")
    if args.attempts < 1:
        parser.error("--attempts must be at least one")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.clusters < 1:
        parser.error("--clusters must be positive")

    try:
        manifest = load_manifest(args.manifest)
        verify_manifest_files(manifest, args.data_root)
        if args.download_full:
            acquired = acquire_full_manifest(
                manifest,
                args.data_root,
                allow_network=True,
                attempts=args.attempts,
                timeout=args.timeout,
            )
            if args.download_only:
                print(
                    json.dumps(
                        {
                            "status": "ok",
                            "verified_full_assets": len(acquired),
                            "data_root": str(args.data_root),
                        },
                        sort_keys=True,
                    )
                )
                return 0
        if args.verify_only:
            print(
                json.dumps(
                    {
                        "status": "ok",
                        "verified_fixture_files": sum(
                            len(source.fixture_files) for source in manifest.sources
                        ),
                        "network_used": False,
                    },
                    sort_keys=True,
                )
            )
            return 0

        inputs = load_mining_inputs(
            manifest,
            args.data_root,
            profile=args.profile,
        )
        bundle = mine_candidates(
            inputs,
            cluster_adapter=SklearnTfidfClusterAdapter(
                n_clusters=args.clusters,
                random_state=args.seed,
            ),
            stratify_adapter=SklearnCosineStratifyAdapter(),
            config=MiningConfig(
                candidate_count=args.candidate_count,
                seed=args.seed,
            ),
            expected_counts=expected_counts_for_profile(args.profile),
        )
        output = args.output or args.data_root / "generated" / args.profile
        write_mining_bundle_atomic(bundle, output)
        print(_summary(bundle, output))
        return 0
    except (
        AcquisitionError,
        CandidateCapacityError,
        ClusterDependencyError,
        ManifestDriftError,
        SourceCountDriftError,
        SourceShapeError,
        ValueError,
    ) as exc:
        print(f"prepare_data: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
