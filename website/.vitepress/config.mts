import { readFileSync } from "node:fs";

import { defineConfig } from "vitepress";
import { withMermaid } from "vitepress-plugin-mermaid";

type Lesson = {
  number: number;
  slug: string;
  title: string;
  phase: string;
};

type Catalog = {
  lessons: Lesson[];
};

const catalogUrl = new URL("../../course/catalog.json", import.meta.url);
const catalog = JSON.parse(readFileSync(catalogUrl, "utf8")) as Catalog;
const base = process.env.DOCS_BASE_PATH || "/";

const lessonItems = catalog.lessons.map((lesson) => ({
  text: `${String(lesson.number).padStart(2, "0")} · ${lesson.title}`,
  link: `/course/lessons/${lesson.slug}/`,
}));

export default withMermaid(
  defineConfig({
    lang: "zh-CN",
    title: "Learn Self-Evolving Skills",
    description: "从可重放评测到有界自动进化的项目制 Python 课程。",
    base,
    cleanUrls: true,
    lastUpdated: true,
    head: [
      ["link", { rel: "icon", type: "image/svg+xml", href: `${base}mark.svg` }],
      ["meta", { name: "theme-color", content: "#0e766e" }],
    ],
    markdown: {
      lineNumbers: true,
    },
    sitemap: {
      hostname: "https://tangwiki-ai.github.io/learn-self-evolving-skills/",
    },
    themeConfig: {
      logo: "/mark.svg",
      siteTitle: "Self-Evolving Skills",
      nav: [
        { text: "开始学习", link: "/start/" },
        { text: "十课", link: "/course/" },
        { text: "报告", link: "/reports/" },
        { text: "证据与边界", link: "/evidence/" },
      ],
      sidebar: [
        {
          text: "开始",
          items: [
            { text: "课程首页", link: "/" },
            { text: "先跑出第一份证据", link: "/start/" },
            { text: "四阶段课程地图", link: "/course/" },
          ],
        },
        {
          text: "十课",
          collapsed: false,
          items: lessonItems,
        },
        {
          text: "理解证据",
          items: [
            { text: "报告总览", link: "/reports/" },
            { text: "L1 · 单轮发生了什么", link: "/reports/level-1" },
            { text: "L2 · 前后发生了什么变化", link: "/reports/level-2" },
            { text: "L3 · 版本怎样演进", link: "/reports/level-3" },
            { text: "证据与边界", link: "/evidence/" },
            { text: "排障", link: "/troubleshooting/" },
          ],
        },
      ],
      socialLinks: [
        {
          icon: "github",
          link: "https://github.com/TangWiki-Ai/learn-self-evolving-skills",
        },
      ],
      search: {
        provider: "local",
        options: {
          translations: {
            button: {
              buttonText: "搜索课程",
              buttonAriaLabel: "搜索课程",
            },
            modal: {
              noResultsText: "没有找到相关内容",
              resetButtonTitle: "清除查询",
              footer: {
                selectText: "选择",
                navigateText: "切换",
                closeText: "关闭",
              },
            },
          },
        },
      },
      outline: {
        level: [2, 3],
        label: "本页内容",
      },
      docFooter: {
        prev: "上一页",
        next: "下一页",
      },
      lastUpdated: {
        text: "最后更新",
        formatOptions: {
          dateStyle: "medium",
          timeStyle: "short",
        },
      },
      footer: {
        message: "用证据判断改进，用边界保护实验。",
        copyright: "Apache-2.0 · TangWiki-Ai",
      },
    },
    mermaid: {
      theme: "base",
      themeVariables: {
        primaryColor: "#dff5f0",
        primaryTextColor: "#123c3a",
        primaryBorderColor: "#0e766e",
        lineColor: "#64748b",
        secondaryColor: "#fff4de",
        tertiaryColor: "#f4f7f7",
      },
    },
  }),
);
