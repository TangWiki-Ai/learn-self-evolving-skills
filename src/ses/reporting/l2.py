"""Self-contained, offline L2 paired-comparison renderer."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

from ses.contracts import MeasurementKind, TriggerEvalResult
from ses.contracts.runner import PairCategory, PairedComparison

_KEY = re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{8,}")


def _verify_l2_inputs(
    paired: PairedComparison,
    trigger: TriggerEvalResult,
    artifact_root: Path,
) -> None:
    if paired.skill_sha256 != trigger.skill_sha256:
        raise ValueError("L2 inputs refer to different Skill artifacts")
    from ses.skills.paired import compare_run_events

    expected = compare_run_events(
        artifact_root / paired.baseline_events.path,
        artifact_root / paired.skill_events.path,
        output_root=artifact_root,
        measurement_kind=paired.measurement_kind,
        measured_at=paired.measured_at,
        engine_version=paired.engine_version,
        model_id=paired.model_id,
    )
    if expected != paired:
        raise ValueError("L2 paired comparison does not match its event evidence")


def _result_kind(
    paired: PairedComparison, trigger: TriggerEvalResult
) -> tuple[str, str]:
    if (
        paired.measurement_kind is MeasurementKind.LIVE_MEASURED
        and trigger.measurement_kind is MeasurementKind.LIVE_MEASURED
    ):
        return "live_measured", "Live measured"
    if (
        paired.measurement_kind is MeasurementKind.SYNTHETIC_OFFLINE
        and trigger.measurement_kind is MeasurementKind.SYNTHETIC_OFFLINE
    ):
        return "fixed_offline_reference", "Fixed/offline reference"
    return "mixed_measurement", "Mixed live and synthetic/offline measurements"


def _label(value: str) -> str:
    if value == "fixed_offline_reference":
        return "Fixed/offline reference"
    if value == "live_measured":
        return "Live measured"
    return "Live measured Creator/Trigger · fixed/offline paired comparison"


def _safe_payload(
    paired: PairedComparison, trigger: TriggerEvalResult
) -> tuple[dict[str, object], str, str]:
    result_kind, label = _result_kind(paired, trigger)
    payload: dict[str, object] = {
        "schema_version": "v1alpha1",
        "record_type": "l2_report_data",
        "result_kind": result_kind,
        "paired": paired.model_dump(mode="json"),
        "trigger": trigger.model_dump(mode="json"),
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    lowered = serialized.casefold()
    if (
        _KEY.search(serialized)
        or "/users/" in lowered
        or "/private/" in lowered
        or "siliconflow_api_key" in lowered
        or '"gold"' in lowered
        or '"hidden_' in lowered
    ):
        raise ValueError("L2 source data contains sensitive or private material")
    return payload, serialized.replace("<", "\\u003c"), label


def render_l2_html(
    paired: PairedComparison,
    trigger: TriggerEvalResult,
    *,
    artifact_root: Path,
) -> str:
    """Render metrics and evidence links without network dependencies."""

    _verify_l2_inputs(paired, trigger, artifact_root.resolve())
    _, serialized, result_label = _safe_payload(paired, trigger)
    score_delta = paired.skill_pass_rate - paired.baseline_pass_rate
    cost_delta = paired.skill_cost_amount - paired.baseline_cost_amount
    category_cards = "".join(
        f"<li><strong>{html.escape(category.value)}</strong><span>{paired.category_counts[category]}</span></li>"
        for category in PairCategory
    )
    rows = []
    for row in paired.cases:
        evidence = (
            ("baseline Trace", row.baseline_trace),
            ("Skill Trace", row.skill_trace),
            ("baseline StateDiff", row.baseline_state_diff),
            ("Skill StateDiff", row.skill_state_diff),
            ("baseline CaseGrade", row.baseline_grade),
            ("Skill CaseGrade", row.skill_grade),
        )
        links = " · ".join(
            f'<a href="{html.escape(reference.path, quote=True)}">{label}</a>'
            for label, reference in evidence
            if reference is not None
        )
        rows.append(
            "<tr>"
            f"<th>{html.escape(row.case_id)}</th>"
            f'<td><span class="badge {html.escape(row.category.value)}">{html.escape(row.category.value)}</span></td>'
            f"<td>{html.escape(row.baseline_status.value)} → {html.escape(row.skill_status.value)}<br>{row.baseline_score:.0f} → {row.skill_score:.0f} ({row.score_delta:+.0f})</td>"
            f"<td>{row.baseline_input_tokens + row.baseline_output_tokens} → {row.skill_input_tokens + row.skill_output_tokens}</td>"
            f"<td>{row.baseline_cost_amount} → {row.skill_cost_amount} {html.escape(paired.cost_currency)}</td>"
            f"<td>{row.baseline_latency_ms} → {row.skill_latency_ms} ms</td>"
            f'<td class="links">{links or "No terminal evidence"}</td>'
            "</tr>"
        )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Paired L2 comparison</title>
<style>
:root{{--ink:#152238;--muted:#5f6b7a;--paper:#f5f7fb;--card:#fff;--blue:#3157d5;--green:#16794b;--red:#b42318;--line:#d9e0ea}}*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.5 system-ui,-apple-system,sans-serif}}main{{max-width:1400px;margin:auto;padding:32px}}h1{{font-size:30px;margin:0 0 6px}}h2{{margin-top:32px}}.notice{{color:var(--muted)}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}}.metric,.panel{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px}}.metric strong{{display:block;font-size:25px}}.categories{{list-style:none;padding:0;display:grid;grid-template-columns:repeat(4,1fr);gap:10px}}.categories li{{background:#fff;border:1px solid var(--line);border-radius:8px;padding:12px;display:flex;justify-content:space-between}}table{{width:100%;border-collapse:collapse;background:#fff}}th,td{{padding:10px;border:1px solid var(--line);text-align:left;vertical-align:top}}thead th{{background:#eaf0ff}}.badge{{white-space:nowrap;font-weight:700}}.fail-to-pass{{color:var(--green)}}.pass-to-fail{{color:var(--red)}}.links a{{display:inline-block;color:var(--blue)}}.bars{{display:flex;gap:18px;align-items:flex-end;height:160px}}.bar{{width:110px;background:var(--blue);color:white;padding:8px;text-align:center;min-height:24px}}.bar.skill{{background:var(--green)}}@media(max-width:800px){{main{{padding:16px}}.categories{{grid-template-columns:1fr 1fr}}table{{display:block;overflow:auto}}}}
</style></head><body><main>
<h1>Paired L2 comparison</h1><p class="notice">{result_label} · measured {paired.measured_at.isoformat()} · data {html.escape(paired.data_version[:12])} · model {html.escape(paired.model_id)} · protocol {html.escape(paired.protocol_sha256[:12])} · Skill {html.escape(paired.skill_sha256[:12])}</p>
<section class="grid" aria-label="Overall metrics">
<div class="metric">Baseline pass rate<strong>{paired.baseline_pass_rate:.1%}</strong></div>
<div class="metric">Skill v0 pass rate<strong>{paired.skill_pass_rate:.1%}</strong></div>
<div class="metric">Score improvement / regression<strong>{score_delta:+.1%}</strong></div>
<div class="metric">Trigger precision<strong>{trigger.precision:.1%}</strong></div>
<div class="metric">Trigger recall<strong>{trigger.recall:.1%}</strong></div>
<div class="metric">Indeterminate triggers<strong>{trigger.indeterminate_count}</strong></div>
</section>
<h2>Paired outcomes</h2><ul class="categories">{category_cards}</ul>
<section class="grid"><div class="panel"><h2>Score distribution</h2><div class="bars"><div class="bar" style="height:{max(24, paired.baseline_pass_rate * 140):.0f}px">Baseline {paired.baseline_pass_rate:.0%}</div><div class="bar skill" style="height:{max(24, paired.skill_pass_rate * 140):.0f}px">Skill {paired.skill_pass_rate:.0%}</div></div></div>
<div class="panel"><h2>Cost difference</h2><p>Baseline: {paired.baseline_cost_amount} {html.escape(paired.cost_currency)}</p><p>Skill: {paired.skill_cost_amount} {html.escape(paired.cost_currency)}</p><p>Delta: {cost_delta:+} {html.escape(paired.cost_currency)}</p><p>Tokens: {paired.baseline_input_tokens + paired.baseline_output_tokens} → {paired.skill_input_tokens + paired.skill_output_tokens}</p><p>Elapsed: {paired.baseline_latency_ms} → {paired.skill_latency_ms} ms</p></div></section>
<h2>Provenance</h2><div class="panel"><p>Pair execution: {paired.pair_execution_sha256}</p><p>Model lock: {paired.model_lock_sha256}</p><p>Baseline log: {paired.baseline_events.sha256}</p><p>Skill log: {paired.skill_events.sha256}</p><p>Trigger prompt set: {trigger.prompt_set_sha256}</p><p>Trigger model: {html.escape(trigger.model_id)} · {trigger.measured_at.isoformat()}</p></div>
<h2>Case-level evidence</h2><table><thead><tr><th>Case</th><th>Pair class</th><th>Score</th><th>Tokens</th><th>Cost</th><th>Elapsed</th><th>Evidence</th></tr></thead><tbody>{"".join(rows)}</tbody></table>
<script type="application/json" id="l2-data">{serialized}</script>
</main></body></html>"""


def write_l2_html(
    paired: PairedComparison,
    trigger: TriggerEvalResult,
    destination: Path,
    *,
    artifact_root: Path,
) -> Path:
    rendered = render_l2_html(paired, trigger, artifact_root=artifact_root)
    encoded = rendered.encode("utf-8")
    if len(encoded) >= 2_000_000:
        raise ValueError("L2 HTML exceeds the 2 MB target")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(encoded)
    return destination
