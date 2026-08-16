"""Installed resources for the offline Lesson 1 demo."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from importlib.resources import files

from ses.foundation.config import ModelLock, RuntimeConfig


@dataclass(frozen=True, slots=True)
class DemoResources:
    runtime_config: RuntimeConfig
    model_lock: ModelLock
    runtime_config_sha256: str
    model_lock_sha256: str


def load_demo_resources() -> DemoResources:
    """Read and validate defaults from the installed package, never the repo root."""
    root = files(__name__)
    runtime_bytes = root.joinpath("runtime-config.json").read_bytes()
    model_lock_bytes = root.joinpath("models.lock.json").read_bytes()
    return DemoResources(
        runtime_config=RuntimeConfig.model_validate_json(runtime_bytes),
        model_lock=ModelLock.model_validate_json(model_lock_bytes),
        runtime_config_sha256=hashlib.sha256(runtime_bytes).hexdigest(),
        model_lock_sha256=hashlib.sha256(model_lock_bytes).hexdigest(),
    )
