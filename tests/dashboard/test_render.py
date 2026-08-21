from __future__ import annotations

from ses.dashboard import STATIONS, render_dashboard_html


def test_home_leads_with_the_graduation_sample_and_all_eight_stations() -> None:
    rendered = render_dashboard_html()

    assert rendered.startswith("<!doctype html>")
    assert "毕业产物样例" in rendered
    assert "Self-evolving Skill 实战" in rendered
    assert "以上均为版式样例" in rendered
    assert "中英简历段落" in rendered
    assert "面试追问准备" in rendered
    assert "概念清单" in rendered
    assert len(STATIONS) == 8
    assert rendered.count('<li class="flow-item" data-flow-station="') == 8
    assert rendered.count('<article class="station-card" id="station-') == 8
    for station_id in range(8):
        assert f"uv run ses journey station {station_id}" in rendered


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
    assert "<main>" in rendered
    assert '<nav class="flow-wrap" aria-label="八站学习流程">' in rendered
    assert '<ol class="flow">' in rendered
    assert 'role="status" aria-live="polite"' in rendered
    assert 'aria-label="课程属性"' in rendered
    assert ":focus-visible" in rendered
    assert "prefers-reduced-motion" in rendered
    assert rendered.count('href="#station-') == 8
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
    assert "原始逐 case 证据" in rendered
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
    assert "FIXED CI JOURNEY" in rendered
    assert "固定 CI 不调用外部 Provider" in rendered
    assert "SiliconFlow" in rendered
    assert "ChatAnywhere" in rendered


def test_home_fails_closed_when_cost_is_unavailable_partial_or_unattributed() -> None:
    rendered = render_dashboard_html()

    assert 'source === "unavailable"' in rendered
    assert "费用不可用 · status 没有可靠成本" in rendered
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
        "command",
        "error_message",
    ):
        assert field in rendered
    assert "SILICONFLOW_API_KEY" not in rendered
    assert "CHATANYWHERE_API_KEY" not in rendered
    assert "sk-" not in rendered
