/* =========================================================================
   A股历史形态匹配研究看板 · 前端逻辑（本地运行版）
   数据来源：本地 FastAPI 接口（/api/*），全部为历史统计与指标展示。
   分数含义：与历史上升段启动样本的特征相似程度，不代表未来表现。
   版本：v1.8.0（页面结构优化：单页长滚动 → 顶部导航标签页，纯前端布局调整，
   打分逻辑、数据接口与各模块交互均保持不变；保留 v1.7.0 四项研究能力）

   双语实现约定：
   - 所有界面文案走 I18N.t()（语言包见 static/i18n.js），页面内不散落文案；
   - 语言切换为纯前端行为：不刷新页面、不重新请求接口，
     各模块从 state 缓存的原始数据按新语言重绘（数据层完全不动）；
   - 行业 / 板块 / 指数以中文名为键去匹配接口返回值，仅展示时翻译；
   - 股票名称与代码一律保持原样。
   ========================================================================= */

"use strict";

/* 取词快捷方式 */
const t = (key, vars) => window.I18N.t(key, vars);

/* 图表配色：与 app.css 的设计令牌保持一致（柔和红 / 柔和绿 / 低饱和蓝） */
const C = {
  up: "#c4574e",        // 涨（A 股惯例为红，使用柔和红）
  down: "#3f9c78",      // 跌（使用柔和绿）
  accent: "#3b6fd4",
  accent2: "#6d9ae6",
  violet: "#7b73c9",
  warn: "#bd8a41",
  flat: "#8a929c",
  grid: "#f0f2f6",
  axis: "#a4adb9",
  text: "#1c1f24",
  text2: "#454b55",
  muted: "#78818d",
  border: "#e7eaef",
  barBg: "#edeff4",
};

/* ---------------- 全局状态 ---------------- */
const state = {
  runId: null,
  runInfo: null,
  scanStatus: null,
  items: [],
  total: 0,
  offset: 0,
  limit: 200,
  industries: [],
  hotIndustries: [],
  indices: null,          // 宏观指数快照（切换语言时按新语言重绘）
  indicesFailed: false,
  industryStats: null,    // 行业分布接口原始数据（切换语言时复用，不重复请求）
  chart: null,            // 行业分布图实例
  detailChart: null,      // 个股明细图实例（与容器一一对应，永不销毁）
  detailToken: 0,         // 详情请求令牌：防止快速切换个股时的响应乱序
  detailPayload: null,    // 最近一次详情数据（切换语言时重绘用）
  polling: null,
  news: [],               // 市场资讯条目（一次拉取，前端按分类切换）
  newsCat: "all",
  newsLoaded: false,
  newsUnavailable: false,
  newsCounts: {},
  newsSources: [],
  newsCachedAt: "",
  newsSourceNote: "",
  bt: null,               // 回测摘要（一次拉取，切周期/切语言均本地重绘）
  btHorizon: 10,          // 当前持仓周期（交易日）
  btChart: null,          // 回测净值曲线图实例
  btMode: "tier",         // 回测对比口径：tier=三档分档绩效，resonance=日线+周线共振
  repMeta: "",            // 研究简报最近一次导出时间
  tab: "pool",            // 当前标签页（v1.8.0）
  /* 内嵌简报预览的加载守卫：用独立布尔位 + 批次快照，
     不能只比较「上次批次 === 当前批次」——两者初始同为 null 时，
     首次加载会被误判成「已加载」而直接返回。 */
  repFrameLoaded: false,
  repFrameKey: "",

  /* v1.7.0 研究深度分析（四项，各自独立加载、互不阻塞） */
  industry: null,         // 分行业回测
  significance: null,     // Welch t 检验 + 分年度稳健性
  resonance: null,        // 日线 + 周线双共振对比
  sensitivity: null,      // 参数敏感性分析
  indHorizon: 10,         // 分行业板块当前持仓周期
  indIndustry: null,      // 分行业板块当前选中行业（null = 自动取首个）
  indChart: null,         // 分行业收益对比图实例
};

const $ = (id) => document.getElementById(id);

const fmt = (v, digits = 2) => {
  const n = Number(v);
  return v === null || v === undefined || v === "" || Number.isNaN(n)
    ? "--" : n.toFixed(digits);
};
const fmtPct = (v, digits = 2) =>
  v === null || v === undefined || Number.isNaN(Number(v))
    ? "--" : `${Number(v).toFixed(digits)}%`;
const pctCls = (v) => (Number(v) > 0 ? "up" : Number(v) < 0 ? "down" : "flat");
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

/* 内联 SVG 功能图标：统一描边、随字号缩放、继承 currentColor。
   团队 P0 规则要求功能图标不得使用 emoji，也不宜用打字机符号充当图标
   （此前命中状态用的是 U+2713 字符），故命中 / 一致等状态一律走这里。 */
const ICON = {
  check: (size = 11) =>
    `<svg class="ico" width="${size}" height="${size}" viewBox="0 0 16 16" fill="none" `
    + `stroke="currentColor" stroke-width="2.2" stroke-linecap="round" `
    + `stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="M3 8.6l3.3 3.3L13 5"/></svg>`,
};

/* 涨跌文案遵循 A 股惯例：涨红跌绿 */
function chgText(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "--";
  const n = Number(v);
  const arrow = n > 0 ? "▲" : n < 0 ? "▼" : "—";
  return `${arrow} ${n > 0 ? "+" : ""}${n.toFixed(2)}%`;
}

/* =========================================================================
   启动
   ========================================================================= */
document.addEventListener("DOMContentLoaded", () => {
  bindEvents();
  syncLangUI();
  syncStickyOffset();
  $("scanLog").textContent = t("scanLogIdle");
  refreshAll();
});

function bindEvents() {
  $("btnScan").addEventListener("click", () => {
    $("scanPanel").hidden = false;
    // 扫描面板位于主导航之上，回到页首即可完整露出（顶部导航为吸顶）
    window.scrollTo({ top: 0, behavior: "smooth" });
  });
  $("btnScanClose").addEventListener("click", () => { $("scanPanel").hidden = true; });
  $("btnScanStart").addEventListener("click", startScan);
  $("btnExport").addEventListener("click", exportCsv);
  $("btnRepInline").addEventListener("click", () => loadReportFrame(true));

  // 顶部导航标签页（v1.8.0）：纯前端切换
  initTabs();

  for (const id of ["fBoard", "fIndustry", "fMinScore", "fOrder"]) {
    $(id).addEventListener("change", () => { state.offset = 0; loadPool(); });
  }
  $("fSearch").addEventListener("input", debounce(renderTable, 200));
  $("btnMore").addEventListener("click", () => { state.offset += state.limit; loadPool(true); });

  $("btnDrawerClose").addEventListener("click", closeDrawer);
  $("drawerMask").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });

  // 资讯分类切换：数据一次拉取，切换只做前端过滤，不再打接口
  for (const tab of document.querySelectorAll(".news-tab")) {
    tab.addEventListener("click", () => {
      state.newsCat = tab.dataset.cat || "all";
      for (const x of document.querySelectorAll(".news-tab")) {
        x.classList.toggle("is-on", x === tab);
      }
      renderNews();
    });
  }

  // 语言切换：无刷新，仅重绘界面，不重新请求任何接口
  for (const btn of document.querySelectorAll(".lang-opt")) {
    btn.addEventListener("click", () => I18N.setLang(btn.dataset.lang));
  }
  I18N.onChange(() => { syncLangUI(); rerenderAll(); });

  bindBacktestAndReport();

  window.addEventListener("resize", debounce(() => {
    syncStickyOffset();
    resizeCharts();
  }, 200));
}

function debounce(fn, ms) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}

/* =========================================================================
   顶部导航标签页（v1.8.0）
   -------------------------------------------------------------------------
   把原来的单页长滚动拆成 5 个标签页，减少纵向滚动、提升各模块可达性。
   切换为纯前端行为：不刷新页面、不重新请求接口，所有数据仍按原逻辑在首屏
   一次性加载并缓存在 state 中；各模块内部的功能与交互完全保持不变。
   默认进入「候选池选股」页，可用 URL hash 直达（#pool / #backtest / ...）。
   ========================================================================= */
const TABS = ["pool", "backtest", "report", "market", "settings"];
const DEFAULT_TAB = "pool";

