<template>
  <div class="task-list-page">
    <app-topbar />
    <main class="task-list-main">
      <section class="task-list-surface" aria-label="历史核对任务">
        <form class="task-query-form" @submit.prevent="search">
          <el-input v-model="keyword" placeholder="基金名称 / 文件名" aria-label="基金名称或文件名" clearable prefix-icon="el-icon-search" class="task-keyword" maxlength="200" />
          <el-select v-model="workflowId" clearable placeholder="全部任务类型" aria-label="任务类型"><el-option v-for="item in workflowTypes" :key="item.value" :value="item.value" :label="item.label" /></el-select>
          <el-select v-model="status" clearable placeholder="全部任务状态" aria-label="任务状态"><el-option v-for="item in states" :key="item.value" :value="item.value" :label="item.label" /></el-select>
          <el-select v-model="resultStatus" clearable placeholder="全部核对结果" aria-label="核对结果"><el-option v-for="item in results" :key="item.value" :value="item.value" :label="item.label" /></el-select>
          <el-date-picker v-model="reportDates" type="daterange" value-format="yyyy-MM-dd" format="yyyy-MM-dd" range-separator="至" start-placeholder="报告开始日期" end-placeholder="报告结束日期" aria-label="报告区间" />
          <div class="task-query-actions">
            <el-button type="primary" native-type="submit">查询</el-button>
            <el-button @click="reset">重置</el-button>
            <el-button
              v-if="selectedJobIds.length"
              type="danger"
              plain
              icon="el-icon-delete"
              :loading="batchDeleting"
              @click="batchDeleteTasks"
            >
              批量删除 ({{ selectedJobIds.length }})
            </el-button>
            <el-button type="primary" icon="el-icon-plus" @click="uploadVisible = true">新增核对</el-button>
          </div>
        </form>
        <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="task-query-error" />
        <div class="task-table-scroll" v-loading="loading">
          <table class="task-table">
            <thead><tr><th scope="col" class="task-select-column"><input type="checkbox" aria-label="全选当前页可删除任务" :checked="allSelectableSelected" :indeterminate.prop="someSelectableSelected" :disabled="!selectableTaskIds.length" @change="toggleSelectAll($event.target.checked)"></th><th scope="col" class="task-files-column">核对报告</th><th scope="col" class="task-type-column">任务类型</th><th scope="col" class="task-product-column">基金名称</th><th scope="col" class="task-dates-column">报告日期</th><th scope="col" class="task-state-column">任务状态</th><th scope="col" class="task-result-column">核对结果</th><th scope="col" class="task-created-column">创建时间</th><th scope="col" class="task-duration-column">执行用时</th><th scope="col" class="task-action-column">操作</th></tr></thead>
            <tbody>
              <tr v-for="task in tasks" :key="task.job_id">
                <td class="task-select-cell">
                  <input
                    type="checkbox"
                    :aria-label="'选择任务 ' + task.job_id"
                    :checked="selectedJobIds.indexOf(task.job_id) >= 0"
                    :disabled="['queued', 'running'].includes(task.status) || batchDeleting"
                    @change="toggleSelectTask(task.job_id, $event.target.checked)"
                  >
                </td>
                <td class="task-files-cell">
                  <div v-for="role in documentRolesFor(task)" v-if="fileName(task, role.key) !== '未记录文件名'" :key="role.key" class="task-document-row" :title="fileName(task, role.key)">
                    <span class="task-document-icon" :class="role.key">{{ role.icon }}</span><span class="task-document-label">{{ role.label }}</span><span class="task-document-name">{{ fileName(task, role.key) }}</span>
                  </div>
                </td>
                <td class="task-type-cell">{{ workflowLabel(task) }}</td>
                <td class="task-product-cell">
                  <span class="task-product-name" :title="task.fund_name || '未识别基金名称'">{{ task.fund_name || '未识别' }}</span>
                  <small v-if="task.fund_code">{{ task.fund_code }}</small>
                </td>
                <td class="task-report-dates-cell">
                  <div v-for="role in documentRolesFor(task)" v-if="fileName(task, role.key) !== '未记录文件名'" :key="role.key" class="task-report-date-row" :title="dateLabel(task, role.key)">
                    <span class="task-report-date-label">{{ role.label }}</span><span class="task-report-date-value">{{ reportDate(task, role.key) }}</span>
                  </div>
                </td>
                <td><span class="task-state-tag" :class="task.status"><i />{{ stateLabel(task.status) }}</span></td>
                <td>
                  <div class="task-result-list">
                    <span
                      v-for="(badge, index) in resultBadges(task)"
                      :key="index"
                      class="task-result"
                      :class="badge.cls"
                    >{{ badge.text }}</span>
                  </div>
                </td>
                <td class="task-time-cell">{{ formatDate(task.created_at) }}</td>
                <td class="task-duration-cell">{{ elapsed(task) }}</td>
                <td class="task-action-cell">
                  <div class="task-action-links">
                    <router-link class="task-detail-link" :to="detailLink(task)">查看详情<i class="el-icon-arrow-right" /></router-link>
                    <button
                      type="button"
                      class="task-rerun-button"
                      :disabled="['queued', 'running'].includes(task.status) || rerunningId === task.job_id"
                      @click="rerunTask(task)"
                    >
                      <i :class="rerunningId === task.job_id ? 'el-icon-loading' : 'el-icon-refresh-right'" />
                      {{ rerunningId === task.job_id ? '提交中' : '重新核对' }}
                    </button>
                    <button
                      type="button"
                      class="task-delete-button"
                      :disabled="['queued', 'running'].includes(task.status) || deletingId === task.job_id"
                      @click="deleteTask(task)"
                    >
                      <i :class="deletingId === task.job_id ? 'el-icon-loading' : 'el-icon-delete'" />
                      {{ deletingId === task.job_id ? '删除中' : '删除' }}
                    </button>
                  </div>
                </td>
              </tr>
              <tr v-if="!tasks.length && !loading"><td colspan="10" class="task-empty"><i class="el-icon-document" /><strong>{{ error ? '任务列表暂时不可用' : '暂无核对任务' }}</strong><span v-if="!error">{{ hasFilters ? '请调整查询条件' : '点击新增核对发起任务' }}</span></td></tr>
            </tbody>
          </table>
        </div>
        <footer class="task-list-footer"><span>{{ total ? '第 ' + ((page - 1) * pageSize + 1) + '–' + Math.min(page * pageSize, total) + ' 项，共 ' + total + ' 项' : '共 0 项' }}</span><el-pagination background layout="sizes, prev, pager, next" :page-sizes="[10, 20, 50]" :page-size="pageSize" :current-page="page" :total="total" @current-change="changePage" @size-change="changeSize" /></footer>
      </section>
    </main>
    <upload-dialog :visible.sync="uploadVisible" @submitted="taskCreated" @batch-submitted="batchTasksCreated" />
  </div>
