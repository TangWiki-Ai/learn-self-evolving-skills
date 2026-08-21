"""Doctor CLI with a fail-closed shopping profile route."""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from ses.foundation.doctor import build_parser as build_parser
from ses.foundation.doctor import main as foundation_main
from ses.shopping.profile import load_shopping_profile
from ses.shopping.source import load_shop_simulator_source_manifest


def _has_option(argv: Sequence[str], option: str) -> bool:
    return any(value == option or value.startswith(f"{option}=") for value in argv)


def _profile_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="ses doctor",
        description="Validate one locked shopping capstone profile without network.",
    )
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args(argv)
    project_root = args.project_root.resolve()
    profile_path = args.profile
    if not profile_path.is_absolute():
        profile_path = project_root / profile_path
    try:
        loaded = load_shopping_profile(profile_path)
        source = load_shop_simulator_source_manifest(
            project_root / "fixtures/seed/capstone-shopping-assistant/sources/"
            "shop-simulator-live-no-go.json"
        )
    except (OSError, TypeError, ValueError) as exc:
        print(f"[FAIL] Shopping profile: {type(exc).__name__}")
        print("\n结论: NO-GO。profile 或 source manifest 无效。")
        return 1

    profile = loaded.profile
    group_count = sum(profile.source_group_counts.values())
    slot_count = sum(profile.episode_slot_counts.values())
    print(
        f"[PASS] Shopping profile: {profile.profile_id} / "
        f"{group_count} source groups / {slot_count} episode slots"
    )
    print(f"[PASS] Measurement: {profile.measurement_level.value} / network_used=false")
    print(f"[SKIP] ShopSimulator live release: {source.manifest.decision}")
    if profile.mode == "live" or args.live:
        print("[FAIL] Live route: live source decision is no_go")
        print("\n结论: NO-GO。保持 ShopSimulator live 路线关闭。")
        return 1
    print("\n结论: fixed/in-memory 路线可继续; live release: no_go。")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    values = list(argv or ())
    if _has_option(values, "--profile"):
        return _profile_main(values)
    return foundation_main(values)


__all__ = ["build_parser", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
