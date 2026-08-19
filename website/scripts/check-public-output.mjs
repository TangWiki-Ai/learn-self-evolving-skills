#!/usr/bin/env node

import { promises as fs } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
export const DEFAULT_DIST_ROOT = path.resolve(SCRIPT_DIR, "../.vitepress/dist");

const TEXT_EXTENSIONS = new Set([
  ".css",
  ".html",
  ".js",
  ".json",
  ".mjs",
  ".svg",
  ".txt",
  ".webmanifest",
  ".xml",
]);

const ALLOWED_PUBLIC_FILES = new Set([
  "404.html",
  "course/index.html",
  "course/lessons/01-see-the-difference/index.html",
  "course/lessons/02-grade-terminal-state/index.html",
  "course/lessons/03-calibrate-judges/index.html",
  "course/lessons/04-reproducible-baseline/index.html",
  "course/lessons/05-mine-benchmark-data/index.html",
  "course/lessons/06-verify-develop-cases/index.html",
  "course/lessons/07-create-v0/index.html",
  "course/lessons/08-evidence-linked-candidate/index.html",
  "course/lessons/09-gate-and-govern-versions/index.html",
  "course/lessons/10-auto-evolve-and-portfolio/index.html",
  "evidence/index.html",
  "hashmap.json",
  "index.html",
  "mark.svg",
  "reports/index.html",
  "reports/level-1.html",
  "reports/level-2.html",
  "reports/level-3.html",
  "sitemap.xml",
  "start/index.html",
  "troubleshooting/index.html",
  "vp-icons.css",
]);

const ALLOWED_ASSET_PATH = /^assets\/(?:chunks\/)?[A-Za-z0-9@._+~-]+\.(?:css|js|woff2)$/;

const FORBIDDEN_BASENAMES = new Map([
  ["l1.html", "raw L1 report"],
  ["l2.html", "raw L2 report"],
  ["l3.html", "raw L3 report"],
  ["events.jsonl", "raw event log"],
]);

