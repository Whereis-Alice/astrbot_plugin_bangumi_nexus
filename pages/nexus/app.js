/**
 * 番剧中枢 · Bangumi Nexus — Dashboard 插件页前端
 *
 * 设计约定（改动前请先读完）：
 * 1. 所有后端交互只走 window.AstrBotPluginPage 这个 bridge，
 *    它会把 apiGet("meta") 翻译成 /api/plug/astrbot_plugin_bangumi_nexus/meta，
 *    并自动带上 Dashboard 的鉴权头，所以这里不要自己拼 fetch。
 * 2. 插件页跑在 sandbox iframe 里，没有 localStorage，
 *    因此界面偏好（主题 / 紧凑 / 当前页 / 各视图小状态）统一存后端 /state。
 * 3. 视图渲染是「整块 innerHTML 替换 + 事件委托」，不做虚拟 DOM。
 *    每个视图导出 RENDERERS[key]() 返回 HTML 字符串，
 *    需要在插入后补挂钩子的话再写一个 RENDERERS[key + ":after"](root)。
 * 4. 长任务（渲染卡片 / 体检 / 播报）都用 withBusy 包一层，避免用户重复点。
 *
 * Copyright (C) 2026 Whereis-Alice and AstrBot Plugin Authors.
 * Licensed under the GNU Affero General Public License v3.0 or later.
 */

const bridge = window.AstrBotPluginPage;

const VIEWS = [
  { key: "overview", icon: "overview", label: "概览", tip: "运行状态、活动日志与一键操作" },
  { key: "config", icon: "config", label: "配置", tip: "分组编辑全部插件配置项" },
  { key: "watch", icon: "heart", label: "追番", tip: "按会话管理追番进度与评分" },
  { key: "subs", icon: "rss", label: "订阅", tip: "RSS 订阅增删、测试与导入导出" },
  { key: "targets", icon: "bell", label: "播报", tip: "每日新番播报目标与手动触发" },
  { key: "anirss", icon: "sync", label: "同步", tip: "把本地 ani-rss 的订阅与下载进度同步进追番表" },
  { key: "cards", icon: "cards", label: "卡片", tip: "主题 × 卡片类型的真实渲染预览" },
  { key: "sources", icon: "source", label: "数据源", tip: "八个上游数据源的健康体检" },
  { key: "commands", icon: "commands", label: "指令", tip: "全部聊天指令速查表" },
  { key: "about", icon: "about", label: "关于", tip: "版本、限额、安全提示与致谢" },
];

const VIEW_KEYS = VIEWS.map((item) => item.key);
const DEFAULT_VIEW = "overview";
// 除指令表（自带 sticky 表头、需要面板内滚动）外，其余视图都由外层滚动。
const SCROLL_VIEWS = new Set(VIEW_KEYS.filter((key) => key !== "commands"));
const PREF_SAVE_DELAY = 420;
const LOG_LEVELS = [
  { key: "", label: "全部" },
  { key: "info", label: "信息" },
  { key: "warn", label: "警告" },
  { key: "error", label: "错误" },
];
const KIND_LABEL = {
  help: "帮助卡",
  search: "搜索结果卡",
  watchlist: "追番清单卡",
  notice: "更新通知卡",
};
const RENDERER_LABEL = {
  auto: "自动（推荐）",
  html: "HTML 渲染",
  raster: "Pillow 绘制",
  t2i: "AstrBot 文转图",
  text: "纯文本",
};
const SORT_KEY_LABEL = { score: "评分", doing: "在看人数", time: "放送时间", name: "名称" };

const state = {
  ready: false,
  meta: null,
  themes: [],
  themeMap: new Map(),
  theme: "midnight",
  dense: false,
  view: DEFAULT_VIEW,
  overview: null,
  logs: [],
  logLevel: "",
  config: null,
  configDraft: new Map(),
  sessions: [],
  umo: "",
  watch: { items: [], total: 0, status: "" },
  subs: { items: [], total: 0, enabled: 0 },
  subDraft: { value: "", name: "" },
  targets: null,
  targetsDraft: null,
  anirss: null,
  anirssDraft: null,
  anirssImportDraft: "",
  cards: { kind: "help", renderer: "", shots: new Map(), busy: new Set() },
  probes: null,
  search: { keyword: "", items: [], busy: false },
  gacha: { genre: "", text: "" },
  cmdFilter: "",
  importText: "",
};

/* --- 基础工具 ------------------------------------------------------------ */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ESCAPES[ch]);
const attr = (value) => esc(value);
const icon = (name, extra = "") =>
  `<svg class="icon${extra ? " " + extra : ""}" aria-hidden="true"><use href="#i-${name}"></use></svg>`;

const num = (value) => Number(value || 0).toLocaleString("zh-CN");

function bytes(value) {
  const size = Number(value || 0);
  if (size < 1024) return size + " B";
  if (size < 1024 * 1024) return (size / 1024).toFixed(1) + " KB";
  return (size / 1024 / 1024).toFixed(2) + " MB";
}

function duration(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const day = Math.floor(total / 86400);
  const hour = Math.floor((total % 86400) / 3600);
  const min = Math.floor((total % 3600) / 60);
  if (day > 0) return day + " 天 " + hour + " 小时";
  if (hour > 0) return hour + " 小时 " + min + " 分";
  if (min > 0) return min + " 分 " + (total % 60) + " 秒";
  return total + " 秒";
}

function clock(epoch) {
  const value = Number(epoch) || 0;
  if (value <= 0) return "—";
  const date = new Date(value * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  return pad(date.getMonth() + 1) + "-" + pad(date.getDate()) + " " + pad(date.getHours()) + ":" + pad(date.getMinutes());
}

function relative(epoch) {
  const value = Number(epoch) || 0;
  if (value <= 0) return "从未";
  const delta = Date.now() / 1000 - value;
  if (delta < 60) return "刚刚";
  if (delta < 3600) return Math.floor(delta / 60) + " 分钟前";
  if (delta < 86400) return Math.floor(delta / 3600) + " 小时前";
  return Math.floor(delta / 86400) + " 天前";
}

/** 会话 ID 太长，列表里只展示「平台:类型:尾号」这种可辨识的短形式。 */
function shortUmo(umo) {
  const text = String(umo || "");
  if (!text) return "（未指定）";
  const parts = text.split(":");
  if (parts.length < 3) return text;
  const tail = parts.slice(2).join(":");
  const kind = parts[1].includes("Group") ? "群" : parts[1].includes("Friend") ? "私聊" : parts[1];
  return parts[0] + " · " + kind + " · " + tail;
}

const errText = (error) =>
  String(error?.message || error || "").replace(/^Error:\s*/, "") || "操作失败";

const TOAST_ICON = { ok: "check", err: "close", warn: "info", info: "info" };

function toast(message, kind = "info", ttl = 4200) {
  const host = $("#toasts");
  if (!host) return;
  const node = document.createElement("div");
  node.className = "toast " + kind;
  node.innerHTML = icon(TOAST_ICON[kind] || "info") + "<span>" + esc(message) + "</span>";
  const dismiss = () => {
    node.classList.add("is-leaving");
    setTimeout(() => node.remove(), 200);
  };
  node.addEventListener("click", dismiss);
  host.appendChild(node);
  setTimeout(dismiss, ttl);
}

/**
 * 复制到剪贴板。
 *
 * 三处地方都要「复制一段可直接粘走的文本」，失败文案也一模一样，
 * 所以收成一个函数：非 HTTPS 或没授权时 「navigator.clipboard」 会直接抛，
 * 这时候不能静默失败 —— 得告诉用户手动全选。
 */
async function copyText(text, okMessage = "已复制到剪贴板") {
  try {
    await navigator.clipboard.writeText(String(text ?? ""));
    toast(okMessage, "ok");
    return true;
  } catch {
    toast("这个环境不允许自动复制，请手动全选上面的文本", "warn", 6000);
    return false;
  }
}

/**
 * 危险操作的二次确认，返回 Promise<boolean>。
 *
 * 这里必须自绘，不能用 window.confirm：AstrBot 的插件页外壳给 iframe 的 sandbox
 * 是 「allow-scripts allow-forms allow-downloads」，没有 「allow-modals」。
 * 按规范，这种 iframe 里的 confirm 会被浏览器直接判为「取消」且不弹任何东西 ——
 * 于是「删除订阅」这类按钮从用户视角看就是「点了完全没反应」。
 * 支持 Esc 取消 / 回车确认 / 点遮罩取消，关掉后把焦点还给原来那个按钮。
 */
function ask(question, { title = "确认一下", yes = "确定", no = "取消", kind = "danger" } = {}) {
  const host = $("#askbox");
  const card = host && $(".askbox-card", host);
  const btnYes = host && $("#askbox-yes", host);
  const btnNo = host && $("#askbox-no", host);
  // 骨架缺失（理论上不会）时宁可放行：按钮本身已经是一次明确的点击意图，
  // 静默吞掉操作比少一次确认更难排查。
  if (!host || !card || !btnYes || !btnNo) return Promise.resolve(true);
  // 同一时刻只允许一个确认框，重复触发直接当作放弃，避免 resolve 悬空。
  if (!host.hidden) return Promise.resolve(false);

  const mark = $("#askbox-mark", host);
  if (mark) mark.classList.toggle("danger", kind === "danger");
  $("#askbox-title", host).textContent = title;
  $("#askbox-body", host).textContent = question;
  btnYes.textContent = yes;
  btnNo.textContent = no;
  btnYes.classList.toggle("danger", kind === "danger");
  btnYes.classList.toggle("primary", kind !== "danger");

  const opener = document.activeElement;
  host.hidden = false;
  btnYes.focus();

  return new Promise((resolve) => {
    const settle = (value) => {
      host.hidden = true;
      btnYes.removeEventListener("click", onYes);
      btnNo.removeEventListener("click", onNo);
      host.removeEventListener("mousedown", onBackdrop);
      document.removeEventListener("keydown", onKey, true);
      if (opener && typeof opener.focus === "function") opener.focus();
      resolve(value);
    };
    const onYes = () => settle(true);
    const onNo = () => settle(false);
    // 只认落在遮罩本身的按下，避免卡片内拖选文字松手时被当成取消。
    const onBackdrop = (event) => {
      if (event.target === host) settle(false);
    };
    const onKey = (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        settle(false);
      } else if (event.key === "Enter") {
        event.preventDefault();
        settle(true);
      } else if (event.key === "Tab") {
        // 焦点锁在两个按钮之间，别让 Tab 跑到背后那一屏去。
        event.preventDefault();
        (document.activeElement === btnYes ? btnNo : btnYes).focus();
      }
    };
    btnYes.addEventListener("click", onYes);
    btnNo.addEventListener("click", onNo);
    host.addEventListener("mousedown", onBackdrop);
    document.addEventListener("keydown", onKey, true);
  });
}

async function apiGet(endpoint, params) {
  return bridge.apiGet(endpoint, params);
}

async function apiPost(endpoint, body) {
  return bridge.apiPost(endpoint, body);
}

/** 给按钮加转圈 + 禁用，无论成功失败都还原，顺手把异常转成 toast。 */
async function withBusy(node, action) {
  if (node) {
    node.classList.add("is-busy");
    node.disabled = true;
  }
  try {
    return await action();
  } catch (error) {
    toast(errText(error), "err", 6000);
    return null;
  } finally {
    if (node) {
      node.classList.remove("is-busy");
      node.disabled = false;
    }
  }
}
/* --- 主题 / 偏好 / 路由 --------------------------------------------------- */

const root = document.documentElement;

const swatchesHtml = (theme) =>
  (theme.swatches || [])
    .slice(0, 4)
    .map((color) => `<i style="background:${attr(color)}"></i>`)
    .join("");

/** 紧凑模式只切 html[data-dense]，真正的压缩规则全在 CSS 里，JS 不掺和尺寸。 */
function applyDense() {
  root.dataset.dense = state.dense ? "1" : "0";
  const btn = $("#btn-dense");
  if (btn) {
    btn.classList.toggle("is-on", state.dense);
    btn.setAttribute("aria-pressed", state.dense ? "true" : "false");
  }
}

/**
 * 切换主题。
 *
 * Dashboard 的 bridge 独占 html[data-theme]（它自己的明暗切换），
 * 所以本页用独立的 html[data-nexus-theme]，两套互不打扰。
 * 主题同时决定卡片预览的配色，因此换主题要顺手把预览缓存清掉。
 */
function applyTheme(key, { persist = false } = {}) {
  const theme = state.themeMap.get(key) || state.themes[0];
  if (!theme) return;
  state.theme = theme.key;
  root.dataset.nexusTheme = theme.key;
  root.style.colorScheme = theme.mode === "light" ? "light" : "dark";
  const label = $("#theme-label");
  if (label) label.textContent = theme.name;
  const swatch = $("#theme-swatch");
  if (swatch) swatch.innerHTML = swatchesHtml(theme);
  paintThemeMenu();
  state.cards.shots.clear();
  if (persist) saveState();
  if (state.ready && state.view === "cards") render("cards");
}

function paintThemeMenu() {
  const menu = $("#theme-menu");
  if (!menu) return;
  menu.innerHTML = state.themes
    .map(
      (theme) =>
        `<button class="theme-option${theme.key === state.theme ? " is-active" : ""}" type="button" role="menuitem" data-theme="${attr(theme.key)}">` +
        `<span class="swatches">${swatchesHtml(theme)}</span>` +
        `<span class="theme-option-name"><strong>${esc(theme.name)}</strong><span>${esc(theme.tagline)}</span></span>` +
        icon("check", "sm tick") +
        `</button>`,
    )
    .join("");
}

function closeThemeMenu() {
  const menu = $("#theme-menu");
  const btn = $("#btn-theme");
  if (menu) menu.hidden = true;
  if (btn) btn.setAttribute("aria-expanded", "false");
}

/* 界面偏好：iframe 没有 localStorage，只能存后端 KV，所以要防抖 + 去重。 */

function prefPayload() {
  return {
    theme: state.theme,
    dense: state.dense,
    view: state.view,
    logLevel: state.logLevel,
    umo: state.umo,
    watchStatus: state.watch.status,
    cardKind: state.cards.kind,
    cardRenderer: state.cards.renderer,
  };
}

const prefSignature = (payload) => JSON.stringify(payload);

let savedSignature = "";
let saveTimer = 0;

/** 启动时把后端已存的偏好当成「已保存」的基线，避免首屏立刻回写一次。 */
function seedSavedPrefs(payload) {
  state.dense = !!payload.dense;
  if (typeof payload.logLevel === "string") state.logLevel = payload.logLevel;
  if (typeof payload.umo === "string") state.umo = payload.umo;
  if (typeof payload.watchStatus === "string") state.watch.status = payload.watchStatus;
  if (KIND_LABEL[payload.cardKind]) state.cards.kind = payload.cardKind;
  if (typeof payload.cardRenderer === "string") state.cards.renderer = payload.cardRenderer;
  savedSignature = prefSignature(prefPayload());
}

function saveState() {
  if (!state.ready) return;
  clearTimeout(saveTimer);
  saveTimer = setTimeout(async () => {
    const payload = prefPayload();
    const signature = prefSignature(payload);
    if (signature === savedSignature) return;
    savedSignature = signature;
    try {
      await apiPost("state", { state: payload });
    } catch {
      // 存偏好失败不值得打扰用户，但要允许下次重试。
      savedSignature = "";
    }
  }, PREF_SAVE_DELAY);
}

/** 注意：本插件 GET /state 直接返回偏好对象本身，这里两种形状都兼容。 */
async function loadPrefs() {
  try {
    const payload = await apiGet("state");
    if (!payload || typeof payload !== "object") return null;
    const inner = payload.state;
    return inner && typeof inner === "object" ? inner : payload;
  } catch {
    return null;
  }
}

/* 标签栏与路由 */

function tabCount(key) {
  const store = state.overview?.store;
  if (key === "watch") return store?.watchlist || 0;
  if (key === "subs") return store?.subscriptions || 0;
  if (key === "commands") return state.meta?.counts?.commands || 0;
  return 0;
}

function paintTabs() {
  const bar = $("#tabbar");
  if (!bar) return;
  bar.innerHTML = VIEWS.map((item) => {
    const count = tabCount(item.key);
    return (
      `<a class="tab${item.key === state.view ? " is-active" : ""}" href="#/${attr(item.key)}" title="${attr(item.tip)}">` +
      icon(item.icon, "sm") +
      `<span>${esc(item.label)}</span>` +
      (count ? `<span class="tab-count">${esc(count)}</span>` : "") +
      `</a>`
    );
  }).join("");
}

