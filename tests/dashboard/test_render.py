from __future__ import annotations

from ses.dashboard import STATIONS, render_dashboard_html


def test_home_leads_with_run_state_and_one_eight_step_list() -> None:
    rendered = render_dashboard_html()

    assert rendered.startswith("<!doctype html>")
    assert "<h1>运行进度</h1>" in rendered
    assert 'id="current-step-title"' in rendered
    assert 'id="current-command"' in rendered
    assert '<details class="run-details">' in rendered
    assert len(STATIONS) == 8
    assert rendered.count('class="step-item" id="step-') == 8
    assert 'class="flow-item"' not in rendered
    assert 'class="station-card"' not in rendered
    assert 'class="reports-panel"' not in rendered
    assert "最近的输出" not in rendered
    assert "uv run ses journey station" not in rendered


def test_home_does_not_embed_marketing_copy_or_sample_results() -> None:
    rendered = render_dashboard_html()

    for text in (
        "ONE-DAY PRACTICE",
        "从一次失败\uff0c到一段能追问的经历",
        "毕业产物样例",
        "SAMPLE OUTPUT",
        "68.8%",
        "87.5%",
        "+18.7pp",
        "经历生成器",
        "Evidence-backed Portfolio",
    ):
        assert text not in rendered


def test_home_is_self_contained_and_only_polls_same_origin_status() -> None:
    rendered = render_dashboard_html()
    lowered = rendered.casefold()

    assert 'fetch("/.ses/status.json"' in rendered
    assert "window.settimeout(pollstatus, 2200)" in lowered
    assert "http://" not in lowered
    assert "https://" not in lowered
    assert "@import" not in lowered
    assert "@font-face" not in lowered
    assert "<script src=" not in lowered
    assert "<link rel=" not in lowered
    assert "不发起外网请求" in rendered


def test_home_has_keyboard_and_screen_reader_landmarks() -> None:
    rendered = render_dashboard_html()

    assert '<html lang="zh-CN">' in rendered
    assert 'class="skip-link" href="#dashboard"' in rendered
    assert '<main class="shell" id="dashboard">' in rendered
    assert '<ol class="step-list" aria-label="Skill 改进的 8 个步骤">' in rendered
    assert '<progress id="progress-bar" max="8" value="0">' in rendered
    assert '<label class="sr-only" for="progress-bar">' in rendered
    assert 'progressBar.setAttribute("aria-valuetext", progressText)' in rendered
    assert 'role="status" aria-live="polite"' in rendered
    assert ":focus-visible" in rendered
    assert "prefers-reduced-motion" in rendered
    assert rendered.count('aria-live="polite"') == 1
    assert "data-station-artifacts aria-live" not in rendered


def test_home_updates_live_content_only_when_status_changes() -> None:
    rendered = render_dashboard_html()

    assert "let lastPayloadSignature = null" in rendered
    assert "signature !== lastPayloadSignature" in rendered
    assert "container.dataset.renderSignature === signature" in rendered
    assert "if (node && node.textContent !== value)" in rendered


def test_home_uses_filenames_and_collapses_raw_case_evidence() -> None:
    rendered = render_dashboard_html()

    assert "artifactFileName(value, fallback)" in rendered
    assert "link.title = item.path" in rendered
    assert "isPerCaseEvidence(item)" in rendered
    assert 'path.includes("/workspaces/")' in rendered
    assert "原始逐用例记录" in rendered
    assert 'details.className = "raw-artifacts"' in rendered


def test_home_shows_provider_mode_model_lock_and_cost_provenance() -> None:
    rendered = render_dashboard_html()

    assert 'id="experiment-provider-label"' in rendered
    assert 'id="experiment-mode-label"' in rendered
    assert 'id="model-lock-label"' in rendered
    assert 'id="cost-source-label"' in rendered
    assert 'firstValue(data, ["experiment_provider"])' in rendered
    assert 'firstValue(data, ["model_lock_sha256"])' in rendered
    assert 'firstValue(data, ["cost_source"])' in rendered
    assert 'fixed ? "固定 CI" : mode === "live" ? "实时评测"' in rendered
    assert "固定 CI 不调用外部 Provider" in rendered
    assert "SiliconFlow" in rendered
    assert "ChatAnywhere" in rendered


def test_home_fails_closed_when_cost_is_unavailable_partial_or_unattributed() -> None:
    rendered = render_dashboard_html()

    assert 'source === "unavailable"' in rendered
    assert "费用不可用 · 本地状态没有可靠成本" in rendered
    assert "部分数据 · 不能作为完整账单" in rendered
    assert "固定 CI 合成值 · 不代表 live 成本" in rendered
    assert "Claude Code 估算 · 以 Provider 最终账单为准" in rendered
    assert "费用不可用 · 成本来源未记录" in rendered
    assert '|| "CNY"' not in rendered
    assert "实测累计" not in rendered


def test_home_supports_generic_status_fields_without_embedding_status_data() -> None:
    rendered = render_dashboard_html()

    for field in (
        "completed_stations",
        "current_station",
        "experiment_provider",
        "model_lock_sha256",
        "cost_source",
        "experiment_usage",
        "experiment_cost",
        "reports",
        "artifacts",
        "artifact_refs",
        "decision_refs",
        "attention_reason",
        "error_message",
    ):
        assert field in rendered
    assert "SILICONFLOW_API_KEY" not in rendered
    assert "CHATANYWHERE_API_KEY" not in rendered
    assert "sk-" not in rendered


def test_home_uses_quiet_document_style_without_horizontal_step_scrolling() -> None:
    rendered = render_dashboard_html()

    assert "--canvas: #f7f6f1" in rendered
    assert "--brand: #c45c43" in rendered
    assert "ui-serif, Georgia" in rendered
    assert ".step-list" in rendered
    assert "overflow-x: auto" not in rendered
    assert "transform: rotate" not in rendered


def test_home_distinguishes_running_waiting_attention_and_completion() -> None:
    rendered = render_dashboard_html()

    assert 'overall === "running" && currentState === "pending"' in rendered
    assert 'step.setAttribute("aria-current", "step")' in rendered
    assert "当前步骤尚未启动" in rendered
    assert "完整命令请以终端记录为准" in rendered
    assert "无需继续运行" in rendered
    assert "attention_reason" in rendered
    assert "上次内容可能已过期" in rendered


def test_home_does_not_turn_missing_usage_into_zero() -> None:
    rendered = render_dashboard_html()

    assert "function tokenDisplay(value)" in rendered
    assert 'number >= 0 ? number.toLocaleString("zh-CN") : "—"' in rendered
    assert '<dd id="input-token-label">—</dd>' in rendered
    assert '<dd id="output-token-label">—</dd>' in rendered
