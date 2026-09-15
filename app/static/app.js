/* =========================================================================
   A股历史形态匹配研究看板 · 前端逻辑（本地运行版）
   数据来源：本地 FastAPI 接口（/api/*），全部为历史统计与指标展示。
   分数含义：与历史上升段启动样本的特征相似程度，不代表未来表现。
   版本：v1.4.0（视觉体系收敛 + 市场资讯模块）
   ========================================================================= */

"use strict";

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
  items: [],
  total: 0,
  offset: 0,
  limit: 200,
  industries: [],
  hotIndustries: [],
  chart: null,        // 行业分布图实例
  detailChart: null,  // 个股明细图实例（与容器一一对应，永不销毁）
  detailToken: 0,     // 详情请求令牌：防止快速切换个股时的响应乱序
  polling: null,
  news: [],           // 市场资讯条目（一次拉取，前端按分类切换）
  newsCat: "all",
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
  syncStickyOffset();
  refreshAll();
});

function bindEvents() {
  $("btnScan").addEventListener("click", () => {
    $("scanPanel").hidden = false;
    $("scanPanel").scrollIntoView({ behavior: "smooth", block: "nearest" });
  });
  $("btnScanClose").addEventListener("click", () => { $("scanPanel").hidden = true; });
  $("btnScanStart").addEventListener("click", startScan);
  $("btnExport").addEventListener("click", exportCsv);

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
      for (const t of document.querySelectorAll(".news-tab")) {
        t.classList.toggle("is-on", t === tab);
      }
      renderNews();
    });
  }

  window.addEventListener("resize", debounce(() => {
    syncStickyOffset();
    resizeCharts();
  }, 200));
}

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

/* 表头吸顶偏移 = 顶部导航实际高度（品牌区 + 免责声明通栏） */
function syncStickyOffset() {
  const bar = $("topbar");
  if (!bar) return;
  const h = Math.round(bar.getBoundingClientRect().height);
  document.documentElement.style.setProperty("--sticky-top", `${h}px`);
}

async function refreshAll() {
  // 宏观面板行情源与资讯接口都可能较慢，一律独立加载、不阻塞主内容：
  // 先渲染占位保证版面立即稳定，数据到达后原地替换。
  loadIndices();
  loadNews();
  await Promise.all([loadStatus(), loadFilters()]);
  await loadPool();
  renderIndustryChart();
}

/* =========================================================================
   宏观参考面板
   第一行：国内 4 大指数；第二行：海外隔夜与港股 3 个指数。
   两行都按固定顺序渲染固定数量卡片，缺数据时补占位卡，
   保证网格始终等宽、左右边缘严格对齐、不会参差不齐。
   ========================================================================= */
const DOMESTIC_ORDER = ["上证指数", "深证成指", "创业板指", "科创50"];
const OVERSEAS_ORDER = ["纳斯达克", "标普500", "恒生指数"];
const OVERSEAS_TAG = { "纳斯达克": "隔夜", "标普500": "隔夜", "恒生指数": "当日" };

function pickByOrder(items, order) {
  const map = new Map((items || []).map((x) => [x.name, x]));
  return order.map((name) => map.get(name) || { name, close: null, change_pct: null });
}

function macroCard(x, tag) {
  const cls = pctCls(x.change_pct);
  return `
    <div class="macro-card">
      <div class="m-name">
        <span>${esc(x.name)}</span>
        ${tag ? `<span class="m-tag">${tag}</span>` : ""}
      </div>
      <div>
        <div class="m-value ${cls}">${fmt(x.close)}</div>
        <div class="m-change ${cls}">${chgText(x.change_pct)}</div>
      </div>
    </div>`;
}

