"""Self-contained L3 report over bounded evolution aggregate records."""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path, PurePosixPath

from ses.contracts import (
    AutoEvolveState,
    AutoLoopStatus,
    FinalAggregateReport,
    GateDecision,
    GateOutcome,
    artifact_json_bytes,
)
from ses.contracts.security import validate_public_data
from ses.evolution.registry import RegistryState, SkillRegistry

_MAX_L3_BYTES = 2_000_000


@dataclass(frozen=True, slots=True)
class L3ReportInputs:
    """Verified aggregate records consumed by the public report."""

    state: AutoEvolveState
    registry: RegistryState
    decisions: tuple[GateDecision, ...]
    final_report: FinalAggregateReport | None


def _read_ref(root: Path, relative: str) -> bytes:
    """Read one regular artifact without allowing a symlink escape."""

    lexical = root
    for component in PurePosixPath(relative).parts:
        lexical /= component
        if lexical.is_symlink():
            raise ValueError("L3 artifact reference contains a symlink")
    if lexical.is_symlink() or not lexical.is_file():
        raise ValueError("L3 artifact reference must identify a regular file")
    try:
        resolved = lexical.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ValueError("L3 artifact reference escapes the experiment") from exc
    return resolved.read_bytes()


def load_l3_inputs(
    experiment_root: Path,
    *,
    registry_root: Path | None = None,
) -> L3ReportInputs:
    """Load and cross-check public report inputs from one experiment."""

    if experiment_root.is_symlink() or not experiment_root.is_dir():
        raise ValueError("L3 experiment root must be a real directory")
    root = experiment_root.resolve(strict=True)
    state_bytes = _read_ref(root, "state.json")
    try:
        state = AutoEvolveState.model_validate_json(state_bytes)
    except ValueError as exc:
        raise ValueError("L3 state is invalid") from exc
    if artifact_json_bytes(state) != state_bytes:
        raise ValueError("L3 state is not canonical")

    registry_path = registry_root or root / "registry"
    registry = SkillRegistry(registry_path).audit()
    decisions: list[GateDecision] = []
    for round_record in state.rounds:
        decision_bytes = _read_ref(root, round_record.gate_decision.path)
        round_record.gate_decision.verify_bytes(decision_bytes)
        try:
            decision = GateDecision.model_validate_json(decision_bytes)
        except ValueError as exc:
            raise ValueError("L3 GateDecision is invalid") from exc
        if artifact_json_bytes(decision) != decision_bytes:
            raise ValueError("L3 GateDecision is not canonical")
        decisions.append(decision)

    final_report: FinalAggregateReport | None = None
    if state.final_report is not None:
        final_bytes = _read_ref(root, state.final_report.path)
        state.final_report.verify_bytes(final_bytes)
        try:
            final_report = FinalAggregateReport.model_validate_json(final_bytes)
        except ValueError as exc:
            raise ValueError("L3 final aggregate is invalid") from exc
        if artifact_json_bytes(final_report) != final_bytes:
            raise ValueError("L3 final aggregate is not canonical")

    inputs = L3ReportInputs(
        state=state,
        registry=registry,
        decisions=tuple(decisions),
        final_report=final_report,
    )
    _validate_inputs(inputs)
    return inputs


def _validate_inputs(inputs: L3ReportInputs) -> None:
    state = inputs.state
    registry = inputs.registry
    if not state.rounds:
        raise ValueError("L3 report requires at least one complete Gate round")
    if len(inputs.decisions) != len(state.rounds):
        raise ValueError("L3 decisions do not match completed rounds")
    if state.current_accepted_skill_sha256 != registry.current_accepted_sha256:
        raise ValueError("L3 state and Registry disagree on the accepted Skill")
    protocol_identity = {
        (
            decision.gate_policy_sha256,
            decision.selection_lock_sha256,
            decision.evaluation_protocol_sha256,
            decision.model_lock_sha256,
            decision.mode,
            decision.measurement_kind,
        )
        for decision in inputs.decisions
    }
    if len(protocol_identity) != 1:
        raise ValueError("L3 rounds do not share one locked evaluation protocol")
    for record, decision in zip(state.rounds, inputs.decisions, strict=True):
        if (
            decision.lineage_id != registry.lineage_id
            or decision.candidate_id != record.candidate_id
            or decision.candidate_skill_sha256 != record.candidate_skill_sha256
            or decision.accepted_skill_sha256 != record.parent_skill_sha256
            or decision.outcome is not record.gate_outcome
            or decision.metrics.quality_delta != record.quality_delta
            or decision.metrics.cost_currency != record.cost_currency
        ):
            raise ValueError("L3 round disagrees with its GateDecision")
        version = registry.versions.get(record.candidate_skill_sha256)
        if version is None or version.parent_skill_sha256 != record.parent_skill_sha256:
            raise ValueError("L3 candidate is missing from the Registry lineage")
    if state.status is AutoLoopStatus.FINAL_COMPLETE:
        if inputs.final_report is None:
            raise ValueError("L3 final-complete state lacks its aggregate result")
        final = inputs.final_report
        if (
            final.experiment_id != state.experiment_id
            or final.subject_skill_sha256 != state.current_accepted_skill_sha256
        ):
            raise ValueError("L3 final aggregate belongs to another experiment")
    elif inputs.final_report is not None:
        raise ValueError("L3 cannot attach final data before final completion")


