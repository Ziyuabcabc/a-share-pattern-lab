/* =========================================================================
   A股历史形态匹配研究看板 · 前端 i18n 语言包（v1.5.0）
   -------------------------------------------------------------------------
   设计约定：
   1. 仅做「界面文案」双语化，不触碰任何打分逻辑、接口与数据结构。
   2. 语言偏好保存在 localStorage（键：patternlab.lang），刷新后保持。
   3. 静态文案通过 data-i18n / data-i18n-ph / data-i18n-title /
      data-i18n-aria 属性批量替换；动态渲染部分由 app.js 调用 t() 取词，
      并在语言切换时按注册的监听器重绘。
   4. 专业指标（MACD / KDJ / PE / 换手率等）中英一致，沿用国际通用写法；
      行业使用申万一级行业标准英文译名；指数使用市场通用英文名称。
   5. 股票名称与代码保持原样，不做翻译。
   ========================================================================= */

(function (global) {
  "use strict";

  const STORAGE_KEY = "patternlab.lang";
  const SUPPORTED = ["zh", "en"];

  /* =======================================================================
     一、界面文案
     ======================================================================= */
  const DICT = {
    /* ---------------------------- 简体中文 ---------------------------- */
    zh: {
      htmlLang: "zh-CN",
      docTitle: "A 股历史形态匹配研究看板 · 本地运行版",
      brand: "A 股历史形态匹配研究看板",
      brandSub: "本地运行版 · 仅做历史数据统计与指标展示",
      langGroupAria: "语言切换",
      runIdle: "尚未执行扫描",
      btnScan: "执行全市场扫描",
      btnExport: "导出 CSV",
      disclaimer: "本工具仅为学术研究用途，不构成任何投资建议，股市有风险，投资需谨慎",

      /* 扫描面板 */
      scanTitle: "行情扫描",
      scanHint: "建议在每个交易日收盘后运行；首次扫描需建立本地缓存，耗时约 5–15 分钟。",
      scanScopeAll: "全市场",
      scanScopeGrowthStar: "创业板 + 科创板",
      scanScopeGrowth: "创业板",
      scanScopeStar: "科创板",
      scanScopeSh: "沪市主板",
      scanScopeSz: "深市主板",
      scanScopeLabel: "扫描范围",
      scanScopeNote: "范围越小耗时越短，适合快速验证",
      scanChips: "同时抓取筹码集中度",
      scanChipsNote: "缺失时对应项计 0 分，耗时显著增加",
      btnStart: "开始",
      btnCollapse: "收起",
      scanLogIdle: "等待启动…",
      scanStarting: "正在启动扫描…",
      scanStartFail: "启动失败：{msg}",
      scanPreparing: "扫描准备中…",
      scanDone: "✔ 批次 #{id} 完成：候选 {c}，高匹配分 {h}，中匹配分 {m}",

      /* 宏观参考面板 */
      macroTitle: "宏观参考面板",
      macroSub: "第一行：国内主要指数（点位 + 涨跌幅）；第二行：海外隔夜与港股参考",
      macroNote: "仅为市场数据参考，不构成投资建议",
      macroLoading: "行情加载中…",
      macroUnavailable: "行情源暂不可用",
      macroCached: "本地缓存快照 · {t}",
      macroLive: "实时行情快照",
      tagOvernight: "隔夜",
      tagToday: "当日",

      /* 扫描状态 */
      pillRunning: "扫描进行中…",
      pillLatest: "最新批次 #{id} · {t}",
      pillIdle: "尚未执行扫描",

      /* 统计概览 */
      statTotal: "总候选数",
      statTotalSub: "当前批次入选样本",
      statHigh: "高匹配分（≥60）",
      statHighSub: "与历史样本特征高度相似",
      statMid: "中匹配分（30–59）",
      statMidSub: "与历史样本特征中度相似",
      statProcessed: "预筛后处理只数",
      statProcessedSub: "通过基础过滤的样本",

      /* 候选池 */
      poolTitle: "候选池",
      poolSub: "计分口径 v1.1：核心形态 0–50 + 筹码基本面 0–20 + 行业板块 0–30",
      filterAllBoards: "全部板块",
      filterAllIndustries: "全部行业",
      filterAllScores: "全部匹配分",
      filterScore60: "≥ 60（高匹配分）",
      filterScore40: "≥ 40",
      filterScore30: "≥ 30（中匹配分）",
      orderDesc: "匹配分从高到低",
      orderAsc: "匹配分从低到高",
      searchPlaceholder: "搜索代码 / 名称",
      thCode: "代码",
      thName: "名称",
      thBoard: "板块",
      thIndustry: "行业",
      thScore: "综合匹配分",
      thHits: "指标命中项",
      thClose: "收盘价",
      thTurnover: "换手率",
      thReturn5: "近5日涨幅",
      thPe: "PE(TTM)",
      thKdj: "KDJ-J",
      thChip: "筹码集中度",
      thShrink: "红柱前高收缩",
      thShrinkNote: "历史见顶相关指标，仅供研究，不参与打分",
      poolEmptyScan: "暂无数据 —— 请先执行全市场扫描",
      poolEmptySearch: "没有符合搜索条件的记录",
      poolLoadFail: "候选池加载失败 —— 请确认本地服务已启动",
      btnMore: "加载更多",
      poolCount: "共 {total} 条 · 当前展示 {shown} 条 · 计分口径 v1.1（满分 100）",
      yes: "是",
      no: "否",

      /* 指标命中标签 */
      tagMacd: "MACD",
      tagKdj: "KDJ",
      tagVol13: "量1.3",
      tagVol2: "量2倍",
      tagTurnover: "换手",
      tagRange: "横盘",
      tagChipLow: "筹码低",
      tagChipLoose: "筹码松",
      tagPe: "PE分位",
      tagReturn5: "5日涨",
      tagGrowth: "创科",
      tagHot: "热点",

      /* 指标命中说明（悬停提示，与打分引擎 v1.1 逐项对应） */
      hitMacd: "MACD(3,6,3) 金叉且红柱 > 0（+15）",
      hitKdj: "KDJ(9,3,3) 金叉且 J < 100（+12）",
      hitVol13: "成交量 > 1.3 倍近5日均量（+8）",
      hitVol2: "成交量 > 2 倍近5日均量（叠加 +5）",
      hitTurnover: "当日换手率处于 3% – 15%（+5）",
      hitRange: "近40个交易日振幅 ≤ 1.8（+5）",
      hitChipLow: "筹码集中度 ≤ 18%（+3）",
      hitChipLoose: "筹码集中度 > 20%（加至满分 8）",
      hitPe: "PE 低于所属行业 30% 分位（+6）/ 30%–70% 分位（+3）",
      hitReturn5: "近5日涨幅处于 5% – 20%（+6）",
      hitGrowth: "创业板 / 科创板（+8）",
      hitHot: "所属行业命中热点名单（+22）",

      /* 行业分布 */
      indTitle: "候选池行业分布",
      indSub: "行业分类口径：申万一级行业（31 个）；命中热点名单的行业以暖色区分",
      indCoverage: "覆盖 {n} 个行业 · 样本 {total} 只 · 「其他」占比 {pct}%",
      indEmpty: "暂无数据 —— 请先执行全市场扫描",
      indFail: "行业分布加载失败",
      indSeries: "候选数量",
      indHotTip: " · 命中热点名单",
      indTipCount: "候选数量",
      indTipUnit: " 只",
      indTipShare: "占比",
      indTipAvg: "平均匹配分",
      indTipMax: "最高",
      indLabel: "{v} 只 · {p}%",

      /* 市场资讯 */
      newsTitle: "市场资讯",
      newsNote: "公开财经快讯自动采集并按关键词归类，仅作研究参考",
      newsNoteEn: "News in Chinese —— 英文版下资讯标题保留第三方中文原文，仅按关键词归类展示",
      newsBadge: "News in Chinese",
      newsTabAll: "全部",
      newsCatDomestic: "国内资讯",
      newsCatOverseas: "海外资讯",
      newsCatMacro: "宏观资讯",
      newsUnavailable: "资讯暂不可用",
      newsLoading: "资讯加载中…",
      newsEmptyCat: "该分类暂无资讯",
      newsMeta: "共 {n} 条 · 来源 {src} · 更新 {t}",
      newsFootNote: "资讯为第三方公开内容摘要，不代表本工具观点",

      /* 个股明细抽屉 */
      btnClose: "关闭",
      drawerLoading: "正在读取指标明细…",
      drawerLoadFail: "明细数据加载失败：{msg}",
      drawerChartFail: "图表渲染失败：{msg}",
      drawerChartLoading: "正在加载明细数据…",
      drawerChartTitle: "价格与指标走势",
      drawerChartSub: "前复权价格 · MACD(3,6,3) · KDJ(9,3,3)",
      drawerScore: "综合匹配分",
      drawerScoreLine: "综合匹配分 {n} / 100",
      drawerNotInPool: "该个股不在当前批次候选中，仅展示历史指标",
      subtotal: "小计 {a} / {b}",
      groupCore: "核心形态分",
      groupFund: "筹码基本面分",
      groupInd: "行业板块分",
      itemMacd: "MACD(3,6,3) 金叉且红柱 > 0",
      itemKdj: "KDJ(9,3,3) 金叉且 J < 100",
      itemVol13: "成交量 > 1.3 倍近5日均量",
      itemVol2: "成交量 > 2 倍近5日均量（叠加项）",
      itemTurnover: "当日换手率处于 3% – 15%",
      itemRange: "近 40 个交易日振幅 ≤ 1.8",
      itemChipLow: "筹码集中度 ≤ 18%",
      itemChipLoose: "筹码集中度 > 20%（加至满分 8）",
      itemPe: "PE 行业分位：{tier}",
      itemReturn5: "近5日涨幅处于 5% – 20%",
      itemGrowthBoard: "所属板块为创业板 / 科创板",
      itemHotIndustry: "所属行业命中热点名单",
      peTierLow: "低于行业 30% 分位",
      peTierMid: "行业 30% – 70% 分位",
      peTierHigh: "高于行业 70% 分位",
      peTierMissing: "样本不足或缺失",
      detailIncomplete: "明细数据不完整",
      serKline: "日K（前复权）",
      serVolume: "成交量",
      serMacdHist: "MACD柱",

      /* 页脚 */
      foot1: "本工具仅对 A 股历史公开数据做统计研究：输出为形态特征匹配度分数，用于表示当前个股与历史上升段启动样本的特征相似程度，不代表未来表现，不构成任何形式的操作指引。",
      foot2Strong: "历史数据不等于未来表现。",
      foot2Rest: " 带 * 字段为历史见顶相关指标，仅供研究。仅限本地运行，请勿部署到公网。",

      /* 策略回测（v1.6.0） */
      btTitle: "策略回测",
      btSub: "按形态匹配分分档，统计历史持仓 5 / 10 / 20 个交易日的绩效表现；回测用未来信息已严格规避",
      btHorizonAria: "持仓周期",
      btH5: "5 日",
      btH10: "10 日",
      btH20: "20 日",
      btnBtCsv: "导出回测明细",
      btThGroup: "分组",
      btThSamples: "样本数",
      btThWin: "上涨胜率",
      btThAvg: "平均收益率",
      btThExcess: "超额收益",
      btThMdd: "最大回撤",
      btThPl: "盈亏比",
      btEmpty: "暂无回测结果 —— 请先构建回测数据并执行回测",
      btLoading: "回测结果加载中…",
      btRange: "回测区间",
      btPoints: "调仓点",
      btStocks: "覆盖个股",
      btSamples: "样本数",
      btBenchmark: "基准",
      btScoreMax: "可复现满分",
      btUnitPoints: "个",
      btUnitStocks: "只",
      btUnitSamples: "条",
      btUnitScore: "分",
      btChartTitle: "累计净值与基准对比",
      btChartCum: "累计净值（起点 = 1.00）",
      btGroupHigh: "高匹配分组",
      btGroupMid: "中匹配分组",
      btGroupLow: "低匹配分组",
      btGrpHighShort: "高分组",
      btGrpMidShort: "中分组",
      btGrpLowShort: "低分组",
      btDialogTitle: "回测方法学说明",
      btDialogClose: "知道了",
      btRunNow: "重跑回测",
      btRunning: "回测执行中…",
      btStarted: "回测任务已启动，完成后自动刷新",
      btUnavailable: "暂无回测结果，请先在服务端执行回测脚本",
      btFootnote: "历史统计结果不代表未来表现；回测未扣除交易成本，统计显著性检验与参数敏感性结果见下方板块。",

      /* v1.7.0：回测口径切换（分档 / 共振） */
      btModeAria: "对比口径",
      btModeTier: "分档绩效",
      btModeResonance: "共振对比",
      btUnitDays: "日",
      btThHorizon: "持仓周期",
      rsThScope: "对比口径",
      rsThWinDiff: "胜率之差",
      rsThRetDiff: "收益之差",
      rsLegendResonance: "日线 + 周线双共振",
      rsLegendDaily: "单日线高匹配分组",
      rsNote: "周线共振在 {rows} 条样本中命中 {rate}%；上表为当前持仓周期下两组的口径对比，「之」列均为「双共振 − 单日线」。",
      rsFootnote: "共振分组为在原单日线策略上叠加周线条件得到的子集，样本量约为对照组的三分之一，抽样波动更大；该对比未单独做显著性检验，差异不宜单独下结论。",
      rsUnavailable: "暂无共振对比结果 —— 请先运行 v1.7 分析构建",

      /* v1.7.0：分行业回测 */
      indBtTitle: "分行业回测",
      indBtSub: "按申万一级行业拆分高 / 中 / 低匹配分组的历史绩效；结果由回测明细二次聚合，与「策略回测」同源可对账",
      btnIndCsv: "导出分行业明细",
      indThBar: "相对展示",
      indBtEmpty: "暂无分行业回测结果 —— 请先运行 v1.7 分析构建",
      indSelNoHigh: "高分组样本不足",
      indMetaCovered: "行业覆盖",
      indMetaRanked: "参与排名",
      indMetaRankH: "排名周期",
      indMetaMin: "样本下限",
      indMetaTop3: "前三行业占比",
      indBtFoot: "排名仅纳入高匹配分组样本不少于 {n} 条的行业；高匹配分组样本高度集中于 {industries}，前三行业合计占 {share}%，因此参与排名的行业数会明显少于行业总数。涨幅口径：观察日 T 收盘后打分，T+1 开盘价入场，T+n 收盘价出场；最大回撤由行业内该组按观察点等权聚合后复利累积的净值序列计算。",
      indNoData: "该行业本周期无样本",
      indNoHighData: "当前持仓周期下暂无行业具备高匹配分组样本",
      indRankIndustry: "行业",
      indRankTop: "效果相对靠前",
      indRankBottom: "效果相对靠后",
      indRankTitle: "行业排名（高匹配分组平均收益率，持仓 {h} 个交易日）",

      /* v1.7.0：统计检验与参数稳健性 */
      robTitle: "统计检验与参数稳健性",
      robSub: "高 / 低匹配分组的 Welch t 检验、分年度稳健性拆解，以及 3 组核心参数的敏感性对照",
      robSecSig: "一、分组差异的统计显著性（Welch t 检验）",
      robSecYear: "二、分年度稳健性检验",
      robSecSens: "三、参数敏感性分析",
      sigThHigh: "高匹配分组均值",
      sigThLow: "低匹配分组均值",
      sigThDiff: "均值差异",
      sigThT: "t 值",
      sigThP: "p 值",
      sigThCi: "95% 置信区间（差异）",
      sigEmpty: "暂无检验结果",
      sigFoot: "双尾 Welch t 检验，显著性水平 α = {alpha}；{verdict}。样本在行业与时间上高度重叠，独立性假定并不严格成立，p 值偏小，且未做多重比较校正。",
      sigFootYes: "三个持仓周期均拒绝「高低分组无差异」的原假设",
      sigFootNo: "部分周期未能拒绝原假设",
      robThYear: "年度",
      robThNh: "高分组样本",
      robThWdiff: "胜率之差",
      robThDir: "方向是否一致",
      robThEnough: "样本充足",
      robConsistent: "一致",
      robInconsistent: "不一致",
      robNotEnough: "样本不足",
      robFoot: "仅纳入高分组样本不少于 {min} 条的年度；分周期一致率：{detail}。一致性有限说明结论并非在所有年度都成立。",
      sensThParam: "考察参数",
      sensThValue: "参数取值",
      sensThWin: "胜率",
      sensThRet: "高匹配分组收益",
      sensThDelta: "相对基准变动",
      sensThDir: "分组方向",
      sensThStable: "幅度稳定性",
      sensEmpty: "暂无敏感性分析结果",
      sensBaseLabel: "基准",
      sensBaseRow: "基准参数",
      sensBaseTag: "基准",
      sensDirKept: "保持",
      sensDirLost: "改变",
      sensStable: "稳定",
      sensUnstable: "超阈值",
      sensFoot: "上表为持仓 {h} 个交易日的口径，共 {n} 个扰动档，每档单独跑一次全量回测。{dir}，{mag}；变动最大的参数组为「{worst}」（{pp} 个百分点，预设稳定阈值 {threshold} 个百分点）。分值权重与分组阈值未纳入本次考察。",
      sensFootDirOk: "各档「高匹配分组收益高于低匹配分组」的方向均保持不变",
      sensFootDirNo: "部分扰动档下分组方向发生改变",
      sensFootMagOk: "各档相对基准的幅度变动均低于预设稳定阈值",
      sensFootMagNo: "存在幅度变动超过预设稳定阈值的扰动档",

      /* 研究简报（v1.7.0：九节结构） */
      repTitle: "研究简报",
      repSub: "按标准学术结构自动生成，数据与看板实时同步，同一批次可完整复现",
      btnRepPreview: "预览简报",
      btnRepPdf: "导出 PDF",
      repExporting: "正在渲染 PDF…",
      repExportFail: "PDF 导出失败",
      repPreviewFail: "简报预览打开失败",
      repMeta: "生成时间",
      repFootNote: "简报为本地程序依公开历史数据自动生成，结论可复现",
      repOutline1: "研究摘要",
      repOutline2: "研究方法与规则",
      repOutline3: "历史回测结论",
      repOutline4: "分行业回测",
      repOutline5: "多周期共振策略",
      repOutline6: "当日候选池分析",
      repOutline7: "研究结论与展望",
      repOutline8: "研究局限性与风险提示",
      repOutline9: "合规声明",
    },

    /* ----------------------------- English ---------------------------- */
    en: {
      htmlLang: "en",
      docTitle: "A-Share Historical Pattern Matching Dashboard · Local Edition",
      brand: "A-Share Historical Pattern Matching Dashboard",
      brandSub: "Local edition · Historical statistics and indicator display only",
      langGroupAria: "Language switch",
      runIdle: "No scan run yet",
      btnScan: "Run Full-Market Scan",
      btnExport: "Export CSV",
      disclaimer: "For academic research only. Not investment advice. Markets carry risk — please research with caution.",

      /* Scan panel */
      scanTitle: "Market Scan",
      scanHint: "Run after the close of each trading day. The first scan builds a local cache and takes about 5–15 minutes.",
      scanScopeAll: "Full Market",
      scanScopeGrowthStar: "ChiNext + STAR",
      scanScopeGrowth: "ChiNext",
      scanScopeStar: "STAR Market",
      scanScopeSh: "SSE Main Board",
      scanScopeSz: "SZSE Main Board",
      scanScopeLabel: "Scan scope",
      scanScopeNote: "A narrower scope runs faster — useful for a quick check",
      scanChips: "Also fetch chip concentration",
      scanChipsNote: "Missing data scores 0 for that item and adds significant time",
      btnStart: "Start",
      btnCollapse: "Collapse",
      scanLogIdle: "Waiting to start…",
      scanStarting: "Starting scan…",
      scanStartFail: "Failed to start: {msg}",
      scanPreparing: "Preparing scan…",
      scanDone: "✔ Batch #{id} finished: {c} candidates, {h} high match, {m} medium match",

      /* Macro panel */
      macroTitle: "Market Reference Panel",
      macroSub: "Row 1: key domestic indices (level + change). Row 2: overseas overnight and Hong Kong reference.",
      macroNote: "Market data reference only; not investment advice",
      macroLoading: "Loading quotes…",
      macroUnavailable: "Quote source unavailable",
      macroCached: "Local cache snapshot · {t}",
      macroLive: "Live quote snapshot",
      tagOvernight: "Overnight",
      tagToday: "Today",

      /* Scan status */
      pillRunning: "Scan in progress…",
      pillLatest: "Latest batch #{id} · {t}",
      pillIdle: "No scan run yet",

      /* Stats */
      statTotal: "Total Candidates",
      statTotalSub: "Samples selected in the current batch",
      statHigh: "High Match (≥60)",
      statHighSub: "High similarity to historical samples",
      statMid: "Medium Match (30–59)",
      statMidSub: "Moderate similarity to historical samples",
      statProcessed: "Processed After Pre-filter",
      statProcessedSub: "Samples passing the base filters",

      /* Candidate pool */
      poolTitle: "Candidate Pool",
      poolSub: "Scoring v1.1: Core Pattern 0–50 + Chips & Fundamentals 0–20 + Industry & Board 0–30",
      filterAllBoards: "All Boards",
      filterAllIndustries: "All Industries",
      filterAllScores: "All Scores",
      filterScore60: "≥ 60 (High)",
      filterScore40: "≥ 40",
      filterScore30: "≥ 30 (Medium)",
      orderDesc: "Score: High to Low",
      orderAsc: "Score: Low to High",
      searchPlaceholder: "Search code / name",
      thCode: "Code",
      thName: "Name",
      thBoard: "Board",
      thIndustry: "Industry",
      thScore: "Match Score",
      thHits: "Indicator Hits",
      thClose: "Close",
      thTurnover: "Turnover Rate",
      thReturn5: "5-Day Return",
      thPe: "PE (TTM)",
      thKdj: "KDJ-J",
      thChip: "Chip Conc.",
      thShrink: "Peak Hist. Shrink",
      thShrinkNote: "Related to historical peaks; research only, not scored",
      poolEmptyScan: "No data — run a full-market scan first",
      poolEmptySearch: "No records match the search",
      poolLoadFail: "Failed to load the pool — check that the local service is running",
      btnMore: "Load More",
      poolCount: "{total} records · showing {shown} · scoring v1.1 (out of 100)",
      yes: "Yes",
      no: "No",

      /* Indicator hit tags（保持 ≤5 字符，保证 12 个标签仍按 2 行排布） */
      tagMacd: "MACD",
      tagKdj: "KDJ",
      tagVol13: "VOL",
      tagVol2: "VOL2",
      tagTurnover: "TURN",
      tagRange: "RANGE",
      tagChipLow: "CHIP↓",
      tagChipLoose: "CHIP↑",
      tagPe: "PE",
      tagReturn5: "5D↑",
      tagGrowth: "GROW",
      tagHot: "HOT",

      hitMacd: "MACD(3,6,3) golden cross with a positive histogram (+15)",
      hitKdj: "KDJ(9,3,3) golden cross with J < 100 (+12)",
      hitVol13: "Volume > 1.3× the 5-day average volume (+8)",
      hitVol2: "Volume > 2× the 5-day average volume (add-on +5)",
      hitTurnover: "Turnover rate between 3% and 15% (+5)",
      hitRange: "40-day high/low ratio ≤ 1.8 (+5)",
      hitChipLow: "Chip concentration ≤ 18% (+3)",
      hitChipLoose: "Chip concentration > 20% (up to a full 8)",
      hitPe: "PE below the industry 30th percentile (+6) / 30th–70th percentile (+3)",
      hitReturn5: "5-day return between 5% and 20% (+6)",
      hitGrowth: "ChiNext / STAR Market (+8)",
      hitHot: "Industry is on the hot list (+22)",

      /* Industry distribution */
      indTitle: "Candidate Pool by Industry",
      indSub: "Classification: SW Level-1 industries (31). Industries on the hot list are highlighted in warm tones.",
      indCoverage: "{n} industries covered · {total} samples · \"Others\" {pct}%",
      indEmpty: "No data — run a full-market scan first",
      indFail: "Failed to load industry distribution",
      indSeries: "Candidates",
      indHotTip: " · on hot list",
      indTipCount: "Candidates",
      indTipUnit: "",
      indTipShare: "share",
      indTipAvg: "Avg score",
      indTipMax: "Max",
      indLabel: "{v} · {p}%",

      /* Market news */
      newsTitle: "Market News",
      newsNote: "Public financial newswires aggregated and keyword-classified, for research reference only",
      newsNoteEn: "News in Chinese — headlines are kept in their original Chinese and classified by keyword for reference only",
      newsBadge: "News in Chinese",
      newsTabAll: "All",
      newsCatDomestic: "Domestic",
      newsCatOverseas: "Overseas",
      newsCatMacro: "Macro",
      newsUnavailable: "News unavailable",
      newsLoading: "Loading news…",
      newsEmptyCat: "No news in this category",
      newsMeta: "{n} items · sources {src} · updated {t}",
      newsFootNote: "Third-party public content summaries; not the view of this tool",

      /* Stock detail drawer */
      btnClose: "Close",
      drawerLoading: "Loading indicator details…",
      drawerLoadFail: "Failed to load details: {msg}",
      drawerChartFail: "Chart rendering failed: {msg}",
      drawerChartLoading: "Loading detail data…",
      drawerChartTitle: "Price & Indicator Trend",
      drawerChartSub: "Forward-adjusted price · MACD(3,6,3) · KDJ(9,3,3)",
      drawerScore: "Composite Match Score",
      drawerScoreLine: "Composite match score {n} / 100",
      drawerNotInPool: "Not in the current candidate batch — historical indicators only",
      subtotal: "Subtotal {a} / {b}",
      groupCore: "Core Pattern",
      groupFund: "Chips & Fundamentals",
      groupInd: "Industry & Board",
      itemMacd: "MACD(3,6,3) golden cross with a positive histogram",
      itemKdj: "KDJ(9,3,3) golden cross with J < 100",
      itemVol13: "Volume > 1.3× the 5-day average volume",
      itemVol2: "Volume > 2× the 5-day average volume (add-on)",
      itemTurnover: "Turnover rate between 3% and 15%",
      itemRange: "40-day high/low ratio ≤ 1.8",
      itemChipLow: "Chip concentration ≤ 18%",
      itemChipLoose: "Chip concentration > 20% (up to a full 8)",
      itemPe: "PE industry percentile: {tier}",
      itemReturn5: "5-day return between 5% and 20%",
      itemGrowthBoard: "Listed on ChiNext / STAR Market",
      itemHotIndustry: "Industry is on the hot list",
      peTierLow: "below the industry 30th percentile",
      peTierMid: "industry 30th–70th percentile",
      peTierHigh: "above the industry 70th percentile",
      peTierMissing: "insufficient or missing samples",
      detailIncomplete: "Incomplete detail data",
      serKline: "Daily K (adj.)",
      serVolume: "Volume",
      serMacdHist: "MACD Hist",

      /* Footer */
      foot1: "This tool performs statistical research on public historical A-share data only. Its output is a pattern-similarity score that expresses how closely a stock currently resembles historical advance-phase launch samples. It does not represent future performance and is not any form of operational guidance.",
      foot2Strong: "Historical data does not equal future performance.",
      foot2Rest: " Fields marked with * relate to historical peaks and are for research only. Local use only — do not deploy to the public internet.",

      /* Strategy backtest (v1.6.0) */
      btTitle: "Strategy Backtest",
      btSub: "Performance by pattern-score tier over 5 / 10 / 20 trading-day holding periods; look-ahead bias strictly avoided",
      btHorizonAria: "Holding period",
      btH5: "5D",
      btH10: "10D",
      btH20: "20D",
      btnBtCsv: "Export backtest detail",
      btThGroup: "Tier",
      btThSamples: "Samples",
      btThWin: "Win rate",
      btThAvg: "Avg return",
      btThExcess: "Excess return",
      btThMdd: "Max drawdown",
      btThPl: "Profit/loss ratio",
      btEmpty: "No backtest result yet — build the backtest dataset and run the backtest first",
      btLoading: "Loading backtest result…",
      btRange: "Period",
      btPoints: "Rebalance points",
      btStocks: "Stocks covered",
      btSamples: "Samples",
      btBenchmark: "Benchmark",
      btScoreMax: "Reproducible max",
      btUnitPoints: "",
      btUnitStocks: "",
      btUnitSamples: "",
      btUnitScore: "pts",
      btChartTitle: "Cumulative NAV vs. benchmark",
      btChartCum: "Cumulative NAV (start = 1.00)",
      btGroupHigh: "High tier",
      btGroupMid: "Mid tier",
      btGroupLow: "Low tier",
      btGrpHighShort: "High",
      btGrpMidShort: "Mid",
      btGrpLowShort: "Low",
      btDialogTitle: "Backtest methodology",
      btDialogClose: "Got it",
      btRunNow: "Re-run backtest",
      btRunning: "Backtest running…",
      btStarted: "Backtest started — the page will refresh automatically when done",
      btUnavailable: "No backtest result yet — run the backtest script on the server first",
      btFootnote: "Historical statistics do not represent future performance. Trading costs are not deducted; significance tests and parameter sensitivity results are shown in the panels below.",

      /* v1.7.0: backtest comparison mode (tiers / resonance) */
      btModeAria: "Comparison basis",
      btModeTier: "Tier performance",
      btModeResonance: "Resonance",
      btUnitDays: "D",
      btThHorizon: "Horizon",
      rsThScope: "Comparison",
      rsThWinDiff: "Win-rate diff",
      rsThRetDiff: "Return diff",
      rsLegendResonance: "Daily + weekly resonance",
      rsLegendDaily: "Daily-only high tier",
      rsNote: "Weekly resonance is triggered in {rate}% of {rows} samples. The table compares the two groups at the selected horizon; the diff columns are resonance minus daily-only.",
      rsFootnote: "The resonance group is a subset formed by adding a weekly condition on top of the daily tier, with about one third of the sample size and therefore higher sampling noise. No separate significance test was performed for this comparison.",
      rsUnavailable: "No resonance comparison yet — run the v1.7 analysis build first",

      /* v1.7.0: industry-level backtest */
      indBtTitle: "Industry-level backtest",
      indBtSub: "Historical performance of the high / mid / low tiers split by SW level-1 industry; aggregated from existing backtest records and reconcilable with the tier panel",
      btnIndCsv: "Export industry detail",
      indThBar: "Relative",
      indBtEmpty: "No industry-level results yet — run the v1.7 analysis build first",
      indSelNoHigh: "insufficient high-tier samples",
      indMetaCovered: "Industries covered",
      indMetaRanked: "In ranking",
      indMetaRankH: "Ranking horizon",
      indMetaMin: "Min samples",
      indMetaTop3: "Top-3 share",
      indBtFoot: "Only industries with at least {n} high-tier samples enter the ranking. High-tier samples are heavily concentrated in {industries}, with the top three industries accounting for {share}%, so far fewer industries are ranked than exist in total. Return basis: scored after the close of day T, entered at the open of T+1, exited at the close of T+n. Max drawdown is computed from the compounded equal-weight equity curve of that group within the industry.",
      indNoData: "No samples for this industry at this horizon",
      indNoHighData: "No industry has high-tier samples at the selected horizon",
      indRankIndustry: "Industry",
      indRankTop: "Relatively stronger",
      indRankBottom: "Relatively weaker",
      indRankTitle: "Industry ranking (high-tier mean return, {h}-day holding)",

      /* v1.7.0: significance and parameter robustness */
      robTitle: "Statistical tests and parameter robustness",
      robSub: "Welch t-test between the high and low tiers, year-by-year robustness, and sensitivity across three core parameters",
      robSecSig: "1. Significance of the tier spread (Welch t-test)",
      robSecYear: "2. Year-by-year robustness",
      robSecSens: "3. Parameter sensitivity",
      sigThHigh: "High-tier mean",
      sigThLow: "Low-tier mean",
      sigThDiff: "Mean difference",
      sigThT: "t",
      sigThP: "p",
      sigThCi: "95% CI (difference)",
      sigEmpty: "No test results",
      sigFoot: "Two-sided Welch t-test at α = {alpha}; {verdict}. Samples overlap heavily across industries and time, so the independence assumption does not strictly hold, p-values are understated, and no multiple-comparison correction is applied.",
      sigFootYes: "all three horizons reject the null of no difference",
      sigFootNo: "some horizons fail to reject the null",
      robThYear: "Year",
      robThNh: "High-tier n",
      robThWdiff: "Win-rate diff",
      robThDir: "Direction consistent",
      robThEnough: "Enough samples",
      robConsistent: "Consistent",
      robInconsistent: "Inconsistent",
      robNotEnough: "Too few",
      robFoot: "Only years with at least {min} high-tier samples are included. Consistency by horizon: {detail}. Limited consistency means the conclusion does not hold in every year.",
      sensThParam: "Parameter",
      sensThValue: "Setting",
      sensThWin: "Win rate",
      sensThRet: "High-tier return",
      sensThDelta: "Change vs base",
      sensThDir: "Tier direction",
      sensThStable: "Magnitude stable",
      sensEmpty: "No sensitivity results",
      sensBaseLabel: "base",
      sensBaseRow: "Base parameters",
      sensBaseTag: "base",
      sensDirKept: "Kept",
      sensDirLost: "Changed",
      sensStable: "Stable",
      sensUnstable: "Above threshold",
      sensFoot: "The table shows the {h}-day horizon across {n} perturbation variants, each run as a separate full backtest. {dir}, {mag}. The largest change comes from \"{worst}\" ({pp} pp, against a preset stability threshold of {threshold} pp). Score weights and tier thresholds are not covered here.",
      sensFootDirOk: "the direction \"high tier above low tier\" holds in every variant",
      sensFootDirNo: "the direction changes under some variants",
      sensFootMagOk: "the magnitude change versus base stays below the preset stability threshold in every variant",
      sensFootMagNo: "some variants exceed the preset stability threshold",

      /* Research note (v1.7.0: nine sections) */
      repTitle: "Research Note",
      repSub: "Generated automatically in a standard academic structure, synchronized with the dashboard and fully reproducible for a given run",
      btnRepPreview: "Preview note",
      btnRepPdf: "Export PDF",
      repExporting: "Rendering PDF…",
      repExportFail: "PDF export failed",
      repPreviewFail: "Could not open the note preview",
      repMeta: "Generated",
      repFootNote: "Generated locally from public historical data; all conclusions are reproducible",
      repOutline1: "Research summary",
      repOutline2: "Methodology and rules",
      repOutline3: "Backtest findings",
      repOutline4: "Industry-level backtest",
      repOutline5: "Multi-timeframe resonance",
      repOutline6: "Candidate pool analysis",
      repOutline7: "Conclusions and outlook",
      repOutline8: "Limitations and risks",
      repOutline9: "Compliance statement",
    },
  };

  /* =======================================================================
     二、行业 / 板块 / 指数 标准英文名称
     -----------------------------------------------------------------------
     行业口径固定为申万一级行业（31 个），英文采用申万行业通用译法；
     板块与指数使用市场通用英文名称。均以中文名为键，保证前后端数据
     传输口径不变（接口仍传中文，仅前端展示时翻译）。
     ======================================================================= */
  const SW_INDUSTRY_EN = {
    "农林牧渔": "Agriculture, Forestry & Fishery",
    "基础化工": "Basic Chemicals",
    "钢铁": "Steel",
    "有色金属": "Non-ferrous Metals",
    "电子": "Electronics",
    "家用电器": "Home Appliances",
    "食品饮料": "Food & Beverage",
    "纺织服饰": "Textiles & Apparel",
    "轻工制造": "Light Industry",
    "医药生物": "Pharmaceuticals & Biotech",
    "公用事业": "Utilities",
    "交通运输": "Transportation",
    "房地产": "Real Estate",
    "商贸零售": "Commerce & Retail",
    "社会服务": "Social Services",
    "综合": "Conglomerates",
    "建筑材料": "Building Materials",
    "建筑装饰": "Construction & Decoration",
    "电力设备": "Power Equipment",
    "国防军工": "Defense & Military",
    "计算机": "Computer",
    "传媒": "Media",
    "通信": "Communication",
    "银行": "Banks",
    "非银金融": "Non-bank Financials",
    "汽车": "Automobiles",
    "机械设备": "Machinery",
    "煤炭": "Coal",
    "石油石化": "Petroleum & Petrochemicals",
    "环保": "Environmental Protection",
    "美容护理": "Beauty & Personal Care",
    "其他": "Others",
  };

  const BOARD_EN = {
    "沪主板": "SSE Main Board",
    "深主板": "SZSE Main Board",
    "创业板": "ChiNext",
    "科创板": "STAR Market",
    "其他": "Others",
  };

  const INDEX_EN = {
    "上证指数": "SSE Composite",
    "深证成指": "SZSE Component",
    "创业板指": "ChiNext Index",
    "科创50": "STAR 50",
    "纳斯达克": "Nasdaq Composite",
    "标普500": "S&P 500",
    "恒生指数": "Hang Seng Index",
  };

  /* =======================================================================
     三、运行时
     ======================================================================= */
  let current = readStored();
  const listeners = [];

  function readStored() {
    try {
      const v = global.localStorage.getItem(STORAGE_KEY);
      if (SUPPORTED.indexOf(v) >= 0) return v;
    } catch (e) { /* 隐私模式等场景下 localStorage 不可用，回退默认语言 */ }
    // 默认简体中文：本工具面向 A 股研究场景，未设置偏好时不擅自切换语言
    return "zh";
  }

  function getLang() {
    return current;
  }

  /** 取词：支持 {name} 占位符替换，缺失的词条回退中文再回退键名 */
  function t(key, vars) {
    const pack = DICT[current] || DICT.zh;
    let s = pack[key];
    if (s === undefined) s = DICT.zh[key];
    if (s === undefined) return key;
    if (vars) {
      s = s.replace(/\{(\w+)\}/g, (m, k) => (vars[k] === undefined ? m : String(vars[k])));
    }
    return s;
  }

  /** 行业中文名 → 当前语言的展示名（未知行业原样返回，不做兜底猜测） */
  function industry(name) {
    if (!name) return name;
    if (current === "zh") return name;
    return SW_INDUSTRY_EN[name] || name;
  }

  /** 板块中文名 → 当前语言的展示名 */
  function board(name) {
    if (!name) return name;
    if (current === "zh") return name;
    return BOARD_EN[name] || name;
  }

  /** 指数中文名 → 当前语言的展示名（行情接口仍按中文名匹配） */
  function indexName(name) {
    if (!name) return name;
    if (current === "zh") return name;
    return INDEX_EN[name] || name;
  }

  /** 批量替换静态文案（textContent / placeholder / title / aria-label） */
  function applyStatic(root) {
    const scope = root || global.document;
    scope.querySelectorAll("[data-i18n]").forEach((el) => {
      const key = el.getAttribute("data-i18n");
      const html = DICT[current] && DICT[current][key];
      if (html !== undefined) el.textContent = html;
      else if (DICT.zh[key] !== undefined) el.textContent = DICT.zh[key];
    });
    scope.querySelectorAll("[data-i18n-ph]").forEach((el) => {
      el.setAttribute("placeholder", t(el.getAttribute("data-i18n-ph")));
    });
    scope.querySelectorAll("[data-i18n-title]").forEach((el) => {
      el.setAttribute("title", t(el.getAttribute("data-i18n-title")));
    });
    scope.querySelectorAll("[data-i18n-aria]").forEach((el) => {
      el.setAttribute("aria-label", t(el.getAttribute("data-i18n-aria")));
    });
  }

  /** 语言相关的文档级属性（lang / title / body 标记） */
  function applyDocument() {
    const doc = global.document;
    doc.documentElement.setAttribute("lang", t("htmlLang"));
    doc.title = t("docTitle");
    doc.body.setAttribute("data-lang", current);
  }

  function notify() {
    for (const fn of listeners) {
      try { fn(current); } catch (e) { /* 单个监听器异常不影响其它模块重绘 */ }
    }
  }

  /** 切换语言：无刷新、写入本地偏好、通知各模块重绘 */
  function setLang(lang) {
    if (SUPPORTED.indexOf(lang) < 0 || lang === current) return;
    current = lang;
    try { global.localStorage.setItem(STORAGE_KEY, lang); } catch (e) { /* 忽略写入失败 */ }
    applyDocument();
    applyStatic();
    notify();
  }

  /** 注册语言变化监听器，返回取消注册的函数 */
  function onChange(fn) {
    listeners.push(fn);
    return () => {
      const i = listeners.indexOf(fn);
      if (i >= 0) listeners.splice(i, 1);
    };
  }

  /* 首屏：脚本置于 body 末尾，DOM 已就绪，立即应用一次静态文案 */
  applyDocument();
  applyStatic();

  global.I18N = {
    STORAGE_KEY,
    SUPPORTED,
    getLang,
    setLang,
    t,
    industry,
    board,
    indexName,
    applyStatic,
    onChange,
  };
})(window);
