"""Allowlist-only export of public bounded-evolution evidence."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from ses.contracts import (
    AutoEvolveState,
    FinalAggregateReport,
    PortfolioFile,
    PortfolioManifest,
    SchemaVersion,
    artifact_json_bytes,
)
from ses.contracts.security import validate_public_data
from ses.evolution.gate import public_gate_decision_payload
from ses.foundation.credentials import credential_values, redact
from ses.reporting.l3 import L3ReportInputs, load_l3_inputs, render_l3_html
from ses.skills.installer import load_skill_manifest, normalized_skill_sha256

_KEY = re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{8,}")
_ABSOLUTE_PATH = re.compile(
    r"(?:^|[\s\"'(=:])(?:/(?!/)[A-Za-z0-9._~-]+(?:/|$)|[A-Za-z]:[\\/])"
)
_HOME_PATH = re.compile(r"(?:^|[\s\"'(=:])~[\\/]")
_STRUCTURED_CREDENTIAL = re.compile(
    r"(?i)(?:authorization|proxy-authorization|x-api-key|api[_ -]?key|"
    r"auth[_ -]?token|access[_ -]?token|client[_ -]?secret|private[_ -]?key|"
    r"password|passwd|secret[_ -]?key|session[_ -]?token)"
    r"[\"']?\s*[:=]\s*(?:bearer\s+)?[^\s,;}]+"
)
_FORBIDDEN_VALUE = re.compile(
    r"(?:^|/)(?:private|hidden-gold|gold|credentials?)(?:/|$)|"
    r"(?:selection-pair|accepted-events|candidate-events)\.jsonl?$",
    re.IGNORECASE,
)
_PRIVATE_SKILL_MARKER = re.compile(
    r"(?:^|[\s\"'`(=:,/\\])"
    r"(?:private|hidden-gold|hidden|gold|selection|final)"
    r"(?:[/\\]|[-_](?:answer|case|catalog|fixture|gold|manifest|oracle|"
    r"result|split|trace|trajectory)\b)|"
    r"\b(?:hidden|private|reference)\s+"
    r"(?:answer|gold|trace|trajectory)\b",
    re.IGNORECASE,
)
_PUBLIC_SKILL_TEXT_SUFFIXES = frozenset({".json", ".md", ".txt", ".yaml", ".yml"})


class PortfolioExportError(ValueError):
    """An experiment cannot be exported without private or unstable material."""


def _assert_safe_text(value: str) -> None:
    if (
        _KEY.search(value)
        or _ABSOLUTE_PATH.search(value)
        or _HOME_PATH.search(value)
        or _STRUCTURED_CREDENTIAL.search(value)
        or "file://" in value.casefold()
    ):
        raise PortfolioExportError(
            "portfolio member contains a credential or local path"
        )
    if redact(value, credential_values(os.environ)) != value:
        raise PortfolioExportError("portfolio member contains a process credential")


def _assert_safe_skill_text(value: str) -> None:
    _assert_safe_text(value)
    normalized = value.replace("_", "-")
    if _PRIVATE_SKILL_MARKER.search(normalized):
        raise PortfolioExportError("accepted Skill references private evaluation data")


def _decode_public_text(payload: bytes) -> str:
    if b"\x00" in payload:
        raise PortfolioExportError(
            "portfolio members must be UTF-8 text without NUL bytes"
        )
    try:
        return payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise PortfolioExportError("portfolio members must be UTF-8 text") from exc


def _assert_safe_values(value: object) -> None:
    if isinstance(value, Mapping):
        for child in value.values():
            _assert_safe_values(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            _assert_safe_values(child)
    elif isinstance(value, str) and _FORBIDDEN_VALUE.search(value.replace("_", "-")):
        raise PortfolioExportError(
            "portfolio member references private evaluation data"
        )


def _json_bytes(value: object) -> bytes:
    validate_public_data(value)
    _assert_safe_values(value)
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    _assert_safe_text(payload.decode("utf-8"))
    return payload


def _registry_projection(inputs: L3ReportInputs) -> dict[str, object]:
    state = inputs.registry
    return {
        "schema_version": "v1alpha1",
        "record_type": "registry_event_projection_bundle",
        "registry_id": state.registry_id,
        "lineage_id": state.lineage_id,
        "event_count": len(state.events),
        "head_event_sha256": state.events[-1].event_sha256,
        "events": [
            {
                "sequence": event.sequence,
                "event_id": event.event_id,
                "event_type": event.event_type.value,
                "occurred_at": event.occurred_at.isoformat().replace("+00:00", "Z"),
                "version_id": event.version_id,
                "version_sha256": event.version_sha256,
                "parent_skill_sha256": event.parent_skill_sha256,
                "previous_accepted_skill_sha256": event.previous_accepted_skill_sha256,
                "current_accepted_skill_sha256": event.current_accepted_skill_sha256,
                "status": event.status.value,
                "reason": event.reason,
                "previous_event_sha256": event.previous_event_sha256,
                "event_sha256": event.event_sha256,
            }
            for event in state.events
        ],
    }


def _loop_projection(state: AutoEvolveState) -> dict[str, object]:
    return {
        "schema_version": "v1alpha1",
        "record_type": "auto_evolve_state_projection",
        "experiment_id": state.experiment_id,
        "config_sha256": state.config_sha256,
        "status": state.status.value,
        "current_accepted_skill_sha256": state.current_accepted_skill_sha256,
        "completed_rounds": state.completed_rounds,
        "rounds": [
            {
                "round_number": row.round_number,
                "parent_skill_sha256": row.parent_skill_sha256,
                "candidate_id": row.candidate_id,
                "candidate_skill_sha256": row.candidate_skill_sha256,
                "gate_outcome": row.gate_outcome.value,
                "promoted": row.promoted,
                "quality_delta": row.quality_delta,
                "cost_amount": str(row.cost_amount),
                "cost_currency": row.cost_currency,
                "cost_complete": row.cost_complete,
                "input_tokens": row.input_tokens,
                "output_tokens": row.output_tokens,
                "failure_categories": [item.value for item in row.failure_categories],
                "patch_targets": list(row.patch_targets),
            }
            for row in state.rounds
        ],
        "total_cost_amount": str(state.total_cost_amount),
        "cost_currency": state.cost_currency,
        "cost_complete": state.cost_complete,
        "total_input_tokens": state.total_input_tokens,
        "total_output_tokens": state.total_output_tokens,
        "consecutive_rejections": state.consecutive_rejections,
        "stopped_at": (
            None
            if state.stopped_at is None
            else state.stopped_at.isoformat().replace("+00:00", "Z")
        ),
        "stop_reason": None if state.stop_reason is None else state.stop_reason.value,
        "final_aggregate_included": state.final_report is not None,
    }


def _final_projection(report: FinalAggregateReport) -> dict[str, object]:
    """Expose aggregates without a brute-forceable hash of twelve slot results."""

    return {
        "schema_version": "v1alpha1",
        "record_type": "final_aggregate_projection",
        "experiment_id": report.experiment_id,
        "subject_skill_sha256": report.subject_skill_sha256,
        "final_lock_sha256": report.final_lock_sha256,
        "mode": report.mode,
        "measurement_kind": report.measurement_kind.value,
        "network_used": report.network_used,
        "result_source": report.result_source,
        "executed_at": report.executed_at.isoformat().replace("+00:00", "Z"),
        "case_count": report.case_count,
        "pass_count": report.pass_count,
        "pass_rate": report.pass_rate,
        "cost_amount": str(report.cost_amount),
        "cost_currency": report.cost_currency,
        "cost_complete": report.cost_complete,
        "input_tokens": report.input_tokens,
        "output_tokens": report.output_tokens,
        "privacy_notice": "per_slot_results_and_their_digest_are_not_exported",
    }


def _architecture_markdown() -> bytes:
    payload = b"""# Architecture