/** 路径参数 → 合法标签页（非法值一律回落到默认页） */
function currentTabFromHash() {
  const h = (window.location.hash || "").replace(/^#/, "").trim();
  return TABS.indexOf(h) >= 0 ? h : DEFAULT_TAB;
}

/** 切换标签页；opts.silent 用于浏览器前进/后退触发的切换（不再回写 URL） */
function switchTab(name, opts) {
  const o = opts || {};
  const tab = TABS.indexOf(name) >= 0 ? name : DEFAULT_TAB;
  state.tab = tab;

  for (const el of document.querySelectorAll(".tab, .nav-menu-item")) {
    el.classList.toggle("is-on", el.dataset.tab === tab);
  }
  for (const btn of document.querySelectorAll(".tab")) {
    btn.setAttribute("aria-selected", btn.dataset.tab === tab ? "true" : "false");
  }
  for (const key of TABS) {
    const panel = $("panel-" + key);
    if (panel) panel.hidden = key !== tab;
  }
  closeNavMenu();

  // 隐藏容器中初始化的图表尺寸为 0，进入该页时按缓存数据重绘并重新测量
  redrawTabCharts(tab);
  // 简报预览惰性加载：只有真正进入该页（或批次变化）才请求简报全文
  if (tab === "report") loadReportFrame();

  if (!o.silent) {
    const want = `#${tab}`;
    if (window.location.hash !== want) window.history.pushState(null, "", want);
  }
  if (!o.keepScroll) window.scrollTo({ top: 0, behavior: "auto" });
}

/** 目标页图表重绘（全部使用已缓存数据，零接口请求） */
function redrawTabCharts(tab) {
  if (tab === "market") {
    drawIndustryChart();
  } else if (tab === "backtest") {
    drawBacktestChart();
    drawIndustryBtChart();
  }
  // 等面板完成布局后再统一测量一次，确保首次进入即为正确尺寸
  window.requestAnimationFrame(() => resizeCharts());
}

function initTabs() {
  for (const el of document.querySelectorAll(".tab, .nav-menu-item")) {
    el.addEventListener("click", () => switchTab(el.dataset.tab));
  }
  $("btnNavMenu").addEventListener("click", toggleNavMenu);
  // 点击菜单外部自动收起
  document.addEventListener("click", (e) => {
    if ($("navMenu").hidden) return;
    if (e.target.closest("#navMenu") || e.target.closest("#btnNavMenu")) return;
    closeNavMenu();
  });
  window.addEventListener("popstate", () => {
    switchTab(currentTabFromHash(), { silent: true, keepScroll: true });
  });
  // 首屏：按 hash 落位（无 hash 即默认候选池页），不在历史里留多余记录
  switchTab(currentTabFromHash(), { silent: true, keepScroll: true });
}

function toggleNavMenu() {
  const menu = $("navMenu");
  const open = menu.hidden;
  menu.hidden = !open;
  $("btnNavMenu").setAttribute("aria-expanded", open ? "true" : "false");
}

function closeNavMenu() {
  const menu = $("navMenu");
  if (menu && !menu.hidden) menu.hidden = true;
  const btn = $("btnNavMenu");
  if (btn) btn.setAttribute("aria-expanded", "false");
}

/* 语言切换按钮的选中态 */
function syncLangUI() {
  const cur = I18N.getLang();
  for (const btn of document.querySelectorAll(".lang-opt")) {
    btn.classList.toggle("is-on", btn.dataset.lang === cur);
  }
}

/* 表头吸顶偏移 = 顶部导航实际高度（品牌区 + 免责声明通栏） */
function syncStickyOffset() {
  const bar = $("topbar");
  if (!bar) return;
  const h = Math.round(bar.getBoundingClientRect().height);
  document.documentElement.style.setProperty("--sticky-top", `${h}px`);
}

/* 语言切换后按新语言重绘所有动态模块（全部使用已缓存数据，零接口请求） */
function rerenderAll() {
  renderMacro();
  renderStatus();
  renderFilters();
  renderTable();
  drawIndustryChart();
  renderNewsTabs();
  renderNews();
  renderNewsMeta();
  renderNewsNote();
  renderBacktest();
  renderIndustryBt();
  renderSignificance();
  renderRobustness();
  renderSensitivity();
  renderReportOutline();
  renderReportMeta();
  if (!state.polling) $("scanLog").textContent = t("scanLogIdle");
  if (state.detailPayload && $("drawer").classList.contains("show")) renderDrawerAll();
  // 英文文案会改变轴标签宽度，切语言后按当前页重新测量一次
  window.requestAnimationFrame(() => resizeCharts());
  if (!$("repFrameMsg").hidden && !$("repFrame").src) {
    $("repFrameMsg").textContent = t("repFrameLoading");
  }
  syncStickyOffset();
}

async function refreshAll() {
  // 宏观面板行情源、资讯接口与回测接口都可能较慢，一律独立加载、不阻塞主内容：
  // 先渲染占位保证版面立即稳定，数据到达后原地替换。
  loadIndices();
  loadNews();
  loadBacktest();
  loadAnalysis();
  renderReportOutline();
  renderReportMeta();
  await Promise.all([loadStatus(), loadFilters()]);
  await loadPool();
  loadIndustryChart();
}

/* =========================================================================
   宏观参考面板
   第一行：国内 4 大指数；第二行：海外隔夜与港股 3 个指数。
   两行都按固定顺序渲染固定数量卡片，缺数据时补占位卡，
   保证网格始终等宽、左右边缘严格对齐、不会参差不齐。
   注意：接口按中文名返回，英文名仅用于展示，匹配逻辑始终走中文键。
   ========================================================================= */
const DOMESTIC_ORDER = ["上证指数", "深证成指", "创业板指", "科创50"];
const OVERSEAS_ORDER = ["纳斯达克", "标普500", "恒生指数"];
/* 海外卡片右上角的时点角标：值为语言包键名 */
const OVERSEAS_TAG = { "纳斯达克": "tagOvernight", "标普500": "tagOvernight", "恒生指数": "tagToday" };

function pickByOrder(items, order) {
  const map = new Map((items || []).map((x) => [x.name, x]));
  return order.map((name) => map.get(name) || { name, close: null, change_pct: null });
}

function macroCard(x) {
  const cls = pctCls(x.change_pct);
  const tagKey = OVERSEAS_TAG[x.name];
  return `
    <div class="macro-card">
      <div class="m-name">
        <span>${esc(I18N.indexName(x.name))}</span>
        ${tagKey ? `<span class="m-tag">${t(tagKey)}</span>` : ""}
      </div>
      <div>
        <div class="m-value ${cls}">${fmt(x.close)}</div>
        <div class="m-change ${cls}">${chgText(x.change_pct)}</div>
      </div>
    </div>`;
}

/* 按当前语言重绘宏观面板：数据缺失时铺占位卡，保证两行网格严格等宽 */
function renderMacro() {
  const dom = $("macroDomestic");
  const ovs = $("macroOverseas");
  const data = state.indices;
  if (!data || !(data.items || []).length) {
    dom.innerHTML = DOMESTIC_ORDER.map((n) => macroCard({ name: n })).join("");
    ovs.innerHTML = OVERSEAS_ORDER.map((n) => macroCard({ name: n })).join("");
    $("macroUpdated").textContent = state.indicesFailed
      ? t("macroUnavailable") : t("macroLoading");
    return;
  }
  dom.innerHTML = pickByOrder(data.items, DOMESTIC_ORDER).map(macroCard).join("");
  ovs.innerHTML = pickByOrder(data.overseas, OVERSEAS_ORDER).map(macroCard).join("");
  $("macroUpdated").textContent = data.cached_at
    ? t("macroCached", { t: String(data.cached_at).slice(5, 16).replace("T", " ") })
    : t("macroLive");
}

async function loadIndices(attempt = 0) {
  state.indicesFailed = false;
  renderMacro();  // 先铺占位卡：行情源慢时版面也不会出现空洞或跳动
  let data = null;
  try {
    data = await get("/api/market/indices", 0);
  } catch { /* 数据源不可用时保留占位卡，保持版面稳定 */ }
  if (!data || !(data.items || []).length) {
    state.indices = null;
    state.indicesFailed = true;
    renderMacro();
    // 行情源首发较慢时补一次重试，避免整页只有宏观面板空着
    if (attempt < 2) setTimeout(() => loadIndices(attempt + 1), 6000);
    return;
  }
  state.indices = data;
  renderMacro();
}

/* =========================================================================
   扫描状态与统计概览
   ========================================================================= */
async function loadStatus() {
  try {
    const s = await get("/api/scan/status");
    state.scanStatus = s;
    state.runInfo = s.latest_run || null;
    state.runId = s.latest_run ? s.latest_run.id : null;
    renderStatus();
  } catch {
    $("statStrip").innerHTML = "";
  }
}

function renderStatus() {
  const s = state.scanStatus || {};
  const run = state.runInfo;

  const pill = $("runState");
  if (s.running) {
    pill.textContent = t("pillRunning");
    pill.className = "pill pill-run";
  } else if (run && run.status === "success") {
    pill.textContent = t("pillLatest", {
      id: run.id,
      t: (run.finished_at || "").slice(5, 16).replace("T", " "),
    });
    pill.className = "pill pill-ok";
  } else {
    pill.textContent = t("pillIdle");
    pill.className = "pill pill-muted";
  }

  const stats = [
    { num: run ? run.candidates : "--", label: t("statTotal"), tier: "tier-total",
      sub: t("statTotalSub") },
    { num: run ? run.high_count : "--", label: t("statHigh"), tier: "tier-high",
      sub: t("statHighSub") },
    { num: run ? run.mid_count : "--", label: t("statMid"), tier: "tier-mid",
      sub: t("statMidSub") },
    { num: run ? run.total_scanned : "--", label: t("statProcessed"), tier: "tier-processed",
      sub: t("statProcessedSub") },
  ];
  $("statStrip").innerHTML = stats.map((x) => `
    <div class="stat-card ${x.tier}">
      <div class="num">${x.num}</div>
      <div class="label">${esc(x.label)}</div>
      <div class="sub">${esc(x.sub)}</div>
    </div>`).join("");
}

/* =========================================================================
   筛选项
   注意：option 的 value 始终是接口认识的原始中文值，
   切换语言只替换展示文本，筛选参数不受语言影响。
   ========================================================================= */
const BOARDS = ["沪主板", "深主板", "创业板", "科创板", "其他"];

async function loadFilters() {
  try {
    const { runId } = state;
    const ind = await get(`/api/pool/industries${runId ? `?run_id=${runId}` : ""}`);
    state.industries = ind.industries || [];
    renderFilters();
  } catch { /* 首次扫描前无行业数据，仅保留默认项 */ }
}

function renderFilters() {
  const sel = $("fIndustry");
  const curInd = sel.value;
  sel.innerHTML =
    `<option value="">${esc(t("filterAllIndustries"))}</option>` +
    state.industries.map((x) =>
      `<option value="${esc(x)}">${esc(I18N.industry(x))}</option>`).join("");
  if ([...sel.options].some((o) => o.value === curInd)) sel.value = curInd;

  const brd = $("fBoard");
  const curBrd = brd.value;
  brd.innerHTML =
    `<option value="">${esc(t("filterAllBoards"))}</option>` +
    BOARDS.map((x) =>
      `<option value="${esc(x)}">${esc(I18N.board(x))}</option>`).join("");
  if ([...brd.options].some((o) => o.value === curBrd)) brd.value = curBrd;
}

/* =========================================================================
   候选池表格
   ========================================================================= */
/* 指标命中项：表格标签与抽屉明细共用同一份定义，保证口径一致。
   label / title 均取自语言包，key 后缀与 i18n.js 的 tag / hit 词条一一对应。 */
const HIT_TAGS = [
  { key: "Macd", get: (b) => b.core && b.core.macd_gold_red },
  { key: "Kdj", get: (b) => b.core && b.core.kdj_gold_j_under_100 },
  { key: "Vol13", get: (b) => b.core && b.core.volume_surge_1_3x },
  { key: "Vol2", get: (b) => b.core && b.core.volume_surge_2x },
  { key: "Turnover", get: (b) => b.core && b.core.turnover_healthy_3_15 },
  { key: "Range", get: (b) => b.core && b.core.range_compact_40d },
  { key: "ChipLow", get: (b) => b.fund && b.fund.chip_concentrated_le_18 },
  { key: "ChipLoose", get: (b) => b.fund && b.fund.chip_loose_gt_20 },
  { key: "Pe", get: (b) => b.fund && (b.fund.pe_tier === "low" || b.fund.pe_tier === "mid") },
  { key: "Return5", get: (b) => b.fund && b.fund.return5_healthy_5_20 },
  { key: "Growth", get: (b) => b.industry && b.industry.growth_board },
  { key: "Hot", get: (b) => b.industry && b.industry.hot_industry },
];

/* 候选池列宽：英文界面下板块 / 行业 / 涨跌幅等列需要更宽的呼吸空间，
   因此中英各一套百分比（两套之和均为 100，列数一致）。
   中文列宽沿用 v1.4.0 已验收的布局，不做改动。 */
const POOL_COLS = {
  zh: [5.5, 6.5, 5.5, 6.5, 9, 25, 5.5, 5.5, 8, 5.5, 5, 6.5, 6],
  en: [5, 6, 5.5, 8.5, 9, 25, 5.5, 5.5, 8, 6, 5, 5.5, 5.5],
};

function applyPoolCols() {
  const cols = POOL_COLS[I18N.getLang()] || POOL_COLS.zh;
  const nodes = document.querySelectorAll("#poolTable colgroup col");
  if (nodes.length !== cols.length) return;
  nodes.forEach((col, i) => { col.style.width = `${cols[i]}%`; });
}

async function loadPool(append = false) {
  const params = new URLSearchParams();
  if (state.runId) params.set("run_id", state.runId);
  if ($("fBoard").value) params.set("board", $("fBoard").value);
  if ($("fIndustry").value) params.set("industry", $("fIndustry").value);
  if ($("fMinScore").value) params.set("min_score", $("fMinScore").value);
  params.set("order", $("fOrder").value);
  params.set("limit", state.limit);
  params.set("offset", append ? state.offset : 0);

  try {
    const data = await get(`/api/pool/?${params}`);
    state.total = data.total;
    state.items = append ? state.items.concat(data.items) : data.items;
    renderTable();
  } catch {
    $("poolEmpty").textContent = t("poolLoadFail");
    $("poolEmpty").style.display = "block";
    $("poolBody").innerHTML = "";
  }
}

function renderTable() {
  applyPoolCols();  // 列宽随语言切换（英文名更长，需要不同分配）
  const kw = $("fSearch").value.trim().toLowerCase();
  // 搜索始终同时匹配中文原名与当前语言的展示名，切换语言后仍可正常检索
  const rows = state.items.filter((x) => {
    if (!kw) return true;
    const hay = [
      x.code, x.name, I18N.industry(x.industry), I18N.board(x.board),
    ].filter(Boolean).join(" ").toLowerCase();
    return hay.includes(kw);
  });

  $("poolEmpty").style.display = rows.length ? "none" : "block";
  $("poolEmpty").textContent = state.items.length
    ? t("poolEmptySearch")
    : t("poolEmptyScan");

  $("poolBody").innerHTML = rows.map((x) => {
    const score = x.total_score ?? 0;
    const bd = x.breakdown || {};
    const d = x.display || {};
    const m = x.metrics || {};
    const tags = HIT_TAGS.map((def) => {
      const on = !!def.get(bd);
      const label = t(`tag${def.key}`);
      const tip = t(`hit${def.key}`);
      return `<span class="tag ${on ? "on" : ""}" title="${esc(tip)}">${esc(label)}</span>`;
    }).join("");
    const shrinkTip = t("thShrinkNote");
    const shrink = d.macd_hist_shrink
      ? `<span class="up" title="${esc(shrinkTip)}">${t("yes")}</span>`
      : `<span class="flat" title="${esc(shrinkTip)}">${t("no")}</span>`;
    return `
      <tr data-code="${x.code}">
        <td class="code-cell">${x.code}</td>
        <td class="name-cell" title="${esc(x.name)}">${esc(x.name)}</td>
        <td class="brd-cell">${esc(I18N.board(x.board)) || "--"}</td>
        <td class="ind-cell" title="${esc(I18N.industry(x.industry) || "")}">${esc(I18N.industry(x.industry)) || "--"}</td>
        <td class="num">
          <div class="score-cell">
            <div class="score-bar"><i style="width:${score}%"></i></div>
            <span class="score-num">${score}</span>
          </div>
        </td>
        <td><div class="hit-tags">${tags}</div></td>
        <td class="num">${fmt(m.close)}</td>
        <td class="num">${fmtPct(d.turnover_rate)}</td>
        <td class="num chg-cell ${pctCls(m.return_5d_pct)}" title="${chgText(m.return_5d_pct)}">${chgText(m.return_5d_pct)}</td>
        <td class="num">${fmt(m.pe)}</td>
        <td class="num">${fmt(d.kdj_j)}</td>
        <td class="num">${m.chip_concentration != null ? fmtPct(m.chip_concentration) : "--"}</td>
        <td>${shrink}</td>
      </tr>`;
  }).join("");

  $("poolCount").textContent =
    t("poolCount", { total: state.total, shown: rows.length });
  $("btnMore").hidden = state.items.length >= state.total;

  for (const tr of $("poolBody").querySelectorAll("tr")) {
    tr.addEventListener("click", () => openDrawer(tr.dataset.code));
  }
}

/* =========================================================================
   扫描
   ========================================================================= */
async function startScan() {
  const withChips = $("chkChips").checked;
  const scope = $("selScope") ? $("selScope").value : "";
  const qs = `with_chips=${withChips}${scope ? `&boards=${scope}` : ""}`;
  $("btnScanStart").disabled = true;
  $("scanLog").textContent = t("scanStarting");
  try {
    await post(`/api/scan/run?${qs}`);
    pollScan();
  } catch (e) {
    $("scanLog").textContent = t("scanStartFail", { msg: e.message });
    $("btnScanStart").disabled = false;
  }
}

function pollScan() {
  const log = $("scanLog");
  if (state.polling) clearInterval(state.polling);
  state.polling = setInterval(async () => {
    try {
      const s = await get("/api/scan/status");
      log.textContent = (s.progress || []).join("\n") || t("scanPreparing");
      log.scrollTop = log.scrollHeight;
      if (!s.running) {
        clearInterval(state.polling);
        state.polling = null;
        $("btnScanStart").disabled = false;
        const run = s.latest_run;
        if (run) {
          log.textContent += "\n\n" + t("scanDone", {
            id: run.id, c: run.candidates, h: run.high_count, m: run.mid_count,
          });
        }
        await refreshAll();
      }
    } catch { /* 轮询失败静默重试 */ }
  }, 1500);
}

function exportCsv() {
  const params = new URLSearchParams();
  if (state.runId) params.set("run_id", state.runId);
  if ($("fMinScore").value) params.set("min_score", $("fMinScore").value);
  window.open(`/api/pool/export?${params}`, "_blank");
}

/* =========================================================================
   行业分布（横向条形图，申万一级行业口径）
   - 热点名单命中的行业用暖色区分，便于和「行业板块分」互相印证
   - 「其他」（行业未匹配）保留灰色展示，占比过高时能一眼看到
   - 数据请求与绘制分离：切换语言时复用 state.industryStats 直接重绘
   ========================================================================= */
function isHotIndustry(name) {
  return state.hotIndustries.some(
    (h) => name === h || name.startsWith(h) || h.startsWith(name)
  );
}

async function loadIndustryChart() {
  try {
    const params = state.runId ? `?run_id=${state.runId}` : "";
    state.industryStats = await get(`/api/market/industry-stats${params}`);
  } catch {
    state.industryStats = null;
  }
  drawIndustryChart();
}

function drawIndustryChart() {
  const el = $("industryChart");
  // 统一的空态渲染：先销毁实例再清空容器，避免实例指向被移除的节点
  const showEmpty = (msg) => {
    if (state.chart) { state.chart.dispose(); state.chart = null; }
    el.innerHTML = `<div class="empty">${msg}</div>`;
  };
  const data = state.industryStats;
  if (!data) { showEmpty(t("indFail")); return; }

  state.hotIndustries = data.hot_industries || [];
  const all = data.items || [];
  const total = data.total || all.reduce((s, x) => s + x.count, 0) || 1;
  const named = all.filter((x) => x.industry !== "其他").slice(0, 16);
  const other = all.find((x) => x.industry === "其他");
  const items = other && other.count > 0 ? named.concat([other]) : named;

  $("indCoverage").textContent = t("indCoverage", {
    n: all.length - (other ? 1 : 0),
    total,
    pct: (data.other_pct ?? 0).toFixed(1),
  });

  if (!items.length) {
    showEmpty(t("indEmpty"));
    return;
  }
  // raw = 中文原名（用于热点匹配与配色），labels = 当前语言的展示名
  const raw = items.map((x) => x.industry).reverse();
  const labels = raw.map((n) => I18N.industry(n));
  const hot = raw.map((n) => isHotIndustry(n));
  const counts = items.map((x) => x.count).reverse();
  const metas = items.map((x) => x).reverse();
  // 配色与整体视觉体系一致：常态低饱和蓝渐变，热点名单命中用柔和暖色
  const colors = raw.map((n, i) => {
    if (n === "其他") return ["#cdd3db", "#b0b9c3"];
    return hot[i] ? ["#e8b183", C.up] : ["#9dbfef", C.accent];
  });

  if (!state.chart) {
    el.innerHTML = "";
    state.chart = echarts.init(el);
  }
  // 英文行业名显著更长，左侧留白相应加宽，避免轴标签被裁切
  const leftPad = I18N.getLang() === "en" ? 205 : 110;
  state.chart.setOption({
    animationDuration: 420,
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "shadow", shadowStyle: { color: "rgba(59,111,212,.05)" } },
      backgroundColor: "rgba(255,255,255,.97)",
      borderColor: C.border,
      borderWidth: 1,
      padding: [10, 12],
      textStyle: { color: C.text, fontSize: 12 },
      formatter: (ps) => {
        const p = ps[0];
        const m = metas[p.dataIndex] || {};
        const hotTip = hot[p.dataIndex] ? t("indHotTip") : "";
        return `<b>${p.name}</b>${hotTip}<br/>`
          + `${t("indTipCount")} <b>${p.value}</b>${t("indTipUnit")} · `
          + `${t("indTipShare")} ${(p.value / total * 100).toFixed(1)}%<br/>`
          + `<span style="color:${C.muted}">${t("indTipAvg")} ${m.avg_score ?? "--"}`
          + ` · ${t("indTipMax")} ${m.max_score ?? "--"}</span>`;
      },
    },
    grid: { left: leftPad, right: 92, top: 10, bottom: 16 },
    xAxis: {
      type: "value",
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { show: false },
      splitLine: { lineStyle: { color: C.grid } },
    },
    yAxis: {
      type: "category",
      data: labels,
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: {
        fontSize: I18N.getLang() === "en" ? 11 : 12,
        color: C.text2, margin: 12,
      },
    },
    series: [{
      name: t("indSeries"),
      type: "bar",
      data: counts.map((v, i) => ({
        value: v,
        itemStyle: {
          borderRadius: [0, 6, 6, 0],
          color: {
            type: "linear", x: 0, y: 0, x2: 1, y2: 0,
            colorStops: [
              { offset: 0, color: colors[i][0] },
              { offset: 1, color: colors[i][1] },
            ],
          },
        },
      })),
      barWidth: 12,
      showBackground: true,
      backgroundStyle: { color: "#f4f6f9", borderRadius: [0, 6, 6, 0] },
      label: {
        show: true, position: "right", distance: 8, fontSize: 11, color: C.muted,
        formatter: (p) => t("indLabel", {
          v: p.value, p: (p.value / total * 100).toFixed(1),
        }),
      },
    }],
  }, true);
  state.chart.resize();
}

