<script setup lang="ts">
defineProps<{
  number: number | string;
  phase: string;
  mode: string;
  learnerTestStatus: string;
}>();
</script>

<template>
  <dl class="lesson-meta" aria-label="课程元信息">
    <div>
      <dt>课程</dt>
      <dd>{{ String(number).padStart(2, "0") }}</dd>
    </div>
    <div>
      <dt>阶段</dt>
      <dd>{{ phase }}</dd>
    </div>
    <div>
      <dt>运行方式</dt>
      <dd>{{ mode }}</dd>
    </div>
    <div>
      <dt>测试状态</dt>
      <dd>{{ learnerTestStatus }}</dd>
    </div>
  </dl>
</template>
