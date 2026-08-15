from __future__ import annotations

import gzip
import io
import json
from hashlib import sha256
from pathlib import Path
from typing import cast
from urllib.error import URLError

import pytest

from ses.testset.acquisition import (
    AcquisitionError,
    ChecksumMismatchError,
    NetworkDisabledError,
    acquire_asset,
)
from ses.testset.manifest import (
    AssetSpec,
    ManifestDriftError,
    load_manifest,
    verify_manifest_files,
)

UPSTREAM_ROOT = Path(__file__).resolve().parents[2] / "data" / "upstream"
MANIFEST_PATH = UPSTREAM_ROOT / "manifest.json"


def manifest_document() -> dict[str, object]:
    document = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise AssertionError("repository manifest must be an object")
    return cast(dict[str, object], document)


def materialize_manifest_files(root: Path, document: dict[str, object]) -> None:
    sources = document["sources"]
    assert isinstance(sources, list)
    for source in sources:
        assert isinstance(source, dict)
        license_spec = source["license"]
        assert isinstance(license_spec, dict)
        fixture_specs = source["fixture_files"]
        assert isinstance(fixture_specs, list)
        for spec in [license_spec, *fixture_specs]:
            relative = spec["path"]
            assert isinstance(relative, str)
            destination = root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((UPSTREAM_ROOT / relative).read_bytes())


def test_manifest_rejects_pin_drift(tmp_path: Path) -> None:
    document = manifest_document()
    sources = document["sources"]
    assert isinstance(sources, list)
    sources[1]["commit"] = "f" * 40
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ManifestDriftError, match="abcd commit drift"):
        load_manifest(path)


def test_manifest_rejects_exact_filter_drift(tmp_path: Path) -> None:
    document = manifest_document()
    sources = document["sources"]
    assert isinstance(sources, list)
    sources[1]["slice"]["filter"]["value"] = "Product_Defect"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ManifestDriftError, match="abcd slice drift"):
        load_manifest(path)


def test_manifest_rejects_tau_generation_commit_drift(tmp_path: Path) -> None:
    document = manifest_document()
    sources = document["sources"]
    assert isinstance(sources, list)
    sources[2]["slice"]["generation_commits"]["tau2_result_claude"] = "f" * 40
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ManifestDriftError, match="tau2 slice drift"):
        load_manifest(path)


def test_manifest_rejects_missing_inventory_and_transformation_history(
    tmp_path: Path,
) -> None:
    missing_assets = manifest_document()
    sources = missing_assets["sources"]
    assert isinstance(sources, list)
    sources[0]["assets"] = []
    path = tmp_path / "missing-assets.json"
    path.write_text(json.dumps(missing_assets), encoding="utf-8")
    with pytest.raises(ManifestDriftError, match="asset inventory"):
        load_manifest(path)

    missing_fixtures = manifest_document()
    sources = missing_fixtures["sources"]
    assert isinstance(sources, list)
    sources[2]["fixture_files"] = []
    path = tmp_path / "missing-fixtures.json"
    path.write_text(json.dumps(missing_fixtures), encoding="utf-8")
    with pytest.raises(ManifestDriftError, match="fixture inventory"):
        load_manifest(path)

    missing_transformation = manifest_document()
    missing_transformation.pop("transformation")
    path = tmp_path / "missing-transformation.json"
    path.write_text(json.dumps(missing_transformation), encoding="utf-8")
    with pytest.raises(ManifestDriftError, match="transformation"):
        load_manifest(path)


def test_manifest_rejects_complete_asset_fixture_and_projection_drift(
    tmp_path: Path,
) -> None:
    mutations: list[tuple[str, object]] = []

    changed_url = manifest_document()
    sources = changed_url["sources"]
    assert isinstance(sources, list)
    sources[1]["assets"][0]["url"] = (
        "https://example.invalid/6b8700ce67c6b37b062dd7a60abc76d7ef832a97/different.gz"
    )
    mutations.append(("url", changed_url))

    changed_compression = manifest_document()
    sources = changed_compression["sources"]
    assert isinstance(sources, list)
    sources[1]["assets"][0]["compression"] = "zip"
    mutations.append(("compression", changed_compression))

    renamed_fixture = manifest_document()
    sources = renamed_fixture["sources"]
    assert isinstance(sources, list)
    sources[1]["fixture_files"][0]["path"] = "abcd/fixture/renamed.json"
    mutations.append(("fixture-path", renamed_fixture))

    self_consistent_fixture_rewrite = manifest_document()
    sources = self_consistent_fixture_rewrite["sources"]
    assert isinstance(sources, list)
    rewritten = b"{}\n"
    sources[1]["fixture_files"][0]["bytes"] = len(rewritten)
    sources[1]["fixture_files"][0]["sha256"] = sha256(rewritten).hexdigest()
    mutations.append(("fixture-content", self_consistent_fixture_rewrite))

    changed_projection = manifest_document()
    sources = changed_projection["sources"]
    assert isinstance(sources, list)
    sources[2]["fixture_projection"]["fields"] = ["task_id"]
    mutations.append(("projection", changed_projection))

    for name, document in mutations:
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(ManifestDriftError, match="source identity drifted"):
            load_manifest(path)