/* =========================================================================
   市场资讯
   - 一次拉取全部分类，切换标签只做前端过滤，避免重复请求第三方接口
   - 接口异常或返回空时展示「资讯暂不可用」占位，绝不阻塞主表格
   - 条目跳转第三方原文，rel="noopener noreferrer" 保证安全
   - 英文版保留第三方中文标题，在标题旁标注「News in Chinese」
   ========================================================================= */
const NEWS_CAT_KEY = {
  domestic: "newsCatDomestic",
  overseas: "newsCatOverseas",
  macro: "newsCatMacro",
};

/* 时间展示：`2026-09-15 21:24:41` → `09-15 21:24` */
function fmtClock(s) {
  const str = String(s || "").trim();
  if (str.length < 16) return str || "--";
  return `${str.slice(5, 10)} ${str.slice(11, 16)}`;
}

async function loadNews() {
  let data = null;
  try {
    data = await get("/api/news", 0);
  } catch {
    /* 接口不可用：保持模块结构，只显示占位 */
  }
  if (!data || !data.available || !(data.items || []).length) {
    state.newsUnavailable = true;
    state.newsLoaded = false;
    state.newsMetaText = (data && data.message) || "";
    renderNews();
    renderNewsMeta();
    renderNewsNote();
    return;
  }
  state.newsUnavailable = false;
  state.newsLoaded = true;
  state.news = data.items;
  state.newsCounts = data.counts || {};
  state.newsSources = data.sources || [];
  state.newsCachedAt = data.cached_at || "";
  state.newsSourceNote = data.source_note || "";
  renderNewsTabs();
  renderNews();
  renderNewsMeta();
  renderNewsNote();
}

