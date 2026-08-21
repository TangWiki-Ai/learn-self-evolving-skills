# ruff: noqa: RUF001
"""Render the learner-facing, self-contained journey dashboard."""

from __future__ import annotations

import html
import json

STATIONS: tuple[dict[str, str], ...] = (
    {
        "id": "0",
        "name": "准备与基线",
        "phrase": "Execution & Monitoring",
        "decision": "观察基线，不做结论",
        "command": "uv run ses journey station 0",
    },
    {
        "id": "1",
        "name": "坏案例挖掘",
        "phrase": "Bad Case Mining",
        "decision": "选择进入分析的失败 case",
        "command": "uv run ses journey station 1",
    },
    {
        "id": "2",
        "name": "失败归因",
        "phrase": "Failure Analysis",
        "decision": "为失败证据拍板归因标签",
        "command": "uv run ses journey station 2",
    },
    {
        "id": "3",
        "name": "Skill 诊断",
        "phrase": "Skill Diagnosis",
        "decision": "定位该改的知识块或步骤",
        "command": "uv run ses journey station 3",
    },
    {
        "id": "4",
        "name": "最小修复",
        "phrase": "Minimal Refinement",
        "decision": "确定最小、可追溯的修复",
        "command": "uv run ses journey station 4",
    },
    {
        "id": "5",
        "name": "回归评估",
        "phrase": "Regression Evaluation",
        "decision": "根据 Gate 证据做回归取舍",
        "command": "uv run ses journey station 5",
    },
    {
        "id": "6",
        "name": "发版与回滚",
        "phrase": "Version Release & Rollback",
        "decision": "决定发版，并体验一次回滚",
        "command": "uv run ses journey station 6",
    },
    {
        "id": "7",
        "name": "总结",
        "phrase": "Evidence-backed Portfolio",
        "decision": "生成并核对你的三件套",
        "command": "uv run ses journey station 7",
    },
)


def _flow_markup() -> str:
    items: list[str] = []
    for station in STATIONS:
        station_id = station["id"]
        items.append(
            f"""
<li class="flow-item" data-flow-station="{station_id}" data-state="pending">
  <a href="#station-{station_id}" aria-label="站 {station_id}，{html.escape(station["name"])}，未开始">
    <span class="station-index" aria-hidden="true">{int(station_id):02d}</span>
    <span class="flow-copy">
      <strong>{html.escape(station["name"])}</strong>
      <small>{html.escape(station["phrase"])}</small>
    </span>
    <span class="station-state" data-flow-state>未开始</span>
  </a>
</li>""".strip()
        )
    return "\n".join(items)


def _station_markup() -> str:
    items: list[str] = []
    for station in STATIONS:
        station_id = station["id"]
        items.append(
            f"""
<article class="station-card" id="station-{station_id}" data-station="{station_id}" data-state="pending">
  <div class="station-card-head">
    <div><span class="eyebrow">STATION {int(station_id):02d}</span><h3>{html.escape(station["name"])}</h3></div>
    <span class="detail-state" data-detail-state>未开始</span>
  </div>
  <p class="resume-phrase">{html.escape(station["phrase"])}</p>
  <dl><div><dt>你的判断</dt><dd>{html.escape(station["decision"])}</dd></div></dl>
  <div class="command-block">
    <span>手动命令</span>
    <code data-command>{html.escape(station["command"])}</code>
  </div>
  <div class="station-artifacts" data-station-artifacts>
    <span class="empty-artifact">完成本站后，产物链接会出现在这里。</span>
  </div>
</article>""".strip()
        )
    return "\n".join(items)


