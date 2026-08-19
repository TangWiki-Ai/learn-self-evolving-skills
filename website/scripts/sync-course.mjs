#!/usr/bin/env node

import path from "node:path";
import { fileURLToPath } from "node:url";

import { DEFAULT_REPO_ROOT, generateCourseSite } from "./course-catalog.mjs";

export function parseArguments(argv) {
  const options = {
    check: false,
    repoRoot: DEFAULT_REPO_ROOT,
    websiteRoot: null,
  };

  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === "--check") {
      options.check = true;
      continue;
    }
    if (argument === "--repo-root" || argument === "--website-root") {
      const value = argv[index + 1];
      if (!value || value.startsWith("--")) {
        throw new Error(`${argument} requires a path`);
      }
      index += 1;
      if (argument === "--repo-root") {
        options.repoRoot = path.resolve(value);
      } else {
        options.websiteRoot = path.resolve(value);
      }
      continue;
    }
    if (argument === "--help" || argument === "-h") {
      return { help: true };
    }
    throw new Error(`Unknown argument: ${argument}`);
  }

  options.websiteRoot ??= path.join(options.repoRoot, "website");
  return options;
}

function helpText() {
  return `Usage: node website/scripts/sync-course.mjs [options]

Options:
  --check                Fail when generated files are missing or stale
  --repo-root PATH       Repository root (defaults to the script's repository)
  --website-root PATH    Output website root (defaults to <repo-root>/website)
  -h, --help             Show this help
`;
}

export async function main(argv = process.argv.slice(2)) {
  const options = parseArguments(argv);
  if (options.help) {
    process.stdout.write(helpText());
    return;
  }

  const result = await generateCourseSite(options);
  if (result.checked) {
    process.stdout.write(`Course catalog and ${result.files.length - 1} lesson pages are up to date.\n`);
    return;
  }
  process.stdout.write(
    `Synced course catalog and ${result.files.length - 1} lesson pages (${result.changed.length} files changed).\n`,
  );
}

const isEntryPoint = process.argv[1] && fileURLToPath(import.meta.url) === path.resolve(process.argv[1]);
if (isEntryPoint) {
  main().catch((error) => {
    process.stderr.write(`${error.message}\n`);
    process.exitCode = 1;
  });
}
