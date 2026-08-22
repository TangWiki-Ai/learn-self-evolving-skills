# ruff: noqa: RUF001
"""Render the learner-facing, self-contained journey dashboard."""

from __future__ import annotations

import html
import json

STATIONS: tuple[dict[str, str], ...] = (
    {
        "id": "0",
        "name": "运行基线",
        "decision": "观察 15 个用例的执行结果，先不修改 Skill。",
    },
    {
        "id": "1",
        "name": "选择失败用例",
        "decision": "从基线结果中选择值得继续分析的失败用例。",
    },
    {
        "id": "2",
        "name": "分析失败原因",
        "decision": "判断问题来自环境、用例还是 Skill。",
    },
    {
        "id": "3",
        "name": "定位 Skill 问题",
        "decision": "把诊断定位到具体知识块、步骤或文本位置。",
    },
    {
        "id": "4",
        "name": "做最小修改",
        "decision": "只修改与当前诊断有关的 Skill 内容。",
    },
    {
        "id": "5",
        "name": "回放与回归",
        "decision": "先回放目标用例，再检查完整基线是否出现回归。",
    },
    {
        "id": "6",
        "name": "版本决定",
        "decision": "决定发布、暂缓，或发布后做本地回滚恢复演练。",
    },
    {
        "id": "7",
        "name": "核对输出",
        "decision": "核对由现有运行记录生成的输出文件。",
    },
)


def _step_markup() -> str:
    items: list[str] = []
    for station in STATIONS:
        station_id = station["id"]
        number = int(station_id) + 1
        items.append(
            f"""
<li class="step-item" id="step-{station_id}" data-step-station="{station_id}" data-state="pending">
  <span class="step-marker" aria-hidden="true">{number:02d}</span>
  <div class="step-copy">
    <div class="step-heading"><h3>{html.escape(station["name"])}</h3></div>
    <p>{html.escape(station["decision"])}</p>
    <div class="step-artifacts" data-station-artifacts hidden></div>
  </div>
  <span class="step-state" data-step-state>未开始</span>
</li>""".strip()
        )
    return "\n".join(items)