def build_l3_data(inputs: L3ReportInputs) -> dict[str, object]:
    """Build the public semantic payload rendered by the L3 HTML."""

    _validate_inputs(inputs)
    state = inputs.state
    registry = inputs.registry
    nodes = []
    for skill_sha256, version in registry.versions.items():
        nodes.append(
            {
                "version_id": version.version_id,
                "skill_sha256": skill_sha256,
                "parent_skill_sha256": version.parent_skill_sha256,
                "status": version.status.value,
                "verified": version.verified,
                "was_current": version.was_current,
                "is_current": skill_sha256 == registry.current_accepted_sha256,
            }
        )
    rounds: list[dict[str, object]] = []
    cumulative_cost = Decimal(0)
    accepted_curve: list[float] = []
    for record, decision in zip(state.rounds, inputs.decisions, strict=True):
        cumulative_cost += record.cost_amount
        accepted_after = (
            decision.metrics.candidate_pass_rate
            if record.gate_outcome is GateOutcome.ACCEPTED
            else decision.metrics.accepted_pass_rate
        )
        accepted_curve.append(accepted_after)
        rounds.append(
            {
                "round_number": record.round_number,
                "parent_skill_sha256": record.parent_skill_sha256,
                "candidate_id": record.candidate_id,
                "candidate_skill_sha256": record.candidate_skill_sha256,
                "gate_id": decision.gate_id,
                "gate_outcome": record.gate_outcome.value,
                "gate_reasons": [reason.value for reason in decision.reason_codes],
                "measurement_kind": decision.measurement_kind.value,
                "network_used": decision.network_used,
                "develop": {
                    "quality_metric_available": False,
                    "note": "Fresh rollout provenance is recorded; no develop quality score is part of the aggregate contract.",
                },
                "selection": {
                    "case_count": decision.metrics.selection_case_count,
                    "accepted_pass_rate": decision.metrics.accepted_pass_rate,
                    "candidate_pass_rate": decision.metrics.candidate_pass_rate,
                    "quality_delta": decision.metrics.quality_delta,
                    "critical_regressions": decision.metrics.critical_regression_count,
                },
                "accepted_pass_rate_after_gate": accepted_after,
                "round_cost_amount": str(record.cost_amount),
                "cumulative_cost_amount": str(cumulative_cost),
                "cost_currency": record.cost_currency,
                "cost_complete": record.cost_complete,
                "input_tokens": record.input_tokens,
                "output_tokens": record.output_tokens,
                "patch_targets": list(record.patch_targets),
            }
        )

    final_payload: dict[str, object] | None = None
    if inputs.final_report is not None:
        final = inputs.final_report
        final_payload = {
            "isolation": "one_time_post_loop_aggregate_not_used_for_patch",
            "subject_skill_sha256": final.subject_skill_sha256,
            "final_lock_sha256": final.final_lock_sha256,
            "mode": final.mode,
            "measurement_kind": final.measurement_kind.value,
            "network_used": final.network_used,
            "result_source": final.result_source,
            "executed_at": final.executed_at.isoformat().replace("+00:00", "Z"),
            "case_count": final.case_count,
            "pass_count": final.pass_count,
            "pass_rate": final.pass_rate,
            "cost_amount": str(final.cost_amount),
            "cost_currency": final.cost_currency,
            "cost_complete": final.cost_complete,
            "input_tokens": final.input_tokens,
            "output_tokens": final.output_tokens,
        }

    data: dict[str, object] = {
        "schema_version": "v1alpha1",
        "record_type": "l3_report_data",
        "experiment_id": state.experiment_id,
        "result_kind": (
            "fixed_offline_reference"
            if all(not decision.network_used for decision in inputs.decisions)
            else "live_measured"
        ),
        "status": state.status.value,
        "stop_reason": None if state.stop_reason is None else state.stop_reason.value,
        "protocol": {
            "gate_policy_sha256": inputs.decisions[0].gate_policy_sha256,
            "selection_lock_sha256": inputs.decisions[0].selection_lock_sha256,
            "evaluation_protocol_sha256": inputs.decisions[
                0
            ].evaluation_protocol_sha256,
            "model_lock_sha256": inputs.decisions[0].model_lock_sha256,
            "final_lock_sha256": (
                None
                if inputs.final_report is None
                else inputs.final_report.final_lock_sha256
            ),
            "mode": inputs.decisions[0].mode,
            "measurement_kind": inputs.decisions[0].measurement_kind.value,
            "first_decided_at": inputs.decisions[0]
            .decided_at.isoformat()
            .replace("+00:00", "Z"),
            "last_decided_at": inputs.decisions[-1]
            .decided_at.isoformat()
            .replace("+00:00", "Z"),
        },
        "lineage": {
            "registry_id": registry.registry_id,
            "lineage_id": registry.lineage_id,
            "event_count": len(registry.events),
            "head_event_sha256": registry.events[-1].event_sha256,
            "current_accepted_skill_sha256": registry.current_accepted_sha256,
            "nodes": nodes,
        },
        "rounds": rounds,
        "accepted_capability_curve": accepted_curve,
        "loop_totals": {
            "completed_rounds": state.completed_rounds,
            "cost_amount": str(state.total_cost_amount),
            "cost_currency": state.cost_currency,
            "cost_complete": state.cost_complete,
            "input_tokens": state.total_input_tokens,
            "output_tokens": state.total_output_tokens,
        },
        "final_aggregate": final_payload,
    }
    validate_public_data(data)
    return data