const viewFromHash = () => {
  const raw = String(location.hash || "")
    .replace(/^#\/?/, "")
    .split("?")[0];
  return VIEW_KEYS.includes(raw) ? raw : DEFAULT_VIEW;
};

function go(view) {
  const next = VIEW_KEYS.includes(view) ? view : DEFAULT_VIEW;
  if (viewFromHash() === next) {
    state.view = next;
    render(next);
  } else {
    location.hash = "#/" + next;
  }
}

function onRoute() {
  const view = viewFromHash();
  const changed = view !== state.view;
  state.view = view;
  render(view);
  if (changed) saveState();
}

/**
 * 视图数据是懒加载的：第一次进某个视图才去拉它的数据。
 *
 * 这里刻意「无论成败都标记为已加载」——否则 render 会再次触发 loadView，
 * 失败时就变成无限重试风暴。想重来请点右上角刷新（force = true）。
 */
const VIEW_LOADED = new Set();

async function loadView(view, { force = false } = {}) {
  const loader = LOADERS[view];
  if (!loader) return;
  if (!force && VIEW_LOADED.has(view)) return;
  VIEW_LOADED.add(view);
  try {
    await loader();
  } catch (error) {
    toast(errText(error), "err", 6000);
  }
  if (state.view === view) render(view);
}

/**
 * 渲染一个视图：整块 innerHTML 替换 + 事件委托，不做虚拟 DOM。
 *
 * 每个视图实现 RENDERERS[key]()，只能读当前 state（同步、纯函数）；
 * 需要拉数据的写进 LOADERS[key]()，拉完会自动重渲染。
 */
function render(view = state.view) {
  const key = VIEW_KEYS.includes(view) ? view : DEFAULT_VIEW;
  $$(".view").forEach((section) => {
    const own = section.dataset.view === key;
    section.hidden = !own;
    section.classList.toggle("is-scroll", own && SCROLL_VIEWS.has(key));
    section.classList.toggle("is-fill", own && !SCROLL_VIEWS.has(key));
  });
  const host = $(`.view[data-view="${key}"]`);
  if (!host) return;
  const renderer = RENDERERS[key];
  host.innerHTML = typeof renderer === "function" ? renderer() : "";
  paintTabs();
  paintStatus();
  void loadView(key);
}

function paintStatus() {
  const meta = state.meta;
  const left = $("#status-left");
  const right = $("#status-right");
  if (left) {
    const theme = state.themeMap.get(state.theme);
    left.textContent = meta
      ? `${meta.brand} v${meta.version} · ${theme ? theme.name : state.theme}${state.dense ? " · 紧凑" : ""}`
      : "正在连接…";
  }
  if (!right) return;
  const overview = state.overview;
  if (!overview) {
    right.textContent = meta ? `${meta.counts.commands} 条指令 · ${meta.counts.aliases} 个别名` : "";
    return;
  }
  const store = overview.store || {};
  const sched = overview.scheduler || {};
  right.textContent = [
    `追番 ${num(store.watchlist)}`,
    `订阅 ${num(store.subscriptions)}`,
    `调度 ${sched.running || "未启动"}`,
    `已运行 ${duration(overview.uptime)}`,
  ].join(" · ");
}

/* --- HTML 片段构造器 ------------------------------------------------------ */

function viewbar(title, sub, actions = "") {
  return (
    `<div class="viewbar"><div><h1>${esc(title)}</h1><small>${esc(sub)}</small></div>` +
    (actions ? `<div class="row">${actions}</div>` : "") +
    `</div>`
  );
}

function panel({ eyebrow = "", title = "", desc = "", actions = "", body = "", foot = "", cls = "" }) {
  const hasHead = eyebrow || title || desc || actions;
  const head = hasHead
    ? `<div class="panel-head">` +
      (eyebrow ? `<span class="eyebrow">${esc(eyebrow)}</span>` : "") +
      (title ? `<h2>${esc(title)}</h2>` : "") +
      (desc ? `<p>${esc(desc)}</p>` : "") +
      (actions ? `<div class="head-actions">${actions}</div>` : "") +
      `</div>`
    : "";
  return (
    `<section class="panel${cls ? " " + cls : ""}">` +
    head +
    `<div class="panel-body">${body}</div>` +
    (foot ? `<div class="panel-foot">${foot}</div>` : "") +
    `</section>`
  );
}

function emptyState(title, hint = "", action = "", glyph = "info") {
  return (
    `<div class="empty"><span class="empty-glyph">${icon(glyph, "xl")}</span>` +
    `<strong>${esc(title)}</strong>` +
    (hint ? `<span>${esc(hint)}</span>` : "") +
    (action ? `<div class="row">${action}</div>` : "") +
    `</div>`
  );
}

function btn(label, { act = "", kind = "", glyph = "", sm = false, arg = "", title = "", block = false, disabled = false } = {}) {
  const cls = ["btn", kind, sm ? "sm" : "", block ? "block" : ""].filter(Boolean).join(" ");
  return (
    `<button class="${cls}" type="button"` +
    (act ? ` data-act="${attr(act)}"` : "") +
    (arg ? ` data-arg="${attr(arg)}"` : "") +
    (title ? ` title="${attr(title)}"` : "") +
    (disabled ? " disabled" : "") +
    `>${glyph ? icon(glyph, "sm") : ""}<span>${esc(label)}</span></button>`
  );
}

function iconBtn(glyph, { act = "", arg = "", title = "", kind = "", xs = false } = {}) {
  const cls = ["icon-btn", kind, xs ? "xs" : ""].filter(Boolean).join(" ");
  return (
    `<button class="${cls}" type="button" aria-label="${attr(title)}" title="${attr(title)}"` +
    (act ? ` data-act="${attr(act)}"` : "") +
    (arg ? ` data-arg="${attr(arg)}"` : "") +
    `>${icon(glyph, xs ? "sm" : "")}</button>`
  );
}

function metric(label, value, { foot = "", tone = "", glyph = "", small = false } = {}) {
  return (
    `<div class="metric${tone ? " " + tone : ""}">` +
    `<span class="metric-label">${glyph ? icon(glyph, "sm") : ""}${esc(label)}</span>` +
    `<span class="metric-value${small ? " sm" : ""}">${esc(value)}</span>` +
    (foot ? `<span class="metric-foot">${esc(foot)}</span>` : "") +
    `</div>`
  );
}

function kv(rows) {
  const body = rows
    .filter(Boolean)
    .map(([label, value]) => `<dt>${esc(label)}</dt><dd>${esc(value)}</dd>`)
    .join("");
  return `<dl class="kv">${body}</dl>`;
}

function note(text, tone = "") {
  const glyph = tone === "danger" ? "close" : tone === "warn" ? "info" : "info";
  return `<p class="note${tone ? " " + tone : ""}">${icon(glyph, "sm")}<span>${esc(text)}</span></p>`;
}

const badge = (text, tone = "") => `<span class="badge${tone ? " " + tone : ""}">${esc(text)}</span>`;
const chip = (text, cls = "") => `<span class="chip${cls ? " " + cls : ""}">${esc(text)}</span>`;

function selectHtml(name, options, current, { act = "select", arg = "" } = {}) {
  const body = options
    .map(
      ([value, label]) =>
        `<option value="${attr(value)}"${String(value) === String(current) ? " selected" : ""}>${esc(label)}</option>`,
    )
    .join("");
  return (
    `<span class="select-wrap"><select data-act="${attr(act)}" data-name="${attr(name)}"` +
    (arg ? ` data-arg="${attr(arg)}"` : "") +
    `>${body}</select></span>`
  );
}

function segmented(options, current, act) {
  return (
    `<div class="segmented">` +
    options
      .map(
        ([value, label]) =>
          `<button type="button" data-act="${attr(act)}" data-arg="${attr(value)}"${String(value) === String(current) ? ' class="is-active"' : ""}>${esc(label)}</button>`,
      )
      .join("") +
    `</div>`
  );
}

function switchHtml(text, checked, { act = "", arg = "", name = "", disabled = false } = {}) {
  return (
    `<label class="switch${disabled ? " is-locked" : ""}"><input type="checkbox"${checked ? " checked" : ""}` +
    (act ? ` data-act="${attr(act)}"` : "") +
    (arg ? ` data-arg="${attr(arg)}"` : "") +
    (name ? ` data-name="${attr(name)}"` : "") +
    (disabled ? " disabled" : "") +
    `/><span class="switch-track"></span><span class="switch-text">${esc(text)}</span></label>`
  );
}
/* --- 数据加载 ------------------------------------------------------------- */

async function loadOverview() {
  state.overview = await apiGet("overview");
  paintStatus();
}

async function loadLogs() {
  const payload = await apiGet("logs", { limit: 120, level: state.logLevel || "" });
  state.logs = Array.isArray(payload?.entries) ? payload.entries : [];
}

/**
 * 会话列表是所有「按会话操作」的前置条件。
 * 会话 ID（unified_msg_origin）不可能让人手打，所以只提供下拉选择；
 * 没选过就默认落在最活跃的那个会话上。
 */
async function ensureSessions({ force = false } = {}) {
  if (!force && state.sessions.length) return;
  const payload = await apiGet("sessions");
  state.sessions = Array.isArray(payload?.items) ? payload.items : [];
  const known = state.sessions.some((row) => row.umo === state.umo);
  if (!known) state.umo = state.sessions.length ? state.sessions[0].umo : "";
}

async function loadWatch() {
  state.watch = await apiGet("watchlist", { umo: state.umo, status: state.watch.status || "" });
}

async function loadSubs() {
  state.subs = await apiGet("subs", { umo: state.umo });
}

/**
 * ani-rss 同步面板的数据。
 *
 * 刻意不做缓存：后端每次都会真的去连一趟本地 ani-rss，面板打开那一刻要看到的是
 * 「现在通不通」，而不是上次打开时的结论 —— 用户来这一页多半就是因为怀疑它没通。
 */
async function loadAnirss() {
  state.anirss = await apiGet("anirss");
  state.anirssDraft = (state.anirss?.targets || []).join("\n");
}

/**
 * 排除项：可勾的预设清单 + 这个会话已经勾上的 + 只读的全局层。
 *
 * 后端存的是**预设名**而不是展开后的关键词，所以回显时要把「不在预设表里的」
 * 归到自定义输入框 —— 否则用户自己加的词会在界面上凭空消失。
 *
 * 全局层（配置页设、所有会话都吃）在这里只读展示：面板改不了它，但过滤结果
 * 是两层叠加的，不显示出来用户会把「我没勾却被过滤了」当成 bug。
 */
async function loadExcludes() {
  const payload = await apiGet("excludes", { umo: state.umo || "" });
  const presets = Array.isArray(payload?.presets) ? payload.presets : [];
  const names = new Set(presets.map((row) => row.name));
  const chosen = Array.isArray(payload?.chosen) ? payload.chosen.map(String) : [];
  excludeDraft = {
    presets,
    picked: chosen.filter((name) => names.has(name)),
    custom: chosen.filter((name) => !names.has(name)).join(" "),
    shared: Array.isArray(payload?.global) ? payload.global.map(String) : [],
    dedup: payload?.episode_dedup !== false,
    prefer: Array.isArray(payload?.episode_prefer) ? payload.episode_prefer.map(String) : [],
    window: Number(payload?.episode_window_hours) || 0,
    saved: "",
  };
  excludeDraft.saved = JSON.stringify(excludeValues());
}

const LOADERS = {
  overview: async () => {
    await Promise.all([loadOverview(), loadLogs()]);
  },
  config: async () => {
    state.config = await apiGet("config");
    state.configDraft.clear();
  },
  watch: async () => {
    await ensureSessions();
    await loadWatch();
  },
  subs: async () => {
    await ensureSessions();
    await Promise.all([loadSubs(), loadExcludes()]);
  },
  targets: async () => {
    await ensureSessions();
    state.targets = await apiGet("targets");
    state.targetsDraft = (state.targets.configured || []).join("\n");
  },
  anirss: async () => {
    // Webhook 反推那块要看接收开关/端口/计数，这些都在概览载荷里，顺手取一次。
    await ensureSessions();
    await Promise.all([loadAnirss(), state.overview ? Promise.resolve() : loadOverview()]);
  },
  sources: async () => {
    if (!state.overview) await loadOverview();
  },
  cards: async () => {
    if (!state.overview) await loadOverview();
  },
};

/* --- 视图：概览 ----------------------------------------------------------- */

const RENDERERS = {};

RENDERERS.overview = () => {
  const overview = state.overview;
  if (!overview) return viewbar("概览", "正在读取运行状态…") + skeletonDeck(6);

  const store = overview.store || {};
  const http = overview.http || {};
  const cache = http.cache || {};
  const sched = overview.scheduler || {};
  const notifier = overview.notifier || {};
  const webhook = overview.webhook || {};
  const renderStats = overview.render || {};
  const backends = renderStats.backends || {};
  const sources = overview.sources || {};
  const listener = overview.listener || {};
  const conf = overview.config || {};

  const runtime = panel({
    eyebrow: "runtime",
    title: "运行状态",
    desc: "调度循环对齐整分钟触发，因此下一次时间是准确的钟点，不是「大约」。",
    body:
      `<div class="metrics">` +
      metric("已运行", duration(overview.uptime), { glyph: "clock" }) +
      metric("调度循环", sched.running || "未启动", {
        tone: sched.running === "运行中" ? "ok" : "warn",
        small: true,
        foot: sched.last_tick ? "上次心跳 " + sched.last_tick : "尚未心跳",
      }) +
      metric("下次播报", sched.next_push || "—", {
        small: true,
        foot: sched.push_enabled ? "每日播报已开启" : "每日播报已关闭",
      }) +
      metric("下次轮询", sched.next_rss || "—", {
        small: true,
        foot: sched.rss_enabled ? "RSS 订阅已开启" : "RSS 订阅已关闭",
      }) +
      metric("轮询次数", num(sched.polls), { foot: "已推送 " + num(sched.pushes) + " 次播报" }) +
      metric("调度错误", num(sched.errors), { tone: sched.errors ? "warn" : "ok" }) +
      `</div>`,
  });

  const data = panel({
    eyebrow: "data",
    title: "数据与存储",
    desc: "全部落在插件数据目录的 SQLite 里，卸载插件不会污染 AstrBot 主库。",
    body:
      `<div class="metrics">` +
      metric("追番记录", num(store.watchlist), { glyph: "heart", foot: "在看 " + num(store.watching) }) +
      metric("RSS 订阅", num(store.subscriptions), {
        glyph: "rss",
        foot: "启用 " + num(store.subscriptions_enabled),
      }) +
      metric("涉及会话", num(store.sessions), { foot: "去重后的 umo 数量" }) +
      metric("去重历史", num(store.history), { foot: "按配置的天数自动清理" }) +
      metric("数据库", bytes(store.db_bytes), { small: true, glyph: "save" }) +
      `</div>`,
  });

  const network = panel({
    eyebrow: "network",
    title: "网络与缓存",
    desc: "所有出网请求共用一个带缓存与并发闸门的客户端，命中率越高越省流量。",
    body:
      `<div class="metrics">` +
      metric("请求总数", num(http.requests)) +
      metric("失败次数", num(http.failures), { tone: http.failures ? "warn" : "ok" }) +
      metric("缓存命中", (cache.hit_rate || 0) + "%", {
        tone: (cache.hit_rate || 0) >= 40 ? "ok" : "",
        foot: num(cache.hits) + " 命中 / " + num(cache.misses) + " 未命中",
      }) +
      metric("缓存条目", num(cache.entries), { foot: "TTL " + num(conf.cache_ttl_minutes) + " 分钟" }) +
      `</div>` +
      kv([
        ["代理", http.proxy || "（直连）"],
        ["User-Agent", conf.user_agent || "（默认）"],
        ["Bangumi Token", conf.bangumi_access_token ? "已配置" : "未配置（匿名访问）"],
      ]),
  });

  const sourceCard = panel({
    eyebrow: "sources",
    title: "数据源缓存",
    desc: "跨源匹配靠 bangumi-data 当粘合剂，它的条目越多，匹配命中率越高。",
    body:
      `<div class="metrics">` +
      metric("bangumi-data", num(sources.bangumi_data?.items), {
        small: true,
        foot: num(sources.bangumi_data?.months) + " 个月份 / " + num(sources.bangumi_data?.aliases) + " 条别名",
      }) +
      metric("anime1 索引", num(sources.anime1?.entries), {
        small: true,
        foot: sources.anime1?.fetched_at ? "更新于 " + relative(sources.anime1.fetched_at) : "尚未抓取",
      }) +
      metric("長門番堂", num(sources.yuc?.entries), {
        small: true,
        foot: num(sources.yuc?.seasons) + " 个季度已缓存",
      }) +
      metric("AGE 推荐", num(sources.age?.items), { small: true, foot: "推荐位条目" }) +
      `</div>`,
    foot: btn("刷新 anime1 索引", { act: "refresh-anime1", glyph: "refresh", sm: true, kind: "ghost" }),
  });

  const renderCard = panel({
    eyebrow: "render",
    title: "卡片渲染",
    desc: "回退链：HTML 渲染 → Pillow 绘制 → AstrBot 文转图 → 纯文本，任一环失败自动降级。",
    body:
      `<div class="metrics">` +
      metric("HTML", num(backends.html), { glyph: "cards" }) +
      metric("Pillow", num(backends.raster)) +
      metric("文转图", num(backends.t2i)) +
      metric("纯文本", num(backends.text)) +
      `</div>` +
      `<div class="chips">` +
      badge("当前偏好 " + (RENDERER_LABEL[conf.card_renderer] || conf.card_renderer || "auto"), "accent") +
      badge("Pillow " + (renderStats.pillow ? "可用" : "缺失"), renderStats.pillow ? "ok" : "warn") +
      badge("中文字体 " + (renderStats.font ? "已就绪" : "未找到"), renderStats.font ? "ok" : "warn") +
      (renderStats.html_cooling_down ? badge("HTML 渲染冷却中", "err") : "") +
      `</div>` +
      (renderStats.html_cooling_down
        ? note("HTML 渲染连续失败，已临时切到 Pillow，两分钟后自动重试。常见原因是 AstrBot 未装浏览器内核或截图服务不可达。", "warn")
        : ""),
  });

  const push = panel({
    eyebrow: "notify",
    title: "通知与 Webhook",
    desc: "通知走去重 + 指数退避 + 并发限流，重复回调不会把群刷爆。",
    body:
      `<div class="metrics">` +
      metric("已发送", num(notifier.sent), { tone: "ok" }) +
      metric("发送失败", num(notifier.failed), { tone: notifier.failed ? "warn" : "" }) +
      metric("去重跳过", num(notifier.skipped), { foot: "缓存 " + num(notifier.dedup_cached) + " 条指纹" }) +
      metric("Webhook 收到", num(webhook.received), {
        foot: "已投递 " + num(webhook.delivered) + " / 拒绝 " + num(webhook.rejected),
      }) +
      `</div>` +
      kv([
        ["Webhook 路由", webhook.route || "（未启用）"],
        ["Webhook 开关", webhook.enabled ? "已开启" : "已关闭"],
        ["令牌", webhook.token_set ? "已设置" : "未设置"],
        ["最近一次", webhook.last_at ? relative(webhook.last_at) + "（" + (webhook.last_kind || "未知事件") + "）" : "从未"],
        ["独立监听", listener.running ? listener.host + ":" + listener.port + listener.route : "未启动"],
        ["人格转述", conf.persona_reply_enabled ? "已开启" : "已关闭"],
      ]) +
      (webhook.enabled && !webhook.token_set
        ? note("Webhook 已开启但没有设置令牌。若同时开了独立监听端口，插件会拒绝启动监听——请先在「配置 · Webhook 接入」里填一个足够长的随机串。", "danger")
        : ""),
  });

  const actions = panel({
    eyebrow: "actions",
    title: "快速操作",
    desc: "手动触发一次，用来验证配置是否真的生效，不必等到整点。",
    body:
      `<div class="row">` +
      btn("立即播报今日新番", { act: "push-now", glyph: "play", kind: "primary", sm: true }) +
      btn("立即抓取 RSS", { act: "poll-now", glyph: "rss", sm: true }) +
      btn("刷新 anime1 索引", { act: "refresh-anime1", glyph: "refresh", sm: true }) +
      btn("数据源体检", { act: "diagnose", glyph: "stethoscope", sm: true }) +
      btn("发一条 Webhook 测试", { act: "webhook-test", glyph: "wand", sm: true }) +
      `</div>` +
      note("「立即播报」会真的发消息到当前生效的播报目标，请先在「播报」页确认目标列表。", "warn"),
  });

  // 抽番是聊天里的娱乐指令，但把它放进面板有个实际用处：
  // 不用切到聊天窗口就能确认「長門番堂 / Bangumi 的季度数据到底通不通」。
  const gachaPanel = panel({
    eyebrow: "gacha",
    title: "抽番试玩",
    desc: "和聊天里的 /抽番 是同一条代码路径，可以顺手验证季度数据源是否可用。",
    body:
      `<div class="row">` +
      `<input type="text" class="grow" placeholder="题材（可留空，例如：科幻 / 日常 / 治愈）" value="${attr(state.gacha.genre)}" data-live="gacha-genre" data-enter="gacha-draw" />` +
      btn("抽一部", { act: "gacha-draw", glyph: "dice", kind: "primary", sm: true }) +
      (state.gacha.text ? btn("清空", { act: "gacha-clear", glyph: "close", sm: true, kind: "ghost" }) : "") +
      `</div>` +
      (state.gacha.text
        ? `<pre class="output">${esc(state.gacha.text)}</pre>`
        : note("留空题材就是全随机；填了题材会先按题材筛，筛不到再退回全随机。")),
  });
  const logs = panel({
    eyebrow: "activity",
    title: "活动日志",
    desc: "插件自己的环形日志（最多 240 条），比翻 AstrBot 全局日志快得多。",
    actions:
      segmented(
        LOG_LEVELS.map((item) => [item.key, item.label]),
        state.logLevel,
        "log-level",
      ) + iconBtn("trash", { act: "clear-logs", title: "清空活动日志", kind: "danger", xs: true }),
    body: state.logs.length
      ? `<div class="logs">` +
        state.logs
          .map(
            (row) =>
              `<div class="logline${row.level === "warn" ? " warn" : row.level === "error" ? " error" : ""}">` +
              `<time>${esc(row.time || clock(row.at))}</time>` +
              `<span class="scope">${esc(row.scope || "-")}</span>` +
              `<span class="msg">${esc(row.message || "")}</span>` +
              `</div>`,
          )
          .join("") +
        `</div>`
      : emptyState("暂时没有日志", "插件启动后的每一次抓取、推送与配置改动都会记在这里。", "", "commands"),
  });

  return (
    viewbar(
      "概览",
      "一屏看完运行状态、数据规模与最近发生的事",
      btn("刷新", { act: "reload", glyph: "refresh", sm: true, kind: "ghost" }),
    ) +
    `<div class="deck auto">${runtime}${data}${network}${sourceCard}${renderCard}${push}</div>` +
    `<div class="deck wide" style="margin-top:var(--gap)">${actions}${gachaPanel}${logs}</div>`
  );
};

function skeletonDeck(count) {
  const one =
    `<section class="panel"><div class="panel-body">` +
    `<div class="skeleton line" style="width:38%"></div>` +
    `<div class="skeleton block"></div>` +
    `</div></section>`;
  return `<div class="deck auto">${one.repeat(count)}</div>`;
}
/* --- 视图：配置 ----------------------------------------------------------- */

/** 把后端存的值渲染成表单里的字符串形态；列表用换行、对象用缩进 JSON。 */
function fieldText(field, value) {
  if (field.type === "list") {
    return Array.isArray(value) ? value.join("\n") : String(value ?? "");
  }
  if (field.type === "object") {
    try {
      return JSON.stringify(value ?? {}, null, 2);
    } catch {
      return "{}";
    }
  }
  return value === null || value === undefined ? "" : String(value);
}

/** 草稿优先：用户改了一半还没保存时，切标签页回来不该丢。 */
function fieldValue(field) {
  if (state.configDraft.has(field.key)) return state.configDraft.get(field.key);
  return state.config?.values?.[field.key];
}

const CHOICE_LABEL = (key, value) => {
  if (key === "card_renderer") return RENDERER_LABEL[value] || value;
  if (key === "push_sort_by") return SORT_KEY_LABEL[value] || value;
  if (key === "push_sort_order") return value === "desc" ? "降序" : "升序";
  if (key === "card_theme" || key === "webui_theme") {
    const theme = state.themeMap.get(value);
    return theme ? theme.name + "（" + value + "）" : value;
  }
  if (key === "gacha_source") {
    return { auto: "自动（优先長門番堂）", yuc: "長門番堂", bangumi: "Bangumi" }[value] || value;
  }
  return value;
};

function configField(field) {
  const disabled = state.config?.writable === false;
  const dirty = state.configDraft.has(field.key);
  const raw = fieldValue(field);
  const bind = ` data-bind="${attr(field.key)}" data-kind="${attr(field.type)}"${disabled ? " disabled" : ""}`;
  let control = "";

  if (field.type === "bool") {
    control =
      `<label class="switch"><input type="checkbox"${raw ? " checked" : ""}${bind}/>` +
      `<span class="switch-track"></span><span class="switch-text">${raw ? "已开启" : "已关闭"}</span></label>`;
  } else if (Array.isArray(field.choices) && field.choices.length) {
    control =
      `<span class="select-wrap"><select${bind}>` +
      field.choices
        .map(
          (value) =>
            `<option value="${attr(value)}"${String(value) === String(raw ?? "") ? " selected" : ""}>${esc(CHOICE_LABEL(field.key, value))}</option>`,
        )
        .join("") +
      `</select></span>`;
  } else if (field.type === "int" || field.type === "float") {
    const step = field.type === "float" ? "0.1" : "1";
    control = `<input type="number" step="${step}" value="${attr(raw ?? 0)}"${bind}/>`;
  } else if (field.type === "text" || field.type === "list" || field.type === "object") {
    const placeholder =
      field.type === "list" ? "每行一项，也接受逗号分隔" : field.type === "object" ? "JSON 对象" : "";
    control = `<textarea placeholder="${attr(placeholder)}"${bind}>${esc(fieldText(field, raw))}</textarea>`;
  } else if (field.secret) {
    // 敏感字段后端不回显明文，这里保持空值 + 提示：留空即「不修改」。
    const isSet = !!state.config?.secrets?.[field.key] || !!raw;
    control = `<input type="password" autocomplete="new-password" value="" placeholder="${attr(isSet ? "已设置，留空则不修改" : "尚未设置")}"${bind}/>`;
  } else {
    control = `<input type="text" value="${attr(raw ?? "")}"${bind}/>`;
  }

  const defaultHint =
    field.type === "bool" || field.secret || field.default === undefined || field.default === ""
      ? ""
      : "默认 " + fieldText(field, field.default);
  const hint = [field.hint, defaultHint].filter(Boolean).join(" · ");

  return (
    `<div class="field">` +
    `<span class="field-label">${esc(field.label)}${dirty ? badge("已修改", "accent") : ""}` +
    `<code class="mono" style="margin-inline-start:auto;color:var(--faint);font-size:10px">${esc(field.key)}</code></span>` +
    control +
    (hint ? `<span class="field-hint">${esc(hint)}</span>` : "") +
    `</div>`
  );
}

const GROUP_ICON = {
  render: "cards",
  network: "source",
  search: "search",
  push: "bell",
  persona: "wand",
  rss: "rss",
  webhook: "link",
  anime1: "tv",
  anirss: "sync",
  delivery: "upload",
  misc: "config",
  other: "config",
};

RENDERERS.config = () => {
  const config = state.config;
  if (!config) return viewbar("配置", "正在读取配置…") + skeletonDeck(4);

  const dirty = state.configDraft.size;
  const actions =
    (dirty ? badge(dirty + " 项待保存", "accent") : badge("没有未保存的改动")) +
    btn("保存", { act: "config-save", glyph: "save", kind: "primary", sm: true, disabled: !dirty || config.writable === false }) +
    btn("放弃改动", { act: "config-reset", glyph: "close", kind: "ghost", sm: true, disabled: !dirty });

  const panels = (config.groups || [])
    .map((group) =>
      panel({
        eyebrow: group.key,
        title: group.title,
        actions: icon(GROUP_ICON[group.key] || "config", "lg"),
        body: (group.fields || []).map(configField).join(""),
      }),
    )
    .join("");

  const warning =
    config.writable === false
      ? note("当前运行环境不支持从面板写配置，这里只能查看。请到 AstrBot 的插件配置页修改。", "danger")
      : "";

  return (
    viewbar("配置", "共 " + (config.groups || []).reduce((sum, g) => sum + (g.fields || []).length, 0) + " 项，改完记得点保存", actions) +
    (warning ? `<div style="margin-bottom:var(--gap)">${warning}</div>` : "") +
    `<div class="deck auto">${panels}</div>`
  );
};

/** 把表单控件的值收敛成后端 coerce_config_value 认识的形状。 */
function readFieldValue(node) {
  const kind = node.dataset.kind || "string";
  if (kind === "bool") return node.checked;
  if (kind === "int") return Math.trunc(Number(node.value) || 0);
  if (kind === "float") return Number(node.value) || 0;
  if (kind === "list") {
    return String(node.value || "")
      .split(/[\n,，]/)
      .map((part) => part.trim())
      .filter(Boolean);
  }
  if (kind === "object") {
    const text = String(node.value || "").trim();
    if (!text) return {};
    return JSON.parse(text);
  }
  return String(node.value ?? "");
}

async function saveConfig(node) {
  if (!state.configDraft.size) return;
  const patch = Object.fromEntries(state.configDraft);
  const result = await withBusy(node, () => apiPost("config", { patch }));
  if (!result) return;
  state.configDraft.clear();
  state.config = await apiGet("config");
  VIEW_LOADED.delete("overview");
  const changed = Array.isArray(result.changed) ? result.changed.length : 0;
  toast("已保存 " + (changed || Object.keys(patch).length) + " 项配置", "ok");
  render("config");
}

/* --- 视图：追番 ----------------------------------------------------------- */

/**
 * 会话选择器。
 *
 * unified_msg_origin 是「平台:消息类型:ID」的长串，没人愿意手打，
 * 所以按会话操作的三个视图（追番 / 订阅 / 播报）统一只提供下拉选择。
 */
function sessionPicker() {
  if (!state.sessions.length) return badge("还没有任何会话记录");
  const options = state.sessions.map((row) => [
    row.umo,
    shortUmo(row.umo) + " · 追番 " + (row.watch || 0) + " / 订阅 " + (row.subscriptions || 0),
  ]);
  return selectHtml("umo", options, state.umo, { act: "pick-umo" });
}

/** 空列表没有信息量，真正有用的是告诉用户「怎样才会出现会话」。 */
function noSessionHint(sample) {
  return emptyState(
    "还没有可管理的会话",
    "会话就是插件在聊天里见过的那个窗口。先在群里或私聊发一次 " + sample + "，这里就会出现它。",
    "",
    "info",
  );
}

const WATCH_TONE = { watching: "accent", planned: "", finished: "ok", dropped: "warn" };

const watchStatusOptions = () =>
  (state.meta?.options?.watch_statuses || []).map((row) => [row.key, row.label]);

// 展开中的追番记录 id。评分 / 总集数 / 备注都是低频编辑，
// 默认收起，免得每一行都拖着一排输入框。
let watchEditing = "";
// 「加入追番」后端会顺带回一批可订阅的源，先存这里等用户一键订阅。
let watchSuggest = { title: "", items: [] };

function watchEditor(item) {
  const id = String(item.id);
  return (
    `<div class="list-row" style="background:var(--lift)">` +
    `<div class="list-main">` +
    `<span class="list-sub mono">${esc(item.title)}</span>` +
    `<div class="row tight">` +
    `<span class="field-hint">评分</span>` +
    `<input type="number" min="0" max="10" step="0.5" style="width:84px" value="${attr(item.score ?? 0)}" data-act="watch-score" data-arg="${attr(id)}" title="0 ~ 10，填 0 表示还没打分" />` +
    `<span class="field-hint">总集数</span>` +
    `<input type="number" min="0" step="1" style="width:84px" value="${attr(item.total ?? 0)}" data-act="watch-total" data-arg="${attr(id)}" title="填 0 表示未知；填了才会有进度条" />` +
    `<input type="text" class="grow" placeholder="备注：在哪看、追到哪、想说点什么（最多 200 字）" value="${attr(item.note || "")}" data-act="watch-note" data-arg="${attr(id)}" />` +
    `</div></div></div>`
  );
}

function watchRow(item) {
  const id = String(item.id);
  const percent = Math.max(0, Math.min(100, Number(item.percent) || 0));
  const cover = item.cover
    ? `<img class="thumb" src="${attr(item.cover)}" alt="" loading="lazy" />`
    : `<span class="thumb"></span>`;
  const facts = [
    item.weekday || "",
    Number(item.score) > 0 ? "我给 " + item.score + " 分" : "",
    item.note ? "备注：" + item.note : "",
    item.updated_at ? "更新于 " + relative(item.updated_at) : "",
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    `<div class="list-row">` +
    cover +
    `<div class="list-main">` +
    `<span class="list-title"><span class="text">${esc(item.title)}</span>` +
    badge(item.status_label || item.status, WATCH_TONE[item.status] || "") +
    `</span>` +
    `<span class="list-sub">${esc(item.progress_label || "")}${facts ? " · " + esc(facts) : ""}</span>` +
    `<span class="progress"><i style="width:${percent}%"></i></span>` +
    `</div>` +
    `<div class="list-actions">` +
    `<input type="number" min="0" step="1" value="${attr(item.progress ?? 0)}" data-act="watch-progress" data-arg="${attr(id)}" title="看到第几集，改完回车或点别处即保存" />` +
    selectHtml("status", watchStatusOptions(), item.status, { act: "watch-status-set", arg: id }) +
    iconBtn(watchEditing === id ? "close" : "wand", {
      act: "watch-edit",
      arg: id,
      title: "评分 / 总集数 / 备注",
      xs: true,
    }) +
    iconBtn("trash", { act: "watch-delete", arg: id, title: "从追番清单里移除", kind: "danger", xs: true }) +
    `</div></div>` +
    (watchEditing === id ? watchEditor(item) : "")
  );
}

function searchResultRow(row) {
  const query = row.display_name || row.name || String(row.id || "");
  const cover = row.image
    ? `<img class="thumb" src="${attr(row.image)}" alt="" loading="lazy" />`
    : `<span class="thumb"></span>`;
  const facts = [
    row.type_label,
    row.score_label,
    row.air_date,
    row.weekday_label,
    row.eps ? row.eps + " 集" : "",
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    `<div class="list-row">` +
    cover +
    `<div class="list-main">` +
    `<span class="list-title"><span class="text">${esc(query)}</span>` +
    (row.doing ? badge(num(row.doing) + " 人在看", "accent") : "") +
    `</span>` +
    `<span class="list-sub">${esc(facts)}</span>` +
    (row.tags && row.tags.length ? `<span class="list-sub mono">${esc(row.tags.join(" / "))}</span>` : "") +
    `</div>` +
    `<div class="list-actions">` +
    (row.url
      ? `<a class="icon-btn xs" href="${attr(row.url)}" target="_blank" rel="noopener noreferrer" title="在 Bangumi 打开">${icon("link", "sm")}</a>`
      : "") +
    btn("加入追番", { act: "watch-add", arg: query, glyph: "plus", kind: "primary", sm: true }) +
    `</div></div>`
  );
}

RENDERERS.watch = () => {
  const items = state.watch.items || [];
  if (!state.sessions.length && !items.length) {
    return viewbar("追番", "按会话管理追番进度、评分与备注") + panel({ body: noSessionHint("/追番 <番名>") });
  }

  const tally = {};
  items.forEach((row) => {
    tally[row.status] = (tally[row.status] || 0) + 1;
  });
  const summary = watchStatusOptions()
    .map(([key, label]) => (tally[key] ? badge(label + " " + tally[key], WATCH_TONE[key] || "") : ""))
    .filter(Boolean)
    .join(" ");

  const listPanel = panel({
    eyebrow: "watchlist",
    title: "追番清单",
    desc: "集数和状态改完立刻写库，效果与聊天里的 /看到、/弃坑 完全一致。",
    actions: segmented([["", "全部"]].concat(watchStatusOptions()), state.watch.status || "", "watch-filter"),
    body: items.length
      ? `<div class="list">${items.map(watchRow).join("")}</div>`
      : emptyState(
          "这个会话还没有追番记录",
          "用右边的搜索框加一部，或者在聊天里发 /追番 <番名>。",
          "",
          "heart",
        ),
    foot: summary || "",
  });

  const results = state.search.items || [];
  const addPanel = panel({
    eyebrow: "add",
    title: "搜索并加入",
    desc: "搜到的条目会跨源补齐（bangumi-data / anime1 / 長門番堂 / AGE 动漫），加入后还会顺手推荐能订阅的 RSS 源。",
    body:
      `<div class="row">` +
      `<input type="text" class="grow" placeholder="番名（中日英都行），或者直接填 Bangumi 条目 ID" value="${attr(state.search.keyword)}" data-live="search-keyword" data-enter="search-run" />` +
      btn("搜索", { act: "search-run", glyph: "search", kind: "primary", sm: true }) +
      `</div>` +
      (results.length
        ? `<div class="list" style="margin-top:var(--gap)">${results.map(searchResultRow).join("")}</div>`
        : state.search.keyword
          ? note("按下「搜索」查一次；关键词太泛的话结果会很杂，加上年份或原名更准。")
          : ""),
  });

  const suggestPanel = watchSuggest.items.length
    ? panel({
        eyebrow: "suggest",
        title: "顺手订阅它的更新源",
        desc: "这些源来自刚加入的「" + watchSuggest.title + "」的跨源匹配结果，点一下就会订阅到当前会话。",
        body:
          `<div class="chips">` +
          watchSuggest.items
            .map(
              (row) =>
                btn(row.label, { act: "sub-add-url", arg: row.url, glyph: "rss", sm: true, title: row.url }),
            )
            .join("") +
          `</div>`,
        foot: btn("不用了", { act: "suggest-dismiss", kind: "ghost", sm: true }),
      })
    : "";

  return (
    viewbar(
      "追番",
      "共 " + num(state.watch.total || items.length) + " 条记录 · " + shortUmo(state.umo),
      sessionPicker() + btn("刷新", { act: "reload", glyph: "refresh", sm: true, kind: "ghost" }),
    ) +
    (suggestPanel ? `<div class="deck wide" style="margin-bottom:var(--gap)">${suggestPanel}</div>` : "") +
    `<div class="deck wide">${listPanel}${addPanel}</div>`
  );
};
/* --- 视图：订阅 ----------------------------------------------------------- */

function subRow(item) {
  const id = String(item.id);
  const facts = [
    item.subject_id ? "bgm " + item.subject_id : "",
    item.keywords && item.keywords.length ? "必含 " + item.keywords.join(" / ") : "",
    item.excludes && item.excludes.length ? "排除 " + item.excludes.join(" / ") : "",
    item.last_checked ? "上次检查 " + item.last_checked : "",
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    `<div class="list-row${item.enabled ? "" : " is-off"}">` +
    `<div class="list-main">` +
    `<span class="list-title"><span class="text">${esc(item.name)}</span>` +
    (item.enabled ? "" : badge("已暂停", "warn")) +
    (item.error ? badge("抓取出错", "err") : "") +
    `</span>` +
    `<span class="list-sub mono">${esc(item.url)}</span>` +
    (facts ? `<span class="list-sub">${esc(facts)}</span>` : "") +
    (item.last_item ? `<span class="list-sub">最新一条：${esc(item.last_item)}</span>` : "") +
    (item.error ? `<span class="list-sub">${esc(item.error)}</span>` : "") +
    `</div>` +
    `<div class="list-actions">` +
    switchHtml(item.enabled ? "启用" : "暂停", item.enabled, { act: "sub-toggle", arg: id }) +
    iconBtn("stethoscope", { act: "sub-test-row", arg: item.name, title: "立刻抓一次，看这个源通不通", xs: true }) +
    `<a class="icon-btn xs" href="${attr(item.url)}" target="_blank" rel="noopener noreferrer" title="在浏览器里打开这个源">${icon("link", "sm")}</a>` +
    iconBtn("trash", { act: "sub-remove", arg: item.name, title: "删掉这条订阅", kind: "danger", xs: true }) +
    `</div></div>`
  );
}

const SUB_HINTS = [
  ["完整地址", "https://mikanani.me/RSS/Bangumi?bangumiId=3600"],
  ["Mikan 简写", "mikan:3600"],
  ["RSSHub 路由", "rsshub:/bangumi/calendar/today"],
  ["动漫花园", "dmhy:芙莉莲 简体"],
  ["只写番名", "葬送的芙莉莲"],
];

/** 自定义排除词：逗号 / 空格 / 换行都算分隔符，去空去重（忽略大小写）。 */
function excludeCustomWords() {
  const seen = new Set();
  const words = [];
  for (const raw of String(excludeDraft.custom || "").split(/[,，、\s]+/)) {
    const word = raw.trim();
    const key = word.toLowerCase();
    if (!word || seen.has(key)) continue;
    seen.add(key);
    words.push(word);
  }
  return words;
}

/** 提交给后端的原始清单：预设名在前，自定义词在后。 */
function excludeValues() {
  return [...excludeDraft.picked, ...excludeCustomWords()];
}

/**
 * 本地展开一次，只为了让用户看见「实际拿去比对的词」。
 *
 * 展开规则与后端 「expand_excludes」 一致，而预设词表本身也是后端下发的，
 * 所以勾一下就能立刻看到效果，不必为一个预览再跑一趟接口。
 *
 * ⚠ 与后端一样不对词 「trim」：预设里的 「CR 」 靠尾部空格划边界，
 * 预览里抹掉就会跟真实过滤结果对不上（还会跟裸 「CR」 错误地判成重复）。
 */
function excludeExpand(names) {
  const table = new Map(excludeDraft.presets.map((row) => [row.name, row.words || []]));
  const seen = new Set();
  const words = [];
  for (const name of names) {
    for (const raw of table.get(name) || [name]) {
      const word = String(raw);
      const key = word.toLowerCase();
      if (!word.trim() || seen.has(key)) continue;
      seen.add(key);
      words.push(word);
    }
  }
  return words;
}

/** 本会话勾选展开后的词。 */
const excludeExpanded = () => excludeExpand(excludeValues());

/** 两层合并后真正拿去比对的词，与后端 「effective_excludes」 一致。 */
const excludeEffective = () => excludeExpand([...(excludeDraft.shared || []), ...excludeValues()]);

const excludeDirty = () => JSON.stringify(excludeValues()) !== excludeDraft.saved;

function sourcePickRow(item) {
  const tags = (item.tags || []).map((text) => chip(text)).join("");
  return (
    `<div class="list-row">` +
    `<div class="list-main">` +
    `<span class="list-title"><span class="text">${esc(item.index + ". " + item.label)}</span>${tags}</span>` +
    (item.detail ? `<span class="list-sub">${esc(item.detail)}</span>` : "") +
    `<span class="list-sub mono">${esc(item.url)}</span>` +
    `</div>` +
    `<div class="list-actions">` +
    btn("订阅这个", {
      act: "sub-source-pick",
      arg: String(item.index),
      glyph: "rss",
      kind: "primary",
      sm: true,
    }) +
    `<a class="icon-btn xs" href="${attr(item.url)}" target="_blank" rel="noopener noreferrer" title="先在浏览器里看一眼这个源">${icon("link", "sm")}</a>` +
    `</div></div>`
  );
}

/** 选源候选面板。没拉过就不占版面，所以返回空串。 */
function sourcesPanel() {
  if (!subSources.items.length) return "";
  return panel({
    eyebrow: "pick",
    title: "挑一个字幕组",
    desc: "这份候选跟聊天里 /sub 的选源列表完全一致：第 1 项是整部番的合并源（所有组都收），其余每项只收一个组 —— 只订一个组，一集就只推一条。",
    actions: btn("收起", { act: "sub-sources-clear", kind: "ghost", sm: true }),
    body: `<div class="list">${subSources.items.map(sourcePickRow).join("")}</div>`,
    foot: badge(subSources.name + " · " + num(subSources.items.length) + " 个候选"),
  });
}

/** 排除项面板（全局层只读 + 本会话层可改）。预设表拉不到（接口失败）时整块隐藏，避免只剩一个空框。 */
function excludePanel() {
  if (!excludeDraft.presets.length) return "";
  const shared = excludeDraft.shared || [];
  const sharedWords = excludeExpand(shared);
  const effective = excludeEffective();
  const dirty = excludeDirty();
  const prefer = excludeDraft.prefer || [];
  return panel({
    eyebrow: "filter",
    title: "排除项",
    desc:
      "过滤分三层：配置页的全局排除项（所有会话都吃）→ 这里的本会话排除项 → 每条订阅自己的关键词。" +
      "订的是「某个组的某部番」而这个组稳定同发 Baha / ABEMA / CR / B-Global 四版时，" +
      "把不想要的三个片源排掉是最可控的做法 —— 同集归并是先到先得，不保证留下你要的那一版。" +
      "订的是关键词搜索、dmhy 全站这种混多个组的宽源时反过来：片源别排，交给同集归并，" +
      "否则某个组那天没出片就整集收不到。" +
      "勾「繁体」不会误杀「简繁日内封」「[CHS][CHT]」这类双语单文件，要连双语一起躲请勾「简繁」。",
    actions:
      btn(dirty ? "保存改动" : "保存", {
        act: "exclude-save",
        glyph: "check",
        kind: dirty ? "primary" : "ghost",
        sm: true,
      }) +
      btn("回写到已有订阅", {
        act: "exclude-apply",
        glyph: "refresh",
        sm: true,
        title: "把本会话这份清单覆盖到该会话现有的每条订阅上（全局层不用回写）",
      }),
    body:
      `<div class="field">` +
      `<span class="field-label">全局层（只读）</span>` +
      (shared.length
        ? `<div class="chips">${shared.map((name) => chip(name, "mono")).join("")}</div>` +
          `<span class="field-hint">展开成 ${num(sharedWords.length)} 个词，所有会话无条件生效。改它请去「配置 → RSS 与推送 → 全局排除项」。</span>`
        : note("还没设全局排除项。想让所有会话都少收几种版本，去「配置 → RSS 与推送 → 全局排除项」填一次就够。")) +
      `</div>` +
      `<div class="field" style="margin-top:var(--gap)">` +
      `<span class="field-label">本会话排除项</span>` +
      `<div class="chips">` +
      excludeDraft.presets
        .map((row) =>
          switchHtml(row.name, excludeDraft.picked.includes(row.name), {
            act: "exclude-toggle",
            arg: row.name,
          }),
        )
        .join("") +
      `</div>` +
      `</div>` +
      `<div class="field" style="margin-top:var(--gap)">` +
      `<span class="field-label">自定义排除词</span>` +
      `<input type="text" placeholder="逗号或空格分隔，例如：内嵌 修复 v2" value="${attr(excludeDraft.custom)}" data-live="exclude-custom" data-enter="exclude-save" />` +
      `<span class="field-hint">大小写不敏感，命中标题任意位置就跳过那一条。改完记得按回车或点「保存」。</span>` +
      `</div>` +
      (effective.length
        ? `<div class="field" style="margin-top:var(--gap)"><span class="field-label">两层合并后实际过滤</span><div class="chips">${effective.map((word) => chip(word, "mono")).join("")}</div></div>`
        : note("现在没有任何排除项，抓到的条目会原样推送。")),
    foot:
      badge("全局 " + num(shared.length) + " · 本会话 " + num(excludeValues().length) + " → 实际过滤 " + num(effective.length) + " 个词") +
      badge(
        excludeDraft.dedup
          ? "同集归并：开" + (prefer.length ? "（优先 " + prefer.join(" > ") + "）" : "")
          : "同集归并：关",
        excludeDraft.dedup ? "ok" : "warn",
      ) +
      badge(
        excludeDraft.dedup && excludeDraft.window > 0
          ? "跨轮次窗口 " + excludeDraft.window + " 小时"
          : "跨轮次窗口：关（同集跨天发布会推两次）",
        excludeDraft.dedup && excludeDraft.window > 0 ? "ok" : "warn",
      ) +
      (dirty ? badge("有未保存的改动", "warn") : ""),
  });
}

RENDERERS.subs = () => {
  const items = state.subs.items || [];
  if (!state.sessions.length && !items.length) {
    return viewbar("订阅", "RSS 订阅的增删、测试与批量开关") + panel({ body: noSessionHint("/sub <番名>") });
  }

  const limit = state.meta?.limits?.subscriptions_per_session || 0;
  const listPanel = panel({
    eyebrow: "feeds",
    title: "订阅列表",
    desc: "开关只是暂停轮询，不会丢历史记录；删除才会连去重历史一起清掉。",
    actions:
      btn("全部启用", { act: "sub-enable-all", glyph: "check", sm: true, kind: "ghost" }) +
      btn("全部暂停", { act: "sub-disable-all", glyph: "close", sm: true, kind: "ghost" }) +
      iconBtn("trash", { act: "sub-clear", title: "清空这个会话的全部订阅", kind: "danger", xs: true }),
    body: items.length
      ? `<div class="list">${items.map(subRow).join("")}</div>`
      : emptyState("这个会话还没有订阅", "在右边填一个地址，或者只写番名让插件自己去 Mikan 找源。", "", "rss"),
    foot:
      badge("启用 " + num(state.subs.enabled || 0) + " / 共 " + num(state.subs.total || items.length)) +
      (limit ? badge("每会话上限 " + limit) : ""),
  });

  const addPanel = panel({
    eyebrow: "add",
    title: "新增订阅",
    desc: "名称是你自己认的标签，也是 /unsub 时要写的那个词。地址留空时插件会自动去 Mikan 找这部番的单番源。",
    body:
      `<div class="row">` +
      `<input type="text" style="width:170px" placeholder="名称（必填）" value="${attr(state.subDraft.name)}" data-live="sub-name" data-enter="sub-add" />` +
      `<input type="text" class="grow" placeholder="RSS 地址或简写（可留空）" value="${attr(state.subDraft.value)}" data-live="sub-value" data-enter="sub-add" />` +
      btn("添加", { act: "sub-add", glyph: "plus", kind: "primary", sm: true }) +
      btn("列字幕组", {
        act: "sub-sources",
        glyph: "search",
        sm: true,
        title: "按名称去 Mikan 查这部番有哪些字幕组，挑一个订，避免一集被推七八遍",
      }) +
      btn("先测一下", { act: "sub-test", glyph: "stethoscope", sm: true }) +
      `</div>` +
      `<div class="notes">` +
      SUB_HINTS.map(([label, sample]) => note(label + "：" + sample)).join("") +
      `</div>` +
      note("新加的订阅第一次抓取默认静默入库（不会把历史条目一次性刷出来），可在「配置 · RSS 订阅」里关掉。"),
    foot: subMessage ? `<pre class="output">${esc(subMessage)}</pre>` : "",
  });

  const backupPanel = panel({
    eyebrow: "backup",
    title: "备份与迁移",
    desc: "导出的 JSON 同时包含追番清单、订阅和会话偏好。导入是「合并」而不是「覆盖」，同名订阅会被更新。",
    body:
      `<div class="row">` +
      btn("导出当前会话", { act: "export-run", glyph: "download", sm: true, kind: "primary" }) +
      btn("导出全部会话", { act: "export-all", glyph: "download", sm: true }) +
      (exportText ? btn("复制", { act: "export-copy", glyph: "copy", sm: true, kind: "ghost" }) : "") +
      `</div>` +
      (exportText ? `<pre class="output">${esc(exportText)}</pre>` : "") +
      `<div class="field" style="margin-top:var(--gap)">` +
      `<span class="field-label">导入 JSON</span>` +
      `<textarea rows="5" placeholder="把导出的 JSON 粘到这里" data-live="import-text">${esc(state.importText)}</textarea>` +
      `<span class="field-hint">留空的话会导入到「当前会话」；JSON 里自带 umo 的记录以自带的为准。</span>` +
      `</div>` +
      btn("导入到当前会话", { act: "import-run", glyph: "upload", sm: true }),
  });

  // 这两块按需出现：没列过字幕组、或排除项预设拉不到时都返回空串。
  const pickPanel = sourcesPanel();
  const filterPanel = excludePanel();

  return (
    viewbar(
      "订阅",
      "共 " + num(state.subs.total || items.length) + " 条 · " + shortUmo(state.umo),
      sessionPicker() + btn("刷新", { act: "reload", glyph: "refresh", sm: true, kind: "ghost" }),
    ) +
    (pickPanel ? `<div class="deck wide" style="margin-bottom:var(--gap)">${pickPanel}</div>` : "") +
    `<div class="deck wide">${listPanel}${addPanel}${filterPanel}${backupPanel}</div>`
  );
};/* --- 视图：播报 ----------------------------------------------------------- */

// 以下几个模块级状态被前面的视图引用（订阅页的操作回执、导出文本、手动播报的星期、
// 选源候选、排除项草稿）。放在这里而不是 state 里，是因为它们都是「用完即弃」的
// 一次性中间态，不该跟着界面偏好一起被持久化到后端。
let subMessage = "";
let exportText = "";
let pushWeekday = 0;

// 选源候选表：点一次「列字幕组」拉一次，订完或换会话就丢。
// 同样不进 state —— 重新进面板时应该是干净的，而不是弹出上次的半截列表。
let subSources = { name: "", items: [] };

// 本会话排除项草稿。预设勾选与自定义词分开存（界面上一个是开关、一个是输入框），
// 「saved」 是落库那一刻的签名，用来判断有没有未保存的改动。
let excludeDraft = { presets: [], picked: [], custom: "", shared: [], dedup: true, prefer: [], window: 0, saved: "" };

const WEEKDAY_OPTIONS = [
  ["0", "按今天"],
  ["1", "周一"],
  ["2", "周二"],
  ["3", "周三"],
  ["4", "周四"],
  ["5", "周五"],
  ["6", "周六"],
  ["7", "周日"],
];

/** 会话 ID 列表统一渲染成 chips；空列表比起留白更需要一句解释。 */
function umoChips(rows, empty) {
  const list = (rows || []).filter(Boolean);
  if (!list.length) return note(empty);
  return (
    `<div class="chips">` +
    list.map((umo) => `<span class="chip mono" title="${attr(umo)}">${esc(shortUmo(umo))}</span>`).join("") +
    `</div>`
  );
}

RENDERERS.targets = () => {
  const data = state.targets;
  if (!data) return viewbar("播报", "正在读取播报目标…") + skeletonDeck(3);

  const draft = state.targetsDraft ?? (data.configured || []).join("\n");
  const dirty = draft !== (data.configured || []).join("\n");

  const configuredPanel = panel({
    eyebrow: "configured",
    title: "固定播报目标",
    desc: "每行一个会话 ID。这里写的是「无论如何都要播报」的目标，保存后会写回插件配置。",
    body:
      `<div class="field">` +
      `<textarea rows="6" placeholder="aiocqhttp:GroupMessage:123456789" data-live="targets-draft">${esc(draft)}</textarea>` +
      `<span class="field-hint">会话 ID 就是 unified_msg_origin，格式是「平台:消息类型:ID」。不想手打的话，用下面的按钮从已知会话里挑。</span>` +
      `</div>` +
      `<div class="row">` +
      btn("保存目标", { act: "targets-save", glyph: "save", kind: "primary", sm: true, disabled: !dirty }) +
      btn("放弃改动", { act: "targets-reset", glyph: "close", kind: "ghost", sm: true, disabled: !dirty }) +
      (dirty ? badge("有未保存的改动", "accent") : "") +
      `</div>`,
    foot: state.sessions.length
      ? `<div class="chips">` +
        state.sessions
          .map((row) =>
            btn(shortUmo(row.umo), {
              act: "targets-append",
              arg: row.umo,
              glyph: "plus",
              sm: true,
              kind: "ghost",
              title: row.umo,
            }),
          )
          .join("") +
        `</div>`
      : note("插件还没在任何聊天窗口里见过消息，所以暂时没有可挑的会话。"),
  });

  const effectivePanel = panel({
    eyebrow: "effective",
    title: "最终生效的目标",
    desc: "最终目标 = 配置里的固定目标 ∪ 自己在聊天里发过 /日历订阅 开 的会话。两边去重后取并集。",
    body:
      `<div class="metrics">` +
      metric("每日播报", data.push_enabled ? "已开启" : "已关闭", {
        tone: data.push_enabled ? "ok" : "warn",
        small: true,
        foot: (data.push_times || []).length ? "每天 " + (data.push_times || []).join(" / ") : "尚未设置时间",
      }) +
      metric("生效目标", num((data.effective || []).length), {
        glyph: "bell",
        foot: "固定 " + num((data.configured || []).length) + " / 自主订阅 " + num((data.opted_in || []).length),
      }) +
      `</div>` +
      `<div class="field"><span class="field-label">固定目标（解析后）</span>` +
      umoChips(data.resolved, "配置里还没有写死任何目标。") +
      `</div>` +
      `<div class="field"><span class="field-label">自主订阅的会话</span>` +
      umoChips(data.opted_in, "还没有会话在聊天里开过每日播报。") +
      `</div>` +
      kv([["默认平台 ID", data.default_platform_id || "（未设置）"]]) +
      (!data.default_platform_id && (data.configured || []).some((row) => !String(row).includes(":"))
        ? note("配置里有不带平台前缀的目标，但「默认平台 ID」是空的，这些目标会被丢掉。请补一个平台 ID，或把目标写成完整的会话 ID。", "danger")
        : ""),
  });

  const manualPanel = panel({
    eyebrow: "manual",
    title: "手动播报",
    desc: "不想等到整点就想看效果时用这个。默认按今天的星期取新番，也可以指定某一天。",
    body:
      `<div class="row">` +
      `<span class="field-hint">播报哪一天</span>` +
      selectHtml("weekday", WEEKDAY_OPTIONS, String(pushWeekday), { act: "push-weekday" }) +
      btn("立即播报", { act: "push-now-custom", glyph: "play", kind: "primary", sm: true }) +
      btn("立即抓取 RSS", { act: "poll-now", glyph: "rss", sm: true }) +
      `</div>` +
      note("这两个按钮都会真的往上面「生效目标」里的每个会话发消息，不是预演。想先看长相请去「卡片」页。", "warn"),
  });

  return (
    viewbar(
      "播报",
      num((data.effective || []).length) + " 个会话会收到每日新番",
      btn("刷新", { act: "reload", glyph: "refresh", sm: true, kind: "ghost" }),
    ) +
    `<div class="deck wide">${configuredPanel}${effectivePanel}${manualPanel}</div>`
  );
};
/* --- 视图：ani-rss 同步 --------------------------------------------------- */

// 鉴权方式的中文说法必须和服务层的 AUTH_LABEL 一致：同一件事在指令回执里叫
// 「API Key」、在面板里换个叫法，用户会以为是两套互不相干的配置。
const ANIRSS_AUTH_LABEL = { api_key: "API Key", password: "账号密码", none: "未设置" };

// isoweekday → 中文。ani-rss 认不出播出日时给 0，这里就查不到，界面上直接不显示。
const WEEKDAY_LABEL = Object.fromEntries(WEEKDAY_OPTIONS.slice(1));

// 同步范围的四个开关：[配置键, 状态字段, 标题, 解释]。
// 配置键和状态字段名字不一样（一个是 anirss_enabled、一个是 enabled），
// 放同一张表里渲染与保存共用，分两处写迟早会漏改一处。
const ANIRSS_FLAGS = [
  ["anirss_enabled", "enabled", "定时同步", "关掉之后这一页的「立即同步」照样能点，只是不再自动跑。"],
  ["anirss_sync_watchlist", "sync_watchlist", "写进追番表", "ani-rss 的一条订阅对应追番表里的一行，下载到第几集就回填成看到第几集。"],
  ["anirss_sync_subscriptions", "sync_subscriptions", "顺带建 RSS 订阅", "默认关：ani-rss 已经在下载了，再让插件推同一集多半只是刷屏。"],
  ["anirss_notify_on_change", "notify_on_change", "有变化才播报", "只在真的新增 / 更新 / 建订阅时发账目卡，两边没差异就不打扰。"],
];

// 用户在自己电脑上跑这一条就能拿到导出。端口是 ani-rss 的默认值，
// 没设 API Key 的话把 「-H」 那一段去掉即可。
const ANIRSS_EXPORT_CMD =
  'curl -s -X POST "http://127.0.0.1:7789/api/listAni" -H "api-key: 你的APIKey" -o ani.json';

// ani-rss 的 WebHook 只能把占位符拼进 body，所以这里给一份「刚够用」的模板：
// 事件名 / 番名 / 季集 / 封面 / bgm 链接 / 字幕组 / 评分，最后带上 ani-rss 自己拼好的整段文本。
// ⚠ 「${message}」 外面不能加引号 —— 它已经是转义好的 JSON 片段，再包一层引号整个 body 就坏了；
//   其余占位符反而必须加引号，因为 「${season}」「${episode}」 展开出来是裸数字。
const WEBHOOK_BODY_TPL =
  '{"event":"${action}","title":"${title}","season":"${season}","episode":"${episode}",' +
  '"poster_url":"${image}","url":"${bgmUrl}","subgroup":"${subgroup}","score":"${score}",' +
  '"message":${message}}';

// 请求头一行一条。ani-rss 是按第一个冒号切开的，所以令牌值里绝不能再出现冒号。
const WEBHOOK_HEADER_TPL = "Content-Type: application/json\nX-Webhook-Token: 你设的 webhook_token";

// ani-rss 默认只勾了「开始下载 / 缺少集数 / 发生错误」，「下载完成」是没勾的。
// 不勾它，进度回填这条链路永远不会被触发 —— 最容易漏的一步，值得在界面上写死。
const WEBHOOK_STATUS_HINT =
  "ani-rss 的「通知状态」里务必勾上「下载完成」：它默认没勾，不勾进度回填永远不触发。";

/** 当前配置能拼出哪些可用地址。域名只有用户自己知道，所以给的是带占位符的写法。 */
function webhookEndpoints(webhook) {
  const path = "/" + String(webhook.route || "bangumi_nexus/notify");
  const rows = [["反代域名（推荐）", "https://你的域名" + path]];
  const port = Number(webhook.port) || 0;
  if (port) rows.push(["独立端口直连", "http://AstrBot主机:" + port + path]);
  return rows;
}

/** ani-rss 里的一条订阅。「已认领」= 本地追番表里已经有它对应的那一行。 */
function anirssRow(item, claimed) {
  const progress =
    Number(item.total) > 0
      ? num(item.progress) + " / " + num(item.total) + " 集"
      : num(item.progress) + " 集";
  const facts = [
    item.subgroup ? "字幕组 " + item.subgroup : "",
    item.subject_id ? "bgm " + item.subject_id : "",
    WEEKDAY_LABEL[String(item.weekday || "")] || "",
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    `<div class="list-row${item.enabled ? "" : " is-off"}">` +
    `<div class="list-main">` +
    `<span class="list-title"><span class="text">${esc(item.title)}</span>` +
    (claimed ? badge("已认领", "ok") : badge("待同步", "accent")) +
    (item.enabled ? "" : badge("已停用", "warn")) +
    (item.completed ? badge("已完结") : "") +
    `</span>` +
    (item.summary ? `<span class="list-sub">${esc(item.summary)}</span>` : "") +
    (facts ? `<span class="list-sub">${esc(facts)}</span>` : "") +
    `</div>` +
    `<div class="list-actions">` +
    badge(progress) +
    (item.url
      ? `<a class="icon-btn xs" href="${attr(item.url)}" target="_blank" rel="noopener noreferrer" title="打开这条订阅的 RSS 地址">${icon("link", "sm")}</a>`
      : "") +
    `</div></div>`
  );
}

RENDERERS.anirss = () => {
  const data = state.anirss;
  if (!data) return viewbar("同步", "正在连接本地 ani-rss…") + skeletonDeck(3);

  const items = Array.isArray(data.items) ? data.items : [];
  const claimed = new Set((data.links || []).map((row) => String(row.ani_id)));
  const baseline = (data.targets || []).join("\n");
  const draft = state.anirssDraft ?? baseline;
  const dirty = draft !== baseline;
  const writable = data.writable !== false;
  const auth = ANIRSS_AUTH_LABEL[String(data.auth || "none")] || "未设置";
  const last = data.last_result || {};
  // 离线导入与在线同步共用这块账目，标题得跟着来路走，否则用户会以为「同步」偷偷成功过。
  const lastOrigin = String(last.origin || "同步");

  const connectionPanel = panel({
    eyebrow: "connection",
    title: "本地 ani-rss",
    desc: "同步是单向的：插件只读 ani-rss，永远不回写。下载器里的配置是你的资产，同步没资格改它。",
    actions:
      btn("测试连接", { act: "anirss-test", glyph: "stethoscope", sm: true }) +
      btn("立即同步", {
        act: "anirss-sync",
        glyph: "sync",
        kind: "primary",
        sm: true,
        disabled: !data.configured,
      }),
    body:
      `<div class="metrics">` +
      metric("定时同步", data.enabled ? "已开启" : "已关闭", {
        tone: data.enabled ? "ok" : "warn",
        small: true,
        foot: Number(data.interval) > 0 ? "每 " + num(data.interval) + " 分钟一次" : "间隔为 0，只在手点时同步",
      }) +
      metric("连接", data.ok ? "正常" : data.configured ? "连不上" : "未配置", {
        tone: data.ok ? "ok" : data.configured ? "err" : "warn",
        small: true,
        glyph: "link",
        foot: "鉴权：" + auth + (data.token_cached ? "（票据已缓存）" : ""),
      }) +
      metric("对方条目", num(data.total || items.length), {
        glyph: "library",
        foot: "启用中 " + num(data.active || 0),
      }) +
      metric("已认领", num(data.synced || 0), {
        glyph: "heart",
        foot: data.last_at ? "上次" + lastOrigin + " " + relative(data.last_at) : "还没同步过",
      }) +
      `</div>` +
      kv([
        ["地址", data.base || "（未设置）"],
        ["校验 HTTPS 证书", data.verify_tls === false ? "已关闭（只在自签证书时这么设）" : "开启"],
        ["同步方向", "ani-rss → 番剧中枢（只读）"],
      ]) +
      (data.configured
        ? ""
        : note(
            "还没填 ani-rss 地址。去「配置 → ani-rss 同步」填 anirss_base，再配 API Key 或账号密码。" +
              "连不上（例如 ani-rss 在自己电脑、AstrBot 在公网服务器）就用下面的「离线导入」，效果一样。",
            "warn",
          )) +
      (data.configured && data.error ? note(String(data.error), "danger") : ""),
  });

  const targetsPanel = panel({
    eyebrow: "targets",
    title: "同步到哪些会话",
    desc: "每行一个会话 ID。这一条是独立的推送链：只发同步账目，不会混进 RSS 更新那条链里。",
    body:
      `<div class="field">` +
      `<textarea rows="5" placeholder="aiocqhttp:FriendMessage:10000" data-live="anirss-targets-draft">${esc(draft)}</textarea>` +
      `<span class="field-hint">这里同时决定「写进谁的追番表」：ani-rss 是一台机器上的下载器，进度理应只落到你自己的会话。留空则同步不会执行。</span>` +
      `</div>` +
      `<div class="row">` +
      btn("保存目标", {
        act: "anirss-targets-save",
        glyph: "save",
        kind: "primary",
        sm: true,
        disabled: !dirty || !writable,
      }) +
      btn("放弃改动", { act: "anirss-targets-reset", glyph: "close", kind: "ghost", sm: true, disabled: !dirty }) +
      (dirty ? badge("有未保存的改动", "accent") : "") +
      `</div>`,
    foot: state.sessions.length
      ? `<div class="chips">` +
        state.sessions
          .map((row) =>
            btn(shortUmo(row.umo), {
              act: "anirss-targets-append",
              arg: row.umo,
              glyph: "plus",
              sm: true,
              kind: "ghost",
              title: row.umo,
            }),
          )
          .join("") +
        `</div>`
      : note("插件还没在任何聊天窗口里见过消息，所以暂时没有可挑的会话。"),
  });

  const importDraft = state.anirssImportDraft || "";
  const importPanel = panel({
    eyebrow: "offline",
    title: "离线导入",
    desc:
      "连不上 ani-rss 时走这条：把导出的 JSON 搬过来，结果和在线同步一模一样。" +
      "不用开端口、不用内网穿透，也不用在插件里填任何凭据。",
    actions: btn("复制导出命令", {
      act: "anirss-import-copy",
      glyph: "copy",
      kind: "ghost",
      sm: true,
    }),
    body:
      note("在跑着 ani-rss 的那台电脑上执行，然后把 ani.json 的内容整份粘到下面。") +
      `<pre class="output">${esc(ANIRSS_EXPORT_CMD)}</pre>` +
      `<div class="field" style="margin-top:var(--gap)">` +
      `<span class="field-label">listAni 的响应</span>` +
      `<textarea rows="6" placeholder='{"code":200,"data":{"weekList":[…]}}' data-live="anirss-import-draft">${esc(importDraft)}</textarea>` +
      `<span class="field-hint">带 code / data 的整份包封和里层的 data 两种都认。落到哪些会话仍看上面那份已保存的名单。</span>` +
      `</div>` +
      `<div class="row">` +
      btn("导入这份数据", { act: "anirss-import", glyph: "upload", kind: "primary", sm: true }) +
      btn("清空", { act: "anirss-import-clear", glyph: "close", kind: "ghost", sm: true }) +
      `</div>`,
    foot: note(
      "导入是「合并」而不是「覆盖」：本地没有的补上、进度只往前推，本地多出来的条目一律保留。",
    ),
  });

  // Webhook 反推：让 ani-rss 主动把「刚下完哪一集」推过来。
  // 放在这一页而不是单独开一屏，因为需要它的人正是「服务器连不上家里 ani-rss」的那批人 ——
  // 他们会先走到这一页，再发现在线同步走不通。
  const webhook = state.overview?.webhook || {};
  const webhookPort = Number(webhook.port) || 0;
  const webhookReady = webhook.enabled === true && webhook.token_set === true && webhookPort > 0;
  const silentKinds = Array.isArray(webhook.silent_kinds) ? webhook.silent_kinds : [];
  const webhookPanel = panel({
    eyebrow: "webhook",
    title: "Webhook 反推",
    desc:
      "让 ani-rss 每下完一集主动推一条过来：群里当场收到卡片、追番进度立刻往前推，" +
      "既不用等下一次同步，也不用让服务器连回你家。",
    actions:
      btn("复制请求头", { act: "anirss-webhook-copy", arg: "header", glyph: "copy", kind: "ghost", sm: true }) +
      btn("复制 Body", { act: "anirss-webhook-copy", arg: "body", glyph: "copy", kind: "ghost", sm: true }),
    body:
      `<div class="chips">` +
      badge("接收开关 " + (webhook.enabled ? "已开" : "未开"), webhook.enabled ? "ok" : "warn") +
      badge("令牌 " + (webhook.token_set ? "已设置" : "未设置"), webhook.token_set ? "ok" : "warn") +
      badge("独立端口 " + (webhookPort || "未开"), webhookPort ? "ok" : "warn") +
      badge("进度回填 " + (webhook.auto_progress ? "已开" : "未开"), webhook.auto_progress ? "ok" : "") +
      `</div>` +
      (webhookReady
        ? ""
        : note(
            "接收开关 / 令牌 / 独立端口 三项齐了才能被外部直连，去「配置 · Webhook 接入」里补齐。",
            "warn",
          )) +
      `<div class="field" style="margin-top:var(--gap)">` +
      `<span class="field-label">WebHook 地址</span>` +
      kv(webhookEndpoints(webhook)) +
      `<span class="field-hint">公网推送请套一层 HTTPS 反代，并把 webhook_bind 改成 127.0.0.1 —— 令牌走明文很容易被路上的人捡走。</span>` +
      `</div>` +
      `<div class="field"><span class="field-label">请求头（一行一条）</span>` +
      `<pre class="output">${esc(WEBHOOK_HEADER_TPL)}</pre></div>` +
      `<div class="field"><span class="field-label">消息内容（Body）</span>` +
      `<pre class="output">${esc(WEBHOOK_BODY_TPL)}</pre></div>` +
      note(WEBHOOK_STATUS_HINT, "warn") +
      (silentKinds.length
        ? note("已静默：" + silentKinds.join("、") + " —— 这些事件只回填进度、不发卡片。")
        : note(
            "两个状态都勾会一集来两条。想都收但只看一张卡片，把不想看的那个填进 webhook_silent_kinds。",
          )),
    foot:
      badge("收到 " + num(webhook.received || 0)) +
      badge("已投递 " + num(webhook.delivered || 0)) +
      badge("静默 " + num(webhook.silenced || 0)) +
      badge("拒绝 " + num(webhook.rejected || 0)) +
      btn("发一条测试", { act: "webhook-test", glyph: "wand", kind: "ghost", sm: true }),
  });

  const scopePanel = panel({
    eyebrow: "scope",
    title: "同步范围",
    desc: "这几项直接写插件配置，改完立刻生效，不用再去配置页点保存。",
    body:
      `<div class="chips">` +
      ANIRSS_FLAGS.map(([key, field, label]) =>
        switchHtml(label, data[field] === true, { act: "anirss-flag", arg: key, disabled: !writable }),
      ).join("") +
      `</div>` +
      `<div class="row" style="margin-top:var(--gap)">` +
      `<span class="field-hint">自动同步间隔（分钟）</span>` +
      `<input type="number" min="0" max="1440" step="5" style="width:96px" value="${attr(Number(data.interval) || 0)}" data-act="anirss-interval" title="0 表示不自动同步，只在这里手点" ${writable ? "" : "disabled"}/>` +
      `</div>` +
      `<div class="notes" style="margin-top:var(--gap)">` +
      ANIRSS_FLAGS.map(([, , label, hint]) => note(label + "：" + hint)).join("") +
      `</div>` +
      (writable
        ? ""
        : note("当前运行环境不允许插件写配置文件，这一页的开关只能看不能改 —— 请去 AstrBot 自带的插件配置页修改。", "danger")),
  });

  const itemsPanel = panel({
    eyebrow: "entries",
    title: "ani-rss 里的订阅",
    desc: "同步只做两件事：本地没有的补上、进度往前推。ani-rss 里删掉的条目这边只报「已失联」，绝不自动删。",
    body: items.length
      ? `<div class="list">${items.map((row) => anirssRow(row, claimed.has(String(row.ani_id)))).join("")}</div>`
      : emptyState(
          data.ok ? "ani-rss 里还没有订阅" : "读不到 ani-rss 的订阅列表",
          data.ok
            ? "先在本地 ani-rss 里订几部番，再回来点「立即同步」。"
            : "按上面的提示把地址和凭据配好，然后点「测试连接」确认能通。",
          "",
          "sync",
        ),
    foot: items.length ? badge("共 " + num(items.length) + " 条 · 已认领 " + num(claimed.size)) : "",
  });

  const buckets = [
    ["新增追番", last.added],
    ["进度更新", last.updated],
    ["新建订阅", last.subscribed],
    ["已失联", last.orphans],
    ["失败", last.failures],
  ].filter(([, rows]) => Array.isArray(rows) && rows.length);

  const reportPanel = panel({
    eyebrow: "report",
    title: "上次" + lastOrigin + "的账目",
    desc: "「已失联」是 ani-rss 里已经没有、本地却还留着的条目。删不删由你决定，插件不替你做不可逆的事。",
    body: buckets.length
      ? buckets
          .map(
            ([label, rows]) =>
              `<div class="field" style="margin-top:var(--gap)"><span class="field-label">${esc(label)}${badge(String(rows.length))}</span>` +
              `<div class="chips">${rows.map((row) => chip(String(row))).join("")}</div></div>`,
          )
          .join("")
      : emptyState(
          data.last_at ? "上次" + lastOrigin + "没有任何变化" : "还没同步过，也没导入过",
          data.last_at
            ? "两边已经对齐了，这是正常状态。"
            : "点一次「立即同步」或用「离线导入」，这里会列出到底动了哪些条目。",
          "",
          "check",
        ),
    foot: data.last_at
      ? badge(lastOrigin + "于 " + clock(data.last_at)) +
        badge("读到 " + num(last.active || 0) + " 条启用中") +
        badge("落到 " + num((last.sessions || []).length) + " 个会话")
      : "",
  });

  return (
    viewbar(
      "同步",
      data.configured
        ? data.ok
          ? num(items.length) + " 条 ani-rss 订阅 · 已认领 " + num(data.synced || 0)
          : "连不上本地 ani-rss"
        : Number(data.synced) > 0
          ? "离线导入 · 已认领 " + num(data.synced) + " 条"
          : "还没配置 ani-rss",
      btn("刷新", { act: "reload", glyph: "refresh", sm: true, kind: "ghost" }),
    ) +
    `<div class="deck wide">${connectionPanel}${targetsPanel}${importPanel}${webhookPanel}${scopePanel}${itemsPanel}${reportPanel}</div>`
  );
};
/* --- 视图：卡片预览 ------------------------------------------------------- */

// 预览缓存的键要把「主题 × 卡片类型 × 渲染器」三者都算进去，
// 否则换了类型还会拿到上一次的图。
const shotKey = (themeKey) => themeKey + "|" + state.cards.kind + "|" + (state.cards.renderer || "auto");

async function fetchShot(themeKey) {
  const key = shotKey(themeKey);
  if (state.cards.shots.has(key) || state.cards.busy.has(key)) return;
  state.cards.busy.add(key);
  if (state.view === "cards") render("cards");
  try {
    const payload = await apiGet("card", {
      theme: themeKey,
      kind: state.cards.kind,
      renderer: state.cards.renderer || "",
    });
    state.cards.shots.set(key, payload);
  } catch (error) {
    state.cards.shots.set(key, { error: errText(error) });
  } finally {
    state.cards.busy.delete(key);
    if (state.view === "cards") render("cards");
  }
}

/**
 * 串行渲染。
 *
 * 每张卡片在后端都要开一次无头浏览器截图（或退化到 Pillow 绘制），
 * 并发只会让它们互相抢 CPU、一起变慢甚至超时，所以这里刻意一张一张来。
 */
async function fetchShots(keys) {
  for (const key of keys) {
    await fetchShot(key);
  }
}

// 覆盖 chunk3 里的占位实现：进页面只预渲染「当前主题」那一张，
// 剩下五张等用户点「全部渲染」或单独点某一张时再来。
LOADERS.cards = async () => {
  if (!state.overview) await loadOverview();
  void fetchShots([state.theme]);
};

function cardTile(theme) {
  const key = shotKey(theme.key);
  const shot = state.cards.shots.get(key);
  const busy = state.cards.busy.has(key);
  const label = KIND_LABEL[state.cards.kind] || state.cards.kind;

  let stage = "";
  if (shot && shot.data_uri) {
    stage =
      `<img src="${attr(shot.data_uri)}" alt="${attr(theme.name + " 主题的" + label)}" />` +
      `<button class="icon-btn xs card-tile-zoom" type="button" data-act="card-zoom" data-arg="${attr(theme.key)}" title="放大看原图" aria-label="放大看原图">${icon("fit", "sm")}</button>`;
  } else if (shot && shot.error) {
    stage = `<div class="empty"><strong>渲染失败</strong><span>${esc(shot.error)}</span></div>`;
  } else if (!busy) {
    stage =
      `<div class="empty"><span class="empty-glyph">${icon("cards", "xl")}</span>` +
      btn("渲染这张", { act: "card-redraw", arg: theme.key, glyph: "play", sm: true }) +
      `</div>`;
  }
  if (busy) stage += `<div class="busy-veil"><span class="spinner"></span></div>`;

  const foot = shot && shot.data_uri ? bytes(shot.bytes) : theme.tagline;

  return (
    `<article class="card-tile">` +
    `<div class="card-tile-shot">${stage}</div>` +
    `<div class="card-tile-foot">` +
    `<strong>${esc(theme.name)}</strong><small>${esc(foot)}</small>` +
    `<span class="row tight" style="margin-inline-start:auto">` +
    iconBtn("refresh", { act: "card-redraw", arg: theme.key, title: "重新渲染", xs: true }) +
    iconBtn("download", { act: "card-download", arg: theme.key, title: "下载 PNG", xs: true }) +
    iconBtn("palette", { act: "card-apply", arg: theme.key, title: "把这个主题设为当前预览主题", xs: true }) +
    `</span>` +
    `</div>` +
    `</article>`
  );
}

RENDERERS.cards = () => {
  const themes = state.themes || [];
  const kinds = (state.meta?.options?.preview_kinds || Object.keys(KIND_LABEL)).map((key) => [
    key,
    KIND_LABEL[key] || key,
  ]);
  const renderers = [["", "跟随配置"]].concat(
    (state.meta?.options?.renderers || []).map((key) => [key, RENDERER_LABEL[key] || key]),
  );
  const cooling = state.overview?.render?.html_cooling_down;

  const controls = panel({
    eyebrow: "preview",
    title: "卡片预览",
    desc: "这里渲染的是真卡片：走的是插件实际用的那条渲染链，看到什么样，聊天里就是什么样（数据是示例数据）。",
    body:
      `<div class="row">` +
      segmented(kinds, state.cards.kind, "card-kind") +
      `<span class="field-hint">渲染方式</span>` +
      selectHtml("renderer", renderers, state.cards.renderer, { act: "card-renderer" }) +
      btn("渲染全部主题", { act: "cards-render-all", glyph: "play", kind: "primary", sm: true }) +
      btn("清空预览缓存", { act: "cards-clear", glyph: "trash", sm: true, kind: "ghost" }) +
      `</div>` +
      note("渲染一张要开一次无头浏览器，六张会串行跑，慢是正常的。「跟随配置」表示用「配置 · 卡片与渲染」里的偏好。") +
      (cooling
        ? note("HTML 渲染正在冷却（连续失败 3 次会暂停两分钟），现在渲出来的可能是 Pillow 版本。", "warn")
        : ""),
  });

  return (
    viewbar(
      "卡片",
      "六套主题 × 四种卡片，挑一套顺眼的写进配置",
      btn("刷新", { act: "reload", glyph: "refresh", sm: true, kind: "ghost" }),
    ) +
    `<div class="deck wide" style="margin-bottom:var(--gap)">${controls}</div>` +
    `<div class="card-gallery">${themes.map(cardTile).join("")}</div>`
  );
};/* --- 视图：数据源 --------------------------------------------------------- */

async function runProbes() {
  state.probes = await apiGet("diagnose");
}

// 体检要真的去访问八个站点，进页面就自动跑一次即可，之后交给按钮。
LOADERS.sources = async () => {
  if (!state.overview) await loadOverview();
  if (!state.probes) {
    try {
      await runProbes();
    } catch (error) {
      state.probes = { probes: [], healthy: 0, total: 0, error: errText(error) };
    }
  }
};

const SOURCE_GLYPH = {
  bangumi: "tv",
  bangumi_data: "library",
  anime1: "play",
  yuc: "calendar",
  age: "cards",
  moegirl: "search",
  mikan: "rss",
  rsshub: "link",
};

function sourceRow(source) {
  return (
    `<div class="list-row">` +
    `<span class="thumb" style="display:flex;align-items:center;justify-content:center;color:var(--accent)">${icon(SOURCE_GLYPH[source.key] || "source", "lg")}</span>` +
    `<div class="list-main">` +
    `<span class="list-title"><span class="text">${esc(source.name)}</span>` +
    (source.license ? badge(source.license) : "") +
    `</span>` +
    `<span class="list-sub">${esc(source.role)}</span>` +
    `<span class="list-sub mono">${esc(source.home)}</span>` +
    `</div>` +
    `<div class="list-actions">` +
    `<a class="icon-btn xs" href="${attr(source.home)}" target="_blank" rel="noopener noreferrer" title="打开官网">${icon("link", "sm")}</a>` +
    `</div></div>`
  );
}

function probeRow(row) {
  return (
    `<div class="list-row">` +
    `<div class="list-main">` +
    `<span class="list-title"><span class="text">${esc(row.name)}</span>` +
    badge(row.ok ? "正常" : "异常", row.ok ? "ok" : "err") +
    `</span>` +
    `<span class="list-sub">${esc(row.detail || "（无附加信息）")}</span>` +
    `</div>` +
    `<div class="list-actions"><span class="chip mono">${esc(Number(row.elapsed || 0).toFixed(2))}s</span></div>` +
    `</div>`
  );
}

RENDERERS.sources = () => {
  const meta = state.meta;
  if (!meta) return viewbar("数据源", "正在读取数据源清单…") + skeletonDeck(3);

  const cache = state.overview?.sources || {};
  const probes = state.probes;
  const rows = probes?.probes || [];

  const healthPanel = panel({
    eyebrow: "health",
    title: "健康体检",
    desc: "并发探测每个上游站点，跟聊天里的 /番剧诊断 是同一套探针。单项超时 12 秒。",
    actions:
      btn("重新体检", { act: "probe-run", glyph: "stethoscope", kind: "primary", sm: true }) +
      (probes && probes.total
        ? badge(probes.healthy + " / " + probes.total + " 正常", probes.healthy === probes.total ? "ok" : "warn")
        : ""),
    body: probes?.error
      ? note(probes.error, "danger")
      : rows.length
        ? `<div class="list">${rows.map(probeRow).join("")}</div>`
        : `<div class="list">${[1, 2, 3, 4].map(() => `<div class="list-row"><div class="list-main"><div class="skeleton line" style="width:40%"></div><div class="skeleton line" style="width:70%"></div></div></div>`).join("")}</div>`,
    foot: note("某个源短暂异常不影响其它功能——跨源匹配是「能补就补」，缺一个源只是卡片上少一段信息。"),
  });

  const cachePanel = panel({
    eyebrow: "cache",
    title: "本地缓存",
    desc: "这些索引会按配置的周期自动刷新，也可以在这里手动催一次。",
    body:
      `<div class="metrics">` +
      metric("bangumi-data 条目", num(cache.bangumi_data?.items), {
        small: true,
        foot: num(cache.bangumi_data?.months) + " 个月份 / " + num(cache.bangumi_data?.aliases) + " 条别名",
      }) +
      metric("anime1 索引", num(cache.anime1?.entries), {
        small: true,
        foot: cache.anime1?.fetched_at ? "更新于 " + relative(cache.anime1.fetched_at) : "尚未抓取",
      }) +
      metric("長門番堂季度", num(cache.yuc?.seasons), {
        small: true,
        foot: "共 " + num(cache.yuc?.entries) + " 部番",
      }) +
      metric("AGE 推荐位", num(cache.age?.items), { small: true }) +
      `</div>` +
      `<div class="row">` +
      btn("刷新 anime1 索引", { act: "refresh-anime1", glyph: "refresh", sm: true }) +
      btn("刷新概览", { act: "reload", glyph: "refresh", sm: true, kind: "ghost" }) +
      `</div>`,
  });

  const listPanel = panel({
    eyebrow: "upstream",
    title: "八个数据源分别管什么",
    desc: "bangumi-data 是整套跨源匹配的枢纽：它提供各站点 ID 与多语言标题的对照，其它源都靠它对齐。",
    body: `<div class="list">${(meta.sources || []).map(sourceRow).join("")}</div>`,
  });

  return (
    viewbar(
      "数据源",
      (meta.sources || []).length + " 个上游站点",
      btn("重新体检", { act: "probe-run", glyph: "stethoscope", sm: true, kind: "ghost" }),
    ) +
    `<div class="deck wide">${healthPanel}${cachePanel}${listPanel}</div>`
  );
};
/* --- 视图：指令表 --------------------------------------------------------- */

function commandRow(command) {
  const hay = [command.name, command.usage, command.summary, (command.aliases || []).join(" ")]
    .join(" ")
    .toLowerCase();
  return (
    `<tr data-hay="${attr(hay)}">` +
    `<td class="c-name">${esc(command.name)}${command.admin ? " " + badge("管理员", "warn") : ""}</td>` +
    `<td class="c-usage">${esc(command.usage)}</td>` +
    `<td class="c-detail">${esc(command.summary)}</td>` +
    `<td><div class="c-alias">${(command.aliases || []).map((alias) => chip(alias, "mono")).join("")}</div></td>` +
    `</tr>`
  );
}

RENDERERS.commands = () => {
  const meta = state.meta;
  if (!meta) return viewbar("指令", "正在读取指令表…") + skeletonDeck(2);

  const categories = meta.catalog || [];
  const body = categories
    .map(
      (category) =>
        `<tr class="cat-head" data-cat="${attr(category.key)}">` +
        `<td colspan="4"><span class="cat-icon">${esc(category.icon)}</span> ${esc(category.title)}` +
        `<span class="cat-blurb">${esc(category.blurb)}</span></td></tr>` +
        (category.commands || [])
          .map((command) => commandRow(command).replace("<tr ", `<tr data-cat="${attr(category.key)}" `))
          .join(""),
    )
    .join("");

  const table = panel({
    eyebrow: "commands",
    title: "全部聊天指令",
    desc: "指令前缀取自 AstrBot 的唤醒前缀设置，所以下面的用法是可以直接复制到聊天框的。",
    actions:
      `<input type="text" style="width:220px" placeholder="过滤：指令名 / 说明 / 别名" value="${attr(state.cmdFilter)}" data-live="cmd-filter" />` +
      badge(meta.counts.commands + " 条 · " + meta.counts.aliases + " 别名", "accent"),
    body:
      `<table class="cmd"><thead><tr>` +
      `<th style="width:16%">指令</th><th style="width:26%">用法</th><th>说明</th><th style="width:16%">别名</th>` +
      `</tr></thead><tbody>${body}</tbody></table>`,
    cls: "fill",
  });

  return (
    viewbar(
      "指令",
      meta.counts.categories + " 个分类 · 前缀「" + meta.prefix + "」",
      btn("刷新", { act: "reload", glyph: "refresh", sm: true, kind: "ghost" }),
    ) + table
  );
};

/**
 * 指令表过滤刻意不走 render()。
 *
 * 输入框每敲一个字都重渲染会丢焦点、还会把光标弹到末尾，
 * 所以这里直接改 DOM 的 hidden：分类头按该组是否还有可见行决定显隐。
 */
function filterCommands(text) {
  const needle = String(text || "").trim().toLowerCase();
  const table = $(".view[data-view='commands'] table.cmd");
  if (!table) return;
  const visible = new Set();
  $$("tbody tr[data-hay]", table).forEach((row) => {
    const hit = !needle || (row.dataset.hay || "").includes(needle);
    row.hidden = !hit;
    if (hit && row.dataset.cat) visible.add(row.dataset.cat);
  });
  $$("tbody tr.cat-head", table).forEach((row) => {
    row.hidden = !!needle && !visible.has(row.dataset.cat || "");
  });
}
/* --- 视图：关于 ----------------------------------------------------------- */

const UPSTREAMS = [
  ["astrbot_plugin_bangumi_calendar", "NoFizz", "每日放送卡片、封面内联与推送骨架"],
  ["astrbot_plugin_bangumi", "united-pooh", "条目搜索、简介翻译、长回复转图与追番指令"],
  ["astrbot_plugin_autobangumi_notify", "Yometenma", "AutoBangumi Webhook 事件与目标解析"],
  ["astrbot_plugin_anime1_list", "zhist2028", "anime1.me 在线观看索引与 LLM 工具"],
  ["astrbot_plugin_rsshub", "FlanChanXwO", "RSS 订阅的指令语义与 RSSHub 路由简写"],
  ["astrbot_plugin_anime_gacha", "xco2", "抽番、季度新番表与萌娘百科检索"],
];

RENDERERS.about = () => {
  const meta = state.meta;
  if (!meta) return viewbar("关于", "正在读取版本信息…") + skeletonDeck(2);

  const limits = meta.limits || {};
  const counts = meta.counts || {};

  const idPanel = panel({
    eyebrow: "identity",
    title: meta.display_name + " · " + meta.brand,
    desc: "把六个上游番剧插件与四个数据源整合成一套：查番、追番、订阅、播报、抽番共享同一份跨源索引。",
    body:
      `<div class="metrics">` +
      metric("版本", "v" + meta.version, { small: true, glyph: "info" }) +
      metric("聊天指令", num(counts.commands), { foot: num(counts.categories) + " 个分类 / " + num(counts.aliases) + " 个别名" }) +
      metric("卡片主题", num((meta.themes || []).length), { foot: "每套主题都有四种卡片版式" }) +
      metric("数据源", num((meta.sources || []).length), { glyph: "source" }) +
      `</div>` +
      `<div class="row">` +
      `<a class="btn sm" href="${attr(meta.repo)}" target="_blank" rel="noopener noreferrer">${icon("link", "sm")}<span>GitHub 仓库</span></a>` +
      `<a class="btn sm ghost" href="${attr(meta.repo)}/issues" target="_blank" rel="noopener noreferrer">${icon("info", "sm")}<span>反馈问题</span></a>` +
      `</div>`,
  });

  const safetyPanel = panel({
    eyebrow: "security",
    title: "安全须知",
    desc: "这一段值得认真读一遍，尤其是打算让外部程序回调本插件的时候。",
    body:
      note("不要把 AstrBot 面板直接暴露到公网。本页所有接口都挂在面板的鉴权后面，面板一旦裸奔，配置和会话 ID 也就跟着裸奔了。", "danger") +
      note("要用独立监听端口接 AutoBangumi 回调，必须先填 webhook_token。令牌为空时插件会拒绝启动监听，而不是「先跑起来再说」。", "danger") +
      note("Bangumi Access Token 只用于提高接口配额，保存后面板不再回显；留空提交表示「不修改」，不会把它清空。", "warn") +
      note("「立即播报」「立即抓取 RSS」「Webhook 测试」都会真的发消息到生效目标，别在生产群里随手点。", "warn"),
  });

  const limitPanel = panel({
    eyebrow: "limits",
    title: "内置限额",
    desc: "这些上限是为了防止单个会话把数据库和消息队列拖垮，写死在代码里、不走配置。",
    body: kv([
      ["每会话订阅上限", num(limits.subscriptions_per_session) + " 条"],
      ["每会话追番上限", num(limits.watchlist_per_session) + " 条"],
      ["界面偏好体积上限", bytes(limits.state_bytes)],
      ["活动日志保留", num(limits.logs) + " 条（环形覆盖）"],
      ["RSS 最小轮询间隔", "5 分钟"],
      ["封面缓存", "30 天 / 单张 4 MB"],
    ]),
  });

  const creditPanel = panel({
    eyebrow: "credits",
    title: "上游与致谢",
    desc: "本插件是这六个插件的整合与重写版；指令名有意保持兼容，方便直接替换。",
    body:
      `<div class="list">` +
      UPSTREAMS.map(
        ([name, author, what]) =>
          `<div class="list-row"><div class="list-main">` +
          `<span class="list-title"><span class="text">${esc(name)}</span>${badge("@" + author)}</span>` +
          `<span class="list-sub">${esc(what)}</span>` +
          `</div><div class="list-actions">` +
          `<a class="icon-btn xs" href="https://github.com/${attr(author)}/${attr(name)}" target="_blank" rel="noopener noreferrer" title="打开原仓库">${icon("link", "sm")}</a>` +
          `</div></div>`,
      ).join("") +
      `</div>` +
      note("数据全部来自公开接口与页面，仅做展示与索引，不缓存、不分发任何影音内容。版权归各站点与版权方所有。") +
      note("授权协议 AGPL-3.0-or-later —— 与上游保持一致。"),
  });

  const gapPanel = panel({
    eyebrow: "notice",
    title: "没有迁移的功能",
    desc: "上游 astrbot_plugin_rsshub 的四个知识库指令（rsshub_kb_*）没有搬过来。",
    body:
      note("原因：那套功能依赖 AstrBot 知识库的内部接口，跨版本极易失效，而且和「番剧」这个主题关系不大——为它引入一层不稳定的耦合不划算。") +
      note("替代方案：用 /sub_export 把订阅导出成 JSON，再从 AstrBot 面板的知识库页面导入，效果一样且不会随版本变化而崩。") +
      `<div class="row">` +
      btn("去订阅页导出", { act: "goto-subs", glyph: "download", sm: true }) +
      `</div>`,
  });

  return (
    viewbar("关于", meta.brand + " v" + meta.version, "") +
    `<div class="deck auto">${idPanel}${safetyPanel}${limitPanel}${creditPanel}${gapPanel}</div>`
  );
};
/* --- 交互层：动作表 + 事件委托 ------------------------------------------- */

/**
 * 刷新「当前视图」。
 *
 * 右上角刷新按钮和大部分写操作都走这里：强制重拉当前视图的数据，
 * 顺带把概览刷新一次（标签页上的计数徽标来自概览，不刷会对不上）。
 */
async function reloadCurrent() {
  await loadView(state.view, { force: true });
  if (state.view !== "overview") {
    await loadOverview();
    paintTabs();
  }
}

/**
 * 轻量刷新概览，失败不打扰用户。
 *
 * 追番 / 订阅数量变了要让标签徽标跟上，但这是「顺手」的事：
 * 拉失败时只把缓存标记清掉，下次进概览再重试，绝不能让主操作看起来失败了。
 */
async function touchOverview() {
  try {
    await loadOverview();
    paintTabs();
  } catch {
    VIEW_LOADED.delete("overview");
  }
}

/** 手动操作（播报 / 轮询 / 刷新索引）之后：概览的计数和日志都会变。 */
async function refreshOverview() {
  await loadOverview();
  if (state.view === "overview") await loadLogs();
  render();
}

/** 追番某一行的标题，用于确认框和回执文案；找不到就给个中性说法。 */
function watchTitleOf(id) {
  const row = (state.watch.items || []).find((item) => String(item.id) === String(id));
  return row ? row.title : "这一条";
}

/** 追番清单的六个字段改动共用一条链路：写库 → 重拉 → 刷新徽标 → 重渲染。 */
async function watchOp(op, id, value) {
  await apiPost("watchlist", { op, id, value });
  await loadWatch();
  await touchOverview();
  render("watch");
}

/**
 * 订阅侧的统一入口。
 *
 * 后端把 add / remove / clear / test / toggle 都收在同一个 POST subs 上，
 * 回执统一放在 message 字段里，所以这里也统一回显到订阅页的 output 区。
 * 例外：test 不需要会话（只是抓一次源看通不通），其余操作必须先选会话。
 */
async function subOp(op, extra = {}, ok = "") {
  if (op !== "test" && !state.umo) {
    toast("先在右上角选一个会话，订阅是按会话存的", "warn");
    return null;
  }
  const result = await apiPost("subs", { op, umo: state.umo, ...extra });
  if (result && typeof result.message === "string") subMessage = result.message;
  if (ok) toast(ok, "ok");
  await loadSubs();
  await touchOverview();
  render("subs");
  return result;
}

/**
 * 保存本会话排除项。
 *
 * 「apply」 为真时才回写到已有订阅 —— 那是一次批量覆盖（每条订阅原有的排除词
 * 会被整份替换），必须由用户显式点那个按钮，不能顺手跟普通保存一起发生。
 */
async function saveExcludes(apply) {
  if (!state.umo) {
    toast("先在右上角选一个会话，排除项是按会话存的", "warn");
    return;
  }
  const values = excludeValues();
  const result = await apiPost("excludes", { umo: state.umo, values, apply });
  excludeDraft.saved = JSON.stringify(values);
  if (apply) {
    await loadSubs();
    toast("已回写到 " + num(result?.applied || 0) + " 条订阅", "ok");
  } else {
    toast("本会话排除项已保存，之后新增的订阅会自动带上", "ok");
  }
  render("subs");
}

/** 导出：umo 为空串表示导出全部会话。 */
async function exportTo(umo) {
  const payload = await apiGet("export", { umo });
  exportText = JSON.stringify(payload, null, 2);
  toast("已导出 " + bytes(new Blob([exportText]).size), "ok");
  render("subs");
}

/**
 * 播报目标的两个按钮跟着 textarea 实时联动，但刻意不走 render()。
 *
 * 重渲染会把 textarea 换成新节点，用户正在打的字和光标都会丢，
 * 所以这里只改按钮的 disabled 状态。
 */
/**
 * ani-rss 面板上的开关 / 间隔直接写插件配置。
 *
 * 写完必须把配置页的草稿和缓存一起作废：两页读的是同一份配置，
 * 只刷新其中一页的话，另一页会拿着旧值再存一次、把这里的改动顶掉。
 */
async function anirssWriteConfig(patch, ok = "已保存") {
  await apiPost("config", { patch });
  state.configDraft.clear();
  VIEW_LOADED.delete("config");
  toast(ok, "ok", 2600);
  await loadView("anirss", { force: true });
  await touchOverview();
}

function syncTargetButtons() {
  const baseline = (state.targets?.configured || []).join("\n");
  const dirty = (state.targetsDraft ?? baseline) !== baseline;
  const host = $(`.view[data-view="targets"]`);
  if (!host) return;
  ["targets-save", "targets-reset"].forEach((act) => {
    const node = $(`[data-act="${act}"]`, host);
    if (node) node.disabled = !dirty;
  });
}

/** 同理：ani-rss 的目标文本框也只同步按钮状态，不整块重渲染。 */
function syncAnirssButtons() {
  const baseline = (state.anirss?.targets || []).join("\n");
  const dirty = (state.anirssDraft ?? baseline) !== baseline;
  const writable = state.anirss?.writable !== false;
  const host = $(`.view[data-view="anirss"]`);
  if (!host) return;
  const save = $(`[data-act="anirss-targets-save"]`, host);
  const reset = $(`[data-act="anirss-targets-reset"]`, host);
  if (save) save.disabled = !dirty || !writable;
  if (reset) reset.disabled = !dirty;
}

/**
 * 把同步 / 导入结果里的账目摘成一句话。
 *
 * 在线同步和离线导入返回的是同一个结构（都出自 「_commit」），提示语也该一致 ——
 * 各写一份迟早会有一边漏掉某个桶。
 */
function anirssChanges(result) {
  return [
    ["新增", result.added],
    ["更新", result.updated],
    ["建订阅", result.subscribed],
    ["失联", result.orphans],
    ["失败", result.failures],
  ]
    .filter(([, rows]) => Array.isArray(rows) && rows.length)
    .map(([label, rows]) => label + " " + num(rows.length))
    .join(" · ");
}

/** 同理：配置页的文本框每敲一个字都重渲染会丢焦点，只同步按钮与计数徽标。 */
function syncConfigButtons() {
  const host = $(`.view[data-view="config"]`);
  if (!host) return;
  const dirty = state.configDraft.size;
  const writable = state.config?.writable !== false;
  const save = $(`[data-act="config-save"]`, host);
  const reset = $(`[data-act="config-reset"]`, host);
  if (save) save.disabled = !dirty || !writable;
  if (reset) reset.disabled = !dirty;
  const tally = $(`.viewbar .badge`, host);
  if (tally) {
    tally.textContent = dirty ? dirty + " 项待保存" : "没有未保存的改动";
    tally.classList.toggle("accent", !!dirty);
  }
}

/** 星期下拉的值域是 0..7（0 = 按今天），后端 push_now 直接吃这个数。 */
const weekdayValue = (node) => {
  const value = Number(node?.value);
  return Number.isFinite(value) && value >= 0 && value <= 7 ? value : 0;
};

const intValue = (node) => Math.max(0, Math.trunc(Number(node?.value) || 0));
/**
 * 全局动作表。
 *
 * 所有按钮 / 下拉 / 开关都只写 data-act，真正的行为集中在这里，
 * 好处是：视图渲染保持纯函数（只读 state 拼字符串），
 * 而 innerHTML 整块替换后也不需要重新绑定任何监听器。
 *
 * 统一签名 (arg, node)：arg 来自 data-arg（渲染时就确定的静态参数，
 * 比如行 id、主题名），node 是触发的那个元素（下拉和开关的值要现取）。
 */
const ACTIONS = {
  /* — 通用 — */
  reload: () => reloadCurrent(),

  "goto-subs": () => go("subs"),

  "boot-retry": () => {
    location.reload();
  },

  /* — 概览：一键操作 — */
  "refresh-anime1": async () => {
    const result = await apiPost("refresh");
    toast("anime1 索引已刷新，共 " + num(result?.entries) + " 条", "ok");
    await refreshOverview();
  },

  "push-now": async () => {
    const result = await apiPost("push_now", { targets: [], weekday: 0 });
    toast(
      result?.sent ? "已向 " + num(result.sent) + " 个会话播报今日新番" : "没有生效的播报目标",
      result?.sent ? "ok" : "warn",
    );
    await refreshOverview();
  },

  "poll-now": async () => {
    const result = await apiPost("poll_now", { umo: "" });
    toast(
      result?.pushed ? "抓到并推送了 " + num(result.pushed) + " 条更新" : "所有订阅都没有新条目",
      result?.pushed ? "ok" : "info",
    );
    await refreshOverview();
  },

  diagnose: async () => {
    state.probes = null;
    await runProbes();
    toast("体检完成：" + num(state.probes?.healthy) + " / " + num(state.probes?.total) + " 个源正常", "ok");
    go("sources");
  },

  "probe-run": async () => {
    state.probes = null;
    render("sources");
    await runProbes();
    toast("体检完成：" + num(state.probes?.healthy) + " / " + num(state.probes?.total) + " 个源正常", "ok");
    render("sources");
  },

  "webhook-test": async () => {
    const result = await apiPost("webhook/test");
    toast(
      "测试事件已投递 " + num(result?.delivered) + " / " + num(result?.targets) + " 个目标",
      result?.delivered ? "ok" : "warn",
    );
    await refreshOverview();
  },

  "clear-logs": async () => {
    const result = await apiPost("logs/clear");
    toast("已清空 " + num(result?.cleared) + " 条日志", "ok");
    await loadLogs();
    render("overview");
  },

  "log-level": (arg) => {
    state.logLevel = arg || "";
    saveState();
    void loadLogs().then(() => render("overview"));
  },

  /* — 抽番试玩 — */
  "gacha-draw": async () => {
    const result = await apiPost("gacha", { genre: state.gacha.genre.trim() });
    state.gacha.text = [result?.text || "", ...(result?.notes || [])].filter(Boolean).join("\n");
    if (!state.gacha.text) toast("这次没抽到东西，可能是季度数据源暂时不通", "warn");
    render("overview");
  },

  "gacha-clear": () => {
    state.gacha.text = "";
    render("overview");
  },

  /* — 配置 — */
  "config-save": (arg, node) => saveConfig(node),

  "config-reset": () => {
    state.configDraft.clear();
    toast("已放弃未保存的改动", "info");
    render("config");
  },

  /* — 会话切换 — */
  "pick-umo": async (arg, node) => {
    const next = String(node?.value || "");
    if (next === state.umo) return;
    state.umo = next;
    // 换会话等于换了一整套上下文，上一会话的搜索结果 / 回执留着只会误导。
    state.search = { keyword: "", items: [], busy: false };
    watchSuggest = { title: "", items: [] };
    subSources = { name: "", items: [] };
    subMessage = "";
    exportText = "";
    saveState();
    VIEW_LOADED.delete("watch");
    VIEW_LOADED.delete("subs");
    VIEW_LOADED.delete("targets");
    await loadView(state.view, { force: true });
  },
  /* — 追番 — */
  "watch-filter": (arg) => {
    state.watch.status = arg || "";
    saveState();
    void loadWatch().then(() => render("watch"));
  },

  "watch-progress": (arg, node) => watchOp("progress", arg, intValue(node)),
  "watch-total": (arg, node) => watchOp("total", arg, intValue(node)),
  "watch-score": (arg, node) => watchOp("score", arg, Number(node?.value) || 0),
  "watch-note": (arg, node) => watchOp("note", arg, String(node?.value || "")),
  "watch-status-set": (arg, node) => watchOp("status", arg, String(node?.value || "")),

  "watch-edit": (arg) => {
    watchEditing = watchEditing === arg ? "" : arg;
    render("watch");
  },

  "watch-delete": async (arg) => {
    const title = watchTitleOf(arg);
    await watchOp("delete", arg);
    toast("已移除「" + title + "」", "ok");
  },

  "search-run": async () => {
    const keyword = state.search.keyword.trim();
    if (!keyword) {
      toast("先填一个番名或 Bangumi 条目 ID", "warn");
      return;
    }
    state.search.busy = true;
    try {
      const result = await apiGet("search", { keyword, limit: 12 });
      state.search.items = result?.items || [];
      if (!state.search.items.length) toast("没搜到条目，换个写法试试（原名 / 加年份）", "warn");
    } finally {
      state.search.busy = false;
      render("watch");
    }
  },

  "watch-add": async (arg) => {
    if (!state.umo) {
      toast("先在右上角选一个会话，追番清单是按会话存的", "warn");
      return;
    }
    const result = await apiPost("watchlist/add", { umo: state.umo, query: arg });
    // 后端顺手做了跨源匹配，把能订阅的 RSS 源一并带回来，直接摆到用户眼前。
    watchSuggest = { title: arg, items: result?.suggestions || [] };
    if (result?.message) toast(result.message, "ok", 6000);
    await loadWatch();
    await touchOverview();
    render("watch");
  },

  "suggest-dismiss": () => {
    watchSuggest = { title: "", items: [] };
    render("watch");
  },

  "sub-add-url": async (arg) => {
    if (!state.umo) {
      toast("先在右上角选一个会话", "warn");
      return;
    }
    // 订阅名沿用刚加入追番的那部番，这样之后 /unsub 写番名就能退订。
    const result = await apiPost("subs", {
      op: "add",
      umo: state.umo,
      value: (watchSuggest.title + " " + arg).trim(),
    });
    if (result && typeof result.message === "string") subMessage = result.message;
    toast(result?.ok ? "已订阅这个源" : result?.message || "订阅失败", result?.ok ? "ok" : "warn", 6000);
    VIEW_LOADED.delete("subs");
    await touchOverview();
    render("watch");
  },

  /* — 订阅 — */
  "sub-enable-all": () => subOp("enable_all", {}, "已启用全部订阅"),
  "sub-disable-all": () => subOp("disable_all", {}, "已暂停全部订阅"),

  "sub-clear": () => subOp("clear", {}, "已清空这个会话的订阅"),

  "sub-toggle": (arg, node) => subOp("toggle", { id: Number(arg), enabled: !!node?.checked }),

  "sub-test-row": (arg) => subOp("test", { value: arg }),

  "sub-remove": (arg) => subOp("remove", { value: arg }),

  "sub-add": async () => {
    const name = state.subDraft.name.trim();
    if (!name) {
      toast("名称是必填的 —— 它也是 /unsub 时要写的那个词", "warn");
      return;
    }
    const result = await subOp("add", { value: (name + " " + state.subDraft.value).trim() });
    if (result?.ok) {
      state.subDraft = { value: "", name: "" };
      render("subs");
    }
  },

  "sub-test": async () => {
    const token = (state.subDraft.value || state.subDraft.name).trim();
    if (!token) {
      toast("填一个地址（或已有订阅的名称）再测", "warn");
      return;
    }
    await subOp("test", { value: token });
  },

  /* — 订阅：选源 — */
  "sub-sources": async () => {
    const name = state.subDraft.name.trim();
    if (!name) {
      toast("先在「名称」里填番名 —— 字幕组是按番名去 Mikan 查的", "warn");
      return;
    }
    const payload = await apiGet("subs/sources", { name });
    const items = Array.isArray(payload?.items) ? payload.items : [];
    subSources = { name: payload?.name || name, items };
    if (!items.length) {
      toast("Mikan 上没找到「" + name + "」，换个写法再试（日文原名通常最准）", "warn", 6000);
    }
    render("subs");
  },

  "sub-sources-clear": () => {
    subSources = { name: "", items: [] };
    render("subs");
  },

  "sub-source-pick": async (arg) => {
    const option = subSources.items.find((row) => String(row.index) === String(arg));
    if (!option) {
      toast("这个候选已经不在列表里了，重新列一次", "warn");
      return;
    }
    // 订阅名沿用「名称」框里的番名，这样之后写番名就能 /unsub。
    const name = (state.subDraft.name || subSources.name).trim();
    const result = await subOp("add", { value: (name + " " + option.url).trim() });
    if (result?.ok) {
      subSources = { name: "", items: [] };
      state.subDraft = { value: "", name: "" };
      render("subs");
    }
  },

  /* — 订阅：排除项 — */
  "exclude-toggle": (arg, node) => {
    const name = String(arg || "");
    const picked = excludeDraft.picked.filter((item) => item !== name);
    if (node?.checked) picked.push(name);
    excludeDraft.picked = picked;
    render("subs");
  },

  "exclude-save": () => saveExcludes(false),

  "exclude-apply": () => saveExcludes(true),

  /* — 备份与迁移 — */
  "export-run": () => exportTo(state.umo),
  "export-all": () => exportTo(""),

  "export-copy": () => copyText(exportText),

  "import-run": async () => {
    let payload = null;
    try {
      payload = JSON.parse(state.importText);
    } catch (error) {
      toast("这不是合法的 JSON：" + errText(error), "err", 6000);
      return;
    }
    const result = await apiPost("import", { payload, umo: state.umo });
    const counts = result?.counts || {};
    const detail = Object.entries(counts)
      .map(([key, value]) => key + " " + num(value))
      .join(" · ");
    toast(detail ? "导入完成：" + detail : "导入完成", "ok", 6000);
    state.importText = "";
    VIEW_LOADED.delete("watch");
    await loadSubs();
    await touchOverview();
    render("subs");
  },
  /* — 播报目标 — */
  "targets-save": async () => {
    const targets = String(state.targetsDraft || "")
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    await apiPost("targets", { targets });
    toast("已保存 " + num(targets.length) + " 个固定播报目标", "ok");
    await loadView("targets", { force: true });
    await touchOverview();
  },

  "targets-reset": () => {
    state.targetsDraft = (state.targets?.configured || []).join("\n");
    render("targets");
  },

  "targets-append": (arg) => {
    const lines = String(state.targetsDraft || "")
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    if (lines.includes(arg)) {
      toast("这个会话已经在列表里了", "info");
      return;
    }
    lines.push(arg);
    state.targetsDraft = lines.join("\n");
    render("targets");
  },

  // 星期下拉只是「下一次点播报按钮时用哪天」，没有副作用，所以不重渲染。
  "push-weekday": (arg, node) => {
    pushWeekday = weekdayValue(node);
  },

  "push-now-custom": async () => {
    const result = await apiPost("push_now", { targets: [], weekday: pushWeekday });
    toast(
      result?.sent ? "已向 " + num(result.sent) + " 个会话播报" : "没有生效的播报目标",
      result?.sent ? "ok" : "warn",
    );
    await touchOverview();
    render();
  },

  /* — ani-rss 同步 — */
  "anirss-test": async () => {
    const result = await apiPost("anirss/test", {});
    if (result?.ok) {
      toast(
        "连上了：共 " + num(result.total || result.entries || 0) + " 条订阅，启用中 " + num(result.active || 0),
        "ok",
        6000,
      );
    } else {
      toast("连不上：" + (result?.error || "未知原因"), "err", 7000);
    }
    await loadView("anirss", { force: true });
  },

  // 手点同步默认 force：会先让 ani-rss 重扫一遍 RSS。手点的人通常刚在下载器里
  // 加完订阅，若等它自己轮询，看到「没有变化」会以为同步坏了。
  "anirss-sync": async () => {
    const result = await apiPost("anirss/sync", { targets: [], force: true });
    if (!result?.ok) {
      toast("同步没跑起来：" + (result?.error || "未知原因"), "err", 7000);
    } else {
      const detail = anirssChanges(result);
      toast(
        "读到 " + num(result.active || 0) + " 条启用中的订阅" + (detail ? "：" + detail : "，两边已经对齐"),
        "ok",
        6000,
      );
    }
    // 同步会动追番表和订阅表，这两页的缓存必须作废，否则切过去还是旧数字。
    VIEW_LOADED.delete("watch");
    VIEW_LOADED.delete("subs");
    await loadView("anirss", { force: true });
    await touchOverview();
  },

  // 前端刻意不先 JSON.parse：后端要区分「不是 JSON」「这份本身是失败响应」
  // 「里面没有条目」三种错法并给不同提示，提前解析只会把这些信息吃掉。
  "anirss-import": async () => {
    const text = String(state.anirssImportDraft || "").trim();
    if (!text) {
      toast("先把 ani-rss 导出的 JSON 粘进文本框", "warn");
      return;
    }
    const result = await apiPost("anirss/import", { payload: text, targets: [] });
    if (!result?.ok) {
      toast("导入失败：" + (result?.error || "未知原因"), "err", 7000);
      return;
    }
    const detail = anirssChanges(result);
    toast(
      "读到 " + num(result.active || 0) + " 条启用中的订阅" + (detail ? "：" + detail : "，两边已经对齐"),
      "ok",
      6000,
    );
    state.anirssImportDraft = "";
    VIEW_LOADED.delete("watch");
    VIEW_LOADED.delete("subs");
    await loadView("anirss", { force: true });
    await touchOverview();
  },

  "anirss-import-clear": () => {
    state.anirssImportDraft = "";
    render("anirss");
  },

  "anirss-import-copy": () => copyText(ANIRSS_EXPORT_CMD, "已复制，去那台电脑上执行"),

  "anirss-webhook-copy": (arg) =>
    arg === "header"
      ? copyText(WEBHOOK_HEADER_TPL, "请求头已复制，粘进 ani-rss 的「请求头」")
      : copyText(WEBHOOK_BODY_TPL, "Body 已复制，粘进 ani-rss 的「消息内容」"),

  "anirss-targets-save": async () => {
    const targets = String(state.anirssDraft || "")
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    await anirssWriteConfig(
      { anirss_sync_targets: targets },
      "已保存 " + num(targets.length) + " 个同步目标",
    );
  },

  "anirss-targets-reset": () => {
    state.anirssDraft = (state.anirss?.targets || []).join("\n");
    render("anirss");
  },

  "anirss-targets-append": (arg) => {
    const lines = String(state.anirssDraft || "")
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean);
    if (lines.includes(arg)) {
      toast("这个会话已经在列表里了", "info");
      return;
    }
    lines.push(arg);
    state.anirssDraft = lines.join("\n");
    render("anirss");
  },

  // arg 是配置键名。这里对着白名单核一遍：data-arg 是渲染时拼进 HTML 的，
  // 万一哪天改错了字，直接把任意键写进配置比静默失败更糟。
  "anirss-flag": async (arg, node) => {
    if (!ANIRSS_FLAGS.some(([key]) => key === arg)) return;
    await anirssWriteConfig({ [arg]: !!node?.checked });
  },

  "anirss-interval": async (arg, node) => {
    const minutes = Math.max(0, Math.min(1440, intValue(node)));
    await anirssWriteConfig(
      { anirss_sync_interval_minutes: minutes },
      minutes > 0 ? "每 " + num(minutes) + " 分钟同步一次" : "已改为只手动同步",
    );
  },

  /* — 卡片预览 — */
  "card-kind": async (arg) => {
    if (!KIND_LABEL[arg] && arg) return;
    state.cards.kind = arg;
    saveState();
    render("cards");
    await fetchShots([state.theme]);
  },

  "card-renderer": async (arg, node) => {
    state.cards.renderer = String(node?.value || "");
    saveState();
    render("cards");
    await fetchShots([state.theme]);
  },

  "cards-render-all": () => fetchShots((state.themes || []).map((theme) => theme.key)),

  "cards-clear": () => {
    state.cards.shots.clear();
    render("cards");
  },

  "card-redraw": async (arg) => {
    state.cards.shots.delete(shotKey(arg));
    await fetchShot(arg);
  },

  "card-download": (arg) =>
    bridge.download(
      "card/download",
      { theme: arg, kind: state.cards.kind, renderer: state.cards.renderer || "" },
      "nexus_" + state.cards.kind + "_" + arg + ".png",
    ),

  "card-zoom": (arg) => {
    const shot = state.cards.shots.get(shotKey(arg));
    if (!shot || !shot.data_uri) {
      toast("这张还没渲染出来", "warn");
      return;
    }
    const theme = state.themeMap.get(arg);
    openLightbox({
      title: theme ? theme.name : arg,
      sub: (KIND_LABEL[state.cards.kind] || state.cards.kind) + " · " + bytes(shot.bytes),
      src: shot.data_uri,
    });
  },

  "card-apply": (arg) => {
    applyTheme(arg, { persist: true });
    const theme = state.themeMap.get(arg);
    toast(
      "预览主题已切到「" + (theme ? theme.name : arg) + "」；要让聊天里的卡片也用它，请去「配置 · 卡片与渲染」改 card_theme",
      "info",
      6000,
    );
  },
};

