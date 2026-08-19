from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

import pytest

import ses.release.validator as validator_module
from ses.release.validator import (
    _EXPECTED_ABCD_SUMMARY,
    _EXPECTED_FULL_FUNNEL,
    _EXPECTED_FULL_OUTPUT_RECORDS,
    _EXPECTED_TAU2_SUMMARY,
    CheckStatus,
    DocumentedCommand,
    ReleaseCheck,
    ReleaseReport,
    _check_absolute_paths,
    _check_command_evidence,
    _check_command_syntax,
    _check_cost_classification,
    _check_credentials,
    _check_full_data_regeneration,
    _check_historical_live_provenance,
    _check_holdout,
    _check_public_holdout_leak,
    _check_public_wording,
    _legacy_develop_review_claims,
    _logical_commands,
    _pytest_count_summary,
    validate_release,
)
from ses.testset.holdout import HoldoutLeakScanResult, HoldoutSummary

_ROOT = Path(__file__).resolve().parents[2]


def _track_release_files(repository: Path) -> None:
    subprocess.run(
        ["git", "init", "--quiet", str(repository)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "add", "--all"],
        check=True,
        capture_output=True,
    )


def _write_private_inventory(protected_root: Path, source_id: str) -> None:
    inventory = protected_root / "private/holdout-inventory.json"
    inventory.parent.mkdir(parents=True)
    inventory.write_text(
        json.dumps({"records": [{"source_id": source_id}]}),
        encoding="utf-8",
    )


def _stub_external_holdout_leak_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_scan_external_holdout_leaks(
        *,
        bundle_root: Path,
        public_lock_root: Path,
        candidate_documents: Mapping[str, str],
    ) -> HoldoutLeakScanResult:
        del public_lock_root
        inventory = json.loads(
            (bundle_root / "private/holdout-inventory.json").read_text(encoding="utf-8")
        )
        protected_values = {
            row["source_id"]
            for row in inventory["records"]
            if isinstance(row, dict) and isinstance(row.get("source_id"), str)
        }
        return HoldoutLeakScanResult(
            status="external_holdout_snapshot_verified",
            matched_relative_paths=tuple(
                sorted(
                    relative
                    for relative, text in candidate_documents.items()
                    if any(value in text for value in protected_values)
                )
            ),
        )

    monkeypatch.setattr(
        validator_module,
        "scan_external_holdout_leaks",
        fake_scan_external_holdout_leaks,
    )


def test_report_status_prefers_fail_then_deviation() -> None:
    report = ReleaseReport(
        checks=(
            ReleaseCheck("pass", CheckStatus.PASS, "ok"),
            ReleaseCheck("deviation", CheckStatus.DEVIATION, "pending"),
            ReleaseCheck("fail", CheckStatus.FAIL, "broken"),
        ),
        lesson_count=10,
        documented_command_count=1,
    )

    assert report.status is CheckStatus.FAIL
    assert report.as_dict()["check_counts"] == {
        "pass": 1,
        "deviation": 1,
        "fail": 1,
    }


def test_logical_commands_join_continuations_and_ignore_comments() -> None:
    commands = _logical_commands(
        """Try `uv run ses run-case --json` first.
```bash
# setup note
uv run ses auto-evolve \\
  --mode fixed

uv run pytest course/ch10-auto-evolve-and-portfolio/tests
```
""",
        readme="course/ch10-auto-evolve-and-portfolio/README.md",
        lesson=10,
    )

    assert [command.command for command in commands] == [
        "uv run ses run-case --json",
        "uv run ses auto-evolve --mode fixed",
        "uv run pytest course/ch10-auto-evolve-and-portfolio/tests",
    ]
    assert [command.line for command in commands] == [1, 4, 7]
    assert commands[1].command_id.startswith("lesson-10:line-4:")


def test_absolute_path_check_reports_relative_location(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "runbook.md").write_text(
        "Run /Us" + "ers/example/private/release.sh\n", encoding="utf-8"
    )

    check = _check_absolute_paths(tmp_path)

    assert check.status is CheckStatus.FAIL
    assert check.details == ("absolute:docs/runbook.md:1",)