</template>

<script>
import axios from "axios";
import AppTopbar from "../components/AppTopbar.vue";
import UploadDialog from "../components/UploadDialog.vue";

export default {
  name: "TaskList",
  components: { AppTopbar, UploadDialog },
  data() {
    return {
      tasks: [], total: 0, loading: false, error: "", keyword: "", workflowId: "", status: "", resultStatus: "", reportDates: [], page: 1, pageSize: 10,
      uploadVisible: false, rerunningId: "", deletingId: "", selectedJobIds: [], batchDeleting: false, clockNow: Date.now(), pollTimer: null, clockTimer: null, requestId: 0, refreshing: false,
      workflowTypes: [{ value: "product-summary-verification", label: "产品概要" }, { value: "prospectus-periodic-comparison", label: "招募说明书" }],
      states: [{ value: "queued", label: "排队中" }, { value: "running", label: "核对中" }, { value: "completed", label: "已完成" }, { value: "failed", label: "失败" }],
      results: [{ value: "MATCH", label: "一致" }, { value: "MISMATCH", label: "存在差异" }, { value: "UNRESOLVED", label: "待人工复核" }],
      documentRoles: [{ key: "prospectus", icon: "招", label: "招募说明书" }, { key: "periodic_report", icon: "定", label: "定期报告" }, { key: "product_summary", icon: "概", label: "产品概要" }, { key: "annual_report", icon: "年", label: "年度报告" }],
    };
  },
  computed: {
    hasFilters() { return !!(this.keyword || this.workflowId || this.status || this.resultStatus || this.reportDates && this.reportDates.length); },
    selectableTaskIds() { return this.tasks.filter((task) => !["queued", "running"].includes(task.status)).map((task) => task.job_id); },
    allSelectableSelected() { return this.selectableTaskIds.length > 0 && this.selectableTaskIds.every((id) => this.selectedJobIds.indexOf(id) >= 0); },
    someSelectableSelected() { return !this.allSelectableSelected && this.selectableTaskIds.some((id) => this.selectedJobIds.indexOf(id) >= 0); },
  },
  watch: { "$route.query": { immediate: true, handler() { this.readQuery(); this.fetchTasks(); } } },
  mounted() {
    this.pollTimer = window.setInterval(() => { if (!document.hidden && !this.refreshing) this.fetchTasks(true); }, 5000);
    this.clockTimer = window.setInterval(() => { this.clockNow = Date.now(); }, 1000);
  },
  beforeDestroy() { window.clearInterval(this.pollTimer); window.clearInterval(this.clockTimer); this.requestId += 1; },
  methods: {
    readQuery() {
      const q = this.$route.query;
      this.keyword = String(q.q || ""); this.workflowId = q.workflow_id === "historical-performance-verification" ? "prospectus-periodic-comparison" : q.workflow_id || ""; this.status = q.status || ""; this.resultStatus = q.result_status || "";
      this.reportDates = q.report_start && q.report_end ? [q.report_start, q.report_end] : [];
      this.page = Math.max(1, Math.floor(Number(q.page) || 1));
      this.pageSize = [10, 20, 50].indexOf(Number(q.page_size)) >= 0 ? Number(q.page_size) : 10;
    },
    query() {
      const query = { page: String(this.page), page_size: String(this.pageSize) };
      if (this.keyword.trim()) query.q = this.keyword.trim();
      if (this.workflowId) query.workflow_id = this.workflowId;
      if (this.status) query.status = this.status;
      if (this.resultStatus) query.result_status = this.resultStatus;
      if (this.reportDates && this.reportDates.length === 2) { query.report_start = this.reportDates[0]; query.report_end = this.reportDates[1]; }
      return query;
    },
    applyQuery() {
      const location = { path: "/", query: this.query() };
      if (this.$router.resolve(location).route.fullPath === this.$route.fullPath) this.fetchTasks();
      else this.$router.push(location);
    },
    search() { this.page = 1; this.applyQuery(); },
    reset() { this.keyword = this.workflowId = this.status = this.resultStatus = ""; this.reportDates = []; this.page = 1; this.applyQuery(); },
    changePage(page) { this.page = page; this.applyQuery(); },
    changeSize(size) { this.pageSize = size; this.page = 1; this.applyQuery(); },
    async fetchTasks(silent) {
      const request = ++this.requestId;
      this.refreshing = true;
      if (!silent) this.loading = true;
      try {
        const response = await axios.get("/api/tasks", { params: { ...this.$route.query, workflow_id: this.workflowId || undefined } });
        if (request !== this.requestId) return;
        this.tasks = response.data.tasks || []; this.total = response.data.total || 0; this.error = "";
        if (!this.tasks.length && this.total && this.page > 1) { this.page = Math.ceil(this.total / this.pageSize); this.applyQuery(); }
      } catch (error) {
        if (request !== this.requestId) return;
        const detail = error.response && error.response.data && error.response.data.detail;
        this.error = typeof detail === "string" ? detail : "无法加载任务列表，请重试。";
      } finally { if (request === this.requestId) { this.loading = false; this.refreshing = false; } }
    },
    detailLink(task) { return { path: "/tasks/" + encodeURIComponent(task.job_id), query: { returnTo: this.$route.fullPath } }; },
    taskCreated(task) { this.$router.push(this.detailLink(task)); },
    async rerunTask(task) {
      if (!task || !task.job_id || this.rerunningId) return;
      this.rerunningId = task.job_id;
      try {
        await axios.post("/api/jobs/" + encodeURIComponent(task.job_id) + "/rerun");
        this.$message.success("已提交重新核对任务");
        this.page = 1;
        await this.fetchTasks();
      } catch (error) {
        const detail = error.response && error.response.data && error.response.data.detail;
        this.$message.error(typeof detail === "string" ? detail : "重新核对失败，请重试。");
      } finally {
        this.rerunningId = "";
      }
    },
    async deleteTask(task) {
      if (!task || !task.job_id || this.deletingId) return;
      try {
        await this.$confirm("删除后不可恢复，确认删除该核对任务？", "删除任务", {
          confirmButtonText: "删除",
          cancelButtonText: "取消",
          type: "warning",
        });
      } catch (_) {
        return;
      }
      this.deletingId = task.job_id;
      try {
        await axios.delete("/api/jobs/" + encodeURIComponent(task.job_id));
        this.selectedJobIds = this.selectedJobIds.filter((id) => id !== task.job_id);
        this.$message.success("已删除核对任务");
        await this.fetchTasks();
      } catch (error) {
        const detail = error.response && error.response.data && error.response.data.detail;
        this.$message.error(typeof detail === "string" ? detail : "删除失败，请重试。");
      } finally {
        this.deletingId = "";
      }
    },
    toggleSelectTask(jobId, checked) {
      if (checked) {
        if (this.selectedJobIds.indexOf(jobId) < 0) this.selectedJobIds = [...this.selectedJobIds, jobId];
      } else {
        this.selectedJobIds = this.selectedJobIds.filter((id) => id !== jobId);
      }
    },
    toggleSelectAll(checked) {
      const pageIds = this.selectableTaskIds;
      if (checked) {
        const next = new Set([...this.selectedJobIds, ...pageIds]);
        this.selectedJobIds = Array.from(next);
      } else {
        this.selectedJobIds = this.selectedJobIds.filter((id) => pageIds.indexOf(id) < 0);
      }
    },
    async batchDeleteTasks() {
      if (!this.selectedJobIds.length || this.batchDeleting) return;
      const count = this.selectedJobIds.length;
      try {
        await this.$confirm(`删除后不可恢复，确认删除选中的 ${count} 个核对任务？`, "批量删除任务", {
          confirmButtonText: "删除",
          cancelButtonText: "取消",
          type: "warning",
        });
      } catch (_) {
        return;
      }
      this.batchDeleting = true;
      try {
        const response = await axios.post("/api/jobs/batch-delete", { job_ids: this.selectedJobIds });
        const deleted = (response.data && response.data.deleted_ids) || [];
        const skipped = (response.data && response.data.skipped) || [];
        this.selectedJobIds = [];
        if (skipped.length) {
          this.$message.warning(`已删除 ${deleted.length} 个任务，另有 ${skipped.length} 个未删除`);
        } else {
          this.$message.success(`已删除 ${deleted.length} 个核对任务`);
        }
        await this.fetchTasks();
      } catch (error) {
        const detail = error.response && error.response.data && error.response.data.detail;
        this.$message.error(typeof detail === "string" ? detail : "批量删除失败，请重试。");
      } finally {
        this.batchDeleting = false;
      }
    },
    batchTasksCreated(result) {
      this.page = 1;
      this.fetchTasks();
      const failed = result.errors && result.errors.length;
      this.$message({ type: failed ? "warning" : "success", message: failed ? "已创建 " + result.jobs.length + " 个任务，另有 " + failed + " 组未创建。" : "已创建 " + result.jobs.length + " 个核对任务。" });
    },
    fileName(task, role) { return task.document_names && task.document_names[role] || "未记录文件名"; },
    dateLabel(task, role) { const item = task.document_dates && task.document_dates[role]; return item && item.label || "报告日期"; },
    reportDate(task, role) {
      const item = task.document_dates && task.document_dates[role];
      if (!item || !item.start) return ["queued", "running"].includes(task.status) ? "识别中" : "未识别";
      const start = item.start, end = item.end || start;
      if (start === end) return start;
      const startMatch = /^(\d{4})-(\d{2})-01$/.exec(start);
      if (startMatch && end === startMatch[1] + "-" + startMatch[2] + "-" + String(new Date(Number(startMatch[1]), Number(startMatch[2]), 0).getDate()).padStart(2, "0")) return startMatch[1] + "-" + startMatch[2];
      return start + " 至 " + end;
    },
    documentRolesFor(task) {
      const dynamic = Object.keys(task.document_names || {}).filter((key) => /^historical_report_\d{3}$/.test(key)).sort().map((key, index) => ({ key, icon: "定", label: "历史报告 " + (index + 1) }));
      return [...this.documentRoles.filter((role) => role.key !== "periodic_report"),
        ...(dynamic.length ? dynamic : this.documentRoles.filter((role) => role.key === "periodic_report"))];
    },
    workflowLabel(task) { const item = this.workflowTypes.find((t) => t.value === (task.workflow_id || "prospectus-periodic-comparison")); return item ? item.label : "招募说明书"; },
    stateLabel(status) { const item = this.states.find((item) => item.value === status); return item ? item.label : "未知状态"; },
    resultBadges(task) {
      if (task.status === "failed") return [{ text: "核对失败", cls: "danger" }];
      if (task.status !== "completed") return [{ text: "等待结果", cls: "muted" }];
      const diffCount = task.difference_count || 0;
      const reviewCount = (task.unresolved_count || 0) + (task.not_checked_count || 0);
      if (task.result_status === "NO_COMMON_CONTENT" || task.result_status === "SKIPPED") {
        return [{ text: "无可比较内容", cls: "muted" }];
      }
      if (task.result_status === "INCOMPATIBLE") {
        return [{ text: "文件不适配", cls: "muted" }];
      }
      if (task.result_status === "MISMATCH") {
        const badges = [{ text: "差异 " + diffCount + " 项", cls: "danger" }];
        if (reviewCount > 0) badges.push({ text: "待复核 " + reviewCount + " 项", cls: "warning" });
        return badges;
      }
      if (task.result_status === "UNRESOLVED" || task.result_status === "NOT_CHECKED") {
        const badges = [];
        if (diffCount > 0) badges.push({ text: "差异 " + diffCount + " 项", cls: "danger" });
        badges.push({ text: reviewCount ? "待复核 " + reviewCount + " 项" : "有待复核项", cls: "warning" });
        return badges;
      }
      if (task.result_status === "MATCH") {
        const badges = [{ text: "一致", cls: "success" }];
        if (reviewCount > 0) badges.push({ text: "待复核 " + reviewCount + " 项", cls: "warning" });
        return badges;
      }
      const item = this.results.find((entry) => entry.value === task.result_status);
      const badges = [{ text: item ? item.label : "未记录结果", cls: item ? "warning" : "muted" }];
      if (reviewCount > 0) badges.push({ text: "待复核 " + reviewCount + " 项", cls: "warning" });
      return badges;
    },
    formatDate(value) { const date = new Date(value); return value && !Number.isNaN(date.getTime()) ? date.toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).replace(/\//g, "-") : "—"; },
    elapsed(task) {
      if (!task.started_at) return "—";
      const start = Date.parse(task.started_at), end = task.completed_at ? Date.parse(task.completed_at) : this.clockNow;
      if (!Number.isFinite(start) || !Number.isFinite(end)) return "—";
      const seconds = Math.max(0, Math.floor((end - start) / 1000)), hours = Math.floor(seconds / 3600);
      const two = (n) => String(n).padStart(2, "0");
      return (hours ? hours + ":" : "") + two(Math.floor(seconds / 60) % 60) + ":" + two(seconds % 60);
    },
  },
};
</script>
