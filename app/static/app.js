/* =========================================================================
   A股历史形态匹配研究看板 · 前端逻辑（本地运行版）
   数据来源：本地 FastAPI 接口（/api/*），全部为历史统计与指标展示。
   分数含义：与历史上升段启动样本的特征相似程度，不代表未来表现。
   版本：v1.2.0（阶段2 布局优化 + 详情图表渲染修复）
   ========================================================================= */

"use strict";

/* ---------------- 全局状态 ---------------- */
const state = {
  runId: null,
  runInfo: null,
  items: [],
  total: 0,
  offset: 0,
  limit: 200,
  industries: [],
  chart: null,        // 行业分布图
  detailChart: null,  // 个股明细图
  polling: null,
};

const $ = (id) => document.getElementById(id);
const fmt = (v, digits = 2) =>
  v === null || v === undefined || Number.isNaN(Number(v)) ? "--" : Number(v).toFixed(digits);
const pctCls = (v) => (v > 0 ? "up" : v < 0 ? "down" : "flat");
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

/* ---------------- 涨跌配色遵循 A 股惯例：涨红跌绿 ---------------- */
function chgText(v) {
  if (v === null || v === undefined || Number.isNaN(Number(v))) return "--";
  return (v > 0 ? "+" : "") + Number(v).toFixed(2) + "%";
}

/* =========================================================================
   启动
   ========================================================================= */
document.addEventListener("DOMContentLoaded", () => {
  bindEvents();
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
  $("fSearch").addEventListener("input", debounce(() => renderTable(), 200));

  $("btnMore").addEventListener("click", () => { state.offset += state.limit; loadPool(true); });

  $("btnDrawerClose").addEventListener("click", closeDrawer);
  $("drawerMask").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeDrawer();
  });
  window.addEventListener("resize", debounce(resizeCharts, 200));
}

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

async function refreshAll() {
  await Promise.all([loadIndices(), loadStatus(), loadFilters()]);
  await loadPool();
  renderIndustryChart();
}

/* =========================================================================
   宏观参考面板
   第一行：国内 4 大指数（点位 + 涨跌幅）
   第二行：海外/港股 3 个指数（仅涨跌幅，仅做数据展示，不参与个股打分）
   ========================================================================= */
const DOMESTIC_ORDER = ["上证指数", "深证成指", "创业板指", "科创50"];
const OVERSEAS_ORDER = ["纳斯达克", "标普500", "恒生指数"];
const OVERSEAS_TAG = { "纳斯达克": "隔夜", "标普500": "隔夜", "恒生指数": "当日" };

function sortByOrder(items, order) {
  return [...items].sort(
    (a, b) => order.indexOf(a.name) - order.indexOf(b.name)
  );
}

function domesticCard(x) {
  return `
    <div class="macro-card">
      <div class="name">${esc(x.name)}</div>
      <div class="row">
        <span class="val ${pctCls(x.change_pct)}">${fmt(x.close)}</span>
        <span class="chg ${pctCls(x.change_pct)}">${chgText(x.change_pct)}</span>
      </div>
    </div>`;
}

function overseasCard(x) {
  const tag = OVERSEAS_TAG[x.name] || "隔夜";
  return `
    <div class="macro-card compact">
      <div class="name">${esc(x.name)}<span class="macro-tag">${tag}</span></div>
      <div class="row">
        <span class="val ${pctCls(x.change_pct)}">${chgText(x.change_pct)}</span>
      </div>
    </div>`;
}

function placeholderCard(name) {
  return `
    <div class="macro-card">
      <div class="name">${esc(name)}</div>
      <div class="row"><span class="val flat">--</span>
        <span class="chg flat">--</span></div>
    </div>`;
}

