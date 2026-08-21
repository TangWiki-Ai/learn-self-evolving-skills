# ruff: noqa: RUF001 -- Learner-facing Chinese copy intentionally uses CJK punctuation.
"""Evidence-producing business logic for the eight-station learner journey.

The learner CLI is live-first.  ``fixed`` exists only as an explicit test seam and
uses the repository's deterministic evaluator.  This module never reads tutor
credentials and never writes provider credentials to an artifact.
"""

from __future__ import annotations

import difflib
import hashlib
import html
import json
import os
import re
import shutil
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from pydantic import JsonValue

from ses.contracts import ArtifactRef, ArtifactRoot, Usage
from ses.contracts.security import validate_public_data
from ses.foundation.config import (
    ModelRole,
    ProviderId,
    load_model_lock,
    load_runtime_config,
)
from ses.foundation.credentials import read_provider_credentials
from ses.foundation.doctor import run_doctor
from ses.reporting.baseline import build_baseline_report
from ses.reporting.html_l1 import write_l1_html
from ses.runner import (
    BaselineRunner,
    BudgetLimits,
    DevelopCatalogEvaluator,
    LiveDevelopConfig,
    develop_catalog_sha256,
    load_develop_catalog,
)
from ses.runner.fake import ExecutableDevelopCase
from ses.skills.installer import (
    load_skill_manifest,
    normalized_skill_sha256,
    write_skill_manifest,
)
from ses.skills.static_gate import StaticGateStatus, run_static_gate

JourneyMode = Literal["live", "fixed"]
JourneyResultStatus = Literal["completed", "needs_attention"]

_EMPTY_HASH = hashlib.sha256(b"").hexdigest()
_STATION_NAMES = (
    "Execution & Monitoring",
    "Bad Case Mining",
    "Failure Analysis",
    "Skill Diagnosis",
    "Minimal Refinement",
    "Regression Evaluation",
    "Version Release & Rollback",
    "Summary",
)
_ATTRIBUTIONS = frozenset(
    {
        "environment",
        "case",
        "skill:knowledge",
        "skill:tool",
        "skill:clarification",
        "skill:style",
    }
)
_DIAGNOSES = frozenset(
    {"missing", "incomplete", "misleading", "not_effective", "rule_correct"}
)
_PASS = "pass"
_VALID_EVALUATION_STATUSES = frozenset({_PASS, "agent_fail"})
_CREDENTIAL_VALUE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{20,}\b", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret)\s*[:=]\s*[^\s,;]{12,}",
        re.IGNORECASE,
    ),
)
_SENSITIVE_ENV_NAME = re.compile(
    r"(?:^|_)(?:API_KEY|TOKEN|SECRET|PASSWORD|CREDENTIALS?)(?:$|_)",
    re.IGNORECASE,
)


class JourneyCourseError(RuntimeError):
    """A learner-actionable station failure."""


@dataclass(frozen=True, slots=True)
class StationRun:
    """One station result projected into the shared journey status store."""

    number: int
    status: JourneyResultStatus
    artifacts: tuple[Path, ...]
    decisions: tuple[Path, ...]
    usage: Usage
    metrics: Mapping[str, JsonValue]
    reason: str | None = None
    cached: bool = False


@dataclass(frozen=True, slots=True)
class GateEvaluation:
    """Deterministic two-door Gate projection over fresh case statuses."""

    rows: tuple[Mapping[str, JsonValue], ...]
    counts: Mapping[str, int]
    target_passed: bool
    target_pass_count: int
    full_regression_ran: bool
    regression_case_set_complete: bool
    regression_case_count: int
    candidate_pass_count: int
    target_regression_pass_count: int
    regression_passed: bool
    accepted: bool


def station_name(number: int) -> str:
    """Return the stable resume/interview phrase for one station."""

    try:
        return _STATION_NAMES[number]
    except IndexError as exc:
        raise JourneyCourseError("station must be between 0 and 7") from exc


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assert_no_credential_text(value: str) -> None:
    """Fail closed before learner-controlled text reaches a journey artifact."""

    if any(pattern.search(value) for pattern in _CREDENTIAL_VALUE_PATTERNS):
        raise JourneyCourseError("refusing to persist credential-like text")
    for name, secret in os.environ.items():
        if _SENSITIVE_ENV_NAME.search(name) and len(secret) >= 12 and secret in value:
            raise JourneyCourseError("refusing to persist credential-like text")


