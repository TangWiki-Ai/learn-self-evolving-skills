from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
TICKET = ROOT / "data" / "testset" / "ticket07"


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in tuple(environment):
        if "API_KEY" in name or "TOKEN" in name:
            environment.pop(name)
    environment["HTTP_PROXY"] = "http://127.0.0.1:9"
    environment["HTTPS_PROXY"] = "http://127.0.0.1:9"
    return environment


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "ses.cli.app", *args],
        cwd=ROOT,
        env=_environment(),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def _qualification_args(output: Path, attestations: Path) -> list[str]:
    return [
        "qualify-cases",
        "--candidates",
        str(TICKET / "candidate-seeds.jsonl"),
        "--variants",
        str(TICKET / "variant-plan.json"),
        "--attestations",
        str(attestations),
        "--output",
        str(output),
        "--json",
    ]


def test_cli_runs_candidate_to_expanded_l1_fully_offline(tmp_path: Path) -> None:
    output = tmp_path / "qualified"
    attestations = TICKET / "course-attestations.jsonl"

    pending = _run(*_qualification_args(output, attestations))
    assert pending.returncode == 0, pending.stderr
    pending_payload = json.loads(pending.stdout)
    assert pending_payload["pending_count"] == 15
    assert pending_payload["source_candidate_count"] == 2
    assert pending_payload["selected_source_count"] == 1
    assert pending_payload["fixed_course_count"] == 15
    assert pending_payload["excluded_count"] == 7
    assert pending_payload["qualified_count"] == 0
    assert pending_payload["review_status"] == ("course_authored_pending_human_review")
    assert pending_payload["curation_response_source"] == "fixed_response"
    assert pending_payload["network_used"] is False
    assert pending_payload["live_provider_used"] is False
    assert pending_payload["protected_split_validation_status"] == (
        "fixed_offline_unverified"
    )
    assert pending_payload["protected_split_provenance_sha256"] is None
    baseline_root = tmp_path / "baseline"
    baseline = _run(
        "baseline",
        "--catalog-manifest",
        str(output / "develop-manifest.json"),
        "--output-root",
        str(baseline_root),
        "--run-id",
        "run-ticket07-integration",
        "--iterations",
        "1",
        "--json",
    )
    assert baseline.returncode == 0, baseline.stderr
    payload = json.loads(baseline.stdout)
    assert payload["metrics"]["sample_size"] == 15
    assert payload["metrics"]["pass_at_1"] == 1.0
    events = [
        json.loads(line)
        for line in Path(payload["events"]).read_text(encoding="utf-8").splitlines()
    ]
    attempts = [row for row in events if row["event_type"] == "attempt"]
    assert len(attempts) == 15
    assert {row["status"] for row in attempts} == {"pass"}
    assert all(row["artifacts"]["traces"] for row in attempts)
    assert all(row["artifacts"]["state_diff"] for row in attempts)
    assert all(row["artifacts"]["grade"] for row in attempts)
    html = Path(payload["html"]).read_text(encoding="utf-8").casefold()
    assert "oracle" not in html
    assert "gold" not in html
    assert "human_review" not in html
    assert "live_provider" not in html


def test_cli_protected_split_failure_leaves_output_unchanged(tmp_path: Path) -> None:
    output = tmp_path / "protected-output"
    output.mkdir()
    marker = output / "marker.txt"
    marker.write_text("unchanged", encoding="utf-8")
    attestations = tmp_path / "attestations.jsonl"
    attestations.write_text("", encoding="utf-8")

    completed = _run(
        *_qualification_args(output, attestations),
        "--split",
        "selection",
    )

    assert completed.returncode == 1
    assert marker.read_text(encoding="utf-8") == "unchanged"
    assert list(output.iterdir()) == [marker]

    live = _run(
        *_qualification_args(output, attestations),
        "--split",
        "final",
        "--curation-mode",
        "live",
    )
    assert live.returncode == 1
    assert "split_write_protected:final" in live.stderr
    assert "SILICONFLOW_API_KEY" not in live.stderr
    assert list(output.iterdir()) == [marker]


def test_live_curation_fails_closed_before_reading_provider_credentials(
    tmp_path: Path,
) -> None:
    output = tmp_path / "live-output"
    attestations = TICKET / "course-attestations.jsonl"

    completed = _run(
        *_qualification_args(output, attestations),
        "--curation-mode",
        "live",
    )

    assert completed.returncode == 1
    assert "requires a trusted external holdout verifier" in completed.stderr
    assert "SILICONFLOW_API_KEY" not in completed.stderr
    assert not output.exists()
