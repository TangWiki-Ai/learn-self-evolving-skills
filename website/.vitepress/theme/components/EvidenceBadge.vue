<script setup lang="ts">
withDefaults(
  defineProps<{
    label: string;
    tone?: "fixed" | "protected" | "pending" | "neutral";
  }>(),
  { tone: "neutral" },
);
</script>

<template>
  <span class="evidence-badge" :data-tone="tone">{{ label }}</span>
</template>
