<template>
  <el-drawer
    :visible.sync="visibleProxy"
    :before-close="beforeClose"
    :show-close="false"
    :size="drawerSize"
    custom-class="rule-maintenance-drawer"
    append-to-body
    :with-header="false"
  >
    <section class="rule-maintenance">
      <header class="rule-maintenance-header">
        <div>
          <h2>规则维护</h2>
          <span>{{ rules.length }} 份规则</span>
        </div>
        <button class="rule-close-button" type="button" aria-label="关闭规则维护" @click="requestClose"><i class="el-icon-close" /></button>
      </header>
      <div class="rule-maintenance-body">
        <aside class="rule-navigation">
          <div class="rule-nav-head">
            <el-input v-model="query" clearable prefix-icon="el-icon-search" placeholder="搜索规则名称或文件" aria-label="搜索规则" class="rule-search" />
            <span class="rule-nav-count">{{ filteredRuleCount }} 项</span>
          </div>
          <div v-loading="loading" class="rule-groups">
            <section v-for="group in visibleGroups" :key="group.scope" class="rule-group">
              <div class="rule-group-title"><span>{{ group.label }}</span><small>{{ group.rules.length }}</small></div>
              <button
                v-for="rule in group.rules"
                :key="rule.scope + '/' + rule.filename"
                type="button"
                class="rule-nav-item"
                :class="{ active: isActive(rule) }"
                @click="selectRule(rule)"
              >
                <i class="el-icon-document" aria-hidden="true" />
                <span class="rule-nav-copy"><strong>{{ rule.title }}</strong></span>
                <i v-if="isActive(rule)" class="el-icon-arrow-right rule-nav-arrow" aria-hidden="true" />
              </button>
            </section>
            <el-empty v-if="!loading && !visibleGroups.length" :image-size="48" description="没有匹配的规则" />
          </div>
        </aside>

        <main class="rule-editor-panel" :class="{ 'is-preview': !editing }">
          <template v-if="activeRule">
            <div class="rule-editor-toolbar">
              <div class="rule-editor-file">
                <div class="rule-editor-breadcrumb"><span>{{ activeRule.scope_label }}</span></div>
                <strong>{{ activeRule.title }}</strong>
              </div>
              <div class="rule-editor-actions">
                <el-button v-if="!editing" type="primary" icon="el-icon-edit-outline" @click="enterEditMode">编辑规则</el-button>
                <template v-else>
                  <el-button :disabled="saving" @click="cancelEdit">取消编辑</el-button>
                  <el-button :disabled="!dirty || saving" icon="el-icon-refresh" @click="reloadRule">还原</el-button>
                  <el-button type="primary" :disabled="saving" :loading="saving" @click="saveRule">保存并预览</el-button>
                </template>
              </div>
            </div>
            <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon class="rule-editor-error" />
            <div v-if="!editing" class="rule-preview-content" v-loading="loadingRule">
              <div class="rule-preview-toolbar"><span>规则内容</span></div>
              <article class="rule-markdown-preview" v-html="renderedMarkdown" />
            </div>
            <template v-else>
              <div class="rule-editor-hint"><span><i class="el-icon-edit-outline" />编辑业务规则</span><small>新建核对任务将使用保存后的规则</small></div>
              <div class="rule-editor-content rule-section-editor" v-loading="loadingRule">
                <label class="rule-title-field">
                  <span>规则名称</span>
                  <el-input v-model="draftTitle" :disabled="loadingRule || saving" />
                </label>
                <section v-for="(section, index) in draftSections" :key="`${activeRule.filename}-${index}`" class="rule-section-field">
                  <h3>{{ section.title }}</h3>
                  <el-input
                    v-model="section.content"
                    type="textarea"
                    :rows="sectionRows(section.content)"
                    resize="vertical"
                    :disabled="loadingRule || saving"
                    :aria-label="section.title"
                    placeholder="填写这一部分的核对要求"
                    @keydown.native="handleShortcut"
                  />
                </section>
              </div>
            </template>
            <footer class="rule-editor-footer">
              <span v-if="!editing" class="is-preview"><i />预览模式</span>
              <span v-else :class="dirty ? 'is-dirty' : 'is-saved'"><i />{{ dirty ? '尚未保存' : '已保存' }}</span>
              <small v-if="editing && dirty" class="rule-shortcut">⌘/Ctrl + S 保存</small>
              <span>{{ serializeDraft().length.toLocaleString() }} 字符</span>
            </footer>
          </template>
          <el-empty v-else-if="!loading" :image-size="72" description="选择一个规则文件开始维护" />
        </main>
      </div>
    </section>
  </el-drawer>
