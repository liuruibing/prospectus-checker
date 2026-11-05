<template>
  <section class="pdf-pane">
    <header class="pdf-toolbar">
      <div class="pdf-title-block">
        <span class="pdf-title-icon" :class="roleName">{{ roleName === 'prospectus' ? '招' : '报' }}</span>
        <div><strong>{{ title }}</strong><small>{{ documentInfo ? documentInfo.filename : '尚未上传 PDF' }}</small></div>
      </div>
      <div v-if="documentInfo" class="pdf-controls">
        <el-button size="mini" icon="el-icon-arrow-left" aria-label="上一页" @click="changePage(-1)" />
        <span class="page-number-control">
          <el-input-number v-model="currentPage" :min="1" :max="pageCount" size="mini" controls-position="right" @change="goToPage" />
          <span>/ {{ pageCount }}</span>
        </span>
        <el-button size="mini" icon="el-icon-arrow-right" aria-label="下一页" @click="changePage(1)" />
        <el-button size="mini" icon="el-icon-minus" aria-label="缩小" @click="changeZoom(-10)" />
        <span class="zoom-value">{{ zoom }}%</span>
        <el-button size="mini" icon="el-icon-plus" aria-label="放大" @click="changeZoom(10)" />
      </div>
    </header>

    <div ref="scroller" class="pdf-scroll-area" @scroll.passive="updateCurrentPage">
      <template v-if="documentInfo && documentInfo.preview_id && pageCount">
        <article v-for="page in pages" :key="documentInfo.preview_id + ':' + page" class="pdf-page-card" :data-page="page">
          <div class="pdf-page-caption">
            <span>{{ title }}</span><span>第 {{ page }} 页 / 共 {{ pageCount }} 页</span>
          </div>
          <div class="pdf-image-wrap" :style="pageWrapStyle(page)">
            <img
              v-if="!imageErrors[page]"
              class="pdf-page-image"
              :data-src="imageUrl(page)"
              :alt="title + '第 ' + page + ' 页'"
              decoding="async"
              @error="handleImageError(page)"
            >
            <div v-else class="pdf-page-error" role="status">页面预览加载失败，请重新打开任务。</div>
            <div
              v-for="(item, index) in evidencesForPage(page)"
              :key="item.id || page + ':' + index"
              class="evidence-box"
              :class="statusClass(item.difference_status)"
              :style="boxStyle(item)"
            >
              <span>{{ item.field_label || '核对位置' }}</span>
            </div>
          </div>
        </article>
      </template>
      <el-empty v-else :image-size="70" :description="'暂无 PDF'" class="pdf-empty" />
    </div>

    <footer class="pdf-footer">
      <span v-if="evidences.length">已定位 {{ locatedEvidenceCount }} / {{ evidences.length }} 处证据</span>
      <span v-else>第 {{ currentPage }} 页</span>
      <span class="evidence-legends">
        <span class="evidence-legend"><i /> 差异位置</span>
        <span class="evidence-legend review"><i /> 待复核位置</span>
      </span>
    </footer>
  </section>
</template>

