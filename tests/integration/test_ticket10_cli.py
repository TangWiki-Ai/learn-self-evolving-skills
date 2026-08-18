from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[2]
PARENT = ROOT / "course/ch07-create-v0/artifacts/skill/v0"
SEED_EVIDENCE = ROOT / "course/ch07-create-v0/artifacts/summary.json"
FAILURE_EVIDENCE = ROOT / "tests/fixtures/evolution/synthetic-failure-evidence.json"
SELECTION_LOCK = ROOT / "data/testset/protected/selection-manifest.json"
NOW = "2026-08-18T09:00:00+00:00"


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in tuple(environment):
        if "API_KEY" in name or "TOKEN" in name:
            environment.pop(name)
    environment["PYTHONPATH"] = str(ROOT / "src")
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
        timeout=90,
        check=False,
    )


def _candidate(tmp_path: Path) -> Path:
    output = tmp_path / "candidate"
    completed = _run(
        "evolve",
        "--parent",
        str(PARENT),
        "--evidence",
        str(FAILURE_EVIDENCE),
        "--output",
        str(output),
        "--mode",
        "fixed",
        "--json",
    )
    assert completed.returncode == 0, completed.stderr
    return output


def _initialize_and_register(
    registry: Path, candidate: Path
) -> tuple[dict[str, object], dict[str, object]]:
    initialized = _run(
        "registry",
        "init",
        "--registry",
        str(registry),
        "--accepted-skill",
        str(PARENT),
        "--evidence",
        str(SEED_EVIDENCE),
        "--command-id",
        "command-cli-initialize",
        "--occurred-at",
        NOW,
        "--json",
    )
    assert initialized.returncode == 0, initialized.stderr
    registered = _run(
        "registry",
        "register",
        "--registry",
        str(registry),
        "--candidate-bundle",
        str(candidate),
        "--command-id",
        "command-cli-register",
        "--occurred-at",
        NOW,
        "--json",
    )
    assert registered.returncode == 0, registered.stderr
    return json.loads(initialized.stdout), json.loads(registered.stdout)


def test_cli_accepts_promotes_inspects_and_rolls_back_fixed_candidate(
    tmp_path: Path,
) -> None:
    candidate = _candidate(tmp_path)
    registry = tmp_path / "registry"
    initialized, registered = _initialize_and_register(registry, candidate)

    gated = _run(
        "gate",
        "candidate",
        "--registry",
        str(registry),
        "--candidate-bundle",
        str(candidate),
        "--selection-lock",
        str(SELECTION_LOCK),
        "--project-root",
        str(ROOT),
        "--gate-id",
        "gate-cli-accept",
        "--fixed-scenario",
        "accept",
        "--command-id",
        "command-cli-accept",
        "--measured-at",
        NOW,
        "--json",
    )
    assert gated.returncode == 0, gated.stderr
    decision = json.loads(gated.stdout)
    assert decision["outcome"] == "accepted"
    assert decision["mode"] == "fixed"
    assert decision["measurement_kind"] == "synthetic_offline"
    assert decision["network_used"] is False
    assert (registry / "gates/gate-cli-accept/gate-decision.json").is_file()

    promoted = _run(
        "registry",
        "promote",
        "--registry",
        str(registry),
        "--candidate-id",
        str(registered["version_id"]),
        "--command-id",
        "command-cli-promote",
        "--occurred-at",
        NOW,
        "--json",
    )
    assert promoted.returncode == 0, promoted.stderr
    assert json.loads(promoted.stdout)["event_type"] == "promoted"

    inspected = _run("registry", "inspect", "--registry", str(registry), "--json")
    assert inspected.returncode == 0, inspected.stderr
    promoted_state = json.loads(inspected.stdout)
    assert promoted_state["audit_status"] == "pass"
    assert promoted_state["current_accepted_sha256"] == registered["version_sha256"]
    assert promoted_state["event_count"] == 4

    rolled_back = _run(
        "registry",
        "rollback",
        "--registry",
        str(registry),
        "--target-skill-sha256",
        str(initialized["version_sha256"]),
        "--command-id",
        "command-cli-rollback",
        "--occurred-at",
        NOW,
        "--json",
    )
    assert rolled_back.returncode == 0, rolled_back.stderr
    assert json.loads(rolled_back.stdout)["event_type"] == "rolled_back"

    audited = _run("registry", "audit", "--registry", str(registry), "--json")
    assert audited.returncode == 0, audited.stderr
    rolled_back_state = json.loads(audited.stdout)
    assert rolled_back_state["audit_status"] == "pass"
    assert rolled_back_state["current_accepted_sha256"] == initialized["version_sha256"]
    assert rolled_back_state["event_count"] == 5


