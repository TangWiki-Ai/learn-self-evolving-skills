"""Content-addressed artifact references."""

from __future__ import annotations

import hashlib
import hmac
import re
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, TypeAlias

from pydantic import AfterValidator, Field, StrictStr

from ses.contracts.base import ContractModel


class ArtifactRoot(StrEnum):
    """Roots from which artifact paths may be resolved."""

    WORKSPACE = "workspace"
    RUN = "run"


Sha256Digest: TypeAlias = Annotated[
    StrictStr,
    Field(pattern=r"^[0-9a-f]{64}$"),
]


def _validate_artifact_path(value: str) -> str:
    if not value or value == ".":
        raise ValueError("artifact path must name a file")
    if "\x00" in value or "\\" in value:
        raise ValueError("artifact path must be a relative POSIX path")
    if value.endswith("/") or "//" in value:
        raise ValueError("artifact path must be canonical")
    if re.match(r"^[A-Za-z]:", value):
        raise ValueError("artifact path must not use a Windows drive")
    path = PurePosixPath(value)
    if path.is_absolute() or value == "~" or value.startswith("~/"):
        raise ValueError("artifact path must be relative")
    if any(part in {".", ".."} for part in value.split("/")):
        raise ValueError("artifact path must not traverse directories")
    if path.as_posix() != value:
        raise ValueError("artifact path must be canonical")
    return value


RelativeArtifactPath: TypeAlias = Annotated[
    StrictStr,
    AfterValidator(_validate_artifact_path),
]


def _validate_json_pointer(value: str) -> str:
    if not value.startswith("/"):
        raise ValueError("JSON pointer must start with '/'")
    if re.search(r"~(?:[^01]|$)", value):
        raise ValueError("JSON pointer contains an invalid escape")
    return value


JsonPointer: TypeAlias = Annotated[
    StrictStr,
    AfterValidator(_validate_json_pointer),
]


class ArtifactRef(ContractModel):
    """Content-addressed file under a controlled workspace or run root."""

    root: ArtifactRoot
    path: RelativeArtifactPath
    sha256: Sha256Digest

    def verify_bytes(self, content: bytes) -> None:
        """Raise when content does not match the declared wire-byte digest."""
        actual = hashlib.sha256(content).hexdigest()
        if not hmac.compare_digest(actual, self.sha256):
            raise ValueError(
                f"artifact checksum mismatch: expected {self.sha256}, got {actual}"
            )