/* 分类标签上的数量来自接口统计，与实际可展示条数一致 */
function renderNewsTabs() {
  const counts = state.newsCounts || {};
  for (const tab of document.querySelectorAll(".news-tab")) {
    const cat = tab.dataset.cat || "all";
    const base = cat === "all" ? t("newsTabAll") : t(NEWS_CAT_KEY[cat] || "newsTabAll");
    const n = counts[cat];
    tab.textContent = n === undefined ? base : `${base} ${n}`;
  }
}

function renderNewsMeta() {
  if (!state.newsLoaded) {
    $("newsMeta").textContent = state.newsUnavailable
      ? (state.newsMetaText || t("newsUnavailable"))
      : "—";
    return;
  }
  $("newsMeta").textContent = t("newsMeta", {
    n: state.news.length,
    src: state.newsSources.join(" / ") || "--",
    t: fmtClock(state.newsCachedAt),
  });
}

/* 资讯说明与英文版标注：接口下发的 source_note 为中文，英文界面改用语言包文案 */
function renderNewsNote() {
  const en = I18N.getLang() === "en";
  $("newsNote").textContent = en
    ? t("newsNoteEn")
    : (state.newsSourceNote || t("newsNote"));
  $("newsLangBadge").hidden = !en;
}

function renderNews() {
  const list = $("newsList");
  if (state.newsUnavailable) {
    list.innerHTML = `<div class="news-empty">${esc(t("newsUnavailable"))}</div>`;
    return;
  }
  if (!state.newsLoaded) {
    list.innerHTML = `<div class="news-empty">${esc(t("newsLoading"))}</div>`;
    return;
  }
  const rows = state.newsCat === "all"
    ? state.news
    : state.news.filter((x) => x.category === state.newsCat);

  if (!rows.length) {
    list.innerHTML = `<div class="news-empty">${esc(t("newsEmptyCat"))}</div>`;
    return;
  }
  list.innerHTML = rows.map((x) => {
    // 分类标签做双语，标题保留第三方中文原文不做翻译
    const cat = NEWS_CAT_KEY[x.category] ? x.category : "domestic";
    const url = x.url || "";
    const inner = `
      <span class="news-tag t-${cat}">${esc(t(NEWS_CAT_KEY[cat]))}</span>
      <span class="news-title" title="${esc(x.title)}">${esc(x.title)}</span>
      <span class="news-src" title="${esc(x.source)}">${esc(x.source) || "--"}</span>
      <span class="news-time">${fmtClock(x.time)}</span>`;
    // 有原文链接则整条可点击跳转，否则退化为纯文本行
    return url
      ? `<a class="news-item" href="${esc(url)}" target="_blank" rel="noopener noreferrer">${inner}</a>`
      : `<div class="news-item">${inner}</div>`;
  }).join("");
}

/* =========================================================================
   个股明细抽屉
   =========================================================================
   渲染要点（此前「偶发空白」的根因与修复）：
   1. 明细图容器 #dChart 一经创建就不再清空 innerHTML。此前每次渲染都先
      清空容器，但 ECharts 实例仍持有已被移除的 DOM 节点，第二次打开时
      setOption 作用在游离节点上，画布不显示 —— 表现为白屏。
   2. 加载中 / 失败提示放在独立的 #dChartMsg 浮层里，与图表容器解耦。
   3. 每次请求带令牌，快速连点不同个股时只采纳最后一次响应，避免乱序。
   4. 最近一次明细数据缓存在 state.detailPayload，切换语言时据此重绘。
   ========================================================================= */

/* PE 行业分位的中文区间说明 → 语言包键名 */
const PE_TIER_KEY = {
  low: "peTierLow", mid: "peTierMid",
  high: "peTierHigh", missing: "peTierMissing",
};

/* 三组得分明细定义：与打分引擎 v1.1 逐项对应，含每项分值 */
function buildScoreGroups(bd) {
  const core = bd.core || {};
  const fund = bd.fund || {};
  const ind = bd.industry || {};
  const tier = fund.pe_tier || "missing";
  const peHit = tier === "low" || tier === "mid";
  const pePts = tier === "low" ? 6 : tier === "mid" ? 3 : 0;
  return [
    {
      key: "core", cls: "g-core", title: t("groupCore"),
      score: bd.core_score ?? 0, max: 50,
      items: [
        { name: t("itemMacd"), pts: 15, hit: !!core.macd_gold_red },
        { name: t("itemKdj"), pts: 12, hit: !!core.kdj_gold_j_under_100 },
        { name: t("itemVol13"), pts: 8, hit: !!core.volume_surge_1_3x },
        { name: t("itemVol2"), pts: 5, hit: !!core.volume_surge_2x },
        { name: t("itemTurnover"), pts: 5, hit: !!core.turnover_healthy_3_15 },
        { name: t("itemRange"), pts: 5, hit: !!core.range_compact_40d },
      ],
    },
    {
      key: "fund", cls: "g-fund", title: t("groupFund"),
      score: bd.fund_score ?? 0, max: 20,
      items: [
        { name: t("itemChipLow"), pts: 3, hit: !!fund.chip_concentrated_le_18 },
        { name: t("itemChipLoose"), pts: 5, hit: !!fund.chip_loose_gt_20 },
        { name: t("itemPe", { tier: t(PE_TIER_KEY[tier] || "peTierMissing") }),
          pts: pePts, max: 6, hit: peHit },
        { name: t("itemReturn5"), pts: 6, hit: !!fund.return5_healthy_5_20 },
      ],
    },
    {
      key: "industry", cls: "g-ind", title: t("groupInd"),
      score: bd.industry_score ?? 0, max: 30,
      items: [
        { name: t("itemGrowthBoard"), pts: 8, hit: !!ind.growth_board },
        { name: t("itemHotIndustry"), pts: 22, hit: !!ind.hot_industry },
      ],
    },
  ];
}

async function openDrawer(code) {
  const item = state.items.find((x) => x.code === code);
  const token = ++state.detailToken;

  $("dTitle").textContent = item ? `${item.name} · ${item.code}` : code;
  $("dSub").textContent = t("drawerLoading");
  $("dHero").innerHTML = "";
  $("dGroups").innerHTML = "";
  setChartMsg(t("drawerChartLoading"), false);
  $("drawerMask").classList.add("show");
  $("drawer").classList.add("show");
  $("drawer").setAttribute("aria-hidden", "false");

  const params = new URLSearchParams({ days: "120" });
  if (state.runId) params.set("run_id", state.runId);

  let detail;
  try {
    detail = await get(`/api/pool/${encodeURIComponent(code)}?${params}`);
  } catch (e) {
    if (token !== state.detailToken) return;
    $("dSub").textContent = item
      ? [I18N.board(item.board), I18N.industry(item.industry)].filter(Boolean).join(" ")
      : "";
    state.detailPayload = null;
    $("dChart").innerHTML = "";
    setChartMsg(t("drawerLoadFail", { msg: e.message }), true);
    return;
  }
  if (token !== state.detailToken) return;  // 已切换到其它个股，丢弃过期响应

  const profile = detail.profile || item || {};
  const score = detail.score || item || {};
  state.detailPayload = { profile, score, detail };
  renderDrawerAll();
  // 抽屉滑入动画结束后容器尺寸才稳定，再校正一次图表宽度
  setTimeout(() => state.detailChart && state.detailChart.resize(), 360);
}

/* 抽屉整体重绘：语言切换与首次加载共用同一条渲染路径 */
function renderDrawerAll() {
  const payload = state.detailPayload;
  if (!payload) return;
  renderDetailHeader(payload.profile, payload.score, payload.detail);
  try {
    renderDetailChart(payload.detail);
    setChartMsg("", false);
  } catch (e) {
    setChartMsg(t("drawerChartFail", { msg: e.message }), true);
  }
}

/* 抽屉头部：档案信息 + 综合匹配分概览 + 三组得分明细 */
function renderDetailHeader(profile, score, detail) {
  const name = profile.name || detail.code || "";
  const code = profile.code || detail.code || "";
  if (name) $("dTitle").textContent = `${name} · ${code}`;

  const bd = score.breakdown || {};
  const total = score.total_score ?? bd.total_score ?? 0;
  // 板块 / 行业按当前语言展示（接口返回的始终是中文原名）
  const meta = [I18N.board(profile.board), I18N.industry(profile.industry)]
    .filter(Boolean).join(" · ");
  const inPool = score.total_score !== undefined && score.total_score !== null;
  $("dSub").textContent = inPool
    ? `${meta} · ${t("drawerScoreLine", { n: total })}`
    : `${meta} · ${t("drawerNotInPool")}`;

  const groups = buildScoreGroups(bd);
  $("dHero").innerHTML = `
    <div class="hero-top">
      <span class="hero-num">${total}<small>/100</small></span>
      <span class="hero-label">${esc(t("drawerScore"))}</span>
    </div>
    <div class="hero-bar"><i style="width:${Math.max(0, Math.min(100, total))}%"></i></div>
    <div class="hero-groups">
      ${groups.map((g) => `
        <div class="hero-group ${g.cls}">
          <div class="g-name">${esc(g.title)}</div>
          <div class="g-val">${g.score}<small> / ${g.max}</small></div>
        </div>`).join("")}
    </div>`;

  $("dGroups").innerHTML = groups.map((g) => `
    <div class="group-card ${g.cls}">
      <div class="group-head">
        <h3>${esc(g.title)}</h3>
        <span class="g-subtotal">${esc(t("subtotal", { a: g.score, b: g.max }))}</span>
      </div>
      <div class="item-list">
        ${g.items.map((it) => `
          <div class="item ${it.hit ? "hit" : ""}">
            <span class="dot">${it.hit ? ICON.check(11) : ""}</span>
            <span class="name">${esc(it.name)}</span>
            <span class="pts">${it.hit ? `+${it.pts}` : "0"}</span>
          </div>`).join("")}
      </div>
    </div>`).join("");
}

