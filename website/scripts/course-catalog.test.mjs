import assert from "node:assert/strict";
import { promises as fs } from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {
  DEFAULT_REPO_ROOT,
  buildGeneratedCatalog,
  generateCourseSite,
  loadCatalog,
  validateCatalog,
} from "./course-catalog.mjs";

async function readRepositoryCatalog() {
  return JSON.parse(await fs.readFile(path.join(DEFAULT_REPO_ROOT, "course/catalog.json"), "utf8"));
}

async function makeFixtureRepo(t) {
  const repoRoot = await fs.mkdtemp(path.join(os.tmpdir(), "ses-course-catalog-"));
  t.after(() => fs.rm(repoRoot, { recursive: true, force: true }));
  const catalog = await readRepositoryCatalog();

  await fs.mkdir(path.join(repoRoot, "course"), { recursive: true });
  for (const lesson of catalog.lessons) {
    const sourcePath = path.join(repoRoot, ...lesson.source.split("/"));
    await fs.mkdir(path.dirname(sourcePath), { recursive: true });
    await fs.writeFile(sourcePath, `# 第 ${lesson.number} 课：${lesson.title}\n\n不应进入摘要页的源正文。\n`);

    const artifactPath = path.join(repoRoot, ...lesson.primary_artifact.path.split("/"));
    await fs.mkdir(path.dirname(artifactPath), { recursive: true });
    await fs.writeFile(artifactPath, "{}\n");
  }
  await fs.writeFile(path.join(repoRoot, "course/catalog.json"), `${JSON.stringify(catalog, null, 2)}\n`);
  return { catalog, repoRoot };
}

test("repository catalog validates ten ordered, unique canonical lessons", async () => {
  const catalog = await loadCatalog();
  assert.equal(catalog.lessons.length, 10);
  assert.deepEqual(
    catalog.lessons.map((lesson) => lesson.number),
    [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
  );
  assert.equal(new Set(catalog.lessons.map((lesson) => lesson.slug)).size, 10);
});

test("validation rejects duplicate or out-of-order lessons", async (t) => {
  const { catalog, repoRoot } = await makeFixtureRepo(t);
  catalog.lessons[1].number = 1;
  await assert.rejects(validateCatalog(catalog, { repoRoot }), /must be 2; lessons must stay in order/);
});

test("validation rejects a symbolic-link source component that escapes the repository", async (t) => {
  const { catalog, repoRoot } = await makeFixtureRepo(t);
  const sourcePath = path.join(repoRoot, ...catalog.lessons[0].source.split("/"));
  const outsidePath = path.join(path.dirname(repoRoot), `${path.basename(repoRoot)}-outside.md`);
  t.after(() => fs.rm(outsidePath, { force: true }));
  await fs.writeFile(outsidePath, `# 第 1 课：${catalog.lessons[0].title}\n`);
  await fs.rm(sourcePath);
  await fs.symlink(outsidePath, sourcePath);

  await assert.rejects(
    validateCatalog(catalog, { repoRoot }),
    /must not contain a symbolic-link component/,
  );
});

test("generation is stable and pages contain the learner-facing contract", async (t) => {
  const { repoRoot } = await makeFixtureRepo(t);
  const websiteRoot = path.join(repoRoot, "website-output");

  const first = await generateCourseSite({ repoRoot, websiteRoot });
  const second = await generateCourseSite({ repoRoot, websiteRoot });

  assert.equal(first.files.length, 11);
  assert.equal(first.changed.length, 11);
  assert.deepEqual(second.changed, []);
  await generateCourseSite({ repoRoot, websiteRoot, check: true });

  const firstPage = await fs.readFile(
    path.join(websiteRoot, "course/lessons/01-see-the-difference/index.md"),
    "utf8",
  );
  assert.match(firstPage, /<LessonMeta :number="1" phase="看见与判分"/);
  assert.match(firstPage, /<ProgressTracker lesson-id="01-see-the-difference"/);
  assert.match(firstPage, /<EvidenceBadge label="fixed\/offline 教学参考" tone="fixed"/);
  assert.match(firstPage, /## 你要解决的问题/);
  assert.match(firstPage, /## 学完你能做到/);
  assert.match(firstPage, /## 这份证据能证明什么/);
  assert.match(firstPage, /## 这份证据不能证明什么/);
  assert.match(firstPage, /github\.com\/TangWiki-Ai\/learn-self-evolving-skills\/blob\/main\/course/);
  assert.match(firstPage, /第 2 课：从终态给一个 case 判分/);
  assert.doesNotMatch(firstPage, /不应进入摘要页的源正文/);
  assert.doesNotMatch(firstPage, /comparison-artifact\.json/);

  const lastPage = await fs.readFile(
    path.join(websiteRoot, "course/lessons/10-auto-evolve-and-portfolio/index.md"),
    "utf8",
  );
  assert.match(lastPage, /第 9 课：门控并治理 Skill 版本/);
  assert.match(lastPage, /你已经完成全部课程/);

  const generated = JSON.parse(
    await fs.readFile(path.join(websiteRoot, ".vitepress/generated/course-catalog.json"), "utf8"),
  );
  assert.deepEqual(generated, buildGeneratedCatalog(await loadCatalog({ repoRoot })));
  assert.equal(generated.lessons[0].previous, null);
  assert.equal(generated.lessons[9].next, null);
});

test("check mode reports a stale generated page", async (t) => {
  const { repoRoot } = await makeFixtureRepo(t);
  const websiteRoot = path.join(repoRoot, "website-output");
  await generateCourseSite({ repoRoot, websiteRoot });
  await fs.appendFile(
    path.join(websiteRoot, "course/lessons/01-see-the-difference/index.md"),
    "stale\n",
  );

  await assert.rejects(
    generateCourseSite({ repoRoot, websiteRoot, check: true }),
    /Generated course file is stale/,
  );
});

test("generation rejects an output path outside the repository", async (t) => {
  const { repoRoot } = await makeFixtureRepo(t);
  const outsideRoot = await fs.mkdtemp(path.join(os.tmpdir(), "ses-course-output-outside-"));
  t.after(() => fs.rm(outsideRoot, { recursive: true, force: true }));

  await assert.rejects(
    generateCourseSite({ repoRoot, websiteRoot: outsideRoot }),
    /Unsafe generated output escapes the repository/,
  );
});

test("generation rejects a symbolic-link output component", async (t) => {
  const { repoRoot } = await makeFixtureRepo(t);
  const outsideRoot = await fs.mkdtemp(path.join(os.tmpdir(), "ses-course-output-target-"));
  t.after(() => fs.rm(outsideRoot, { recursive: true, force: true }));
  const websiteRoot = path.join(repoRoot, "website-output");
  await fs.symlink(outsideRoot, websiteRoot);

  await assert.rejects(
    generateCourseSite({ repoRoot, websiteRoot }),
    /Unsafe generated output contains a symbolic-link component/,
  );
});