async function loadIndices() {
  const dom = $("macroDomestic");
  const ovs = $("macroOverseas");
  try {
    const data = await get("/api/market/indices");
    const domestic = sortByOrder(data.items || [], DOMESTIC_ORDER).slice(0, 4);
    const overseas = sortByOrder(data.overseas || [], OVERSEAS_ORDER).slice(0, 3);

    dom.innerHTML = domestic.length
      ? domestic.map(domesticCard).join("")
      : DOMESTIC_ORDER.map(placeholderCard).join("");
    ovs.innerHTML = overseas.length
      ? overseas.map(overseasCard).join("")
      : OVERSEAS_ORDER.map((n) => `
          <div class="macro-card compact">
            <div class="name">${n}<span class="macro-tag">${OVERSEAS_TAG[n] || "隔夜"}</span></div>
            <div class="row"><span class="val flat">--</span></div>
          </div>`).join("");

    $("macroUpdated").textContent = data.cached_at
      ? `本地缓存快照 · ${String(data.cached_at).slice(5, 16).replace("T", " ")}`
      : "实时行情快照";
  } catch {
    dom.innerHTML = DOMESTIC_ORDER.map(placeholderCard).join("");
    ovs.innerHTML = OVERSEAS_ORDER.map(placeholderCard).join("");
    $("macroUpdated").textContent = "行情源暂不可用";
  }
}

/* =========================================================================
   扫描状态与统计概览
   ========================================================================= */
async function loadStatus() {
  try {
    const s = await get("/api/scan/status");
    const run = s.latest_run;
    state.runInfo = run || null;
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
    state.runId = run ? run.id : null;

    const stats = [
      { num: run ? run.candidates : "--", label: "总候选数", tier: "tier-total",
        sub: "当前批次入选样本" },
      { num: run ? run.high_count : "--", label: "高匹配分（≥60）", tier: "tier-high",
        sub: "特征高度相似" },
      { num: run ? run.mid_count : "--", label: "中匹配分（30–59）", tier: "tier-mid",
        sub: "特征中度相似" },
      { num: run ? run.total_scanned : "--", label: "预筛后处理只数", tier: "tier-processed",
        sub: "通过基础过滤样本" },
    ];
    $("statStrip").innerHTML = stats
      .map(
        (x) => `
      <div class="stat-card ${x.tier}">
        <div class="num">${x.num}</div>
        <div class="label">${x.label}</div>
        <div class="sub">${x.sub}</div>
      </div>`
      )
      .join("");
  } catch {
    $("statStrip").innerHTML = "";
  }
}

/* =========================================================================
   候选池
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

  $("poolBody").innerHTML = rows
    .map((x) => {
      const t = x.total_score;
      const bd = x.breakdown || {};
      const core = bd.core || {}, fund = bd.fund || {}, ind = bd.industry || {};
      const d = x.display || {};
      const tags = [
        ["MACD", core.macd_gold_red],
        ["KDJ", core.kdj_gold_j_under_100],
        ["量1.3x", core.volume_surge_1_3x],
        ["量2x", core.volume_surge_2x],
        ["换手", core.turnover_healthy_3_15],
        ["横盘", core.range_compact_40d],
        ["筹码低", fund.chip_concentrated_le_18],
        ["筹码松", fund.chip_loose_gt_20],
        ["5日涨", fund.return5_healthy_5_20],
        ["创/科", ind.growth_board],
        ["热点", ind.hot_industry],
      ]
        .map(([n, on]) => `<span class="tag ${on ? "on" : ""}" title="${n}">${n}${on ? " ✓" : ""}</span>`)
        .join("");
      const shrink = d.macd_hist_shrink
        ? '<span class="up" title="历史见顶相关指标，仅供研究，不参与打分">是</span>'
        : '<span class="flat" title="历史见顶相关指标，仅供研究，不参与打分">否</span>';
      return `
      <tr data-code="${x.code}">
        <td class="code-cell">${x.code}</td>
        <td class="name-cell">${esc(x.name)}</td>
        <td>${esc(x.board) || "--"}</td>
        <td>${esc(x.industry) || "--"}</td>
        <td class="num">
          <div class="score-cell">
            <div class="score-bar"><i style="width:${t}%"></i></div>
            <span class="score-num">${t}</span>
          </div>
        </td>
        <td><div class="hit-tags">${tags}</div></td>
        <td class="num">${fmt(x.metrics?.close)}</td>
        <td class="num">${fmt(d.turnover_rate)}%</td>
        <td class="num">${chgText(x.metrics?.return_5d_pct)}</td>
        <td class="num">${fmt(x.metrics?.pe)}</td>
        <td class="num">${fmt(d.kdj_j)}</td>
        <td class="num">${x.metrics?.chip_concentration != null ? fmt(x.metrics.chip_concentration) + "%" : "--"}</td>
        <td>${shrink}</td>
      </tr>`;
    })
    .join("");

  $("poolCount").textContent =
    `共 ${state.total} 条 · 当前展示 ${rows.length} 条 · 计分口径 v1.1：核心形态 0–50 + 筹码基本面 0–20 + 行业板块 0–30`;
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
   ========================================================================= */