/**
 * 不可撤销操作的二次确认文案表：动作名 → 由参数生成的问法。
 *
 * 之所以把确认从 handler 里抽到这张表，是为了让 dispatch 能「先问、答应了才转圈」。
 * 如果在 handler 内部 await 确认，按钮会在弹窗还开着的时候就一直转圈，
 * 看着像已经在删了，用户反而不敢点「取消」。
 */
const CONFIRMS = {
  "watch-delete": (arg) => ({
    title: "移除「" + watchTitleOf(arg) + "」？",
    body: "这一条的观看进度和评分会一起丢掉，之后重新加要从头记。",
    yes: "移除",
  }),

  "sub-remove": (arg) => ({
    title: "删除订阅「" + arg + "」？",
    body: "它的去重历史也会一并清掉，以后重新订上会把最近几集当成新的再推一遍。",
    yes: "删除",
  }),

  "sub-clear": () => ({
    title: "清空这个会话的全部订阅？",
    body: "这个会话下每一条订阅连同去重历史都会被删掉，无法恢复。",
    yes: "全部清空",
  }),

  "exclude-apply": () => ({
    title: "把排除词回写到现有的每条订阅？",
    body: "各条订阅原先单独设过的排除词，会被这份清单整体替换掉。",
    yes: "回写",
  }),
};