function setChartMsg(text, isError) {
  const el = $("dChartMsg");
  el.textContent = text || "";
  el.className = `chart-msg${text ? " show" : ""}${isError ? " error" : ""}`;
}

/* 明细图：4 个 grid（K线 / 成交量 / MACD / KDJ）
   注意：多 grid 布局必须让 xAxis 与 yAxis 都显式声明 gridIndex，
   否则 ECharts 内部坐标轴与网格错配并抛异常，表现为图表空白。 */
function renderDetailChart(detail) {
  if (!detail || !detail.kline || !detail.indicators || !detail.dates) {
    throw new Error(t("detailIncomplete"));
  }
  const el = $("dChart");
  if (!state.detailChart) {
    // 首次创建实例；此后始终复用，绝不清空容器（清空会让实例指向游离节点）
    state.detailChart = echarts.init(el);
  }
  const c = state.detailChart;
  c.clear();

  const dates = detail.dates;
  const k = detail.kline;
  const ind = detail.indicators;
  const upColor = C.up, downColor = C.down;  // A股惯例：涨红跌绿（柔和色）

  // ECharts K 线数据顺序：[open, close, low, high]
  const candles = k.open.map((o, i) => [o, k.close[i], k.low[i], k.high[i]]);
  const grid = [
    { left: 58, right: 20, top: 22, height: "40%" },
    { left: 58, right: 20, top: "52%", height: "12%" },
    { left: 58, right: 20, top: "70%", height: "12%" },
    { left: 58, right: 20, top: "86%", height: "10%" },
  ];
  const xAxis = [0, 1, 2, 3].map((i) => ({
    type: "category", data: dates, gridIndex: i, boundaryGap: true,
    axisLine: { lineStyle: { color: C.border } },
    axisTick: { show: false },
    splitLine: { show: false },
    axisLabel: { show: i === 3, fontSize: 10, color: C.axis },
  }));
  const yAxis = [
    { gridIndex: 0, scale: true, splitLine: { lineStyle: { color: C.grid } },
      axisLine: { show: false }, axisLabel: { fontSize: 10, color: C.axis } },
    { gridIndex: 1, scale: true, splitLine: { show: false }, axisLine: { show: false }, axisLabel: { show: false } },
    { gridIndex: 2, scale: true, splitLine: { show: false }, axisLine: { show: false }, axisLabel: { show: false } },
    { gridIndex: 3, scale: true, splitLine: { show: false }, axisLine: { show: false },
      axisLabel: { fontSize: 10, color: C.axis } },
  ];

  c.setOption({
    animation: false,
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "cross", lineStyle: { color: "#c8cfd8" } },
      backgroundColor: "rgba(255,255,255,.97)",
      borderColor: C.border, borderWidth: 1,
      textStyle: { color: C.text, fontSize: 12 },
    },
    axisPointer: { link: [{ xAxisIndex: "all" }] },
    grid, xAxis, yAxis,
    dataZoom: [
      { type: "inside", xAxisIndex: [0, 1, 2, 3], start: 40, end: 100 },
      { type: "slider", xAxisIndex: [0, 1, 2, 3], bottom: 2, height: 14,
        borderColor: "transparent", backgroundColor: "#f2f4f8",
        fillerColor: "rgba(59,111,212,.10)", handleSize: 0,
        textStyle: { color: C.axis, fontSize: 10 } },
    ],
    series: [
      {
        name: t("serKline"), type: "candlestick", xAxisIndex: 0, yAxisIndex: 0,
        data: candles,
        itemStyle: { color: upColor, color0: downColor, borderColor: upColor, borderColor0: downColor },
      },
      { name: "MA5", type: "line", data: ind.ma5, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { width: 1, color: C.warn } },
      { name: "MA10", type: "line", data: ind.ma10, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { width: 1, color: C.violet } },
      { name: "MA20", type: "line", data: ind.ma20, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { width: 1, color: C.accent } },
      { name: "MA60", type: "line", data: ind.ma60, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { width: 1, color: C.flat } },
      {
        name: t("serVolume"), type: "bar", xAxisIndex: 1, yAxisIndex: 1,
        data: k.volume.map((v, i) => ({
          value: v, itemStyle: { color: k.close[i] >= k.open[i] ? upColor : downColor },
        })),
      },
      {
        name: t("serMacdHist"), type: "bar", xAxisIndex: 2, yAxisIndex: 2,
        data: ind.hist.map((v) => ({ value: v, itemStyle: { color: v >= 0 ? upColor : downColor } })),
      },
      { name: "DIF", type: "line", xAxisIndex: 2, yAxisIndex: 2, symbol: "none", data: ind.dif, lineStyle: { width: 1, color: C.accent } },
      { name: "DEA", type: "line", xAxisIndex: 2, yAxisIndex: 2, symbol: "none", data: ind.dea, lineStyle: { width: 1, color: C.warn } },
      { name: "K", type: "line", xAxisIndex: 3, yAxisIndex: 3, symbol: "none", data: ind.k, lineStyle: { width: 1, color: C.accent } },
      { name: "D", type: "line", xAxisIndex: 3, yAxisIndex: 3, symbol: "none", data: ind.d, lineStyle: { width: 1, color: C.warn } },
      { name: "J", type: "line", xAxisIndex: 3, yAxisIndex: 3, symbol: "none", data: ind.j, lineStyle: { width: 1, color: C.violet } },
    ],
  }, true);
  requestAnimationFrame(() => c.resize());
}

function closeDrawer() {
  $("drawerMask").classList.remove("show");
  $("drawer").classList.remove("show");
  $("drawer").setAttribute("aria-hidden", "true");
}

/* 图表重新测量。
   注意：隐藏标签页内的容器尺寸为 0，对隐藏图表调用 resize() 会把画布压成 0，
   反而破坏已经渲染好的结果——因此只重测当前真正可见的图表。
   容器挂载在隐藏面板（display:none）或是抽屉未展开时，clientWidth/Height 为 0。 */
function resizeCharts() {
  for (const key of ["chart", "detailChart", "btChart", "indChart"]) {
    const inst = state[key];
    if (!inst || typeof inst.resize !== "function") continue;
    const dom = typeof inst.getDom === "function" ? inst.getDom() : null;
    if (!dom || !dom.clientWidth || !dom.clientHeight) continue;
    inst.resize();
  }
}

/* =========================================================================
   策略回测（v1.6.0）
   -------------------------------------------------------------------------
   数据一次拉取（/api/backtest/summary）缓存在 state.bt，
   切换持仓周期与切换语言都只做本地重绘，不重复请求接口。
   图表为「三档累计净值 + 沪深300 基准」四条线，按调仓周期复利，起点 = 1.00。
   ========================================================================= */
const BT_GROUP_KEY = {
  high: { label: "btGroupHigh", short: "btGrpHighShort", color: "#c4574e" },
  mid: { label: "btGroupMid", short: "btGrpMidShort", color: "#d99a3d" },
  low: { label: "btGroupLow", short: "btGrpLowShort", color: "#8c93a3" },
};
const BT_BENCH_COLOR = "#6b7280";
const BT_ORDER = ["high", "mid", "low"];

const btGroupLabel = (g) => t((BT_GROUP_KEY[g] || {}).label || "btThGroup");
const btGroupShort = (g) => t((BT_GROUP_KEY[g] || {}).short || "btThGroup");

/* 百分比展示：带正负号（涨红跌绿沿用全站惯例） */
function btPct(v, digits = 2) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "--";
  const n = Number(v) * 100;
  return `${n > 0 ? "+" : ""}${n.toFixed(digits)}%`;
}
function btRatio(v) {
  return v === null || v === undefined || Number.isNaN(Number(v)) ? "—" : Number(v).toFixed(2);
}

async function loadBacktest() {
  $("btBody").innerHTML = `<tr><td colspan="7" class="bt-loading">${esc(t("btLoading"))}</td></tr>`;
  try {
    state.bt = await get("/api/backtest/summary");
  } catch {
    state.bt = null;
  }
  renderBacktest();
}

function renderBacktest() {
  const d = state.bt;
  const empty = $("btEmpty");
  const ok = d && d.available;
  empty.hidden = !!ok;
  if (!ok) {
    $("btBody").innerHTML = "";
    $("btMeta").innerHTML = "";
    $("btMethod").innerHTML = "";
    $("btEmpty").textContent = d && d.reason ? d.reason : t("btEmpty");
    if (state.btChart) { state.btChart.dispose(); state.btChart = null; }
    $("btChart").innerHTML = "";
    return;
  }

  const horizons = (d.horizons || []).map(Number);
  if (!horizons.includes(state.btHorizon)) state.btHorizon = horizons.includes(20) ? 20 : horizons[0];
  for (const b of document.querySelectorAll("#btHorizon .seg-opt")) {
    b.classList.toggle("is-on", Number(b.dataset.h) === state.btHorizon);
  }
  for (const b of document.querySelectorAll("#btMode .seg-opt")) {
    b.classList.toggle("is-on", b.dataset.mode === state.btMode);
  }

  // 概况条
  const bm = d.benchmark || {};
  const items = [
    ["btRange", `${d.start_date || "--"} ~ ${d.end_date || "--"}`],
    ["btPoints", `${d.rebalance_points || 0} ${t("btUnitPoints")}`],
    ["btStocks", `${(d.stock_count || 0).toLocaleString()} ${t("btUnitStocks")}`],
    ["btSamples", `${(d.obs_count || 0).toLocaleString()} ${t("btUnitSamples")}`],
    ["btBenchmark", `${I18N.indexName(bm.name) || bm.name || "--"}`],
    ["btScoreMax", `${d.score_max || "--"} ${t("btUnitScore")}`],
  ];
  $("btMeta").innerHTML = items
    .map(([k, v]) => `<span>${esc(t(k))}　<b>${esc(v)}</b></span>`).join("");

  // 两种口径互斥展示：分档绩效（三组） / 共振对比（双共振 vs 单日线）
  const showRes = state.btMode === "resonance";
  const resOk = !!(state.resonance && state.resonance.available);
  $("btTierWrap").hidden = showRes;
  $("btResWrap").hidden = !showRes || !resOk;

  renderBacktestTable();
  if (showRes) renderResonanceTable();
  drawBacktestChart();

  // 方法学说明（折叠展示，避免占用主视觉）
  const notes = d.method_notes || [];
  $("btMethod").innerHTML = showRes
    ? (resOk
        ? `<p class="bt-foot">${esc(t("rsFootnote"))}</p>`
        : `<p class="bt-foot">${esc(t("rsUnavailable"))}</p>`)
    : (notes.length
        ? `<details class="bt-notes"><summary>${esc(t("btDialogTitle"))}</summary><ol>${
            notes.map((x) => `<li>${esc(x)}</li>`).join("")}</ol></details>`
          + `<p class="bt-foot">${esc(t("btFootnote"))}</p>`
        : `<p class="bt-foot">${esc(t("btFootnote"))}</p>`);
}

