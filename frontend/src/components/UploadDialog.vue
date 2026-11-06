<template>
    <el-dialog
      title="上传核对文档"
      :visible="visible"
      @update:visible="$emit('update:visible', $event)"
      width="720px"
      top="8vh"
      append-to-body
      custom-class="upload-dialog"
      :close-on-click-modal="false"
    >
      <div class="upload-pair-grid">
        <label
          class="upload-card"
          :class="{ selected: files.prospectus, dragging: dragTarget === 'prospectus' }"
          @dragover.prevent="dragTarget = 'prospectus'"
          @dragleave.prevent="dragTarget = null"
          @drop.prevent="handleDrop('prospectus', $event)"
        >
          <input ref="prospectusInput" type="file" accept="application/pdf,.pdf" @change="handleFileChange('prospectus', $event)">
          <span class="upload-file-icon prospectus-type">招</span>
          <span class="upload-file-label">招募说明书</span>
          <strong>{{ files.prospectus ? files.prospectus.name : '选择 PDF 文件' }}</strong>
          <small v-if="files.prospectus">{{ fileSize(files.prospectus.size) }}</small>
          <i v-if="files.prospectus" class="el-icon-circle-check upload-check" />
        </label>
        <div class="upload-vs">VS</div>
        <label
          class="upload-card"
          :class="{ selected: files.periodic_report, dragging: dragTarget === 'periodic_report' }"
          @dragover.prevent="dragTarget = 'periodic_report'"
          @dragleave.prevent="dragTarget = null"
          @drop.prevent="handleDrop('periodic_report', $event)"
        >
          <input ref="reportInput" type="file" accept="application/pdf,.pdf" @change="handleFileChange('periodic_report', $event)">
          <span class="upload-file-icon report-type">报</span>
          <span class="upload-file-label">定期报告</span>
          <strong>{{ files.periodic_report ? files.periodic_report.name : '选择 PDF 文件' }}</strong>
          <small v-if="files.periodic_report">{{ fileSize(files.periodic_report.size) }}</small>
          <i v-if="files.periodic_report" class="el-icon-circle-check upload-check" />
        </label>
      </div>
      <div v-if="loading" class="loading-status" role="status"><i class="el-icon-loading" /> {{ progressText }}</div>
      <el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false" show-icon class="upload-error" />
      <span slot="footer" class="dialog-footer">
        <el-button @click="$emit('update:visible', false)">取消</el-button>
        <el-button type="primary" :loading="loading" :disabled="!canCompare" @click="compareDocuments">
          {{ loading ? '正在核对' : '开始核对' }} <i v-if="!loading" class="el-icon-right" />
        </el-button>
      </span>
    </el-dialog>
</template>

<script>
import axios from "axios";
export default {
  name: "UploadDialog",
  props: { visible: Boolean },
  data() { return { files: { prospectus: null, periodic_report: null }, loading: false, errorMessage: "", dragTarget: null, progressText: "正在提交核对任务" }; },
  computed: { canCompare() { return !!(this.files.prospectus && this.files.periodic_report && !this.loading); } },
  watch: { visible(value) { if (value && !this.loading) { this.files = { prospectus: null, periodic_report: null }; this.errorMessage = ""; } } },
  methods: {
    fileSize(bytes) { return (bytes / 1024 / 1024).toFixed(2) + " MB"; },
    setFile(role, file) {
      if (!file) return;
      if (!/\.pdf$/i.test(file.name) || (file.type && file.type !== "application/pdf")) { this.errorMessage = "仅支持 PDF 文件。"; return; }
      if (file.size > 40 * 1024 * 1024) { this.errorMessage = "单个 PDF 不能超过 40 MiB。"; return; }
      this.files[role] = file;
      this.errorMessage = "";
    },
    handleFileChange(role, event) { this.setFile(role, event.target.files && event.target.files[0]); event.target.value = ""; },
    handleDrop(role, event) { this.dragTarget = null; this.setFile(role, event.dataTransfer && event.dataTransfer.files[0]); },
    async compareDocuments() {
      if (!this.canCompare) return;
      this.loading = true;
      this.errorMessage = "";
      const data = new FormData();
      data.append("prospectus", this.files.prospectus);
      data.append("periodic_report", this.files.periodic_report);
      try {
        const response = await axios.post("/api/jobs", data);
        this.$emit("update:visible", false);
        this.$emit("submitted", response.data);
      } catch (error) {
        this.errorMessage = error.response && error.response.data && error.response.data.detail || "提交核对任务失败，请稍后重试。";
      } finally { this.loading = false; }
    },
  },
};
</script>
