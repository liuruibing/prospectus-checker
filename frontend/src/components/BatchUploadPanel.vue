<template>
  <section class="batch-upload-panel">
    <div class="batch-upload-picker" @dragover.prevent @drop.prevent="handleDrop">
      <i class="el-icon-folder-opened" />
      <strong>选择一批 PDF 或文件夹</strong>
      <span>文件夹中的 PDF 会一起识别，文件夹结构只用于展示</span>
      <div class="batch-upload-actions">
        <el-button @click="$refs.filesInput.click()">选择多个文件</el-button>
        <el-button @click="$refs.folderInput.click()">选择文件夹</el-button>
      </div>
      <small>也可以把多个 PDF 拖到这里</small>
      <input ref="filesInput" type="file" accept="application/pdf,.pdf" multiple @change="handleFileChange">
      <input ref="folderInput" type="file" accept="application/pdf,.pdf" multiple webkitdirectory directory @change="handleFileChange">
    </div>

    <div v-if="selectedFiles.length" class="batch-file-summary">
      <span>已选择 {{ selectedFiles.length }} 个 PDF</span>
      <span>{{ formatSize(totalBytes) }}</span>
      <el-button type="text" @click="clearFiles">清空</el-button>
    </div>
    <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="upload-error" />
    <el-button v-if="selectedFiles.length && !documents.length" type="primary" :loading="scanning" class="batch-scan-button" @click="scanFiles">
      {{ scanning ? '正在识别文件' : '识别并预览' }}
    </el-button>

    <template v-if="documents.length">
      <div class="batch-preview-heading">
        <strong>文件识别结果</strong>
        <span>{{ documents.length }} 个文件，必要时 AI 会结合文件内容和日期协助匹配</span>
      </div>
      <div class="batch-document-list">
        <div class="batch-document-row batch-document-header">
          <span>文件</span><span>识别类型</span><span>基金名称</span><span>日期</span><span />
        </div>
        <div v-for="doc in documents" :key="doc.id" class="batch-document-row">
          <span class="batch-document-source" :title="doc.relative_path">
            <strong>{{ doc.filename }}</strong><small>{{ doc.relative_path }}</small>
            <small v-if="doc.report_subtype && roleFor(doc) === 'periodic_report'">{{ reportSubtypeLabel(doc.report_subtype) }}</small>
          <small v-if="roleFor(doc) !== doc.role && doc.role !== 'unknown'" class="batch-document-warning">手动改为{{ roleLabel(roleFor(doc)) }}，自动识别为{{ roleLabel(doc.role) }}</small>
          </span>
          <el-select :value="roleFor(doc)" size="mini" @change="setRole(doc.id, $event)">
            <el-option v-for="item in roleOptions" :key="item.value" :label="item.label" :value="item.value" />
          </el-select>
          <span class="batch-document-fund" :title="doc.fund_name || ''">{{ doc.fund_name || '未识别' }}</span>
          <span class="batch-document-date">{{ formatInterval(doc.date_interval) }}</span>
          <el-button type="text" class="batch-remove-file" @click="removeDocument(doc.id)">移除</el-button>
        </div>
      </div>

      <div class="batch-group-heading">
        <strong>任务分组</strong>
        <span>以招募说明书或产品概要建立任务</span>
      </div>
      <div v-if="!groups.length" class="batch-no-groups">请将一个文件识别为招募说明书或产品概要，作为任务核心文件。</div>
      <article v-for="group in groups" :key="group.core.id" class="batch-task-group" :class="{ incomplete: !group.periodicId || group.identityMismatch || group.dateMismatch }">
        <div class="batch-group-core">
          <el-checkbox
            class="batch-group-checkbox"
            :value="isGroupSelected(group)"
            :disabled="!group.periodicId || group.identityMismatch || group.dateMismatch || submitting"
            :aria-label="'选择' + roleLabel(group.coreRole) + '任务'"
            @change="toggleGroupSelection(group, $event)"
          />
          <span class="batch-group-type" :class="group.workflow">{{ group.workflow === 'investment' ? '招' : '概' }}</span>
          <div>
            <strong>{{ roleLabel(group.coreRole) }}</strong>
            <span>{{ group.core.filename }}</span>
            <small v-if="group.workflow === 'investment' && group.core.portfolio_period_end_interval">
              （组合报告期末：{{ formatInterval(group.core.portfolio_period_end_interval) }}）
            </small>
            <small v-else-if="group.workflow === 'summary' && group.core.date_interval">
              （编制日期：{{ formatInterval(group.core.date_interval) }}）
            </small>
          </div>
          <el-tag v-if="group.localRecommendation && group.periodicId === group.localRecommendation" size="mini" type="info">{{ group.workflow === 'investment' ? '已匹配同期末报告' : '已匹配最近报告' }}</el-tag>
          <el-tag v-if="group.identityMismatch" size="mini" type="danger">基金身份不符</el-tag>
          <el-tag v-else-if="group.dateMismatch" size="mini" type="danger">{{ group.workflow === 'investment' ? '报告期末不符' : '送出日期不符' }}</el-tag>
          <el-tag v-else-if="group.periodicId" size="mini" type="success">可创建</el-tag>
          <el-tag v-else-if="group.reportOptions.length" size="mini" type="warning">待选择报告</el-tag>
          <el-tag v-else size="mini" type="warning">缺少定期报告</el-tag>
          <el-button
            type="text"
            size="mini"
            icon="el-icon-delete"
            class="batch-group-delete"
            :disabled="submitting"
            @click="removeGroup(group.core.id)"
          >
            删除
          </el-button>
        </div>
        <div class="batch-group-fields">
          <label><span>定期报告</span>
            <el-select :value="group.periodicId" size="small" placeholder="选择对应报告" @change="setReport(group.core.id, $event)">
              <el-option v-for="doc in group.reportOptions" :key="doc.id" :label="documentOption(doc)" :value="doc.id" />
            </el-select>
            <small v-if="group.dateMismatch" class="batch-match-note">{{ group.dateMismatchMessage }}</small>
            <small v-else-if="!group.periodicId" class="batch-match-note">{{ group.reportMatchMessage }}</small>
          </label>
          <label v-if="group.workflow === 'summary'"><span>补充招募说明书</span>
            <el-select :value="group.prospectusId" clearable size="small" placeholder="可选" @change="setSupplement(group.core.id, $event)">
              <el-option v-for="doc in group.prospectusOptions" :key="doc.id" :label="documentOption(doc)" :value="doc.id" />
            </el-select>
          </label>
          <label v-else-if="group.workflow === 'investment'"><span>历年业绩补充报告（可选）</span>
            <el-select v-if="group.historicalOptions.length" :value="group.historicalIds" multiple clearable collapse-tags size="small" placeholder="自动匹配历年年报/Q4（可留空）" @change="setHistoricalReports(group.core.id, $event)">
              <el-option v-for="doc in group.historicalOptions" :key="doc.id" :label="documentOption(doc)" :value="doc.id" />
            </el-select>
            <small v-if="group.historicalNote" class="batch-match-note">{{ group.historicalNote }}</small>
          </label>
        </div>
      </article>

      <el-alert v-if="incompleteCount" :title="incompleteCount + ' 组缺少定期报告、基金身份不匹配或日期条件不符，这些组不会创建。可先创建已配齐的任务。'" type="warning" :closable="false" show-icon class="batch-missing-alert" />
      <div class="batch-submit-row">
        <span>将创建 {{ selectedReadyGroups.length }} 个核对任务</span>
        <el-button type="primary" :loading="submitting" :disabled="!selectedReadyGroups.length" @click="submitGroups">
          {{ submitting ? '正在创建' : '创建选中任务' }}
        </el-button>
      </div>
    </template>
  </section>
