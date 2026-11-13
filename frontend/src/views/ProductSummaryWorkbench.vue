<template>
  <div class="workbench-page">
    <app-topbar />
    <main class="workbench-main summary-workbench-main" v-loading="loading">
      <nav class="task-detail-nav summary-task-nav"><router-link :to="returnPath"><i class="el-icon-arrow-left" /> 核对任务</router-link><span class="detail-nav-divider" /><el-tag v-if="job" size="small" :type="job.status === 'failed' ? 'danger' : job.status === 'completed' ? '' : 'info'">{{ jobStatus }}</el-tag><div v-if="result" class="summary-detail-title"><strong :title="result.documents.product_summary.filename">{{ result.documents.product_summary.filename }}</strong><small>产品资料概要</small></div><span v-if="elapsed" class="detail-elapsed">执行用时 {{ elapsed }}</span></nav>
      <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon />
      <div v-if="job && job.status !== 'completed'" class="task-execution-state" :class="{ failed: job.status === 'failed' }"><i :class="job.status === 'failed' ? 'el-icon-circle-close' : 'el-icon-loading'" /><strong>{{ job.message }}</strong><span v-if="job.error">{{ job.error }}</span></div>
      <div v-if="job && job.status !== 'completed' && job.steps" class="summary-progress"><span v-for="step in progressSteps" :key="step.key"><i :class="job.steps[step.key]" />{{ step.label }}<small>{{ stepLabel(job.steps[step.key]) }}</small></span></div>
      <div v-if="result && result.warnings && result.warnings.length" class="warning-box"><i class="el-icon-warning-outline" /><div><p v-for="(warning, index) in result.warnings" :key="index">{{ warning }}</p></div></div>
      <section v-if="result" class="summary-review-layout">
        <aside class="findings-panel">
          <div class="panel-heading"><h2>核对结果</h2><el-select v-model="statusFilter" size="mini" class="result-category-filter" aria-label="按核对结果筛选"><el-option label="全部结果" value="" /><el-option label="存在差异" value="difference" /><el-option label="待人工复核" value="review" /><el-option label="一致" value="match" /></el-select><el-tag size="small" :type="tagType(result.status)">{{ statusLabel(result.status) }}</el-tag></div>
          <div class="findings-subheading"><span class="difference-total">已比对 {{ result.compared_count || 0 }} 项，<span class="review-count">待复核 {{ (result.unresolved_count || 0) + (result.not_checked_count || 0) }} 项</span></span></div>
          <div class="summary-items">
            <section v-for="group in groupedItems" :key="group.id" class="module-card" :class="{ expanded: expandedGroupId === group.id }">
              <button type="button" class="module-toggle" :aria-expanded="expandedGroupId === group.id" @click="toggleGroup(group.id)">
                <span class="status-dot" :class="statusClass(groupStatus(group))" />
                <span class="module-title">{{ group.label }}</span>
                <span class="module-count">{{ groupStatus(group) === 'MATCH' ? '一致' : group.items.length + ' 项' }}</span>
                <i :class="expandedGroupId === group.id ? 'el-icon-arrow-up' : 'el-icon-arrow-down'" />
              </button>
              <div v-if="expandedGroupId === group.id" class="module-details">
                <div v-for="entry in group.items" :key="entry.item.key" class="summary-item" :class="[{ selected: selectedIndex === entry.index }, statusClass(entry.item.status)]">
                  <button type="button" class="summary-item-main" @click="selectItem(entry.index)">
                    <span class="status-dot" :class="statusClass(entry.item.status)" />
                    <span class="summary-item-body"><strong>{{ entry.item.label }}</strong><span>{{ valueText(entry.item.summary) }} <i class="el-icon-right" /> {{ valueText(entry.item.reference) }}</span><small>{{ roleLabel(entry.item.reference && entry.item.reference.role) }}<template v-if="itemReason(entry.item)">：{{ itemReason(entry.item) }}</template></small></span>
                    <el-tag size="mini" :type="tagType(entry.item.status)">{{ statusLabel(entry.item.status) }}</el-tag>
                  </button>
                  <div v-if="resultCategory(entry.item.status) === 'review'" class="summary-item-actions">
                    <button type="button" class="draft-exception-button" @click="draftException(entry.item)">草拟例外</button>
                  </div>
                </div>
              </div>
            </section>
            <el-empty v-if="!groupedItems.length" description="暂无核对结果" />
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
import AppTopbar from "../components/AppTopbar.vue";
import PdfViewer from "../components/PdfViewer.vue";
export default {
  name: "ProductSummaryWorkbench",
  components: { AppTopbar, PdfViewer },
  props: { taskId: { type: String, required: true } },
  data() { return { job: null, result: null, loading: true, error: "", selectedIndex: 0, statusFilter: "", expandedGroupId: "", activeRole: "", now: Date.now(), timer: null, pollTimer: null }; },
  computed: {
    progressSteps() { return [{ key: "preflight", label: "文件检查" }, { key: "source_index", label: "文档解析" }, { key: "primary_lookup", label: "指标定位" }, { key: "fallback_lookup", label: "补充校验" }, { key: "comparison", label: "结果核对" }]; },
    returnPath() { return this.$route.query.returnTo || "/"; },
    availableRoles() {
      const roles = [{ key: "product_summary", label: "产品资料概要" }, { key: "periodic_report", label: "定期报告" }, { key: "prospectus", label: "招募说明书" }];
      return roles.filter((role) => this.result && this.result.documents[role.key]);
    },
    referenceRoles() { return this.availableRoles.filter((role) => role.key !== "product_summary"); },
    displayItems() { return this.result ? this.result.items.filter((item) => !["SKIPPED", "NO_COMMON_CONTENT"].includes(item.status)) : []; },
    filteredItems() { return this.displayItems.filter((item) => !this.statusFilter || this.resultCategory(item.status) === this.statusFilter); },
    groupedItems() {
      const labels = { fund_profile: "基金基本信息", investment_details: "投资信息", fees: "费用信息" };
      const groupOrder = ["基金基本信息", "投资信息", "费用信息"];
      const itemOrder = [
        "基金简称", "基金代码", "基金管理人", "基金托管人", "基金合同生效日",
        "基金经理", "开始担任本基金经理的日期", "场内简称与扩位证券简称",
        "投资目标", "主要投资策略", "业绩比较基准", "风险收益特征",
        "管理费率", "托管费率", "审计费用", "信息披露费",
      ];
      const groups = new Map();
      this.filteredItems.forEach((item, index) => {
        const rawId = item.module_id || "other";
        const label = labels[rawId] || rawId || "其他信息";
        if (!groups.has(label)) groups.set(label, { id: label, label, items: [] });
        groups.get(label).items.push({ item, index });
      });
      const rank = (list, value) => {
        const idx = list.indexOf(value);
        return idx >= 0 ? idx : list.length;
      };
      return Array.from(groups.values())
        .sort((a, b) => rank(groupOrder, a.label) - rank(groupOrder, b.label))
        .map((group) => ({
          ...group,
          items: group.items.slice().sort((a, b) => {
            const diff = rank(itemOrder, a.item.label) - rank(itemOrder, b.item.label);
            return diff !== 0 ? diff : a.index - b.index;
          }),
        }));
    },
    currentItem() { return this.filteredItems[this.selectedIndex]; },
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
  watch: {
    taskId: { immediate: true, handler() { this.statusFilter = ""; this.selectedIndex = 0; this.expandedGroupId = ""; this.load(); } },
    statusFilter() {
      const firstGroup = this.groupedItems[0];
      this.expandedGroupId = firstGroup ? firstGroup.id : "";
      this.selectedIndex = firstGroup && firstGroup.items[0] ? firstGroup.items[0].index : 0;
    },
    groupedItems(groups) {
      if (!groups.length) {
        this.expandedGroupId = "";
        return;
      }
      if (!groups.some((group) => group.id === this.expandedGroupId)) {
        this.expandedGroupId = groups[0].id;
        if (groups[0].items[0] && !this.filteredItems[this.selectedIndex]) {
          this.selectedIndex = groups[0].items[0].index;
        }
      }
    },
    activeRole() { this.$nextTick(() => this.scrollEvidence()); },
    selectedIndex() { this.$nextTick(() => this.scrollEvidence()); },
  },
  mounted() { this.timer = window.setInterval(() => { this.now = Date.now(); }, 1000); this.pollTimer = window.setInterval(() => { if (this.job && ["queued", "running"].includes(this.job.status)) this.load(true); }, 3000); },
  beforeDestroy() { window.clearInterval(this.timer); window.clearInterval(this.pollTimer); },
  methods: {
    itemReason(item) {
      const reference = item.reference || {};
      if (item.status === "NOT_CHECKED" && item.primary_lookup && item.primary_lookup.status === "NOT_APPLICABLE") {
        const reportName = this.result && this.result.documents.periodic_report && this.result.documents.periodic_report.filename || "";
        if (["audit_fee", "disclosure_fee"].includes(item.key) && /季度报告/.test(reportName)) {
          return "当前上传的是季度报告，报告类型不适用于年度预估费用核对；请使用中期报告或年度报告。";
        }
        return "当前定期报告类型不适用于此项核对，请使用适用的报告。";
      }
      if (item.status === "MATCH" && reference.role === "prospectus") {
        return item.fallback_lookup && item.fallback_lookup.comparison_reason || "概要与补充招募说明书的本项披露一致。";
      }
      if (item.status === "MATCH") return reference.reason || item.reason || "双方字段原文及引用已通过核验，核对一致。";
      return item.reason;
    },
    groupStatus(group) {
      const statuses = group.items.map((entry) => entry.item.status);
      if (statuses.every((status) => status === "MATCH")) return "MATCH";
      if (statuses.includes("MISMATCH")) return "MISMATCH";
      if (statuses.some((status) => ["UNRESOLVED", "UNRESOLVED_STRUCTURE", "UNRESOLVED_ROW_IDENTITY"].includes(status))) return "UNRESOLVED";
      return statuses[0] || "NOT_CHECKED";
    },
    toggleGroup(groupId) { this.expandedGroupId = this.expandedGroupId === groupId ? "" : groupId; },
    resultCategory(status) {
      if (status === "MATCH") return "match";
      if (["MISMATCH", "MISSING_ROW", "MISSING_FIELD", "MISSING_MODULE", "RANK_ORDER_CHANGED"].includes(status)) return "difference";
      if (["UNRESOLVED", "NOT_CHECKED", "UNRESOLVED_STRUCTURE", "UNRESOLVED_ROW_IDENTITY"].includes(status)) return "review";
      return "not-compared";
    },
    async load(silent) {
      if (!silent) this.loading = true;
      try {
        const { data } = await axios.get("/api/jobs/" + encodeURIComponent(this.taskId));
        this.job = data; this.result = data.result || null; this.error = "";
        const firstGroup = this.groupedItems[0];
        if (!silent && firstGroup && firstGroup.items[0]) {
          this.expandedGroupId = firstGroup.id;
          this.selectedIndex = firstGroup.items[0].index;
        }
        if (this.result && !this.result.documents[this.activeRole]) this.activeRole = this.referenceRoles[0] && this.referenceRoles[0].key || "";
      } catch (error) { this.error = error.response && error.response.data && error.response.data.detail || "无法读取核对任务"; }
      finally { this.loading = false; }
    },
    selectItem(index) { this.selectedIndex = index; const item = this.filteredItems[index]; const reference = this.referenceRoles.find((role) => item.references && item.references[role.key] && item.references[role.key].evidence.length); if (reference) this.activeRole = reference.key; },
    draftException(item) { this.$message.info("已选择「" + (item && item.label || "该待复核项") + "」草拟例外"); },
    toViewerEvidences(side) { const item = this.currentItem; return (side && side.evidence || []).map((source) => ({ ...source, status: "LOCATED", field_label: item.label, difference_status: item.status })); },
    scrollEvidence() { const summary = this.summaryEvidences[0], reference = this.referenceEvidences[0]; if (summary && this.$refs.summaryViewer) this.$refs.summaryViewer.scrollToPage(summary.page); if (reference && this.$refs.referenceViewer) this.$refs.referenceViewer.scrollToPage(reference.page); },
    roleLabel(role) { const item = this.availableRoles.find((value) => value.key === role); return item ? item.label : role; },
    valueText(side) { return side && side.raw_text || "—"; },
    statusClass(status) { return String(status || "").toLowerCase().replace(/_/g, "-"); },
    statusLabel(status) { return ({ MATCH: "一致", MISMATCH: "存在差异", UNRESOLVED: "待复核", NO_COMMON_CONTENT: "无可比较内容", NOT_CHECKED: "待复核", SOURCE_MISSING: "缺少来源", SKIPPED: "待复核" })[status] || "待复核"; },
    stepLabel(status) { return ({ queued: "等待", running: "处理中", completed: "完成", failed: "失败" })[status] || "等待"; },
    tagType(status) { return status === "MATCH" ? "" : status === "MISMATCH" ? "danger" : ["SKIPPED", "NO_COMMON_CONTENT"].includes(status) ? "info" : "warning"; },
  },
};
</script>
