<template>
  <el-dialog title="新增核对任务" :visible="visible" @update:visible="$emit('update:visible', $event)" width="720px" top="8vh" append-to-body custom-class="upload-dialog" :close-on-click-modal="false">
    <div class="upload-mode">
      <button type="button" :class="{ active: workflowType === 'investment' }" @click="workflowType = 'investment'">投资组合核对</button>
      <button type="button" :class="{ active: workflowType === 'summary' }" @click="workflowType = 'summary'">产品资料概要核对</button>
    </div>
    <div v-if="workflowType === 'investment'" class="upload-pair-grid">
      <label v-for="role in investmentRoles" :key="role.key" class="upload-card" :class="{ selected: files[role.key], dragging: dragTarget === role.key }" @dragover.prevent="dragTarget = role.key" @dragleave.prevent="dragTarget = null" @drop.prevent="handleDrop(role.key, $event)">
        <input :ref="role.key + 'Input'" type="file" accept="application/pdf,.pdf" @change="handleFileChange(role.key, $event)">
        <span class="upload-file-icon" :class="role.key === 'prospectus' ? 'prospectus-type' : 'report-type'">{{ role.icon }}</span>
        <span class="upload-file-label">{{ role.label }}</span><strong>{{ files[role.key] ? files[role.key].name : '选择 PDF 文件' }}</strong>
        <small v-if="files[role.key]">{{ fileSize(files[role.key].size) }}</small><i v-if="files[role.key]" class="el-icon-circle-check upload-check" />
      </label>
    </div>
    <div v-else class="upload-summary-grid">
      <label v-for="role in summaryRequiredRoles" :key="role.key" class="upload-card" :class="{ selected: files[role.key], dragging: dragTarget === role.key }" @dragover.prevent="dragTarget = role.key" @dragleave.prevent="dragTarget = null" @drop.prevent="handleDrop(role.key, $event)">
        <input :ref="role.key + 'Input'" type="file" accept="application/pdf,.pdf" @change="handleFileChange(role.key, $event)">
        <span class="upload-file-icon" :class="role.key === 'product_summary' ? 'prospectus-type' : 'report-type'">{{ role.icon }}</span>
        <span class="upload-file-label">{{ role.label }}</span><strong>{{ files[role.key] ? files[role.key].name : '必需文件，选择 PDF' }}</strong>
        <small v-if="files[role.key]">{{ fileSize(files[role.key].size) }}</small><i v-if="files[role.key]" class="el-icon-circle-check upload-check" />
        <button v-if="files[role.key]" type="button" class="upload-remove" @click.prevent.stop="clearFile(role.key)">移除</button>
      </label>
    </div>
    <div v-if="workflowType === 'summary'" class="upload-supplement">
      <div><strong>补充依据</strong><span>定期报告未披露的指定指标可使用招募说明书</span></div>
      <label class="upload-supplement-file" :class="{ selected: files.prospectus, dragging: dragTarget === 'prospectus' }" @dragover.prevent="dragTarget = 'prospectus'" @dragleave.prevent="dragTarget = null" @drop.prevent="handleDrop('prospectus', $event)">
        <input ref="prospectusInput" type="file" accept="application/pdf,.pdf" @change="handleFileChange('prospectus', $event)">
        <span>{{ files.prospectus ? files.prospectus.name : '选择招募说明书 PDF（可选）' }}</span>
        <button v-if="files.prospectus" type="button" @click.prevent.stop="clearFile('prospectus')">移除</button>
      </label>
    </div>
    <div v-if="loading" class="loading-status" role="status"><i class="el-icon-loading" /> {{ progressText }}</div>
    <el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false" show-icon class="upload-error" />
    <span slot="footer" class="dialog-footer"><el-button @click="$emit('update:visible', false)">取消</el-button><el-button type="primary" :loading="loading" :disabled="!canCompare" @click="compareDocuments">{{ loading ? '正在核对' : '创建核对任务' }} <i v-if="!loading" class="el-icon-right" /></el-button></span>
  </el-dialog>
</template>

<script>
import axios from "axios";
const EMPTY_FILES = () => ({ prospectus: null, periodic_report: null, product_summary: null });
export default {
  name: "UploadDialog",
  props: { visible: Boolean },
  data() { return {
    workflowType: "investment", files: EMPTY_FILES(), loading: false, errorMessage: "", dragTarget: null, progressText: "正在提交核对任务",
    investmentRoles: [{ key: "prospectus", icon: "招", label: "招募说明书" }, { key: "periodic_report", icon: "报", label: "定期报告" }],
    summaryRequiredRoles: [{ key: "product_summary", icon: "概", label: "产品资料概要" }, { key: "periodic_report", icon: "报", label: "定期报告" }],
  }; },
  computed: { canCompare() {
    if (this.loading) return false;
    if (this.workflowType === "investment") return !!(this.files.prospectus && this.files.periodic_report);
    return !!(this.files.product_summary && this.files.periodic_report);
  } },
  watch: { visible(value) { if (value && !this.loading) { this.files = EMPTY_FILES(); this.errorMessage = ""; } } },
  methods: {
    fileSize(bytes) { return (bytes / 1024 / 1024).toFixed(2) + " MB"; },
    setFile(role, file) {
      if (!file) return;
      if (!/\.pdf$/i.test(file.name) || (file.type && file.type !== "application/pdf")) { this.errorMessage = "仅支持 PDF 文件。"; return; }
      if (file.size > 40 * 1024 * 1024) { this.errorMessage = "单个 PDF 不能超过 40 MiB。"; return; }
      this.$set(this.files, role, file); this.errorMessage = "";
    },
    clearFile(role) { this.$set(this.files, role, null); this.errorMessage = ""; },
    handleFileChange(role, event) { this.setFile(role, event.target.files && event.target.files[0]); event.target.value = ""; },
    handleDrop(role, event) { this.dragTarget = null; this.setFile(role, event.dataTransfer && event.dataTransfer.files[0]); },
    async compareDocuments() {
      if (!this.canCompare) return;
      this.loading = true; this.errorMessage = "";
      const data = new FormData();
      let endpoint = "/api/jobs";
      if (this.workflowType === "investment") {
        data.append("prospectus", this.files.prospectus); data.append("periodic_report", this.files.periodic_report);
      } else {
        endpoint = "/api/product-summary/jobs";
        data.append("product_summary", this.files.product_summary);
        data.append("periodic_report", this.files.periodic_report);
        if (this.files.prospectus) data.append("prospectus", this.files.prospectus);
      }
      try {
        const response = await axios.post(endpoint, data);
        this.$emit("update:visible", false); this.$emit("submitted", response.data);
      } catch (error) {
        this.errorMessage = error.response && error.response.data && error.response.data.detail || "提交核对任务失败，请稍后重试。";
      } finally { this.loading = false; }
    },
  },
};
</script>
