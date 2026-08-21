"""Self-contained, offline L2 paired-comparison renderer."""

from __future__ import annotations

import hashlib
import html
import json
import re
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from ses.contracts import (
    ArtifactRef,
    ArtifactRoot,
    MeasurementKind,
    SchemaVersion,
    TriggerEvalResult,
)
from ses.contracts.runner import PairCategory, PairedComparison
from ses.contracts.shopping import (
    ShoppingMetricProjection,
    ShoppingPairMetrics,
    ShoppingPairStratumMetrics,
    ShoppingScenario,
    ShopSimulatorEpisodeResult,
)

_KEY = re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{8,}")


def _verify_l2_inputs(
    paired: PairedComparison,
    trigger: TriggerEvalResult,
    artifact_root: Path,
) -> ShoppingPairMetrics | None:
    if paired.skill_sha256 != trigger.skill_sha256:
        raise ValueError("L2 inputs refer to different Skill artifacts")
    from ses.skills.paired import compare_run_events

    shopping_ref = paired.shopping_metrics
    expected = compare_run_events(
        artifact_root / paired.baseline_events.path,
        artifact_root / paired.skill_events.path,
        output_root=artifact_root,
        measurement_kind=paired.measurement_kind,
        measured_at=paired.measured_at,
        engine_version=paired.engine_version,
        model_id=paired.model_id,
        shopping_metrics_builder=(
            (lambda _execution_sha256, _rows: shopping_ref)
            if shopping_ref is not None
            else None
        ),
    )
    if expected != paired:
        raise ValueError("L2 paired comparison does not match its event evidence")
    if paired.schema_version is SchemaVersion.V1ALPHA1:
        return None
    if shopping_ref is None:
        raise ValueError("L2 shopping pair is missing shopping metrics")
    return _verify_shopping_metrics(paired, shopping_ref, artifact_root)


def _verified_bytes(root: Path, reference: ArtifactRef) -> bytes:
    if reference.root is not ArtifactRoot.RUN:
        raise ValueError("L2 shopping evidence must use its run root")
    path = root / reference.path
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ValueError("L2 shopping evidence escapes its artifact root") from exc
    if path.is_symlink() or not path.is_file():
        raise ValueError("L2 shopping evidence must be a regular file")
    content = path.read_bytes()
    reference.verify_bytes(content)
    return content


def _rooted(run_id: str, reference: ArtifactRef) -> ArtifactRef:
    if reference.path.startswith(f"{run_id}/"):
        return reference
    return reference.model_copy(update={"path": f"{run_id}/{reference.path}"})


def _domain_metric(
    root: Path,
    reference: ArtifactRef,
) -> tuple[ShopSimulatorEpisodeResult, ShoppingMetricProjection]:
    result = ShopSimulatorEpisodeResult.model_validate_json(
        _verified_bytes(root, reference)
    )
    metric = ShoppingMetricProjection.model_validate_json(
        _verified_bytes(root, _rooted(result.run_id, result.metric))
    )
    if metric.safety_violation_count != result.safety_violation_count:
        raise ValueError("L2 shopping metrics disagree with episode safety evidence")
    return result, metric


