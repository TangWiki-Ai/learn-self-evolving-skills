"""Private auditor for Registry initialization and v0 evidence snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from types import MappingProxyType

from ses.contracts import (
    SELECTION_ITERATION_ID,
    ArtifactRef,
    ArtifactRoot,
    MeasurementKind,
    PairedComparison,
    RegistryEvent,
    RunEventType,
    RunRecord,
    SkillV0PipelineSummary,
    TriggerEvalResult,
    artifact_json_bytes,
)
from ses.evolution.registry_internal import RegistryError
from ses.evolution.registry_store import _RegistryStore
from ses.foundation.credentials import credential_values, is_sensitive_name, redact
from ses.skills.static_gate import (
    StaticGateReport,
    StaticGateStatus,
)


def _contains_secret_value(value: object) -> bool:
    if value is None or isinstance(value, (bool, int, float)):
        return False
    if isinstance(value, str):
        return value.strip().casefold() not in {
            "",
            "[redacted]",
            "redacted",
            "none",
            "not_present",
            "not-present",
            "scrubbed",
        }
    if isinstance(value, Mapping):
        return any(_contains_secret_value(child) for child in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_secret_value(child) for child in value)
    return True


def _contains_sensitive_field(value: object) -> bool:
    if isinstance(value, Mapping):
        return any(
            (
                isinstance(key, str)
                and is_sensitive_name(key)
                and _contains_secret_value(child)
            )
            or _contains_sensitive_field(child)
            for key, child in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_sensitive_field(child) for child in value)
    return False


class _InitialEvidenceAuditor:
    """Snapshot and deeply validate evidence for the initial accepted Skill."""

    def __init__(
        self,
        store: _RegistryStore,
        *,
        static_gate: Callable[[Path], StaticGateReport],
    ) -> None:
        self._store = store
        self._static_gate = static_gate
        self._run_log_cache: dict[str, tuple[RunRecord, ...]] = {}
        self._credential_structure_cache: set[str] = set()

    def validate_initial_evidence(
        self,
        evidence: Sequence[ArtifactRef],
        *,
        skill_hash: str,
    ) -> None:
        verified = False
        for reference in evidence:
            path = self._store.verify_ref(reference)
            content = path.read_bytes()
            self.assert_credential_free(content)
            try:
                SkillV0PipelineSummary.model_validate_json(content)
            except ValueError:
                continue
            self.pipeline_evidence_inventory(path, skill_hash=skill_hash)
            verified = True
        if not verified:
            raise RegistryError(
                "initial evidence does not identify a verified accepted Skill"
            )

    def verify_initial_event_evidence(self, event: RegistryEvent) -> None:
        verified = False
        for reference in event.evidence:
            path = self._store.verify_ref(reference)
            content = path.read_bytes()
            self.assert_credential_free(content)
            try:
                SkillV0PipelineSummary.model_validate_json(content)
            except ValueError:
                continue
            self.pipeline_evidence_inventory(path, skill_hash=event.version_sha256)
            verified = True
        if not verified:
            raise RegistryError(
                "registry initialization evidence does not verify its Skill"
            )

    def read_evidence_source(self, path: Path) -> bytes:
        if path.is_symlink() or not path.is_file():
            raise RegistryError("verification evidence must be a regular file")
        try:
            resolved = path.resolve(strict=True)
            if path.absolute() != resolved:
                raise RegistryError(
                    "verification evidence cannot contain symlink ancestors"
                )
            content = resolved.read_bytes()
        except OSError as exc:
            raise RegistryError("verification evidence cannot be read") from exc
        self.assert_credential_free(content)
        return content

    def assert_credential_free(self, content: bytes) -> None:
        text = content.decode("utf-8", errors="replace")
        if redact(text, credential_values(os.environ)) != text:
            raise RegistryError("verification evidence contains credentials")
        digest = hashlib.sha256(content).hexdigest()
        if digest in self._credential_structure_cache:
            return
        candidates: list[object] = []
        try:
            candidates.append(json.loads(text))
        except json.JSONDecodeError:
            for line in text.splitlines():
                if not line.strip():
                    continue
                try:
                    candidates.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        if any(_contains_sensitive_field(value) for value in candidates):
            raise RegistryError("verification evidence contains credential fields")
        self._credential_structure_cache.add(digest)

    @staticmethod
    def _artifact_references(value: object) -> tuple[ArtifactRef, ...]:
        references: list[ArtifactRef] = []

        def visit(child: object) -> None:
            if isinstance(child, Mapping):
                if {"root", "path", "sha256"}.issubset(child):
                    try:
                        references.append(
                            ArtifactRef.model_validate(
                                {
                                    "root": child["root"],
                                    "path": child["path"],
                                    "sha256": child["sha256"],
                                }
                            )
                        )
                    except ValueError:
                        pass
                for nested in child.values():
                    visit(nested)
            elif isinstance(child, (list, tuple)):
                for nested in child:
                    visit(nested)

        visit(value)
        return tuple(dict.fromkeys(references))

    @staticmethod
    def _pair_rooted_reference(
        reference: ArtifactRef,
        *,
        run_id: str,
    ) -> ArtifactRef:
        if PurePosixPath(reference.path).parts[:1] == (run_id,):
            return reference
        return reference.model_copy(update={"path": f"{run_id}/{reference.path}"})

    def capture_pipeline_evidence(
        self,
        summary_path: Path,
        summary: SkillV0PipelineSummary,
    ) -> Mapping[str, bytes]:
        """Read every referenced source artifact once into an immutable snapshot."""

        try:
            summary_root = summary_path.parent.resolve(strict=True)
        except OSError as exc:
            raise RegistryError("initial pipeline root cannot be resolved") from exc
        if summary_path.parent.absolute() != summary_root:
            raise RegistryError("initial pipeline evidence contains a symlink ancestor")
        inventory: dict[str, bytes] = {}

        def capture(reference: ArtifactRef) -> bytes:
            if reference.root is not ArtifactRoot.RUN:
                raise RegistryError("initial pipeline evidence must use its run root")
            previous = inventory.get(reference.path)
            if previous is not None:
                try:
                    reference.verify_bytes(previous)
                except ValueError as exc:
                    raise RegistryError(
                        "initial pipeline evidence path is ambiguous"
                    ) from exc
                return previous
            current = summary_path.parent
            for part in PurePosixPath(reference.path).parts:
                current = current / part
                if current.is_symlink():
                    raise RegistryError("initial pipeline evidence contains symlinks")
            try:
                resolved = current.resolve(strict=True)
                resolved.relative_to(summary_root)
                content = resolved.read_bytes()
                reference.verify_bytes(content)
            except (OSError, ValueError) as exc:
                raise RegistryError(
                    "initial pipeline evidence reference is invalid"
                ) from exc
            self.assert_credential_free(content)
            inventory[reference.path] = content
            return content

        static_content = capture(summary.static_gate_result)
        trigger_content = capture(summary.trigger_result)
        paired_content = capture(summary.paired_comparison)
        capture(summary.l2_html)
        try:
            StaticGateReport.model_validate_json(static_content)
            TriggerEvalResult.model_validate_json(trigger_content)
            paired = PairedComparison.model_validate_json(paired_content)
        except ValueError as exc:
            raise RegistryError("initial pipeline evidence record is invalid") from exc
        for run_id, content in (
            (paired.baseline_run_id, capture(paired.baseline_events)),
            (paired.skill_run_id, capture(paired.skill_events)),
        ):
            for record in self._parse_initial_run_log(content):
                for artifact_reference in self._artifact_references(
                    record.model_dump(mode="json")
                ):
                    capture(
                        self._pair_rooted_reference(
                            artifact_reference,
                            run_id=run_id,
                        )
                    )
        for case in paired.cases:
            for case_reference in (
                case.baseline_trace,
                case.skill_trace,
                case.baseline_state_diff,
                case.skill_state_diff,
                case.baseline_grade,
                case.skill_grade,
            ):
                if case_reference is not None:
                    capture(case_reference)
        if "summary.json" in inventory:
            raise RegistryError("initial pipeline evidence shadows its summary")
        return MappingProxyType(inventory)

    def pipeline_evidence_inventory(
        self,
        summary_path: Path,
        *,
        skill_hash: str,
    ) -> tuple[SkillV0PipelineSummary, Mapping[str, Path]]:
        """Validate and enumerate the complete v0 evidence chain."""

        try:
            if summary_path.is_symlink() or not summary_path.is_file():
                raise ValueError("pipeline summary is not a regular file")
            summary_root = summary_path.parent.resolve(strict=True)
            if summary_path.parent.absolute() != summary_root:
                raise ValueError("pipeline evidence contains a symlink ancestor")
            summary = SkillV0PipelineSummary.model_validate_json(
                summary_path.read_bytes()
            )
        except (OSError, UnicodeError, ValueError) as exc:
            raise RegistryError("initial pipeline summary is invalid") from exc
        inventory: dict[str, Path] = {}
        contents: dict[str, bytes] = {}

        def resolve(reference: ArtifactRef) -> Path:
            if reference.root is not ArtifactRoot.RUN:
                raise RegistryError("initial pipeline evidence must use its run root")
            cached = contents.get(reference.path)
            if cached is not None:
                try:
                    reference.verify_bytes(cached)
                except ValueError as exc:
                    raise RegistryError(
                        "initial pipeline evidence path is ambiguous"
                    ) from exc
                return inventory[reference.path]
            current = summary_path.parent
            for part in PurePosixPath(reference.path).parts:
                current = current / part
                if current.is_symlink():
                    raise RegistryError("initial pipeline evidence contains symlinks")
            try:
                resolved = current.resolve(strict=True)
                resolved.relative_to(summary_root)
                content = resolved.read_bytes()
                reference.verify_bytes(content)
            except (OSError, ValueError) as exc:
                raise RegistryError(
                    "initial pipeline evidence reference is invalid"
                ) from exc
            self.assert_credential_free(content)
            existing = inventory.get(reference.path)
            if existing is not None and existing != resolved:
                raise RegistryError("initial pipeline evidence path is ambiguous")
            inventory[reference.path] = resolved
            contents[reference.path] = content
            return resolved

        for reference in (
            summary.static_gate_result,
            summary.trigger_result,
            summary.paired_comparison,
            summary.l2_html,
        ):
            resolve(reference)
        try:
            static = StaticGateReport.model_validate_json(
                contents[summary.static_gate_result.path]
            )
            trigger = TriggerEvalResult.model_validate_json(
                contents[summary.trigger_result.path]
            )
            paired = PairedComparison.model_validate_json(
                contents[summary.paired_comparison.path]
            )
        except ValueError as exc:
            raise RegistryError("initial pipeline evidence record is invalid") from exc
        if summary.mode == "live":
            raise RegistryError(
                "live initial evidence requires a trusted external attestation"
            )
        expected_measurement = (
            MeasurementKind.SYNTHETIC_OFFLINE
            if summary.mode == "fixed"
            else MeasurementKind.LIVE_MEASURED
        )
        if (
            summary.skill_sha256 != skill_hash
            or summary.seed_count == 0
            or summary.static_gate != "pass"
            or summary.creator_measurement is not expected_measurement
            or summary.trigger_measurement is not expected_measurement
            or summary.paired_measurement is not expected_measurement
            or static.status is not StaticGateStatus.PASS
            or static.skill_sha256 != skill_hash
            or not static.checks
            or not all(check.passed for check in static.checks)
            or trigger.skill_sha256 != skill_hash
            or trigger.measurement_kind is not summary.trigger_measurement
            or trigger.precision != summary.trigger_precision
            or trigger.recall != summary.trigger_recall
            or paired.skill_sha256 != skill_hash
            or paired.measurement_kind is not summary.paired_measurement
            or len(paired.cases) != summary.paired_case_count
            or paired.baseline_pass_rate != summary.baseline_pass_rate
            or paired.skill_pass_rate != summary.skill_pass_rate
        ):
            raise RegistryError(
                "initial pipeline summary disagrees with its measured evidence"
            )
        nested_references = [paired.baseline_events, paired.skill_events]
        for case in paired.cases:
            nested_references.extend(
                reference
                for reference in (
                    case.baseline_trace,
                    case.skill_trace,
                    case.baseline_state_diff,
                    case.skill_state_diff,
                    case.baseline_grade,
                    case.skill_grade,
                )
                if reference is not None
            )
        for reference in nested_references:
            resolve(reference)
        baseline_records = self._parse_initial_run_log(
            contents[paired.baseline_events.path]
        )
        skill_records = self._parse_initial_run_log(contents[paired.skill_events.path])
        for run_id, records in (
            (paired.baseline_run_id, baseline_records),
            (paired.skill_run_id, skill_records),
        ):
            for record in records:
                for reference in self._artifact_references(
                    record.model_dump(mode="json")
                ):
                    resolve(self._pair_rooted_reference(reference, run_id=run_id))
        self._verify_initial_pair_runs(
            paired,
            baseline_records=baseline_records,
            skill_records=skill_records,
        )
        if "summary.json" in inventory:
            raise RegistryError("initial pipeline evidence shadows its summary")
        actual_files = {
            path.relative_to(summary_path.parent).as_posix()
            for path in summary_path.parent.rglob("*")
            if path.is_file()
        }
        if summary_path.name == "summary.json" and summary_path.parent.is_relative_to(
            self._store.root
        ):
            expected_files = set(inventory) | {"summary.json"}
            if actual_files != expected_files:
                raise RegistryError(
                    "stored pipeline evidence contains undeclared files"
                )
        return summary, MappingProxyType(inventory)

    def _parse_initial_run_log(self, content: bytes) -> tuple[RunRecord, ...]:
        digest = hashlib.sha256(content).hexdigest()
        cached = self._run_log_cache.get(digest)
        if cached is not None:
            return cached
        try:
            text = content.decode("utf-8")
        except UnicodeError as exc:
            raise RegistryError("initial paired event log is not UTF-8") from exc
        lines = text.splitlines()
        if not lines or text != "\n".join(lines) + "\n":
            raise RegistryError("initial paired event log is not canonical JSONL")
        records: list[RunRecord] = []
        for sequence, line in enumerate(lines):
            try:
                record = RunRecord.model_validate_json(line)
            except ValueError as exc:
                raise RegistryError("initial paired event log is invalid") from exc
            if (
                record.sequence != sequence
                or artifact_json_bytes(record).decode("utf-8") != line
            ):
                raise RegistryError("initial paired event sequence is invalid")
            records.append(record)
        parsed = tuple(records)
        self._run_log_cache[digest] = parsed
        return parsed

    @classmethod
    def _verify_initial_pair_runs(
        cls,
        paired: PairedComparison,
        *,
        baseline_records: tuple[RunRecord, ...],
        skill_records: tuple[RunRecord, ...],
    ) -> None:
        case_ids = tuple(row.case_id for row in paired.cases)
        starts: list[RunRecord] = []
        attempts_by_side: list[dict[str, RunRecord]] = []
        for expected_run_id, records in (
            (paired.baseline_run_id, baseline_records),
            (paired.skill_run_id, skill_records),
        ):
            if not records or records[0].event_type is not RunEventType.RUN_STARTED:
                raise RegistryError("initial paired run is missing run_started")
            if any(record.run_id != expected_run_id for record in records):
                raise RegistryError("initial paired run ID does not match its summary")
            started = records[0]
            config = started.config
            if config is None:
                raise RegistryError("initial paired run config is missing")
            protocol_payload = {
                "data_version": config.data_version,
                "model_lock_hash": config.model_lock_hash,
                "protocol_version": config.protocol_version,
                "case_ids": list(config.case_ids),
                "case_plan": list(config.case_plan),
                "iterations": config.iterations,
            }
            protocol_hash = hashlib.sha256(
                json.dumps(
                    protocol_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if (
                config.case_ids != case_ids
                or config.case_plan
                != tuple(f"{case_id}:{SELECTION_ITERATION_ID}" for case_id in case_ids)
                or config.iterations != 1
                or config.data_version != paired.data_version
                or config.model_lock_hash != paired.model_lock_sha256
                or protocol_hash != paired.protocol_sha256
            ):
                raise RegistryError("initial paired run config does not match summary")
            attempts: dict[str, RunRecord] = {}
            for record in records[1:]:
                if (
                    record.event_type is not RunEventType.ATTEMPT
                    or record.case_id is None
                    or record.iteration_id != SELECTION_ITERATION_ID
                    or record.usage is None
                    or record.case_id in attempts
                ):
                    raise RegistryError("initial paired attempts are incomplete")
                attempts[record.case_id] = record
            if tuple(attempts) != case_ids:
                raise RegistryError("initial paired attempts do not match case order")
            starts.append(started)
            attempts_by_side.append(attempts)
        baseline_config = starts[0].config
        skill_config = starts[1].config
        assert baseline_config is not None and skill_config is not None
        if (
            baseline_config.skill_hash != hashlib.sha256(b"").hexdigest()
            or skill_config.skill_hash != paired.skill_sha256
        ):
            raise RegistryError("initial paired Skill identity is invalid")
        for row in paired.cases:
            baseline = attempts_by_side[0][row.case_id]
            skill = attempts_by_side[1][row.case_id]
            baseline_usage = baseline.usage
            skill_usage = skill.usage
            assert baseline_usage is not None and skill_usage is not None
            if (
                baseline.status is not row.baseline_status
                or skill.status is not row.skill_status
                or baseline_usage.input_tokens != row.baseline_input_tokens
                or skill_usage.input_tokens != row.skill_input_tokens
                or baseline_usage.output_tokens != row.baseline_output_tokens
                or skill_usage.output_tokens != row.skill_output_tokens
                or baseline_usage.cost_amount != row.baseline_cost_amount
                or skill_usage.cost_amount != row.skill_cost_amount
                or baseline_usage.cost_currency != paired.cost_currency
                or skill_usage.cost_currency != paired.cost_currency
                or baseline.latency_ms != row.baseline_latency_ms
                or skill.latency_ms != row.skill_latency_ms
                or row.baseline_trace
                not in tuple(
                    cls._pair_rooted_reference(
                        reference,
                        run_id=paired.baseline_run_id,
                    )
                    for reference in baseline.artifacts.traces
                )
                or row.skill_trace
                not in tuple(
                    cls._pair_rooted_reference(
                        reference,
                        run_id=paired.skill_run_id,
                    )
                    for reference in skill.artifacts.traces
                )
                or row.baseline_state_diff
                != cls._optional_rooted(
                    baseline.artifacts.state_diff,
                    run_id=paired.baseline_run_id,
                )
                or row.skill_state_diff
                != cls._optional_rooted(
                    skill.artifacts.state_diff,
                    run_id=paired.skill_run_id,
                )
                or row.baseline_grade
                != cls._optional_rooted(
                    baseline.artifacts.grade,
                    run_id=paired.baseline_run_id,
                )
                or row.skill_grade
                != cls._optional_rooted(
                    skill.artifacts.grade,
                    run_id=paired.skill_run_id,
                )
            ):
                raise RegistryError(
                    "initial paired case does not match its event records"
                )

    @classmethod
    def _optional_rooted(
        cls,
        reference: ArtifactRef | None,
        *,
        run_id: str,
    ) -> ArtifactRef | None:
        return (
            None
            if reference is None
            else cls._pair_rooted_reference(reference, run_id=run_id)
        )

    def store_evidence(self, source: Path, content: bytes) -> ArtifactRef:
        self.assert_credential_free(content)
        try:
            summary = SkillV0PipelineSummary.model_validate_json(content)
        except ValueError:
            summary = None
        if summary is not None:
            return self._store_pipeline_evidence(source, summary, content)
        digest = hashlib.sha256(content).hexdigest()
        suffix = source.suffix if source.suffix in {".json", ".jsonl"} else ".bin"
        return self._store.store_object("evidence", digest + suffix, content)

    def _store_pipeline_evidence(
        self,
        source: Path,
        summary: SkillV0PipelineSummary,
        summary_content: bytes,
    ) -> ArtifactRef:
        inventory = self.capture_pipeline_evidence(source, summary)
        digest = hashlib.sha256(summary_content).hexdigest()
        target_parent = self._store.storage_directory("objects", "pipeline")
        target = target_parent / digest
        summary_target = target / "summary.json"
        if target.exists():
            if target.is_symlink() or not target.is_dir():
                raise RegistryError("stored pipeline evidence was tampered with")
            try:
                if summary_target.read_bytes() != summary_content:
                    raise RegistryError("stored pipeline summary already differs")
                for relative, content in inventory.items():
                    if (target / PurePosixPath(relative)).read_bytes() != content:
                        raise RegistryError("stored pipeline artifact already differs")
                self.pipeline_evidence_inventory(
                    summary_target,
                    skill_hash=summary.skill_sha256,
                )
            except OSError as exc:
                raise RegistryError("stored pipeline evidence cannot be read") from exc
            return self._store.ref(summary_target)
        staging = Path(tempfile.mkdtemp(prefix=".pipeline-", dir=target_parent))
        try:
            for relative, content in inventory.items():
                destination = staging / PurePosixPath(relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(content)
            (staging / "summary.json").write_bytes(summary_content)
            self.pipeline_evidence_inventory(
                staging / "summary.json",
                skill_hash=summary.skill_sha256,
            )
            os.replace(staging, target)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return self._store.ref(summary_target)

    def verify_initial_static_gate(self, source: Path, *, skill_hash: str) -> None:
        report = self._static_gate(source)
        if (
            report.status is not StaticGateStatus.PASS
            or report.skill_sha256 != skill_hash
            or not report.checks
            or not all(check.passed for check in report.checks)
        ):
            raise RegistryError("initial accepted Skill fails a fresh Static Gate")


__all__: tuple[str, ...] = ()
