<template>
  <header class="app-topbar">
    <div class="brand-block">
      <div class="brand-logo">审</div>
      <div class="brand-name">
        <strong>基金招募说明书、产品资料概要审核</strong>
      </div>
    </div>
    <nav class="topbar-nav" aria-label="主导航">
      <router-link
        to="/"
        class="topbar-tab"
        :aria-current="activeTab === 'reports' ? 'page' : null"
      >
        审核列表
      </router-link>
      <router-link
        to="/rules"
        class="topbar-tab"
        :aria-current="activeTab === 'rules' ? 'page' : null"
      >
        规则管理
      </router-link>
    </nav>
    <div v-if="$slots.actions" class="topbar-actions">
      <slot name="actions" />
    </div>
  </header>
</template>

<script>
export default {
  name: "AppTopbar",
  computed: {
    activeTab() {
      return this.$route.path === "/rules" ? "rules" : (this.$route.path === "/" ? "reports" : "");
    },
  },
};
</script>