async function loadIndices(attempt = 0) {
  const dom = $("macroDomestic");
  const ovs = $("macroOverseas");
  // 先铺占位卡：行情源慢时版面也不会出现空洞或跳动
  dom.innerHTML = DOMESTIC_ORDER.map((n) => macroCard({ name: n })).join("");
  ovs.innerHTML = OVERSEAS_ORDER.map((n) => macroCard({ name: n }, OVERSEAS_TAG[n])).join("");
  $("macroUpdated").textContent = "行情加载中…";

  let data = null;
  try {
    data = await get("/api/market/indices", 0);
  } catch { /* 数据源不可用时保留占位卡，保持版面稳定 */ }
  if (!data || !(data.items || []).length) {
    $("macroUpdated").textContent = "行情源暂不可用";
    // 行情源首发较慢时补一次重试，避免整页只有宏观面板空着
    if (attempt < 2) setTimeout(() => loadIndices(attempt + 1), 6000);
    return;
  }
  dom.innerHTML = pickByOrder(data.items, DOMESTIC_ORDER).map((x) => macroCard(x)).join("");
  ovs.innerHTML = pickByOrder(data.overseas, OVERSEAS_ORDER)
    .map((x) => macroCard(x, OVERSEAS_TAG[x.name])).join("");
  $("macroUpdated").textContent = data.cached_at
    ? `本地缓存快照 · ${String(data.cached_at).slice(5, 16).replace("T", " ")}`
    : "实时行情快照";
}

/* =========================================================================
   扫描状态与统计概览
   ========================================================================= */
async function loadStatus() {
  try {
    const s = await get("/api/scan/status");
    const run = s.latest_run;
    state.runInfo = run || null;
    state.runId = run ? run.id : null;

    const pill = $("runState");
    if (s.running) {
      pill.textContent = "扫描进行中…";
      pill.className = "pill pill-run";
    } else if (run && run.status === "success") {
      pill.textContent = `最新批次 #${run.id} · ${(run.finished_at || "").slice(5, 16).replace("T", " ")}`;
      pill.className = "pill pill-ok";
    } else {
      pill.textContent = "尚未执行扫描";
      pill.className = "pill pill-muted";
    }

    const stats = [
      { num: run ? run.candidates : "--", label: "总候选数", tier: "tier-total",
        sub: "当前批次入选样本" },
      { num: run ? run.high_count : "--", label: "高匹配分（≥60）", tier: "tier-high",
        sub: "与历史样本特征高度相似" },
      { num: run ? run.mid_count : "--", label: "中匹配分（30–59）", tier: "tier-mid",
        sub: "与历史样本特征中度相似" },
      { num: run ? run.total_scanned : "--", label: "预筛后处理只数", tier: "tier-processed",
        sub: "通过基础过滤的样本" },
    ];
    $("statStrip").innerHTML = stats.map((x) => `
      <div class="stat-card ${x.tier}">
        <div class="num">${x.num}</div>
        <div class="label">${x.label}</div>
        <div class="sub">${x.sub}</div>
      </div>`).join("");
  } catch {
    $("statStrip").innerHTML = "";
  }
}

/* =========================================================================
   筛选项
   ========================================================================= */
async function loadFilters() {
  try {
    const { runId } = state;
    const ind = await get(`/api/pool/industries${runId ? `?run_id=${runId}` : ""}`);
    state.industries = ind.industries || [];
    const sel = $("fIndustry");
    const cur = sel.value;
    sel.innerHTML =
      '<option value="">全部行业</option>' +
      state.industries.map((x) => `<option value="${esc(x)}">${esc(x)}</option>`).join("");
    if ([...sel.options].some((o) => o.value === cur)) sel.value = cur;

    const boards = ["沪主板", "深主板", "创业板", "科创板", "其他"];
    $("fBoard").innerHTML =
      '<option value="">全部板块</option>' +
      boards.map((x) => `<option value="${x}">${x}</option>`).join("");
  } catch { /* 首次扫描前无行业数据，忽略 */ }
}

/* =========================================================================
   候选池表格
   ========================================================================= */