function renderBacktestTable() {
  const d = state.bt;
  const rows = (d.stats_by_horizon || {})[String(state.btHorizon)] || [];
  const byGroup = {};
  for (const r of rows) byGroup[r.group] = r;
  $("btBody").innerHTML = BT_ORDER.map((g) => {
    const r = byGroup[g] || {};
    const color = (BT_GROUP_KEY[g] || {}).color || C.flat;
    return `<tr>
      <td><span class="grp-dot" style="background:${color}"></span>${esc(btGroupLabel(g))}</td>
      <td class="num">${(r.samples || 0).toLocaleString()}</td>
      <td class="num">${r.win_rate == null ? "—" : (Number(r.win_rate) * 100).toFixed(1) + "%"}</td>
      <td class="num ${pctCls(r.avg_return)}">${btPct(r.avg_return)}</td>
      <td class="num ${pctCls(r.excess_return)}">${btPct(r.excess_return)}</td>
      <td class="num">${r.max_drawdown == null ? "—" : (Number(r.max_drawdown) * 100).toFixed(2) + "%"}</td>
      <td class="num">${btRatio(r.pl_ratio)}</td>
    </tr>`;
  }).join("");
}

const RS_COLOR = { resonance: "#7b73c9", daily: "#3b6fd4" };

/* 共振对比表：双共振分组 vs 单日线高匹配分组（当前持仓周期） */
function renderResonanceTable() {
  const res = state.resonance;
  const body = $("btResBody");
  if (!res || !res.available) { body.innerHTML = ""; return; }
  const row = (res.stats_by_horizon || {})[String(state.btHorizon)];
  if (!row) { body.innerHTML = ""; return; }
  const fmtRow = (label, s, color, diff) => `<tr>
      <td><span class="grp-dot" style="background:${color}"></span>${esc(label)}</td>
      <td class="num">${(s.samples || 0).toLocaleString()}</td>
      <td class="num">${s.win_rate == null ? "—" : (Number(s.win_rate) * 100).toFixed(1) + "%"}</td>
      <td class="num ${pctCls(s.avg_return)}">${btPct(s.avg_return)}</td>
      <td class="num">${s.max_drawdown == null ? "—" : (Number(s.max_drawdown) * 100).toFixed(2) + "%"}</td>
      <td class="num ${diff && diff.win_rate != null ? pctCls(diff.win_rate) : ""}">${
        diff && diff.win_rate != null ? btPct(diff.win_rate) : "—"}</td>
      <td class="num ${diff && diff.avg_return != null ? pctCls(diff.avg_return) : ""}">${
        diff && diff.avg_return != null ? btPct(diff.avg_return) : "—"}</td>
    </tr>`;
  body.innerHTML =
    fmtRow(res.resonance.label, res.resonance, RS_COLOR.resonance, row.diff)
    + fmtRow(res.daily_high.label, res.daily_high, RS_COLOR.daily, null);
  const note = t("rsNote", {
    rate: (Number(res.weekly_hit_rate || 0) * 100).toFixed(1),
    rows: (res.marked_rows || 0).toLocaleString(),
  });
  $("btResNote").textContent = note;
}

function drawBacktestChart() {
  const el = $("btChart");
  const d = state.bt;
  if (!d || !d.available) return;
  if (state.btMode === "resonance") { drawResonanceChart(el); return; }
  const h = String(state.btHorizon);
  const eq = d.equity || {};
  const bench = (d.benchmark_equity || {})[h] || [];

  const series = [];
  let dates = [];
  for (const g of BT_ORDER) {
    const pts = (eq[g] || {})[h] || [];
    if (!pts.length) continue;
    if (pts.length > dates.length) dates = pts.map((p) => p.date);
    series.push({
      name: btGroupLabel(g),
      color: (BT_GROUP_KEY[g] || {}).color || C.accent,
      values: pts.map((p) => p.nav),
    });
  }
  if (bench.length) {
    if (bench.length > dates.length) dates = bench.map((p) => p.date);
    series.push({
      name: I18N.indexName((d.benchmark || {}).name) || t("btBenchmark"),
      color: BT_BENCH_COLOR,
      values: bench.map((p) => p.nav),
    });
  }

  if (!series.length) {
    if (state.btChart) { state.btChart.dispose(); state.btChart = null; }
    el.innerHTML = `<div class="empty">${esc(t("btEmpty"))}</div>`;
    return;
  }
  if (el.querySelector(".empty")) el.innerHTML = "";
  if (!state.btChart) state.btChart = echarts.init(el);

  state.btChart.setOption({
    animationDuration: 480,
    textStyle: { fontFamily: "inherit" },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "line", lineStyle: { color: "#c7ccd6" } },
      backgroundColor: "rgba(255,255,255,.97)",
      borderColor: C.border, borderWidth: 1, padding: [9, 12],
      textStyle: { color: C.text, fontSize: 12 },
      valueFormatter: (v) => (v == null ? "--" : Number(v).toFixed(3)),
    },
    legend: {
      top: 0, right: 4, itemWidth: 14, itemHeight: 8, itemGap: 16,
      textStyle: { color: C.text2, fontSize: 11 },
    },
    grid: { left: 56, right: 22, top: 34, bottom: 44 },
    xAxis: {
      type: "category", data: dates, boundaryGap: false,
      axisLine: { lineStyle: { color: C.border } },
      axisTick: { show: false },
      axisLabel: { fontSize: 10, color: C.axis, hideOverlap: true },
    },
    yAxis: {
      type: "value", scale: true,
      splitLine: { lineStyle: { color: C.grid } },
      axisLine: { show: false }, axisTick: { show: false },
      axisLabel: { fontSize: 10, color: C.axis, formatter: (v) => Number(v).toFixed(2) },
    },
    series: series.map((s) => ({
      name: s.name, type: "line", data: s.values, smooth: false,
      symbol: "none", lineStyle: { width: 1.8, color: s.color },
      itemStyle: { color: s.color },
      emphasis: { focus: "series" },
    })),
  }, true);
  state.btChart.resize();
}

/* 共振模式图表：三个持仓周期上「双共振 / 单日线」平均收益率的对照柱状图 */
function drawResonanceChart(el) {
  const res = state.resonance;
  const stats = (res && res.stats) || [];
  if (!stats.length) {
    if (state.btChart) { state.btChart.dispose(); state.btChart = null; }
    el.innerHTML = `<div class="empty">${esc(t("rsUnavailable"))}</div>`;
    return;
  }
  if (el.querySelector(".empty")) el.innerHTML = "";
  if (!state.btChart) state.btChart = echarts.init(el);
  const cats = stats.map((s) => `${s.horizon}${t("btUnitDays")}`);
  const pct = (v) => (v == null ? null : Number(v) * 100);
  state.btChart.setOption({
    animationDuration: 480,
    textStyle: { fontFamily: "inherit" },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "shadow" },
      backgroundColor: "rgba(255,255,255,.97)",
      borderColor: C.border, borderWidth: 1, padding: [9, 12],
      textStyle: { color: C.text, fontSize: 12 },
      valueFormatter: (v) => (v == null ? "--" : `${Number(v) > 0 ? "+" : ""}${Number(v).toFixed(2)}%`),
    },
    legend: {
      top: 0, right: 4, itemWidth: 14, itemHeight: 8, itemGap: 16,
      textStyle: { color: C.text2, fontSize: 11 },
    },
    grid: { left: 56, right: 22, top: 34, bottom: 34 },
    xAxis: {
      type: "category", data: cats,
      axisLine: { lineStyle: { color: C.border } },
      axisTick: { show: false },
      axisLabel: { fontSize: 11, color: C.axis },
    },
    yAxis: {
      type: "value", scale: true,
      splitLine: { lineStyle: { color: C.grid } },
      axisLine: { show: false }, axisTick: { show: false },
      axisLabel: { fontSize: 10, color: C.axis, formatter: (v) => `${v.toFixed(1)}%` },
    },
    series: [
      {
        name: res.resonance_label || t("rsLegendResonance"),
        type: "bar", barMaxWidth: 34, barGap: "18%",
        data: stats.map((s) => pct(s.resonance.avg_return)),
        itemStyle: { color: RS_COLOR.resonance, borderRadius: [3, 3, 0, 0] },
      },
      {
        name: t("rsLegendDaily"),
        type: "bar", barMaxWidth: 34,
        data: stats.map((s) => pct(s.daily_high.avg_return)),
        itemStyle: { color: RS_COLOR.daily, borderRadius: [3, 3, 0, 0] },
      },
    ],
  }, true);
  state.btChart.resize();
}

/* =========================================================================
   v1.7.0 研究深度分析（四项）
   -------------------------------------------------------------------------
   分行业回测 / 统计显著性 / 参数敏感性 / 多周期共振，全部为对已有历史回测
   批次的二次统计，一次拉取后缓存在 state，切周期与切语言都只做本地重绘。
   四个接口互相独立：任一失败只影响对应板块，其余照常渲染。
   ========================================================================= */
const GRP_COLOR = { high: "#c4574e", mid: "#d99a3d", low: "#8c93a3" };
const IND_BAR_POS = "#c4574e";   // A 股惯例：正收益红
const IND_BAR_NEG = "#3f9c78";   // 负收益绿

async function loadAnalysis() {
  const jobs = [
    ["industry", "/api/analysis/industry"],
    ["significance", "/api/analysis/significance"],
    ["resonance", "/api/analysis/resonance"],
    ["sensitivity", "/api/analysis/sensitivity"],
  ];
  await Promise.all(jobs.map(async ([key, url]) => {
    try {
      state[key] = await get(url);
    } catch {
      state[key] = null;
    }
  }));
  renderIndustryBt();
  renderSignificance();
  renderRobustness();
  renderSensitivity();
  // 共振数据到达后，若当前正处在共振口径需重绘回测板块
  if (state.bt && state.btMode === "resonance") renderBacktest();
}

/* ---------------- 分行业回测 ---------------- */
function indRowsOf(d, industry, h) {
  return ((d.by_industry || {})[industry] || {})[String(h)] || {};
}