async function renderIndustryChart() {
  const el = $("industryChart");
  try {
    const params = state.runId ? `?run_id=${state.runId}` : "";
    const data = await get(`/api/market/industry-stats${params}`);
    const all = (data.items || []).filter((x) => x.industry !== "其他");
    const items = (all.length ? all : data.items || []).slice(0, 18);
    if (!items.length) {
      el.innerHTML = '<div class="empty">暂无数据 —— 请先执行全市场扫描</div>';
      return;
    }
    const total = (data.items || []).reduce((s, x) => s + x.count, 0) || 1;
    const labels = items.map((x) => x.industry).reverse();
    const counts = items.map((x) => x.count).reverse();

    if (!state.chart) state.chart = echarts.init(el);
    state.chart.setOption(
      {
        animationDuration: 420,
        tooltip: {
          trigger: "axis",
          axisPointer: { type: "shadow" },
          backgroundColor: "rgba(255,255,255,.96)",
          borderColor: "#e8e8ed",
          textStyle: { color: "#1d1d1f", fontSize: 12 },
          formatter: (ps) => {
            const p = ps[0];
            return `${p.name}<br/>候选数量 <b>${p.value}</b> 只 · 占比 ${(p.value / total * 100).toFixed(1)}%`;
          },
        },
        grid: { left: 96, right: 64, top: 12, bottom: 24 },
        xAxis: {
          type: "value",
          axisLine: { show: false },
          axisTick: { show: false },
          splitLine: { lineStyle: { color: "#f2f2f6" } },
          axisLabel: { color: "#a1a1a6", fontSize: 11 },
        },
        yAxis: {
          type: "category",
          data: labels,
          axisLine: { lineStyle: { color: "#e8e8ed" } },
          axisTick: { show: false },
          axisLabel: { fontSize: 12, color: "#1d1d1f" },
        },
        series: [
          {
            name: "候选数量",
            type: "bar",
            data: counts,
            barWidth: 13,
            itemStyle: {
              borderRadius: [0, 7, 7, 0],
              color: {
                type: "linear", x: 0, y: 0, x2: 1, y2: 0,
                colorStops: [
                  { offset: 0, color: "#8fc2f5" },
                  { offset: 1, color: "#0071e3" },
                ],
              },
            },
            label: {
              show: true, position: "right", fontSize: 11,
              color: "#86868b", formatter: "{c}",
            },
          },
        ],
      },
      true
    );
    state.chart.resize();
  } catch {
    el.innerHTML = '<div class="empty">行业分布加载失败</div>';
  }
}

/* =========================================================================
   个股明细抽屉
   ========================================================================= */
async function openDrawer(code) {
  const item = state.items.find((x) => x.code === code);
  let profile = null, score = null;

  if (item) {
    $("dTitle").textContent = `${item.name} · ${item.code}`;
    profile = item;
    score = item;
  }

  $("drawerMask").classList.add("show");
  $("drawer").classList.add("show");

  try {
    const detail = await get(`/api/pool/${encodeURIComponent(code)}?days=120`);
    profile = detail.profile || profile;
    score = detail.score || score;
    renderDetailHeader(profile, score, detail);
    renderDetailChart(detail);
  } catch (e) {
    if (!profile) $("dTitle").textContent = code;
    $("dSub").textContent = "";
    $("dChart").innerHTML =
      `<div class="empty">明细数据加载失败<br><span style="font-size:12px">${esc(e.message)}</span></div>`;
  }
}