/* 指标命中项：表格标签与抽屉明细共用同一份定义，保证口径一致 */
const HIT_TAGS = [
  { label: "MACD", title: "MACD(3,6,3) 金叉且红柱 > 0（+15）", get: (b) => b.core && b.core.macd_gold_red },
  { label: "KDJ", title: "KDJ(9,3,3) 金叉且 J < 100（+12）", get: (b) => b.core && b.core.kdj_gold_j_under_100 },
  { label: "量1.3", title: "成交量 > 1.3 倍近5日均量（+8）", get: (b) => b.core && b.core.volume_surge_1_3x },
  { label: "量2倍", title: "成交量 > 2 倍近5日均量（叠加 +5）", get: (b) => b.core && b.core.volume_surge_2x },
  { label: "换手", title: "当日换手率处于 3% – 15%（+5）", get: (b) => b.core && b.core.turnover_healthy_3_15 },
  { label: "横盘", title: "近40个交易日振幅 ≤ 1.8（+5）", get: (b) => b.core && b.core.range_compact_40d },
  { label: "筹码低", title: "筹码集中度 ≤ 18%（+3）", get: (b) => b.fund && b.fund.chip_concentrated_le_18 },
  { label: "筹码松", title: "筹码集中度 > 20%（加至满分 8）", get: (b) => b.fund && b.fund.chip_loose_gt_20 },
  { label: "PE分位", title: "PE 低于所属行业 30% 分位（+6）/ 30%–70% 分位（+3）",
    get: (b) => b.fund && (b.fund.pe_tier === "low" || b.fund.pe_tier === "mid") },
  { label: "5日涨", title: "近5日涨幅处于 5% – 20%（+6）", get: (b) => b.fund && b.fund.return5_healthy_5_20 },
  { label: "创科", title: "创业板 / 科创板（+8）", get: (b) => b.industry && b.industry.growth_board },
  { label: "热点", title: "所属行业命中热点名单（+22）", get: (b) => b.industry && b.industry.hot_industry },
];

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
    $("poolEmpty").textContent = "候选池加载失败 —— 请确认本地服务已启动";
    $("poolEmpty").style.display = "block";
    $("poolBody").innerHTML = "";
  }
}

