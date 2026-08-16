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


def _qualification_args(output: Path, reviews: Path) -> list[str]:
    return [
        "qualify-cases",
        "--candidates",
        str(TICKET / "candidate-seeds.jsonl"),
        "--variants",
        str(TICKET / "variant-plan.json"),
        "--reviews",
        str(reviews),
        "--output",
        str(output),
        "--json",
    ]


def test_cli_runs_candidate_to_expanded_l1_fully_offline(tmp_path: Path) -> None:
    output = tmp_path / "qualified"
    reviews = tmp_path / "synthetic-reviews.jsonl"
    reviews.write_text("", encoding="utf-8")

    pending = _run(*_qualification_args(output, reviews))
    assert pending.returncode == 0, pending.stderr
    assert json.loads(pending.stdout)["pending_count"] == 15
    packet = json.loads((output / "review-packet.json").read_text())
    review_rows = [
        {
            "case_id": row["case_id"],
            "reviewed_hash": row["reviewed_hash"],
            "decision": "approved",
            "reason": "synthetic integration review",
            "reviewed_at": "2026-08-16T12:00:00Z",
            "reviewer": "synthetic-test-reviewer",
        }
        for row in packet
    ]
    reviews.write_text(
        "".join(
            json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
            for row in review_rows
        ),
        encoding="utf-8",
    )

    qualified = _run(*_qualification_args(output, reviews))
    assert qualified.returncode == 0, qualified.stderr
    assert json.loads(qualified.stdout)["qualified_count"] == 15

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
    reviews = tmp_path / "reviews.jsonl"
    reviews.write_text("", encoding="utf-8")

    completed = _run(
        *_qualification_args(output, reviews),
        "--split",
        "selection",
    )

    assert completed.returncode == 1
    assert marker.read_text(encoding="utf-8") == "unchanged"
    assert list(output.iterdir()) == [marker]