_DOCUMENT = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <meta name="referrer" content="no-referrer">
  <title>Learn Self-Evolving Skills · 运行进度</title>
  <style>
    :root {
      color-scheme: light;
      --canvas: #f7f6f1;
      --surface: #ffffff;
      --surface-soft: #efede6;
      --ink: #22211e;
      --muted: #67645f;
      --line: rgba(34, 33, 30, .11);
      --brand: #c45c43;
      --brand-strong: #a94b37;
      --brand-soft: rgba(196, 92, 67, .09);
      --green: #2f7451;
      --green-soft: #eef6f0;
      --red: #a44037;
      --red-soft: #fbefed;
      --yellow: #8a651c;
      --yellow-soft: #fbf5e7;
      --radius: 10px;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--canvas);
      color: var(--ink);
      font-synthesis: none;
    }

    * { box-sizing: border-box; }
    html { scroll-behavior: smooth; }
    body { margin: 0; min-width: 320px; background: var(--canvas); }
    a { color: inherit; }
    code, output { font-family: ui-monospace, "SFMono-Regular", Menlo, Monaco, Consolas, monospace; }
    :focus-visible { outline: 3px solid var(--brand); outline-offset: 3px; }
    [hidden] { display: none !important; }
    .sr-only {
      position: absolute;
      width: 1px;
      height: 1px;
      padding: 0;
      margin: -1px;
      overflow: hidden;
      clip: rect(0, 0, 0, 0);
      white-space: nowrap;
      border: 0;
    }

    .shell { width: min(1080px, calc(100% - 40px)); margin: 0 auto; }
    .skip-link {
      position: fixed;
      top: 10px;
      left: 10px;
      z-index: 20;
      transform: translateY(-160%);
      padding: 9px 13px;
      color: #fff;
      background: var(--brand-strong);
      border-radius: 7px;
    }
    .skip-link:focus { transform: translateY(0); }

    .site-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      min-height: 64px;
      border-bottom: 1px solid var(--line);
    }
    .brand {
      display: inline-flex;
      align-items: center;
      gap: 10px;
      font-family: ui-serif, Georgia, Cambria, "Times New Roman", serif;
      font-size: 1.15rem;
      font-weight: 600;
      letter-spacing: -.015em;
    }
    .brand-mark {
      width: 15px;
      height: 15px;
      border: 2px solid var(--brand);
      border-radius: 50%;
    }
    .read-only {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      color: var(--muted);
      font-size: .78rem;
    }
    .read-only::before {
      content: "";
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: var(--green);
    }

    main { padding: 74px 0 96px; }
    .page-intro {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 32px;
      align-items: end;
      margin-bottom: 32px;
    }
    .eyebrow {
      color: var(--brand-strong);
      font-size: .76rem;
      font-weight: 700;
      letter-spacing: .08em;
    }
    h1, h2 {
      font-family: ui-serif, Georgia, Cambria, "Times New Roman", serif;
      font-weight: 500;
      letter-spacing: -.025em;
    }
    h1 {
      margin: 10px 0 12px;
      font-size: clamp(2.65rem, 6vw, 4.8rem);
      line-height: 1;
    }
    .intro-copy {
      max-width: 42rem;
      margin: 0;
      color: var(--muted);
      font-size: 1rem;
      line-height: 1.7;
    }
    .sync-state {
      min-width: 190px;
      color: var(--muted);
      text-align: right;
      font-size: .78rem;
      line-height: 1.6;
    }
    .sync-state strong { display: block; color: var(--ink); font-size: .9rem; }

    .overview-grid { display: block; }
    .panel {
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: var(--surface);
    }
    .current-panel { padding: clamp(24px, 4vw, 40px); }
    .current-topline {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 20px;
      margin-bottom: 28px;
    }
    .current-topline span { color: var(--muted); font-size: .82rem; }
    .progress-count { color: var(--ink) !important; font-weight: 700; }
    progress {
      display: block;
      width: 100%;
      height: 4px;
      margin-top: 10px;
      border: 0;
      border-radius: 999px;
      overflow: hidden;
      background: var(--surface-soft);
    }
    progress::-webkit-progress-bar { background: var(--surface-soft); }
    progress::-webkit-progress-value { background: var(--brand); }
    progress::-moz-progress-bar { background: var(--brand); }
    .current-index {
      display: block;
      margin-bottom: 8px;
      color: var(--brand-strong);
      font-size: .78rem;
      font-weight: 700;
    }
    .current-panel h2 {
      margin: 0;
      font-size: clamp(2rem, 4vw, 3.1rem);
      line-height: 1.1;
    }
    .current-description {
      max-width: 38rem;
      margin: 14px 0 0;
      color: var(--muted);
      line-height: 1.65;
    }
    .status-banner {
      margin: 26px 0 0;
      padding: 12px 14px;
      color: var(--muted);
      border-left: 3px solid var(--yellow);
      background: var(--yellow-soft);
      font-size: .9rem;
      line-height: 1.5;
    }
    .status-banner[data-state="complete"] { color: var(--green); border-left-color: var(--green); background: var(--green-soft); }
    .status-banner[data-state="running"] { color: var(--ink); border-left-color: var(--brand); background: var(--brand-soft); }
    .status-banner[data-state="error"] { color: var(--red); border-left-color: var(--red); background: var(--red-soft); }
    .next-action { margin-top: 26px; }
    .next-action span {
      display: block;
      margin-bottom: 8px;
      color: var(--muted);
      font-size: .75rem;
      font-weight: 700;
    }
    .command-line {
      display: block;
      width: 100%;
      padding: 13px 15px;
      color: var(--ink);
      border: 1px solid var(--line);
      border-radius: 8px;
      background: var(--surface-soft);
      font-size: .84rem;
      line-height: 1.55;
      overflow-wrap: anywhere;
    }

    .step-artifacts a {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      min-height: 36px;
      color: var(--brand-strong);
      text-decoration: none;
      font-size: .82rem;
      overflow-wrap: anywhere;
    }
    .step-artifacts a::after { content: "↗"; }
    .step-artifacts a:hover { text-decoration: underline; text-underline-offset: 3px; }

    .steps-section { margin-top: 64px; }
    .section-heading {
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 24px;
      margin-bottom: 18px;
    }
    .section-heading h2 { margin: 8px 0 0; font-size: clamp(1.9rem, 4vw, 2.7rem); }
    .section-heading p { max-width: 32rem; margin: 0; color: var(--muted); font-size: .86rem; line-height: 1.55; }
    .step-list {
      margin: 0;
      padding: 0;
      border-top: 1px solid var(--line);
      list-style: none;
    }
    .step-item {
      display: grid;
      grid-template-columns: 46px minmax(0, 1fr) auto;
      gap: 18px;
      align-items: start;
      padding: 20px 16px;
      border-bottom: 1px solid var(--line);
    }
    .step-item[data-state="running"] { background: var(--brand-soft); }
    .step-item[data-state="error"] { background: var(--red-soft); }
    .step-item[data-current="true"] { box-shadow: inset 3px 0 0 var(--brand); }
    .step-marker {
      display: grid;
      place-items: center;
      width: 34px;
      height: 34px;
      color: var(--muted);
      border: 1px solid var(--line);
      border-radius: 50%;
      background: var(--surface);
      font-size: .7rem;
      font-weight: 700;
    }
    .step-item[data-state="complete"] .step-marker { color: #fff; border-color: var(--green); background: var(--green); }
    .step-item[data-state="running"] .step-marker { color: #fff; border-color: var(--brand); background: var(--brand); }
    .step-item[data-state="error"] .step-marker { color: #fff; border-color: var(--red); background: var(--red); }
    .step-heading { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }
    .step-heading h3 { margin: 0; font-size: 1rem; }
    .step-copy > p { margin: 6px 0 0; color: var(--muted); font-size: .86rem; line-height: 1.55; }
    .step-state {
      padding-top: 7px;
      color: var(--muted);
      font-size: .76rem;
      font-weight: 700;
      white-space: nowrap;
    }
    .step-item[data-state="complete"] .step-state { color: var(--green); }
    .step-item[data-state="running"] .step-state { color: var(--brand-strong); }
    .step-item[data-state="error"] .step-state { color: var(--red); }
    .step-item[data-state="waiting"] .step-state { color: var(--yellow); }
    .step-artifacts {
      display: flex;
      flex-wrap: wrap;
      gap: 8px 18px;
      margin-top: 10px;
    }
    .raw-artifacts { flex-basis: 100%; color: var(--muted); font-size: .76rem; }
    .raw-artifacts summary { cursor: pointer; }
    .raw-artifact-links { display: flex; flex-wrap: wrap; gap: 8px 18px; padding-top: 8px; }

    .run-details {
      margin-top: 48px;
      border: 1px solid var(--line);
      border-radius: var(--radius);
      background: var(--surface);
    }
    .run-details > summary {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 20px;
      padding: 18px 20px;
      cursor: pointer;
      font-weight: 700;
      list-style: none;
    }
    .run-details > summary::-webkit-details-marker { display: none; }
    .run-details > summary::after { content: "+"; color: var(--brand-strong); font-size: 1.25rem; font-weight: 400; }
    .run-details[open] > summary::after { content: "−"; }
    .details-body { padding: 0 20px 22px; border-top: 1px solid var(--line); }
    .detail-grid {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 1px;
      margin-top: 20px;
      border: 1px solid var(--line);
      background: var(--line);
    }
    .detail-grid div { min-width: 0; padding: 14px; background: var(--surface); }
    .detail-grid dt { color: var(--muted); font-size: .72rem; }
    .detail-grid dd { margin: 6px 0 0; font-size: .84rem; font-weight: 700; overflow-wrap: anywhere; }
    .usage-line {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
      margin-top: 18px;
    }
    .usage-card { padding: 14px 0; border-top: 1px solid var(--line); }
    .usage-card span { display: block; color: var(--muted); font-size: .72rem; }
    .usage-card strong, .usage-card output { display: block; margin-top: 6px; font-size: 1rem; }
    .cost-note { color: var(--brand-strong); font-size: .76rem !important; font-weight: 400 !important; }
    .bill-note { margin: 18px 0 0; color: var(--muted); font-size: .8rem; line-height: 1.6; }

    footer { padding: 24px 0 36px; color: var(--muted); border-top: 1px solid var(--line); font-size: .75rem; line-height: 1.6; }

    @media (max-width: 800px) {
      main { padding-top: 52px; }
      .page-intro { grid-template-columns: 1fr; }
      .sync-state { min-width: 0; text-align: left; }
      .section-heading { display: block; }
      .section-heading p { margin-top: 10px; }
      .detail-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
    @media (max-width: 520px) {
      .shell { width: min(100% - 24px, 1080px); }
      .site-header { min-height: 58px; }
      .read-only span { display: none; }
      main { padding: 42px 0 72px; }
      h1 { font-size: 2.8rem; }
      .current-panel { padding: 22px; }
      .current-topline { display: block; }
      .current-topline .progress-count { display: block; margin-top: 7px; }
      .step-item { grid-template-columns: 38px minmax(0, 1fr); gap: 12px; padding: 18px 4px; }
      .step-marker { width: 30px; height: 30px; }
      .step-state { grid-column: 2; padding-top: 0; }
      .detail-grid, .usage-line { grid-template-columns: 1fr; }
    }
    @media (prefers-reduced-motion: reduce) {
      html { scroll-behavior: auto; }
      *, *::before, *::after { transition-duration: .01ms !important; }
    }
  </style>
</head>
<body>
  <a class="skip-link" href="#dashboard">跳到运行进度</a>
  <header class="site-header shell">
    <div class="brand"><span class="brand-mark" aria-hidden="true"></span><span>Learn Self-Evolving Skills</span></div>
    <div class="read-only" title="本地看板只读取状态，不执行命令"><span>本地只读</span></div>
  </header>

  <main class="shell" id="dashboard">
    <div class="page-intro">
      <div>
        <span class="eyebrow" id="run-mode-label">运行状态</span>
        <h1>运行进度</h1>
        <p class="intro-copy">查看当前步骤、下一项操作和已经生成的运行记录。页面不会执行命令，也不会扫描工作目录。</p>
      </div>
      <div class="sync-state"><strong id="sync-label">等待本地状态</strong><span id="sync-time">尚未同步</span></div>
    </div>

    <div class="overview-grid">
      <section class="panel current-panel" aria-labelledby="current-step-title">
        <div class="current-topline">
          <div>
            <label class="sr-only" for="progress-bar">8 个步骤的整体进度</label>
            <span aria-hidden="true">整体进度</span>
            <progress id="progress-bar" max="8" value="0">0 / 8</progress>
          </div>
          <span class="progress-count" id="progress-label">0 / 8 个步骤完成</span>
        </div>
        <span class="current-index" id="current-step-index">尚未开始</span>
        <h2 id="current-step-title">等待第一条运行记录</h2>
        <p class="current-description" id="current-step-description">回到终端，让讲师检查环境并开始第一个步骤。</p>
        <div class="status-banner" id="status-banner" data-state="waiting" role="status" aria-live="polite">等待第一条运行记录。</div>
        <div class="next-action">
          <span id="current-command-label">下一步</span>
          <code class="command-line" id="current-command">在 coding agent 中输入：我要学习 Skill 自进化</code>
        </div>
      </section>
    </div>

    <section class="steps-section" aria-labelledby="steps-title">
      <div class="section-heading">
        <div><span class="eyebrow">8 个步骤</span><h2 id="steps-title">Skill 改进步骤</h2></div>
        <p>每个步骤只要求一种判断。当前步骤会突出显示，相关运行记录会附在该步骤下。</p>
      </div>
      <ol class="step-list" aria-label="Skill 改进的 8 个步骤">
        __STEP_ITEMS__
      </ol>
    </section>

    <details class="run-details">
      <summary>运行详情</summary>
      <div class="details-body">
        <dl class="detail-grid">
          <div><dt>模式</dt><dd id="experiment-mode-label">等待状态</dd></div>
          <div><dt>Provider</dt><dd id="experiment-provider-label">未记录</dd></div>
          <div><dt>成本来源</dt><dd id="cost-source-label">未记录</dd></div>
          <div><dt>模型锁</dt><dd id="model-lock-label">未记录</dd></div>
          <div><dt>输入 token</dt><dd id="input-token-label">—</dd></div>
          <div><dt>输出 token</dt><dd id="output-token-label">—</dd></div>
        </dl>
        <div class="usage-line">
          <div class="usage-card"><span>实验引擎成本</span><output id="cost-amount">—</output><strong class="cost-note" id="cost-kind">等待本地状态</strong></div>
          <div class="usage-card"><span>两笔费用</span><strong>讲师与实验引擎分开结算</strong><span class="cost-note" id="experiment-bill">本看板不读取任何 Key。</span></div>
        </div>
        <p class="bill-note">讲师费用来自你自己的 coding-agent 订阅或 Key；实验引擎费用来自所选 Provider。估算、缺失费用和 fixed CI 合成值都不能当作 Provider 最终账单。</p>
      </div>
    </details>
  </main>

  <footer><div class="shell">本地只读看板 · 只轮询 <code>/.ses/status.json</code> · 不保存 Key · 不发起外网请求</div></footer>

  <script>
    "use strict";
    const stationDefinitions = __STATIONS_JSON__;
    const stateLabels = { complete: "已完成", running: "进行中", error: "需处理", waiting: "等待中", pending: "未开始" };
    const statusBanner = document.getElementById("status-banner");
    const syncLabel = document.getElementById("sync-label");
    const syncTime = document.getElementById("sync-time");
    const runModeLabel = document.getElementById("run-mode-label");
    const progressBar = document.getElementById("progress-bar");
    const progressLabel = document.getElementById("progress-label");
    const currentStepIndex = document.getElementById("current-step-index");
    const currentStepTitle = document.getElementById("current-step-title");
    const currentStepDescription = document.getElementById("current-step-description");
    const currentCommandLabel = document.getElementById("current-command-label");
    const currentCommand = document.getElementById("current-command");
    const experimentModeLabel = document.getElementById("experiment-mode-label");
    const experimentProviderLabel = document.getElementById("experiment-provider-label");
    const costSourceLabel = document.getElementById("cost-source-label");
    const modelLockLabel = document.getElementById("model-lock-label");
    const inputTokenLabel = document.getElementById("input-token-label");
    const outputTokenLabel = document.getElementById("output-token-label");
    const experimentBill = document.getElementById("experiment-bill");
    const costAmount = document.getElementById("cost-amount");
    const costKind = document.getElementById("cost-kind");
    let lastPayloadSignature = null;

    function setText(node, value) {
      if (node && node.textContent !== value) node.textContent = value;
    }

    function firstValue(source, keys) {
      if (!source || typeof source !== "object") return undefined;
      for (const key of keys) if (source[key] !== undefined && source[key] !== null) return source[key];
      return undefined;
    }

    function providerDisplayName(value) {
      const provider = String(value || "").trim().toLowerCase();
      if (provider === "siliconflow") return "SiliconFlow";
      if (provider === "chatanywhere") return "ChatAnywhere";
      return provider ? "未识别 Provider" : "未记录";
    }

    function costSourceDisplayName(value) {
      const source = String(value || "").trim().toLowerCase();
      if (source === "synthetic_ci") return "固定 CI 合成值";
      if (source === "claude_code_estimate") return "Claude Code 估算";
      if (source === "unavailable") return "不可用";
      return "未记录";
    }

    function renderEngineContext(data) {
      const mode = String(firstValue(data, ["experiment_mode", "mode"]) || "").trim().toLowerCase();
      const provider = providerDisplayName(firstValue(data, ["experiment_provider"]));
      const costSource = firstValue(data, ["cost_source"]);
      const lockHash = firstValue(data, ["model_lock_sha256"]);
      const fixed = mode === "fixed";

      setText(runModeLabel, fixed ? "固定 CI" : mode === "live" ? "实时评测" : "运行状态");
      setText(experimentModeLabel, fixed ? "固定 CI" : mode === "live" ? "实时" : "等待状态");
      setText(experimentProviderLabel, fixed ? "不调用 Provider" : provider);
      setText(costSourceLabel, costSourceDisplayName(costSource));
      if (typeof lockHash === "string" && /^[a-f0-9]{64}$/i.test(lockHash)) {
        setText(modelLockLabel, "sha256:" + lockHash.slice(0, 12) + "…");
        modelLockLabel.title = lockHash;
      } else {
        setText(modelLockLabel, "未记录");
        modelLockLabel.removeAttribute("title");
      }
      if (fixed) {
        setText(experimentBill, "固定 CI 不调用外部 Provider；合成值不代表 live 成本。");
      } else if (provider === "SiliconFlow" || provider === "ChatAnywhere") {
        setText(experimentBill, provider + " 结算；这里只读取本地状态，不读取 Key。");
      } else {
        setText(experimentBill, "等待状态写入所选 Provider；本看板不读取 Key。");
      }
    }

    function normalizeState(value) {
      const state = String(value || "").toLowerCase().replaceAll("-", "_");
      if (["complete", "completed", "done", "pass", "passed", "success", "accepted", "released"].includes(state)) return "complete";
      if (["running", "in_progress", "active", "working", "started"].includes(state)) return "running";
      if (["error", "failed", "fail", "blocked", "needs_attention", "infrastructure_error", "judge_error"].includes(state)) return "error";
      if (["waiting", "queued", "paused", "budget_stop"].includes(state)) return "waiting";
      return "pending";
    }

    function stationRecord(data, id) {
      const source = firstValue(data, ["stations", "progress"]);
      if (Array.isArray(source)) {
        return source.find((item, index) => String(firstValue(item, ["id", "number", "station_id", "station", "index"]) ?? index) === String(id)) || {};
      }
      if (source && typeof source === "object") return source[id] || source["station-" + id] || source["station_" + id] || {};
      return {};
    }

    function idSet(value) {
      const values = Array.isArray(value) ? value : [];
      return new Set(values.map(item => String((item && typeof item === "object") ? firstValue(item, ["id", "number", "station_id", "station"]) : item)));
    }

    function stationState(data, record, id) {
      const explicit = firstValue(record, ["status", "state", "result"]);
      if (explicit !== undefined) return normalizeState(explicit);
      const completed = idSet(firstValue(data, ["completed_stations", "completed"]));
      if (completed.has(String(id))) return "complete";
      const current = firstValue(data, ["current_station", "active_station", "station"]);
      if (String(current) === String(id)) {
        return normalizeState(firstValue(data, ["overall_status", "status", "state"])) === "error" ? "error" : "running";
      }
      return "pending";
    }

    function pathFrom(value) {
      if (typeof value === "string") return value;
      return firstValue(value, ["path", "href", "report_path", "artifact_path", "output_path"]);
    }

    function labelFrom(value, fallback) {
      if (!value || typeof value !== "object") return fallback;
      const explicit = firstValue(value, ["label", "title", "name", "kind", "type"]);
      if (explicit) return String(explicit);
      return artifactFileName(pathFrom(value), fallback);
    }

    function artifactFileName(value, fallback) {
      if (typeof value !== "string") return fallback;
      const clean = value.split(/[?#]/, 1)[0].replaceAll("\\", "/");
      const name = clean.split("/").filter(Boolean).at(-1);
      if (!name) return fallback;
      try { return decodeURIComponent(name); } catch (_error) { return name; }
    }

    function isPerCaseEvidence(item) {
      const path = typeof item?.path === "string" ? item.path.replaceAll("\\", "/") : "";
      const name = artifactFileName(path, "");
      return path.includes("/artifacts/") || path.includes("/workspaces/") || /^(?:trace-turn-\d+|before|after|state-diff|grade|case-fixture)\.json$/i.test(name);
    }

    function flattenArtifacts(value, fallback, output) {
      if (typeof value === "string") {
        output.push({ label: artifactFileName(value, fallback), path: value });
        return;
      }
      if (Array.isArray(value)) {
        value.forEach((item, index) => flattenArtifacts(item, fallback + " " + (index + 1), output));
        return;
      }
      if (!value || typeof value !== "object") return;
      const directPath = pathFrom(value);
      if (typeof directPath === "string") {
        output.push({ label: labelFrom(value, fallback), path: directPath });
        return;
      }
      Object.entries(value).forEach(([key, item]) => flattenArtifacts(item, key, output));
    }

    function artifactsFrom(source, fallback) {
      const output = [];
      if (!source || typeof source !== "object") return output;
      ["reports", "artifacts", "outputs", "report", "artifact", "output", "artifact_refs", "decision_refs"].forEach(key => {
        if (source[key] !== undefined) flattenArtifacts(source[key], fallback, output);
      });
      return output;
    }

    function localArtifactHref(raw) {
      if (typeof raw !== "string" || raw.includes("\\") || raw.includes("\0") || raw.includes("?") || raw.includes("#")) return null;
      let value = raw.trim();
      if (/^[a-z][a-z0-9+.-]*:/i.test(value) || value.startsWith("//")) return null;
      if (value.startsWith("/artifact/")) value = value.slice(10);
      value = value.replace(/^\.\//, "").replace(/^\//, "");
      const parts = value.split("/");
      if (!parts.length || parts.some(part => !part || part === "." || part === "..")) return null;
      return "/artifact/" + parts.map(encodeURIComponent).join("/");
    }

    function artifactLink(item) {
      const href = localArtifactHref(item.path);
      if (!href) return null;
      const link = document.createElement("a");
      link.href = href;
      link.textContent = item.label || "打开文件";
      link.title = item.path;
      return link;
    }

    function setStation(id, state, record, isCurrent) {
      const step = document.querySelector('[data-step-station="' + id + '"]');
      if (!step) return [];
      const label = stateLabels[state] || stateLabels.pending;
      step.dataset.state = state;
      if (isCurrent) {
        step.dataset.current = "true";
        step.setAttribute("aria-current", "step");
      } else {
        delete step.dataset.current;
        step.removeAttribute("aria-current");
      }
      setText(step.querySelector("[data-step-state]"), label);

      const artifacts = artifactsFrom(record, "步骤 " + (Number(id) + 1) + " 输出");
      const container = step.querySelector("[data-station-artifacts]");
      const signature = JSON.stringify({ state, artifacts });
      if (container.dataset.renderSignature === signature) return artifacts;
      container.dataset.renderSignature = signature;
      container.replaceChildren();
      const rawArtifacts = [];
      let count = 0;
      artifacts.forEach(item => {
        if (isPerCaseEvidence(item)) { rawArtifacts.push(item); return; }
        const link = artifactLink(item);
        if (link) { container.append(link); count += 1; }
      });
      if (rawArtifacts.length) {
        const details = document.createElement("details");
        details.className = "raw-artifacts";
        const summary = document.createElement("summary");
        summary.textContent = "原始逐用例记录（" + rawArtifacts.length + "）";
        const links = document.createElement("div");
        links.className = "raw-artifact-links";
        rawArtifacts.forEach(item => {
          const link = artifactLink(item);
          if (link) links.append(link);
        });
        details.append(summary, links);
        container.append(details);
        count += links.childElementCount ? 1 : 0;
      }
      container.hidden = count === 0;
      return artifacts;
    }

    function renderCurrentStep(data) {
      const overall = normalizeState(firstValue(data, ["overall_status", "status", "state"]));
      const rawCurrent = firstValue(data, ["current_station", "active_station", "station"]);
      const current = Number(rawCurrent);
      const valid = Number.isInteger(current) && current >= 0 && current < stationDefinitions.length;
      if (!valid) {
        if (overall === "error") {
          setText(currentStepIndex, "状态不可用");
          setText(currentStepTitle, "无法确定当前步骤");
          setText(currentStepDescription, "本地状态无法安全读取。请回到终端查看错误，再修复或恢复 status.json。");
          setText(currentCommandLabel, "下一步");
          setText(currentCommand, "回到终端检查 .ses/status.json 和最近一条命令。");
        } else {
          setText(currentStepIndex, "尚未开始");
          setText(currentStepTitle, "等待第一条运行记录");
          setText(currentStepDescription, "回到终端，让讲师检查环境并开始第一个步骤。");
          setText(currentCommandLabel, "下一步");
          setText(currentCommand, "在 coding agent 中输入：我要学习 Skill 自进化");
        }
        return;
      }
      const definition = stationDefinitions[current];
      const record = stationRecord(data, definition.id);
      const state = stationState(data, record, definition.id);
      if (overall === "complete") {
        setText(currentStepIndex, "8 / 8 个步骤完成");
        setText(currentStepTitle, "运行已经完成");
        setText(currentStepDescription, "无需继续执行命令。请核对核心运行记录和可选说明文件。");
        setText(currentCommandLabel, "状态");
        setText(currentCommand, "无需继续运行。请核对 evidence-facts.json 和 evidence-index.json。");
        return;
      }
      setText(currentStepIndex, "步骤 " + (current + 1) + " / " + stationDefinitions.length);
      setText(currentStepTitle, definition.name);
      setText(currentStepDescription, definition.decision);
      if (state === "running") {
        setText(currentCommandLabel, "运行中");
        setText(currentCommand, "当前步骤正在运行。完整命令请以终端记录为准。");
      } else if (state === "error") {
        setText(currentCommandLabel, "下一步");
        setText(currentCommand, "回到终端查看失败原因，再让讲师继续当前步骤。");
      } else {
        setText(currentCommandLabel, "下一步");
        setText(currentCommand, "回到 coding agent，让讲师准备当前步骤需要的参数。");
      }
    }

    function tokenDisplay(value) {
      const number = Number(value);
      return Number.isSafeInteger(number) && number >= 0 ? number.toLocaleString("zh-CN") : "—";
    }

    function renderCost(data) {
      const value = firstValue(data, ["experiment_usage", "experiment_cost", "experimental_cost", "total_cost", "cost"]);
      let amount = value;
      let currency = firstValue(data, ["cost_currency", "currency"]);
      let complete = firstValue(data, ["cost_complete"]) === true;
      const source = String(firstValue(data, ["cost_source"]) || "").trim().toLowerCase();
      let inputTokens = firstValue(data, ["input_tokens", "total_input_tokens"]);
      let outputTokens = firstValue(data, ["output_tokens", "total_output_tokens"]);
      if (value && typeof value === "object") {
        amount = firstValue(value, ["amount", "value", "total", "cost_amount"]);
        const minor = firstValue(value, ["amount_minor"]);
        if (amount === undefined && Number.isFinite(Number(minor)) && Number(minor) >= 0) amount = Number(minor) / 100;
        currency = firstValue(value, ["currency", "cost_currency"]) || currency;
        complete = firstValue(value, ["cost_complete"]) === true;
        inputTokens = firstValue(value, ["input_tokens", "total_input_tokens"]) ?? inputTokens;
        outputTokens = firstValue(value, ["output_tokens", "total_output_tokens"]) ?? outputTokens;
      }
      setText(inputTokenLabel, tokenDisplay(inputTokens));
      setText(outputTokenLabel, tokenDisplay(outputTokens));

      const hasAmount = amount !== undefined && amount !== null && amount !== "" && Number.isFinite(Number(amount)) && Number(amount) >= 0;
      const code = typeof currency === "string" ? currency.trim().toUpperCase() : "";
      const formatted = hasAmount && code
        ? (code === "CNY" ? "¥" : code === "USD" ? "$" : code + " ") + Number(amount).toFixed(4).replace(/0+$/, "").replace(/\.$/, "")
        : null;

      if (source === "unavailable") {
        setText(costAmount, "—");
        setText(costKind, "费用不可用 · 本地状态没有可靠成本");
        return;
      }
      if (!complete) {
        setText(costAmount, formatted || "—");
        setText(costKind, "部分数据 · 不能作为完整账单");
        return;
      }
      if (!formatted) {
        setText(costAmount, "—");
        setText(costKind, "费用不可用 · 本地状态缺少金额或币种");
        return;
      }
      if (source === "synthetic_ci") {
        setText(costAmount, formatted);
        setText(costKind, "固定 CI 合成值 · 不代表 live 成本");
        return;
      }
      if (source === "claude_code_estimate") {
        setText(costAmount, formatted);
        setText(costKind, "Claude Code 估算 · 以 Provider 最终账单为准");
        return;
      }
      setText(costAmount, "—");
      setText(costKind, "费用不可用 · 成本来源未记录");
    }

    function renderStatus(data) {
      const rawCurrent = firstValue(data, ["current_station", "active_station", "station"]);
      const currentNumber = Number(rawCurrent);
      const hasCurrent = Number.isInteger(currentNumber) && currentNumber >= 0 && currentNumber < stationDefinitions.length;
      const overall = normalizeState(firstValue(data, ["overall_status", "status", "state"]));
      let completeCount = 0;
      let currentState = "pending";
      let currentRecord = {};
      stationDefinitions.forEach(definition => {
        const record = stationRecord(data, definition.id);
        const state = stationState(data, record, definition.id);
        const isCurrent = hasCurrent && overall !== "complete" && String(currentNumber) === definition.id;
        if (state === "complete") completeCount += 1;
        if (isCurrent) {
          currentState = state;
          currentRecord = record;
        }
        setStation(definition.id, state, record, isCurrent);
      });
      renderCurrentStep(data);
      renderEngineContext(data);
      renderCost(data);

      let state = overall !== "pending" ? overall : currentState;
      if (overall === "running" && currentState === "pending") state = "waiting";
      const message = firstValue(data, ["message", "status_message", "error_message"]);
      const attentionReason = firstValue(currentRecord, ["attention_reason", "message", "status_message", "error_message"]);
      statusBanner.dataset.state = state;
      if (typeof message === "string" && message.trim()) setText(statusBanner, message);
      else if (state === "error" && typeof attentionReason === "string" && attentionReason.trim()) setText(statusBanner, attentionReason);
      else if (completeCount === stationDefinitions.length) setText(statusBanner, "8 个步骤已完成。请核对输出文件和对应的运行记录。");
      else if (state === "error") setText(statusBanner, "当前步骤需要处理。请回到终端查看原因和下一步。");
      else if (state === "running") setText(statusBanner, "模型正在运行。完成后，本看板会自动更新。");
      else if (hasCurrent) setText(statusBanner, "当前步骤尚未启动。请回到 coding agent 继续。");
      else setText(statusBanner, "等待终端写入下一条运行记录。本看板会自动更新。");

      progressBar.value = completeCount;
      progressBar.textContent = completeCount + " / " + stationDefinitions.length;
      const progressText = completeCount + " / " + stationDefinitions.length + " 个步骤完成";
      progressBar.setAttribute("aria-valuetext", progressText);
      setText(progressLabel, progressText);
      setText(syncLabel, stateLabels[state] || "等待状态");
      const sourceTime = firstValue(data, ["updated_at", "last_updated_at", "timestamp"]);
      setText(syncTime, sourceTime ? "状态时间 " + String(sourceTime) : "刚刚同步 · " + new Date().toLocaleTimeString("zh-CN", { hour12: false }));
    }

    async function pollStatus() {
      try {
        const response = await fetch("/.ses/status.json", { cache: "no-store", credentials: "same-origin", headers: { Accept: "application/json" } });
        if (!response.ok) throw new Error("status unavailable");
        const payload = await response.json();
        if (!payload || typeof payload !== "object" || Array.isArray(payload)) throw new Error("status invalid");
        const signature = JSON.stringify(payload);
        if (signature !== lastPayloadSignature) {
          renderStatus(payload);
          lastPayloadSignature = signature;
        }
      } catch (_error) {
        lastPayloadSignature = null;
        statusBanner.dataset.state = "error";
        setText(statusBanner, "暂时读不到本地状态。请确认本地看板仍在运行，再回到终端查看最近一条命令。");
        setText(syncLabel, "等待重新连接");
        setText(syncTime, "上次内容可能已过期 · 本看板会自动重试");
      } finally {
        window.setTimeout(pollStatus, 2200);
      }
    }

    pollStatus();
  </script>
</body>
</html>
"""


def render_dashboard_html() -> str:
    """Return one offline-capable page with only same-origin polling."""

    stations_json = json.dumps(STATIONS, ensure_ascii=False, separators=(",", ":"))
    return _DOCUMENT.replace("__STEP_ITEMS__", _step_markup()).replace(
        "__STATIONS_JSON__", stations_json.replace("</", "<\\/")
    )
