<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";

import catalog from "../../generated/course-catalog.json";

const storageKey = "ses-course-progress:v1";
const completedCount = ref(0);
const total = catalog.lessons.length;
const lessonIds = new Set(catalog.lessons.map((lesson) => lesson.slug));

function update() {
  try {
    const raw = window.localStorage.getItem(storageKey);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    const values = Array.isArray(parsed)
      ? parsed.filter((value): value is string => typeof value === "string" && lessonIds.has(value))
      : [];
    completedCount.value = new Set(values).size;
  } catch {
    completedCount.value = 0;
  }
}

const percentage = computed(() =>
  total === 0 ? 0 : Math.round((completedCount.value / total) * 100),
);

onMounted(() => {
  update();
  window.addEventListener("ses-course-progress-changed", update);
  window.addEventListener("storage", update);
});

onBeforeUnmount(() => {
  window.removeEventListener("ses-course-progress-changed", update);
  window.removeEventListener("storage", update);
});
</script>

<template>
  <section class="progress-summary" aria-label="课程进度">
    <div class="progress-summary__copy">
      <span>你的本地进度</span>
      <strong>{{ completedCount }} / {{ total }} 课</strong>
    </div>
    <div
      class="progress-summary__bar"
      role="progressbar"
      aria-label="课程完成比例"
      :aria-valuenow="percentage"
      aria-valuemin="0"
      aria-valuemax="100"
    >
      <span :style="{ width: `${percentage}%` }" />
    </div>
    <small>只保存在当前浏览器，不需要账号。</small>
  </section>
</template>
