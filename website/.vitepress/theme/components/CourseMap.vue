<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
import { withBase } from "vitepress";

import catalog from "../../generated/course-catalog.json";

type CatalogLesson = (typeof catalog.lessons)[number];

const storageKey = "ses-course-progress:v1";
const completed = ref(new Set<string>());

function updateProgress() {
  try {
    const raw = window.localStorage.getItem(storageKey);
    completed.value = new Set(raw ? (JSON.parse(raw) as string[]) : []);
  } catch {
    completed.value = new Set();
  }
}

function lessonsForPhase(phase: (typeof catalog.phases)[number]): CatalogLesson[] {
  return catalog.lessons.filter(
    (lesson) =>
      lesson.phase === phase.id ||
      lesson.phase === phase.title ||
      phase.lesson_numbers.includes(lesson.number),
  );
}

onMounted(() => {
  updateProgress();
  window.addEventListener("ses-course-progress-changed", updateProgress);
  window.addEventListener("storage", updateProgress);
});

onBeforeUnmount(() => {
  window.removeEventListener("ses-course-progress-changed", updateProgress);
  window.removeEventListener("storage", updateProgress);
});
</script>

<template>
  <div class="course-map">
    <section v-for="(phase, phaseIndex) in catalog.phases" :key="phase.id" class="course-phase">
      <header class="course-phase__header">
        <span>阶段 {{ phaseIndex + 1 }}</span>
        <h2>{{ phase.title }}</h2>
        <p>{{ phase.question }}</p>
      </header>
      <div class="course-phase__lessons">
        <a
          v-for="lesson in lessonsForPhase(phase)"
          :key="lesson.slug"
          class="lesson-card"
          :class="{ 'is-complete': completed.has(lesson.slug) }"
          :href="withBase(`/course/lessons/${lesson.slug}/`)"
        >
          <span class="lesson-card__number">{{ String(lesson.number).padStart(2, "0") }}</span>
          <span class="lesson-card__content">
            <strong>{{ lesson.title }}</strong>
            <small class="lesson-card__question">{{ lesson.question }}</small>
            <small class="lesson-card__outcome">完成后：{{ lesson.outcome }}</small>
          </span>
          <span class="lesson-card__state" aria-hidden="true">
            {{ completed.has(lesson.slug) ? "✓" : "→" }}
          </span>
        </a>
      </div>
    </section>
  </div>
</template>
