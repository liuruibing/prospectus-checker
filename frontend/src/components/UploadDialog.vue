<template>
  <el-dialog
    title="新增核对任务"
    :visible="visible"
    @update:visible="$emit('update:visible', $event)"
    width="960px"
    top="0"
    append-to-body
    custom-class="upload-dialog batch-upload-dialog"
    :close-on-click-modal="false"
  >
    <batch-upload-panel @submitted="batchTasksCreated" />
    <span slot="footer" class="dialog-footer">
      <el-button @click="$emit('update:visible', false)">取消</el-button>
    </span>
  </el-dialog>
</template>

<script>
import BatchUploadPanel from "./BatchUploadPanel.vue";

export default {
  name: "UploadDialog",
  components: { BatchUploadPanel },
  props: { visible: Boolean },
  methods: {
    batchTasksCreated(result) {
      if (!result.jobs || !result.jobs.length) return;
      this.$emit("update:visible", false);
      this.$emit("batch-submitted", result);
    },
  },
};
</script>