def test_absolute_path_check_allows_only_the_known_l3_security_fixture(
    tmp_path: Path,
) -> None:
    test_file = tmp_path / "tests/reporting/test_l3_report.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        "fixture = "
        + repr("/Us" + "ers/example/project/private.json")
        + "; leak = "
        + repr("/Us" + "ers/alice/real-secret.json")
        + "\n",
        encoding="utf-8",
    )

    check = _check_absolute_paths(tmp_path)

    assert check.status is CheckStatus.FAIL
    assert check.details == ("absolute:tests/reporting/test_l3_report.py:1",)


def test_pytest_summary_discards_runtime() -> None:
    assert _pytest_count_summary("..\n2 passed in 0.12s\n") == "2 passed"
    assert (
        _pytest_count_summary("short summary\n2 failed, 7 passed in 22.63s\n")
        == "2 failed, 7 passed"
    )


def test_credential_check_rejects_literal_bearer_token(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "probe.sh").write_text(
        "Authorization: Bear" + "er abcdefghijklmnopqrstuvwxyz\n", encoding="utf-8"
    )

    check = _check_credentials(tmp_path)

    assert check.status is CheckStatus.FAIL
    assert check.details == ("credential:scripts/probe.sh",)


def test_security_scans_fail_closed_on_non_utf8_release_text(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "opaque.md").write_bytes(b"not utf-8: \xff\n")

    credential_check = _check_credentials(tmp_path)
    path_check = _check_absolute_paths(tmp_path)

    assert credential_check.status is CheckStatus.FAIL
    assert credential_check.details == ("unreadable:docs/opaque.md",)
    assert path_check.status is CheckStatus.FAIL
    assert path_check.details == ("unreadable:docs/opaque.md",)


def test_security_scans_read_staged_blobs_when_worktree_was_sanitized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    credential = repository / "docs/credential.md"
    absolute_path = repository / "docs/path.md"
    source_leak = repository / "course/ch01-example/starter/source.md"
    gold_leak = repository / "scripts/gold.py"
    for path in (credential, absolute_path, source_leak, gold_leak):
        path.parent.mkdir(parents=True, exist_ok=True)
    credential.write_text(
        "Authorization: Bear" + "er abcdefghijklmnopqrstuvwxyz\n",
        encoding="utf-8",
    )
    absolute_path.write_text(
        "release root: /Us" + "ers/alice/private/release\n",
        encoding="utf-8",
    )
    source_leak.write_text("private-source-in-index\n", encoding="utf-8")
    record_key = "record_" + "type"
    record_value = "holdout_deterministic_" + "oracle"
    gold_leak.write_text(
        "payload = " + repr({record_key: record_value}) + "\n",
        encoding="utf-8",
    )
    _track_release_files(repository)
    for path in (credential, absolute_path, source_leak, gold_leak):
        path.write_text("sanitized worktree\n", encoding="utf-8")
    protected = tmp_path / "protected"
    _write_private_inventory(protected, "private-source-in-index")
    _stub_external_holdout_leak_scan(monkeypatch)

    credential_check = _check_credentials(repository)
    path_check = _check_absolute_paths(repository)
    leak_check = _check_public_holdout_leak(
        repository,
        protected_holdout_root=protected,
    )

    assert credential_check.status is CheckStatus.FAIL
    assert credential_check.details == ("credential:docs/credential.md",)
    assert path_check.status is CheckStatus.FAIL
    assert path_check.details == ("absolute:docs/path.md:1",)
    assert leak_check.status is CheckStatus.FAIL
    assert leak_check.details == (
        "course/ch01-example/starter/source.md",
        "scripts/gold.py",
    )


