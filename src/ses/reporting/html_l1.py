"""Self-contained offline HTML rendering for an L1 baseline report."""

from __future__ import annotations

import html
import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from ses.contracts.security import validate_public_data
from ses.reporting.baseline import build_baseline_report

_CSS = """
:root{color-scheme:light;font-family:ui-sans-serif,system-ui,sans-serif;color:#17202a;background:#f6f7f9}
body{max-width:1180px;margin:0 auto;padding:24px}h1,h2,h3{line-height:1.2}h1{margin-bottom:4px}
.muted{color:#5f6b76}.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:20px 0}
.card,details{background:#fff;border:1px solid #dfe3e8;border-radius:8px;padding:14px}.metric{font-size:1.5rem;font-weight:700}
table{width:100%;border-collapse:collapse;background:#fff}th,td{text-align:left;border-bottom:1px solid #e5e8eb;padding:10px;vertical-align:top}
th{background:#eef1f4}.status{font-weight:700}.pass{color:#14733b}.agent_fail,.judge_error,.infrastructure_error,.budget_stop{color:#a33b20}.not_evaluated{color:#68737d}
details{margin:10px 0}summary{cursor:pointer;font-weight:650}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f5f7;padding:10px;border-radius:6px}
ol{padding-left:22px}.repeat{border-left:4px solid #dfe3e8;margin-top:12px;padding-left:12px}
""".strip()


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _json(value: object) -> str:
    return html.escape(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2), quote=False
    )


def _status(value: object) -> str:
    text = str(value)
    allowed = {
        "pass",
        "agent_fail",
        "simulator_error",
        "judge_error",
        "infrastructure_error",
        "budget_stop",
        "not_evaluated",
    }
    return text if text in allowed else "not_evaluated"


def _artifact_links(result: Mapping[str, object]) -> str:
    value = result.get("artifacts")
    if not isinstance(value, Mapping):
        return ""
    links: list[str] = []
    for name, reference in value.items():
        references = reference if isinstance(reference, Sequence) else (reference,)
        for index, item in enumerate(references):
            if not isinstance(item, Mapping) or not isinstance(item.get("path"), str):
                continue
            path = str(item["path"])
            label = f"{name} {index + 1}" if len(references) > 1 else str(name)
            links.append(f'<li><a href="{_escape(path)}">{_escape(label)}</a></li>')
    return f"<details><summary>Artifact records</summary><ul>{''.join(links)}</ul></details>"


def _repetition(result: Mapping[str, object]) -> str:
    status = _status(result.get("status"))
    usage_value = result.get("usage")
    usage: Mapping[str, object] = (
        usage_value if isinstance(usage_value, Mapping) else {}
    )
    evidence = result.get("evidence", [])
    timeline = result.get("tool_timeline", [])
    state_diff = result.get("state_diff", {})
    transcript = result.get("transcript", [])
    return f"""
<section class="repeat">
  <h3>{_escape(result.get("iteration_id"))}: <span class="status {status}">{status}</span></h3>
  <p><strong>Usage / cost / latency:</strong> input={_escape(usage.get("input_tokens"))}, output={_escape(usage.get("output_tokens"))}, cost={_escape(usage.get("cost_amount"))} {_escape(usage.get("cost_currency"))}, latency={_escape(result.get("latency_ms"))} ms, turns={_escape(result.get("turn_count"))}</p>
  <details open><summary>Evidence</summary><pre>{_json(evidence)}</pre></details>
  {_artifact_links(result)}
  <details><summary>Tool timeline</summary><pre>{_json(timeline)}</pre></details>
  <details><summary>StateDiff</summary><pre>{_json(state_diff)}</pre></details>
  <details><summary>Transcript</summary><pre>{_json(transcript)}</pre></details>
</section>""".strip()


def render_l1_html(report: Mapping[str, object]) -> str:
    """Render a static report with inline CSS and no scripts or remote assets."""
    validate_public_data(report)
    metrics_value = report.get("metrics")
    totals_value = report.get("totals")
    metrics: Mapping[str, object] = (
        metrics_value if isinstance(metrics_value, Mapping) else {}
    )
    totals: Mapping[str, object] = (
        totals_value if isinstance(totals_value, Mapping) else {}
    )
    raw_cases = report.get("cases")
    cases = (
        raw_cases
        if isinstance(raw_cases, Sequence) and not isinstance(raw_cases, str)
        else []
    )
    rows: list[str] = []
    details: list[str] = []
    for value in cases:
        if not isinstance(value, Mapping):
            continue
        case_id = value.get("case_id")
        first_status = _status(value.get("first_status"))
        repetitions_value = value.get("repetitions")
        repetitions = (
            repetitions_value if isinstance(repetitions_value, Sequence) else []
        )
        statuses = [
            _status(item.get("status"))
            for item in repetitions
            if isinstance(item, Mapping)
        ]
        rows.append(
            f'<tr><td><a href="#{_escape(case_id)}">{_escape(case_id)}</a></td>'
            f'<td class="status {first_status}">{first_status}</td>'
            f"<td>{_escape(', '.join(statuses))}</td></tr>"
        )
        rendered_repetitions = "".join(
            _repetition(item) for item in repetitions if isinstance(item, Mapping)
        )
        details.append(
            f'<details id="{_escape(case_id)}"><summary>{_escape(case_id)} — Repeated results</summary>{rendered_repetitions}</details>'
        )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>L1 baseline {_escape(report.get("run_id"))}</title><style>{_CSS}</style></head>
<body><header><h1>L1 reproducible baseline</h1><p class="muted">Run {_escape(report.get("run_id"))} · formula {_escape(report.get("formula_version"))}</p></header>
<main><section class="metrics" aria-label="Summary metrics">
<div class="card"><div class="muted">pass@1</div><div class="metric">{_escape(metrics.get("pass_at_1"))}</div></div>
<div class="card"><div class="muted">pass^k (k={_escape(metrics.get("k"))})</div><div class="metric">{_escape(metrics.get("pass_power_k"))}</div></div>
<div class="card"><div class="muted">Cases / iterations</div><div class="metric">{_escape(metrics.get("sample_size"))} / {_escape(metrics.get("iteration_sample_size"))}</div></div>
<div class="card"><div class="muted">Tokens</div><div class="metric">{_escape(totals.get("input_tokens"))} in / {_escape(totals.get("output_tokens"))} out</div></div>
<div class="card"><div class="muted">Cost</div><div class="metric">{_escape(totals.get("cost_amount"))} {_escape(totals.get("cost_currency"))}</div></div>
<div class="card"><div class="muted">Latency</div><div class="metric">{_escape(totals.get("latency_ms"))} ms</div></div>
</section>
<h2>Per-case status</h2><table><thead><tr><th>Case</th><th>First result</th><th>Repeated results</th></tr></thead><tbody>{"".join(rows)}</tbody></table>
<h2>Evidence and traces</h2>{"".join(details)}</main></body></html>"""


def write_l1_html(events_path: Path, output_path: Path) -> Path:
    """Read runner records and write one bounded, offline HTML file."""
    payload = render_l1_html(build_baseline_report(events_path)).encode("utf-8")
    if len(payload) >= 2 * 1024 * 1024:
        raise ValueError("L1 HTML exceeds the 2 MB single-file limit")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(payload)
    return output_path