/**
 * 统一分派。
 *
 * 只给 <button> 套转圈：下拉框和复选框被 disabled 一下会跳焦点、
 * 复选框还会看起来「弹回去」，体验反而更差。
 */
async function dispatch(act, arg, node) {
  const handler = ACTIONS[act];
  if (!handler) {
    toast("这个按钮还没接上处理逻辑：" + act, "warn");
    return;
  }
  const spec = CONFIRMS[act] ? CONFIRMS[act](arg) : null;
  if (spec && !(await ask(spec.body, spec))) return;
  const busy = node && node.tagName === "BUTTON" ? node : null;
  await withBusy(busy, () => handler(arg, node));
}

/** data-live 的实时同步表：只改 state，不重渲染（否则输入框会丢焦点）。 */
const LIVE_SETTERS = {
  "search-keyword": (value) => {
    state.search.keyword = value;
  },
  "sub-name": (value) => {
    state.subDraft.name = value;
  },
  "sub-value": (value) => {
    state.subDraft.value = value;
  },
  "gacha-genre": (value) => {
    state.gacha.genre = value;
  },
  "import-text": (value) => {
    state.importText = value;
  },
  "exclude-custom": (value) => {
    // 刻意不重渲染：会把正在打字的输入框换掉。展开预览等下一次渲染再更新，
    // 「保存」按钮则始终可点，所以不会出现「改了却存不下去」。
    excludeDraft.custom = value;
  },
  "targets-draft": (value) => {
    state.targetsDraft = value;
    syncTargetButtons();
  },
  "anirss-targets-draft": (value) => {
    state.anirssDraft = value;
    syncAnirssButtons();
  },
  "anirss-import-draft": (value) => {
    // 不重渲染：导入按钮始终可点，空内容由 handler 拦，所以没必要动 DOM。
    state.anirssImportDraft = value;
  },
  "cmd-filter": (value) => {
    state.cmdFilter = value;
    filterCommands(value);
  },
};
/**
 * 配置项（data-bind）的改动只进草稿 Map，点保存才真的写回。
 *
 * 敏感字段留空表示「不修改」，所以空值要从草稿里删掉而不是提交空串，
 * 否则一次保存就会把已配置的 token 抹掉。
 */