def _verify_shopping_metrics(
    paired: PairedComparison,
    reference: ArtifactRef,
    root: Path,
) -> ShoppingPairMetrics:
    try:
        metrics = ShoppingPairMetrics.model_validate_json(
            _verified_bytes(root, reference)
        )
    except ValueError as exc:
        raise ValueError("L2 shopping metrics are invalid") from exc
    comparable = tuple(row for row in paired.cases if row.comparable)
    if (
        metrics.pair_execution_sha256 != paired.pair_execution_sha256
        or metrics.case_count != len(paired.cases)
        or metrics.comparable_case_count != len(comparable)
        or metrics.baseline_cost_amount != paired.baseline_cost_amount
        or metrics.skill_cost_amount != paired.skill_cost_amount
        or metrics.cost_currency != paired.cost_currency
    ):
        raise ValueError("L2 shopping metrics disagree with the canonical Pair")

    by_scenario: dict[
        ShoppingScenario,
        list[tuple[ShoppingMetricProjection, ShoppingMetricProjection]],
    ] = defaultdict(list)
    for row in paired.cases:
        baseline = skill = None
        if row.baseline_domain_result is not None:
            baseline = _domain_metric(root, row.baseline_domain_result)
        if row.skill_domain_result is not None:
            skill = _domain_metric(root, row.skill_domain_result)
        for result_and_metric, expected_run_id, expected_skill_hash in (
            (baseline, paired.baseline_run_id, hashlib.sha256(b"").hexdigest()),
            (skill, paired.skill_run_id, paired.skill_sha256),
        ):
            if result_and_metric is None:
                continue
            result, _ = result_and_metric
            if (
                result.run_id != expected_run_id
                or result.case_id != row.case_id
                or result.skill_sha256 != expected_skill_hash
                or result.profile_sha256 != metrics.profile_sha256
                or result.model_lock_sha256 != paired.model_lock_sha256
                or result.measurement_level.value != paired.measurement_kind.value
            ):
                raise ValueError("L2 shopping domain result drifted from the Pair")
        if not row.comparable:
            continue
        if baseline is None or skill is None:
            raise ValueError("L2 comparable shopping row lacks domain evidence")
        baseline_result, baseline_metric = baseline
        skill_result, skill_metric = skill
        if baseline_result.scenario is not skill_result.scenario:
            raise ValueError("L2 shopping pair scenarios disagree")
        by_scenario[baseline_result.scenario].append((baseline_metric, skill_metric))

    expected_strata = tuple(
        _expected_stratum(
            source,
            by_scenario[source.scenario],
        )
        for source in metrics.strata
    )
    baseline_strict = sum(
        (baseline.r_strict for pairs in by_scenario.values() for baseline, _ in pairs),
        Decimal(0),
    )
    skill_strict = sum(
        (skill.r_strict for pairs in by_scenario.values() for _, skill in pairs),
        Decimal(0),
    )
    if (
        metrics.strata != expected_strata
        or metrics.baseline_full_success_count
        != sum(row.baseline_full_success_count for row in expected_strata)
        or metrics.skill_full_success_count
        != sum(row.skill_full_success_count for row in expected_strata)
        or metrics.baseline_safety_violation_count
        != sum(row.baseline_safety_violation_count for row in expected_strata)
        or metrics.skill_safety_violation_count
        != sum(row.skill_safety_violation_count for row in expected_strata)
        or metrics.baseline_mean_strict_reward
        != (baseline_strict / len(comparable) if comparable else Decimal(0))
        or metrics.skill_mean_strict_reward
        != (skill_strict / len(comparable) if comparable else Decimal(0))
    ):
        raise ValueError("L2 shopping metrics disagree with domain evidence")
    return metrics


