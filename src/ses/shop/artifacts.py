"""Trusted atomic snapshot output for the shop MCP subprocess."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from ses.contracts import ShopSnapshot, artifact_json_bytes

_ARTIFACT_DIRECTORY = "shop"
BEFORE_SNAPSHOT_NAME = "before.json"
AFTER_SNAPSHOT_NAME = "after.json"


class SnapshotArtifactWriter:
    """Write canonical snapshots only to fixed children of an evaluator root."""

    def __init__(self, artifact_root: Path) -> None:
        root = artifact_root.resolve()
        if root == Path(root.anchor):
            raise ValueError("artifact root must not be a filesystem root")
        root.mkdir(parents=True, exist_ok=True)
        directory = root / _ARTIFACT_DIRECTORY
        directory.mkdir(parents=True, exist_ok=True)
        if directory.resolve().parent != root:
            raise ValueError("shop artifact directory escapes artifact root")
        self._directory = directory

    @property
    def before_path(self) -> Path:
        return self._directory / BEFORE_SNAPSHOT_NAME

    @property
    def after_path(self) -> Path:
        return self._directory / AFTER_SNAPSHOT_NAME

    def write_before(self, snapshot: ShopSnapshot) -> None:
        self._write_atomic(self.before_path, snapshot)

    def write_after(self, snapshot: ShopSnapshot) -> None:
        self._write_atomic(self.after_path, snapshot)

    def _write_atomic(self, destination: Path, snapshot: ShopSnapshot) -> None:
        payload = artifact_json_bytes(snapshot)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=self._directory,
            prefix=f".{destination.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