function onFieldEvent(event) {
  const node = event.target;
  const key = node.dataset.bind;
  if (!key) return;
  let value = null;
  try {
    value = readFieldValue(node);
  } catch (error) {
    toast("「" + key + "」不是合法的 JSON：" + errText(error), "err", 6000);
    return;
  }
  if (node.type === "password" && !String(value || "")) {
    state.configDraft.delete(key);
  } else {
    state.configDraft.set(key, value);
  }
  // 开关和下拉需要立刻更新文案（「已开启 / 已关闭」），文本框重渲染会丢焦点。
  if (node.dataset.kind === "bool" || node.tagName === "SELECT") {
    render("config");
  } else {
    syncConfigButtons();
  }
}

function onViewClick(event) {
  // 外链交给浏览器自己处理，别被下面的 preventDefault 吃掉。
  if (event.target.closest("a")) return;
  const node = event.target.closest("[data-act]");
  if (!node) return;
  // 下拉和开关的值要等 change 才稳定，点击阶段先放过，避免一次操作触发两遍。
  if (["SELECT", "INPUT", "TEXTAREA"].includes(node.tagName)) return;
  event.preventDefault();
  void dispatch(node.dataset.act, node.dataset.arg || "", node);
}

function onViewChange(event) {
  if (event.target.dataset.bind) {
    onFieldEvent(event);
    return;
  }
  const node = event.target.closest("[data-act]");
  // 只认「变化的正是带 data-act 的那个控件」，不接受冒泡上来的容器。
  if (!node || node !== event.target) return;
  void dispatch(node.dataset.act, node.dataset.arg || "", node);
}