def test_public_holdout_scan_uses_trusted_seam_for_index_and_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    protected = tmp_path / "external-protected"
    protected.mkdir()
    protected_values = {
        "selected-source-id",
        "Selected private request.",
        "a" * 64,
    }
    staged_paths = {
        "docs/index-source.md": "selected-source-id",
        "docs/index-prompt.md": "Selected private request.",
        "docs/index-hash.md": "a" * 64,
    }
    worktree_paths = {
        "docs/worktree-source.md": "selected-source-id",
        "docs/worktree-prompt.md": "Selected private request.",
        "docs/worktree-hash.md": "a" * 64,
    }
    for relative, value in staged_paths.items():
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
    for relative in worktree_paths:
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("safe staged text", encoding="utf-8")
    _track_release_files(repository)
    for relative in staged_paths:
        (repository / relative).write_text("safe worktree text", encoding="utf-8")
    for relative, value in worktree_paths.items():
        (repository / relative).write_text(value, encoding="utf-8")

    def fake_scan_external_holdout_leaks(
        *,
        bundle_root: Path,
        public_lock_root: Path,
        candidate_documents: Mapping[str, str],
    ) -> HoldoutLeakScanResult:
        assert bundle_root == protected
        assert public_lock_root == repository / "data/testset/protected"
        return HoldoutLeakScanResult(
            status="external_holdout_snapshot_verified",
            matched_relative_paths=tuple(
                sorted(
                    relative
                    for relative, text in candidate_documents.items()
                    if any(value in text for value in protected_values)
                )
            ),
        )

    monkeypatch.setattr(
        validator_module,
        "scan_external_holdout_leaks",
        fake_scan_external_holdout_leaks,
        raising=False,
    )

    check = _check_public_holdout_leak(
        repository,
        protected_holdout_root=protected,
    )

    assert check.status is CheckStatus.FAIL
    assert check.details == tuple(sorted((*staged_paths, *worktree_paths)))
    assert not any(value in json.dumps(check.as_dict()) for value in protected_values)


def test_public_holdout_scan_redacts_trusted_seam_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    release_file = repository / "README.md"
    release_file.parent.mkdir(parents=True)
    release_file.write_text("safe release text", encoding="utf-8")
    _track_release_files(repository)
    protected = tmp_path / "external-protected"
    protected.mkdir()
    protected_identity = "selected-private-identity"

    def fail_external_scan(**kwargs: object) -> HoldoutLeakScanResult:
        del kwargs
        raise RuntimeError(f"external inventory mismatch: {protected_identity}")

    monkeypatch.setattr(
        validator_module,
        "scan_external_holdout_leaks",
        fail_external_scan,
    )

    check = _check_public_holdout_leak(
        repository,
        protected_holdout_root=protected,
    )

    assert check.status is CheckStatus.FAIL
    assert check.details == ("external_holdout_leak_scan_failed",)
    assert protected_identity not in json.dumps(check.as_dict())