def test_cli_rejection_is_nonzero_but_remains_auditable(tmp_path: Path) -> None:
    candidate = _candidate(tmp_path)
    registry = tmp_path / "registry"
    initialized, registered = _initialize_and_register(registry, candidate)

    gated = _run(
        "gate",
        "candidate",
        "--registry",
        str(registry),
        "--candidate-bundle",
        str(candidate),
        "--selection-lock",
        str(SELECTION_LOCK),
        "--project-root",
        str(ROOT),
        "--gate-id",
        "gate-cli-tie",
        "--fixed-scenario",
        "tie",
        "--command-id",
        "command-cli-reject",
        "--measured-at",
        NOW,
        "--json",
    )

    assert gated.returncode == 1
    decision = json.loads(gated.stdout)
    assert decision["outcome"] == "rejected"
    assert decision["reason_codes"] == ["selection_tie"]
    persisted = json.loads(
        (registry / "gates/gate-cli-tie/gate-decision.json").read_text()
    )
    assert persisted["outcome"] == "rejected"

    inspected = _run("registry", "inspect", "--registry", str(registry), "--json")
    assert inspected.returncode == 0, inspected.stderr
    state = json.loads(inspected.stdout)
    assert state["current_accepted_sha256"] == initialized["version_sha256"]
    assert state["event_count"] == 3
    assert state["events"][-1]["event_type"] == "candidate_rejected"
    rejected = next(
        version
        for version in state["versions"]
        if version["skill_sha256"] == registered["version_sha256"]
    )
    assert rejected["status"] == "rejected"
    assert rejected["gate_decision"] is not None

    promoted = _run(
        "registry",
        "promote",
        "--registry",
        str(registry),
        "--candidate-id",
        str(registered["version_id"]),
        "--command-id",
        "command-cli-invalid-promote",
        "--occurred-at",
        NOW,
        "--json",
    )
    assert promoted.returncode == 1
    assert "promotion requires an accepted candidate" in promoted.stderr


def test_cli_audit_rejects_a_tampered_event_log(tmp_path: Path) -> None:
    registry = tmp_path / "registry"
    initialized = _run(
        "registry",
        "init",
        "--registry",
        str(registry),
        "--accepted-skill",
        str(PARENT),
        "--evidence",
        str(SEED_EVIDENCE),
        "--command-id",
        "command-cli-tamper-initialize",
        "--occurred-at",
        NOW,
        "--json",
    )
    assert initialized.returncode == 0, initialized.stderr

    events_path = registry / "events.jsonl"
    event = json.loads(events_path.read_text())
    event["reason"] = "tampered"
    events_path.write_text(
        json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    audited = _run("registry", "audit", "--registry", str(registry), "--json")
    assert audited.returncode == 1
    assert "registry_error:" in audited.stderr
    assert "hash" in audited.stderr


def test_gate_cli_rejects_a_noncanonical_registry_path_without_a_traceback() -> None:
    completed = _run(
        "gate",
        "candidate",
        "--candidate-bundle",
        "missing-candidate",
        "--gate-id",
        "gate-cli-invalid-root",
        "--command-id",
        "command-cli-invalid-root",
        "--measured-at",
        NOW,
        "--registry",
        "../outside-registry",
        "--json",
    )

    assert completed.returncode == 1
    assert completed.stderr.startswith("gate_error:")
    assert "Traceback" not in completed.stderr
    assert str(ROOT) not in completed.stderr


def test_cli_rejects_an_unsafe_registry_path_without_a_traceback() -> None:
    completed = _run(
        "registry",
        "audit",
        "--registry",
        "../outside",
        "--json",
    )

    assert completed.returncode == 1
    assert completed.stderr.startswith("registry_error:")
    assert "Traceback" not in completed.stderr
    assert str(ROOT) not in completed.stderr
