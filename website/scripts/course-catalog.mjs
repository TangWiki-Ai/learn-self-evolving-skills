import { promises as fs } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));

export const DEFAULT_REPO_ROOT = path.resolve(SCRIPT_DIR, "../..");
export const DEFAULT_CATALOG_PATH = "course/catalog.json";
export const GENERATED_CATALOG_PATH = ".vitepress/generated/course-catalog.json";
export const GENERATED_LESSONS_PATH = "course/lessons";

const REQUIRED_LESSON_STRINGS = [
  "slug",
  "title",
  "phase",
  "question",
  "outcome",
  "source",
  "evidence_summary",
  "mode",
  "learner_test_status",
];

function fail(message) {
  throw new Error(`Invalid course catalog: ${message}`);
}

function isObject(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function requireObject(value, label) {
  if (!isObject(value)) {
    fail(`${label} must be an object`);
  }
}

function requireString(value, label) {
  if (typeof value !== "string" || value.trim() === "") {
    fail(`${label} must be a non-empty string`);
  }
  if (value !== value.trim()) {
    fail(`${label} must not have leading or trailing whitespace`);
  }
  if (value.includes("\0")) {
    fail(`${label} must not contain a null byte`);
  }
}

function requireSingleLine(value, label) {
  requireString(value, label);
  if (/\r|\n/.test(value)) {
    fail(`${label} must stay on one line`);
  }
}

function requireStringList(value, label) {
  if (!Array.isArray(value) || value.length === 0) {
    fail(`${label} must be a non-empty array`);
  }
  value.forEach((item, index) => requireSingleLine(item, `${label}[${index}]`));
}

function requireSafeRelativePath(value, label) {
  requireSingleLine(value, label);
  if (
    path.posix.isAbsolute(value) ||
    value.includes("\\") ||
    path.posix.normalize(value) !== value ||
    value === ".." ||
    value.startsWith("../")
  ) {
    fail(`${label} must be a normalized repository-relative POSIX path`);
  }
}

function resolveInside(root, relativePath, label) {
  const resolved = path.resolve(root, ...relativePath.split("/"));
  const relative = path.relative(root, resolved);
  if (relative === "" || relative.startsWith("..") || path.isAbsolute(relative)) {
    fail(`${label} escapes the repository root`);
  }
  return resolved;
}

function escapeHtmlAttribute(value) {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll('"', "&quot;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function renderBulletList(items) {
  return items.map((item) => `- ${item}`).join("\n");
}

function sourceUrlFor(catalog, source) {
  return `${catalog.repository.url}/blob/${catalog.repository.default_branch}/${source}`;
}

function lessonLink(lesson) {
  return `/course/lessons/${lesson.slug}/`;
}

function adjacentLesson(lesson) {
  return lesson
    ? {
        number: lesson.number,
        slug: lesson.slug,
        title: lesson.title,
        href: lessonLink(lesson),
      }
    : null;
}

async function assertPathExists(repoRoot, relativePath, label, expectedKind) {
  const absolutePath = resolveInside(repoRoot, relativePath, label);
  const rootRealPath = await fs.realpath(repoRoot);
  let currentPath = repoRoot;
  for (const segment of relativePath.split("/")) {
    currentPath = path.join(currentPath, segment);
    let componentStat;
    try {
      componentStat = await fs.lstat(currentPath);
    } catch (error) {
      if (error && error.code === "ENOENT") {
        fail(`${label} does not exist: ${relativePath}`);
      }
      throw error;
    }
    if (componentStat.isSymbolicLink()) {
      fail(`${label} must not contain a symbolic-link component: ${relativePath}`);
    }
  }

  const realPath = await fs.realpath(absolutePath);
  const realPathRelative = path.relative(rootRealPath, realPath);
  if (
    realPathRelative === "" ||
    realPathRelative.startsWith("..") ||
    path.isAbsolute(realPathRelative)
  ) {
    fail(`${label} resolves outside the repository root`);
  }
  let stat;
  try {
    stat = await fs.stat(absolutePath);
  } catch (error) {
    if (error && error.code === "ENOENT") {
      fail(`${label} does not exist: ${relativePath}`);
    }
    throw error;
  }

  if (expectedKind === "file" && !stat.isFile()) {
    fail(`${label} must be a file: ${relativePath}`);
  }
  if (expectedKind === "file-or-directory" && !stat.isFile() && !stat.isDirectory()) {
    fail(`${label} must be a file or directory: ${relativePath}`);
  }
  return absolutePath;
}

async function assertSafeOutputPath(repoRoot, outputPath) {
  const resolvedRoot = path.resolve(repoRoot);
  const resolvedOutput = path.resolve(outputPath);
  const relativePath = path.relative(resolvedRoot, resolvedOutput);
  if (
    relativePath === "" ||
    relativePath.startsWith("..") ||
    path.isAbsolute(relativePath)
  ) {
    throw new Error(`Unsafe generated output escapes the repository: ${resolvedOutput}`);
  }

  let currentPath = resolvedRoot;
  for (const segment of relativePath.split(path.sep)) {
    currentPath = path.join(currentPath, segment);
    let stat;
    try {
      stat = await fs.lstat(currentPath);
    } catch (error) {
      if (error && error.code === "ENOENT") {
        continue;
      }
      throw error;
    }
    if (stat.isSymbolicLink()) {
      throw new Error(
        `Unsafe generated output contains a symbolic-link component: ${relativePath}`,
      );
    }
  }
}

async function findCanonicalLessonSources(repoRoot) {
  const courseRoot = path.join(repoRoot, "course");
  const entries = await fs.readdir(courseRoot, { withFileTypes: true });
  const sources = [];

  for (const entry of entries) {
    if (!entry.isDirectory() || !/^ch\d{2}-[a-z0-9-]+$/.test(entry.name)) {
      continue;
    }
    const source = `course/${entry.name}/README.md`;
    try {
      const stat = await fs.stat(path.join(courseRoot, entry.name, "README.md"));
      if (stat.isFile()) {
        sources.push(source);
      }
    } catch (error) {
      if (!error || error.code !== "ENOENT") {
        throw error;
      }
    }
  }

  return sources.sort();
}

export async function validateCatalog(catalog, { repoRoot = DEFAULT_REPO_ROOT } = {}) {
  requireObject(catalog, "catalog");
  requireSingleLine(catalog.schema_version, "schema_version");
  if (catalog.schema_version !== "1.0") {
    fail(`unsupported schema_version ${catalog.schema_version}`);
  }

  requireObject(catalog.repository, "repository");
  requireSingleLine(catalog.repository.url, "repository.url");
  requireSingleLine(catalog.repository.default_branch, "repository.default_branch");
  if (!/^https:\/\/github\.com\/[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(catalog.repository.url)) {
    fail("repository.url must point to a GitHub repository over HTTPS");
  }
  if (!/^[A-Za-z0-9._/-]+$/.test(catalog.repository.default_branch)) {
    fail("repository.default_branch contains unsupported characters");
  }

  if (!Array.isArray(catalog.phases) || catalog.phases.length === 0) {
    fail("phases must be a non-empty array");
  }
  if (!Array.isArray(catalog.lessons) || catalog.lessons.length !== 10) {
    fail("lessons must contain exactly 10 entries");
  }

  const phaseByTitle = new Map();
  const phaseIds = new Set();
  const phaseLessonNumbers = new Set();
  for (const [phaseIndex, phase] of catalog.phases.entries()) {
    const label = `phases[${phaseIndex}]`;
    requireObject(phase, label);
    requireSingleLine(phase.id, `${label}.id`);
    requireSingleLine(phase.title, `${label}.title`);
    requireSingleLine(phase.question, `${label}.question`);
    if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(phase.id)) {
      fail(`${label}.id must be a lowercase kebab-case identifier`);
    }
    if (phaseIds.has(phase.id)) {
      fail(`phase id is duplicated: ${phase.id}`);
    }
    if (phaseByTitle.has(phase.title)) {
      fail(`phase title is duplicated: ${phase.title}`);
    }
    phaseIds.add(phase.id);
    phaseByTitle.set(phase.title, phase);

    if (!Array.isArray(phase.lesson_numbers) || phase.lesson_numbers.length === 0) {
      fail(`${label}.lesson_numbers must be a non-empty array`);
    }
    for (const number of phase.lesson_numbers) {
      if (!Number.isInteger(number) || number < 1 || number > 10) {
        fail(`${label}.lesson_numbers contains an invalid lesson number`);
      }
      if (phaseLessonNumbers.has(number)) {
        fail(`lesson ${number} belongs to more than one phase`);
      }
      phaseLessonNumbers.add(number);
    }
  }

  const slugs = new Set();
  const titles = new Set();
  const sources = new Set();

  for (const [lessonIndex, lesson] of catalog.lessons.entries()) {
    const label = `lessons[${lessonIndex}]`;
    requireObject(lesson, label);

    const expectedNumber = lessonIndex + 1;
    if (!Number.isInteger(lesson.number) || lesson.number !== expectedNumber) {
      fail(`${label}.number must be ${expectedNumber}; lessons must stay in order`);
    }
    for (const field of REQUIRED_LESSON_STRINGS) {
      requireSingleLine(lesson[field], `${label}.${field}`);
    }
    if (!/^\d{2}-[a-z0-9]+(?:-[a-z0-9]+)*$/.test(lesson.slug)) {
      fail(`${label}.slug must start with a two-digit number and use lowercase kebab-case`);
    }
    if (!lesson.slug.startsWith(String(lesson.number).padStart(2, "0") + "-")) {
      fail(`${label}.slug must start with its two-digit lesson number`);
    }
    if (slugs.has(lesson.slug)) {
      fail(`lesson slug is duplicated: ${lesson.slug}`);
    }
    if (titles.has(lesson.title)) {
      fail(`lesson title is duplicated: ${lesson.title}`);
    }
    if (sources.has(lesson.source)) {
      fail(`lesson source is duplicated: ${lesson.source}`);
    }
    slugs.add(lesson.slug);
    titles.add(lesson.title);
    sources.add(lesson.source);

    const expectedSource = `course/ch${lesson.slug}/README.md`;
    requireSafeRelativePath(lesson.source, `${label}.source`);
    if (lesson.source !== expectedSource) {
      fail(`${label}.source must be ${expectedSource}`);
    }

    const phase = phaseByTitle.get(lesson.phase);
    if (!phase) {
      fail(`${label}.phase does not match a declared phase`);
    }
    if (!phase.lesson_numbers.includes(lesson.number)) {
      fail(`${label}.phase does not include lesson ${lesson.number}`);
    }

    requireObject(lesson.primary_artifact, `${label}.primary_artifact`);
    requireSingleLine(lesson.primary_artifact.label, `${label}.primary_artifact.label`);
    requireSafeRelativePath(lesson.primary_artifact.path, `${label}.primary_artifact.path`);
    requireSingleLine(lesson.primary_artifact.tone, `${label}.primary_artifact.tone`);
    if (!/^[a-z0-9-]+$/.test(lesson.primary_artifact.tone)) {
      fail(`${label}.primary_artifact.tone must be a lowercase identifier`);
    }

    requireObject(lesson.run, `${label}.run`);
    requireSingleLine(lesson.run.kind, `${label}.run.kind`);
    if (lesson.run.command !== null) {
      requireString(lesson.run.command, `${label}.run.command`);
      if (lesson.run.command.includes("```")) {
        fail(`${label}.run.command must not contain a Markdown fence`);
      }
    }
    if (lesson.run.test_command !== undefined) {
      requireString(lesson.run.test_command, `${label}.run.test_command`);
      if (lesson.run.test_command.includes("```")) {
        fail(`${label}.run.test_command must not contain a Markdown fence`);
      }
    }
    requireStringList(lesson.proves, `${label}.proves`);
    requireStringList(lesson.does_not_prove, `${label}.does_not_prove`);

    const sourcePath = await assertPathExists(repoRoot, lesson.source, `${label}.source`, "file");
    await assertPathExists(
      repoRoot,
      lesson.primary_artifact.path,
      `${label}.primary_artifact.path`,
      "file-or-directory",
    );

    const sourceHeading = (await fs.readFile(sourcePath, "utf8")).replace(/^\uFEFF/, "").split(/\r?\n/, 1)[0];
    const expectedHeading = `# 第 ${lesson.number} 课：${lesson.title}`;
    if (sourceHeading !== expectedHeading) {
      fail(`${label}.title must match the canonical source heading ${JSON.stringify(sourceHeading)}`);
    }
  }

  const expectedNumbers = Array.from({ length: 10 }, (_, index) => index + 1);
  const actualPhaseNumbers = [...phaseLessonNumbers].sort((left, right) => left - right);
  if (JSON.stringify(actualPhaseNumbers) !== JSON.stringify(expectedNumbers)) {
    fail("phases must cover every lesson exactly once");
  }

  const canonicalSources = await findCanonicalLessonSources(repoRoot);
  const catalogSources = [...sources].sort();
  if (JSON.stringify(canonicalSources) !== JSON.stringify(catalogSources)) {
    fail("lesson sources must match the ten canonical course/ch*/README.md files");
  }

  return catalog;
}

export async function loadCatalog({
  repoRoot = DEFAULT_REPO_ROOT,
  catalogPath = DEFAULT_CATALOG_PATH,
} = {}) {
  requireSafeRelativePath(catalogPath, "catalogPath");
  const absolutePath = resolveInside(repoRoot, catalogPath, "catalogPath");
  let catalog;
  try {
    catalog = JSON.parse(await fs.readFile(absolutePath, "utf8"));
  } catch (error) {
    if (error instanceof SyntaxError) {
      throw new Error(`Cannot parse ${catalogPath}: ${error.message}`, { cause: error });
    }
    throw error;
  }
  return validateCatalog(catalog, { repoRoot });
}

export function buildGeneratedCatalog(catalog) {
  const lessons = catalog.lessons.map((lesson, index) => ({
    number: lesson.number,
    slug: lesson.slug,
    title: lesson.title,
    phase: lesson.phase,
    question: lesson.question,
    outcome: lesson.outcome,
    source: lesson.source,
    source_url: sourceUrlFor(catalog, lesson.source),
    href: lessonLink(lesson),
    primary_artifact: { ...lesson.primary_artifact },
    evidence_summary: lesson.evidence_summary,
    run: { ...lesson.run },
    mode: lesson.mode,
    learner_test_status: lesson.learner_test_status,
    proves: [...lesson.proves],
    does_not_prove: [...lesson.does_not_prove],
    previous: adjacentLesson(catalog.lessons[index - 1]),
    next: adjacentLesson(catalog.lessons[index + 1]),
  }));

  return {
    schema_version: catalog.schema_version,
    generated_notice: "由 course/catalog.json 生成，请勿直接编辑。",
    phases: catalog.phases.map((phase) => ({ ...phase, lesson_numbers: [...phase.lesson_numbers] })),
    lessons,
  };
}

export function buildLessonPage(catalog, lessonIndex) {
  const lesson = catalog.lessons[lessonIndex];
  if (!lesson) {
    throw new Error(`Cannot render unknown lesson index ${lessonIndex}`);
  }
  const previous = catalog.lessons[lessonIndex - 1] ?? null;
  const next = catalog.lessons[lessonIndex + 1] ?? null;
  const sourceUrl = sourceUrlFor(catalog, lesson.source);
  const navigation = [
    previous ? `[← 第 ${previous.number} 课：${previous.title}](${lessonLink(previous)})` : "← 这是第一课",
    next ? `[第 ${next.number} 课：${next.title} →](${lessonLink(next)})` : "你已经完成全部课程 →",
  ].join("\n\n");
  const runCommand = lesson.run.command
    ? `\n\n\`\`\`bash\n${lesson.run.command}\n\`\`\``
    : "\n\n本课没有需要执行的命令。";
  const testCommand = lesson.run.test_command
    ? `\n\n以下命令只维护课程基线，不验收你的 starter 实现。完成 starter 后，这组测试失败不代表你的答案错误：\n\n\`\`\`bash\n${lesson.run.test_command}\n\`\`\``
    : "";

  return `---
title: ${JSON.stringify(`第 ${lesson.number} 课：${lesson.title}`)}
description: ${JSON.stringify(lesson.outcome)}
prev: false
next: false
---

<!-- 本页由 course/catalog.json 生成。完整内容以源讲义为准。 -->

<LessonMeta :number="${lesson.number}" phase="${escapeHtmlAttribute(lesson.phase)}" mode="${escapeHtmlAttribute(lesson.mode)}" learner-test-status="${escapeHtmlAttribute(lesson.learner_test_status)}" />

# 第 ${lesson.number} 课：${lesson.title}

> ${lesson.question}

<ProgressTracker lesson-id="${escapeHtmlAttribute(lesson.slug)}" lesson-title="${escapeHtmlAttribute(lesson.title)}" />

## 你要解决的问题

${lesson.question}

## 学完你能做到

${lesson.outcome}

## 主要证据

<EvidenceBadge label="fixed/offline 教学参考" tone="${escapeHtmlAttribute(lesson.primary_artifact.tone)}" />

**${lesson.primary_artifact.label}**

${lesson.evidence_summary}

本站只解释这份证据在课程中的作用，不复制原始 artifact 或内部运行材料。

## 运行方式

运行类型：\`${lesson.run.kind}\`${runCommand}${testCommand}

## 这份证据能证明什么

${renderBulletList(lesson.proves)}

## 这份证据不能证明什么

${renderBulletList(lesson.does_not_prove)}

## 阅读完整讲义

摘要页不会复制 canonical lesson 正文。请回到源讲义阅读 Starter、实现任务、测试解释和拓展阅读。

[在 GitHub 阅读第 ${lesson.number} 课完整讲义](${sourceUrl})

## 继续学习

${navigation}
`;
}

function expectedOutputFiles(catalog, websiteRoot) {
  const generatedCatalog = `${JSON.stringify(buildGeneratedCatalog(catalog), null, 2)}\n`;
  const outputs = new Map([
    [path.join(websiteRoot, ...GENERATED_CATALOG_PATH.split("/")), generatedCatalog],
  ]);

  catalog.lessons.forEach((lesson, index) => {
    outputs.set(
      path.join(websiteRoot, ...GENERATED_LESSONS_PATH.split("/"), lesson.slug, "index.md"),
      buildLessonPage(catalog, index),
    );
  });
  return outputs;
}

async function writeIfChanged(filePath, content) {
  try {
    if ((await fs.readFile(filePath, "utf8")) === content) {
      return false;
    }
  } catch (error) {
    if (!error || error.code !== "ENOENT") {
      throw error;
    }
  }
  await fs.mkdir(path.dirname(filePath), { recursive: true });
  await fs.writeFile(filePath, content, "utf8");
  return true;
}

export async function generateCourseSite({
  repoRoot = DEFAULT_REPO_ROOT,
  websiteRoot = path.join(repoRoot, "website"),
  check = false,
} = {}) {
  const catalog = await loadCatalog({ repoRoot });
  const outputs = expectedOutputFiles(catalog, websiteRoot);
  const changed = [];

  for (const [filePath, expectedContent] of outputs) {
    await assertSafeOutputPath(repoRoot, filePath);
    if (check) {
      let actualContent;
      try {
        actualContent = await fs.readFile(filePath, "utf8");
      } catch (error) {
        if (error && error.code === "ENOENT") {
          throw new Error(`Generated course file is missing: ${path.relative(repoRoot, filePath)}`);
        }
        throw error;
      }
      if (actualContent !== expectedContent) {
        throw new Error(`Generated course file is stale: ${path.relative(repoRoot, filePath)}`);
      }
      continue;
    }

    if (await writeIfChanged(filePath, expectedContent)) {
      changed.push(path.relative(repoRoot, filePath));
    }
  }

  return {
    catalog,
    checked: check,
    files: [...outputs.keys()].map((filePath) => path.relative(repoRoot, filePath)),
    changed,
  };
}