function renderIndustryBt() {
  const d = state.industry;
  const sel = $("indSel");
  const ok = d && d.available;
  $("indBtEmpty").hidden = !!ok;
  if (!ok) {
    $("indBtBody").innerHTML = "";
    $("indBtMeta").innerHTML = "";
    $("indRankPair").innerHTML = "";
    $("indBtFoot").innerHTML = "";
    sel.innerHTML = "";
    if (state.indChart) { state.indChart.dispose(); state.indChart = null; }
    $("indBtChart").innerHTML = "";
    $("indBtEmpty").textContent = (d && d.reason) ? d.reason : t("indBtEmpty");
    return;
  }

  const hs = (d.horizons || []).map(Number);
  if (!hs.includes(state.indHorizon)) state.indHorizon = hs.includes(20) ? 20 : hs[0];
  for (const b of document.querySelectorAll("#indHorizon .seg-opt")) {
    b.classList.toggle("is-on", Number(b.dataset.h) === state.indHorizon);
  }

  // 行业下拉：有高匹配分组样本的行业优先，其余按名称排列
  const ranked = (d.ranking || []).map((x) => x.industry);
  const rest = (d.universe || []).filter((x) => !ranked.includes(x));
  const order = ranked.concat(rest);
  if (!order.includes(state.indIndustry)) state.indIndustry = order[0] || null;
  sel.innerHTML = order.map((name) => {
    const mark = ranked.includes(name) ? "" : `　·　${t("indSelNoHigh")}`;
    return `<option value="${esc(name)}">${esc(name)}${esc(mark)}</option>`;
  }).join("");
  sel.value = state.indIndustry || "";

  // 概况条
  const c = d.concentration || {};
  $("indBtMeta").innerHTML = [
    ["indMetaCovered", `${d.covered_count || 0} / ${d.industry_count || 0}`],
    ["indMetaRanked", `${(d.ranking || []).length}`],
    ["indMetaRankH", `${d.rank_horizon || "--"} ${t("btUnitDays")}`],
    ["indMetaMin", `${d.min_samples || "--"}`],
    ["indMetaTop3", `${((c.top3_share || 0) * 100).toFixed(1)}%`],
  ].map(([k, v]) => `<span>${esc(t(k))}　<b>${esc(v)}</b></span>`).join("");

  renderIndustryTable();
  drawIndustryBtChart();
  renderIndustryRank();

  $("indBtFoot").textContent = t("indBtFoot", {
    n: d.min_samples || 0,
    industries: (c.top_industries || []).join("、") || "—",
    share: ((c.top3_share || 0) * 100).toFixed(1),
  });
}

function renderIndustryTable() {
  const d = state.industry;
  const cells = indRowsOf(d, state.indIndustry, state.indHorizon);
  const maxAbs = Math.max(
    1e-9,
    ...["high", "mid", "low"].map((g) => Math.abs(Number((cells[g] || {}).avg_return) || 0)),
  );
  $("indBtBody").innerHTML = BT_ORDER.map((g) => {
    const r = cells[g];
    const color = GRP_COLOR[g] || C.flat;
    if (!r) {
      return `<tr><td><span class="grp-dot" style="background:${color}"></span>${esc(btGroupLabel(g))}</td>
        <td class="num">—</td><td class="num">—</td><td class="num">—</td><td class="num">—</td>
        <td class="num">${esc(t("indNoData"))}</td></tr>`;
    }
    const v = Number(r.avg_return) || 0;
    const w = Math.max(2, Math.round((Math.abs(v) / maxAbs) * 100));
    const bar = `<span class="ind-bar-wrap"><span class="ind-bar" style="width:${w}%;background:${
      v >= 0 ? IND_BAR_POS : IND_BAR_NEG}"></span></span>`;
    return `<tr>
      <td><span class="grp-dot" style="background:${color}"></span>${esc(btGroupLabel(g))}</td>
      <td class="num">${(r.samples || 0).toLocaleString()}</td>
      <td class="num">${r.win_rate == null ? "—" : (Number(r.win_rate) * 100).toFixed(1) + "%"}</td>
      <td class="num ${pctCls(r.avg_return)}">${btPct(r.avg_return)}</td>
      <td class="num">${r.max_drawdown == null ? "—" : (Number(r.max_drawdown) * 100).toFixed(2) + "%"}</td>
      <td class="num">${bar}</td>
    </tr>`;
  }).join("");
}

function drawIndustryBtChart() {
  const el = $("indBtChart");
  const d = state.industry;
  if (!d || !d.available) return;
  const h = String(state.indHorizon);
  const pts = (d.universe || [])
    .map((name) => {
      const cell = ((d.by_industry || {})[name] || {})[h] || {};
      const hi = cell.high;
      return hi && hi.avg_return != null
        ? { name, v: Number(hi.avg_return) * 100, n: hi.samples }
        : null;
    })
    .filter(Boolean)
    .sort((a, b) => b.v - a.v);

  if (!pts.length) {
    if (state.indChart) { state.indChart.dispose(); state.indChart = null; }
    el.innerHTML = `<div class="empty">${esc(t("indNoHighData"))}</div>`;
    return;
  }
  if (el.querySelector(".empty")) el.innerHTML = "";
  if (!state.indChart) state.indChart = echarts.init(el);

  state.indChart.setOption({
    animationDuration: 480,
    textStyle: { fontFamily: "inherit" },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "shadow" },
      backgroundColor: "rgba(255,255,255,.97)",
      borderColor: C.border, borderWidth: 1, padding: [9, 12],
      textStyle: { color: C.text, fontSize: 12 },
      formatter: (ps) => {
        const p = ps[0];
        const n = pts[p.dataIndex].n;
        return `${esc(p.name)}<br/>${esc(t("btThAvg"))}：<b>${
          p.value > 0 ? "+" : ""}${Number(p.value).toFixed(2)}%</b><br/>${
          esc(t("btThSamples"))}：${n.toLocaleString()}`;
      },
    },
    grid: { left: 96, right: 40, top: 16, bottom: 34 },
    xAxis: {
      type: "value",
      splitLine: { lineStyle: { color: C.grid } },
      axisLine: { show: false }, axisTick: { show: false },
      axisLabel: { fontSize: 10, color: C.axis, formatter: (v) => `${v.toFixed(0)}%` },
    },
    yAxis: {
      type: "category", inverse: true,
      data: pts.map((p) => I18N.industry(p.name) || p.name),
      axisLine: { lineStyle: { color: C.border } },
      axisTick: { show: false },
      axisLabel: { fontSize: 11, color: C.text2 },
    },
    series: [{
      type: "bar", barMaxWidth: 16,
      data: pts.map((p) => ({
        value: p.v,
        itemStyle: { color: p.v >= 0 ? IND_BAR_POS : IND_BAR_NEG, borderRadius: [0, 3, 3, 0] },
      })),
      label: {
        show: true, position: "right", fontSize: 10, color: C.muted,
        formatter: (p) => `${p.value > 0 ? "+" : ""}${Number(p.value).toFixed(2)}%`,
      },
    }],
  }, true);
  state.indChart.resize();
}

function renderIndustryRank() {
  const d = state.industry;
  if (!d || !d.available) return;
  const card = (title, rows, cls) => {
    if (!rows || !rows.length) return "";
    return `<div class="rank-card">
      <div class="rank-head ${cls}">${esc(title)}</div>
      <table class="rank-tbl"><tbody>${rows.map((r) => `<tr>
        <td class="rank-name">${esc(I18N.industry(r.industry) || r.industry)}</td>
        <td class="num">${(r.samples || 0).toLocaleString()}</td>
        <td class="num">${r.win_rate == null ? "—" : (Number(r.win_rate) * 100).toFixed(1) + "%"}</td>
        <td class="num ${pctCls(r.avg_return)}">${btPct(r.avg_return)}</td>
        <td class="num">${r.max_drawdown == null ? "—" : (Number(r.max_drawdown) * 100).toFixed(1) + "%"}</td>
      </tr>`).join("")}</tbody></table>
    </div>`;
  };
  const head = `<table class="rank-tbl rank-tbl-head"><tbody><tr>
    <td class="rank-name">${esc(t("indRankIndustry"))}</td>
    <td class="num">${esc(t("btThSamples"))}</td>
    <td class="num">${esc(t("btThWin"))}</td>
    <td class="num">${esc(t("btThAvg"))}</td>
    <td class="num">${esc(t("btThMdd"))}</td></tr></tbody></table>`;
  const body = card(t("indRankTop"), d.top, "up") + card(t("indRankBottom"), d.bottom, "down");
  $("indRankPair").innerHTML = body
    ? `<h3 class="sub-h3">${esc(t("indRankTitle", { h: d.rank_horizon || "--" }))}</h3>`
      + `<div class="rank-grid"><div class="rank-col">${head}${card(t("indRankTop"), d.top, "up")}</div>`
      + `<div class="rank-col">${head}${card(t("indRankBottom"), d.bottom, "down")}</div></div>`
    : "";
}

/* ---------------- 统计显著性（t 检验） ---------------- */
function renderSignificance() {
  const d = state.significance;
  const ok = d && d.available;
  $("sigEmpty").hidden = !!ok;
  const body = $("sigBody");
  if (!ok) {
    body.innerHTML = "";
    $("sigFoot").textContent = (d && d.reason) ? d.reason : t("sigEmpty");
    return;
  }
  body.innerHTML = (d.tests || []).map((x) => `<tr>
    <td>${esc(String(x.horizon))} ${esc(t("btUnitDays"))}</td>
    <td class="num ${pctCls(x.mean1)}">${btPct(x.mean1)}</td>
    <td class="num ${pctCls(x.mean2)}">${btPct(x.mean2)}</td>
    <td class="num ${pctCls(x.mean_diff)}"><b>${btPct(x.mean_diff)}</b></td>
    <td class="num">${fmt(x.t, 2)}</td>
    <td class="num">${esc(x.p_text || fmt(x.p_value, 4))}</td>
    <td class="num">[${btPct(x.ci_low)}, ${btPct(x.ci_high)}]</td>
  </tr>`).join("");
  const anysig = (d.tests || []).some((x) => x.significant);
  $("sigFoot").textContent = t("sigFoot", {
    alpha: d.alpha,
    verdict: anysig ? t("sigFootYes") : t("sigFootNo"),
  });
}