</template>

<script>
import axios from "axios";

export default {
  name: "RuleMaintenanceDrawer",
  props: { visible: { type: Boolean, default: false } },
  data() {
    return {
      rules: [], activeRule: null, draftTitle: "", draftSections: [], savedContent: "", expectedSha256: "", query: "", editing: false,
      loading: false, loadingRule: false, saving: false, error: "", viewportWidth: window.innerWidth,
    };
  },
  computed: {
    visibleProxy: {
      get() { return this.visible; },
      set(value) { this.$emit("update:visible", value); },
    },
    drawerSize() { return this.viewportWidth < 760 ? "100%" : "1080px"; },
    dirty() { return !!this.activeRule && this.serializeDraft() !== this.savedContent; },
    visibleGroups() {
      const needle = this.query.trim().toLocaleLowerCase();
      const groups = new Map();
      this.rules.forEach((rule) => {
        if (needle && !`${rule.title} ${rule.filename} ${rule.scope_label}`.toLocaleLowerCase().includes(needle)) return;
        if (!groups.has(rule.scope)) groups.set(rule.scope, { scope: rule.scope, label: rule.scope_label, rules: [] });
        groups.get(rule.scope).rules.push(rule);
      });
      return Array.from(groups.values());
    },
    filteredRuleCount() { return this.visibleGroups.reduce((count, group) => count + group.rules.length, 0); },
    markdownBody() {
      const content = this.activeRule && this.activeRule.content || "";
      return content.replace(/^\+\+\+\s*\r?\n[\s\S]*?\r?\n\+\+\+\s*(?:\r?\n|$)/, "").trim();
    },
    renderedMarkdown() { return this.renderMarkdown(this.markdownBody); },
  },
  watch: {
    visible(value) { if (value) this.loadRules(); },
  },
  mounted() { window.addEventListener("resize", this.updateViewportWidth); },
  beforeDestroy() { window.removeEventListener("resize", this.updateViewportWidth); },
  methods: {
    updateViewportWidth() { this.viewportWidth = window.innerWidth; },
    isActive(rule) { return this.activeRule && this.activeRule.scope === rule.scope && this.activeRule.filename === rule.filename; },
    async loadRules(preferred) {
      this.loading = true;
      this.error = "";
      try {
        const response = await axios.get("/api/rules");
        this.rules = response.data.rules || [];
        const next = preferred
          ? this.rules.find((rule) => rule.scope === preferred.scope && rule.filename === preferred.filename)
          : this.rules.find((rule) => this.isActive(rule)) || this.rules[0];
        if (next) await this.fetchRule(next);
        else { this.activeRule = null; this.draftTitle = ""; this.draftSections = []; this.savedContent = ""; }
      } catch (error) {
        this.error = this.errorText(error, "无法读取规则目录。");
      } finally { this.loading = false; }
    },
    async fetchRule(rule) {
      this.loadingRule = true;
      this.error = "";
      try {
        const response = await axios.get(`/api/rules/${encodeURIComponent(rule.scope)}/${encodeURIComponent(rule.filename)}`);
        this.activeRule = response.data;
        this.savedContent = response.data.content;
        this.setDraft(response.data.content);
        this.expectedSha256 = response.data.sha256;
        this.editing = false;
      } catch (error) {
        this.error = this.errorText(error, "无法读取规则文件。");
      } finally { this.loadingRule = false; }
    },
    async selectRule(rule) {
      if (this.isActive(rule)) return;
      if (this.dirty) {
        try { await this.$confirm("切换文件会丢弃尚未保存的修改。", "放弃修改？", { confirmButtonText: "继续切换", cancelButtonText: "留在此页", type: "warning" }); }
        catch (_) { return; }
      }
      await this.fetchRule(rule);
    },
    async reloadRule() {
      if (this.dirty) {
        try { await this.$confirm("重新载入会丢弃尚未保存的修改。", "重新载入规则？", { confirmButtonText: "重新载入", cancelButtonText: "取消", type: "warning" }); }
        catch (_) { return; }
      }
      const active = this.activeRule;
      await this.fetchRule(active);
      this.editing = true;
    },
    enterEditMode() { this.editing = true; this.error = ""; },
    async cancelEdit() {
      if (this.dirty) {
        try { await this.$confirm("放弃尚未保存的修改？", "取消编辑", { confirmButtonText: "放弃修改", cancelButtonText: "继续编辑", type: "warning" }); }
        catch (_) { return; }
      }
      this.setDraft(this.savedContent);
      this.error = "";
      this.editing = false;
    },
    async saveRule() {
      if (!this.activeRule || this.saving) return;
      if (!this.dirty) { this.editing = false; return; }
      this.saving = true;
      this.error = "";
      try {
        const response = await axios.put(
          `/api/rules/${encodeURIComponent(this.activeRule.scope)}/${encodeURIComponent(this.activeRule.filename)}`,
          { content: this.serializeDraft(), expected_sha256: this.expectedSha256 },
        );
        this.activeRule = response.data;
        this.savedContent = response.data.content;
        this.setDraft(response.data.content);
        this.expectedSha256 = response.data.sha256;
        this.editing = false;
        const index = this.rules.findIndex((rule) => this.isActive(rule));
        if (index >= 0) this.$set(this.rules, index, { ...this.rules[index], sha256: response.data.sha256, updated_at: response.data.updated_at, characters: response.data.content.length });
        this.$message.success("规则已保存，新核对任务将读取最新内容。");
      } catch (error) {
        this.error = this.errorText(error, "保存失败，请重试。");
      } finally { this.saving = false; }
    },
    beforeClose(done) {
      if (!this.dirty) { done(); return; }
      this.$confirm("关闭后会丢弃尚未保存的修改。", "放弃修改？", { confirmButtonText: "关闭", cancelButtonText: "继续编辑", type: "warning" })
        .then(done).catch(() => {});
    },
    requestClose() {
      if (!this.dirty) { this.visibleProxy = false; return; }
      this.$confirm("关闭后会丢弃尚未保存的修改。", "放弃修改？", { confirmButtonText: "关闭", cancelButtonText: "继续编辑", type: "warning" })
        .then(() => { this.visibleProxy = false; }).catch(() => {});
    },
    handleShortcut(event) {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        this.saveRule();
      }
    },
    parseRule(content) {
      const body = (content || "").replace(/^\+\+\+\s*\r?\n[\s\S]*?\r?\n\+\+\+\s*(?:\r?\n|$)/, "").trim();
      const lines = body.split(/\r?\n/);
      const titleIndex = lines.findIndex((line) => /^#\s+/.test(line));
      const title = titleIndex >= 0 ? lines[titleIndex].replace(/^#\s+/, "").trim() : "";
      const sections = [];
      let current = null;
      for (const line of lines) {
        const heading = line.match(/^##\s+(.+?)\s*#*$/);
        if (heading) {
          current = { title: heading[1].trim(), content: "" };
          sections.push(current);
        } else if (current) {
          current.content += `${current.content ? "\n" : ""}${line}`;
        }
      }
      return { title: title || "规则说明", sections };
    },
    setDraft(content) {
      const parsed = this.parseRule(content);
      this.draftTitle = parsed.title;
      this.draftSections = parsed.sections;
    },
    serializeDraft() {
      const sections = this.draftSections.map((section) => `## ${section.title}\n${section.content.trim()}`).join("\n\n");
      return `# ${this.draftTitle.trim()}\n\n${sections}\n`;
    },
    sectionRows(content) { return Math.max(3, Math.min(10, (content || "").split("\n").length + 1)); },
    renderMarkdown(source) {
      const escape = (value) => value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
      const inline = (value) => escape(value)
        .replace(/`([^`]+)`/g, "<code>$1</code>")
        .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
        .replace(/\*([^*]+)\*/g, "<em>$1</em>")
        .replace(/\[([^\]]+)\]\([^)]+\)/g, "$1");
      const lines = (source || "").replace(/\r/g, "").split("\n");
      const html = [];
      let paragraph = [], listType = "", listItems = [], tableRows = [], codeLines = [], inCode = false;
      const flushParagraph = () => { if (paragraph.length) html.push(`<p>${inline(paragraph.join(" "))}</p>`); paragraph = []; };
      const flushList = () => { if (listItems.length) { html.push(`<${listType}>${listItems.map((item) => `<li>${inline(item)}</li>`).join("")}</${listType}>`); listItems = []; listType = ""; } };
      const flushTable = () => {
        if (!tableRows.length) return;
        const rows = tableRows.filter((row) => !row.every((cell) => /^:?-{3,}:?$/.test(cell.trim())));
        if (rows.length) html.push(`<table><thead><tr>${rows[0].map((cell) => `<th>${inline(cell.trim())}</th>`).join("")}</tr></thead><tbody>${rows.slice(1).map((row) => `<tr>${row.map((cell) => `<td>${inline(cell.trim())}</td>`).join("")}</tr>`).join("")}</tbody></table>`);
        tableRows = [];
      };
      for (const line of lines) {
        if (/^\s*```/.test(line)) { flushParagraph(); flushList(); flushTable(); if (inCode) { html.push(`<pre><code>${escape(codeLines.join("\n"))}</code></pre>`); codeLines = []; } inCode = !inCode; continue; }
        if (inCode) { codeLines.push(line); continue; }
        const tableMatch = line.match(/^\s*\|(.+)\|\s*$/);
        if (tableMatch) { flushParagraph(); flushList(); tableRows.push(tableMatch[1].split("|")); continue; }
        flushTable();
        if (!line.trim()) { flushParagraph(); flushList(); continue; }
        const heading = line.match(/^\s*(#{1,6})\s+(.+)$/);
        if (heading) { flushParagraph(); flushList(); const level = Math.min(heading[1].length + 1, 6); html.push(`<h${level}>${inline(heading[2])}</h${level}>`); continue; }
        const unordered = line.match(/^\s*[-*+]\s+(.+)$/);
        const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
        if (unordered || ordered) { flushParagraph(); const type = unordered ? "ul" : "ol"; if (listItems.length && listType !== type) flushList(); listType = type; listItems.push((unordered || ordered)[1]); continue; }
        const quote = line.match(/^\s*>\s?(.*)$/);
        if (quote) { flushParagraph(); flushList(); html.push(`<blockquote>${inline(quote[1])}</blockquote>`); continue; }
        paragraph.push(line.trim());
      }
      flushParagraph(); flushList(); flushTable();
      if (inCode && codeLines.length) html.push(`<pre><code>${escape(codeLines.join("\n"))}</code></pre>`);
      return html.join("");
    },
    errorText(error, fallback) {
      const detail = error.response && error.response.data && error.response.data.detail;
      return typeof detail === "string" ? detail : fallback;
    },
  },
};
</script>
