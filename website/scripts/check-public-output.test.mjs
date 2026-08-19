import assert from "node:assert/strict";
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { checkPublicOutput, findPublicOutputViolations } from "./check-public-output.mjs";

async function makeDist(t) {
  const distRoot = await fs.mkdtemp(path.join(os.tmpdir(), "ses-public-output-"));
  t.after(() => fs.rm(distRoot, { recursive: true, force: true }));
  return distRoot;
}

test("allows learner-facing cost and budget concepts without a specific price", async (t) => {
  const distRoot = await makeDist(t);
  await fs.mkdir(path.join(distRoot, "reports"));
  await fs.mkdir(path.join(distRoot, "assets"));
  await fs.writeFile(
    path.join(distRoot, "index.html"),
    "<main><h1>课程</h1><p>你会学习成本与预算护栏，但本站不公开维护记录。</p></main>",
  );
  await fs.writeFile(path.join(distRoot, "reports/level-1.html"), "<main>L1 报告阅读指南</main>");
  await fs.writeFile(
    path.join(distRoot, "assets/app.test.js"),
    'const parserToken = "$0,0"; export const ready = Boolean(parserToken);\n',
  );
  await fs.writeFile(
    path.join(distRoot, "hashmap.json"),
    JSON.stringify({ "course_lessons_02-grade-terminal-state_index.md": "short-hash" }),
  );

  await checkPublicOutput(distRoot);
});

test("rejects raw report, event, and evidence filenames", async (t) => {
  const distRoot = await makeDist(t);
  await fs.mkdir(path.join(distRoot, "downloads"));
  await fs.writeFile(path.join(distRoot, "index.html"), '<a href="/downloads/l3.html">raw</a>');
  await fs.writeFile(path.join(distRoot, "l1.html"), "report");
  await fs.writeFile(path.join(distRoot, "downloads/l2.html"), "report");
  await fs.writeFile(path.join(distRoot, "downloads/events.jsonl"), "{}\n");
  await fs.writeFile(path.join(distRoot, "downloads/trace.json"), "{}\n");

  const violations = await findPublicOutputViolations(distRoot);
  assert.ok(violations.some((item) => item.rule === "raw L1 report"));
  assert.ok(violations.some((item) => item.rule === "raw L2 report"));
  assert.ok(violations.some((item) => item.rule === "raw L1, L2, or L3 report reference"));
  assert.ok(violations.some((item) => item.rule === "raw event log"));
  assert.ok(violations.some((item) => item.rule === "raw trace, state, grade, or prompt file"));
});

test("rejects prices, maintenance fields, token counts, provider details, and full hashes", async (t) => {
  const distRoot = await makeDist(t);
  const fullHash = "a".repeat(64);
  await fs.writeFile(
    path.join(distRoot, "unsafe.json"),
    JSON.stringify({
      note: "本次记录是 0.01 USD，使用 120 tokens",
      price: "$5",
      provider_name: "example",
      model_lock: "internal",
      trace: { prompt: "hidden" },
      digest: fullHash,
    }),
  );

  const violations = await findPublicOutputViolations(distRoot);
  const rules = new Set(violations.map((item) => item.rule));
  assert.ok(rules.has("currency code or name"));
  assert.ok(rules.has("specific currency amount"));
  assert.ok(rules.has("currency or amount field"));
  assert.ok(rules.has("specific token count"));
  assert.ok(rules.has("provider maintenance detail"));
  assert.ok(rules.has("model-lock maintenance detail"));
  assert.ok(rules.has("raw trace, state, grade, or prompt field"));
  assert.ok(rules.has("64-character hexadecimal hash"));
  await assert.rejects(checkPublicOutput(distRoot), /Public output contains forbidden material/);
});

test("checks visible text embedded in SVG files", async (t) => {
  const distRoot = await makeDist(t);
  await fs.writeFile(
    path.join(distRoot, "mark.svg"),
    '<svg xmlns="http://www.w3.org/2000/svg"><text>维护记录：5 元</text></svg>',
  );

  const violations = await findPublicOutputViolations(distRoot);
  assert.ok(violations.some((item) => item.rule === "specific currency amount"));
});

test("rejects every file path outside the explicit public allowlist", async (t) => {
  const distRoot = await makeDist(t);
  await fs.writeFile(path.join(distRoot, "course-export.zip"), "not really a zip");
  await fs.writeFile(path.join(distRoot, "unexpected.html"), "<main>unexpected</main>");

  const violations = await findPublicOutputViolations(distRoot);
  assert.deepEqual(
    violations.filter((item) => item.rule === "file path outside public allowlist"),
    [
      { file: "course-export.zip", rule: "file path outside public allowlist" },
      { file: "unexpected.html", rule: "file path outside public allowlist" },
    ],
  );
});

test("rejects symbolic links in the public output", async (t) => {
  const distRoot = await makeDist(t);
  const outsidePath = path.join(path.dirname(distRoot), `${path.basename(distRoot)}-outside.txt`);
  t.after(() => fs.rm(outsidePath, { force: true }));
  await fs.writeFile(outsidePath, "outside\n");
  await fs.symlink(outsidePath, path.join(distRoot, "linked.txt"));

  const violations = await findPublicOutputViolations(distRoot);
  assert.deepEqual(violations, [{ file: "linked.txt", rule: "symbolic link in public output" }]);
});