function onViewInput(event) {
  if (event.target.dataset.bind) {
    onFieldEvent(event);
    return;
  }
  const setter = LIVE_SETTERS[event.target.dataset.live];
  if (setter) setter(String(event.target.value ?? ""));
}

/** 输入框里按回车等于点它旁边的主按钮；多行文本框要留给换行。 */
function onViewKeydown(event) {
  if (event.key !== "Enter") return;
  if (event.target.tagName === "TEXTAREA") return;
  const act = event.target.dataset.enter;
  if (!act) return;
  event.preventDefault();
  void dispatch(act, event.target.dataset.arg || "", null);
}

/**
 * 卡片放大预览。
 *
 * 刻意不在 index.html 里预留节点：卡片是 base64 内联的大图（单张可达数百 KB），
 * 长期挂在 DOM 上白占内存，用完就整块移除最干净。
 */
function openLightbox({ title, sub, src }) {
  $$(".lightbox").forEach((node) => node.remove());

  const host = document.createElement("div");
  host.className = "lightbox";
  host.dataset.mode = "fit";
  host.innerHTML =
    `<div class="lightbox-bar">` +
    `<span class="lightbox-title"><strong>${esc(title)}</strong><small>${esc(sub)}</small></span>` +
    `<span class="lightbox-hint">滚轮可滚动，Esc 关闭</span>` +
    iconBtn("fit", { act: "lb-fit", title: "适应窗口", xs: true }) +
    iconBtn("actual", { act: "lb-actual", title: "原始尺寸", xs: true }) +
    iconBtn("close", { act: "lb-close", title: "关闭", kind: "danger", xs: true }) +
    `</div>` +
    `<div class="lightbox-stage"><img class="fit" src="${attr(src)}" alt="${attr(title)}" /></div>`;

  const close = () => {
    document.removeEventListener("keydown", onKey);
    host.remove();
  };
  function onKey(event) {
    if (event.key === "Escape") close();
  }
  const setMode = (mode) => {
    host.dataset.mode = mode;
    const img = $("img", host);
    if (img) img.className = mode === "actual" ? "actual" : "fit";
  };

  host.addEventListener("click", (event) => {
    const btnNode = event.target.closest("[data-act]");
    const act = btnNode ? btnNode.dataset.act : "";
    if (act === "lb-close") close();
    else if (act === "lb-fit") setMode("fit");
    else if (act === "lb-actual") setMode("actual");
    // 点图片外的空白区域也关掉，这是大家对灯箱的默认预期。
    else if (event.target.classList.contains("lightbox-stage")) close();
  });

  document.addEventListener("keydown", onKey);
  document.body.appendChild(host);
}
/* --- 启动 ---------------------------------------------------------------- */

