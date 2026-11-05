import Vue from "vue";
import VueRouter from "vue-router";
import ElementUI from "element-ui";
import "normalize.css/normalize.css";
import "element-ui/lib/theme-chalk/index.css";
import "./styles.css";
import App from "./App.vue";
import Workbench from "./views/Workbench.vue";

Vue.use(VueRouter);
Vue.use(ElementUI, { size: "small" });
Vue.config.productionTip = false;

const router = new VueRouter({
  mode: "hash",
  routes: [{ path: "/", component: Workbench }],
});

new Vue({
  router,
  render: (createElement) => createElement(App),
}).$mount("#app");
