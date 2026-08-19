<script setup lang="ts">
import { computed, onMounted, ref } from "vue";

const props = defineProps<{
  lessonId: string;
  lessonTitle: string;
}>();

const storageKey = "ses-course-progress:v1";
const completed = ref(false);

function readProgress(): string[] {
  try {
    const raw = window.localStorage.getItem(storageKey);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed)
      ? parsed.filter((value): value is string => typeof value === "string")
      : [];
  } catch {
    return [];
  }
}

function writeProgress(values: string[]) {
  window.localStorage.setItem(storageKey, JSON.stringify(values));
  window.dispatchEvent(new CustomEvent("ses-course-progress-changed"));
}

function toggle() {
  const progress = new Set(readProgress());
  if (progress.has(props.lessonId)) {
    progress.delete(props.lessonId);
  } else {
    progress.add(props.lessonId);
  }
  completed.value = progress.has(props.lessonId);
  writeProgress([...progress].sort());
}

const buttonLabel = computed(() =>
  completed.value ? "标记为未完成" : "完成本课",
);

onMounted(() => {
  completed.value = readProgress().includes(props.lessonId);
});
</script>

<template>
  <aside class="progress-tracker" :data-completed="completed">
    <div>
      <span class="progress-tracker__eyebrow">你的本地进度</span>
      <strong>{{ completed ? "本课已完成" : "完成实验后再打勾" }}</strong>
      <small>进度只保存在当前浏览器。</small>
    </div>
    <button type="button" :aria-pressed="completed" @click="toggle">
      {{ buttonLabel }}
    </button>
  </aside>
</template>