def _expected_stratum(
    source: ShoppingPairStratumMetrics,
    pairs: list[tuple[ShoppingMetricProjection, ShoppingMetricProjection]],
) -> ShoppingPairStratumMetrics:
    count = len(pairs)
    return ShoppingPairStratumMetrics(
        scenario=source.scenario,
        case_count=source.case_count,
        comparable_case_count=count,
        baseline_full_success_count=sum(baseline.course_pass for baseline, _ in pairs),
        skill_full_success_count=sum(skill.course_pass for _, skill in pairs),
        baseline_mean_strict_reward=(
            sum((baseline.r_strict for baseline, _ in pairs), Decimal(0)) / count
            if count
            else Decimal(0)
        ),
        skill_mean_strict_reward=(
            sum((skill.r_strict for _, skill in pairs), Decimal(0)) / count
            if count
            else Decimal(0)
        ),
        baseline_safety_violation_count=sum(
            baseline.safety_violation_count for baseline, _ in pairs
        ),
        skill_safety_violation_count=sum(
            skill.safety_violation_count for _, skill in pairs
        ),
    )


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
    paired: PairedComparison,
    trigger: TriggerEvalResult,
    shopping_metrics: ShoppingPairMetrics | None,
) -> tuple[dict[str, object], str, str]:
    result_kind, label = _result_kind(paired, trigger)
    payload: dict[str, object] = {
        "schema_version": "v1alpha1",
        "record_type": "l2_report_data",
        "result_kind": result_kind,
        "paired": paired.model_dump(mode="json"),
        "trigger": trigger.model_dump(mode="json"),
    }
    if shopping_metrics is not None:
        payload["shopping_metrics"] = shopping_metrics.model_dump(mode="json")
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

    shopping_metrics = _verify_l2_inputs(
        paired,
        trigger,
        artifact_root.resolve(),
    )
    _, serialized, result_label = _safe_payload(
        paired,
        trigger,
        shopping_metrics,
    )
    score_delta = paired.skill_pass_rate - paired.baseline_pass_rate
    cost_delta = paired.skill_cost_amount - paired.baseline_cost_amount
    cost_panel = (
        f"<p>Baseline: {paired.baseline_cost_amount} {html.escape(paired.cost_currency)}</p>"
        f"<p>Skill: {paired.skill_cost_amount} {html.escape(paired.cost_currency)}</p>"
        f"<p>Delta: {cost_delta:+} {html.escape(paired.cost_currency)}</p>"
        if paired.cost_complete
        else "<p>Baseline: unavailable</p><p>Skill: unavailable</p><p>Delta: unavailable</p>"
    )
    category_cards = "".join(
        f"<li><strong>{html.escape(category.value)}</strong><span>{paired.category_counts[category]}</span></li>"
        for category in PairCategory
    )
    rows = []
    for row in paired.cases:
        row_cost = (
            f"{row.baseline_cost_amount} → {row.skill_cost_amount} "
            f"{html.escape(paired.cost_currency)}"
            if paired.cost_complete
            else "unavailable"
        )
        evidence = (
            ("baseline Trace", row.baseline_trace),
            ("Skill Trace", row.skill_trace),
            ("baseline StateDiff", row.baseline_state_diff),
            ("Skill StateDiff", row.skill_state_diff),
            ("baseline domain result", row.baseline_domain_result),
            ("Skill domain result", row.skill_domain_result),
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
            f"<td>{row_cost}</td>"
            f"<td>{row.baseline_latency_ms} → {row.skill_latency_ms} ms</td>"
            f'<td class="links">{links or "No terminal evidence"}</td>'
            "</tr>"
        )
    shopping_section = _shopping_section(shopping_metrics, paired)
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
<div class="panel"><h2>Cost difference</h2>{cost_panel}<p>Tokens: {paired.baseline_input_tokens + paired.baseline_output_tokens} → {paired.skill_input_tokens + paired.skill_output_tokens}</p><p>Elapsed: {paired.baseline_latency_ms} → {paired.skill_latency_ms} ms</p></div></section>
{shopping_section}
<h2>Provenance</h2><div class="panel"><p>Pair execution: {paired.pair_execution_sha256}</p><p>Model lock: {paired.model_lock_sha256}</p><p>Baseline log: {paired.baseline_events.sha256}</p><p>Skill log: {paired.skill_events.sha256}</p><p>Trigger prompt set: {trigger.prompt_set_sha256}</p><p>Trigger model: {html.escape(trigger.model_id)} · {trigger.measured_at.isoformat()}</p></div>
<h2>Case-level evidence</h2><table><thead><tr><th>Case</th><th>Pair class</th><th>Score</th><th>Tokens</th><th>Cost</th><th>Elapsed</th><th>Evidence</th></tr></thead><tbody>{"".join(rows)}</tbody></table>
<script type="application/json" id="l2-data">{serialized}</script>
</main></body></html>"""


def _shopping_section(
    metrics: ShoppingPairMetrics | None,
    paired: PairedComparison,
) -> str:
    if metrics is None:
        return ""
    rows = "".join(
        "<tr>"
        f"<th>{html.escape(row.scenario.value)}</th>"
        f"<td>{row.comparable_case_count}/{row.case_count}</td>"
        f"<td>{row.baseline_full_success_count} → {row.skill_full_success_count}</td>"
        f"<td>{row.baseline_mean_strict_reward} → {row.skill_mean_strict_reward}</td>"
        f"<td>{row.baseline_safety_violation_count} → {row.skill_safety_violation_count}</td>"
        "</tr>"
        for row in metrics.strata
    )
    assert paired.shopping_metrics is not None
    metrics_link = html.escape(paired.shopping_metrics.path, quote=True)
    cost_delta = (
        f"{metrics.cost_delta_amount:+} {html.escape(metrics.cost_currency)}"
        if paired.cost_complete
        else "unavailable"
    )
    return f"""<h2>Shopping metrics</h2>
<section class="grid" aria-label="Shopping metrics">
<div class="metric">Full-success<strong>{metrics.baseline_full_success_count} → {metrics.skill_full_success_count}</strong></div>
<div class="metric">Mean strict reward<strong>{metrics.baseline_mean_strict_reward} → {metrics.skill_mean_strict_reward}</strong></div>
<div class="metric">Safety violations<strong>{metrics.baseline_safety_violation_count} → {metrics.skill_safety_violation_count}</strong></div>
<div class="metric">Cost delta<strong>{cost_delta}</strong></div>
</section>
<p><a href="{metrics_link}">ShoppingPairMetrics evidence</a></p>
<table><thead><tr><th>Scenario stratum</th><th>Comparable</th><th>Full-success</th><th>Mean strict</th><th>Safety violations</th></tr></thead><tbody>{rows}</tbody></table>"""


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
