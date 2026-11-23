<template>
  <div class="workbench-page history-workbench">
    <app-topbar />
    <main class="workbench-main" v-loading="loading">
      <nav class="task-detail-nav">
        <router-link :to="returnPath"><i class="el-icon-arrow-left" /> 核对任务</router-link>
        <span class="detail-nav-divider" />
        <el-tag size="small" :type="job && job.status === 'failed' ? 'danger' : 'success'">{{ jobStatus }}</el-tag>
        <strong class="history-task-title">历史专项任务（旧版）</strong>
        <span v-if="result" class="history-coverage">目标 {{ result.coverage.target_rows }} 项，已确认 {{ confirmedRows }} 项，待复核 {{ result.coverage.unresolved_rows }} 项</span>
      </nav>
      <el-alert
        title="这是旧版业绩专项任务，仅保留历史结果查阅。重新核对请使用招募说明书核对任务。"
        type="info"
        :closable="false"
        show-icon
      />
      <el-alert v-if="error || (job && job.error)" :title="error || job.error" type="error" :closable="false" show-icon />
      <div v-if="job && job.status !== 'completed' && job.status !== 'failed'" class="task-execution-state">
        <i class="el-icon-loading" /><strong>{{ job.message }}</strong>
      </div>
      <section v-if="result" class="history-review-layout">
        <aside class="history-findings">
          <header><strong>{{ moduleTitle }}</strong><el-tag size="mini" :type="tagType(moduleStatus)">{{ statusLabel(moduleStatus) }}</el-tag></header>
          <button v-for="(row, index) in rows" :key="index" type="button" class="history-result-row" :class="[{ selected: index === selectedIndex }, statusClass(row.status)]" @click="selectedIndex = index">
            <span class="history-row-heading"><strong>{{ rowLabel(row) }}</strong><el-tag size="mini" :type="tagType(row.status)">{{ statusLabel(row.status) }}</el-tag></span>
            <span v-for="field in fieldEntries(row)" :key="field.key" class="history-field-line">
              <small>{{ field.label }}</small><span>{{ value(field.target) }} <i class="el-icon-right" /> {{ value(field.reference) }}</span>
            </span>
            <small v-if="row.review_message" class="history-review-reason">{{ row.review_message }}</small>
          </button>
          <el-empty v-if="!rows.length" description="没有可比较的年度行" :image-size="64" />
        </aside>
        <section class="history-viewers">
          <pdf-viewer role-name="prospectus" title="招募说明书" :document-info="targetDocument" :evidences="targetEvidence" />
          <div class="history-reference-pane">
            <div class="history-reference-select" v-if="sourceOptions.length > 1">
              <el-select v-model="selectedSourceRole" size="mini" aria-label="选择对应历史报告">
                <el-option v-for="source in sourceOptions" :key="source.role" :label="source.filename" :value="source.role" />
              </el-select>
            </div>
            <pdf-viewer v-if="referenceDocument" :key="selectedSourceRole" :role-name="selectedSourceRole" title="历史定期报告" :document-info="referenceDocument" :evidences="referenceEvidence" />
            <el-empty v-else description="尚无对应来源报告" />
          </div>
        </section>
      </section>
    </main>
  </div>
</template>

<script>
import axios from "axios";
import AppTopbar from "../components/AppTopbar.vue";
import PdfViewer from "../components/PdfViewer.vue";