function hydrateMeta(meta) {
  state.meta = meta || {};
  state.themes = state.meta.themes || [];
  state.themeMap = new Map(state.themes.map((theme) => [theme.key, theme]));
  const sub = $("#brand-sub");
  if (sub) {
    sub.textContent = (state.meta.display_name || state.meta.brand || "Bangumi Nexus") + " v" + (state.meta.version || "?");
  }
}

/** 连不上后端时给一个能自救的界面，而不是让首屏永远停在「正在连接」。 */
function bootFailed(message) {
  const boot = $("#boot");
  if (!boot) return;
  boot.classList.remove("is-gone");
  boot.innerHTML =
    `<img class="sprite" src="./assets/logo.svg" alt="" width="72" height="72" />` +
    `<strong>没能连上插件后端</strong>` +
    `<p class="mono">${esc(message)}</p>` +
    `<p>常见原因：插件刚重载还没注册好路由，或者当前 AstrBot 版本不支持插件页接口。</p>` +
    btn("重试", { act: "boot-retry", glyph: "refresh", kind: "primary", sm: true });
  const retry = $(`[data-act="boot-retry"]`, boot);
  if (retry) retry.addEventListener("click", () => location.reload());
}

/**
 * 事件绑定只做一次。
 *
 * 视图内容是 innerHTML 整块替换的，所以四个交互事件全部委托在 #views 上；
 * 只有顶栏那几个固定按钮才需要直接绑。
 */