<script>
export default {
  name: "PdfViewer",
  props: {
    roleName: { type: String, required: true },
    title: { type: String, required: true },
    documentInfo: { type: Object, default: null },
    evidences: { type: Array, default: () => [] },
  },
  data() {
    return { currentPage: 1, zoom: 100, pageObserver: null, imageErrors: {} };
  },
  computed: {
    pageCount() {
      return Number(this.documentInfo && this.documentInfo.page_count || 0);
    },
    pages() {
      return Array.from({ length: this.pageCount }, (_, index) => index + 1);
    },
    pageDimensions() {
      return this.documentInfo && this.documentInfo.page_dimensions || [];
    },
    previewId() {
      return this.documentInfo && this.documentInfo.preview_id || "";
    },
    locatedEvidenceCount() {
      return this.evidences.filter((evidence) => evidence.status === "LOCATED" && evidence.bbox && evidence.page_width && evidence.page_height).length;
    },
    evidencesByPage() {
      return this.evidences.reduce((pages, evidence) => {
        if (
          evidence.status === "LOCATED" && evidence.bbox &&
          evidence.page_width && evidence.page_height && evidence.page
        ) {
          const page = Number(evidence.page);
          if (!pages[page]) pages[page] = [];
          pages[page].push(evidence);
        }
        return pages;
      }, {});
    },
  },
  watch: {
    previewId() {
      this.currentPage = 1;
      this.imageErrors = {};
      this.$nextTick(this.observePages);
    },
    evidences: {
      deep: true,
      handler(value) {
        const evidence = value.find((item) => (
          item && item.page && item.status === "LOCATED" &&
          item.bbox && item.page_width && item.page_height
        )) || value.find((item) => item && item.page);
        if (evidence) this.$nextTick(() => this.scrollToPage(evidence.page));
      },
    },
  },
  mounted() {
    this.observePages();
  },
  beforeDestroy() {
    if (this.pageObserver) this.pageObserver.disconnect();
  },
  methods: {
    imageUrl(page) {
      const taskId = this.documentInfo && this.documentInfo.task_id;
      if (taskId) {
        return "/api/jobs/" + encodeURIComponent(taskId) + "/documents/" +
          encodeURIComponent(this.roleName) + "/pages/" + page + "/image?scale=1.4";
      }
      return "/api/documents/" + encodeURIComponent(this.previewId) + "/pages/" + page + "/image?scale=1.4";
    },
    handleImageError(page) {
      this.$set(this.imageErrors, page, true);
    },
    pageWrapStyle(page) {
      const dimensions = this.pageDimensions[page - 1];
      return {
        width: this.zoom + "%",
        aspectRatio: dimensions ? dimensions.width + " / " + dimensions.height : "210 / 297",
      };
    },
    statusClass(status) {
      return String(status || "").toLowerCase().replace(/_/g, "-");
    },
    evidencesForPage(page) {
      return this.evidencesByPage[page] || [];
    },
    boxStyle(evidence) {
      const values = evidence.bbox.map(Number);
      return {
        left: Math.max(0, values[0] / evidence.page_width * 100) + "%",
        top: Math.max(0, values[1] / evidence.page_height * 100) + "%",
        width: Math.min(100, Math.max(0.5, (values[2] - values[0]) / evidence.page_width * 100)) + "%",
        height: Math.min(100, Math.max(0.5, (values[3] - values[1]) / evidence.page_height * 100)) + "%",
      };
    },
    observePages() {
      if (this.pageObserver) this.pageObserver.disconnect();
      const root = this.$refs.scroller;
      if (!root) return;
      const cards = this.$el.querySelectorAll(".pdf-page-card");
      const loadImage = (card) => {
        const image = card.querySelector("img[data-src]");
        if (image) {
          image.src = image.getAttribute("data-src");
          image.removeAttribute("data-src");
        }
      };
      if (!window.IntersectionObserver) {
        Array.prototype.forEach.call(cards, loadImage);
        return;
      }
      this.pageObserver = new IntersectionObserver((entries, observer) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          loadImage(entry.target);
          observer.unobserve(entry.target);
        });
      }, { root, rootMargin: "900px 0px" });
      Array.prototype.forEach.call(cards, (card) => this.pageObserver.observe(card));
    },
    scrollToPage(pageNumber) {
      const page = Math.max(1, Math.min(this.pageCount, Number(pageNumber || 1)));
      const scroller = this.$refs.scroller;
      const sheet = this.$el.querySelector('.pdf-page-card[data-page="' + page + '"]');
      if (!scroller || !sheet) return;
      this.currentPage = page;
      const focus = sheet.querySelector(".evidence-box") || sheet;
      const focusRect = focus.getBoundingClientRect();
      const scrollerRect = scroller.getBoundingClientRect();
      const focusCenter = focusRect.top + focusRect.height / 2;
      const viewportCenter = scrollerRect.top + scroller.clientHeight / 2;
      scroller.scrollTo({
        top: scroller.scrollTop + focusCenter - viewportCenter,
        behavior: "smooth",
      });
    },
    goToPage() {
      this.scrollToPage(this.currentPage);
    },
    changePage(delta) {
      this.scrollToPage(this.currentPage + delta);
    },
    changeZoom(delta) {
      this.zoom = Math.max(60, Math.min(160, this.zoom + delta));
    },
    updateCurrentPage() {
      const scroller = this.$refs.scroller;
      if (!scroller) return;
      const threshold = scroller.getBoundingClientRect().top + 8;
      const cards = this.$el.querySelectorAll(".pdf-page-card");
      for (let index = 0; index < cards.length; index += 1) {
        if (cards[index].getBoundingClientRect().bottom > threshold) {
          this.currentPage = Number(cards[index].getAttribute("data-page"));
          return;
        }
      }
    },
  },
};
</script>
