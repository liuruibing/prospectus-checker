<template>
  <div class="workbench-page">
    <header class="app-topbar">
      <div class="brand-block"><div class="brand-logo">审</div><div class="brand-name"><strong>产品资料概要核对</strong></div></div>
      <div class="topbar-actions"><el-button type="primary" class="upload-trigger" @click="$router.push('/')">核对任务</el-button></div>
    </header>
    <main class="workbench-main summary-workbench-main" v-loading="loading">
      <nav class="task-detail-nav"><router-link :to="returnPath"><i class="el-icon-arrow-left" /> 核对任务</router-link><span class="detail-nav-divider" /><span class="detail-task-id">{{ taskId }}</span><el-tag v-if="job" size="small" :type="job.status === 'failed' ? 'danger' : job.status === 'completed' ? 'success' : 'info'">{{ jobStatus }}</el-tag><span v-if="elapsed" class="detail-elapsed">执行用时 {{ elapsed }}</span></nav>
      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
      <div v-if="job && job.status !== 'completed'" class="task-execution-state" :class="{ failed: job.status === 'failed' }"><i :class="job.status === 'failed' ? 'el-icon-circle-close' : 'el-icon-loading'" /><strong>{{ job.message }}</strong><span v-if="job.error">{{ job.error }}</span></div>
      <div v-if="job && job.status !== 'completed' && job.steps" class="summary-progress"><span v-for="step in progressSteps" :key="step.key"><i :class="job.steps[step.key]" />{{ step.label }}<small>{{ stepLabel(job.steps[step.key]) }}</small></span></div>
      <section v-if="result" class="summary-result-bar">
        <div><strong>{{ result.documents.product_summary.filename }}</strong><small>产品资料概要</small></div>
        <i class="el-icon-right" />
        <div class="summary-reference-tabs"><button v-for="role in referenceRoles" :key="role.key" type="button" :class="{ active: activeRole === role.key }" @click="activeRole = role.key">{{ role.label }}</button></div>
        <el-tag :type="tagType(result.status)">{{ statusLabel(result.status) }}</el-tag>
      </section>
      <div v-if="result && result.warnings && result.warnings.length" class="warning-box"><i class="el-icon-warning-outline" /><div><p v-for="(warning, index) in result.warnings" :key="index">{{ warning }}</p></div></div>
      <section v-if="result" class="summary-review-layout">
        <aside class="findings-panel">
          <div class="panel-heading"><h2>核对结果</h2><el-tag size="small" :type="tagType(result.status)">{{ statusLabel(result.status) }}</el-tag></div>
          <div class="findings-subheading"><span>Excel 核对项</span><span class="difference-total">已比对 {{ result.compared_count || 0 }} 项，待复核 {{ result.unresolved_count || 0 }} 项，未核对 {{ result.not_checked_count || 0 }} 项</span></div>
          <div class="summary-items">
            <button v-for="(item, index) in displayItems" :key="item.key" type="button" class="summary-item" :class="[{ selected: selectedIndex === index }, statusClass(item.status)]" @click="selectItem(index)">
              <span class="status-dot" :class="statusClass(item.status)" />
              <span class="summary-item-body"><strong>{{ item.label }}</strong><span>{{ valueText(item.summary) }} <i class="el-icon-right" /> {{ valueText(item.reference) }}</span><small>{{ roleLabel(item.reference && item.reference.role) }}<template v-if="itemReason(item)">：{{ itemReason(item) }}</template></small></span>
              <el-tag size="mini" :type="tagType(item.status)">{{ statusLabel(item.status) }}</el-tag>
            </button>
            <el-empty v-if="!displayItems.length" description="暂无核对结果" />
          </div>
          <div class="legend-note"><span><i class="legend-dot mismatch" />披露差异</span><span><i class="legend-dot unresolved" />待复核</span></div>
        </aside>
        <section class="summary-document-panel">
          <div class="summary-document-heading"><span>产品资料概要</span><strong>{{ summaryDocument && summaryDocument.filename }}</strong></div>
          <pdf-viewer v-if="summaryDocument" ref="summaryViewer" role-name="product_summary" title="产品资料概要" :document-info="summaryDocument" :evidences="summaryEvidences" />
          <el-empty v-else description="概要文件不可用" />
        </section>
        <section class="summary-document-panel">
          <div v-if="referenceDocument" class="summary-document-heading"><span>{{ roleLabel(activeRole) }}</span><strong>{{ referenceDocument.filename }}</strong></div>
          <pdf-viewer v-if="referenceDocument" ref="referenceViewer" :role-name="activeRole" :title="roleLabel(activeRole)" :document-info="referenceDocument" :evidences="referenceEvidences" />
          <el-empty v-else description="参考文件不可用" />
        </section>
      </section>
    </main>
  </div>
</template>