def _curve_svg(rounds: list[dict[str, object]]) -> str:
    width = 760
    height = 220
    left = 54
    top = 20
    plot_width = width - left - 24
    plot_height = height - top - 42
    count = len(rounds)
    x_step = plot_width / max(1, count - 1)

    accepted_points: list[str] = []
    candidate_points: list[str] = []
    costs = [Decimal(str(row["cumulative_cost_amount"])) for row in rounds]
    max_cost = max(costs, default=Decimal(1)) or Decimal(1)
    cost_points: list[str] = []
    for index, row in enumerate(rounds):
        x = left + index * x_step
        accepted = float(str(row["accepted_pass_rate_after_gate"]))
        selection = row["selection"]
        if not isinstance(selection, dict):
            raise ValueError("L3 selection payload is invalid")
        candidate = float(selection["candidate_pass_rate"])
        accepted_y = top + plot_height * (1 - accepted)
        candidate_y = top + plot_height * (1 - candidate)
        cost_y = top + plot_height * (1 - float(costs[index] / max_cost))
        accepted_points.append(f"{x:.1f},{accepted_y:.1f}")
        candidate_points.append(f"{x:.1f},{candidate_y:.1f}")
        cost_points.append(f"{x:.1f},{cost_y:.1f}")
    return f"""<svg class="curve" viewBox="0 0 {width} {height}" role="img" aria-label="Accepted capability, candidate capability, and cumulative cost by round">
<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_height}" class="axis"/><line x1="{left}" y1="{top + plot_height}" x2="{width - 24}" y2="{top + plot_height}" class="axis"/>
<text x="6" y="{top + 6}" class="tick">100%</text><text x="22" y="{top + plot_height + 5}" class="tick">0</text>
<polyline points="{" ".join(accepted_points)}" class="accepted-line"/><polyline points="{" ".join(candidate_points)}" class="candidate-line"/><polyline points="{" ".join(cost_points)}" class="cost-line"/>
</svg>"""


