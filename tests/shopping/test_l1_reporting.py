from __future__ import annotations

from pathlib import Path

from tests.shopping._fixed_v0_pipeline import build_fixed_v0_pipeline


def test_fresh_pair_writes_both_shared_l1_reports_into_the_learner_receipt(
    tmp_path: Path,
) -> None:
    pipeline = build_fixed_v0_pipeline(tmp_path)

    expected = (
        pipeline.root / "run-shopping-develop-baseline-fixed" / "l1.html",
        pipeline.root / "run-shopping-develop-skill-v0-fixed" / "l1.html",
    )
    output_refs = {
        reference.path: reference for reference in pipeline.paired.receipt.outputs
    }

    for report in expected:
        relative = report.relative_to(pipeline.root).as_posix()
        reference = output_refs[relative]
        payload = report.read_bytes()
        reference.verify_bytes(payload)
        rendered = payload.decode("utf-8")
        assert "L1 reproducible baseline" in rendered
        assert "shopping_raw_reward" in rendered