/* 抽屉头部：所属板块/行业 + 三模块得分 + 命中项拆解 */
function renderDetailHeader(profile, score, detail) {
  const name = profile?.name || score?.name || detail?.code || "";
  const code = profile?.code || score?.code || detail?.code || "";
  if (name) $("dTitle").textContent = `${name} · ${code}`;

  const bd = score?.breakdown || {};
  const core = bd.core || {}, fund = bd.fund || {}, ind = bd.industry || {};
  const coreScore = bd.core_score ?? score?.pattern_score ?? 0;
  const fundScore = bd.fund_score ?? 0;
  const indScore = bd.industry_score ?? 0;
  const total = score?.total_score ?? detail?.total_score ?? 0;

  $("dSub").textContent =
    `${profile?.board || ""} ${profile?.industry || ""} · 综合匹配分 ${total}/100` +
    `（核心形态 ${coreScore} + 筹码基本面 ${fundScore} + 行业板块 ${indScore}）`;

  const peTierText = {
    low: "低于行业30%分位", mid: "行业30%-70%分位",
    high: "高于行业70%分位", missing: "样本不足或缺失",
  };
  const chips = [
    ["MACD(3,6,3)金叉且红柱>0 +15", core.macd_gold_red],
    ["KDJ(9,3,3)金叉且J<100 +12", core.kdj_gold_j_under_100],
    ["量能>1.3×5日均量 +8", core.volume_surge_1_3x],
    ["量能>2×5日均量 +5", core.volume_surge_2x],
    ["换手率3%-15% +5", core.turnover_healthy_3_15],
    ["近40日振幅≤1.8 +5", core.range_compact_40d],
    ["筹码集中度≤18% +3", fund.chip_concentrated_le_18],
    ["筹码集中度>20% 加至8", fund.chip_loose_gt_20],
    [`PE行业分位：${peTierText[fund.pe_tier] || peTierText.missing}`, fund.pe_tier === "low" || fund.pe_tier === "mid"],
    ["近5日涨幅5%-20% +6", fund.return5_healthy_5_20],
    ["创业板/科创板 +8", ind.growth_board],
    ["热点行业 +22", ind.hot_industry],
  ]
    .map(([n, on]) => `<span class="chip ${on ? "on" : ""}">${esc(n)}${on ? " ✓" : ""}</span>`)
    .join("");
  $("dBreakdown").innerHTML = chips;
}