</template>

<script>
import axios from "axios";

const ROLE_OPTIONS = [
  { value: "prospectus", label: "招募说明书" },
  { value: "periodic_report", label: "定期报告" },
  { value: "product_summary", label: "产品概要" },
  { value: "unknown", label: "未识别" },
];

export default {
  name: "BatchUploadPanel",
  data() {
    return {
      files: [], documents: [], batchId: "", roles: {}, removedFileIds: {}, removedGroupIds: {}, aiMatches: {}, reportAssignments: {}, reportTouched: {}, selectedGroupIds: [],
      supplementAssignments: {}, supplementTouched: {}, historicalAssignments: {}, historicalTouched: {}, scanning: false, submitting: false, error: "",
      roleOptions: ROLE_OPTIONS,
    };
  },
  computed: {
    selectedFiles() { return this.files.filter((_file, index) => !this.removedFileIds[String(index)]); },
    totalBytes() { return this.selectedFiles.reduce((sum, file) => sum + file.size, 0); },
    groups() {
      const reports = this.documents.filter((doc) => this.roleFor(doc) === "periodic_report");
      const prospectuses = this.documents.filter((doc) => this.roleFor(doc) === "prospectus");
      return this.documents.filter((doc) => ["prospectus", "product_summary"].includes(this.roleFor(doc)) && !this.removedGroupIds[doc.id]).map((core) => {
        const coreRole = this.roleFor(core);
        const workflow = coreRole === "prospectus" ? "investment" : "summary";
        const corePortfolioPeriod = workflow === "investment" ? this.intervalDate(core.portfolio_period_end_interval) : "";
        const coreDate = this.intervalDate(core.date_interval);
        const aiMatch = this.aiMatches[core.id];
        const rawAiReport = aiMatch && aiMatch.same_product && Number(aiMatch.confidence) >= 0.75
          ? reports.find((report) => report.id === aiMatch.periodic_file_id) : null;
        const aiReportPeriod = rawAiReport ? this.reportPeriodEnd(rawAiReport) : "";
        const aiReport = rawAiReport && (
          workflow === "investment"
            ? (!corePortfolioPeriod || !aiReportPeriod || aiReportPeriod === corePortfolioPeriod)
            : this.summaryReportEligible(coreDate, rawAiReport)
        ) ? rawAiReport : null;
        const subtypeOrder = { quarterly: 0, interim: 1, annual: 2 };
        const subtypeRank = (report) => {
          if (workflow === "investment" && core.portfolio_report_subtype && report.report_subtype === core.portfolio_report_subtype) return -1;
          return report.report_subtype in subtypeOrder ? subtypeOrder[report.report_subtype] : 3;
        };
        const matchingReports = reports.filter((report) => this.sameFund(core, report)).sort((left, right) => {
          if (workflow === "summary") {
            const leftEligible = this.summaryReportEligible(coreDate, left) ? 0 : 1;
            const rightEligible = this.summaryReportEligible(coreDate, right) ? 0 : 1;
            if (leftEligible !== rightEligible) return leftEligible - rightEligible;
            const leftSend = this.reportSendDate(left), rightSend = this.reportSendDate(right);
            const sendOrder = (rightSend || "").localeCompare(leftSend || "");
            if (sendOrder) return sendOrder;
          }
          const leftDate = this.intervalDate(left.date_interval), rightDate = this.intervalDate(right.date_interval);
          const dateOrder = (rightDate || "").localeCompare(leftDate || "");
          return dateOrder || (subtypeRank(left) - subtypeRank(right));
        });
        const baseReportOptions = matchingReports.length ? matchingReports : reports;
        const reportOptions = aiReport && !baseReportOptions.some((report) => report.id === aiReport.id)
          ? [aiReport, ...baseReportOptions] : baseReportOptions;
        const localRecommendation = this.recommendReport(core, matchingReports);
        const recommendedReportId = localRecommendation || (aiReport && aiReport.id) || "";
        const periodicId = this.reportTouched[core.id] ? (this.reportAssignments[core.id] || "") : recommendedReportId;
        const datedReports = workflow === "investment"
          ? matchingReports.filter((report) => this.intervalDate(report.date_interval))
          : matchingReports.filter((report) => this.reportSendDate(report) || this.intervalDate(report.date_interval));
        const eligibleReports = workflow === "investment"
          ? (corePortfolioPeriod ? matchingReports.filter((report) => this.reportPeriodEnd(report) === corePortfolioPeriod) : [])
          : datedReports.filter((report) => this.summaryReportEligible(coreDate, report));
        const reportMatchMessage = !reports.length
          ? "未识别到定期报告。"
          : !matchingReports.length
            ? "已识别到定期报告，但基金身份未匹配，请确认后手动选择。"
            : workflow === "investment"
              ? (corePortfolioPeriod
                ? "招募说明书投资组合报告截止日为 " + corePortfolioPeriod + "，当前批次未找到同报告期末的定期报告。"
                : "未识别到招募说明书投资组合报告期末，请手动确认对应的定期报告。")
              : coreDate && datedReports.length && !eligibleReports.length
                ? "同基金定期报告的报告送出日期晚于产品概要编制日期（" + coreDate + "），未自动关联。"
                : "已匹配到同基金报告，但日期信息不足以自动选择，请手动确认。";
        const matchingProspectuses = prospectuses.filter((item) => this.sameFund(core, item) && item.id !== core.id);
        const prospectusOptions = matchingProspectuses.length ? matchingProspectuses : prospectuses.filter((item) => item.id !== core.id);
        const recommendedProspectus = matchingProspectuses.length === 1 ? matchingProspectuses[0].id : "";
        const prospectusId = this.supplementTouched[core.id] ? (this.supplementAssignments[core.id] || "") : recommendedProspectus;
        const periodic = reports.find((item) => item.id === periodicId);
        const periodicPeriod = this.reportPeriodEnd(periodic);
        const historicalOptions = (matchingReports.length ? matchingReports : reports).filter((item) => item.id !== periodicId);
        const isEligibleHistorical = (item) => {
          if (!item || item.id === periodicId) return false;
          const periodEnd = this.reportPeriodEnd(item);
          if (corePortfolioPeriod && periodEnd && periodEnd > corePortfolioPeriod) return false;
          const isYearEnd = /-12-31$/.test(periodEnd);
          return item.report_subtype === "annual" || (item.report_subtype === "quarterly" && isYearEnd);
        };
        const recommendedByPeriod = {};
        if (workflow === "investment") {
          matchingReports.filter(isEligibleHistorical).forEach((item) => {
            const periodEnd = this.reportPeriodEnd(item);
            if (periodic && periodic.report_subtype === "annual" && periodEnd && periodEnd === periodicPeriod) return;
            const key = periodEnd || item.id;
            const existing = recommendedByPeriod[key];
            if (!existing || (existing.report_subtype !== "annual" && item.report_subtype === "annual")) {
              recommendedByPeriod[key] = item;
            }
          });
        }
        const recommendedHistoricals = Object.values(recommendedByPeriod).map((item) => item.id);
        const historicalIds = workflow === "investment"
          ? (this.historicalTouched[core.id] ? (this.historicalAssignments[core.id] || []) : recommendedHistoricals)
              .filter((id) => id !== periodicId && historicalOptions.some((opt) => opt.id === id))
          : [];
        const periodicIsYearEnd = Boolean(periodic && (periodic.report_subtype === "annual" || (periodic.report_subtype === "quarterly" && /-12-31$/.test(periodicPeriod))));
        const historicalNote = workflow !== "investment"
          ? ""
          : historicalIds.length
            ? "已按同基金优先匹配历年年报（其次 Q4 季报），共关联 " + historicalIds.length + " 份补充报告用于近十年逐年度业绩比对。"
            : periodic && !periodicIsYearEnd
              ? "当前未匹配到年度报告或 Q4 季报，逐年度基金业绩将跳过核对（如需核对请同批上传历年年报）。"
              : periodic
                ? "当前未检测到更早年度的年报/Q4，将仅核对所选报告已披露年度，缺失年份保留待人工复核。"
                : "";
        const hasIdentity = (item) => Boolean(item && (item.fund_code || item.fund_name));
        const aiConfirmedSelection = Boolean(aiReport && periodicId === aiReport.id);
        const identityMismatch = Boolean(periodic && hasIdentity(core) && hasIdentity(periodic) && !this.sameFund(core, periodic) && !aiConfirmedSelection);
        const periodicSendDate = this.reportSendDate(periodic);
        const periodMismatch = Boolean(
          workflow === "investment" && periodic && corePortfolioPeriod && periodicPeriod && corePortfolioPeriod !== periodicPeriod
        );
        const sendDateMismatch = Boolean(
          workflow === "summary" && periodic && coreDate && periodicSendDate && coreDate < periodicSendDate
        );
        const dateMismatch = periodMismatch || sendDateMismatch;
        const dateMismatchMessage = periodMismatch
          ? "招募说明书投资组合报告截止日为 " + corePortfolioPeriod + "，与所选定期报告期末（" + periodicPeriod + "）不一致，请改选同报告期末的定期报告。"
          : sendDateMismatch
            ? "产品概要编制日期（" + coreDate + "）必须大于等于所选定期报告送出日期（" + periodicSendDate + "），请改选送出日期不晚于编制日期的定期报告。"
            : "";
        return {
          core, coreRole, workflow,
          periodicId, recommendedReportId, localRecommendation, reportOptions, reportMatchMessage, prospectusId, prospectusOptions,
          historicalIds, historicalOptions, historicalNote, identityMismatch, dateMismatch, dateMismatchMessage,
        };
      });
    },
    readyGroups() { return this.groups.filter((group) => group.periodicId && !group.identityMismatch && !group.dateMismatch); },
    selectedReadyGroups() {
      const selected = new Set(this.selectedGroupIds);
      return this.readyGroups.filter((group) => selected.has(group.core.id));
    },
    incompleteCount() { return this.groups.filter((group) => !group.periodicId || group.identityMismatch || group.dateMismatch).length; },
  },
  methods: {
    roleFor(doc) { return this.roles[doc.id] || doc.role || "unknown"; },
    roleLabel(role) { return (ROLE_OPTIONS.find((item) => item.value === role) || ROLE_OPTIONS[3]).label; },
    formatSize(bytes) { return bytes >= 1024 * 1024 ? (bytes / 1024 / 1024).toFixed(1) + " MiB" : Math.ceil(bytes / 1024) + " KiB"; },
    formatInterval(interval) {
      if (!interval || !interval.start) return "未识别";
      if (!interval.end || interval.start === interval.end) return interval.start;
      const match = /^(\d{4})-(\d{2})-01$/.exec(interval.start);
      if (match && interval.end === match[1] + "-" + match[2] + "-" + String(new Date(Number(match[1]), Number(match[2]), 0).getDate()).padStart(2, "0")) return match[1] + "-" + match[2];
      return interval.start + " 至 " + interval.end;
    },
    intervalDate(interval) { return interval && (interval.end || interval.start) || ""; },
    reportPeriodEnd(doc) {
      if (!doc || doc.date_label === "定期报告送出日期") return "";
      return this.intervalDate(doc.date_interval);
    },
    reportSendDate(doc) {
      if (!doc) return "";
      const direct = this.intervalDate(doc.report_send_date_interval);
      if (direct) return direct;
      if (doc.date_label === "定期报告送出日期") return this.intervalDate(doc.date_interval);
      return "";
    },
    summaryReportEligible(coreDate, report) {
      if (!coreDate || !report) return true;
      const sendDate = this.reportSendDate(report);
      if (sendDate) return sendDate <= coreDate;
      const reportDate = this.intervalDate(report.date_interval);
      return !reportDate || reportDate <= coreDate;
    },
    recommendReport(core, reports) {
      if (this.roleFor(core) === "prospectus") {
        const targetPeriod = this.intervalDate(core.portfolio_period_end_interval);
        if (targetPeriod) {
          const exactMatches = reports.filter((report) => this.reportPeriodEnd(report) === targetPeriod);
          return exactMatches.length ? exactMatches[0].id : "";
        }
        return reports.length === 1 ? reports[0].id : "";
      }
      const coreDate = this.intervalDate(core.date_interval);
      const datedReports = reports.filter((report) => this.reportSendDate(report) || this.intervalDate(report.date_interval));
      const eligible = datedReports.filter((report) => this.summaryReportEligible(coreDate, report));
      if (eligible.length) return eligible[0].id;
      if (coreDate && datedReports.length) return "";
      return reports.length === 1 ? reports[0].id : "";
    },
    reportSubtypeLabel(subtype) {
      return ({ annual: "年度报告", interim: "中期报告", quarterly: "季度报告" })[subtype] || "定期报告";
    },
    normalizeFund(name) { return String(name || "").replace(/\s+/g, "").replace(/[（(][^）)]*[）)]/g, "").replace(/基金/g, ""); },
    sameFund(left, right) {
      if (left.fund_code && right.fund_code) return left.fund_code === right.fund_code;
      const leftName = this.normalizeFund(left.fund_name), rightName = this.normalizeFund(right.fund_name);
      return !!(leftName && rightName && leftName === rightName);
    },
    documentOption(doc) {
      const identity = doc.fund_name || (doc.fund_code ? "代码 " + doc.fund_code : "名称未识别");
      const subtype = doc.report_subtype ? "（" + this.reportSubtypeLabel(doc.report_subtype) + "）" : "";
      const sendText = doc.report_send_date_interval ? " | 送出 " + this.formatInterval(doc.report_send_date_interval) : "";
      return identity + " | " + doc.filename + " | " + this.formatInterval(doc.date_interval) + subtype + sendText;
    },
    handleFileChange(event) {
      this.addFiles(event.target.files);
      event.target.value = "";
    },
    handleDrop(event) { this.addFiles(event.dataTransfer && event.dataTransfer.files); },
    addFiles(fileList) {
      if (this.documents.length && Object.keys(this.removedFileIds).length) {
        this.files = this.selectedFiles;
        this.removedFileIds = {};
      }
      const incoming = Array.from(fileList || []).filter((file) => /\.pdf$/i.test(file.name));
      const existing = new Set(this.files.map((file) => (file.webkitRelativePath || file.name) + ":" + file.size));
      incoming.forEach((file) => {
        const key = (file.webkitRelativePath || file.name) + ":" + file.size;
        if (!existing.has(key)) { this.files.push(file); existing.add(key); }
      });
      this.documents = []; this.batchId = ""; this.error = ""; this.selectedGroupIds = []; this.removedGroupIds = {};
      this.aiMatches = {};
      if (!incoming.length) this.error = "请选择 PDF 文件或文件夹。";
      if (this.files.length > 100) { this.files = this.files.slice(0, 100); this.error = "一次最多上传 100 个 PDF。"; }
    },
    clearFiles() { this.files = []; this.documents = []; this.batchId = ""; this.removedFileIds = {}; this.removedGroupIds = {}; this.aiMatches = {}; this.selectedGroupIds = []; this.error = ""; },
    removeDocument(id) {
      this.documents = this.documents.filter((doc) => doc.id !== id);
      this.$set(this.removedFileIds, id, true);
      this.$delete(this.removedGroupIds, id);
      this.$delete(this.roles, id);
      this.reportAssignments = {}; this.reportTouched = {}; this.supplementAssignments = {}; this.supplementTouched = {};
      this.historicalAssignments = {}; this.historicalTouched = {};
      this.aiMatches = {};
      this.selectedGroupIds = this.selectedGroupIds.filter((groupId) => groupId !== id);
    },
    removeGroup(coreId) {
      this.$set(this.removedGroupIds, coreId, true);
      this.selectedGroupIds = this.selectedGroupIds.filter((groupId) => groupId !== coreId);
    },
    setRole(id, role) {
      this.$set(this.roles, id, role);
      this.$delete(this.removedGroupIds, id);
      this.reportTouched = {}; this.supplementTouched = {}; this.historicalTouched = {};
      this.aiMatches = {};
      this.$nextTick(() => {
        this.selectedGroupIds = this.readyGroups.map((group) => group.core.id);
      });
    },
    setReport(coreId, fileId) {
      const wasReady = this.readyGroups.some((group) => group.core.id === coreId);
      this.$set(this.reportAssignments, coreId, fileId);
      this.$set(this.reportTouched, coreId, true);
      this.$nextTick(() => {
        const isReady = this.readyGroups.some((group) => group.core.id === coreId);
        if (!wasReady && isReady && !this.selectedGroupIds.includes(coreId)) {
          this.selectedGroupIds = this.selectedGroupIds.concat(coreId);
        } else if (!isReady && this.selectedGroupIds.includes(coreId)) {
          this.selectedGroupIds = this.selectedGroupIds.filter((groupId) => groupId !== coreId);
        }
      });
    },
    isGroupSelected(group) { return this.selectedGroupIds.includes(group.core.id); },
    toggleGroupSelection(group, checked) {
      const selected = new Set(this.selectedGroupIds);
      if (checked) selected.add(group.core.id);
      else selected.delete(group.core.id);
      this.selectedGroupIds = Array.from(selected);
    },
    setSupplement(coreId, fileId) { this.$set(this.supplementAssignments, coreId, fileId); this.$set(this.supplementTouched, coreId, true); },
    setHistoricalReports(coreId, fileIds) { this.$set(this.historicalAssignments, coreId, Array.isArray(fileIds) ? fileIds : []); this.$set(this.historicalTouched, coreId, true); },
    async scanFiles() {
      if (!this.files.length || this.scanning) return;
      if (this.totalBytes > 160 * 1024 * 1024) { this.error = "这批文件总大小不能超过 160 MiB。"; return; }
      this.scanning = true; this.error = "";
      const data = new FormData();
      this.selectedFiles.forEach((file) => {
        data.append("files", file, file.name);
        data.append("relative_paths", file.webkitRelativePath || file.name);
      });
      try {
        const response = await axios.post("/api/intake/preview", data);
        this.batchId = response.data.batch_id;
        this.documents = response.data.documents || [];
        this.aiMatches = {};
        (response.data.suggested_matches || []).forEach((match) => { this.$set(this.aiMatches, match.core_file_id, match); });
        this.roles = {};
        this.reportAssignments = {}; this.reportTouched = {};
        this.supplementAssignments = {}; this.supplementTouched = {};
        this.historicalAssignments = {}; this.historicalTouched = {};
        this.selectedGroupIds = this.readyGroups.map((group) => group.core.id);
      } catch (error) {
        const detail = error.response && error.response.data && error.response.data.detail;
        this.error = typeof detail === "string" ? detail : "识别文件失败，请检查 PDF 后重试。";
      } finally { this.scanning = false; }
    },
    async submitGroups() {
      if (!this.batchId || !this.selectedReadyGroups.length || this.submitting) return;
      this.submitting = true; this.error = "";
      const roles = {};
      this.documents.forEach((doc) => { roles[doc.id] = this.roleFor(doc); });
      const groups = this.selectedReadyGroups.map((group) => ({
        workflow: group.workflow,
        core_file_id: group.core.id,
        periodic_file_id: group.periodicId,
        prospectus_file_id: group.workflow === "summary" ? group.prospectusId || null : null,
        historical_file_ids: group.workflow === "investment" ? (group.historicalIds || []) : [],
      }));
      try {
        const response = await axios.post("/api/intake/submit", { batch_id: this.batchId, roles, groups });
        if (!response.data.jobs || !response.data.jobs.length) {
          this.error = (response.data.errors || []).map((item) => item.message).join("；") || "没有创建任务。";
          return;
        }
        this.$emit("submitted", response.data);
      } catch (error) {
        const detail = error.response && error.response.data && error.response.data.detail;
        this.error = typeof detail === "string" ? detail : "创建批量任务失败，请重试。";
      } finally { this.submitting = false; }
    },
  },
};
</script>
