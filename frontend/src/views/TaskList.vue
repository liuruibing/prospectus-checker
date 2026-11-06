<template>
  <div class="task-list-page">
    <header class="app-topbar">
      <div class="brand-block"><div class="brand-logo">审</div><div class="brand-name"><strong>基金招募说明书核对</strong></div></div>
      <div class="topbar-actions"><el-button type="primary" icon="el-icon-plus" class="upload-trigger" @click="uploadVisible = true">新增核对</el-button></div>
    </header>
    <main class="task-list-main">
      <div class="task-list-heading"><h1>核对任务</h1><span>共 {{ total }} 项</span></div>
      <section class="task-list-surface" aria-label="历史核对任务">
        <form class="task-query-form" @submit.prevent="search">
          <el-input v-model="keyword" placeholder="任务 ID / 基金名称 / 文件名" aria-label="任务 ID、基金名称或文件名" clearable prefix-icon="el-icon-search" class="task-keyword" maxlength="200" />
          <el-select v-model="status" clearable placeholder="全部任务状态" aria-label="任务状态"><el-option v-for="item in states" :key="item.value" :value="item.value" :label="item.label" /></el-select>
          <el-select v-model="resultStatus" clearable placeholder="全部核对结果" aria-label="核对结果"><el-option v-for="item in results" :key="item.value" :value="item.value" :label="item.label" /></el-select>
          <el-date-picker v-model="dates" type="daterange" value-format="yyyy-MM-dd" format="yyyy-MM-dd" range-separator="至" start-placeholder="创建开始日期" end-placeholder="结束日期" aria-label="任务创建日期范围" />
          <div class="task-query-actions"><el-button type="primary" native-type="submit">查询</el-button><el-button @click="reset">重置</el-button></div>
        </form>
        <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="task-query-error" />
        <div class="task-table-scroll" v-loading="loading">
          <table class="task-table">
            <thead><tr><th scope="col" class="task-code-column">任务 ID</th><th scope="col" class="task-files-column">核对文档</th><th scope="col">任务状态</th><th scope="col">核对结果</th><th scope="col">创建时间</th><th scope="col">执行用时</th><th scope="col" class="task-action-column">操作</th></tr></thead>
            <tbody>
              <tr v-for="task in tasks" :key="task.job_id">
                <td><router-link class="task-id-link" :to="detailLink(task)">{{ task.job_id }}</router-link></td>
                <td class="task-files-cell">
                  <div v-for="role in documentRoles" :key="role.key" class="task-document-row" :title="fileName(task, role.key)">
                    <span class="task-document-icon" :class="role.key">{{ role.icon }}</span><span class="task-document-label">{{ role.label }}</span><span class="task-document-name">{{ fileName(task, role.key) }}</span>
                  </div>
                </td>
                <td><span class="task-state-tag" :class="task.status"><i />{{ stateLabel(task.status) }}</span></td>
                <td><span class="task-result" :class="resultClass(task)">{{ resultLabel(task) }}</span></td>
                <td class="task-time-cell">{{ formatDate(task.created_at) }}</td>
                <td class="task-duration-cell">{{ elapsed(task) }}</td>
                <td><router-link class="task-detail-link" :to="detailLink(task)">查看详情<i class="el-icon-arrow-right" /></router-link></td>
              </tr>
              <tr v-if="!tasks.length && !loading"><td colspan="7" class="task-empty"><i class="el-icon-document" /><strong>{{ error ? '任务列表暂时不可用' : '暂无核对任务' }}</strong><span v-if="!error">{{ hasFilters ? '请调整查询条件' : '点击右上角新增核对' }}</span></td></tr>
            </tbody>
          </table>
        </div>
        <footer class="task-list-footer"><span>{{ total ? '第 ' + ((page - 1) * pageSize + 1) + '–' + Math.min(page * pageSize, total) + ' 项，共 ' + total + ' 项' : '共 0 项' }}</span><el-pagination background layout="sizes, prev, pager, next" :page-sizes="[10, 20, 50]" :page-size="pageSize" :current-page="page" :total="total" @current-change="changePage" @size-change="changeSize" /></footer>
      </section>
    </main>
    <upload-dialog :visible.sync="uploadVisible" @submitted="taskCreated" />
  </div>
