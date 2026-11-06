<template>
  <div class="workbench-page">
    <header class="app-topbar">
      <div class="brand-block">
        <div class="brand-logo">审</div>
        <div class="brand-name">
          <strong>基金招募说明书核对</strong>
        </div>
      </div>
      <div class="topbar-actions">
        <el-button type="primary" icon="el-icon-upload2" class="upload-trigger topbar-upload" @click="uploadVisible = true">
          上传文档
        </el-button>
        <el-popover ref="jobPopover" placement="bottom-end" width="360" trigger="click" popper-class="job-notification-popover">
          <div class="job-notification-head">
            <strong>核对消息</strong>
          </div>
          <div v-if="jobLoadError" class="job-notification-error">{{ jobLoadError }}</div>
          <button
            v-for="job in notificationJobs"
            :key="job.job_id"
            type="button"
            class="job-notification-item"
            :class="job.status"
            :disabled="job.status !== 'completed' && job.status !== 'failed'"
            @click="viewJobNotification(job)"
          >
            <span class="job-notification-text">
              <span class="job-notification-heading">
                <i class="job-status-dot" />
                <strong>{{ job.message }}</strong>
                <i v-if="job.status === 'completed'" class="el-icon-arrow-right job-notification-arrow" />
              </span>
              <span class="job-notification-meta">
                <small>{{ jobStatusLabel(job.status) }}</small>
                <small v-if="jobElapsed(job)">执行用时 {{ jobElapsed(job) }}</small>
              </span>
              <small v-if="job.error" class="job-notification-error">{{ job.error }}</small>
              <span v-if="job.steps" class="job-notification-steps">
                <span v-for="step in jobSteps" :key="step.key" class="job-notification-step" :class="job.steps[step.key]">
                  <span class="job-notification-step-name"><i />{{ step.label }}</span>
                  <small>{{ stepStatusLabel(job.steps[step.key]) }}</small>
                </span>
              </span>
            </span>
          </button>
          <div v-if="!notificationJobs.length && !jobLoadError" class="job-notification-empty">暂无未读消息</div>
          <el-badge slot="reference" :value="unreadJobCount" :hidden="!unreadJobCount" class="notification-badge">
            <el-button size="small" plain class="notification-trigger">
              <i class="el-icon-message" /> 核对消息
            </el-button>
          </el-badge>
        </el-popover>
      </div>
    </header>

    <main class="workbench-main" v-loading="taskLoading">
      <nav class="task-detail-nav" aria-label="任务导航">
        <router-link :to="returnPath"><i class="el-icon-arrow-left" /> 核对任务</router-link>
        <span class="detail-nav-divider" />
        <span class="detail-task-id">{{ $route.params.taskId }}</span>
        <el-tag v-if="activeJob" size="small" :type="activeJob.status === 'failed' ? 'danger' : activeJob.status === 'completed' ? 'success' : 'info'">{{ jobStatusLabel(activeJob.status) }}</el-tag>
        <span v-if="jobElapsed(activeJob)" class="detail-elapsed">执行用时 {{ jobElapsed(activeJob) }}</span>
      </nav>
      <el-alert v-if="detailError" :title="detailError" type="error" :closable="false" show-icon class="task-detail-alert" />
      <div v-if="activeJob && !result" class="task-execution-state" :class="{ failed: activeJob.status === 'failed' }" role="status">
        <i :class="activeJob.status === 'failed' ? 'el-icon-circle-close' : 'el-icon-loading'" />
        <strong>{{ activeJob.status === 'failed' ? '核对失败' : activeJob.message || '正在核对' }}</strong>
        <span v-if="activeJob.error">{{ activeJob.error }}</span>
        <div v-if="activeJob.steps" class="detail-step-list">
          <span v-for="step in jobSteps" :key="step.key">{{ step.label }} <strong>{{ stepStatusLabel(activeJob.steps[step.key]) }}</strong></span>
        </div>
      </div>
      <section class="pair-summary" :class="{ 'has-files': prospectusName || reportName }">
        <div class="pair-summary-title"><i class="el-icon-document-checked" /> 当前核对</div>
        <div class="pair-file" :title="prospectusName || '尚未选择招募说明书'">
          <span class="document-type prospectus-type">招</span>
          <span>
            <small>招募说明书</small>
            <strong>{{ prospectusName || '尚未上传' }}</strong>
            <small v-if="prospectusDate">封面日期 {{ prospectusDate }}</small>
          </span>
        </div>
        <i class="el-icon-right pair-arrow" />
        <div class="pair-file" :title="reportName || '尚未选择定期报告'">
          <span class="document-type report-type">报</span>
          <span>
            <small>定期报告</small>
            <strong>{{ reportName || '尚未上传' }}</strong>
            <small v-if="reportDate">{{ reportDateLabel }} {{ reportDate }}</small>
          </span>
        </div>
        <el-tag v-if="result" size="small" :type="tagType(result.status)" class="pair-status">
          {{ statusLabel(result.status) }}
        </el-tag>
      </section>

      <section class="review-layout">
        <aside class="findings-panel">
          <div class="panel-heading">
            <div>
              <h2>核对结果</h2>
            </div>
            <el-tag v-if="result" size="small" :type="tagType(result.status)">{{ statusLabel(result.status) }}</el-tag>
          </div>

          <div v-if="result && result.warnings && result.warnings.length" class="warning-box">
            <i class="el-icon-warning-outline" />
            <div><p v-for="(warning, index) in result.warnings" :key="index">{{ warning }}</p></div>
          </div>

          <div class="findings-subheading">
            <span>核对模块</span>
            <span v-if="result" class="difference-total">差异 {{ result.difference_count || 0 }} 项</span>
          </div>

          <div v-if="result" class="module-list">
            <section
              v-for="(module, moduleIndex) in result.modules"
              :key="module.rule_id"
              class="module-card"
              :class="{ expanded: expandedModuleIndex === moduleIndex }"
            >
              <button
                type="button"
                class="module-toggle"
                :aria-expanded="expandedModuleIndex === moduleIndex"
                @click="toggleModule(moduleIndex)"
              >
                <span class="status-dot" :class="statusClass(module.status)" />
                <span class="module-title">{{ module.title }}</span>
                <span class="module-count">{{ module.differences.length ? module.differences.length + ' 项' : moduleStatusLabel(module) }}</span>
                <i :class="expandedModuleIndex === moduleIndex ? 'el-icon-arrow-up' : 'el-icon-arrow-down'" />
              </button>

              <div v-if="expandedModuleIndex === moduleIndex" class="module-details">
                <p v-if="module.review_message" class="module-empty">
                  {{ module.review_message }}
                </p>
                <p v-else-if="!module.differences.length" class="module-empty">
                  {{ module.status === 'MATCH' ? '已定位字段一致。' : (moduleMessage(module)) }}
                </p>
                <button
                  v-for="(difference, differenceIndex) in module.differences"
                  :key="difference.row_id + ':' + difference.field_key + ':' + differenceIndex"
                  type="button"
                  class="difference-card"
                  :class="{ selected: selectedModuleIndex === moduleIndex && selectedDifferenceIndex === differenceIndex }"
                  @click="selectDifference(moduleIndex, differenceIndex)"
                >
                  <div class="difference-heading">
                    <el-tag size="mini" :type="tagType(difference.status)">{{ statusLabel(difference.status) }}</el-tag>
                    <strong>{{ difference.row_label || '模块' }} / {{ difference.field_label || difference.field_key }}</strong>
                  </div>
                  <div class="value-pair">
                    <div class="value-cell" :class="{ changed: difference.status === 'MISMATCH' }">
                      <small>招募说明书</small><strong>{{ valueText(difference.prospectus) }}</strong>
                    </div>
                    <div class="value-cell" :class="{ changed: difference.status === 'MISMATCH' }">
                      <small>定期报告</small><strong>{{ valueText(difference.periodic_report) }}</strong>
                    </div>
                  </div>
                </button>
              </div>
            </section>
          </div>

          <el-empty
            v-else
            :image-size="74"
            description="暂无核对结果"
            class="findings-empty"
          />

          <div class="legend-note">
            <span><i class="legend-dot mismatch" />披露差异</span>
            <span><i class="legend-dot unresolved" />待人工复核</span>
          </div>
        </aside>

        <section class="document-viewers" aria-label="招募说明书与定期报告并排 PDF 预览">
          <pdf-viewer
            ref="prospectusViewer"
            role-name="prospectus"
            title="招募说明书"
            :document-info="result && result.documents.prospectus"
            :evidences="prospectusEvidences"
          />
          <pdf-viewer
            ref="periodicReportViewer"
            role-name="periodic_report"
            title="定期报告"
            :document-info="result && result.documents.periodic_report"
            :evidences="periodicReportEvidences"
          />
        </section>
      </section>

    </main>

    <upload-dialog :visible.sync="uploadVisible" @submitted="handleJobSubmitted" />
  </div>