function renderTable() {
  const kw = $("fSearch").value.trim().toLowerCase();
  const rows = state.items.filter(
    (x) => !kw || x.code.includes(kw) || (x.name || "").toLowerCase().includes(kw)
  );

  $("poolEmpty").style.display = rows.length ? "none" : "block";
  $("poolEmpty").textContent = state.items.length
    ? "没有符合搜索条件的记录"
    : "暂无数据 —— 请先执行全市场扫描";

  $("poolBody").innerHTML = rows.map((x) => {
    const t = x.total_score ?? 0;
    const bd = x.breakdown || {};
    const d = x.display || {};
    const m = x.metrics || {};
    const tags = HIT_TAGS.map((def) => {
      const on = !!def.get(bd);
      return `<span class="tag ${on ? "on" : ""}" title="${esc(def.title)}">${def.label}</span>`;
    }).join("");
    const shrink = d.macd_hist_shrink
      ? '<span class="up" title="历史见顶相关指标，仅供研究，不参与打分">是</span>'
      : '<span class="flat" title="历史见顶相关指标，仅供研究，不参与打分">否</span>';
    return `
      <tr data-code="${x.code}">
        <td class="code-cell">${x.code}</td>
        <td class="name-cell" title="${esc(x.name)}">${esc(x.name)}</td>
        <td>${esc(x.board) || "--"}</td>
        <td title="${esc(x.industry || "")}">${esc(x.industry) || "--"}</td>
        <td class="num">
          <div class="score-cell">
            <div class="score-bar"><i style="width:${t}%"></i></div>
            <span class="score-num">${t}</span>
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
    `共 ${state.total} 条 · 当前展示 ${rows.length} 条 · 计分口径 v1.1（满分 100）`;
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
  $("scanLog").textContent = "正在启动扫描…";
  try {
    await post(`/api/scan/run?${qs}`);
    pollScan();
  } catch (e) {
    $("scanLog").textContent = `启动失败：${e.message}`;
    $("btnScanStart").disabled = false;
  }
}

function pollScan() {
  const log = $("scanLog");
  if (state.polling) clearInterval(state.polling);
  state.polling = setInterval(async () => {
    try {
      const s = await get("/api/scan/status");
      log.textContent = (s.progress || []).join("\n") || "扫描准备中…";
      log.scrollTop = log.scrollHeight;
      if (!s.running) {
        clearInterval(state.polling);
        state.polling = null;
        $("btnScanStart").disabled = false;
        const run = s.latest_run;
        if (run) {
          log.textContent +=
            `\n\n✔ 批次 #${run.id} 完成：候选 ${run.candidates}，高匹配分 ${run.high_count}，中匹配分 ${run.mid_count}`;
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
   ========================================================================= */
function isHotIndustry(name) {
  return state.hotIndustries.some(
    (h) => name === h || name.startsWith(h) || h.startsWith(name)
  );
}

async function renderIndustryChart() {
  const el = $("industryChart");
  // 统一的空态渲染：先销毁实例再清空容器，避免实例指向被移除的节点
  const showEmpty = (msg) => {
    if (state.chart) { state.chart.dispose(); state.chart = null; }
    el.innerHTML = `<div class="empty">${msg}</div>`;
  };
  try {
    const params = state.runId ? `?run_id=${state.runId}` : "";
    const data = await get(`/api/market/industry-stats${params}`);
    state.hotIndustries = data.hot_industries || [];
    const all = data.items || [];
    const total = data.total || all.reduce((s, x) => s + x.count, 0) || 1;
    const named = all.filter((x) => x.industry !== "其他").slice(0, 16);
    const other = all.find((x) => x.industry === "其他");
    const items = other && other.count > 0 ? named.concat([other]) : named;

    $("indCoverage").textContent =
      `覆盖 ${all.length - (other ? 1 : 0)} 个行业 · 样本 ${total} 只 · 「其他」占比 ${(data.other_pct ?? 0).toFixed(1)}%`;

    if (!items.length) {
      showEmpty("暂无数据 —— 请先执行全市场扫描");
      return;
    }
    const labels = items.map((x) => x.industry).reverse();
    const counts = items.map((x) => x.count).reverse();
    const metas = items.map((x) => x).reverse();
    // 配色与整体视觉体系一致：常态低饱和蓝渐变，热点名单命中用柔和暖色
    const colors = labels.map((n) => {
      if (n === "其他") return ["#cdd3db", "#b0b9c3"];
      return isHotIndustry(n) ? ["#e8b183", C.up] : ["#9dbfef", C.accent];
    });

    if (!state.chart) {
      el.innerHTML = "";
      state.chart = echarts.init(el);
    }
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
          const hot = isHotIndustry(p.name) ? " · 命中热点名单" : "";
          return `<b>${p.name}</b>${hot}<br/>候选数量 <b>${p.value}</b> 只 · 占比 `
            + `${(p.value / total * 100).toFixed(1)}%<br/>`
            + `<span style="color:${C.muted}">平均匹配分 ${m.avg_score ?? "--"}`
            + ` · 最高 ${m.max_score ?? "--"}</span>`;
        },
      },
      grid: { left: 110, right: 92, top: 10, bottom: 16 },
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
        axisLabel: { fontSize: 12, color: C.text2, margin: 12 },
      },
      series: [{
        name: "候选数量",
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
          formatter: (p) => `${p.value} 只 · ${(p.value / total * 100).toFixed(1)}%`,
        },
      }],
    }, true);
    state.chart.resize();
  } catch {
    showEmpty("行业分布加载失败");
  }
}

/* =========================================================================
   市场资讯
   - 一次拉取全部分类，切换标签只做前端过滤，避免重复请求第三方接口
   - 接口异常或返回空时展示「资讯暂不可用」占位，绝不阻塞主表格
   - 条目跳转第三方原文，rel="noopener noreferrer" 保证安全
   ========================================================================= */
const NEWS_CAT_TEXT = {
  domestic: "国内资讯", overseas: "海外资讯", macro: "宏观资讯",
};

/* 时间展示：`2026-09-15 21:24:41` → `09-15 21:24` */
function fmtClock(s) {
  const t = String(s || "").trim();
  if (t.length < 16) return t || "--";
  return `${t.slice(5, 10)} ${t.slice(11, 16)}`;
}

