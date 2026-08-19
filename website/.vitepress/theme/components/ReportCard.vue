<script setup lang="ts">
withDefaults(
  defineProps<{
    level: "L1" | "L2" | "L3";
    title: string;
    question: string;
    summary: string;
    href?: string;
    relatedLessons?: Array<number | string>;
  }>(),
  { href: undefined, relatedLessons: () => [] },
);
</script>

<template>
  <article class="report-card">
    <div class="report-card__level">{{ level }}</div>
    <div>
      <h3>{{ title }}</h3>
      <p class="report-card__question">{{ question }}</p>
      <p>{{ summary }}</p>
      <p v-if="relatedLessons.length" class="report-card__lessons">
        相关课程：{{ relatedLessons.map((lesson) => `第 ${lesson} 课`).join("、") }}
      </p>
      <a v-if="href" :href="href">查看学习者摘要 →</a>
    </div>
  </article>
</template>