export default {
  name: "HistoricalPerformanceWorkbench",
  components: { AppTopbar, PdfViewer },
  props: { taskId: { type: String, required: true } },
  data() { return { job: null, result: null, loading: true, error: "", selectedIndex: 0, selectedSourceRole: "", timer: null }; },
  computed: {
    returnPath() { return this.$route.query.returnTo || "/"; },
    jobStatus() { return this.job && this.job.status === "completed" ? "已完成" : this.job && this.job.status === "failed" ? "失败" : "核对中"; },
    module() { return this.result && this.result.modules && this.result.modules[0] || null; },
    moduleTitle() { return this.module && this.module.title || "逐年度业绩"; },
    moduleStatus() { return this.module && this.module.status || this.result && this.result.status || "UNRESOLVED"; },
    rows() { return this.module && this.module.rows || []; },
    confirmedRows() { return this.result ? this.result.coverage.matched_rows + this.result.coverage.mismatched_rows : 0; },
    currentRow() { return this.rows[this.selectedIndex] || null; },
    targetDocument() { return this.result && this.result.documents.prospectus || null; },
    sourceOptions() {
      const matchedSources = this.currentRow ? (this.currentRow.sources || []).map((source) => ({
        role: source.role, filename: source.filename || (this.result.documents[source.role] || {}).filename,
      })) : [];
      if (matchedSources.length) return matchedSources;
      return Object.entries((this.result && this.result.documents) || {})
        .filter(([role, document]) => role !== "prospectus" && document && document.filename)
        .map(([role, document]) => ({ role, filename: document.filename }));
    },
    referenceDocument() { return this.result && this.result.documents[this.selectedSourceRole] || null; },
    targetEvidence() { return this.currentRow ? this.viewerEvidence(this.currentRow.target, this.currentRow.status) : []; },
    referenceEvidence() {
      const source = this.currentRow && (this.currentRow.sources || []).find((item) => item.role === this.selectedSourceRole);
      return source ? this.viewerEvidence(source.row, this.currentRow.status) : [];
    },
  },
  watch: {
    taskId: { immediate: true, handler() { this.load(); } },
    currentRow() { this.chooseSource(); },
    sourceOptions() { this.chooseSource(); },
  },
  beforeDestroy() { if (this.timer) clearInterval(this.timer); },
  methods: {
    async load() {
      this.loading = true;
      try {
        const response = await axios.get("/api/jobs/" + encodeURIComponent(this.taskId));
        this.job = response.data;
        this.result = response.data.result || null;
        this.error = "";
        if (this.job.status === "queued" || this.job.status === "running") this.timer = setTimeout(this.load, 1800);
      } catch (error) {
        this.error = error.message || "任务读取失败。";
      } finally { this.loading = false; }
    },
    chooseSource() { if (this.sourceOptions.length && !this.sourceOptions.some((item) => item.role === this.selectedSourceRole)) this.selectedSourceRole = this.sourceOptions[0].role; },
    rowLabel(row) { return (row.identity || []).filter(Boolean).join(" / ") || "年度行待复核"; },
    fieldEntries(row) { return Object.keys(row.fields || {}).map((key) => ({ key, ...row.fields[key] })); },
    value(item) { return item && item.raw_text || "—"; },
    statusClass(status) { return String(status || "").toLowerCase(); },
    statusLabel(status) { return ({ MATCH: "一致", MISMATCH: "存在差异", UNRESOLVED: "待复核" })[status] || "待复核"; },
    tagType(status) { return status === "MATCH" ? "success" : status === "MISMATCH" ? "danger" : "warning"; },
    viewerEvidence(row, status) {
      if (!row) return [];
      const values = [row.row_evidence, ...Object.values(row.fields || {}).map((item) => item && item.evidence)].filter(Boolean);
      return values.map((item, index) => ({ ...item, id: index + 1, difference_status: status || "UNRESOLVED" }));
    },
  },
};
</script>

<style scoped>
.history-task-title { margin-left: 10px; }
.history-coverage { margin-left: auto; color: var(--gray-11, #60646c); }
.history-review-layout { display: grid; grid-template-columns: minmax(280px, 360px) minmax(0, 1fr); gap: 12px; height: calc(100vh - 156px); min-height: 520px; }
.history-findings { overflow: auto; border: .5px solid var(--gray-6, #d9d9e0); background: var(--color-panel, #fff); }
.history-findings header { display: flex; justify-content: space-between; align-items: center; padding: 14px; border-bottom: .5px solid var(--gray-6, #d9d9e0); }
.history-result-row { display: grid; width: 100%; gap: 8px; padding: 12px; text-align: left; border: 0; border-bottom: .5px solid var(--gray-5, #e5e5eb); background: transparent; color: inherit; cursor: pointer; }
.history-result-row.selected { background: var(--blue-3, #e6f4fe); }
.history-row-heading { display: flex; justify-content: space-between; gap: 8px; }
.history-field-line { display: flex; justify-content: space-between; gap: 8px; }
.history-field-line small,.history-review-reason { color: var(--gray-11, #60646c); }
.history-viewers { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 10px; min-width: 0; }
.history-reference-pane { display: flex; min-width: 0; min-height: 0; flex-direction: column; }
.history-reference-select { flex: 0 0 auto; padding: 6px; border: .5px solid var(--gray-6, #d9d9e0); border-bottom: 0; }
.history-reference-select .el-select { width: 100%; }
@media (max-width: 900px) { .history-review-layout,.history-viewers { grid-template-columns: 1fr; height: auto; } .history-viewers { min-height: 900px; } }
</style>