def render_l3_html(inputs: L3ReportInputs) -> str:
    """Render a bounded single-file report without external resources."""

    data = build_l3_data(inputs)
    rounds = data["rounds"]
    lineage = data["lineage"]
    totals = data["loop_totals"]
    protocol = data["protocol"]
    if (
        not isinstance(rounds, list)
        or not isinstance(lineage, dict)
        or not isinstance(totals, dict)
        or not isinstance(protocol, dict)
    ):
        raise ValueError("L3 semantic payload is invalid")

    node_cards = []
    nodes = lineage["nodes"]
    if not isinstance(nodes, list):
        raise ValueError("L3 lineage nodes are invalid")
    for value in nodes:
        if not isinstance(value, dict):
            raise ValueError("L3 lineage node is invalid")
        status = str(value["status"])
        current = " · current" if value["is_current"] else ""
        parent = value["parent_skill_sha256"]
        parent_text = "root" if parent is None else str(parent)[:12]
        node_cards.append(
            f'<article class="node {html.escape(status)}"><strong>{html.escape(str(value["version_id"]))}</strong>'
            f"<span>{html.escape(status + current)}</span><code>{html.escape(str(value['skill_sha256'])[:12])}</code>"
            f"<small>parent {html.escape(parent_text)}</small></article>"
        )

    rows = []
    for value in rounds:
        if not isinstance(value, dict) or not isinstance(value["selection"], dict):
            raise ValueError("L3 round payload is invalid")
        selection = value["selection"]
        outcome = str(value["gate_outcome"])
        reasons = ", ".join(str(item) for item in value["gate_reasons"])
        completeness = "complete" if value["cost_complete"] else "incomplete"
        rows.append(
            "<tr>"
            f"<th>{value['round_number']}</th><td><code>{html.escape(str(value['parent_skill_sha256'])[:12])}</code> → <code>{html.escape(str(value['candidate_skill_sha256'])[:12])}</code></td>"
            f'<td><span class="badge {html.escape(outcome)}">{html.escape(outcome)}</span><br><small>{html.escape(reasons)}</small></td>'
            f"<td>{float(selection['accepted_pass_rate']):.1%} → {float(selection['candidate_pass_rate']):.1%}<br>Δ {float(selection['quality_delta']):+.1%}</td>"
            f"<td>{html.escape(str(value['round_cost_amount']))} {html.escape(str(value['cost_currency']))}<br><small>{completeness}</small></td>"
            f"<td>{value['input_tokens']} / {value['output_tokens']}</td>"
            "</tr>"
        )

    final = data["final_aggregate"]
    if isinstance(final, dict):
        final_html = f"""<section class="panel final"><h2>Final aggregate — isolated after the loop</h2>
<p class="notice">One-time aggregate only. It did not enter reflection, patch generation, or another round.</p>
<div class="metrics"><div><small>Cases</small><strong>{final["case_count"]}</strong></div><div><small>Pass rate</small><strong>{float(final["pass_rate"]):.1%}</strong></div><div><small>Result source</small><strong>{html.escape(str(final["result_source"]))}</strong></div><div><small>Final cost</small><strong>{html.escape(str(final["cost_amount"]))} {html.escape(str(final["cost_currency"]))}</strong></div></div>
<p>{html.escape(str(final["measurement_kind"]))} · network_used={str(final["network_used"]).lower()} · cost {("complete" if final["cost_complete"] else "incomplete")}</p></section>"""
    else:
        final_html = """<section class="panel final"><h2>Final aggregate</h2><p class="notice">Not run. This report does not infer or fabricate a final result.</p></section>"""

    serialized = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).replace("<", "\\u003c")
    result_label = (
        "Fixed/offline reference"
        if data["result_kind"] == "fixed_offline_reference"
        else "Live measured"
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>L3 bounded evolution report</title>
<style>
:root{{--ink:#18202b;--muted:#647080;--paper:#f4f6f8;--card:#fff;--line:#d9dee6;--accepted:#137a53;--rejected:#b13a32;--candidate:#3159c8;--cost:#b36b00}}*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.5 system-ui,-apple-system,sans-serif}}main{{max-width:1180px;margin:auto;padding:30px}}h1{{margin:0 0 4px;font-size:30px}}h2{{margin-top:0}}code{{font-size:.9em}}.notice,.meta,small{{color:var(--muted)}}.panel{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px;margin:18px 0}}.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}}.metrics div{{padding:12px;background:#f7f9fb;border-radius:8px}}.metrics small,.metrics strong{{display:block}}.metrics strong{{font-size:1.2rem}}.dag{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px}}.node{{display:grid;gap:3px;border:1px solid var(--line);border-left:5px solid var(--candidate);border-radius:8px;padding:12px}}.node.accepted{{border-left-color:var(--accepted)}}.node.rejected{{border-left-color:var(--rejected)}}table{{width:100%;border-collapse:collapse}}th,td{{border:1px solid var(--line);padding:10px;text-align:left;vertical-align:top}}thead th{{background:#edf1f5}}.badge{{font-weight:700}}.badge.accepted{{color:var(--accepted)}}.badge.rejected{{color:var(--rejected)}}.curve{{width:100%;max-height:250px;background:#fbfcfd;border:1px solid var(--line)}}.axis{{stroke:#8a94a2;stroke-width:1}}.tick{{font-size:11px;fill:#647080}}polyline{{fill:none;stroke-width:4}}.accepted-line{{stroke:var(--accepted)}}.candidate-line{{stroke:var(--candidate);stroke-dasharray:8 5}}.cost-line{{stroke:var(--cost);stroke-width:2}}.legend span{{margin-right:18px}}.legend .a{{color:var(--accepted)}}.legend .c{{color:var(--candidate)}}.legend .k{{color:var(--cost)}}.final{{border-left:5px solid #6a3db5}}@media(max-width:760px){{main{{padding:14px}}table{{display:block;overflow:auto}}}}
</style></head><body><main>
<header><h1>L3 bounded evolution report</h1><p class="meta">{result_label} · {html.escape(str(data["experiment_id"]))} · status {html.escape(str(data["status"]))} · stop {html.escape(str(data["stop_reason"]))}</p></header>
<section class="panel"><h2>Version DAG and rejected branches</h2><div class="dag">{"".join(node_cards)}</div><p class="meta">Registry {html.escape(str(lineage["registry_id"]))} · events {lineage["event_count"]} · head <code>{html.escape(str(lineage["head_event_sha256"])[:16])}</code></p></section>
<section class="panel"><h2>Capability and cost curve</h2><p class="legend"><span class="a">● accepted capability</span><span class="c">● candidate capability</span><span class="k">● cumulative loop cost</span></p>{_curve_svg(rounds)}<p class="notice">Capability uses locked selection aggregate pass rates. The cost line uses cumulative loop cost on its own normalized axis. Final is intentionally excluded.</p></section>
<section class="panel"><h2>Complete Gate rounds</h2><table><thead><tr><th>Round</th><th>Parent → candidate</th><th>Gate</th><th>Selection capability</th><th>Round cost</th><th>Input / output tokens</th></tr></thead><tbody>{"".join(rows)}</tbody></table><p class="notice">Develop fresh-run provenance is retained by the experiment, but this aggregate contract has no develop quality score. The report leaves it unavailable instead of inventing one.</p></section>
<section class="panel"><h2>Loop totals</h2><div class="metrics"><div><small>Rounds</small><strong>{totals["completed_rounds"]}</strong></div><div><small>Cost</small><strong>{html.escape(str(totals["cost_amount"]))} {html.escape(str(totals["cost_currency"]))}</strong></div><div><small>Cost coverage</small><strong>{"complete" if totals["cost_complete"] else "incomplete"}</strong></div><div><small>Tokens</small><strong>{totals["input_tokens"]} / {totals["output_tokens"]}</strong></div></div></section>
<section class="panel"><h2>Locked protocol provenance</h2><p>{html.escape(str(protocol["measurement_kind"]))} · mode {html.escape(str(protocol["mode"]))} · {html.escape(str(protocol["first_decided_at"]))} to {html.escape(str(protocol["last_decided_at"]))}</p><p>Model lock <code>{html.escape(str(protocol["model_lock_sha256"]))}</code><br>Evaluation protocol <code>{html.escape(str(protocol["evaluation_protocol_sha256"]))}</code><br>Selection lock <code>{html.escape(str(protocol["selection_lock_sha256"]))}</code><br>Final lock <code>{html.escape(str(protocol["final_lock_sha256"]))}</code></p></section>
{final_html}
<script type="application/json" id="l3-data">{serialized}</script>
</main></body></html>"""


def write_l3_html(
    experiment_root: Path,
    destination: Path,
    *,
    registry_root: Path | None = None,
) -> Path:
    """Verify one experiment and write its bounded self-contained L3 report."""

    rendered = render_l3_html(
        load_l3_inputs(experiment_root, registry_root=registry_root)
    )
    payload = rendered.encode("utf-8")
    lowered = rendered.casefold()
    if len(payload) >= _MAX_L3_BYTES:
        raise ValueError("L3 HTML exceeds the 2 MB single-file limit")
    if any(
        marker in lowered
        for marker in (
            "http://",
            "https://",
            "<script src=",
            "<link rel=",
            "file://",
        )
    ):
        raise ValueError("L3 HTML contains an external resource or local URL")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    return destination


__all__ = [
    "L3ReportInputs",
    "build_l3_data",
    "load_l3_inputs",
    "render_l3_html",
    "write_l3_html",
]
