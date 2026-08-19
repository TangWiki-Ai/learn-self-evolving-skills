import DefaultTheme from "vitepress/theme";
import type { Theme } from "vitepress";

import CourseMap from "./components/CourseMap.vue";
import EvidenceBadge from "./components/EvidenceBadge.vue";
import LessonMeta from "./components/LessonMeta.vue";
import ProgressSummary from "./components/ProgressSummary.vue";
import ProgressTracker from "./components/ProgressTracker.vue";
import ReportCard from "./components/ReportCard.vue";
import "./style.css";

export default {
  extends: DefaultTheme,
  enhanceApp({ app }) {
    app.component("CourseMap", CourseMap);
    app.component("EvidenceBadge", EvidenceBadge);
    app.component("LessonMeta", LessonMeta);
    app.component("ProgressSummary", ProgressSummary);
    app.component("ProgressTracker", ProgressTracker);
    app.component("ReportCard", ReportCard);
  },
} satisfies Theme;
