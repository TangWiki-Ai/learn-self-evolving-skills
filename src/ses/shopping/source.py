"""Fail-closed loader for the pinned ShopSimulator Phase 0 decision."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ses.contracts.serialization import content_sha256
from ses.contracts.shopping import ShopSimulatorSourceManifest


@dataclass(frozen=True, slots=True)
class LoadedShopSimulatorSourceManifest:
    """Validated source decision and its semantic identity."""

    manifest: ShopSimulatorSourceManifest
    manifest_sha256: str

    @property
    def live_enabled(self) -> bool:
        return self.manifest.decision == "go"


def load_shop_simulator_source_manifest(
    path: Path,
) -> LoadedShopSimulatorSourceManifest:
    """Load the strict source record; validation rejects an unsafe `go`."""

    manifest = ShopSimulatorSourceManifest.model_validate_json(path.read_bytes())
    return LoadedShopSimulatorSourceManifest(
        manifest=manifest,
        manifest_sha256=content_sha256(manifest),
    )