/* ---------------- 分年度稳健性 ---------------- */
function renderRobustness() {
  const d = state.significance;
  const rob = d && d.robustness;
  const ok = !!(rob && rob.available);
  $("robEmpty").hidden = !!ok;
  const body = $("robBody");
  if (!ok) {
    body.innerHTML = "";
    $("robFoot").textContent = "";
    return;
  }
  const years = rob.years || [];
  const rows = [];
  years.forEach((y, yi) => {
    const list = (rob.by_year || {})[y] || [];
    list.forEach((r, ri) => {
      const cells = [
        `<td>${esc(y)}</td>`,
        `<td class="num">${esc(String(r.horizon))} ${esc(t("btUnitDays"))}</td>`,
        `<td class="num">${(r.n_high || 0).toLocaleString()}</td>`,
        `<td class="num ${pctCls(r.ret_high)}">${btPct(r.ret_high)}</td>`,
        `<td class="num ${pctCls(r.ret_low)}">${btPct(r.ret_low)}</td>`,
        `<td class="num ${pctCls(r.ret_diff)}"><b>${btPct(r.ret_diff)}</b></td>`,
        `<td class="num ${pctCls(r.win_diff)}">${btPct(r.win_diff)}</td>`,
        `<td class="num">${r.enough
            ? (r.consistent ? `<span class="tag-ok">${esc(t("robConsistent"))}</span>`
                            : `<span class="tag-bad">${esc(t("robInconsistent"))}</span>`)
            : `<span class="tag-mute">${esc(t("robNotEnough"))}</span>`}</td>`,
        `<td class="num">${(r.n_high || 0) + (r.n_low || 0) < (rob.min_samples || 0)
            ? "—" : `<span class="tag-ok">${ICON.check(11)}</span>`}</td>`,
      ];
      rows.push(`<tr>${cells.join("")}</tr>`);
    });
  });
  body.innerHTML = rows.join("");

  const per = rob.per_horizon || {};
  const parts = Object.keys(per).sort((a, b) => Number(a) - Number(b)).map((h) => {
    const x = per[h] || {};
    return `${h} ${t("btUnitDays")} ${x.consistent_years}/${x.valid_years}`;
  });
  $("robFoot").textContent = t("robFoot", {
    min: rob.min_samples || 0,
    detail: parts.join("　·　") || "—",
  });
}

/* ---------------- 参数敏感性 ---------------- */
function renderSensitivity() {
  const d = state.sensitivity;
  const ok = d && d.available;
  $("sensEmpty").hidden = !!ok;
  const body = $("sensBody");
  if (!ok) {
    body.innerHTML = "";
    $("sensFoot").textContent = (d && d.reason) ? d.reason : t("sensEmpty");
    return;
  }
  const h = (d.horizons || []).includes(state.btHorizon) ? state.btHorizon
    : (d.horizons || [])[(d.horizons || []).length - 1];
  const rows = [];
  for (const g of d.groups || []) {
    const variants = g.variants || [];
    const span = variants.length + 1;
    // 基准行
    const baseStat = ((d.base_stats || {})[String(h)] || {}).high || {};
    const drawRow = (first, label, stat, deltaPct, dirOk, stableCell) => {
      const cells = [];
      if (first) cells.push(
        `<td rowspan="${span}" class="sens-param"><b>${esc(g.label)}</b><br>`
        + `<span class="sens-base">${esc(t("sensBaseLabel"))} ${esc(g.base_label || "")}</span></td>`);
      cells.push(
        `<td>${esc(label)}</td>`,
        `<td class="num">${stat.win_rate == null ? "—" : (Number(stat.win_rate) * 100).toFixed(1) + "%"}</td>`,
        `<td class="num ${pctCls(stat.avg_return)}">${btPct(stat.avg_return)}</td>`,
        `<td class="num ${deltaPct == null ? "" : pctCls(deltaPct)}">${
          deltaPct == null ? "—" : `${deltaPct > 0 ? "+" : ""}${(deltaPct * 100).toFixed(3)} pp`}</td>`,
        `<td class="num">${dirOk == null ? "—" : (dirOk
          ? `<span class="tag-ok">${esc(t("sensDirKept"))}</span>`
          : `<span class="tag-bad">${esc(t("sensDirLost"))}</span>`)}</td>`,
        `<td class="num">${stableCell}</td>`,
      );
      rows.push(`<tr>${cells.join("")}</tr>`);
    };
    drawRow(true, t("sensBaseRow"), baseStat, null, null,
      `<span class="tag-mute">${esc(t("sensBaseTag"))}</span>`);
    for (const v of variants) {
      const st = ((v.stats || {})[String(h)] || {}).high || {};
      const delta = v.delta && v.delta[String(h)] && v.delta[String(h)].high
        ? v.delta[String(h)].high.avg_return : null;
      const dirOk = v.direction_kept ? v.direction_kept[String(h)] : null;
      const stable = Math.abs(delta || 0) * 100 <= (d.stable_threshold_pp || 0);
      drawRow(false, v.label, st, delta, dirOk,
        stable ? `<span class="tag-ok">${esc(t("sensStable"))}</span>`
               : `<span class="tag-bad">${esc(t("sensUnstable"))}</span>`);
    }
  }
  body.innerHTML = rows.join("");

  const c = d.conclusion || {};
  $("sensFoot").textContent = t("sensFoot", {
    h,
    n: (d.groups || []).reduce((a, g) => a + (g.variants || []).length, 0),
    dir: c.direction_all_kept ? t("sensFootDirOk") : t("sensFootDirNo"),
    mag: c.magnitude_stable_all ? t("sensFootMagOk") : t("sensFootMagNo"),
    worst: c.worst_group || "—",
    pp: fmt(c.worst_delta_pp, 2),
    threshold: d.stable_threshold_pp,
  });
}

/* =========================================================================
   研究简报（v1.6.0）
   - 预览：新标签页打开 /api/report/html（自包含 HTML，可直接打印）
   - 导出：/api/report/export 返回 PDF，前端以 blob 方式下载并提示状态
   ========================================================================= */
const REP_OUTLINE_KEYS = [
  "repOutline1", "repOutline2", "repOutline3", "repOutline4", "repOutline5",
  "repOutline6", "repOutline7", "repOutline8", "repOutline9",
];

function renderReportOutline() {
  $("repOutline").innerHTML = REP_OUTLINE_KEYS
    .map((k, i) => `<li><span class="rep-n">${String(i + 1).padStart(2, "0")}</span>`
      + `<span>${esc(t(k))}</span></li>`).join("");
}

function previewReport() {
  const url = state.runId ? `/api/report/html?scan_run_id=${state.runId}` : "/api/report/html";
  const w = window.open(url, "_blank");
  if (!w) window.alert(t("repPreviewFail"));
}

async function exportReportPdf() {
  const btn = $("btnRepPdf");
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = t("repExporting");
  try {
    const r = await fetch("/api/report/export", { cache: "no-store" });
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      throw new Error(body.detail || `HTTP ${r.status}`);
    }
    const blob = await r.blob();
    const a = document.createElement("a");
    const url = URL.createObjectURL(blob);
    a.href = url;
    a.download = `research-note-${new Date().toISOString().slice(0, 10)}.pdf`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    state.repMeta = new Date().toLocaleString();
    renderReportMeta();
  } catch (e) {
    window.alert(`${t("repExportFail")}：${e.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

function renderReportMeta() {
  const el = $("repMeta");
  const ts = state.repMeta || new Date().toLocaleString();
  el.textContent = `${t("repMeta")}：${ts}　|　${t("repFootNote")}`;
}

/* 报告在线预览（v1.8.0）：把简报全文内嵌到「研究简报」页。
   - 惰性加载：只有真正进入该页才请求简报全文，不拖慢首屏；
   - 批次联动：扫描批次变化后自动重载，避免看到旧批次的简报；
   - 简报由服务端生成为中文版式，语言切换不改变其内容。 */
function loadReportFrame(force) {
  const frame = $("repFrame");
  const msg = $("repFrameMsg");
  if (!frame) return;
  // 批次快照用字符串表示：null（尚未取到批次）与真实批次都能区分开
  const key = state.runId == null ? "" : String(state.runId);
  if (!force && state.repFrameLoaded && state.repFrameKey === key) return;
  state.repFrameLoaded = true;
  state.repFrameKey = key;

  msg.hidden = false;
  msg.textContent = t("repFrameLoading");
  const base = state.runId
    ? `/api/report/html?scan_run_id=${state.runId}`
    : "/api/report/html";
  const sep = base.indexOf("?") >= 0 ? "&" : "?";
  frame.onload = () => { msg.hidden = true; };
  frame.onerror = () => { msg.textContent = t("repFrameFail"); };
  // 时间戳绕开浏览器对内嵌文档的缓存
  frame.src = `${base}${sep}_t=${Date.now()}`;
}

function bindBacktestAndReport() {
  // 对比口径切换：分档绩效（三组） / 共振对比（双共振 vs 单日线）
  for (const b of document.querySelectorAll("#btMode .seg-opt")) {
    b.addEventListener("click", () => {
      state.btMode = b.dataset.mode || "tier";
      for (const x of document.querySelectorAll("#btMode .seg-opt")) {
        x.classList.toggle("is-on", x === b);
      }
      renderBacktest();
    });
  }
  for (const b of document.querySelectorAll("#btHorizon .seg-opt")) {
    b.addEventListener("click", () => {
      state.btHorizon = Number(b.dataset.h);
      for (const x of document.querySelectorAll("#btHorizon .seg-opt")) {
        x.classList.toggle("is-on", x === b);
      }
      renderBacktestTable();
      if (state.btMode === "resonance") renderResonanceTable();
      drawBacktestChart();
    });
  }
  $("btnBtCsv").addEventListener("click", () => {
    const p = new URLSearchParams();
    if (state.bt && state.bt.run_id) p.set("run_id", state.bt.run_id);
    window.open(`/api/backtest/trades.csv?${p}`, "_blank");
  });

  // --- v1.7.0 分行业回测板块 ---
  $("indSel").addEventListener("change", () => {
    state.indIndustry = $("indSel").value || null;
    renderIndustryTable();
  });
  for (const b of document.querySelectorAll("#indHorizon .seg-opt")) {
    b.addEventListener("click", () => {
      state.indHorizon = Number(b.dataset.h);
      for (const x of document.querySelectorAll("#indHorizon .seg-opt")) {
        x.classList.toggle("is-on", x === b);
      }
      renderIndustryTable();
      drawIndustryBtChart();
    });
  }
  $("btnIndCsv").addEventListener("click", () => {
    const p = new URLSearchParams();
    if (state.industry && state.industry.run_id) p.set("run_id", state.industry.run_id);
    window.open(`/api/analysis/industry.csv?${p}`, "_blank");
  });

  $("btnRepPreview").addEventListener("click", previewReport);
  $("btnRepPdf").addEventListener("click", exportReportPdf);
}

/* ---------------- 请求封装 ---------------- */
/* GET 自动重试：网络抖动或扫描期间数据库短暂繁忙（5xx）时重试一次，
   404 等确定性错误不重试，直接抛出。 */
async function get(url, retries = 1) {
  for (let attempt = 0; ; attempt++) {
    let r;
    try {
      r = await fetch(url, { cache: "no-store" });
    } catch (e) {
      if (attempt >= retries) throw e;
      await new Promise((res) => setTimeout(res, 600));
      continue;
    }
    if (r.ok) return r.json();
    if (r.status >= 500 && attempt < retries) {
      await new Promise((res) => setTimeout(res, 600));
      continue;
    }
    const body = await r.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${r.status}`);
  }
}

async function post(url) {
  const r = await fetch(url, { method: "POST" });
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${r.status}`);
  }
  return r.json();
}
