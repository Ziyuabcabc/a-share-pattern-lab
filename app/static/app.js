/* =========================================================================
   A股历史形态匹配研究看板 · 前端逻辑（本地运行版）
   数据来源：本地 FastAPI 接口（/api/*），全部为历史统计与指标展示。
   分数含义：与历史上升段启动样本的特征相似程度，不代表未来表现。
   ========================================================================= */

"use strict";

/* ---------------- 全局状态 ---------------- */
const state = {
  runId: null,
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

/* ---------------- 涨跌配色遵循 A 股惯例：涨红跌绿 ---------------- */
function chgCell(v) {
  if (v === null || v === undefined) return '<span class="flat">--</span>';
  const cls = v > 0 ? "up" : v < 0 ? "down" : "flat";
  return `<span class="${cls}">${v > 0 ? "+" : ""}${Number(v).toFixed(2)}%</span>`;
}

/* =========================================================================
   启动
   ========================================================================= */
document.addEventListener("DOMContentLoaded", () => {
  bindEvents();
  refreshAll();
});

function bindEvents() {
  $("btnScan").addEventListener("click", () => { $("scanPanel").hidden = false; });
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
   指数与统计
   ========================================================================= */
async function loadIndices() {
  const strip = $("indexStrip");
  try {
    const data = await get("/api/market/indices");
    strip.innerHTML = (data.items || [])
      .map(
        (x) => `
      <div class="index-card">
        <div class="name">${x.name}</div>
        <div class="row">
          <span class="val ${pctCls(x.change_pct)}">${fmt(x.close)}</span>
          <span class="chg ${pctCls(x.change_pct)}">${chgText(x.change_pct)}</span>
        </div>
      </div>`
      )
      .join("") || `<div class="index-card"><div class="name">指数数据</div><div class="row"><span class="val flat">--</span></div></div>`;
  } catch {
    strip.innerHTML = `<div class="index-card"><div class="name">指数数据加载失败</div></div>`;
  }
}

function chgText(v) {
  if (v === null || v === undefined) return "--";
  return (v > 0 ? "+" : "") + Number(v).toFixed(2) + "%";
}

async function loadStatus() {
  try {
    const s = await get("/api/scan/status");
    const run = s.latest_run;
    const pill = $("runState");
    if (s.running) {
      pill.textContent = "扫描中…";
      pill.className = "pill pill-run";
    } else if (run && run.status === "success") {
      pill.textContent = `批次 #${run.id} · ${run.finished_at?.slice(5, 16) || ""}`;
      pill.className = "pill pill-ok";
    } else {
      pill.textContent = "未扫描";
      pill.className = "pill pill-muted";
    }
    state.runId = run ? run.id : null;

    const stats = [
      { num: run ? run.candidates : "--", label: "总候选数", cls: "" },
      { num: run ? run.high_count : "--", label: "高匹配分（≥60）", cls: "up" },
      { num: run ? run.mid_count : "--", label: "中匹配分（30–59）", cls: "" },
      { num: run ? run.total_scanned : "--", label: "预筛后处理只数", cls: "" },
    ];
    $("statStrip").innerHTML = stats
      .map(
        (x) => `
      <div class="stat-card">
        <div class="num ${x.cls}">${x.num}</div>
        <div class="label">${x.label}</div>
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
      state.industries.map((x) => `<option value="${x}">${x}</option>`).join("");
    if ([...sel.options].some((o) => o.value === cur)) sel.value = cur;

    const boards = ["沪市主板", "深市主板", "创业板", "科创板"];
    const bsel = $("fBoard");
    bsel.innerHTML =
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
      const p = x.pattern_score, b = x.bonus_score, t = x.total_score;
      const hits = x.breakdown?.pattern || {};
      const d = x.display || {};
      const tags = [
        ["MACD", hits.macd_gold_red],
        ["KDJ", hits.kdj_gold_j_under_100],
        ["放量", hits.volume_surge],
        ["筹码", hits.chip_concentrated],
      ]
        .map(([n, on]) => `<span class="tag ${on ? "on" : ""}">${n}${on ? " ✓" : ""}</span>`)
        .join("");
      const shrink = d.macd_hist_shrink
        ? '<span class="up" title="历史见顶相关指标，仅供研究，不参与打分">是</span>'
        : '<span class="flat" title="历史见顶相关指标，仅供研究，不参与打分">否</span>';
      return `
      <tr data-code="${x.code}">
        <td class="code-cell">${x.code}</td>
        <td class="name-cell">${x.name}</td>
        <td>${x.board || "--"}</td>
        <td>${x.industry || "--"}</td>
        <td class="num">
          <div class="score-cell">
            <div class="score-bar"><i style="width:${t}%"></i></div>
            <span class="score-num">${t}</span>
          </div>
        </td>
        <td><div class="hit-tags">${tags}</div></td>
        <td class="num">${fmt(x.metrics?.close)}</td>
        <td class="num">${fmt(d.turnover_rate)}%</td>
        <td class="num">${fmt(d.kdj_j)}</td>
        <td class="num">${x.metrics?.chip_concentration != null ? fmt(x.metrics.chip_concentration) + "%" : "--"}</td>
        <td>${shrink}</td>
      </tr>`;
    })
    .join("");

  $("poolCount").textContent = `共 ${state.total} 条 · 当前展示 ${rows.length} 条 · 计分口径：形态匹配分 0–60 + 增强加分 0–40`;
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
  $("btnScanStart").disabled = true;
  $("scanLog").textContent = "正在启动扫描…";
  try {
    await post(`/api/scan/run?with_chips=${withChips}`);
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
        if (run) log.textContent += `\n\n✔ 批次 #${run.id} 完成：候选 ${run.candidates}，高匹配分 ${run.high_count}，中匹配分 ${run.mid_count}`;
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
   行业分布
   ========================================================================= */
async function renderIndustryChart() {
  try {
    const params = state.runId ? `?run_id=${state.runId}` : "";
    const data = await get(`/api/market/industry-stats${params}`);
    const items = (data.items || []).slice(0, 15);
    const el = $("industryChart");
    if (!items.length) {
      el.innerHTML = '<div class="empty">暂无数据 —— 请先执行全市场扫描</div>';
      return;
    }
    if (!state.chart) state.chart = echarts.init(el);
    state.chart.setOption({
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
      grid: { left: 90, right: 40, top: 16, bottom: 30 },
      xAxis: { type: "value", splitLine: { lineStyle: { color: "#f0f0f2" } } },
      yAxis: {
        type: "category",
        data: items.map((x) => x.industry).reverse(),
        axisLabel: { fontSize: 12, color: "#1d1d1f" },
      },
      series: [
        {
          name: "候选数量",
          type: "bar",
          data: items.map((x) => x.count).reverse(),
          barWidth: 14,
          itemStyle: { borderRadius: 7, color: "#0071e3" },
        },
      ],
    });
  } catch { /* 首次扫描前无数据 */ }
}

/* =========================================================================
   个股明细抽屉
   ========================================================================= */
async function openDrawer(code) {
  const item = state.items.find((x) => x.code === code);
  if (item) {
    $("dTitle").textContent = `${item.name} · ${item.code}`;
    $("dSub").textContent = `${item.board || ""} ${item.industry || ""} · 综合匹配分 ${item.total_score}（形态 ${item.pattern_score} + 增强 ${item.bonus_score}）`;
    const bd = item.breakdown || {};
    const chips = [
      ["MACD金叉且红柱大于0", bd.pattern?.macd_gold_red],
      ["KDJ金叉且J<100", bd.pattern?.kdj_gold_j_under_100],
      ["当日放量", bd.pattern?.volume_surge],
      ["筹码集中度≤18%", bd.pattern?.chip_concentrated],
      ["创业板/科创板", bd.bonus?.growth_board],
      ["筹码集中度>20%", bd.bonus?.chip_loose],
      ["量不低于5日均值", bd.bonus?.volume_above_mean],
      ["近40日横盘", bd.bonus?.range_compact],
    ]
      .map(([n, on]) => `<span class="chip ${on ? "on" : ""}">${n}${on ? " ✓" : ""}</span>`)
      .join("");
    $("dBreakdown").innerHTML = chips;
  }

  $("drawerMask").classList.add("show");
  $("drawer").classList.add("show");

  try {
    const detail = await get(`/api/pool/${code}?days=120`);
    renderDetailChart(detail);
  } catch {
    $("dChart").innerHTML = '<div class="empty">明细数据加载失败</div>';
  }
}

function closeDrawer() {
  $("drawerMask").classList.remove("show");
  $("drawer").classList.remove("show");
}

function renderDetailChart(detail) {
  const el = $("dChart");
  if (!state.detailChart) state.detailChart = echarts.init(el);
  const c = state.detailChart;

  const dates = detail.dates;
  const k = detail.kline;
  const ind = detail.indicators;

  // ECharts K 线数据顺序：[open, close, low, high]
  const candles = k.open.map((o, i) => [o, k.close[i], k.low[i], k.high[i]]);

  const upColor = "#fa5151", downColor = "#00b578"; // A股惯例：涨红跌绿
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
      grid: [
        { left: 60, right: 20, top: 24, height: "42%" },
        { left: 60, right: 20, top: "56%", height: "12%" },
        { left: 60, right: 20, top: "74%", height: "11%" },
        { left: 60, right: 20, top: "89%", height: "9%" },
      ],
      xAxis: [0, 1, 2, 3].map(() => ({
        type: "category",
        data: dates,
        boundaryGap: true,
        axisLine: { lineStyle: { color: "#e8e8ed" } },
        axisLabel: { show: false },
      })),
      yAxis: [
        { scale: true, splitLine: { lineStyle: { color: "#f0f0f2" } } },
        { scale: true, gridIndex: 1, splitLine: { show: false }, axisLabel: { show: false } },
        { scale: true, gridIndex: 2, splitLine: { show: false }, axisLabel: { show: false } },
        { scale: true, gridIndex: 3, splitLine: { show: false } },
      ],
      dataZoom: [
        { type: "inside", xAxisIndex: [0, 1, 2, 3], start: 40, end: 100 },
      ],
      series: [
        {
          name: "日K（前复权）",
          type: "candlestick",
          data: candles,
          itemStyle: { color: upColor, color0: downColor, borderColor: upColor, borderColor0: downColor },
        },
        { name: "MA5", type: "line", data: ind.ma5, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { width: 1, color: "#f59f00" } },
        { name: "MA10", type: "line", data: ind.ma10, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { width: 1, color: "#9775fa" } },
        { name: "MA20", type: "line", data: ind.ma20, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { width: 1, color: "#0071e3" } },
        { name: "MA60", type: "line", data: ind.ma60, xAxisIndex: 0, yAxisIndex: 0, symbol: "none", lineStyle: { width: 1, color: "#86868b" } },
        {
          name: "成交量",
          type: "bar",
          xAxisIndex: 1,
          yAxisIndex: 1,
          data: k.volume.map((v, i) => ({
            value: v,
            itemStyle: { color: k.close[i] >= k.open[i] ? upColor : downColor },
          })),
        },
        {
          name: "MACD柱",
          type: "bar",
          xAxisIndex: 2,
          yAxisIndex: 2,
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
  c.resize();
}

function resizeCharts() {
  state.chart?.resize();
  state.detailChart?.resize();
}

/* ---------------- 请求封装 ---------------- */
async function get(url) {
  const r = await fetch(url);
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${r.status}`);
  }
  return r.json();
}

async function post(url) {
  const r = await fetch(url, { method: "POST" });
  if (!r.ok) {
    const body = await r.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${r.status}`);
  }
  return r.json();
}