def test_security_scans_fail_closed_on_non_utf8_staged_blob(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    opaque = repository / "docs/opaque.md"
    opaque.parent.mkdir(parents=True)
    opaque.write_bytes(b"not utf-8: \xff\n")
    _track_release_files(repository)
    opaque.write_text("sanitized worktree\n", encoding="utf-8")
    protected = tmp_path / "protected"
    _write_private_inventory(protected, "private-source-in-index")
    _stub_external_holdout_leak_scan(monkeypatch)

    credential_check = _check_credentials(repository)
    path_check = _check_absolute_paths(repository)
    leak_check = _check_public_holdout_leak(
        repository,
        protected_holdout_root=protected,
    )

    assert credential_check.status is CheckStatus.FAIL
    assert credential_check.details == ("unreadable:docs/opaque.md",)
    assert path_check.status is CheckStatus.FAIL
    assert path_check.details == ("unreadable:docs/opaque.md",)
    assert leak_check.status is CheckStatus.FAIL
    assert leak_check.details == ("unreadable:docs/opaque.md",)


def test_security_scans_do_not_reinclude_a_staged_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    removed = repository / "docs/removed.md"
    removed.parent.mkdir(parents=True)
    removed.write_text("private-source-in-removed-file\n", encoding="utf-8")
    _track_release_files(repository)
    subprocess.run(
        ["git", "-C", str(repository), "rm", "--cached", "docs/removed.md"],
        check=True,
        capture_output=True,
    )
    protected = tmp_path / "protected"
    _write_private_inventory(protected, "private-source-in-removed-file")
    _stub_external_holdout_leak_scan(monkeypatch)

    leak_check = _check_public_holdout_leak(
        repository,
        protected_holdout_root=protected,
    )

    assert removed.is_file()
    assert leak_check.status is CheckStatus.PASS


def test_public_holdout_scan_rejects_gold_in_creator_visible_artifact(
    tmp_path: Path,
) -> None:
    inventory = tmp_path / "data/testset/protected/private/holdout-inventory.json"
    inventory.parent.mkdir(parents=True)
    inventory.write_text(
        json.dumps({"records": [{"source_id": "private-source-001"}]}),
        encoding="utf-8",
    )
    artifact = tmp_path / "data/skill-v0/creator/leaked.json"
    artifact.parent.mkdir(parents=True)
    record_key = "record_" + "type"
    record_value = "holdout_deterministic_" + "oracle"
    state_key = "state_" + "requirements"
    artifact.write_text(
        json.dumps(
            {
                record_key: record_value,
                state_key: [],
            }
        ),
        encoding="utf-8",
    )

    check = _check_public_holdout_leak(tmp_path)

    assert check.status is CheckStatus.FAIL
    assert check.details == ("data/skill-v0/creator/leaked.json",)


def test_public_holdout_scan_rejects_source_id_in_tracked_starter_markdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    leak = repository / "course/ch01-example/starter/notes.md"
    leak.parent.mkdir(parents=True)
    leak.write_text("debug identity: private-source-001\n", encoding="utf-8")
    _track_release_files(repository)
    protected = tmp_path / "protected"
    _write_private_inventory(protected, "private-source-001")
    _stub_external_holdout_leak_scan(monkeypatch)

    check = _check_public_holdout_leak(
        repository,
        protected_holdout_root=protected,
    )

    assert check.status is CheckStatus.FAIL
    assert check.details == ("course/ch01-example/starter/notes.md",)


def test_public_holdout_scan_rejects_hidden_gold_in_tracked_python(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    leak = repository / "release-support/private_probe.py"
    leak.parent.mkdir(parents=True)
    record_key = "record_" + "type"
    record_value = "holdout_deterministic_" + "oracle"
    leak.write_text(
        "payload = " + repr({record_key: record_value}) + "\n",
        encoding="utf-8",
    )
    _track_release_files(repository)
    protected = tmp_path / "protected"
    _write_private_inventory(protected, "private-source-001")
    _stub_external_holdout_leak_scan(monkeypatch)

    check = _check_public_holdout_leak(
        repository,
        protected_holdout_root=protected,
    )

    assert check.status is CheckStatus.FAIL
    assert check.details == ("release-support/private_probe.py",)


@pytest.mark.parametrize(
    "relative",
    (
        "course/ch01-example/solution/leak.py",
        "course/ch01-example/tests/test_leak.py",
        "scripts/leak.py",
    ),
)
def test_public_holdout_scan_does_not_skip_executable_release_directories(
    tmp_path: Path,
    relative: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    leak = repository / relative
    leak.parent.mkdir(parents=True)
    leak.write_text("SOURCE_ID = 'private-source-001'\n", encoding="utf-8")
    _track_release_files(repository)
    protected = tmp_path / "protected"
    _write_private_inventory(protected, "private-source-001")
    _stub_external_holdout_leak_scan(monkeypatch)

    check = _check_public_holdout_leak(
        repository,
        protected_holdout_root=protected,
    )

    assert check.status is CheckStatus.FAIL
    assert check.details == (relative,)


def test_public_holdout_scan_fails_closed_on_non_utf8_tracked_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    opaque = repository / "docs/opaque.md"
    opaque.parent.mkdir(parents=True)
    opaque.write_bytes(b"not utf-8: \xff\n")
    _track_release_files(repository)
    protected = tmp_path / "protected"
    _write_private_inventory(protected, "private-source-001")
    _stub_external_holdout_leak_scan(monkeypatch)

    check = _check_public_holdout_leak(
        repository,
        protected_holdout_root=protected,
    )

    assert check.status is CheckStatus.FAIL
    assert check.details == ("unreadable:docs/opaque.md",)


def test_public_holdout_scan_rejects_unapproved_tracked_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    opaque = repository / "assets/opaque.png"
    opaque.parent.mkdir(parents=True)
    opaque.write_bytes(b"\x89PNG\r\n\x1a\n\xff")
    _track_release_files(repository)
    protected = tmp_path / "protected"
    _write_private_inventory(protected, "private-source-001")
    _stub_external_holdout_leak_scan(monkeypatch)

    check = _check_public_holdout_leak(
        repository,
        protected_holdout_root=protected,
    )

    assert check.status is CheckStatus.FAIL
    assert check.details == ("unreadable:assets/opaque.png",)


def test_public_holdout_scan_ignores_untracked_local_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    tracked = repository / "README.md"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("public release\n", encoding="utf-8")
    _track_release_files(repository)
    untracked = repository / "scratch/private_probe.py"
    untracked.parent.mkdir(parents=True)
    untracked.write_text("SOURCE_ID = 'private-source-001'\n", encoding="utf-8")
    protected = tmp_path / "protected"
    _write_private_inventory(protected, "private-source-001")
    _stub_external_holdout_leak_scan(monkeypatch)

    check = _check_public_holdout_leak(
        repository,
        protected_holdout_root=protected,
    )

    assert check.status is CheckStatus.PASS


def test_public_holdout_scan_allows_only_one_known_schema_fixture_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    schema = repository / "src/ses/testset/holdout.py"
    schema.parent.mkdir(parents=True)
    record_key = "record_" + "type"
    record_value = "holdout_deterministic_" + "oracle"
    fixture_line = f'        "{record_key}": "{record_value}",\n'
    schema.write_text(fixture_line * 2, encoding="utf-8")
    _track_release_files(repository)
    protected = tmp_path / "protected"
    _write_private_inventory(protected, "private-source-001")
    _stub_external_holdout_leak_scan(monkeypatch)

    check = _check_public_holdout_leak(
        repository,
        protected_holdout_root=protected,
    )

    assert check.status is CheckStatus.FAIL
    assert check.details == ("src/ses/testset/holdout.py",)


def test_public_only_holdout_is_validated_but_remains_a_deviation(
    tmp_path: Path,
) -> None:
    protected = tmp_path / "data/testset/protected"
    shutil.copytree(
        _ROOT / "data/testset/protected",
        protected,
        ignore=shutil.ignore_patterns("private"),
    )

    check = _check_holdout(tmp_path)

    assert check.status is CheckStatus.DEVIATION
    assert "selection=6" in check.details
    assert "final=12" in check.details
    assert any("--protected-holdout-root" in detail for detail in check.details)
    assert _check_public_holdout_leak(tmp_path).status is CheckStatus.DEVIATION

    commitments = protected / "holdout-commitments.json"
    payload = json.loads(commitments.read_text(encoding="utf-8"))
    payload["selection_manifest_sha256"] = "0" * 64
    commitments.write_text(json.dumps(payload), encoding="utf-8")

    assert _check_holdout(tmp_path).status is CheckStatus.FAIL


def test_external_holdout_and_source_tar_reach_full_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    public = repository / "data/testset/protected"
    external = tmp_path / "external-protected"
    shutil.copytree(
        _ROOT / "data/testset/protected",
        public,
        ignore=shutil.ignore_patterns("private"),
    )
    shutil.copytree(_ROOT / "data/testset/protected", external)
    archive = tmp_path / "state-bench-source.tar.gz"
    archive.write_bytes(b"source-tar-test-double")
    captured: dict[str, object] = {}

    def fake_validate_holdout_bundle(**kwargs: object) -> HoldoutSummary:
        captured.update(kwargs)
        return HoldoutSummary(
            selection_count=6,
            final_count=12,
            inventory_sha256="1" * 64,
            selection_manifest_sha256="2" * 64,
            final_manifest_sha256="3" * 64,
        )

    monkeypatch.setattr(
        validator_module,
        "validate_holdout_bundle",
        fake_validate_holdout_bundle,
    )

    check = _check_holdout(
        repository,
        protected_holdout_root=external,
        state_bench_archive=archive,
    )

    assert check.status is CheckStatus.PASS
    assert captured["bundle_root"] == external.resolve()
    assert captured["archive_path"] == archive

    without_archive = _check_holdout(
        repository,
        protected_holdout_root=external,
    )
    assert without_archive.status is CheckStatus.DEVIATION
    assert any("--state-bench-archive" in item for item in without_archive.details)

    manifest = external / "selection-manifest.json"
    manifest.write_bytes(manifest.read_bytes() + b" ")
    assert (
        _check_holdout(
            repository,
            protected_holdout_root=external,
            state_bench_archive=archive,
        ).status
        is CheckStatus.FAIL
    )


def test_external_holdout_failure_does_not_echo_protected_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    public = repository / "data/testset/protected"
    external = tmp_path / "external-protected"
    shutil.copytree(
        _ROOT / "data/testset/protected",
        public,
        ignore=shutil.ignore_patterns("private"),
    )
    shutil.copytree(
        _ROOT / "data/testset/protected",
        external,
        ignore=shutil.ignore_patterns("private"),
    )
    archive = tmp_path / "state-bench-source.tar.gz"
    archive.write_bytes(b"source-tar-test-double")
    protected_identity = "synthetic-protected-source-identity"

    def fail_validation(**kwargs: object) -> HoldoutSummary:
        del kwargs
        raise ValueError(f"private inventory mismatch: {protected_identity}")

    monkeypatch.setattr(
        validator_module,
        "validate_holdout_bundle",
        fail_validation,
    )

    check = _check_holdout(
        repository,
        protected_holdout_root=external,
        state_bench_archive=archive,
    )

    assert check.status is CheckStatus.FAIL
    assert protected_identity not in json.dumps(check.as_dict())
    assert check.details == ("external_holdout_validation_failed",)


def test_command_evidence_requires_clean_room_and_locked_sync(tmp_path: Path) -> None:
    command = DocumentedCommand(
        lesson=1,
        readme="course/ch01/README.md",
        line=1,
        command="uv run pytest course/ch01/tests",
    )
    evidence = {
        "schema_version": "v1alpha1",
        "record_type": "clean_room_command_evidence",
        "environment_kind": "fresh_clone",
        "repository_commit": "1" * 40,
        "source_clean": True,
        "source_materialization": "git_archive_head_regular_files",
        "shell_grouping": "readme_fenced_blocks",
        "credential_environment_names": [],
        "locked_sync": {
            "command": "uv sync --all-extras --locked",
            "status": "passed",
        },
        "commands": [
            {
                "command_id": command.command_id,
                "command_sha256": command.sha256,
                "line": command.line,
                "readme": command.readme,
                "status": "passed",
                "exit_code": 0,
            }
        ],
    }
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")

    assert (
        _check_command_evidence(
            [command],
            path,
            expected_repository_commit="1" * 40,
        ).status
        is CheckStatus.PASS
    )

    evidence["environment_kind"] = "shared_worktree"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    assert (
        _check_command_evidence(
            [command],
            path,
            expected_repository_commit="1" * 40,
        ).status
        is CheckStatus.FAIL
    )


def test_command_evidence_rejects_a_stale_repository_commit(tmp_path: Path) -> None:
    command = DocumentedCommand(
        lesson=0,
        readme="README.md",
        line=1,
        command="uv run ses doctor",
    )
    evidence = {
        "schema_version": "v1alpha1",
        "record_type": "clean_room_command_evidence",
        "environment_kind": "fresh_temporary_copy",
        "repository_commit": "1" * 40,
        "source_clean": True,
        "source_materialization": "git_archive_head_regular_files",
        "shell_grouping": "readme_fenced_blocks",
        "credential_environment_names": [],
        "locked_sync": {
            "command": "uv sync --all-extras --locked",
            "status": "passed",
        },
        "commands": [
            {
                "command_id": command.command_id,
                "command_sha256": command.sha256,
                "line": command.line,
                "readme": command.readme,
                "status": "passed",
                "exit_code": 0,
            }
        ],
    }
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")

    check = _check_command_evidence(
        [command],
        path,
        expected_repository_commit="2" * 40,
    )

    assert check.status is CheckStatus.FAIL
    assert check.details == ("ValueError",)


def test_command_syntax_rejects_placeholder_and_checked_output(
    tmp_path: Path,
) -> None:
    app = tmp_path / "src/ses/cli"
    app.mkdir(parents=True)
    (app / "app.py").write_text('command = "skill-demo"\n', encoding="utf-8")
    command = DocumentedCommand(
        lesson=1,
        readme="course/ch01-example/README.md",
        line=1,
        command=(
            "uv run ses skill-demo --candidate ./my-skill "
            "--output course/ch01-example/artifact.json"
        ),
    )

    check = _check_command_syntax(tmp_path, [command])

    assert check.status is CheckStatus.FAIL
    assert any("placeholder" in detail for detail in check.details)
    assert any("checked release data" in detail for detail in check.details)


def test_public_wording_allows_prohibitions_but_rejects_a_claim(
    tmp_path: Path,
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "scope.md").write_text(
        "## Out of Scope\n\n- 使用真实企业生产日志。\n", encoding="utf-8"
    )
    readme = tmp_path / "README.md"
    readme.write_text("课程使用真实生产日志。\n", encoding="utf-8")

    check = _check_public_wording(tmp_path, {})

    assert check.status is CheckStatus.FAIL
    assert check.details == ("README.md:1",)

    readme.write_text("课程使用 benchmark 数据。\n", encoding="utf-8")
    assert _check_public_wording(tmp_path, {}).status is CheckStatus.PASS


def test_cost_classification_belongs_in_release_report(tmp_path: Path) -> None:
    lesson = tmp_path / "course/ch01-example"
    lesson.mkdir(parents=True)
    (lesson / "README.md").write_text("本课预算有明确边界。\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "这是一门面向学习者的课程。\n", encoding="utf-8"
    )
    report = tmp_path / "docs/release/release-report.md"
    report.parent.mkdir(parents=True)
    report.write_text(
        "measured canonical\nfixed\nestimated\nnoncanonical\n",
        encoding="utf-8",
    )

    assert _check_cost_classification(tmp_path, {1: lesson}).status is CheckStatus.PASS

    report.write_text("measured canonical\nfixed\nestimated\n", encoding="utf-8")
    check = _check_cost_classification(tmp_path, {1: lesson})

    assert check.status is CheckStatus.FAIL
    assert "release report does not identify noncanonical cost" in check.details


def test_historical_live_results_need_current_release_disclaimer(
    tmp_path: Path,
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    record = docs / "phase0-validation.md"
    record.write_text(
        "# Phase 0\n\n## 当前结论\n\n| Provider | PASS |\n",
        encoding="utf-8",
    )
    release_report = docs / "release/release-report.md"
    release_report.parent.mkdir()
    release_report.write_text("状态: live_not_rerun。\n", encoding="utf-8")

    check = _check_historical_live_provenance(tmp_path)

    assert check.status is CheckStatus.FAIL
    assert "historical section is still labeled current" in check.details


def test_historical_live_disclaimer_belongs_in_release_report(
    tmp_path: Path,
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "phase0-validation.md").write_text(
        "历史 smoke 记录。\n"
        "本轮 release 本轮未复测, 不能把本页当作当前证据。\n"
        "状态: live_not_rerun。\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("Live 端到端路径尚未完成。\n", encoding="utf-8")
    release_report = docs / "release/release-report.md"
    release_report.parent.mkdir()
    release_report.write_text("状态: live_not_rerun。\n", encoding="utf-8")

    assert _check_historical_live_provenance(tmp_path).status is CheckStatus.PASS

    release_report.write_text("Live 路径尚未完成。\n", encoding="utf-8")
    check = _check_historical_live_provenance(tmp_path)

    assert check.status is CheckStatus.FAIL
    assert "release report lacks the current live-not-rerun deviation" in check.details


def test_legacy_and_unsigned_review_claims_are_detected(tmp_path: Path) -> None:
    generated = tmp_path / "data/testset/ticket07/generated"
    generated.mkdir(parents=True)
    (generated / "qualification-manifest.jsonl").write_text(
        '{"reviewer":"ticket-owner"}\n', encoding="utf-8"
    )
    (generated / "review-packet.json").write_text(
        '[{"review_scope":"case","status":"approved"}]\n',
        encoding="utf-8",
    )

    creator = tmp_path / "data/skill-v0/creator/private/reviews"
    creator.mkdir(parents=True)
    (creator / "review-001.json").write_text(
        '{"reviewed_at":"2026-08-19T00:00:00Z"}\n',
        encoding="utf-8",
    )

    assert _legacy_develop_review_claims(tmp_path) == (
        "data/skill-v0/creator/private/reviews/review-001.json",
        "data/testset/ticket07/generated/qualification-manifest.jsonl",
        "data/testset/ticket07/generated/review-packet.json",
    )


def test_review_claim_scan_allows_pending_attestations_and_gate_rejections(
    tmp_path: Path,
) -> None:
    generated = tmp_path / "data/testset/ticket07/generated"
    generated.mkdir(parents=True)
    (generated / "qualification-manifest.jsonl").write_text(
        '{"course_attestation":{"status":"course_authored_pending_human_review"}}\n',
        encoding="utf-8",
    )
    registry = tmp_path / "course/ch09-example/artifacts/fixed-rejection"
    registry.mkdir(parents=True)
    (registry / "events.jsonl").write_text(
        '{"record_type":"registry_event","status":"rejected"}\n',
        encoding="utf-8",
    )

    assert _legacy_develop_review_claims(tmp_path) == ()


def test_review_claim_scan_does_not_read_executable_test_fixtures(
    tmp_path: Path,
) -> None:
    tests = tmp_path / "course/ch01-example/tests"
    tests.mkdir(parents=True)
    (tests / "attack.json").write_text(
        '{"reviewer":"delegated to Codex","status":"approved"}\n',
        encoding="utf-8",
    )

    assert _legacy_develop_review_claims(tmp_path) == ()


def test_full_data_regeneration_detects_reference_drift(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    bundle = tmp_path / "bundle"
    reference_dir = root / "course/ch05-mine-benchmark-data"
    upstream = root / "data/upstream"
    reference_dir.mkdir(parents=True)
    upstream.mkdir(parents=True)
    bundle.mkdir()
    upstream_manifest = upstream / "manifest.json"
    upstream_manifest.write_text("{}\n", encoding="utf-8")
    rows: list[dict[str, object]] = []
    for name, record_count in sorted(_EXPECTED_FULL_OUTPUT_RECORDS.items()):
        path = bundle / name
        payload = (
            json.dumps(_EXPECTED_FULL_FUNNEL, sort_keys=True) + "\n"
            if name == "funnel-counts.json"
            else "{}\n" * record_count
        )
        path.write_text(payload, encoding="utf-8")
        rows.append(
            {
                "bytes": len(payload.encode()),
                "path": name,
                "records": record_count,
                "sha256": hashlib.sha256(payload.encode()).hexdigest(),
            }
        )
    manifest = {
        "record_type": "candidate_artifact_manifest",
        "profile": "full",
        "seed": 0,
        "upstream_manifest_sha256": hashlib.sha256(b"{}\n").hexdigest(),
        "artifacts": rows,
    }
    manifest_path = bundle / "artifact-manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    reference = {
        "schema_version": "v1alpha1",
        "record_type": "lesson05_full_mining_reference",
        "profile": "full",
        "abcd": _EXPECTED_ABCD_SUMMARY,
        "tau2": _EXPECTED_TAU2_SUMMARY,
        "funnel": _EXPECTED_FULL_FUNNEL,
        "pipeline": {
            "artifact_manifest_sha256": hashlib.sha256(
                manifest_path.read_bytes()
            ).hexdigest(),
            "outputs": rows,
        },
    }
    reference_path = reference_dir / "full-funnel-reference.json"
    reference_path.write_text(json.dumps(reference, sort_keys=True), encoding="utf-8")
    repeated_bundle = tmp_path / "bundle-repeat"
    shutil.copytree(bundle, repeated_bundle)

    assert (
        _check_full_data_regeneration(root, [bundle, repeated_bundle]).status
        is CheckStatus.PASS
    )

    drift_rows = [dict(row) for row in rows]
    drift_rows[0]["sha256"] = "0" * 64
    drift_reference = {
        **reference,
        "pipeline": {
            "artifact_manifest_sha256": hashlib.sha256(
                manifest_path.read_bytes()
            ).hexdigest(),
            "outputs": drift_rows,
        },
    }
    reference_path.write_text(
        json.dumps(drift_reference, sort_keys=True), encoding="utf-8"
    )
    check = _check_full_data_regeneration(root, [bundle, repeated_bundle])

    assert check.status is CheckStatus.FAIL
    assert any("reference drift:" in detail for detail in check.details)


def test_repository_validation_is_path_safe_and_complete() -> None:
    report = validate_release(_ROOT)
    checks = {check.check_id: check for check in report.checks}

    assert report.lesson_count == 10
    assert report.documented_command_count > 0
    assert set(checks) == {
        "cost.classification",
        "course.structure",
        "course.tests",
        "course.transitions",
        "data.full_assets",
        "data.full_regeneration",
        "data.provenance",
        "data.split_isolation",
        "docs.command_execution",
        "docs.command_syntax",
        "docs.data_wording",
        "docs.local_links",
        "docs.live_provenance",
        "manual.packet_coverage",
        "manual.review_state",
        "prd.prelaunch_checklist",
        "reports.self_contained",
        "security.absolute_paths",
        "security.credentials",
        "security.public_holdout_leak",
    }
    assert checks["course.transitions"].status is CheckStatus.DEVIATION
    assert checks["docs.command_execution"].status is CheckStatus.DEVIATION
    assert checks["manual.packet_coverage"].status is CheckStatus.PASS
    assert (
        sum(
            detail.startswith("creator-seed-")
            for detail in checks["manual.review_state"].details
        )
        == 9
    )
    assert (
        sum(
            detail.startswith("develop-return-")
            for detail in checks["manual.review_state"].details
        )
        == 15
    )
    assert str(_ROOT).encode() not in report.json_bytes()
    assert report.json_bytes() == report.json_bytes()
