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

    <main class="workbench-main">
      <section class="pair-summary" :class="{ 'has-files': prospectusName || reportName }">
        <div class="pair-summary-title"><i class="el-icon-document-checked" /> 当前核对</div>
        <div class="pair-file" :title="prospectusName || '尚未选择招募说明书'">
          <span class="document-type prospectus-type">招</span>
          <span><small>招募说明书</small><strong>{{ prospectusName || '尚未上传' }}</strong></span>
        </div>
        <i class="el-icon-right pair-arrow" />
        <div class="pair-file" :title="reportName || '尚未选择定期报告'">
          <span class="document-type report-type">报</span>
          <span><small>定期报告</small><strong>{{ reportName || '尚未上传' }}</strong></span>
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
                <p v-if="!module.differences.length" class="module-empty">
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
            role-name="prospectus"
            title="招募说明书"
            :document-info="result && result.documents.prospectus"
            :evidences="prospectusEvidences"
          />
          <pdf-viewer
            role-name="periodic_report"
            title="定期报告"
            :document-info="result && result.documents.periodic_report"
            :evidences="periodicReportEvidences"
          />
        </section>
      </section>

    </main>

    <el-dialog
      title="上传核对文档"
      :visible.sync="uploadVisible"
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
        <el-button @click="uploadVisible = false">取消</el-button>
        <el-button type="primary" :loading="loading" :disabled="!canCompare" @click="compareDocuments">
          {{ loading ? '正在核对' : '开始核对' }} <i v-if="!loading" class="el-icon-right" />
        </el-button>
      </span>
    </el-dialog>
  </div>
</template>

<script>
import axios from "axios";
import PdfViewer from "../components/PdfViewer.vue";

const STATUS_LABELS = {
  MATCH: "一致",
  MISMATCH: "存在差异",
  UNRESOLVED: "待人工复核",
  FUND_MISMATCH: "基金身份不一致",
  UNRESOLVED_STRUCTURE: "结构待复核",
  MISSING_MODULE: "缺少模块",
  MISSING_ROW: "单侧出现",
  MISSING_FIELD: "字段缺失",
  RANK_ORDER_CHANGED: "披露顺序变化",
  UNRESOLVED_ROW_IDENTITY: "行身份待复核",
};

export default {
  name: "ComparisonWorkbench",
  components: { PdfViewer },
  data() {
    return {
      files: { prospectus: null, periodic_report: null },
      result: null,
      uploadVisible: false,
      loading: false,
      progressText: "正在识别并核对",
      errorMessage: "",
      dragTarget: null,
      expandedModuleIndex: null,
      selectedModuleIndex: null,
      selectedDifferenceIndex: null,
      jobs: [],
      jobSteps: [
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
    canCompare() {
      return !!(this.files.prospectus && this.files.periodic_report && !this.loading);
    },
    prospectusName() {
      return (this.result && this.result.documents.prospectus.filename) || (this.files.prospectus && this.files.prospectus.name) || "";
    },
    reportName() {
      return (this.result && this.result.documents.periodic_report.filename) || (this.files.periodic_report && this.files.periodic_report.name) || "";
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
      return { queued: "等待", running: "进行中", completed: "完成", failed: "失败", interrupted: "中断" }[status] || "等待";
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
      if (job.status === "failed") {
        this.markJobRead(job.job_id);
        this.closeJobNotification(job);
        this.$message.error(job.error || "核对任务失败");
        return;
      }
      const opened = await this.openJob(job);
      if (opened) {
        this.markJobRead(job.job_id);
        this.closeJobNotification(job);
      }
    },
    async openJob(job) {
      if (!job || job.status !== "completed") return false;
      try {
        const response = await axios.get("/api/jobs/" + encodeURIComponent(job.job_id));
        this.result = response.data.result;
        this.expandedModuleIndex = null;
        this.selectedModuleIndex = null;
        this.selectedDifferenceIndex = null;
        this.uploadVisible = false;
        this.$message.success("已打开核对结果");
        return true;
      } catch (error) {
        const detail = error.response && error.response.data && error.response.data.detail;
        this.$message.error(detail || "无法读取核对结果");
        return false;
      }
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
      if (["FUND_MISMATCH", "UNRESOLVED", "UNRESOLVED_STRUCTURE", "UNRESOLVED_ROW_IDENTITY"].indexOf(status) >= 0) return "warning";
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
    fileSize(bytes) {
      return (bytes / 1024 / 1024).toFixed(2) + " MB";
    },
    setFile(role, file) {
      if (!file) return;
      if (!/\.pdf$/i.test(file.name) || (file.type && file.type !== "application/pdf")) {
        this.errorMessage = "仅支持 PDF 文件。";
        return;
      }
      if (file.size > 40 * 1024 * 1024) {
        this.errorMessage = "单个 PDF 不能超过 40 MiB。";
        return;
      }
      this.files[role] = file;
      this.errorMessage = "";
      this.result = null;
      this.expandedModuleIndex = null;
      this.selectedModuleIndex = null;
      this.selectedDifferenceIndex = null;
    },
    handleFileChange(role, event) {
      this.setFile(role, event.target.files && event.target.files[0]);
      event.target.value = "";
    },
    handleDrop(role, event) {
      this.dragTarget = null;
      this.setFile(role, event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0]);
    },
    toggleModule(moduleIndex) {
      this.expandedModuleIndex = this.expandedModuleIndex === moduleIndex ? null : moduleIndex;
      this.selectedModuleIndex = moduleIndex;
      this.selectedDifferenceIndex = null;
    },
    selectDifference(moduleIndex, differenceIndex) {
      this.expandedModuleIndex = moduleIndex;
      this.selectedModuleIndex = moduleIndex;
      this.selectedDifferenceIndex = differenceIndex;
    },
    async compareDocuments() {
      if (!this.canCompare) return;
      this.loading = true;
      this.errorMessage = "";
      const formData = new FormData();
      formData.append("prospectus", this.files.prospectus);
      formData.append("periodic_report", this.files.periodic_report);
      try {
        const response = await axios.post("/api/jobs", formData);
        const jobId = response.data.job_id;
        if (jobId) this.trackedJobIds.push(jobId);
        this.uploadVisible = false;
        this.$message.success("核对任务已提交，可在右上角查看进度和结果");
        await this.refreshJobs();
      } catch (error) {
        const detail = error.response && error.response.data && error.response.data.detail;
        this.errorMessage = detail || error.message || "提交核对任务失败，请稍后重试。";
      } finally {
        this.loading = false;
      }
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