</template>

<script>
import axios from "axios";
import UploadDialog from "../components/UploadDialog.vue";

export default {
  name: "TaskList",
  components: { UploadDialog },
  data() {
    return {
      tasks: [], total: 0, loading: false, error: "", keyword: "", status: "", resultStatus: "", dates: [], page: 1, pageSize: 10,
      uploadVisible: false, clockNow: Date.now(), pollTimer: null, clockTimer: null, requestId: 0, refreshing: false,
      states: [{ value: "queued", label: "排队中" }, { value: "running", label: "核对中" }, { value: "completed", label: "已完成" }, { value: "failed", label: "失败" }],
      results: [{ value: "MATCH", label: "一致" }, { value: "MISMATCH", label: "存在差异" }, { value: "UNRESOLVED", label: "待人工复核" }, { value: "FUND_MISMATCH", label: "基金身份不一致" }, { value: "DATE_MISMATCH", label: "文件日期不适配" }],
      documentRoles: [{ key: "prospectus", icon: "招", label: "招募书" }, { key: "periodic_report", icon: "报", label: "定期报告" }],
    };
  },
  computed: { hasFilters() { return !!(this.keyword || this.status || this.resultStatus || this.dates && this.dates.length); } },
  watch: { "$route.query": { immediate: true, handler() { this.readQuery(); this.fetchTasks(); } } },
  mounted() {
    this.pollTimer = window.setInterval(() => { if (!document.hidden && !this.refreshing) this.fetchTasks(true); }, 5000);
    this.clockTimer = window.setInterval(() => { this.clockNow = Date.now(); }, 1000);
  },
  beforeDestroy() { window.clearInterval(this.pollTimer); window.clearInterval(this.clockTimer); this.requestId += 1; },
  methods: {
    readQuery() {
      const q = this.$route.query;
      this.keyword = String(q.q || ""); this.status = q.status || ""; this.resultStatus = q.result_status || "";
      this.dates = q.start_date && q.end_date ? [q.start_date, q.end_date] : [];
      this.page = Math.max(1, Math.floor(Number(q.page) || 1));
      this.pageSize = [10, 20, 50].indexOf(Number(q.page_size)) >= 0 ? Number(q.page_size) : 10;
    },
    query() {
      const query = { page: String(this.page), page_size: String(this.pageSize) };
      if (this.keyword.trim()) query.q = this.keyword.trim();
      if (this.status) query.status = this.status;
      if (this.resultStatus) query.result_status = this.resultStatus;
      if (this.dates && this.dates.length === 2) { query.start_date = this.dates[0]; query.end_date = this.dates[1]; }
      return query;
    },
    applyQuery() {
      const location = { path: "/", query: this.query() };
      if (this.$router.resolve(location).route.fullPath === this.$route.fullPath) this.fetchTasks();
      else this.$router.push(location);
    },
    search() { this.page = 1; this.applyQuery(); },
    reset() { this.keyword = this.status = this.resultStatus = ""; this.dates = []; this.page = 1; this.applyQuery(); },
    changePage(page) { this.page = page; this.applyQuery(); },
    changeSize(size) { this.pageSize = size; this.page = 1; this.applyQuery(); },
    async fetchTasks(silent) {
      const request = ++this.requestId;
      this.refreshing = true;
      if (!silent) this.loading = true;
      try {
        const response = await axios.get("/api/tasks", { params: this.$route.query });
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
    fileName(task, role) { return task.document_names && task.document_names[role] || "未记录文件名"; },
    stateLabel(status) { const item = this.states.find((item) => item.value === status); return item ? item.label : "未知状态"; },
    resultLabel(task) {
      if (task.status === "failed") return "核对失败";
      if (task.status !== "completed") return "等待结果";
      if (task.result_status === "MISMATCH") return "差异 " + (task.difference_count || 0) + " 项";
      const item = this.results.find((item) => item.value === task.result_status);
      return item ? item.label : "未记录结果";
    },
    resultClass(task) { return task.status === "failed" || task.result_status === "MISMATCH" ? "danger" : task.result_status === "MATCH" ? "success" : task.result_status ? "warning" : "muted"; },
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