async function loadNews() {
  const list = $("newsList");
  let data = null;
  try {
    data = await get("/api/news", 0);
  } catch {
    /* 接口不可用：保持模块结构，只显示占位 */
  }
  if (!data || !data.available || !(data.items || []).length) {
    list.innerHTML = '<div class="news-empty">资讯暂不可用</div>';
    $("newsMeta").textContent = (data && data.message) || "资讯暂不可用";
    return;
  }
  state.news = data.items;
  renderNewsTabs(data.counts || {});
  $("newsMeta").textContent =
    `共 ${data.items.length} 条 · 来源 ${(data.sources || []).join(" / ") || "--"}`
    + ` · 更新 ${fmtClock(data.cached_at)}`;
  if (data.source_note) $("newsNote").textContent = data.source_note;
  renderNews();
}

/* 分类标签上的数量来自接口统计，与实际可展示条数一致 */
function renderNewsTabs(counts) {
  for (const tab of document.querySelectorAll(".news-tab")) {
    const cat = tab.dataset.cat || "all";
    const base = cat === "all" ? "全部" : NEWS_CAT_TEXT[cat];
    const n = counts[cat];
    tab.textContent = n === undefined ? base : `${base} ${n}`;
  }
}

function renderNews() {
  const list = $("newsList");
  const rows = state.newsCat === "all"
    ? state.news
    : state.news.filter((x) => x.category === state.newsCat);

  if (!rows.length) {
    list.innerHTML = '<div class="news-empty">该分类暂无资讯</div>';
    return;
  }
  list.innerHTML = rows.map((x) => {
    const cat = NEWS_CAT_TEXT[x.category] ? x.category : "domestic";
    const url = x.url || "";
    const inner = `
      <span class="news-tag t-${cat}">${NEWS_CAT_TEXT[cat]}</span>
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
   ========================================================================= */
const PE_TIER_TEXT = {
  low: "低于行业 30% 分位", mid: "行业 30% – 70% 分位",
  high: "高于行业 70% 分位", missing: "样本不足或缺失",
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
      key: "core", cls: "g-core", title: "核心形态分",
      score: bd.core_score ?? 0, max: 50,
      items: [
        { name: "MACD(3,6,3) 金叉且红柱 > 0", pts: 15, hit: !!core.macd_gold_red },
        { name: "KDJ(9,3,3) 金叉且 J < 100", pts: 12, hit: !!core.kdj_gold_j_under_100 },
        { name: "成交量 > 1.3 倍近5日均量", pts: 8, hit: !!core.volume_surge_1_3x },
        { name: "成交量 > 2 倍近5日均量（叠加项）", pts: 5, hit: !!core.volume_surge_2x },
        { name: "当日换手率处于 3% – 15%", pts: 5, hit: !!core.turnover_healthy_3_15 },
        { name: "近 40 个交易日振幅 ≤ 1.8", pts: 5, hit: !!core.range_compact_40d },
      ],
    },
    {
      key: "fund", cls: "g-fund", title: "筹码基本面分",
      score: bd.fund_score ?? 0, max: 20,
      items: [
        { name: "筹码集中度 ≤ 18%", pts: 3, hit: !!fund.chip_concentrated_le_18 },
        { name: "筹码集中度 > 20%（加至满分 8）", pts: 5, hit: !!fund.chip_loose_gt_20 },
        { name: `PE 行业分位：${PE_TIER_TEXT[tier]}`, pts: pePts, max: 6, hit: peHit },
        { name: "近5日涨幅处于 5% – 20%", pts: 6, hit: !!fund.return5_healthy_5_20 },
      ],
    },
    {
      key: "industry", cls: "g-ind", title: "行业板块分",
      score: bd.industry_score ?? 0, max: 30,
      items: [
        { name: "所属板块为创业板 / 科创板", pts: 8, hit: !!ind.growth_board },
        { name: "所属行业命中热点名单", pts: 22, hit: !!ind.hot_industry },
      ],
    },
  ];
}

async function openDrawer(code) {
  const item = state.items.find((x) => x.code === code);
  const token = ++state.detailToken;

  $("dTitle").textContent = item ? `${item.name} · ${item.code}` : code;
  $("dSub").textContent = "正在读取指标明细…";
  $("dHero").innerHTML = "";
  $("dGroups").innerHTML = "";
  setChartMsg("正在加载明细数据…", false);
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
    $("dSub").textContent = item ? `${item.board || ""} ${item.industry || ""}`.trim() : "";
    $("dChart").innerHTML = "";
    setChartMsg(`明细数据加载失败：${e.message}`, true);
    return;
  }
  if (token !== state.detailToken) return;  // 已切换到其它个股，丢弃过期响应

  const profile = detail.profile || item || {};
  const score = detail.score || item || {};
  renderDetailHeader(profile, score, detail);
  try {
    renderDetailChart(detail);
    setChartMsg("", false);
  } catch (e) {
    setChartMsg(`图表渲染失败：${e.message}`, true);
  }
  // 抽屉滑入动画结束后容器尺寸才稳定，再校正一次图表宽度
  setTimeout(() => state.detailChart && state.detailChart.resize(), 360);
}

/* 抽屉头部：档案信息 + 综合匹配分概览 + 三组得分明细 */
function renderDetailHeader(profile, score, detail) {
  const name = profile.name || detail.code || "";
  const code = profile.code || detail.code || "";
  if (name) $("dTitle").textContent = `${name} · ${code}`;

  const bd = score.breakdown || {};
  const total = score.total_score ?? bd.total_score ?? 0;
  const meta = [profile.board, profile.industry].filter(Boolean).join(" · ");
  const inPool = score.total_score !== undefined && score.total_score !== null;
  $("dSub").textContent = inPool
    ? `${meta} · 综合匹配分 ${total} / 100`
    : `${meta} · 该个股不在当前批次候选中，仅展示历史指标`;

  const groups = buildScoreGroups(bd);
  $("dHero").innerHTML = `
    <div class="hero-top">
      <span class="hero-num">${total}<small>/100</small></span>
      <span class="hero-label">综合匹配分</span>
    </div>
    <div class="hero-bar"><i style="width:${Math.max(0, Math.min(100, total))}%"></i></div>
    <div class="hero-groups">
      ${groups.map((g) => `
        <div class="hero-group ${g.cls}">
          <div class="g-name">${g.title}</div>
          <div class="g-val">${g.score}<small> / ${g.max}</small></div>
        </div>`).join("")}
    </div>`;

  $("dGroups").innerHTML = groups.map((g) => `
    <div class="group-card ${g.cls}">
      <div class="group-head">
        <h3>${g.title}</h3>
        <span class="g-subtotal">小计 ${g.score}<small> / ${g.max}</small></span>
      </div>
      <div class="item-list">
        ${g.items.map((it) => `
          <div class="item ${it.hit ? "hit" : ""}">
            <span class="dot">${it.hit ? "✓" : ""}</span>
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
    throw new Error("明细数据不完整");
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
        name: "日K（前复权）", type: "candlestick", xAxisIndex: 0, yAxisIndex: 0,
        data: candles,
        itemStyle: { color: upColor, color0: downColor, borderColor: upColor, borderColor0: downColor },
      },
      { name: "MA5", type: "line", data: ind.ma5, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { width: 1, color: C.warn } },
      { name: "MA10", type: "line", data: ind.ma10, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { width: 1, color: C.violet } },
      { name: "MA20", type: "line", data: ind.ma20, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { width: 1, color: C.accent } },
      { name: "MA60", type: "line", data: ind.ma60, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { width: 1, color: C.flat } },
      {
        name: "成交量", type: "bar", xAxisIndex: 1, yAxisIndex: 1,
        data: k.volume.map((v, i) => ({
          value: v, itemStyle: { color: k.close[i] >= k.open[i] ? upColor : downColor },
        })),
      },
      {
        name: "MACD柱", type: "bar", xAxisIndex: 2, yAxisIndex: 2,
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

function resizeCharts() {
  state.chart && state.chart.resize();
  state.detailChart && state.detailChart.resize();
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