<script>
import axios from "axios";
import PdfViewer from "../components/PdfViewer.vue";
export default {
  name: "ProductSummaryWorkbench",
  components: { PdfViewer },
  props: { taskId: { type: String, required: true } },
  data() { return { job: null, result: null, loading: true, error: "", selectedIndex: 0, activeRole: "", now: Date.now(), timer: null, pollTimer: null }; },
  computed: {
    progressSteps() { return [{ key: "preflight", label: "文件检查" }, { key: "source_index", label: "文档解析" }, { key: "primary_lookup", label: "指标定位" }, { key: "fallback_lookup", label: "补充校验" }, { key: "comparison", label: "结果核对" }]; },
    returnPath() { return this.$route.query.returnTo || "/"; },
    availableRoles() {
      const roles = [{ key: "product_summary", label: "产品资料概要" }, { key: "periodic_report", label: "定期报告" }, { key: "prospectus", label: "招募说明书" }];
      return roles.filter((role) => this.result && this.result.documents[role.key]);
    },
    referenceRoles() { return this.availableRoles.filter((role) => role.key !== "product_summary"); },
    displayItems() { return this.result ? this.result.items.filter((item) => !["SKIPPED", "NO_COMMON_CONTENT"].includes(item.status)) : []; },
    currentItem() { return this.displayItems[this.selectedIndex]; },
    summaryDocument() { return this.result && this.result.documents.product_summary || null; },
    referenceDocument() { return this.result && this.result.documents[this.activeRole] || null; },
    summaryEvidences() { return this.toViewerEvidences(this.currentItem && this.currentItem.summary); },
    referenceEvidences() { const item = this.currentItem; return this.toViewerEvidences(item && item.references && item.references[this.activeRole]); },
    elapsed() {
      if (!this.job || !this.job.started_at) return "";
      const start = Date.parse(this.job.started_at), end = this.job.completed_at ? Date.parse(this.job.completed_at) : this.now;
      if (!Number.isFinite(start) || !Number.isFinite(end)) return "";
      const seconds = Math.max(0, Math.floor((end - start) / 1000));
      return String(Math.floor(seconds / 60)).padStart(2, "0") + ":" + String(seconds % 60).padStart(2, "0");
    },
    jobStatus() { return this.job && this.job.status === "completed" ? "已完成" : this.job && this.job.status === "failed" ? "失败" : "核对中"; },
  },
  watch: { taskId: { immediate: true, handler() { this.load(); } }, activeRole() { this.$nextTick(() => this.scrollEvidence()); }, selectedIndex() { this.$nextTick(() => this.scrollEvidence()); } },
  mounted() { this.timer = window.setInterval(() => { this.now = Date.now(); }, 1000); this.pollTimer = window.setInterval(() => { if (this.job && ["queued", "running"].includes(this.job.status)) this.load(true); }, 3000); },
  beforeDestroy() { window.clearInterval(this.timer); window.clearInterval(this.pollTimer); },
  methods: {
    itemReason(item) {
      const reference = item.reference || {};
      if (item.status === "MATCH" && reference.role === "prospectus") {
        return item.fallback_lookup && item.fallback_lookup.comparison_reason || "概要与补充招募说明书的本项披露一致。";
      }
      if (item.status === "MATCH") return reference.reason || item.reason || "双方字段原文及引用已通过核验，核对一致。";
      return item.reason;
    },
    async load(silent) {
      if (!silent) this.loading = true;
      try {
        const { data } = await axios.get("/api/jobs/" + encodeURIComponent(this.taskId));
        this.job = data; this.result = data.result || null; this.error = "";
        if (this.result && !this.result.documents[this.activeRole]) this.activeRole = this.referenceRoles[0] && this.referenceRoles[0].key || "";
      } catch (error) { this.error = error.response && error.response.data && error.response.data.detail || "无法读取核对任务"; }
      finally { this.loading = false; }
    },
    selectItem(index) { this.selectedIndex = index; const item = this.displayItems[index]; const reference = this.referenceRoles.find((role) => item.references && item.references[role.key] && item.references[role.key].evidence.length); if (reference) this.activeRole = reference.key; },
    toViewerEvidences(side) { const item = this.currentItem; return (side && side.evidence || []).map((source) => ({ ...source, status: "LOCATED", field_label: item.label, difference_status: item.status })); },
    scrollEvidence() { const summary = this.summaryEvidences[0], reference = this.referenceEvidences[0]; if (summary && this.$refs.summaryViewer) this.$refs.summaryViewer.scrollToPage(summary.page); if (reference && this.$refs.referenceViewer) this.$refs.referenceViewer.scrollToPage(reference.page); },
    roleLabel(role) { const item = this.availableRoles.find((value) => value.key === role); return item ? item.label : role; },
    valueText(side) { return side && side.raw_text || "—"; },
    statusClass(status) { return String(status || "").toLowerCase().replace(/_/g, "-"); },
    statusLabel(status) { return ({ MATCH: "一致", MISMATCH: "存在差异", UNRESOLVED: "待复核", NOT_CHECKED: "未核对", SOURCE_MISSING: "缺少来源", SKIPPED: "未核对" })[status] || "待复核"; },
    stepLabel(status) { return ({ queued: "等待", running: "处理中", completed: "完成", failed: "失败" })[status] || "等待"; },
    tagType(status) { return status === "MATCH" ? "success" : status === "MISMATCH" ? "danger" : ["SKIPPED", "NOT_CHECKED", "NO_COMMON_CONTENT"].includes(status) ? "info" : "warning"; },
  },
};
</script>