```text
fresh develop rollout -> reflect -> bounded patch -> shared Gate -> Registry
          ^                                               |
          |                                               v
          +---------- current accepted Skill <- accept/promote or reject

loop stop -> one-time final aggregate -> L3 report -> allowlisted portfolio
```

The automatic loop composes the same Gate and Registry used by manual candidates.
It cannot change evaluation policy, split locks, Judge logic, or the accepted pointer
without a complete accepted Gate decision. Final runs after the loop and returns only
an aggregate; it never becomes patch input.
"""
    _assert_safe_text(payload.decode())
    return payload


def _summary_markdown(inputs: L3ReportInputs) -> bytes:
    state = inputs.state
    accepted = sum(row.gate_outcome.value == "accepted" for row in state.rounds)
    rejected = len(state.rounds) - accepted
    final = inputs.final_report
    if final is None:
        final_line = "Final aggregate: not run; no score is inferred."
    else:
        final_line = (
            f"Final aggregate: {final.pass_count}/{final.case_count} "
            f"({final.pass_rate:.1%}), {final.result_source}, "
            f"{final.measurement_kind.value}."
        )
    measurement = (
        "fixed/offline reference"
        if all(not decision.network_used for decision in inputs.decisions)
        else "live measured"
    )
    first = inputs.decisions[0]
    final_lock = "not run" if final is None else final.final_lock_sha256
    cost_basis = (
        "fixed synthetic accounting; Provider spend 0"
        if measurement == "fixed/offline reference"
        else "live measured aggregate"
    )
    payload = f"""# System summary

