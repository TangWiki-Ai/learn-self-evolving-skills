from __future__ import annotations

import os
from pathlib import Path

import pytest

from ses.foundation.doctor import run_doctor


@pytest.mark.live
def test_explicit_live_doctor_smoke() -> None:
    if os.environ.get("SES_RUN_LIVE") != "1":
        pytest.skip("set SES_RUN_LIVE=1 to authorize the paid live smoke")
    config = os.environ.get("SES_LIVE_CONFIG")
    if not config:
        pytest.skip("set SES_LIVE_CONFIG to the strict runtime config path")

    config_path = Path(config).resolve()
    results = run_doctor(
        project_root=config_path.parent,
        config_path=config_path,
        live=True,
        timeout=120,
    )

    assert all(result.status in {"PASS", "WARN"} for result in results)