function wireEvents() {
  const themeBtn = $("#btn-theme");
  const themeMenu = $("#theme-menu");

  if (themeBtn && themeMenu) {
    themeBtn.addEventListener("click", (event) => {
      event.stopPropagation();
      paintThemeMenu();
      const open = themeMenu.hidden;
      themeMenu.hidden = !open;
      themeBtn.setAttribute("aria-expanded", open ? "true" : "false");
    });
    themeMenu.addEventListener("click", (event) => {
      const option = event.target.closest("[data-theme]");
      if (!option) return;
      applyTheme(option.dataset.theme, { persist: true });
      closeThemeMenu();
    });
  }

  document.addEventListener("click", (event) => {
    if (event.target.closest(".theme-picker")) return;
    closeThemeMenu();
  });

  const denseBtn = $("#btn-dense");
  if (denseBtn) {
    denseBtn.addEventListener("click", () => {
      state.dense = !state.dense;
      applyDense();
      paintStatus();
      saveState();
    });
  }

  const refreshBtn = $("#btn-refresh");
  if (refreshBtn) {
    refreshBtn.addEventListener("click", () => {
      void withBusy(refreshBtn, reloadCurrent);
    });
  }

  const views = $("#views");
  if (views) {
    views.addEventListener("click", onViewClick);
    views.addEventListener("change", onViewChange);
    views.addEventListener("input", onViewInput);
    views.addEventListener("keydown", onViewKeydown);
  }
}

/**
 * 启动顺序是有讲究的：
 * 先 meta（拿到主题列表才能应用主题），再偏好（可能覆盖主题 / 当前页），
 * 最后才 state.ready = true —— 在此之前 saveState() 是空操作，
 * 避免首屏把默认值当成用户偏好回写一遍。
 */
async function boot() {
  try {
    if (!bridge) throw new Error("这个页面只能在 AstrBot 面板里打开");
    await bridge.ready();

    hydrateMeta(await apiGet("meta"));

    const prefs = await loadPrefs();
    if (prefs) seedSavedPrefs(prefs);

    const wanted =
      prefs && state.themeMap.has(prefs.theme)
        ? prefs.theme
        : state.themeMap.has(state.meta.webui_theme)
          ? state.meta.webui_theme
          : state.themes[0]?.key || "midnight";
    applyTheme(wanted);

    // 没带 hash 时恢复上次停留的页面；带了 hash 说明用户是从链接进来的，尊重它。
    if (!location.hash && prefs && VIEW_KEYS.includes(prefs.view)) {
      location.hash = "#/" + prefs.view;
    }

    applyDense();
    state.ready = true;
    wireEvents();

    window.addEventListener("hashchange", onRoute);
    onRoute();
    paintStatus();

    $("#shell")?.classList.add("is-ready");
    $("#boot")?.classList.add("is-gone");

    // 面板切换语言或明暗时会回调这里，重渲染一次即可跟上。
    if (typeof bridge.onContext === "function") {
      bridge.onContext(() => {
        if (state.ready) render();
      });
    }
  } catch (error) {
    bootFailed(errText(error));
    toast(errText(error), "err", 8000);
  }
}

void boot();