def _assert_no_credentials(value: object) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if isinstance(key, str):
                _assert_no_credential_text(key)
            _assert_no_credentials(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            _assert_no_credentials(child)
    elif isinstance(value, str):
        _assert_no_credential_text(value)


def artifact_ref(path: Path, *, workspace: Path) -> ArtifactRef:
    """Create a content-addressed workspace-relative reference."""

    resolved_workspace = workspace.resolve(strict=True)
    resolved = path.resolve(strict=True)
    try:
        relative = resolved.relative_to(resolved_workspace).as_posix()
    except ValueError as exc:
        raise JourneyCourseError("journey artifact escapes the workspace") from exc
    return ArtifactRef(
        root=ArtifactRoot.WORKSPACE,
        path=relative,
        sha256=_sha256(resolved),
    )


def _write_json(path: Path, value: object) -> Path:
    _assert_no_credentials(value)
    validate_public_data(value)
    payload = (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as stream:
        temporary = Path(stream.name)
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def _read_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise JourneyCourseError(f"cannot read journey artifact: {path.name}") from exc
    if not isinstance(value, dict):
        raise JourneyCourseError(f"journey artifact is not an object: {path.name}")
    validate_public_data(value)
    return cast(dict[str, object], value)


def _write_html(path: Path, *, title: str, body: str) -> Path:
    _assert_no_credential_text(title)
    _assert_no_credential_text(body)
    payload = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>:root{{font-family:ui-sans-serif,system-ui,sans-serif;color:#17211b;background:#f4f1e8}}body{{max-width:1120px;margin:auto;padding:32px}}h1{{font-size:clamp(2rem,5vw,4rem);line-height:1}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}}.card,table{{background:#fff;border:1px solid #d7d2c4;border-radius:12px}}.card{{padding:18px}}table{{width:100%;border-collapse:collapse;overflow:hidden}}th,td{{padding:12px;text-align:left;border-bottom:1px solid #e5e0d4}}th{{background:#162a22;color:#fff}}.pass,.improved{{color:#0a7445;font-weight:700}}.fail,.regressed{{color:#a33a2b;font-weight:700}}.muted{{color:#667068}}code,pre{{background:#ebe7dc;border-radius:6px;padding:.15rem .35rem}}a{{color:#075f53}}.bar{{height:.7rem;background:#d8d3c8;border-radius:999px;overflow:hidden}}.bar>i{{display:block;height:100%;background:#cf5b38}}</style></head><body><main><h1>{html.escape(title)}</h1>{body}</main></body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    return path


def _usage_from_report(report: Mapping[str, object]) -> Usage:
    totals = report.get("totals")
    if not isinstance(totals, Mapping):
        raise JourneyCourseError("baseline report has no usage totals")
    input_tokens = int(cast(int, totals.get("input_tokens", 0)))
    output_tokens = int(cast(int, totals.get("output_tokens", 0)))
    amount = Decimal(str(totals.get("cost_amount", "0")))
    currency = totals.get("cost_currency")
    if totals.get("cost_complete", True) is not True or not isinstance(currency, str):
        return Usage(input_tokens=input_tokens, output_tokens=output_tokens)
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_amount=amount,
        cost_currency=currency,
    )


def _merge_usage(*values: Usage) -> Usage:
    cost_complete = all(value.cost_amount is not None for value in values)
    currencies = {
        value.cost_currency for value in values if value.cost_currency is not None
    }
    if len(currencies) > 1:
        raise JourneyCourseError("station usage mixes cost currencies")
    amount = sum((value.cost_amount or Decimal(0) for value in values), Decimal(0))
    currency = next(iter(currencies)) if cost_complete and currencies else None
    return Usage(
        input_tokens=sum(value.input_tokens for value in values),
        output_tokens=sum(value.output_tokens for value in values),
        cost_amount=amount if currency is not None else None,
        cost_currency=currency,
    )


def journey_usage_from_reports(workspace: Path) -> Usage | None:
    """Rebuild cumulative experiment usage from unique persisted run reports."""

    reports = sorted(
        path
        for path in (workspace.resolve() / ".ses" / "runs").glob(
            "*/baseline-report.json"
        )
        if path.is_file() and not path.is_symlink()
    )
    if not reports:
        return None
    return _merge_usage(*(_usage_from_report(_read_object(path)) for path in reports))


def _report_cases(report: Mapping[str, object]) -> dict[str, str]:
    raw = report.get("cases")
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        raise JourneyCourseError("baseline report has no case results")
    results: dict[str, str] = {}
    for row in raw:
        if not isinstance(row, Mapping):
            continue
        case_id = row.get("case_id")
        status = row.get("first_status")
        if isinstance(case_id, str) and isinstance(status, str):
            results[case_id] = status
    return results


def _skill_files(source: Path) -> tuple[tuple[Path, str], ...]:
    manifest = load_skill_manifest(source)
    return tuple(
        (
            source / PurePosixPath(item.path),
            f"resolve-product-returns/{item.path}",
        )
        for item in manifest.files
    )


def _runtime(
    *,
    project_root: Path,
    mode: JourneyMode,
    timeout: float,
    provider: ProviderId,
) -> LiveDevelopConfig | None:
    if mode == "fixed":
        return None
    config = load_runtime_config(project_root / "ses.json")
    lock_path = project_root / config.models_lock_for(provider)
    lock = load_model_lock(lock_path)
    if lock.provider is not provider:
        raise JourneyCourseError("selected provider differs from its model lock")
    return LiveDevelopConfig(
        model=lock.roles[ModelRole.MAIN],
        credentials=read_provider_credentials(provider, os.environ),
        executable=config.claude_executable,
        environ=os.environ,
        timeout_seconds=timeout,
        provider=provider,
        model_lock_sha256=_sha256(lock_path),
        cost_currency="CNY" if provider is ProviderId.CHATANYWHERE else "USD",
    )


def _model_lock_hash(project_root: Path) -> str:
    return _sha256(project_root / "models.lock.json")


def _run_catalog(
    *,
    run_root: Path,
    run_id: str,
    case_ids: Sequence[str],
    catalog: Mapping[str, ExecutableDevelopCase],
    project_root: Path,
    live_config: LiveDevelopConfig | None,
    skill_source: Path | None,
) -> tuple[dict[str, object], Path, Path]:
    evaluator = DevelopCatalogEvaluator(
        catalog,
        skill_files=() if skill_source is None else _skill_files(skill_source),
        fixed_latency_ms=20 if live_config is None else None,
        live_config=live_config,
        cost_amount=Decimal("0.001") if live_config is None else Decimal(0),
    )
    existing = run_root / run_id / "events.jsonl"
    completed = BaselineRunner(run_root, evaluator).run(
        run_id=run_id,
        case_ids=tuple(case_ids),
        iterations=1,
        budgets=BudgetLimits(
            max_cases=len(case_ids),
            max_turns_per_case=3,
            cost_currency=(
                live_config.cost_currency if live_config is not None else "CNY"
            ),
        ),
        resume=existing.is_file(),
        data_version=develop_catalog_sha256(catalog),
        model_lock_hash=(
            live_config.model_lock_sha256
            if live_config is not None and live_config.model_lock_sha256 is not None
            else _model_lock_hash(project_root)
        ),
        skill_hash=(
            _EMPTY_HASH
            if skill_source is None
            else normalized_skill_sha256(skill_source)
        ),
        protocol_version="ses-one-day-journey-v1",
    )
    report = build_baseline_report(completed.events_path)
    report_path = completed.run_dir / "baseline-report.json"
    _write_json(report_path, report)
    html_path = completed.run_dir / "l1.html"
    write_l1_html(completed.events_path, html_path)
    return report, report_path, html_path


def _public_run_artifacts(run_directory: Path) -> tuple[Path, ...]:
    """List report-linked files that the read-only dashboard may serve."""

    public_suffixes = {".csv", ".html", ".htm", ".json", ".md", ".pdf", ".txt"}
    return tuple(
        sorted(
            path
            for path in run_directory.rglob("*")
            if path.is_file()
            and not path.is_symlink()
            and "workspaces" not in path.relative_to(run_directory).parts
            and path.suffix.casefold() in public_suffixes
        )
    )


def _evaluation_problem(
    statuses: Mapping[str, str], *, expected_case_ids: Sequence[str]
) -> str | None:
    expected = set(expected_case_ids)
    missing = expected - set(statuses)
    invalid = sorted(set(statuses.values()) - _VALID_EVALUATION_STATUSES)
    if not missing and not invalid:
        return None
    details: list[str] = []
    if missing:
        details.append(f"{len(missing)} case(s) missing")
    if invalid:
        details.append("non-outcome status: " + ", ".join(invalid))
    return "; ".join(details)


def _copy_initial_skill(*, project_root: Path, journey_root: Path) -> tuple[Path, Path]:
    source = project_root / "fixtures/seed/skill/v0"
    normalized_skill_sha256(source)
    working = journey_root / "skills/working"
    accepted = journey_root / "versions/v0"
    for destination in (working, accepted):
        if destination.exists():
            normalized_skill_sha256(destination)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination)
        normalized_skill_sha256(destination)
    return working, accepted


def _station_summary(journey_root: Path, number: int) -> Path:
    return journey_root / "evidence" / f"station-{number}.json"


def run_station_0(
    *,
    workspace: Path,
    project_root: Path,
    mode: JourneyMode,
    timeout: float,
    provider: ProviderId = ProviderId.SILICONFLOW,
) -> StationRun:
    """Run local checks, the full v0 baseline, and the fixed five-case control."""

    workspace = workspace.resolve()
    journey_root = workspace / ".ses"
    summary_path = _station_summary(journey_root, 0)
    if summary_path.is_file():
        summary = _read_object(summary_path)
        if summary.get("mode") != mode:
            raise JourneyCourseError(
                "station 0 evidence mode differs from this run; use a fresh workspace"
            )
        expected_provider = provider.value if mode == "live" else None
        if summary.get("provider") != expected_provider:
            raise JourneyCourseError(
                "station 0 evidence provider differs from this run; "
                "use a fresh workspace"
            )
        artifacts = (
            *(
                workspace / value
                for value in cast(list[str], summary.get("artifact_paths", []))
            ),
            summary_path,
        )
        usage = Usage.model_validate(summary.get("usage", {}))
        return StationRun(
            number=0,
            status="completed",
            artifacts=artifacts,
            decisions=(),
            usage=usage,
            metrics=cast(Mapping[str, JsonValue], summary.get("metrics", {})),
            cached=True,
        )

    project_root = project_root.resolve(strict=True)
    journey_root.mkdir(parents=True, exist_ok=True)
    doctor = run_doctor(
        project_root=project_root,
        config_path=project_root / "ses.json",
        live=False,
        timeout=timeout,
        environ=os.environ,
        provider=provider,
    )
    doctor_payload = {
        "checks": [
            {"detail": item.detail, "name": item.name, "status": item.status}
            for item in doctor
        ],
        "live_model_check": "covered_by_the_real_baseline"
        if mode == "live"
        else "fixed_ci_seam",
        "record_type": "journey_doctor",
        "schema_version": "v1alpha1",
    }
    doctor_path = _write_json(journey_root / "evidence/doctor.json", doctor_payload)
    if any(item.status == "FAIL" for item in doctor):
        return StationRun(
            number=0,
            status="needs_attention",
            artifacts=(doctor_path,),
            decisions=(),
            usage=Usage(input_tokens=0, output_tokens=0),
            metrics={},
            reason="doctor checks failed",
        )

    live_config = _runtime(
        project_root=project_root,
        mode=mode,
        timeout=timeout,
        provider=provider,
    )
    # The final Owner handoff authorizes this deterministic sandbox catalog for the
    # learner journey.  The old release/holdout APIs remain fail-closed.
    catalog = load_develop_catalog(mode="fixed")
    working, _accepted = _copy_initial_skill(
        project_root=project_root, journey_root=journey_root
    )
    gate = run_static_gate(
        working, audit_path=journey_root / "evidence/v0-static-gate.json"
    )
    if gate.status is not StaticGateStatus.PASS:
        raise JourneyCourseError("seed v0 failed its static gate")

    run_root = journey_root / "runs"
    suffix = f"live-{provider.value}" if mode == "live" else "fixed"
    baseline, baseline_json, _baseline_html = _run_catalog(
        run_root=run_root,
        run_id=f"run-journey-station0-v0-{suffix}",
        case_ids=tuple(catalog),
        catalog=catalog,
        project_root=project_root,
        live_config=live_config,
        skill_source=working,
    )
    baseline_cases = _report_cases(baseline)
    baseline_usage = _usage_from_report(baseline)
    baseline_artifacts = tuple(
        dict.fromkeys(
            (
                doctor_path,
                journey_root / "evidence/v0-static-gate.json",
                *_public_run_artifacts(baseline_json.parent),
            )
        )
    )
    baseline_problem = _evaluation_problem(
        baseline_cases, expected_case_ids=tuple(catalog)
    )
    if baseline_problem is not None:
        return StationRun(
            number=0,
            status="needs_attention",
            artifacts=baseline_artifacts,
            decisions=(),
            usage=baseline_usage,
            metrics={
                "experiment_mode": mode,
                "measurement_kind": (
                    "live_measured" if mode == "live" else "synthetic_offline"
                ),
            },
            reason=f"baseline incomplete; retry station 0 ({baseline_problem})",
        )
    sample_config = _read_object(
        project_root / "fixtures/seed/journey/no-skill-sample.json"
    )
    sample_ids = tuple(cast(list[str], sample_config.get("case_ids", [])))
    if len(sample_ids) != 5 or not set(sample_ids).issubset(catalog):
        raise JourneyCourseError("station-0 no-skill sample is invalid")
    control, control_json, _control_html = _run_catalog(
        run_root=run_root,
        run_id=f"run-journey-station0-no-skill-n5-{suffix}",
        case_ids=sample_ids,
        catalog=catalog,
        project_root=project_root,
        live_config=live_config,
        skill_source=None,
    )
    control_cases = _report_cases(control)
    control_usage = _usage_from_report(control)
    artifact_paths = tuple(
        dict.fromkeys(
            (
                *baseline_artifacts,
                *_public_run_artifacts(control_json.parent),
            )
        )
    )
    control_problem = _evaluation_problem(control_cases, expected_case_ids=sample_ids)
    if control_problem is not None:
        return StationRun(
            number=0,
            status="needs_attention",
            artifacts=artifact_paths,
            decisions=(),
            usage=_merge_usage(baseline_usage, control_usage),
            metrics={
                "baseline_case_count": len(baseline_cases),
                "baseline_pass_count": sum(
                    value == _PASS for value in baseline_cases.values()
                ),
                "experiment_mode": mode,
                "measurement_kind": (
                    "live_measured" if mode == "live" else "synthetic_offline"
                ),
            },
            reason=f"no-Skill control incomplete; retry station 0 ({control_problem})",
        )
    baseline_pass = sum(value == _PASS for value in baseline_cases.values())
    control_pass = sum(value == _PASS for value in control_cases.values())
    metrics: dict[str, JsonValue] = {
        "baseline_case_count": len(baseline_cases),
        "baseline_pass_count": baseline_pass,
        "baseline_pass_rate": baseline_pass / len(baseline_cases),
        "no_skill_case_count": len(control_cases),
        "no_skill_pass_count": control_pass,
        "no_skill_pass_rate": control_pass / len(control_cases),
        "no_skill_sample_label": "n=5 stratified sample; not the full develop set",
        "experiment_mode": mode,
        "measurement_kind": (
            "live_measured" if mode == "live" else "synthetic_offline"
        ),
    }
    usage = _merge_usage(baseline_usage, control_usage)
    _write_json(
        summary_path,
        {
            "artifact_paths": [
                path.relative_to(workspace).as_posix() for path in artifact_paths
            ],
            "catalog_use": "owner-authorized STATE-Bench sandbox journey",
            "metrics": metrics,
            "mode": mode,
            "provider": provider.value if mode == "live" else None,
            "record_type": "journey_station_summary",
            "schema_version": "v1alpha1",
            "station": 0,
            "usage": usage.model_dump(mode="json"),
            "working_skill": working.relative_to(workspace).as_posix(),
        },
    )
    return StationRun(
        number=0,
        status="completed",
        artifacts=(*artifact_paths, summary_path),
        decisions=(),
        usage=usage,
        metrics=metrics,
    )


def _baseline_summary(journey_root: Path) -> dict[str, object]:
    station = _read_object(_station_summary(journey_root, 0))
    paths = cast(list[str], station.get("artifact_paths", []))
    match = next(
        (path for path in paths if path.endswith("baseline-report.json")), None
    )
    if match is None:
        raise JourneyCourseError("station 0 baseline report is missing")
    return _read_object(journey_root.parent / match)


def _failure_rows(report: Mapping[str, object]) -> list[dict[str, object]]:
    raw = report.get("cases")
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        raise JourneyCourseError("station 0 case rows are unavailable")
    failures: list[dict[str, object]] = []
    for value in raw:
        if not isinstance(value, Mapping) or value.get("first_status") == _PASS:
            continue
        case_id = value.get("case_id")
        repetitions = value.get("repetitions")
        if not isinstance(case_id, str) or not isinstance(repetitions, Sequence):
            continue
        first = repetitions[0] if repetitions else {}
        artifacts: list[str] = []
        if isinstance(first, Mapping):
            raw_artifacts = first.get("artifacts")
            if isinstance(raw_artifacts, Mapping):
                for candidate in raw_artifacts.values():
                    refs = (
                        candidate if isinstance(candidate, Sequence) else (candidate,)
                    )
                    for reference in refs:
                        if isinstance(reference, Mapping) and isinstance(
                            reference.get("path"), str
                        ):
                            artifacts.append(cast(str, reference["path"]))
        failures.append(
            {
                "artifact_paths": artifacts,
                "case_id": case_id,
                "status": str(value.get("first_status")),
            }
        )
    return failures


def run_station_1(
    *, workspace: Path, selected_case_ids: Sequence[str] | None
) -> StationRun:
    """Render baseline failures and persist the learner's mining decision."""

    workspace = workspace.resolve()
    journey_root = workspace / ".ses"
    report = _baseline_summary(journey_root)
    failures = _failure_rows(report)
    failure_ids = {cast(str, row["case_id"]) for row in failures}
    evidence_path = _write_json(
        journey_root / "evidence/bad-cases.json",
        {
            "cases": failures,
            "record_type": "journey_bad_case_list",
            "schema_version": "v1alpha1",
            "source_station": 0,
        },
    )
    rows = "".join(
        f"<tr><td><code>{html.escape(cast(str, row['case_id']))}</code></td><td class='fail'>{html.escape(cast(str, row['status']))}</td><td>{len(cast(list[str], row['artifact_paths']))}</td></tr>"
        for row in failures
    )
    report_path = _write_html(
        journey_root / "reports/station-1-bad-cases.html",
        title="站 1 · Bad Case Mining",
        body=(
            f"<p>基线里有 <strong>{len(failures)}</strong> 条未通过。请回到讲师终端决定哪些进入分析。</p>"
            f"<table><thead><tr><th>Case</th><th>状态</th><th>证据文件数</th></tr></thead><tbody>{rows}</tbody></table>"
            if failures
            else "<div class='card'><p class='pass'>当前基线没有失败 case。你仍可把“本轮不立案”记录为一次真实决定。</p></div>"
        ),
    )
    if selected_case_ids is None:
        return StationRun(
            number=1,
            status="needs_attention",
            artifacts=(evidence_path, report_path),
            decisions=(),
            usage=Usage(input_tokens=0, output_tokens=0),
            metrics={"failure_count": len(failures)},
            reason=("choose cases with --select CASE_ID, or use --select none"),
        )
    selected = tuple(dict.fromkeys(selected_case_ids))
    unknown = sorted(set(selected) - failure_ids)
    if unknown:
        raise JourneyCourseError(f"selected cases are not baseline failures: {unknown}")
    decision_path = _write_json(
        journey_root / "decisions/station-1-selection.json",
        {
            "available_failure_count": len(failures),
            "record_type": "journey_bad_case_selection",
            "schema_version": "v1alpha1",
            "selected_case_ids": list(selected),
            "station": 1,
        },
    )
    metrics: dict[str, JsonValue] = {
        "failure_count": len(failures),
        "selected_count": len(selected),
    }
    summary_path = _write_json(
        _station_summary(journey_root, 1),
        {
            "artifact_paths": [
                evidence_path.relative_to(workspace).as_posix(),
                report_path.relative_to(workspace).as_posix(),
            ],
            "decision_paths": [decision_path.relative_to(workspace).as_posix()],
            "metrics": metrics,
            "record_type": "journey_station_summary",
            "schema_version": "v1alpha1",
            "station": 1,
            "usage": Usage(input_tokens=0, output_tokens=0).model_dump(mode="json"),
        },
    )
    return StationRun(
        number=1,
        status="completed",
        artifacts=(evidence_path, report_path, summary_path),
        decisions=(decision_path,),
        usage=Usage(input_tokens=0, output_tokens=0),
        metrics=metrics,
    )


def parse_assignments(values: Sequence[str]) -> dict[str, str]:
    """Parse repeatable ``CASE_ID=value`` CLI decisions."""

    parsed: dict[str, str] = {}
    for value in values:
        case_id, separator, selected = value.partition("=")
        if not separator or not case_id or not selected:
            raise JourneyCourseError("decisions must use CASE_ID=value")
        if case_id in parsed:
            raise JourneyCourseError(f"duplicate decision for {case_id}")
        parsed[case_id] = selected
    return parsed


def _selection(journey_root: Path) -> tuple[str, ...]:
    value = _read_object(journey_root / "decisions/station-1-selection.json")
    selected = value.get("selected_case_ids")
    if not isinstance(selected, list) or any(
        not isinstance(case_id, str) for case_id in selected
    ):
        raise JourneyCourseError("station 1 selection is invalid")
    return tuple(cast(list[str], selected))


def run_station_2(*, workspace: Path, assignments: Mapping[str, str]) -> StationRun:
    """Validate and visualize the learner's environment/case/Skill attribution."""

    workspace = workspace.resolve()
    journey_root = workspace / ".ses"
    selected = _selection(journey_root)
    if set(assignments) != set(selected):
        missing = sorted(set(selected) - set(assignments))
        extra = sorted(set(assignments) - set(selected))
        raise JourneyCourseError(
            f"attribution must cover each selected case; missing={missing}, extra={extra}"
        )
    invalid = sorted(set(assignments.values()) - _ATTRIBUTIONS)
    if invalid:
        raise JourneyCourseError(f"unsupported attribution labels: {invalid}")
    counts = Counter(assignments.values())
    decision_path = _write_json(
        journey_root / "decisions/station-2-attributions.json",
        {
            "labels": [
                {"case_id": case_id, "label": assignments[case_id]}
                for case_id in sorted(assignments)
            ],
            "record_type": "journey_failure_attributions",
            "schema_version": "v1alpha1",
            "station": 2,
        },
    )
    maximum = max(counts.values(), default=1)
    bars = "".join(
        f"<div class='card'><strong>{html.escape(label)}</strong><p>{count}</p><div class='bar'><i style='width:{100 * count / maximum:.1f}%'></i></div></div>"
        for label, count in sorted(counts.items())
    )
    report_path = _write_html(
        journey_root / "reports/station-2-attributions.html",
        title="站 2 · Failure Analysis",
        body=f"<p>你给 {len(assignments)} 条失败完成了归因。</p><section class='grid'>{bars}</section>",
    )
    metrics: dict[str, JsonValue] = {
        "attributed_count": len(assignments),
        "attribution_category_count": len(counts),
        "skill_attributed_count": sum(
            label.startswith("skill:") for label in assignments.values()
        ),
    }
    summary_path = _write_json(
        _station_summary(journey_root, 2),
        {
            "artifact_paths": [report_path.relative_to(workspace).as_posix()],
            "decision_paths": [decision_path.relative_to(workspace).as_posix()],
            "metrics": metrics,
            "record_type": "journey_station_summary",
            "schema_version": "v1alpha1",
            "station": 2,
            "usage": Usage(input_tokens=0, output_tokens=0).model_dump(mode="json"),
        },
    )
    return StationRun(
        number=2,
        status="completed",
        artifacts=(report_path, summary_path),
        decisions=(decision_path,),
        usage=Usage(input_tokens=0, output_tokens=0),
        metrics=metrics,
    )


def _attributions(journey_root: Path) -> dict[str, str]:
    value = _read_object(journey_root / "decisions/station-2-attributions.json")
    labels = value.get("labels")
    if not isinstance(labels, list):
        raise JourneyCourseError("station 2 attributions are invalid")
    result: dict[str, str] = {}
    for row in labels:
        if not isinstance(row, Mapping):
            raise JourneyCourseError("station 2 attribution row is invalid")
        case_id = row.get("case_id")
        label = row.get("label")
        if not isinstance(case_id, str) or not isinstance(label, str):
            raise JourneyCourseError("station 2 attribution row is incomplete")
        result[case_id] = label
    return result


def _parse_location(value: str, *, skill_root: Path) -> tuple[str, int]:
    relative, separator, line_value = value.rpartition(":")
    if not separator or not line_value.isdigit() or int(line_value) < 1:
        raise JourneyCourseError("locations must use relative/path.md:LINE")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        raise JourneyCourseError(
            "diagnosis location must stay inside the working Skill"
        )
    path = skill_root / pure
    if path.is_symlink() or not path.is_file():
        raise JourneyCourseError(f"diagnosis file does not exist: {relative}")
    try:
        path.resolve(strict=True).relative_to(skill_root.resolve(strict=True))
    except ValueError as exc:
        raise JourneyCourseError(
            "diagnosis location escapes the working Skill"
        ) from exc
    line = int(line_value)
    if line > len(path.read_text(encoding="utf-8").splitlines()):
        raise JourneyCourseError(f"diagnosis line is outside {relative}")
    return pure.as_posix(), line


def run_station_3(
    *,
    workspace: Path,
    diagnoses: Mapping[str, str],
    locations: Mapping[str, str],
) -> StationRun:
    """Bind Skill-attributed failures to a diagnosis label and source line."""

    workspace = workspace.resolve()
    journey_root = workspace / ".ses"
    skill_cases = {
        case_id
        for case_id, label in _attributions(journey_root).items()
        if label.startswith("skill:")
    }
    if set(diagnoses) != skill_cases or set(locations) != skill_cases:
        raise JourneyCourseError(
            "diagnosis and location must each cover every Skill-attributed case"
        )
    invalid = sorted(set(diagnoses.values()) - _DIAGNOSES)
    if invalid:
        raise JourneyCourseError(f"unsupported diagnosis labels: {invalid}")
    skill_root = journey_root / "skills/working"
    rows: list[dict[str, JsonValue]] = []
    for case_id in sorted(skill_cases):
        relative, line = _parse_location(locations[case_id], skill_root=skill_root)
        rows.append(
            {
                "case_id": case_id,
                "diagnosis": diagnoses[case_id],
                "location": {"line": line, "path": relative},
            }
        )
    decision_path = _write_json(
        journey_root / "decisions/station-3-diagnoses.json",
        {
            "diagnoses": rows,
            "record_type": "journey_skill_diagnoses",
            "schema_version": "v1alpha1",
            "station": 3,
        },
    )
    table_rows = "".join(
        f"<tr><td><code>{html.escape(cast(str, row['case_id']))}</code></td><td>{html.escape(cast(str, row['diagnosis']))}</td><td><code>{html.escape(cast(str, cast(dict[str, JsonValue], row['location'])['path']))}:{cast(dict[str, JsonValue], row['location'])['line']}</code></td></tr>"
        for row in rows
    )
    report_path = _write_html(
        journey_root / "reports/station-3-diagnoses.html",
        title="站 3 · Skill Diagnosis",
        body=(
            f"<table><thead><tr><th>Case</th><th>判断</th><th>定位</th></tr></thead><tbody>{table_rows}</tbody></table>"
            if rows
            else "<div class='card'><p>本轮没有失败归因到 Skill，因此没有文档病灶需要修改。</p></div>"
        ),
    )
    counts = Counter(diagnoses.values())
    metrics: dict[str, JsonValue] = {
        "diagnosed_count": len(rows),
        "diagnosis_category_count": len(counts),
        "rule_correct_count": counts["rule_correct"],
    }
    summary_path = _write_json(
        _station_summary(journey_root, 3),
        {
            "artifact_paths": [report_path.relative_to(workspace).as_posix()],
            "decision_paths": [decision_path.relative_to(workspace).as_posix()],
            "metrics": metrics,
            "record_type": "journey_station_summary",
            "schema_version": "v1alpha1",
            "station": 3,
            "usage": Usage(input_tokens=0, output_tokens=0).model_dump(mode="json"),
        },
    )
    return StationRun(
        number=3,
        status="completed",
        artifacts=(report_path, summary_path),
        decisions=(decision_path,),
        usage=Usage(input_tokens=0, output_tokens=0),
        metrics=metrics,
    )


def _diagnosis_rows(journey_root: Path) -> list[dict[str, object]]:
    value = _read_object(journey_root / "decisions/station-3-diagnoses.json")
    rows = value.get("diagnoses")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise JourneyCourseError("station 3 diagnoses are invalid")
    return cast(list[dict[str, object]], rows)


def _runtime_file_names(source: Path) -> tuple[str, ...]:
    return tuple(item.path for item in load_skill_manifest(source).files)


def _skill_diff(before: Path, after: Path, files: Sequence[str]) -> str:
    chunks: list[str] = []
    for relative in files:
        old = (
            (before / PurePosixPath(relative)).read_text(encoding="utf-8").splitlines()
        )
        new = (after / PurePosixPath(relative)).read_text(encoding="utf-8").splitlines()
        chunks.extend(
            difflib.unified_diff(
                old,
                new,
                fromfile=f"v0/{relative}",
                tofile=f"candidate/{relative}",
                lineterm="",
            )
        )
    return "\n".join(chunks)


def _snapshot_candidate(
    *, journey_root: Path, rationale: str
) -> tuple[Path, str, str, int, str]:
    accepted = journey_root / "versions/v0"
    working = journey_root / "skills/working"
    manifest = load_skill_manifest(accepted)
    files = _runtime_file_names(accepted)
    diff = _skill_diff(accepted, working, files)
    candidates_root = journey_root / "candidates"
    candidates_root.mkdir(parents=True, exist_ok=True)
    previous = sorted(
        path
        for path in candidates_root.iterdir()
        if path.is_dir() and not path.is_symlink()
    )
    round_number = len(previous) + 1
    temporary = Path(tempfile.mkdtemp(prefix=".candidate-", dir=candidates_root))
    try:
        for relative in files:
            source = working / PurePosixPath(relative)
            if source.is_symlink() or not source.is_file():
                raise JourneyCourseError(f"working Skill file is invalid: {relative}")
            destination = temporary / PurePosixPath(relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination, follow_symlinks=False)
        write_skill_manifest(
            temporary,
            name=manifest.name,
            version=f"v{round_number}",
            files=files,
            source_version=f"one-day-journey-round-{round_number}",
            provider_compatibility=manifest.provider_compatibility,
        )
        candidate_hash = normalized_skill_sha256(temporary)
        destination = candidates_root / f"candidate-{candidate_hash[:16]}"
        if destination.exists():
            normalized_skill_sha256(destination)
            shutil.rmtree(temporary)
        else:
            os.replace(temporary, destination)
        candidate = destination
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    parent_hash = normalized_skill_sha256(accepted)
    pointer = {
        "candidate_path": candidate.relative_to(journey_root.parent).as_posix(),
        "candidate_skill_sha256": candidate_hash,
        "parent_skill_sha256": parent_hash,
        "rationale": rationale,
        "record_type": "journey_candidate_pointer",
        "round_number": round_number,
        "schema_version": "v1alpha1",
    }
    _write_json(journey_root / "current-candidate.json", pointer)
    return candidate, parent_hash, candidate_hash, round_number, diff


def run_station_4(*, workspace: Path, rationale: str) -> StationRun:
    """Snapshot the edited working Skill and render an evidence-linked diff."""

    workspace = workspace.resolve()
    journey_root = workspace / ".ses"
    _assert_no_credential_text(rationale)
    before = journey_root / "versions/v0"
    working = journey_root / "skills/working"
    files = _runtime_file_names(before)
    for relative in files:
        source = working / PurePosixPath(relative)
        try:
            content = source.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise JourneyCourseError(
                f"working Skill file cannot be scanned safely: {relative}"
            ) from exc
        _assert_no_credential_text(content)
    rows = _diagnosis_rows(journey_root)
    requires_change = any(row.get("diagnosis") != "rule_correct" for row in rows)
    if requires_change and not rationale.strip():
        raise JourneyCourseError("a non-empty --rationale is required for this patch")
    preview_diff = _skill_diff(before, working, files)
    if requires_change and not preview_diff:
        report_path = _write_html(
            journey_root / "reports/station-4-diff.html",
            title="站 4 · Minimal Refinement",
            body="<div class='card'><p class='fail'>诊断要求修改 Skill，但工作副本没有变化。</p></div>",
        )
        return StationRun(
            number=4,
            status="needs_attention",
            artifacts=(report_path,),
            decisions=(),
            usage=Usage(input_tokens=0, output_tokens=0),
            metrics={"changed_line_count": 0},
            reason=f"edit {working.relative_to(workspace).as_posix()} before retrying",
        )
    candidate, parent_hash, candidate_hash, round_number, diff = _snapshot_candidate(
        journey_root=journey_root,
        rationale=rationale.strip() or "no Skill change warranted",
    )
    static_path = journey_root / f"evidence/station-4-static-{candidate_hash[:12]}.json"
    static = run_static_gate(candidate, audit_path=static_path)
    changed_lines = sum(
        line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
        for line in diff.splitlines()
    )
    decision_path = _write_json(
        journey_root / f"decisions/station-4-patch-{candidate_hash[:12]}.json",
        {
            "candidate_skill_sha256": candidate_hash,
            "changed_line_count": changed_lines,
            "parent_skill_sha256": parent_hash,
            "rationale": rationale.strip() or "no Skill change warranted",
            "record_type": "journey_minimal_refinement",
            "round_number": round_number,
            "schema_version": "v1alpha1",
            "station": 4,
        },
    )
    report_path = _write_html(
        journey_root / "reports/station-4-diff.html",
        title="站 4 · Minimal Refinement",
        body=(
            f"<section class='grid'><div class='card'><span class='muted'>轮次</span><h2>{round_number}</h2></div><div class='card'><span class='muted'>变化行</span><h2>{changed_lines}</h2></div><div class='card'><span class='muted'>静态 Gate</span><h2>{html.escape(static.status.value)}</h2></div></section>"
            f"<h2>Diff</h2><pre>{html.escape(diff or '(no runtime text change)')}</pre>"
        ),
    )
    metrics: dict[str, JsonValue] = {
        "candidate_round": round_number,
        "changed_line_count": changed_lines,
        "static_gate": static.status.value,
    }
    summary_path = _write_json(
        _station_summary(journey_root, 4),
        {
            "artifact_paths": [
                report_path.relative_to(workspace).as_posix(),
                static_path.relative_to(workspace).as_posix(),
                candidate.relative_to(workspace).as_posix(),
            ],
            "decision_paths": [decision_path.relative_to(workspace).as_posix()],
            "metrics": metrics,
            "record_type": "journey_station_summary",
            "schema_version": "v1alpha1",
            "station": 4,
            "usage": Usage(input_tokens=0, output_tokens=0).model_dump(mode="json"),
        },
    )
    status: JourneyResultStatus = (
        "completed" if static.status is StaticGateStatus.PASS else "needs_attention"
    )
    return StationRun(
        number=4,
        status=status,
        artifacts=(report_path, static_path, summary_path),
        decisions=(decision_path,),
        usage=Usage(input_tokens=0, output_tokens=0),
        metrics=metrics,
        reason=None if status == "completed" else "candidate failed the static gate",
    )


def _candidate_pointer(journey_root: Path) -> dict[str, object]:
    return _read_object(journey_root / "current-candidate.json")


def _candidate_path(*, workspace: Path, journey_root: Path) -> Path:
    pointer = _candidate_pointer(journey_root)
    value = pointer.get("candidate_path")
    if not isinstance(value, str):
        raise JourneyCourseError("current candidate pointer is invalid")
    path = workspace / PurePosixPath(value)
    normalized_skill_sha256(path)
    return path


def _pair_category(accepted: str, candidate: str) -> str:
    if accepted == _PASS and candidate == _PASS:
        return "both-pass"
    if accepted == _PASS:
        return "pass-to-fail"
    if candidate == _PASS:
        return "fail-to-pass"
    return "both-fail"


def evaluate_two_door_gate(
    *,
    accepted_cases: Mapping[str, str],
    target_statuses: Mapping[str, str],
    regression_statuses: Mapping[str, str],
    target_case_ids: Sequence[str],
) -> GateEvaluation:
    """Require every target to pass and every prior pass to avoid regression."""

    targets = tuple(dict.fromkeys(target_case_ids))
    accepted_case_ids = set(accepted_cases)
    target_pass_count = sum(
        target_statuses.get(case_id) == _PASS for case_id in targets
    )
    target_passed = (
        bool(targets)
        and set(targets) <= accepted_case_ids
        and target_pass_count == len(targets)
    )
    full_regression_ran = bool(regression_statuses)
    regression_case_set_complete = (
        full_regression_ran and set(regression_statuses) == accepted_case_ids
    )
    regression_case_count = len(regression_statuses)
    candidate_pass_count = sum(
        regression_statuses.get(case_id) == _PASS for case_id in accepted_cases
    )
    target_regression_pass_count = sum(
        regression_statuses.get(case_id) == _PASS for case_id in targets
    )
    rows: list[Mapping[str, JsonValue]] = []
    if full_regression_ran:
        for case_id, accepted in sorted(accepted_cases.items()):
            candidate_status = regression_statuses.get(case_id, "not_evaluated")
            rows.append(
                {
                    "accepted_status": accepted,
                    "candidate_status": candidate_status,
                    "case_id": case_id,
                    "category": _pair_category(accepted, candidate_status),
                    "target": case_id in targets,
                }
            )
    counts = Counter(cast(str, row["category"]) for row in rows)
    targets_passed_in_regression = not targets or target_regression_pass_count == len(
        targets
    )
    regression_passed = (
        full_regression_ran
        and regression_case_set_complete
        and targets_passed_in_regression
        and counts["pass-to-fail"] == 0
    )
    complete_counts = {
        category: counts[category]
        for category in ("both-pass", "both-fail", "fail-to-pass", "pass-to-fail")
    }
    return GateEvaluation(
        rows=tuple(rows),
        counts=complete_counts,
        target_passed=target_passed,
        target_pass_count=target_pass_count,
        full_regression_ran=full_regression_ran,
        regression_case_set_complete=regression_case_set_complete,
        regression_case_count=regression_case_count,
        candidate_pass_count=candidate_pass_count,
        target_regression_pass_count=target_regression_pass_count,
        regression_passed=regression_passed,
        accepted=target_passed and regression_passed,
    )


def run_station_5(
    *,
    workspace: Path,
    project_root: Path,
    mode: JourneyMode,
    timeout: float,
    decision: str,
    provider: ProviderId = ProviderId.SILICONFLOW,
) -> StationRun:
    """Replay selected targets, run full regression, and enforce a two-door Gate."""

    if decision not in {"follow-gate", "refine", "hold"}:
        raise JourneyCourseError(
            "station 5 decision must be follow-gate, refine, or hold"
        )
    workspace = workspace.resolve()
    project_root = project_root.resolve(strict=True)
    journey_root = workspace / ".ses"
    pointer = _candidate_pointer(journey_root)
    candidate = _candidate_path(workspace=workspace, journey_root=journey_root)
    candidate_hash = normalized_skill_sha256(candidate)
    parent_hash = normalized_skill_sha256(journey_root / "versions/v0")
    if pointer.get("candidate_skill_sha256") != candidate_hash:
        raise JourneyCourseError("current candidate pointer hash is invalid")
    if pointer.get("parent_skill_sha256") != parent_hash:
        raise JourneyCourseError("current candidate parent hash is invalid")
    candidate_changed = candidate_hash != parent_hash
    accepted_report = _baseline_summary(journey_root)
    if mode == "live":
        station_0 = _read_object(_station_summary(journey_root, 0))
        if station_0.get("provider") != provider.value:
            raise JourneyCourseError(
                "station 5 provider differs from the station 0 baseline"
            )
    accepted_cases = _report_cases(accepted_report)
    attributions = _attributions(journey_root)
    targets = tuple(
        case_id
        for case_id in _selection(journey_root)
        if attributions.get(case_id, "").startswith("skill:")
    )
    catalog = load_develop_catalog(mode="fixed")
    if set(accepted_cases) != set(catalog):
        raise JourneyCourseError(
            "station 0 baseline does not cover the full regression catalog"
        )
    live_config = _runtime(
        project_root=project_root,
        mode=mode,
        timeout=timeout,
        provider=provider,
    )
    suffix = f"live-{provider.value}" if mode == "live" else "fixed"
    run_root = journey_root / "runs"
    artifacts: list[Path] = []
    target_usage = Usage(input_tokens=0, output_tokens=0)
    regression_usage = Usage(input_tokens=0, output_tokens=0)
    target_statuses: dict[str, str] = {}
    regression_statuses: dict[str, str] = {}
    target_passed = False
    if targets:
        target_report, target_json, _target_html = _run_catalog(
            run_root=run_root,
            run_id=f"run-journey-station5-target-{candidate_hash[:12]}-{suffix}",
            case_ids=targets,
            catalog=catalog,
            project_root=project_root,
            live_config=live_config,
            skill_source=candidate,
        )
        artifacts.extend(_public_run_artifacts(target_json.parent))
        target_usage = _usage_from_report(target_report)
        target_statuses = _report_cases(target_report)
        target_passed = all(
            target_statuses.get(case_id) == _PASS for case_id in targets
        )
        if target_passed:
            regression_report, regression_json, _regression_html = _run_catalog(
                run_root=run_root,
                run_id=f"run-journey-station5-regression-{candidate_hash[:12]}-{suffix}",
                case_ids=tuple(catalog),
                catalog=catalog,
                project_root=project_root,
                live_config=live_config,
                skill_source=candidate,
            )
            artifacts.extend(_public_run_artifacts(regression_json.parent))
            regression_usage = _usage_from_report(regression_report)
            regression_statuses = _report_cases(regression_report)
    evaluation = evaluate_two_door_gate(
        accepted_cases=accepted_cases,
        target_statuses=target_statuses,
        regression_statuses=regression_statuses,
        target_case_ids=targets,
    )
    rows = evaluation.rows
    counts = evaluation.counts
    target_passed = evaluation.target_passed
    regression_passed = evaluation.regression_passed
    gate_accepted = evaluation.accepted and candidate_changed
    outcome = "accepted" if gate_accepted else "rejected"
    rejection_reasons: list[str] = []
    if not targets:
        rejection_reasons.append("no_skill_targets")
    elif not target_passed:
        rejection_reasons.append("target_replay_failed")
    if not candidate_changed:
        rejection_reasons.append("candidate_has_no_runtime_change")
    if evaluation.full_regression_ran:
        if not evaluation.regression_case_set_complete:
            rejection_reasons.append("full_regression_case_set_incomplete")
        if evaluation.target_regression_pass_count != len(targets):
            rejection_reasons.append("target_failed_in_full_regression")
        if counts["pass-to-fail"]:
            rejection_reasons.append("prior_pass_regressed")
    elif target_passed:
        rejection_reasons.append("full_regression_not_run")
    gate_payload = {
        "candidate_changed": candidate_changed,
        "candidate_skill_sha256": candidate_hash,
        "candidate_pass_count": evaluation.candidate_pass_count,
        "decision": decision,
        "doors": {
            "1_target_replay": {
                "case_ids": list(targets),
                "passed": target_passed,
                "statuses": target_statuses,
                "target_pass_count": evaluation.target_pass_count,
            },
            "2_full_regression": {
                "candidate_pass_count": evaluation.candidate_pass_count,
                "case_set_complete": evaluation.regression_case_set_complete,
                "expected_case_count": len(accepted_cases),
                "pass_to_fail_count": counts["pass-to-fail"],
                "passed": regression_passed,
                "ran": evaluation.full_regression_ran,
                "regression_case_count": evaluation.regression_case_count,
                "target_pass_count": evaluation.target_regression_pass_count,
            },
        },
        "full_regression_ran": evaluation.full_regression_ran,
        "measurement_kind": "live_measured" if mode == "live" else "synthetic_offline",
        "mode": mode,
        "provider": provider.value if mode == "live" else None,
        "outcome": outcome,
        "parent_skill_sha256": parent_hash,
        "record_type": "journey_gate_report",
        "regression_case_count": evaluation.regression_case_count,
        "rejection_reasons": rejection_reasons,
        "rows": rows,
        "schema_version": "v1alpha1",
        "usage": _merge_usage(target_usage, regression_usage).model_dump(mode="json"),
    }
    gate_path = _write_json(
        journey_root / f"evidence/gate-{candidate_hash[:12]}.json", gate_payload
    )
    decision_path = _write_json(
        journey_root / f"decisions/station-5-{candidate_hash[:12]}.json",
        {
            "candidate_skill_sha256": candidate_hash,
            "gate_outcome": outcome,
            "learner_decision": decision,
            "parent_skill_sha256": parent_hash,
            "record_type": "journey_regression_decision",
            "schema_version": "v1alpha1",
            "station": 5,
        },
    )
    table_rows = "".join(
        f"<tr><td>{'目标 · ' if row['target'] else ''}<code>{html.escape(cast(str, row['case_id']))}</code></td><td>{html.escape(cast(str, row['accepted_status']))}</td><td>{html.escape(cast(str, row['candidate_status']))}</td><td class='{'regressed' if row['category'] == 'pass-to-fail' else 'improved' if row['category'] == 'fail-to-pass' else ''}'>{html.escape(cast(str, row['category']))}</td></tr>"
        for row in rows
    )
    target_rows = "".join(
        f"<tr><td><code>{html.escape(case_id)}</code></td><td class='{'pass' if target_statuses.get(case_id) == _PASS else 'fail'}'>{html.escape(target_statuses.get(case_id, 'not_evaluated'))}</td></tr>"
        for case_id in targets
    )
    target_detail = (
        f"<h2>门 1 · 目标状态</h2><table><thead><tr><th>目标 Case</th><th>Fresh 状态</th></tr></thead><tbody>{target_rows}</tbody></table>"
        if targets
        else "<div class='card'><p>本轮没有归因到 Skill 的目标 case，门 1 无法成立。</p></div>"
    )
    if evaluation.full_regression_ran:
        regression_detail = (
            f"<h2>门 2 · 全量明细</h2><p>候选通过 {evaluation.candidate_pass_count}/{len(accepted_cases)}；"
            f"实际回归 {evaluation.regression_case_count}/{len(accepted_cases)} 条。</p>"
            f"<table><thead><tr><th>Case</th><th>v0</th><th>候选</th><th>变化</th></tr></thead><tbody>{table_rows}</tbody></table>"
        )
    elif targets:
        regression_detail = (
            "<div class='card'><p>门 1 没有全部通过，因此门 2 尚未运行。</p></div>"
        )
    else:
        regression_detail = (
            "<div class='card'><p>没有 Skill 目标，门 2 尚未运行。</p></div>"
        )
    door_2_label = (
        "未运行"
        if not evaluation.full_regression_ran
        else "通过"
        if regression_passed
        else "未通过"
    )
    unchanged_detail = (
        "<div class='card'><p class='fail'>候选与父版本没有运行时变化，不能被 Gate 接受或发布。</p></div>"
        if not candidate_changed
        else ""
    )
    report_path = _write_html(
        journey_root / "reports/station-5-gate.html",
        title="站 5 · Regression Evaluation",
        body=(
            f"<section class='grid'><div class='card'><span class='muted'>门 1 · 目标回放</span><h2>{'通过' if target_passed else '未通过'}</h2></div><div class='card'><span class='muted'>门 2 · 全量回归</span><h2>{door_2_label}</h2></div><div class='card'><span class='muted'>Gate</span><h2 class='{'pass' if gate_accepted else 'fail'}'>{outcome}</h2></div></section>"
            + target_detail
            + regression_detail
            + unchanged_detail
        ),
    )
    artifacts.extend((gate_path, report_path))
    metrics: dict[str, JsonValue] = {
        "both_pass_count": counts["both-pass"],
        "candidate_changed": candidate_changed,
        "candidate_pass_count": evaluation.candidate_pass_count,
        "expected_regression_case_count": len(accepted_cases),
        "fail_to_pass_count": counts["fail-to-pass"],
        "full_regression_ran": evaluation.full_regression_ran,
        "gate_outcome": outcome,
        "pass_to_fail_count": counts["pass-to-fail"],
        "regression_case_count": evaluation.regression_case_count,
        "regression_case_set_complete": evaluation.regression_case_set_complete,
        "target_count": len(targets),
        "target_pass_count": evaluation.target_pass_count,
        "target_regression_pass_count": evaluation.target_regression_pass_count,
    }
    usage = _merge_usage(target_usage, regression_usage)
    summary_path = _write_json(
        _station_summary(journey_root, 5),
        {
            "artifact_paths": [
                path.relative_to(workspace).as_posix() for path in artifacts
            ],
            "decision_paths": [decision_path.relative_to(workspace).as_posix()],
            "gate_path": gate_path.relative_to(workspace).as_posix(),
            "metrics": metrics,
            "mode": mode,
            "provider": provider.value if mode == "live" else None,
            "record_type": "journey_station_summary",
            "schema_version": "v1alpha1",
            "station": 5,
            "usage": usage.model_dump(mode="json"),
        },
    )
    status: JourneyResultStatus = "completed" if gate_accepted else "needs_attention"
    return StationRun(
        number=5,
        status=status,
        artifacts=(*artifacts, summary_path),
        decisions=(decision_path,),
        usage=usage,
        metrics=metrics,
        reason=None
        if gate_accepted
        else "Gate rejected; refine station 4 or continue to station 7",
    )


def _latest_gate(journey_root: Path) -> dict[str, object]:
    station = _read_object(_station_summary(journey_root, 5))
    path = station.get("gate_path")
    if not isinstance(path, str):
        raise JourneyCourseError("station 5 gate report is missing")
    return _read_object(journey_root.parent / PurePosixPath(path))


def run_station_6(*, workspace: Path, action: str) -> StationRun:
    """Materialize an immutable v1 timeline and an optional rollback rehearsal."""

    choices = {"release", "release-rollback-restore", "defer"}
    if action not in choices:
        raise JourneyCourseError(f"station 6 action must be one of {sorted(choices)}")
    workspace = workspace.resolve()
    journey_root = workspace / ".ses"
    gate = _latest_gate(journey_root)
    accepted = gate.get("outcome") == "accepted"
    pointer = _candidate_pointer(journey_root)
    candidate = _candidate_path(workspace=workspace, journey_root=journey_root)
    candidate_hash = normalized_skill_sha256(candidate)
    parent_hash = normalized_skill_sha256(journey_root / "versions/v0")
    pointer_candidate_hash = pointer.get("candidate_skill_sha256")
    pointer_parent_hash = pointer.get("parent_skill_sha256")
    if pointer_candidate_hash != candidate_hash:
        raise JourneyCourseError("current candidate pointer hash is invalid")
    if pointer_parent_hash != parent_hash:
        raise JourneyCourseError("current candidate parent hash is invalid")
    if action != "defer":
        if not accepted:
            raise JourneyCourseError("a rejected candidate cannot be released")
        if candidate_hash == parent_hash:
            raise JourneyCourseError(
                "a candidate with no runtime change cannot be released"
            )
        if gate.get("candidate_skill_sha256") != candidate_hash:
            raise JourneyCourseError(
                "current candidate does not match the candidate accepted by the Gate"
            )
        if gate.get("parent_skill_sha256") != parent_hash:
            raise JourneyCourseError(
                "accepted Gate does not bind the current candidate parent"
            )
    events: list[dict[str, JsonValue]] = []
    current_version = "v0"
    version_path: Path | None = None
    if accepted and action != "defer":
        version_path = journey_root / "versions/v1"
        if version_path.exists():
            if normalized_skill_sha256(version_path) != candidate_hash:
                raise JourneyCourseError("v1 already identifies another candidate")
        else:
            shutil.copytree(candidate, version_path)
        events.append(
            {
                "event": "released",
                "sequence": len(events),
                "skill_sha256": candidate_hash,
                "version": "v1",
            }
        )
        current_version = "v1"
        if action == "release-rollback-restore":
            events.extend(
                (
                    {
                        "event": "rolled_back",
                        "sequence": len(events),
                        "skill_sha256": parent_hash,
                        "version": "v0",
                    },
                    {
                        "event": "restored",
                        "sequence": len(events) + 1,
                        "skill_sha256": candidate_hash,
                        "version": "v1",
                    },
                )
            )
    if action == "defer":
        events.append(
            {
                "event": "deferred",
                "sequence": 0,
                "skill_sha256": candidate_hash,
                "version": "candidate",
            }
        )
    decision_path = _write_json(
        journey_root / "decisions/station-6-release.json",
        {
            "action": action,
            "candidate_skill_sha256": candidate_hash,
            "record_type": "journey_release_decision",
            "schema_version": "v1alpha1",
            "station": 6,
        },
    )
    timeline_path = _write_json(
        journey_root / "evidence/version-timeline.json",
        {
            "current_version": current_version,
            "events": events,
            "record_type": "journey_version_timeline",
            "schema_version": "v1alpha1",
        },
    )
    timeline = "".join(
        f"<div class='card'><span class='muted'>#{event['sequence']}</span><h2>{html.escape(cast(str, event['event']))}</h2><p>{html.escape(cast(str, event['version']))}</p></div>"
        for event in events
    )
    report_path = _write_html(
        journey_root / "reports/station-6-versions.html",
        title="站 6 · Version Release & Rollback",
        body=f"<p>当前版本：<strong>{current_version}</strong></p><section class='grid'>{timeline}</section>",
    )
    metrics: dict[str, JsonValue] = {
        "current_version": current_version,
        "release_action": action,
        "release_completed": accepted and action != "defer",
        "release_event_count": len(events),
        "rollback_experienced": any(
            event["event"] == "rolled_back" for event in events
        ),
    }
    summary_path = _write_json(
        _station_summary(journey_root, 6),
        {
            "artifact_paths": [
                timeline_path.relative_to(workspace).as_posix(),
                report_path.relative_to(workspace).as_posix(),
                *(
                    [version_path.relative_to(workspace).as_posix()]
                    if version_path is not None
                    else []
                ),
            ],
            "decision_paths": [decision_path.relative_to(workspace).as_posix()],
            "metrics": metrics,
            "record_type": "journey_station_summary",
            "schema_version": "v1alpha1",
            "station": 6,
            "usage": Usage(input_tokens=0, output_tokens=0).model_dump(mode="json"),
        },
    )
    status: JourneyResultStatus = (
        "completed" if accepted and action != "defer" else "needs_attention"
    )
    return StationRun(
        number=6,
        status=status,
        artifacts=(timeline_path, report_path, summary_path),
        decisions=(decision_path,),
        usage=Usage(input_tokens=0, output_tokens=0),
        metrics=metrics,
        reason=None
        if status == "completed"
        else "release deferred or Gate not accepted; station 7 remains available",
    )


def _optional_object(path: Path) -> dict[str, object] | None:
    return _read_object(path) if path.is_file() else None


def _write_text(path: Path, value: str) -> Path:
    _assert_no_credential_text(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value.rstrip() + "\n", encoding="utf-8")
    return path


def run_station_7(*, workspace: Path) -> StationRun:
    """Generate deterministic resume, interview, concept, and evidence artifacts."""

    workspace = workspace.resolve()
    journey_root = workspace / ".ses"
    selection = _optional_object(_station_summary(journey_root, 1))
    attribution = _optional_object(_station_summary(journey_root, 2))
    diagnosis = _optional_object(_station_summary(journey_root, 3))
    refinement = _optional_object(_station_summary(journey_root, 4))
    gate = _optional_object(_station_summary(journey_root, 5))
    release = _optional_object(_station_summary(journey_root, 6))
    baseline = _optional_object(_station_summary(journey_root, 0))
    baseline_data = baseline or {}
    attribution_data = attribution or {}
    refinement_data = refinement or {}
    gate_data = gate or {}
    release_data = release or {}
    baseline_metrics = cast(Mapping[str, object], baseline_data.get("metrics", {}))
    attribution_metrics = cast(
        Mapping[str, object], attribution_data.get("metrics", {})
    )
    refinement_metrics = cast(Mapping[str, object], refinement_data.get("metrics", {}))
    gate_metrics = cast(Mapping[str, object], gate_data.get("metrics", {}))
    release_metrics = cast(Mapping[str, object], release_data.get("metrics", {}))
    baseline_count = int(cast(int, baseline_metrics.get("baseline_case_count", 0)))
    baseline_pass = int(cast(int, baseline_metrics.get("baseline_pass_count", 0)))
    baseline_verified = baseline is not None and baseline_count > 0
    measurement_kind = str(
        baseline_metrics.get(
            "measurement_kind",
            "live_measured"
            if baseline_data.get("mode") == "live"
            else "synthetic_offline"
            if baseline_data.get("mode") == "fixed"
            else "not_run",
        )
    )
    experiment_mode = str(
        baseline_metrics.get("experiment_mode", baseline_data.get("mode", "not_run"))
    )
    full_regression_ran = gate_metrics.get("full_regression_ran") is True
    candidate_pass_value = gate_metrics.get("candidate_pass_count")
    regression_case_value = gate_metrics.get("regression_case_count")
    candidate_pass = (
        int(candidate_pass_value)
        if full_regression_ran and isinstance(candidate_pass_value, int)
        else None
    )
    regression_case_count = (
        int(regression_case_value)
        if full_regression_ran and isinstance(regression_case_value, int)
        else None
    )
    post_gate_rate = (
        candidate_pass / regression_case_count
        if candidate_pass is not None and regression_case_count
        else None
    )
    pass_to_fail = (
        int(cast(int, gate_metrics.get("pass_to_fail_count", 0)))
        if full_regression_ran
        else None
    )
    candidate_round = (
        int(cast(int, refinement_metrics.get("candidate_round", 0)))
        if refinement is not None
        else None
    )
    changed_line_count = (
        int(cast(int, refinement_metrics.get("changed_line_count", 0)))
        if refinement is not None
        else None
    )
    version = str(release_metrics.get("current_version", "unreleased"))
    release_completed = release_metrics.get("release_completed") is True
    gate_outcome = str(gate_metrics.get("gate_outcome", "not_run"))
    category_count = int(
        cast(int, attribution_metrics.get("attribution_category_count", 0))
    )
    if not baseline_verified:
        portfolio_status = "draft_missing_baseline"
    elif measurement_kind == "synthetic_offline":
        portfolio_status = "synthetic_ci_only"
    elif not full_regression_ran:
        portfolio_status = "draft_missing_full_regression"
    elif gate_outcome != "accepted":
        portfolio_status = "candidate_rejected"
    elif not release_completed:
        portfolio_status = "gate_accepted_unreleased"
    else:
        portfolio_status = "verified_released"
    facts: dict[str, JsonValue] = {
        "attribution_category_count": category_count,
        "baseline_case_count": baseline_count if baseline_verified else None,
        "baseline_pass_count": baseline_pass if baseline_verified else None,
        "baseline_pass_rate": (
            baseline_pass / baseline_count if baseline_verified else None
        ),
        "candidate_snapshot_round": candidate_round,
        "changed_line_count": changed_line_count,
        "current_version": version,
        "experiment_mode": experiment_mode,
        "full_regression_ran": full_regression_ran,
        "gate_outcome": gate_outcome,
        "measurement_kind": measurement_kind,
        "pass_to_fail_count": pass_to_fail,
        "portfolio_status": portfolio_status,
        "post_gate_case_count": regression_case_count,
        "post_gate_pass_count": candidate_pass,
        "post_gate_pass_rate": post_gate_rate,
        "release_completed": release_completed,
        "sandbox": "STATE-Bench customer-support return sandbox",
        "stages_with_evidence": {
            "baseline": baseline is not None,
            "bad_case_selection": selection is not None,
            "attribution": attribution is not None,
            "diagnosis": diagnosis is not None,
            "refinement": refinement is not None,
            "gate": gate is not None,
            "release": release is not None,
        },
    }
    deliverable_root = journey_root / "deliverables"
    facts_path = _write_json(
        deliverable_root / "evidence-facts.json",
        {
            "facts": facts,
            "record_type": "journey_portfolio_facts",
            "schema_version": "v1alpha1",
            "source": "machine-derived journey evidence only; null means not verified",
        },
    )
    if measurement_kind == "synthetic_offline":
        cn_disclosure = (
            "> STATE-Bench 沙盒的 CI 合成证据草稿：这些数字来自 fixed 离线测试，"
            "不是模型实测，不能直接放进正式简历。"
        )
        en_disclosure = (
            "> Synthetic CI evidence draft: fixed/offline results are not live model "
            "measurements and must not be presented as such."
        )
        execution_cn = "确定性 fixed 评测"
        execution_en = "the deterministic fixed CI evaluator"
    elif measurement_kind == "live_measured":
        cn_disclosure = (
            "> 基于 STATE-Bench 客服退货沙盒的实测项目；不是生产部署或真实客户流量。"
        )
        en_disclosure = (
            "> Live-measured sandbox evidence; this is not production deployment or "
            "real customer traffic."
        )
        execution_cn = "锁定模型的真实评测"
        execution_en = "a locked-model live evaluation"
    else:
        cn_disclosure = "> 证据草稿：尚未获得基线，下面不会填入未验证数字。"
        en_disclosure = "> Evidence draft: no baseline has been verified yet."
        execution_cn = "尚未运行评测"
        execution_en = "no verified evaluator run"

    if baseline_verified:
        baseline_cn = (
            f"围绕 {baseline_count} 条可执行客服退货 case，通过{execution_cn}建立了"
            f"从执行、终态判分到失败证据回看的闭环。初始结果为 "
            f"{baseline_pass}/{baseline_count}（{baseline_pass / baseline_count:.1%}）。"
        )
        baseline_en = (
            f"Ran {baseline_count} executable customer-support return cases with "
            f"{execution_en}; the baseline was {baseline_pass}/{baseline_count} "
            f"({baseline_pass / baseline_count:.1%})."
        )
    else:
        baseline_cn = "尚未形成可引用的基线证据，因此不报告 case 数或通过率。"
        baseline_en = "No verified baseline is available, so no case count or pass rate is reported."

    evidence_actions: list[str] = []
    evidence_actions_en: list[str] = []
    if selection is not None:
        evidence_actions.append("bad case 筛选")
        evidence_actions_en.append("bad-case selection")
    if attribution is not None:
        evidence_actions.append(f"{category_count} 类人工归因")
        evidence_actions_en.append(f"{category_count} human attribution categories")
    if diagnosis is not None:
        evidence_actions.append("Skill 文件与行号定位")
        evidence_actions_en.append("Skill file-and-line diagnosis")
    if changed_line_count is not None and changed_line_count > 0:
        evidence_actions.append(
            f"第 {candidate_round} 轮候选 Skill 快照（{changed_line_count} 个 diff 行）"
        )
        evidence_actions_en.append(
            f"candidate snapshot round {candidate_round} ({changed_line_count} diff lines)"
        )
    elif refinement is not None:
        evidence_actions.append(
            f"第 {candidate_round} 轮候选快照（未观察到运行文本变化）"
        )
        evidence_actions_en.append(
            f"candidate snapshot round {candidate_round} (no runtime text change observed)"
        )
    if gate is not None:
        evidence_actions.append(
            "目标回放与全量回归"
            if full_regression_ran
            else "目标回放（全量回归未运行）"
        )
        evidence_actions_en.append(
            "target replay and full regression"
            if full_regression_ran
            else "target replay (full regression did not run)"
        )
    actions_cn = "、".join(evidence_actions) if evidence_actions else "尚无后续站点证据"
    actions_en = (
        ", ".join(evidence_actions_en)
        if evidence_actions_en
        else "no downstream station evidence"
    )

    if (
        candidate_pass is not None
        and regression_case_count is not None
        and post_gate_rate is not None
    ):
        result_cn = (
            f"候选的全量回归结果为 {candidate_pass}/{regression_case_count} "
            f"（{post_gate_rate:.1%}），pass→fail 为 {pass_to_fail} 条，"
            f"Gate={gate_outcome}，当前版本={version}。"
        )
        result_en = (
            f"The candidate's full regression result was {candidate_pass}/"
            f"{regression_case_count} ({post_gate_rate:.1%}), with {pass_to_fail} "
            f"pass-to-fail regression(s); Gate={gate_outcome}, current version={version}."
        )
    else:
        result_cn = (
            f"当前 Gate={gate_outcome}，但没有完整全量回归证据；因此不报告修后通过率、"
            f"零退化或发版效果。当前版本={version}。"
        )
        result_en = (
            f"Gate={gate_outcome}, but no complete full-regression evidence exists. "
            f"No post-change pass rate or zero-regression claim is reported; current "
            f"version={version}."
        )
    resume_cn = _write_text(
        deliverable_root / "resume-zh.md",
        f"""# 简历项目描述（中文）

{cn_disclosure}

背景：{baseline_cn}

已有建设证据：{actions_cn}。

结果：{result_cn}所有数字可从 `evidence-facts.json` 回查；`null` 表示没有证据，不代表 0。""",
    )
    resume_en = _write_text(
        deliverable_root / "resume-en.md",
        f"""# Resume project statement (English)

{en_disclosure}

Background: {baseline_en}

Evidence currently covers: {actions_en}.

Result: {result_en} Every number is traceable to `evidence-facts.json`; `null` means unverified, not zero.
""",
    )

    def evidence_line(*relative_paths: str) -> str:
        existing = [
            path
            for path in relative_paths
            if (workspace / PurePosixPath(path)).is_file()
        ]
        if existing:
            return "、".join(f"`{path}`" for path in existing)
        return "当前缺失；先完成对应站点，不能据此作答"

    interview = _write_text(
        deliverable_root / "interview-prep.md",
        f"""# 面试追问准备

> 回答时只引用本次沙盒证据，不把结果描述成生产流量。生产对照答案仍待 Owner 对 Part B 终审。

1. 你的 bad case 从哪里来，你如何挑选？
   - 证据：{evidence_line(".ses/evidence/bad-cases.json", ".ses/decisions/station-1-selection.json")}
2. 你如何区分环境、case 与 Skill 问题？
   - 证据：{evidence_line(".ses/decisions/station-2-attributions.json")}
3. 你为什么改这一段 Skill，而不是别处？
   - 证据：{evidence_line(".ses/decisions/station-3-diagnoses.json", ".ses/reports/station-4-diff.html")}
4. 你如何证明修复没有破坏原来通过的 case？生产里还缺什么？
   - 证据：{evidence_line(".ses/reports/station-5-gate.html")}
   - 生产对照：待 Owner 终审后补入讲师 playbook；当前只陈述沙盒 Gate 证据。
5. 如果把这个机制带到生产，你如何发布和回滚？
   - 证据：{evidence_line(".ses/evidence/version-timeline.json")}
   - 生产对照：待 Owner 终审后补入讲师 playbook；不要把本地版本时间线说成线上发布。
""",
    )
    concepts = _write_text(
        deliverable_root / "concepts.md",
        f"""# 概念清单

| 站 | 你在沙盒亲手做的 | 证据文件 | 生产里的做法 |
|---|---|---|---|
| 0 | 运行 case，查看 Trace、终态和 Judge | {evidence_line(".ses/evidence/station-0.json")} | 待 Owner 终审 Part B 后补充 |
| 1 | 从失败清单挑选分析对象 | {evidence_line(".ses/decisions/station-1-selection.json")} | 待 Owner 终审 Part B 后补充 |
| 2 | 人工区分环境、case 与 Skill 归因 | {evidence_line(".ses/decisions/station-2-attributions.json")} | 待 Owner 终审 Part B 后补充 |
| 3 | 把失败定位到 Skill 文件与行 | {evidence_line(".ses/decisions/station-3-diagnoses.json")} | 待 Owner 终审 Part B 后补充 |
| 4 | 控制修改范围并查看 diff | {evidence_line(".ses/reports/station-4-diff.html")} | 待 Owner 终审 Part B 后补充 |
| 5 | 跑目标回放与全量回归 | {evidence_line(".ses/reports/station-5-gate.html")} | 待 Owner 终审 Part B 后补充 |
| 6 | 记录发版、回滚与恢复时间线 | {evidence_line(".ses/evidence/version-timeline.json")} | 待 Owner 终审 Part B 后补充 |
| 7 | 核对机器事实并生成三件套 | `.ses/deliverables/evidence-facts.json` | 待 Owner 终审 Part B 后补充 |

本文件明确保留待审项，避免把未经 Owner 终审的生产经验写成定论。
""",
    )
    index_path = deliverable_root / "evidence-index.json"
    excluded_from_index = {
        journey_root / "status.json",
        index_path,
        _station_summary(journey_root, 7),
        journey_root / "reports/station-7-summary.html",
    }
    evidence_paths = sorted(
        path
        for path in journey_root.rglob("*")
        if path.is_file() and not path.is_symlink() and path not in excluded_from_index
    )
    index_path = _write_json(
        index_path,
        {
            "artifacts": [
                {
                    "path": path.relative_to(workspace).as_posix(),
                    "sha256": _sha256(path),
                }
                for path in evidence_paths
            ],
            "record_type": "journey_evidence_index",
            "schema_version": "v1alpha1",
        },
    )
    artifacts: tuple[Path, ...] = (
        facts_path,
        resume_cn,
        resume_en,
        interview,
        concepts,
        index_path,
    )
    report_path = _write_html(
        journey_root / "reports/station-7-summary.html",
        title="站 7 · Summary",
        body=(
            f"<section class='grid'><div class='card'><h2>{f'{baseline_pass}/{baseline_count}' if baseline_verified else '—'}</h2><p>基线通过</p></div><div class='card'><h2>{f'{candidate_pass}/{regression_case_count}' if candidate_pass is not None else '—'}</h2><p>全量回归证据</p></div><div class='card'><h2>{html.escape(version)}</h2><p>当前版本</p></div></section>"
            + "<h2>产物包</h2><ul>"
            + "".join(
                f"<li><a href='/artifact/{path.relative_to(workspace).as_posix()}'>{html.escape(path.name)}</a></li>"
                for path in artifacts
            )
            + "</ul><p class='muted'>生产对照正文仍明确标为待 Owner 终审。</p>"
        ),
    )
    artifacts = (*artifacts, report_path)
    metrics: dict[str, JsonValue] = {
        "deliverable_count": 6,
        "evidence_index_count": len(evidence_paths),
        "portfolio_status": portfolio_status,
        "production_content_review": "pending_owner_review",
    }
    summary_path = _write_json(
        _station_summary(journey_root, 7),
        {
            "artifact_paths": [
                path.relative_to(workspace).as_posix() for path in artifacts
            ],
            "decision_paths": [],
            "metrics": metrics,
            "record_type": "journey_station_summary",
            "schema_version": "v1alpha1",
            "station": 7,
            "usage": Usage(input_tokens=0, output_tokens=0).model_dump(mode="json"),
        },
    )
    return StationRun(
        number=7,
        status="completed",
        artifacts=(*artifacts, summary_path),
        decisions=(),
        usage=Usage(input_tokens=0, output_tokens=0),
        metrics=metrics,
    )
