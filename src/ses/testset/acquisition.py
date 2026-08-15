"""Retryable, checksum-first, atomic acquisition of pinned source assets."""

from __future__ import annotations

import gzip
import os
import ssl
import tempfile
import time
from contextlib import AbstractContextManager
from hashlib import sha256
from pathlib import Path
from typing import BinaryIO, Protocol, cast
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ses.testset.manifest import AssetSpec, UpstreamManifest


class AcquisitionError(RuntimeError):
    """A pinned asset could not be acquired safely."""


class NetworkDisabledError(AcquisitionError):
    """Network access was needed but not explicitly enabled."""


class ChecksumMismatchError(AcquisitionError):
    """Downloaded bytes did not match the machine-readable manifest."""


class Fetcher(Protocol):
    def open(self, url: str, timeout: float) -> AbstractContextManager[BinaryIO]: ...


def _tls_context() -> ssl.SSLContext:
    defaults = ssl.get_default_verify_paths()
    for candidate in (
        os.environ.get("SSL_CERT_FILE"),
        defaults.cafile,
        defaults.openssl_cafile,
        "/etc/ssl/cert.pem",
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/pki/tls/certs/ca-bundle.crt",
    ):
        if candidate and Path(candidate).is_file():
            return ssl.create_default_context(cafile=candidate)
    return ssl.create_default_context()


class UrlFetcher:
    def __init__(self) -> None:
        self._context = _tls_context()

    def open(self, url: str, timeout: float) -> AbstractContextManager[BinaryIO]:
        request = Request(
            url,
            headers={"User-Agent": "learn-self-evolving-skills-data/1"},
        )
        response = urlopen(
            request,
            timeout=timeout,
            context=self._context,
        )
        return cast(AbstractContextManager[BinaryIO], response)


def _verify_existing(path: Path, asset: AssetSpec) -> bool:
    if not path.is_file():
        return False
    payload = path.read_bytes()
    return len(payload) == asset.bytes and sha256(payload).hexdigest() == asset.sha256


def _destination_path(root: Path, relative: str) -> Path:
    resolved_root = root.resolve()
    destination = (resolved_root / relative).resolve()
    if not destination.is_relative_to(resolved_root):
        raise AcquisitionError("asset destination escapes output root")
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


def _download_once(
    asset: AssetSpec,
    destination: Path,
    *,
    timeout: float,
    fetcher: Fetcher,
) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".part",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            digest = sha256()
            byte_count = 0
            with fetcher.open(asset.url, timeout) as response:
                while chunk := response.read(1024 * 1024):
                    temporary.write(chunk)
                    digest.update(chunk)
                    byte_count += len(chunk)
            temporary.flush()
            os.fsync(temporary.fileno())
        if byte_count != asset.bytes or digest.hexdigest() != asset.sha256:
            raise ChecksumMismatchError(
                f"download checksum or size mismatch for {asset.name}"
            )
        if (
            asset.uncompressed_bytes is not None
            and asset.uncompressed_sha256 is not None
        ):
            if asset.compression != "gzip":
                raise ChecksumMismatchError(
                    f"unsupported uncompressed validation for {asset.name}"
                )
            uncompressed_digest = sha256()
            uncompressed_bytes = 0
            try:
                with gzip.open(temporary_path, "rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        uncompressed_digest.update(chunk)
                        uncompressed_bytes += len(chunk)
            except (OSError, EOFError) as exc:
                raise ChecksumMismatchError(
                    f"cannot decompress verified container for {asset.name}"
                ) from exc
            if (
                uncompressed_bytes != asset.uncompressed_bytes
                or uncompressed_digest.hexdigest() != asset.uncompressed_sha256
            ):
                raise ChecksumMismatchError(
                    f"uncompressed checksum or size mismatch for {asset.name}"
                )
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def acquire_asset(
    asset: AssetSpec,
    destination_root: Path,
    *,
    allow_network: bool = False,
    attempts: int = 3,
    timeout: float = 60.0,
    backoff_seconds: float = 0.25,
    fetcher: Fetcher | None = None,
) -> Path:
    """Reuse a verified asset or explicitly download and atomically install it."""

    if attempts < 1:
        raise ValueError("attempts must be at least one")
    destination = _destination_path(destination_root, asset.destination)
    if _verify_existing(destination, asset):
        return destination
    if not allow_network:
        raise NetworkDisabledError(
            f"{asset.name} is missing or drifted; enable network explicitly"
        )
    active_fetcher = fetcher or UrlFetcher()
    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            _download_once(
                asset,
                destination,
                timeout=timeout,
                fetcher=active_fetcher,
            )
            return destination
        except HTTPError as exc:
            last_error = exc
            if 400 <= exc.code < 500 and exc.code not in {408, 429}:
                break
        except (OSError, AcquisitionError) as exc:
            last_error = exc
        if attempt < attempts and backoff_seconds:
            time.sleep(backoff_seconds * attempt)
    if isinstance(last_error, ChecksumMismatchError):
        raise last_error
    raise AcquisitionError(
        f"failed to acquire {asset.name} after {attempts} attempts"
    ) from last_error


def acquire_full_manifest(
    manifest: UpstreamManifest,
    destination_root: Path,
    *,
    allow_network: bool = False,
    attempts: int = 3,
    timeout: float = 60.0,
) -> tuple[Path, ...]:
    """Acquire every full-profile asset; network remains disabled by default."""

    return tuple(
        acquire_asset(
            asset,
            destination_root,
            allow_network=allow_network,
            attempts=attempts,
            timeout=timeout,
        )
        for source in manifest.sources
        for asset in source.assets
    )