def test_manifest_verification_detects_fixture_drift(tmp_path: Path) -> None:
    document = manifest_document()
    materialize_manifest_files(tmp_path, document)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    manifest = load_manifest(path)

    verify_manifest_files(manifest, tmp_path)
    fixture = tmp_path / "abcd" / "fixture" / "conversations.json"
    fixture.write_bytes(b"drifted")
    with pytest.raises(ManifestDriftError, match="drift"):
        verify_manifest_files(manifest, tmp_path)


def test_manifest_verification_detects_license_drift(tmp_path: Path) -> None:
    document = manifest_document()
    materialize_manifest_files(tmp_path, document)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    manifest = load_manifest(path)

    (tmp_path / "tau2" / "LICENSE").write_bytes(b"license drift")

    with pytest.raises(ManifestDriftError, match="drift"):
        verify_manifest_files(manifest, tmp_path)


class CountingFetcher:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls = 0

    def open(self, url: str, timeout: float) -> io.BytesIO:
        del url, timeout
        self.calls += 1
        return io.BytesIO(self.payload)


def asset_for(payload: bytes) -> AssetSpec:
    return AssetSpec(
        name="fixture",
        url="https://example.invalid/fixture.json",
        destination="downloads/fixture.json",
        bytes=len(payload),
        sha256=sha256(payload).hexdigest(),
        role="fixture",
    )


def test_network_is_disabled_unless_explicitly_enabled(tmp_path: Path) -> None:
    fetcher = CountingFetcher(b"payload")

    with pytest.raises(NetworkDisabledError, match="explicit"):
        acquire_asset(
            asset_for(b"payload"),
            tmp_path,
            allow_network=False,
            fetcher=fetcher,
        )

    assert fetcher.calls == 0
    assert not (tmp_path / "downloads" / "fixture.json").exists()


def test_rejected_destination_does_not_create_directories_outside_root(
    tmp_path: Path,
) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    asset = AssetSpec(
        name="escaping-fixture",
        url="https://example.invalid/fixture.json",
        destination=f"../{outside.name}/fixture.json",
        bytes=0,
        sha256=sha256(b"").hexdigest(),
        role="fixture",
    )

    with pytest.raises(AcquisitionError, match="escapes"):
        acquire_asset(asset, tmp_path, allow_network=False)

    assert not outside.exists()


def test_download_retries_then_atomically_installs_verified_bytes(
    tmp_path: Path,
) -> None:
    payload = b"verified payload"

    class FlakyFetcher:
        def __init__(self) -> None:
            self.calls = 0

        def open(self, url: str, timeout: float) -> io.BytesIO:
            del url, timeout
            self.calls += 1
            if self.calls < 3:
                raise URLError("temporary")
            return io.BytesIO(payload)

    fetcher = FlakyFetcher()
    destination = acquire_asset(
        asset_for(payload),
        tmp_path,
        allow_network=True,
        attempts=3,
        fetcher=fetcher,
    )

    assert fetcher.calls == 3
    assert destination.read_bytes() == payload
    assert list(destination.parent.glob("*.part")) == []


def test_bad_download_never_overwrites_existing_file(tmp_path: Path) -> None:
    payload = b"expected"
    destination = tmp_path / "downloads" / "fixture.json"
    destination.parent.mkdir()
    destination.write_bytes(b"existing")

    with pytest.raises(ChecksumMismatchError):
        acquire_asset(
            asset_for(payload),
            tmp_path,
            allow_network=True,
            attempts=1,
            fetcher=CountingFetcher(b"corrupt"),
        )

    assert destination.read_bytes() == b"existing"
    assert list(destination.parent.glob("*.part")) == []


def test_download_validates_declared_uncompressed_digest_before_install(
    tmp_path: Path,
) -> None:
    uncompressed = b"strict benchmark payload"
    payload = gzip.compress(uncompressed, mtime=0)
    asset = AssetSpec(
        name="compressed-fixture",
        url="https://example.invalid/fixture.json.gz",
        destination="downloads/fixture.json.gz",
        bytes=len(payload),
        sha256=sha256(payload).hexdigest(),
        role="fixture",
        compression="gzip",
        uncompressed_bytes=len(uncompressed),
        uncompressed_sha256=sha256(b"different").hexdigest(),
    )

    with pytest.raises(ChecksumMismatchError, match="uncompressed"):
        acquire_asset(
            asset,
            tmp_path,
            allow_network=True,
            attempts=1,
            fetcher=CountingFetcher(payload),
        )

    assert not (tmp_path / "downloads" / "fixture.json.gz").exists()
    assert list((tmp_path / "downloads").glob("*.part")) == []