function renderDetailChart(detail) {
  const el = $("dChart");
  if (!detail || !detail.kline || !detail.indicators || !detail.dates) {
    throw new Error("明细数据不完整");
  }
  el.innerHTML = "";
  if (!state.detailChart) state.detailChart = echarts.init(el);
  const c = state.detailChart;
  c.clear();

  const dates = detail.dates;
  const k = detail.kline;
  const ind = detail.indicators;
  const upColor = "#fa5151", downColor = "#00b578";   // A股惯例：涨红跌绿

  // ECharts K 线数据顺序：[open, close, low, high]
  const candles = k.open.map((o, i) => [o, k.close[i], k.low[i], k.high[i]]);

  // 关键：多 grid 布局必须让 xAxis 与 yAxis 都显式指定 gridIndex，
  // 否则 ECharts 内部坐标轴与网格错配并抛 "Cannot read properties of undefined"，
  // 前端表现为「明细数据加载失败」。
  const grid = [
    { left: 62, right: 22, top: 26, height: "42%" },
    { left: 62, right: 22, top: "56%", height: "12%" },
    { left: 62, right: 22, top: "74%", height: "11%" },
    { left: 62, right: 22, top: "89%", height: "9%" },
  ];
  const xAxis = [0, 1, 2, 3].map((i) => ({
    type: "category", data: dates, gridIndex: i, boundaryGap: true,
    axisLine: { lineStyle: { color: "#e8e8ed" } },
    axisTick: { show: false },
    splitLine: { show: false },
    axisLabel: { show: i === 3, fontSize: 10, color: "#a1a1a6" },
  }));
  const yAxis = [
    { gridIndex: 0, scale: true, splitLine: { lineStyle: { color: "#f2f2f6" } },
      axisLabel: { fontSize: 10, color: "#a1a1a6" } },
    { gridIndex: 1, scale: true, splitLine: { show: false }, axisLabel: { show: false } },
    { gridIndex: 2, scale: true, splitLine: { show: false }, axisLabel: { show: false } },
    { gridIndex: 3, scale: true, splitLine: { show: false },
      axisLabel: { fontSize: 10, color: "#a1a1a6" } },
  ];

  c.setOption(
    {
      animation: false,
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "cross" },
        backgroundColor: "rgba(255,255,255,.96)",
        borderColor: "#e8e8ed",
        textStyle: { color: "#1d1d1f", fontSize: 12 },
      },
      axisPointer: { link: [{ xAxisIndex: "all" }] },
      grid,
      xAxis,
      yAxis,
      dataZoom: [
        { type: "inside", xAxisIndex: [0, 1, 2, 3], start: 40, end: 100 },
      ],
      series: [
        {
          name: "日K（前复权）",
          type: "candlestick",
          xAxisIndex: 0, yAxisIndex: 0,
          data: candles,
          itemStyle: {
            color: upColor, color0: downColor,
            borderColor: upColor, borderColor0: downColor,
          },
        },
        { name: "MA5", type: "line", data: ind.ma5, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { width: 1, color: "#f59f00" } },
        { name: "MA10", type: "line", data: ind.ma10, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { width: 1, color: "#9775fa" } },
        { name: "MA20", type: "line", data: ind.ma20, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { width: 1, color: "#0071e3" } },
        { name: "MA60", type: "line", data: ind.ma60, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { width: 1, color: "#86868b" } },
        {
          name: "成交量",
          type: "bar",
          xAxisIndex: 1, yAxisIndex: 1,
          data: k.volume.map((v, i) => ({
            value: v,
            itemStyle: { color: k.close[i] >= k.open[i] ? upColor : downColor },
          })),
        },
        {
          name: "MACD柱",
          type: "bar",
          xAxisIndex: 2, yAxisIndex: 2,
          data: ind.hist.map((v) => ({
            value: v,
            itemStyle: { color: v >= 0 ? upColor : downColor },
          })),
        },
        { name: "DIF", type: "line", xAxisIndex: 2, yAxisIndex: 2, symbol: "none", data: ind.dif, lineStyle: { width: 1, color: "#0071e3" } },
        { name: "DEA", type: "line", xAxisIndex: 2, yAxisIndex: 2, symbol: "none", data: ind.dea, lineStyle: { width: 1, color: "#f59f00" } },
        { name: "K", type: "line", xAxisIndex: 3, yAxisIndex: 3, symbol: "none", data: ind.k, lineStyle: { width: 1, color: "#0071e3" } },
        { name: "D", type: "line", xAxisIndex: 3, yAxisIndex: 3, symbol: "none", data: ind.d, lineStyle: { width: 1, color: "#f59f00" } },
        { name: "J", type: "line", xAxisIndex: 3, yAxisIndex: 3, symbol: "none", data: ind.j, lineStyle: { width: 1, color: "#9775fa" } },
      ],
    },
    true
  );
  // 抽屉动画结束后尺寸才稳定，再 resize 一次避免图宽偏差
  requestAnimationFrame(() => c.resize());
}

function closeDrawer() {
  $("drawerMask").classList.remove("show");
  $("drawer").classList.remove("show");
}

function resizeCharts() {
  state.chart?.resize();
  state.detailChart?.resize();
}

/* ---------------- 请求封装 ---------------- */
/* GET 自动重试：网络抖动或扫描期间数据库短暂繁忙（5xx）时重试一次，
   404 等确定性错误不重试，直接抛出。 */
async function get(url, retries = 1) {
  for (let attempt = 0; ; attempt++) {
    let r;
    try {
      r = await fetch(url);
    } catch (e) {
      // 网络层异常（服务重启中等），按可重试处理
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