_DOCUMENT = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="dark light">
  <meta name="referrer" content="no-referrer">
  <title>Self-evolving Skill · 实战进度</title>
  <style>
    :root {
      color-scheme: dark;
      --ink: #f5f1e8;
      --muted: #a9b2bd;
      --canvas: #0c1117;
      --panel: #121922;
      --panel-soft: #18212b;
      --line: #2b3743;
      --paper: #f2eadc;
      --paper-ink: #18202a;
      --orange: #ff9b62;
      --cyan: #68d8d6;
      --green: #74d39b;
      --red: #ff8178;
      --yellow: #f2c96d;
      --radius: 18px;
      font-family: ui-monospace, "SFMono-Regular", Menlo, Monaco, Consolas, monospace;
      background: var(--canvas);
      color: var(--ink);
      font-synthesis: none;
    }

    * { box-sizing: border-box; }
    html { scroll-behavior: smooth; }
    body { margin: 0; min-width: 320px; background: var(--canvas); }
    a { color: inherit; }
    button, input, textarea, select { font: inherit; }
    :focus-visible { outline: 3px solid var(--cyan); outline-offset: 4px; }

    .skip-link {
      position: fixed;
      top: 10px;
      left: 10px;
      z-index: 50;
      transform: translateY(-160%);
      padding: 10px 14px;
      color: #081014;
      background: var(--cyan);
      border-radius: 8px;
    }
    .skip-link:focus { transform: translateY(0); }

    .shell { width: min(1240px, calc(100% - 36px)); margin: 0 auto; }
    .site-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      min-height: 70px;
      border-bottom: 1px solid var(--line);
    }
    .brand { display: flex; align-items: center; gap: 11px; font-weight: 800; letter-spacing: -.03em; }
    .brand-mark {
      width: 18px;
      height: 18px;
      border: 2px solid var(--orange);
      border-radius: 50%;
      box-shadow: inset 0 0 0 4px var(--canvas);
      background: var(--orange);
    }
    .read-only {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: var(--muted);
      font-size: .76rem;
      letter-spacing: .08em;
      text-transform: uppercase;
    }
    .read-only::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: var(--green); }

    .hero {
      display: grid;
      grid-template-columns: minmax(0, .82fr) minmax(420px, 1.18fr);
      gap: clamp(36px, 6vw, 88px);
      align-items: center;
      min-height: calc(100vh - 70px);
      padding: 64px 0 74px;
    }
    .kicker, .eyebrow {
      color: var(--orange);
      font-size: .72rem;
      font-weight: 800;
      letter-spacing: .14em;
      text-transform: uppercase;
    }
    h1 {
      margin: 18px 0 22px;
      max-width: 12ch;
      font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: clamp(3rem, 6.4vw, 6.2rem);
      line-height: .96;
      letter-spacing: -.07em;
    }
    .hero-lede {
      max-width: 42rem;
      margin: 0;
      color: var(--muted);
      font-family: ui-sans-serif, system-ui, sans-serif;
      font-size: clamp(1rem, 1.7vw, 1.25rem);
      line-height: 1.7;
    }
    .hero-proof {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 1px;
      margin: 36px 0 0;
      border: 1px solid var(--line);
      background: var(--line);
    }
    .hero-proof div { min-width: 0; padding: 18px 14px; background: var(--canvas); }
    .hero-proof dt { color: var(--muted); font-size: .68rem; text-transform: uppercase; }
    .hero-proof dd { margin: 8px 0 0; font-size: .92rem; font-weight: 800; }

    .artifact-sample {
      position: relative;
      color: var(--paper-ink);
      background: var(--paper);
      border: 1px solid #cfc2ae;
      border-radius: 3px;
      box-shadow: 18px 18px 0 #1c2630, 18px 18px 0 1px #334252;
      transform: rotate(-1deg);
    }
    .artifact-sample::before {
      content: "SAMPLE OUTPUT";
      position: absolute;
      top: 18px;
      right: 22px;
      padding: 7px 9px;
      border: 1px solid #e36b34;
      color: #b94417;
      font-size: .62rem;
      font-weight: 900;
      letter-spacing: .12em;
      transform: rotate(2deg);
    }
    .paper-head { padding: 28px 30px 22px; border-bottom: 1px solid #cec2af; }
    .paper-head small { color: #6d655b; font-size: .7rem; letter-spacing: .1em; }
    .paper-head h2 {
      margin: 8px 0 0;
      font-family: ui-sans-serif, system-ui, sans-serif;
      font-size: clamp(1.5rem, 3vw, 2.4rem);
      letter-spacing: -.045em;
    }
    .resume-copy { padding: 27px 30px 12px; font-family: ui-sans-serif, system-ui, sans-serif; }
    .resume-copy p { margin: 0; font-size: clamp(.98rem, 1.5vw, 1.12rem); line-height: 1.72; }
    .resume-copy mark { padding: 0 .18em; color: inherit; background: #ffd196; }
    .sample-metrics {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      margin: 20px 30px 0;
      border-top: 1px solid #cec2af;
      border-bottom: 1px solid #cec2af;
    }
    .sample-metrics div { padding: 17px 8px; border-right: 1px solid #cec2af; }
    .sample-metrics div:last-child { border-right: 0; }
    .sample-metrics strong { display: block; font-size: 1.35rem; }
    .sample-metrics span { color: #766e64; font-size: .67rem; text-transform: uppercase; }
    .deliverables { display: flex; flex-wrap: wrap; gap: 8px; padding: 22px 30px 27px; }
    .deliverables span { padding: 7px 9px; border: 1px solid #9f9588; border-radius: 999px; font-size: .7rem; font-weight: 800; }
    .sample-note { margin: 0; padding: 0 30px 28px; color: #6d655b; font-size: .7rem; }

    .dashboard { padding: 94px 0 120px; border-top: 1px solid var(--line); }
    .section-head {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 28px;
      align-items: end;
      margin-bottom: 34px;
    }
    .section-head h2 {
      margin: 10px 0 0;
      font-family: ui-sans-serif, system-ui, sans-serif;
      font-size: clamp(2rem, 4vw, 4rem);
      letter-spacing: -.055em;
    }
    .sync-state { min-width: 220px; color: var(--muted); text-align: right; font-size: .76rem; line-height: 1.6; }
    .sync-state strong { display: block; color: var(--ink); font-size: .82rem; }

    .status-banner {
      display: flex;
      align-items: center;
      gap: 12px;
      min-height: 50px;
      margin-bottom: 16px;
      padding: 12px 16px;
      border: 1px solid var(--line);
      border-left: 4px solid var(--yellow);
      background: var(--panel);
      color: var(--muted);
      font-family: ui-sans-serif, system-ui, sans-serif;
      font-size: .9rem;
    }
    .status-banner[data-state="complete"] { border-left-color: var(--green); color: var(--ink); }
    .status-banner[data-state="running"] { border-left-color: var(--cyan); color: var(--ink); }
    .status-banner[data-state="error"] { border-left-color: var(--red); color: #ffd1cd; }

    .flow-wrap { overflow-x: auto; padding: 8px 2px 22px; scrollbar-color: var(--line) transparent; }
    .flow {
      display: grid;
      grid-template-columns: repeat(8, minmax(146px, 1fr));
      min-width: 1168px;
      margin: 0;
      padding: 0;
      list-style: none;
    }
    .flow-item { position: relative; }
    .flow-item::after {
      content: "";
      position: absolute;
      z-index: 0;
      top: 28px;
      right: -50%;
      width: 100%;
      height: 1px;
      background: var(--line);
    }
    .flow-item:last-child::after { display: none; }
    .flow-item a {
      position: relative;
      z-index: 1;
      display: block;
      min-height: 154px;
      margin-right: 10px;
      padding: 14px;
      text-decoration: none;
      border: 1px solid var(--line);
      border-radius: 13px;
      background: var(--panel);
      transition: transform 160ms ease, border-color 160ms ease, background 160ms ease;
    }
    .flow-item a:hover { transform: translateY(-3px); border-color: #536579; }
    .station-index {
      display: grid;
      place-items: center;
      width: 28px;
      height: 28px;
      margin-bottom: 18px;
      color: var(--muted);
      border: 1px solid var(--line);
      border-radius: 50%;
      background: var(--canvas);
      font-size: .65rem;
      font-weight: 900;
    }
    .flow-copy strong { display: block; font-family: ui-sans-serif, system-ui, sans-serif; font-size: .88rem; }
    .flow-copy small { display: block; margin-top: 6px; color: var(--muted); font-size: .62rem; line-height: 1.35; }
    .station-state { display: block; margin-top: 15px; color: var(--muted); font-size: .66rem; font-weight: 800; }
    .flow-item[data-state="complete"] a { border-color: #3e805b; background: #13251e; }
    .flow-item[data-state="complete"] .station-index { color: #07170f; border-color: var(--green); background: var(--green); }
    .flow-item[data-state="complete"] .station-state { color: var(--green); }
    .flow-item[data-state="running"] a { border-color: var(--cyan); background: #112426; }
    .flow-item[data-state="running"] .station-index { color: #071618; border-color: var(--cyan); background: var(--cyan); }
    .flow-item[data-state="running"] .station-state { color: var(--cyan); }
    .flow-item[data-state="error"] a { border-color: var(--red); background: #2b191b; }
    .flow-item[data-state="error"] .station-index { color: #260b0b; border-color: var(--red); background: var(--red); }
    .flow-item[data-state="error"] .station-state { color: var(--red); }
    .flow-item[data-state="waiting"] a { border-style: dashed; }
    .flow-item[data-state="waiting"] .station-state { color: var(--yellow); }

    .summary-grid { display: grid; grid-template-columns: .82fr 1.18fr; gap: 16px; margin-top: 14px; }
    .cost-card, .reports-card { min-height: 222px; padding: 25px; border: 1px solid var(--line); border-radius: var(--radius); background: var(--panel); }
    .card-label { color: var(--muted); font-size: .7rem; font-weight: 800; letter-spacing: .1em; text-transform: uppercase; }
    .cost-amount { display: block; margin: 18px 0 6px; font-family: ui-sans-serif, system-ui, sans-serif; font-size: clamp(2.4rem, 5vw, 4.4rem); line-height: 1; letter-spacing: -.07em; }
    .cost-kind { color: var(--cyan); font-size: .72rem; }
    .engine-meta { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px 14px; margin: 20px 0 0; }
    .engine-meta div { min-width: 0; padding: 9px 10px; border: 1px solid var(--line); border-radius: 8px; background: var(--canvas); }
    .engine-meta dt { color: var(--muted); font-size: .62rem; }
    .engine-meta dd { margin: 4px 0 0; font-size: .72rem; font-weight: 800; overflow-wrap: anywhere; }
    .two-bills { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin: 24px 0 0; }
    .two-bills div { padding-top: 12px; border-top: 1px solid var(--line); }
    .two-bills dt { color: var(--muted); font-size: .65rem; }
    .two-bills dd { margin: 5px 0 0; font-size: .72rem; line-height: 1.45; }
    .reports-card h3 { margin: 12px 0 6px; font-family: ui-sans-serif, system-ui, sans-serif; font-size: 1.5rem; }
    .reports-card > p { margin: 0; color: var(--muted); font-size: .75rem; line-height: 1.5; }
    .report-list { display: flex; flex-wrap: wrap; gap: 9px; margin: 22px 0 0; padding: 0; list-style: none; }
    .report-list a, .station-artifacts a {
      display: inline-flex;
      align-items: center;
      min-height: 36px;
      padding: 8px 11px;
      color: var(--cyan);
      border: 1px solid #365b61;
      border-radius: 8px;
      background: #102024;
      text-decoration: none;
      font-size: .72rem;
      overflow-wrap: anywhere;
    }
    .report-list a::after, .station-artifacts a::after { content: "↗"; margin-left: 8px; }
    .report-empty { margin-top: 22px; color: var(--muted); font-size: .76rem; }

    .stations-head { display: flex; align-items: end; justify-content: space-between; gap: 20px; margin: 90px 0 26px; }
    .stations-head h2 { margin: 8px 0 0; font-family: ui-sans-serif, system-ui, sans-serif; font-size: clamp(1.8rem, 3vw, 3rem); letter-spacing: -.045em; }
    .stations-head p { max-width: 40rem; margin: 0; color: var(--muted); font-size: .75rem; line-height: 1.6; }
    .station-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
    .station-card { scroll-margin-top: 24px; padding: 23px; border: 1px solid var(--line); border-radius: var(--radius); background: var(--panel); }
    .station-card:target { border-color: var(--cyan); }
    .station-card-head { display: flex; align-items: start; justify-content: space-between; gap: 18px; }
    .station-card h3 { margin: 6px 0 0; font-family: ui-sans-serif, system-ui, sans-serif; font-size: 1.35rem; }
    .detail-state { padding: 6px 8px; color: var(--muted); border: 1px solid var(--line); border-radius: 999px; font-size: .65rem; }
    .station-card[data-state="complete"] .detail-state { color: var(--green); border-color: #3e805b; }
    .station-card[data-state="running"] .detail-state { color: var(--cyan); border-color: var(--cyan); }
    .station-card[data-state="error"] .detail-state { color: var(--red); border-color: var(--red); }
    .station-card[data-state="waiting"] .detail-state { color: var(--yellow); }
    .resume-phrase { margin: 8px 0 20px; color: var(--muted); font-size: .72rem; }
    .station-card dl { margin: 0; }
    .station-card dl div { display: grid; grid-template-columns: 82px 1fr; gap: 10px; padding: 13px 0; border-top: 1px solid var(--line); }
    .station-card dt { color: var(--muted); font-size: .68rem; }
    .station-card dd { margin: 0; font-family: ui-sans-serif, system-ui, sans-serif; font-size: .86rem; }
    .command-block { margin-top: 8px; padding: 13px; border: 1px solid var(--line); border-radius: 10px; background: #0a0f14; }
    .command-block span { display: block; margin-bottom: 7px; color: var(--muted); font-size: .62rem; text-transform: uppercase; }
    .command-block code { color: #f6d8ab; font-size: .75rem; overflow-wrap: anywhere; }
    .station-artifacts { display: flex; flex-wrap: wrap; gap: 8px; min-height: 35px; margin-top: 13px; }
    .empty-artifact { align-self: center; color: var(--muted); font-size: .68rem; }
    .raw-artifacts { flex-basis: 100%; padding-top: 6px; color: var(--muted); font-size: .7rem; }
    .raw-artifacts summary { cursor: pointer; }
    .raw-artifact-links { display: flex; flex-wrap: wrap; gap: 8px; padding-top: 10px; }

    footer { padding: 28px 0 44px; color: var(--muted); border-top: 1px solid var(--line); font-size: .7rem; line-height: 1.6; }

    @media (max-width: 880px) {
      .hero { grid-template-columns: 1fr; min-height: auto; padding-top: 70px; }
      h1 { max-width: 10ch; }
      .artifact-sample { transform: none; box-shadow: 10px 10px 0 #1c2630; }
      .summary-grid, .station-list { grid-template-columns: 1fr; }
      .section-head, .stations-head { display: block; }
      .sync-state { margin-top: 20px; text-align: left; }
      .stations-head p { margin-top: 14px; }
    }
    @media (max-width: 540px) {
      .shell { width: min(100% - 24px, 1240px); }
      .site-header { min-height: 60px; }
      .read-only span { display: none; }
      .hero { padding: 48px 0 58px; }
      .hero-proof, .sample-metrics { grid-template-columns: 1fr; }
      .sample-metrics div { border-right: 0; border-bottom: 1px solid #cec2af; }
      .sample-metrics div:last-child { border-bottom: 0; }
      .artifact-sample::before { position: static; display: inline-block; margin: 18px 0 0 20px; }
      .paper-head, .resume-copy { padding-left: 20px; padding-right: 20px; }
      .sample-metrics { margin-left: 20px; margin-right: 20px; }
      .deliverables { padding-left: 20px; padding-right: 20px; }
      .sample-note { padding-left: 20px; padding-right: 20px; }
      .two-bills { grid-template-columns: 1fr; }
      .station-card dl div { grid-template-columns: 1fr; gap: 5px; }
    }
    @media (prefers-reduced-motion: reduce) {
      html { scroll-behavior: auto; }
      *, *::before, *::after { transition-duration: .01ms !important; }
    }
  </style>
</head>
<body>
  <a class="skip-link" href="#dashboard">跳到实战进度</a>
  <header class="site-header shell">
    <div class="brand"><span class="brand-mark" aria-hidden="true"></span><span>SELF-EVOLVING / SKILL</span></div>
    <div class="read-only" title="看板只读取本地状态，不执行命令"><span>LOCAL READ-ONLY</span></div>
  </header>

  <main>
    <section class="hero shell" aria-labelledby="hero-title">
      <div>
        <span class="kicker">ONE-DAY PRACTICE · EVIDENCE FIRST</span>
        <h1 id="hero-title">从一次失败，到一段能追问的经历。</h1>
        <p class="hero-lede">你会亲手跑完 Skill 的真实进化闭环。最后带走的不是证书，是一组有运行证据、有版本记录、能在面试中展开讲的产物。</p>
        <dl class="hero-proof" aria-label="课程属性">
          <div><dt>环境</dt><dd>STATE-Bench 沙盒</dd></div>
          <div><dt>路径</dt><dd>8 站完整闭环</dd></div>
          <div><dt>结果</dt><dd>证据自动填充</dd></div>
        </dl>
      </div>

      <aside class="artifact-sample" aria-labelledby="sample-title">
        <div class="paper-head"><small>毕业产物样例 / RESUME EVIDENCE</small><h2 id="sample-title">Self-evolving Skill 实战</h2></div>
        <div class="resume-copy"><p>在 STATE-Bench 客服退货沙盒中运行 Skill 自进化闭环：执行 <mark>16 个 case</mark>，定位 <mark>3 类失败</mark>，以最小补丁将通过率从 <mark>68.8% 提升至 87.5%</mark>，并通过全量回归 Gate 验证零退化，完成 v0 → v1 发版与回滚演练。</p></div>
        <div class="sample-metrics" aria-label="样例指标">
          <div><strong>+18.7pp</strong><span>通过率变化</span></div>
          <div><strong>0</strong><span>存量退化</span></div>
          <div><strong>v1</strong><span>发布版本</span></div>
        </div>
        <div class="deliverables" aria-label="三件套"><span>中英简历段落</span><span>面试追问准备</span><span>概念清单</span></div>
        <p class="sample-note">以上均为版式样例。你的版本只会引用自己的 evidence 与真实数字。</p>
      </aside>
    </section>

    <section class="dashboard" id="dashboard" aria-labelledby="dashboard-title">
      <div class="shell">
        <div class="section-head">
          <div><span class="kicker" id="journey-mode-kicker">JOURNEY STATUS</span><h2 id="dashboard-title">你的 8 站进度</h2></div>
          <div class="sync-state"><strong id="sync-label">等待本地状态</strong><span id="sync-time">尚未同步</span></div>
        </div>

        <div class="status-banner" id="status-banner" data-state="waiting" role="status" aria-live="polite">等待终端完成第一条命令。看板会自动更新，无需刷新。</div>

        <nav class="flow-wrap" aria-label="八站学习流程">
          <ol class="flow">
            __FLOW__
          </ol>
        </nav>

        <div class="summary-grid">
          <section class="cost-card" aria-labelledby="cost-title">
            <span class="card-label" id="cost-title">实验引擎成本</span>
            <output class="cost-amount" id="cost-amount">—</output>
            <span class="cost-kind" id="cost-kind">等待本地 status</span>
            <dl class="engine-meta" aria-label="实验运行来源">
              <div><dt>模式</dt><dd id="experiment-mode-label">等待状态</dd></div>
              <div><dt>Provider</dt><dd id="experiment-provider-label">未记录</dd></div>
              <div><dt>成本来源</dt><dd id="cost-source-label">未记录</dd></div>
              <div><dt>模型锁</dt><dd id="model-lock-label">未记录</dd></div>
            </dl>
            <dl class="two-bills">
              <div><dt>讲师</dt><dd>由你自己的 coding agent 订阅或 Key 结算；本看板不读取。</dd></div>
              <div><dt>实验引擎</dt><dd id="experiment-bill">等待 status 写入所选 Provider；本看板不读取 Key。</dd></div>
            </dl>
          </section>

          <section class="reports-card" aria-labelledby="reports-title">
            <span class="card-label">EVIDENCE LINKS</span>
            <h3 id="reports-title">报告与毕业产物</h3>
            <p>这里只出现本地 status 明确列出的公开产物；看板不会扫描你的目录。</p>
            <ul class="report-list" id="report-list"></ul>
            <div class="report-empty" id="report-empty">完成一站后，第一份报告会自动出现。</div>
          </section>
        </div>

        <div class="stations-head">
          <div><span class="kicker">COMMAND INDEX</span><h2>每站一条命令</h2></div>
          <p>通常由讲师代跑。你也可以手动执行；页面只展示命令，不会替你执行。</p>
        </div>
        <div class="station-list">
          __STATION_CARDS__
        </div>
      </div>
    </section>
  </main>

  <footer><div class="shell">本地只读看板 · 仅轮询 <code>/.ses/status.json</code> · 不保存 Key · 不发起外网请求</div></footer>

  <script>
    "use strict";
    const stationDefinitions = __STATIONS_JSON__;
    const stateLabels = { complete: "已完成", running: "运行中", error: "需处理", waiting: "等待中", pending: "未开始" };
    const reportList = document.getElementById("report-list");
    const reportEmpty = document.getElementById("report-empty");
    const statusBanner = document.getElementById("status-banner");
    const syncLabel = document.getElementById("sync-label");
    const syncTime = document.getElementById("sync-time");
    const journeyModeKicker = document.getElementById("journey-mode-kicker");
    const experimentModeLabel = document.getElementById("experiment-mode-label");
    const experimentProviderLabel = document.getElementById("experiment-provider-label");
    const costSourceLabel = document.getElementById("cost-source-label");
    const modelLockLabel = document.getElementById("model-lock-label");
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
      if (source === "synthetic_ci") return "Synthetic CI";
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

      setText(journeyModeKicker, fixed ? "FIXED CI JOURNEY" : mode === "live" ? "LIVE JOURNEY" : "JOURNEY STATUS");
      setText(experimentModeLabel, fixed ? "固定 CI" : mode === "live" ? "Live" : "等待状态");
      setText(experimentProviderLabel, fixed ? "不调用 Provider" : provider);
      setText(costSourceLabel, costSourceDisplayName(costSource));
      if (typeof lockHash === "string" && /^[a-f0-9]{64}$/i.test(lockHash)) {
        setText(modelLockLabel, `sha256:${lockHash.slice(0, 12)}…`);
        modelLockLabel.title = lockHash;
      } else {
        setText(modelLockLabel, "未记录");
        modelLockLabel.removeAttribute("title");
      }
      if (fixed) {
        setText(experimentBill, "固定 CI 不调用外部 Provider；合成值不代表 live 成本。");
      } else if (provider === "SiliconFlow" || provider === "ChatAnywhere") {
        setText(experimentBill, `${provider} 结算；这里只读取本地 status，不读取 Key。`);
      } else {
        setText(experimentBill, "等待 status 写入所选 Provider；本看板不读取 Key。");
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
      if (Array.isArray(source)) return source.find((item, index) => String(firstValue(item, ["id", "station_id", "station", "index"]) ?? index) === String(id)) || {};
      if (source && typeof source === "object") return source[id] || source[`station-${id}`] || source[`station_${id}`] || {};
      return {};
    }

    function idSet(value) {
      const values = Array.isArray(value) ? value : [];
      return new Set(values.map(item => String((item && typeof item === "object") ? firstValue(item, ["id", "station_id", "station"]) : item)));
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
        value.forEach((item, index) => flattenArtifacts(item, `${fallback} ${index + 1}`, output));
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
      link.textContent = item.label || "打开产物";
      link.title = item.path;
      return link;
    }

    function setStation(id, state, record) {
      const flow = document.querySelector(`[data-flow-station="${id}"]`);
      const detail = document.querySelector(`[data-station="${id}"]`);
      const label = stateLabels[state] || stateLabels.pending;
      if (flow) {
        flow.dataset.state = state;
        const stateNode = flow.querySelector("[data-flow-state]");
        setText(stateNode, label);
        const link = flow.querySelector("a");
        if (link) {
          const definition = stationDefinitions.find(item => String(item.id) === String(id));
          link.setAttribute("aria-label", `站 ${id}，${definition ? definition.name : ""}，${label}`);
          if (state === "running") link.setAttribute("aria-current", "step"); else link.removeAttribute("aria-current");
        }
      }
      if (!detail) return [];
      detail.dataset.state = state;
      const stateNode = detail.querySelector("[data-detail-state]");
      setText(stateNode, label);
      const command = firstValue(record, ["command", "next_command"]);
      if (typeof command === "string" && command.trim()) setText(detail.querySelector("[data-command]"), command);
      const artifacts = artifactsFrom(record, `站 ${id} 产物`);
      const container = detail.querySelector("[data-station-artifacts]");
      const signature = JSON.stringify({ state, artifacts });
      if (container.dataset.renderSignature === signature) return artifacts;
      container.dataset.renderSignature = signature;
      container.replaceChildren();
      let count = 0;
      const rawArtifacts = [];
      artifacts.forEach(item => {
        if (isPerCaseEvidence(item)) { rawArtifacts.push(item); return; }
        const link = artifactLink(item);
        if (link) { container.append(link); count += 1; }
      });
      if (rawArtifacts.length) {
        const details = document.createElement("details");
        details.className = "raw-artifacts";
        const summary = document.createElement("summary");
        summary.textContent = `原始逐 case 证据（${rawArtifacts.length}）`;
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
      if (!count) {
        const empty = document.createElement("span");
        empty.className = "empty-artifact";
        empty.textContent = state === "complete" ? "本站已完成，暂无公开产物。" : "完成本站后，产物链接会出现在这里。";
        container.append(empty);
      }
      return artifacts;
    }

    function renderCost(data) {
      const value = firstValue(data, ["experiment_usage", "experiment_cost", "experimental_cost", "total_cost", "cost"]);
      let amount = value;
      let currency = firstValue(data, ["cost_currency", "currency"]);
      let complete = firstValue(data, ["cost_complete"]) === true;
      const source = String(firstValue(data, ["cost_source"]) || "").trim().toLowerCase();
      if (value && typeof value === "object") {
        amount = firstValue(value, ["amount", "value", "total", "cost_amount"]);
        const minor = firstValue(value, ["amount_minor"]);
        if (amount === undefined && Number.isFinite(Number(minor))) amount = Number(minor) / 100;
        currency = firstValue(value, ["currency", "cost_currency"]) || currency;
        complete = firstValue(value, ["cost_complete"]) === true;
      }
      const hasAmount = amount !== undefined && amount !== null && amount !== "" && Number.isFinite(Number(amount));
      const code = typeof currency === "string" ? currency.trim().toUpperCase() : "";
      const formatted = hasAmount && code
        ? `${code === "CNY" ? "¥" : code === "USD" ? "$" : `${code} `}${Number(amount).toFixed(4).replace(/0+$/, "").replace(/\.$/, "")}`
        : null;

      if (source === "unavailable") {
        setText(costAmount, "—");
        setText(costKind, "费用不可用 · status 没有可靠成本");
        return;
      }
      if (!complete) {
        setText(costAmount, formatted || "—");
        setText(costKind, "部分数据 · 不能作为完整账单");
        return;
      }
      if (!formatted) {
        setText(costAmount, "—");
        setText(costKind, "费用不可用 · status 缺少金额或币种");
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

    function renderReports(artifacts) {
      reportList.replaceChildren();
      const seen = new Set();
      let count = 0;
      artifacts.forEach(item => {
        if (isPerCaseEvidence(item)) return;
        const link = artifactLink(item);
        if (!link || seen.has(link.href)) return;
        seen.add(link.href);
        const row = document.createElement("li");
        row.append(link);
        reportList.append(row);
        count += 1;
      });
      reportEmpty.hidden = count > 0;
    }

    function renderStatus(data) {
      const allArtifacts = [];
      let completeCount = 0;
      let activeState = "pending";
      stationDefinitions.forEach(definition => {
        const record = stationRecord(data, definition.id);
        const state = stationState(data, record, definition.id);
        if (state === "complete") completeCount += 1;
        if (["error", "running", "waiting"].includes(state)) activeState = state;
        allArtifacts.push(...setStation(definition.id, state, record));
      });
      allArtifacts.push(...artifactsFrom(data, "课程产物"));
      renderReports(allArtifacts);
      renderEngineContext(data);
      renderCost(data);

      const overall = normalizeState(firstValue(data, ["overall_status", "status", "state"]));
      const state = overall !== "pending" ? overall : activeState;
      const message = firstValue(data, ["message", "status_message", "error_message"]);
      statusBanner.dataset.state = state;
      if (typeof message === "string" && message.trim()) setText(statusBanner, message);
      else if (completeCount === stationDefinitions.length) setText(statusBanner, "8 站已全部点亮。你的毕业产物与证据链接已就位。");
      else if (state === "error") setText(statusBanner, "当前站需要处理。请回到终端查看讲师给出的下一步。");
      else if (state === "running") setText(statusBanner, "模型正在运行。你可以继续和讲师聊，完成后看板会自动更新。");
      else setText(statusBanner, "等待终端写入下一条进度。看板会自动更新，无需刷新。");

      setText(syncLabel, `${completeCount} / ${stationDefinitions.length} 站完成`);
      const sourceTime = firstValue(data, ["updated_at", "last_updated_at", "timestamp"]);
      setText(syncTime, sourceTime ? `状态时间 ${String(sourceTime)}` : `刚刚同步 · ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`);
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
        setText(statusBanner, "暂时读不到本地状态。请确认 dashboard 仍在运行，再回终端查看最近一条命令。");
        setText(syncLabel, "等待重新连接");
        setText(syncTime, "看板会自动重试");
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
    return (
        _DOCUMENT.replace("__FLOW__", _flow_markup())
        .replace("__STATION_CARDS__", _station_markup())
        .replace("__STATIONS_JSON__", stations_json.replace("</", "<\\/"))
    )