</template>

<script>
import axios from "axios";
import PdfViewer from "../components/PdfViewer.vue";
import UploadDialog from "../components/UploadDialog.vue";

const STATUS_LABELS = {
  MATCH: "一致",
  MISMATCH: "存在差异",
  UNRESOLVED: "待人工复核",
  FUND_MISMATCH: "基金身份不一致",
  DATE_MISMATCH: "文件日期不适配",
  UNRESOLVED_STRUCTURE: "结构待复核",
  MISSING_MODULE: "缺少模块",
  MISSING_ROW: "单侧出现",
  MISSING_FIELD: "字段缺失",
  RANK_ORDER_CHANGED: "披露顺序变化",
  UNRESOLVED_ROW_IDENTITY: "行身份待复核",
};

export default {
  name: "ComparisonWorkbench",
  components: { PdfViewer, UploadDialog },
  data() {
    return {
      activeJob: null,
      taskLoading: false,
      taskRequest: 0,
      detailError: "",
      result: null,
      uploadVisible: false,
      expandedModuleIndex: null,
      selectedModuleIndex: null,
      selectedDifferenceIndex: null,
      jobs: [],
      jobSteps: [
        { key: "preflight", label: "产品与日期检查" },
        { key: "prospectus", label: "招募说明书" },
        { key: "periodic_report", label: "定期报告" },
        { key: "comparison", label: "结果比对" },
      ],
      clockNow: Date.now(),
      readJobIds: loadReadJobIds(),
      jobNotifications: {},
      jobsTimer: null,
      elapsedTimer: null,
      jobsRefreshing: false,
      jobsInitialized: false,
      jobLoadError: "",
      trackedJobIds: [],
      notifiedJobIds: [],
    };
  },
  computed: {
    unreadJobCount() {
      return this.jobs.filter((job) =>
        (job.status === "completed" || job.status === "failed") && this.readJobIds.indexOf(job.job_id) < 0
      ).length;
    },
    notificationJobs() {
      return this.jobs.filter((job) =>
        job.status === "queued" || job.status === "running" ||
        ((job.status === "completed" || job.status === "failed") && this.readJobIds.indexOf(job.job_id) < 0)
      );
    },
    returnPath() {
      const path = this.$route.query.returnTo;
      return typeof path === "string" && (path === "/" || path.indexOf("/?") === 0) ? path : "/";
    },
    prospectusName() {
      return (this.result && this.result.documents.prospectus.filename) || (this.activeJob && this.activeJob.document_names && this.activeJob.document_names.prospectus) || "";
    },
    reportName() {
      return (this.result && this.result.documents.periodic_report.filename) || (this.activeJob && this.activeJob.document_names && this.activeJob.document_names.periodic_report) || "";
    },
    prospectusDate() {
      return this.documentDisplayDate("prospectus");
    },
    reportDate() {
      return this.documentDisplayDate("periodic_report");
    },
    reportDateLabel() {
      const metadata = this.documentMetadata("periodic_report");
      return metadata && metadata.display_date_label || "报告期末";
    },
    selectedDifference() {
      if (this.selectedModuleIndex === null || this.selectedDifferenceIndex === null || !this.result) return null;
      const module = this.result.modules[this.selectedModuleIndex];
      return module && module.differences[this.selectedDifferenceIndex] || null;
    },
    prospectusEvidences() {
      return this.evidenceListFor("prospectus");
    },
    periodicReportEvidences() {
      return this.evidenceListFor("periodic_report");
    },
  },
  watch: {
    "$route.params.taskId": {
      immediate: true,
      handler() {
        this.result = null;
        this.activeJob = null;
        this.expandedModuleIndex = this.selectedModuleIndex = this.selectedDifferenceIndex = null;
        this.loadTask();
      },
    },
  },
  mounted() {
    this.refreshJobs();
    this.jobsTimer = window.setInterval(this.refreshJobs, 3000);
    this.elapsedTimer = window.setInterval(() => { this.clockNow = Date.now(); }, 1000);
  },
  beforeDestroy() {
    if (this.jobsTimer) window.clearInterval(this.jobsTimer);
    if (this.elapsedTimer) window.clearInterval(this.elapsedTimer);
  },
  methods: {
    async loadTask(silent) {
      const taskId = this.$route.params.taskId;
      if (!taskId) return;
      const request = ++this.taskRequest;
      if (!silent) this.taskLoading = true;
      try {
        const response = await axios.get("/api/jobs/" + encodeURIComponent(taskId));
        if (request !== this.taskRequest) return;
        this.activeJob = response.data;
        this.result = response.data.result || null;
        this.detailError = "";
        if (response.data.status === "completed" || response.data.status === "failed") this.markJobRead(taskId);
      } catch (error) {
        if (request !== this.taskRequest) return;
        this.detailError = error.response && error.response.data && error.response.data.detail || "无法读取核对任务，请稍后重试。";
      } finally {
        if (request === this.taskRequest) this.taskLoading = false;
      }
    },
    documentMetadata(role) {
      if (!this.result) return null;
      const preflight = this.result.preflight || {};
      return preflight[role] || null;
    },
    documentDisplayDate(role) {
      const metadata = this.documentMetadata(role);
      const interval = metadata && (metadata.display_date_interval || metadata.date_interval);
      if (!interval || !interval.start) return "";
      const start = interval.start.split("-").map(Number);
      const end = interval.end && interval.end.split("-").map(Number);
      if (start.length < 2 || start.some(Number.isNaN)) return "";
      const year = start[0];
      const month = start[1];
      if (interval.start === interval.end && start.length === 3) {
        return `${year}年${month}月${start[2]}日`;
      }
      return `${year}年${month}月`;
    },
    evidenceListFor(role) {
      if (!this.result || this.selectedModuleIndex === null) return [];
      const module = this.result.modules[this.selectedModuleIndex];
      if (!module) return [];
      const differences = this.selectedDifference ? [this.selectedDifference] : module.differences;
      return differences
        .map((difference) => {
          const source = difference[role];
          const evidence = source && source.evidence;
          if (!evidence || !evidence.page) return null;
          const rowLabel = difference.row_label || "模块";
          const fieldLabel = difference.field_label || difference.field_key || "指标";
          const needsReview = ["UNRESOLVED", "UNRESOLVED_ROW_IDENTITY", "UNRESOLVED_STRUCTURE"].indexOf(difference.status) >= 0;
          return Object.assign({}, evidence, {
            difference_status: difference.status,
            field_label: needsReview ? rowLabel + "（待复核）" : rowLabel + " / " + fieldLabel,
            id: difference.row_id + ":" + difference.field_key,
          });
        })
        .filter(Boolean)
        .sort((left, right) => Number(left.page) - Number(right.page));
    },
    jobStatusLabel(status) {
      return { queued: "排队中", running: "核对中", completed: "已完成", failed: "失败" }[status] || "待处理";
    },
    stepStatusLabel(status) {
      return { queued: "等待", running: "进行中", completed: "完成", failed: "失败", interrupted: "中断", skipped: "跳过" }[status] || "等待";
    },
    jobElapsed(job) {
      if (!job || !job.started_at) return "";
      const start = Date.parse(job.started_at);
      const end = job.completed_at ? Date.parse(job.completed_at) : this.clockNow;
      if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return "";
      const seconds = Math.floor((end - start) / 1000);
      const hours = Math.floor(seconds / 3600);
      const minutes = Math.floor((seconds % 3600) / 60);
      const remainder = seconds % 60;
      const twoDigits = (value) => String(value).padStart(2, "0");
      return hours ? `${hours}:${twoDigits(minutes)}:${twoDigits(remainder)}` : `${twoDigits(minutes)}:${twoDigits(remainder)}`;
    },
    async refreshJobs() {
      if (this.jobsRefreshing) return;
      this.jobsRefreshing = true;
      try {
        const previous = {};
        this.jobs.forEach((job) => { previous[job.job_id] = job; });
        const response = await axios.get("/api/jobs");
        this.jobs = response.data.jobs || [];
        this.jobLoadError = "";
        this.jobs.forEach((job) => {
          if (job.job_id === this.$route.params.taskId) return;
          const wasCompleted = previous[job.job_id] && previous[job.job_id].status === "completed";
          const isTracked = this.trackedJobIds.indexOf(job.job_id) >= 0;
          if (
            job.status === "completed" && !wasCompleted &&
            this.notifiedJobIds.indexOf(job.job_id) < 0 &&
            (this.jobsInitialized && previous[job.job_id] || isTracked)
          ) {
            this.notifiedJobIds.push(job.job_id);
            let notification;
            notification = this.$notify({
              title: "基金核对完成",
              message: "点击此消息查看核对结果",
              type: "success",
              duration: 0,
              onClick: () => {
                notification.close();
                this.viewJobNotification(job);
              },
            });
            this.$set(this.jobNotifications, job.job_id, notification);
          }
        });
        this.jobsInitialized = true;
        if (!this.activeJob || ["queued", "running"].indexOf(this.activeJob.status) >= 0) await this.loadTask(true);
      } catch (error) {
        this.jobLoadError = error.message || "暂时无法获取核对任务状态";
      } finally {
        this.jobsRefreshing = false;
      }
    },
    markJobRead(jobId) {
      if (!jobId || this.readJobIds.indexOf(jobId) >= 0) return;
      this.readJobIds = this.readJobIds.concat(jobId);
      try {
        window.localStorage.setItem("prospectus-checker-read-jobs", JSON.stringify(this.readJobIds));
      } catch (error) {
        // Keep the current-page unread count working if browser storage is unavailable.
      }
    },
    closeJobNotification(job) {
      const notification = this.jobNotifications[job.job_id];
      if (notification) {
        notification.close();
        this.$delete(this.jobNotifications, job.job_id);
      }
      const popover = this.$refs.jobPopover;
      if (popover) popover.showPopper = false;
    },
    async viewJobNotification(job) {
      if (!job || (job.status !== "completed" && job.status !== "failed")) return;
      const opened = await this.openJob(job);
      if (opened) {
        this.markJobRead(job.job_id);
        this.closeJobNotification(job);
      }
    },
    async openJob(job) {
      if (!job || !job.job_id) return false;
      const path = "/tasks/" + encodeURIComponent(job.job_id);
      if (path === this.$route.path) await this.loadTask();
      else this.$router.push({ path, query: this.$route.query });
      return true;
    },
    statusLabel(status) {
      return STATUS_LABELS[status] || status || "待复核";
    },
    moduleStatusLabel(module) {
      if (module.status === "UNRESOLVED_STRUCTURE" && !module.differences.length) return "证据待复核";
      return this.statusLabel(module.status);
    },
    statusClass(status) {
      return String(status || "").toLowerCase().replace(/_/g, "-");
    },
    tagType(status) {
      if (status === "MATCH") return "success";
      if (["MISMATCH", "MISSING_ROW", "MISSING_FIELD", "MISSING_MODULE"].indexOf(status) >= 0) return "danger";
      if (["FUND_MISMATCH", "DATE_MISMATCH", "UNRESOLVED", "UNRESOLVED_STRUCTURE", "UNRESOLVED_ROW_IDENTITY"].indexOf(status) >= 0) return "warning";
      return "info";
    },
    moduleMessage(module) {
      if (module.status === "UNRESOLVED_STRUCTURE" && !module.differences.length) {
        return "已定位字段未发现差异；部分行证据未能精确定位，请复核 PDF 原文。";
      }
      if (module.status === "UNRESOLVED_STRUCTURE") return "模块结构或字段证据不足，需人工复核。";
      if (module.status === "MISSING_MODULE") return "未能识别到对应章节。";
      return "没有可展示的差异明细。";
    },
    valueText(value) {
      if (!value) return "—";
      return value.raw_text || value.normalized_value || "—";
    },
    toggleModule(moduleIndex) {
      this.expandedModuleIndex = this.expandedModuleIndex === moduleIndex ? null : moduleIndex;
      this.selectedModuleIndex = moduleIndex;
      this.selectedDifferenceIndex = null;
      this.$nextTick(() => {
        this.navigateToModule(moduleIndex, "prospectus");
        this.navigateToModule(moduleIndex, "periodic_report");
      });
    },
    navigateToModule(moduleIndex, role) {
      if (!this.result) return;
      const module = this.result.modules[moduleIndex];
      const rows = module && module[role === "prospectus" ? "prospectus_rows" : "periodic_rows"] || [];
      const evidence = rows.reduce((found, row) => {
        if (found) return found;
        const rowEvidence = row.row_evidence || row.rank_evidence;
        if (rowEvidence && rowEvidence.page) return rowEvidence;
        const fields = Object.values(row.fields || {});
        return fields.map((field) => field.evidence).find((item) => item && item.page) || null;
      }, null);
      const viewer = this.$refs[role === "prospectus" ? "prospectusViewer" : "periodicReportViewer"];
      if (viewer && evidence) viewer.scrollToPage(evidence.page);
    },
    selectDifference(moduleIndex, differenceIndex) {
      this.expandedModuleIndex = moduleIndex;
      this.selectedModuleIndex = moduleIndex;
      this.selectedDifferenceIndex = differenceIndex;
    },
    handleJobSubmitted(job) {
      this.trackedJobIds.push(job.job_id);
      this.$router.push({ path: "/tasks/" + encodeURIComponent(job.job_id), query: this.$route.query });
    },
  },
};

function loadReadJobIds() {
  try {
    const saved = JSON.parse(window.localStorage.getItem("prospectus-checker-read-jobs") || "[]");
    return Array.isArray(saved) ? saved : [];
  } catch (error) {
    return [];
  }
}
</script>