const ALWAYS_CONTENT_RULES = [
  {
    name: "raw L1, L2, or L3 report reference",
    pattern: /(?:^|[\/="'(])l[123]\.html\b/i,
  },
  {
    name: "raw event-log reference",
    pattern: /\bevents\.jsonl\b/i,
  },
  {
    name: "raw trace, state, grade, or prompt artifact reference",
    pattern:
      /(?:^|[\/="'(])(?:trace(?:[-_.][a-z0-9-]+)?|traces|state|state[-_]?diff|case[-_]?grade|grade|prompt(?:[-_.][a-z0-9-]+)?)\.(?:html|json|jsonl|md|txt)\b/i,
  },
  {
    name: "64-character hexadecimal hash",
    pattern: /(?:^|[^0-9a-f])[0-9a-f]{64}(?:$|[^0-9a-f])/i,
  },
];

const VISIBLE_CONTENT_RULES = [
  {
    name: "currency code or name",
    pattern:
      /(?:\b(?:AUD|CAD|CNY|EUR|GBP|HKD|JPY|RMB|USD)\b|人民币|美元|欧元|英镑|日元|港币)/i,
  },
  {
    name: "specific currency amount",
    pattern: /(?:[¥￥$€£]\s*\d|\d(?:[\d,.]*\d)?\s*元(?!素))/,
  },
  {
    name: "token accounting field",
    pattern:
      /\b(?:completion_tokens?|input_tokens?|output_tokens?|prompt_tokens?|token_count|total_tokens?)\b/i,
  },
  {
    name: "specific token count",
    pattern: /\b\d[\d,.]*\s*tokens?\b/i,
  },
  {
    name: "provider maintenance detail",
    pattern:
      /\bProvider\b|\b(?:provider_(?:name|model|endpoint|response|payload)|provider\s+smoke|Provider\s+smoke|SiliconFlow|siliconflow)\b|硅基流动|供应商端点/,
  },
  {
    name: "model-lock maintenance detail",
    pattern: /\bmodel[-_ ]lock\b|模型锁/i,
  },
];

const STRUCTURED_CONTENT_RULES = [
  {
    name: "raw trace, state, grade, or prompt field",
    pattern:
      /["'](?:trace|traces|state|state_diff|state_before|state_after|grade|case_grade|prompt|system_prompt|creator_prompt)["']\s*:/i,
  },
  {
    name: "currency or amount field",
    pattern:
      /["'](?:amount|amount_minor|cost|cost_amount|cost_cny|cost_usd|currency|price|price_amount|total_cost_amount|unit_price)["']\s*:/i,
  },
  {
    name: "provider field",
    pattern:
      /["'](?:model_id|model_name|model_version|provider|provider_endpoint|provider_model|provider_name)["']\s*:/i,
  },
];

function isRawEvidencePath(relativePath) {
  const segments = relativePath.toLowerCase().split("/");
  const basename = segments.at(-1);
  if (FORBIDDEN_BASENAMES.has(basename)) {
    return FORBIDDEN_BASENAMES.get(basename);
  }
  if (
    /^(?:trace|traces|state(?:[-_]?diff)?|case[-_]?grade|grade|prompt)(?:[-_.]|$)/i.test(basename)
  ) {
    return "raw trace, state, grade, or prompt file";
  }
  return null;
}

function isAllowedPublicPath(relativePath) {
  return ALLOWED_PUBLIC_FILES.has(relativePath) || ALLOWED_ASSET_PATH.test(relativePath);
}

async function listFiles(root) {
  const files = [];
  const pending = [root];
  while (pending.length > 0) {
    const current = pending.pop();
    const entries = await fs.readdir(current, { withFileTypes: true });
    entries.sort((left, right) => left.name.localeCompare(right.name));
    for (const entry of entries) {
      const entryPath = path.join(current, entry.name);
      if (entry.isSymbolicLink()) {
        files.push({ path: entryPath, symlink: true });
      } else if (entry.isDirectory()) {
        pending.push(entryPath);
      } else if (entry.isFile()) {
        files.push({ path: entryPath, symlink: false });
      }
    }
  }
  return files.sort((left, right) => left.path.localeCompare(right.path));
}

function addViolation(violations, relativePath, rule) {
  if (!violations.some((item) => item.file === relativePath && item.rule === rule)) {
    violations.push({ file: relativePath, rule });
  }
}

export async function findPublicOutputViolations(distRoot = DEFAULT_DIST_ROOT) {
  let stat;
  try {
    stat = await fs.stat(distRoot);
  } catch (error) {
    if (error && error.code === "ENOENT") {
      throw new Error(`Public output directory does not exist: ${distRoot}`);
    }
    throw error;
  }
  if (!stat.isDirectory()) {
    throw new Error(`Public output path is not a directory: ${distRoot}`);
  }

  const violations = [];
  for (const file of await listFiles(distRoot)) {
    const relativePath = path.relative(distRoot, file.path).split(path.sep).join("/");
    if (file.symlink) {
      addViolation(violations, relativePath, "symbolic link in public output");
      continue;
    }

    if (!isAllowedPublicPath(relativePath)) {
      addViolation(violations, relativePath, "file path outside public allowlist");
    }

    const rawPathRule = isRawEvidencePath(relativePath);
    if (rawPathRule) {
      addViolation(violations, relativePath, rawPathRule);
    }

    const extension = path.extname(file.path).toLowerCase();
    if (!TEXT_EXTENSIONS.has(extension)) {
      continue;
    }
    const content = await fs.readFile(file.path, "utf8");
    for (const rule of ALWAYS_CONTENT_RULES) {
      if (rule.pattern.test(content)) {
        addViolation(violations, relativePath, rule.name);
      }
    }
    if (
      extension === ".html" ||
      extension === ".json" ||
      extension === ".svg" ||
      extension === ".txt" ||
      extension === ".xml"
    ) {
      for (const rule of VISIBLE_CONTENT_RULES) {
        if (rule.pattern.test(content)) {
          addViolation(violations, relativePath, rule.name);
        }
      }
    }
    if (extension === ".json") {
      const normalizedContent = content.replaceAll('\\"', '"').replaceAll("\\'", "'");
      for (const rule of STRUCTURED_CONTENT_RULES) {
        if (rule.pattern.test(normalizedContent)) {
          addViolation(violations, relativePath, rule.name);
        }
      }
    }
  }

  return violations;
}

export async function checkPublicOutput(distRoot = DEFAULT_DIST_ROOT) {
  const violations = await findPublicOutputViolations(distRoot);
  if (violations.length === 0) {
    return;
  }
  const details = violations.map((item) => `- ${item.file}: ${item.rule}`).join("\n");
  throw new Error(`Public output contains forbidden material:\n${details}`);
}

export async function main(argv = process.argv.slice(2)) {
  if (argv.length > 1 || argv.includes("--help") || argv.includes("-h")) {
    if (argv.includes("--help") || argv.includes("-h")) {
      process.stdout.write("Usage: node website/scripts/check-public-output.mjs [DIST_PATH]\n");
      return;
    }
    throw new Error("Expected at most one DIST_PATH argument");
  }
  const distRoot = argv[0] ? path.resolve(argv[0]) : DEFAULT_DIST_ROOT;
  await checkPublicOutput(distRoot);
  process.stdout.write(`Public output check passed: ${distRoot}\n`);
}

const isEntryPoint = process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1]);
if (isEntryPoint) {
  main().catch((error) => {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  });
}