- Experiment: `{state.experiment_id}`
- Evidence class: {measurement}
- Evaluation window: {first.decided_at.date().isoformat()}
- Model: {"fixed synthetic adapter (not a Provider measurement)" if measurement == "fixed/offline reference" else "locked canonical Provider model"}
- Model lock: `{first.model_lock_sha256}`
- Gate policy: `{first.gate_policy_sha256}`
- Evaluation protocol: `{first.evaluation_protocol_sha256}`
- Selection lock: `{first.selection_lock_sha256}`
- Final lock: `{final_lock}`
- Complete rounds: {state.completed_rounds} ({accepted} accepted, {rejected} rejected)
- Current accepted Skill: `{state.current_accepted_skill_sha256}`
- Loop cost: {state.total_cost_amount} {state.cost_currency}; basis: {cost_basis}; coverage: {"complete" if state.cost_complete else "incomplete"}
- Stop reason: {state.stop_reason.value if state.stop_reason else "not stopped"}
- {final_line}

This package contains public aggregates, the current accepted Skill, and lineage
projections. It excludes per-case protected evaluation material and modifying-agent
feedback.
""".encode()
    _assert_safe_text(payload.decode())
    return payload


def _safe_member_path(path: str) -> PurePosixPath:
    candidate = PurePosixPath(path)
    if (
        candidate.is_absolute()
        or path != candidate.as_posix()
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise PortfolioExportError("portfolio member path is not canonical")
    return candidate


def _write_member(
    root: Path,
    relative: str,
    payload: bytes,
    *,
    skill_content: bool = False,
) -> PortfolioFile:
    path = _safe_member_path(relative)
    text = _decode_public_text(payload)
    if skill_content:
        _assert_safe_skill_text(text)
    else:
        _assert_safe_text(text)
    destination = root / path
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as stream:
        stream.write(payload)
    return PortfolioFile(
        path=relative,
        sha256=hashlib.sha256(payload).hexdigest(),
        kind=_member_kind(relative),
    )


def _member_kind(
    relative: str,
) -> Literal[
    "skill",
    "registry",
    "gate",
    "loop_state",
    "l3_report",
    "final_aggregate",
    "architecture",
    "system_summary",
]:
    if relative.startswith("accepted-skill/"):
        return "skill"
    if relative.startswith("registry/"):
        return "registry"
    if relative.startswith("gate-projections/"):
        return "gate"
    if relative == "loop-state.json":
        return "loop_state"
    if relative == "l3.html":
        return "l3_report"
    if relative == "final-aggregate.json":
        return "final_aggregate"
    if relative == "architecture.md":
        return "architecture"
    if relative == "system-summary.md":
        return "system_summary"
    raise PortfolioExportError("portfolio member is outside the allowlist")


def _copy_accepted_skill(
    staging: Path,
    *,
    source: Path,
    expected_sha256: str,
) -> list[PortfolioFile]:
    try:
        manifest = load_skill_manifest(source)
        actual_sha256 = normalized_skill_sha256(source)
    except (OSError, ValueError) as exc:
        raise PortfolioExportError("accepted Skill is invalid") from exc
    if actual_sha256 != expected_sha256:
        raise PortfolioExportError("accepted Skill does not match the loop state")
    for item in manifest.files:
        suffix = PurePosixPath(item.path).suffix.casefold()
        if suffix not in _PUBLIC_SKILL_TEXT_SUFFIXES:
            raise PortfolioExportError(
                "accepted Skill contains a non-public text file extension"
            )
    members = [
        _write_member(
            staging,
            "accepted-skill/skill-manifest.json",
            artifact_json_bytes(manifest),
            skill_content=True,
        )
    ]
    source_root = source.resolve(strict=True)
    for item in manifest.files:
        path = source
        for component in PurePosixPath(item.path).parts:
            path /= component
            if path.is_symlink():
                raise PortfolioExportError(
                    "accepted Skill contains a symlink component"
                )
        if path.is_symlink() or not path.is_file():
            raise PortfolioExportError("accepted Skill contains a non-regular file")
        try:
            path.resolve(strict=True).relative_to(source_root)
        except (OSError, ValueError) as exc:
            raise PortfolioExportError("accepted Skill file escapes its root") from exc
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != item.sha256:
            raise PortfolioExportError("accepted Skill file checksum changed")
        members.append(
            _write_member(
                staging,
                f"accepted-skill/{item.path}",
                payload,
                skill_content=True,
            )
        )
    return members


def _verify_export(root: Path, manifest: PortfolioManifest) -> None:
    expected = {row.path for row in manifest.files} | {"manifest.json"}
    actual: set[str] = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise PortfolioExportError("portfolio contains a symlink")
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    if actual != expected:
        raise PortfolioExportError("portfolio inventory differs from its manifest")
    for row in manifest.files:
        payload = (root / row.path).read_bytes()
        if hashlib.sha256(payload).hexdigest() != row.sha256:
            raise PortfolioExportError("portfolio member checksum changed")


def export_portfolio(
    experiment_root: Path,
    destination: Path,
    *,
    created_at: datetime,
    registry_root: Path | None = None,
) -> PortfolioManifest:
    """Export one verified experiment through a closed public-member allowlist."""

    if destination.exists() or destination.is_symlink():
        raise PortfolioExportError("portfolio destination must not exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.absolute() != destination.resolve():
        raise PortfolioExportError("portfolio destination contains a symlink")

    inputs = load_l3_inputs(experiment_root, registry_root=registry_root)
    registry_path = (registry_root or experiment_root / "registry").resolve(strict=True)
    accepted_source = (
        registry_path / "versions" / inputs.state.current_accepted_skill_sha256
    )
    staging = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.", dir=destination.parent)
    )
    published = False
    try:
        members: list[PortfolioFile] = []
        members.extend(
            _copy_accepted_skill(
                staging,
                source=accepted_source,
                expected_sha256=inputs.state.current_accepted_skill_sha256,
            )
        )
        members.append(
            _write_member(
                staging,
                "registry/events-public.json",
                _json_bytes(_registry_projection(inputs)),
            )
        )
        members.append(
            _write_member(
                staging,
                "loop-state.json",
                _json_bytes(_loop_projection(inputs.state)),
            )
        )
        for index, decision in enumerate(inputs.decisions, start=1):
            members.append(
                _write_member(
                    staging,
                    f"gate-projections/round-{index:03d}.json",
                    _json_bytes(public_gate_decision_payload(decision)),
                )
            )
        l3_payload = render_l3_html(inputs).encode("utf-8")
        if len(l3_payload) >= 2_000_000:
            raise PortfolioExportError("portfolio L3 report exceeds 2 MB")
        members.append(_write_member(staging, "l3.html", l3_payload))
        members.append(
            _write_member(staging, "architecture.md", _architecture_markdown())
        )
        members.append(
            _write_member(staging, "system-summary.md", _summary_markdown(inputs))
        )
        if inputs.final_report is not None:
            members.append(
                _write_member(
                    staging,
                    "final-aggregate.json",
                    _json_bytes(_final_projection(inputs.final_report)),
                )
            )

        manifest = PortfolioManifest(
            schema_version=SchemaVersion.V1ALPHA1,
            record_type="portfolio_manifest",
            experiment_id=inputs.state.experiment_id,
            created_at=created_at,
            files=tuple(sorted(members, key=lambda item: item.path)),
        )
        manifest_payload = artifact_json_bytes(manifest)
        _assert_safe_text(manifest_payload.decode("utf-8"))
        with (staging / "manifest.json").open("xb") as stream:
            stream.write(manifest_payload)
        _verify_export(staging, manifest)
        os.replace(staging, destination)
        published = True
        return manifest
    finally:
        if not published:
            shutil.rmtree(staging)


def portfolio_semantic_sha256(experiment_root: Path) -> str:
    """Hash all members by relative path for deterministic reference checks."""

    if experiment_root.is_symlink() or not experiment_root.is_dir():
        raise PortfolioExportError("portfolio root must be a real directory")
    manifest_path = experiment_root / "manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = PortfolioManifest.model_validate_json(manifest_bytes)
    except (OSError, ValueError) as exc:
        raise PortfolioExportError("portfolio manifest is invalid") from exc
    if artifact_json_bytes(manifest) != manifest_bytes:
        raise PortfolioExportError("portfolio manifest is not canonical")
    _verify_export(experiment_root, manifest)
    digest = hashlib.sha256()
    for path in sorted({row.path for row in manifest.files} | {"manifest.json"}):
        payload = (experiment_root / path).read_bytes()
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(payload).digest())
    return digest.hexdigest()


__all__ = [
    "PortfolioExportError",
    "export_portfolio",
    "portfolio_semantic_sha256",
]
