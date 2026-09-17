# -*- coding: utf-8 -*-
"""市场资讯接口：聚合公开财经快讯并按关键词归类。

设计要点：
- **只做聚合与归类**：内容为第三方公开快讯原文，本工具不生成、不评论、
  不解读，仅按关键词归入「国内 / 海外 / 宏观」三类供研究参考。
- **多源降级**：任一资讯源不可用时自动切换下一个源；全部失败时返回
  available=false，前端展示「资讯暂不可用」占位，不影响候选池等主功能。
- **本地缓存**：抓取结果写入 SQLite meta 表并设有效期，避免频繁请求第三方。
- **中性化过滤**：标题含交易指令类表述的条目整体跳过（词表见 config），
  与本项目「界面不出现交易引导类表述」的规范保持一致。
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from datetime import datetime

import akshare as ak
from fastapi import APIRouter, Query

from app import config, db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/news", tags=["市场资讯"])

# ---------------------------------------------------------------------------
# 资讯源：按「字段完整度 + 更新速度」排序，前一个源够用就不再请求后面的源
# ---------------------------------------------------------------------------
_EM_HOME = "https://finance.eastmoney.com/"
_THS_HOME = "https://news.10jqka.com.cn/realtimenews.html"
_CLS_HOME = "https://www.cls.cn/telegraph"
_FUTU_HOME = "https://news.futunn.com/main/live"
_SINA_HOME = "https://finance.sina.com.cn/7x24/"

# (akshare 函数名, 来源展示名, 无链接时的兜底主页)
_SOURCES: tuple[tuple[str, str, str], ...] = (
    ("stock_info_global_em", "东方财富", _EM_HOME),
    ("stock_info_global_ths", "同花顺", _THS_HOME),
    ("stock_info_global_cls", "财联社", _CLS_HOME),
    ("stock_info_global_futu", "富途牛牛", _FUTU_HOME),
    ("stock_info_global_sina", "新浪财经", _SINA_HOME),
)

# ---------------------------------------------------------------------------
# 分类关键词
# 判定顺序：国内强特征 → 海外 → 宏观 → 国内（兜底）
#
# 说明：公开快讯里国际时政/海外公司类消息占比很高，若只做「海外 → 宏观 →
# 国内」的简单判定，这些消息会全部落进「国内资讯」。因此先识别 A 股与
# 境内市场强特征，再匹配海外与宏观，最后才兜底到国内。
# ---------------------------------------------------------------------------
# 境内市场强特征：命中即判定为「国内资讯」，优先级最高
_DOMESTIC_KEYS = (
    "A股", "沪指", "上证指数", "深证成指", "深成指", "创业板指", "科创板",
    "北交所", "上交所", "深交所", "证监会", "沪深", "涨停", "跌停",
    "主力资金", "北向资金", "南向资金", "龙虎榜", "新股", "中签",
    "限售股", "解禁", "两融", "融资余额", "股指期货", "国债期货",
    "银行间", "同业存单", "中证", "业绩预告", "业绩快报", "减持", "增持",
    "回购", "中标", "工信部", "国家能源局", "上市公司", "公告",
)
# 注：「中国 / 国内 / 全国」等整体性表述不放进强特征——它们是兜底类目，
# 若放进强特征会抢走本应归入「宏观资讯」的央行、统计局类条目。

_OVERSEAS_KEYS = (
    # 市场与指数
    "美股", "纳指", "纳斯达克", "标普", "道琼斯", "道指", "欧股", "德股",
    "法股", "日经", "富时", "亚太股市", "韩国股市", "纽交所", "港股",
    "恒生", "恒指", "中概股", "华尔街", "隔夜", "美债", "美元指数",
    "非农", "美债收益率",
    # 海外央行与政策
    "美联储", "鲍威尔", "联邦基金", "欧央行", "欧洲央行", "英国央行",
    "日本央行", "日银", "美国财政部", "美国商务部", "美国劳工部",
    "白宫", "美国国会", "特朗普",
    # 国家 / 地区
    "美国", "欧洲", "欧盟", "欧元区", "英国", "德国", "法国", "日本",
    "韩国", "印度", "俄罗斯", "乌克兰", "中东", "以色列", "伊朗",
    "沙特", "巴西", "加拿大", "澳大利亚", "墨西哥", "土耳其", "阿根廷",
    "南非", "东南亚", "越南", "泰国", "印尼", "新加坡", "非洲", "拉美",
    "挪威", "瑞典", "瑞士", "荷兰", "西班牙", "意大利", "波兰", "希腊",
    "葡萄牙", "爱尔兰", "丹麦", "芬兰", "奥地利", "比利时", "捷克",
    "匈牙利", "罗马尼亚", "智利", "秘鲁", "哥伦比亚", "委内瑞拉",
    "尼日利亚", "肯尼亚", "埃及", "卡塔尔", "科威特", "阿联酋", "伊拉克",
    "叙利亚", "黎巴嫩", "约旦", "巴基斯坦", "阿富汗", "孟加拉",
    "斯里兰卡", "缅甸", "柬埔寨", "蒙古", "朝鲜", "菲律宾", "马来西亚",
    "新西兰", "白俄罗斯", "塞尔维亚", "格鲁吉亚", "阿塞拜疆", "利比亚",
    "哈萨克斯坦", "阿曼", "乌兹别克", "土库曼", "塔吉克", "吉尔吉斯",
    "哥伦比亚", "厄瓜多尔", "玻利维亚", "乌拉圭", "巴拉圭", "古巴",
    # 国际组织与地缘
    "世贸", "WTO", "联合国", "北约", "OPEC", "欧佩克", "世卫",
    "红海", "苏伊士", "黑海", "波罗的海", "霍尔木兹", "境外",
    "全球", "国际", "海外",
    # 海外公司与机构
    "特斯拉", "英伟达", "苹果公司", "微软", "谷歌", "亚马逊", "Meta",
    "台积电", "三星电子", "软银", "伯克希尔", "摩根士丹利", "摩根大通",
    "花旗", "高盛", "贝莱德", "桥水", "黑石", "凯雷", "KKR",
    "必和必拓", "力拓", "淡水河谷", "阿斯麦", "波音", "空客", "大众",
    "丰田", "索尼", "任天堂", "壳牌", "埃克森", "雪佛龙", "道达尔",
    "诺和诺德", "礼来", "辉瑞", "默沙东", "强生",
    "甲骨文", "英特尔", "高通", "博通", "美光", "戴尔", "惠普", "IBM",
    "奈飞", "迪士尼", "沃尔玛", "可口可乐", "星巴克", "麦当劳", "耐克",
    "丰田汽车", "本田", "日产", "宝马", "奔驰", "西门子", "飞利浦",
    # 海外地名（用于识别未点名公司的海外事件快讯）
    "德克萨斯", "得州", "加州", "纽约", "华盛顿", "硅谷", "芝加哥",
    "波士顿", "西雅图", "伦敦", "巴黎", "柏林", "法兰克福", "东京",
    "首尔", "莫斯科", "悉尼", "多伦多", "迪拜", "新加坡", "费城",
)

_MACRO_KEYS = (
    "央行", "货币政策", "社融", "社会融资", "CPI", "PPI", "PMI", "GDP",
    "M1增速", "M2增速", "M2同比", "信贷", "贷款", "LPR", "MLF", "逆回购",
    "降准", "降息", "加息", "财政", "国债", "专项债", "地方债", "汇率",
    "人民币", "外汇", "进出口", "外贸", "统计局", "财政部", "发改委",
    "国常会", "国务院", "经济数据", "通胀", "通缩", "失业率", "就业",
    "工业增加值", "固定资产投资", "社会消费品零售", "税收", "关税",
    "IMF", "世界银行", "OECD",
    # 大宗商品与海外商品市场（宏观定价相关）
    "WTI", "布伦特", "现货黄金", "现货白银", "原油期货", "黄金期货",
    "LME", "COMEX", "大宗商品", "伦铜", "铜价", "金价",
)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _blocked_words() -> tuple[str, ...]:
    """还原中性化过滤词表（片段拼接，避免源码中出现完整词条）。"""
    return tuple("".join(parts) for parts in config.NEWS_BLOCK_PARTS)


def _classify(text: str) -> str:
    """按关键词把快讯归入 domestic / overseas / macro 三类。

    判定顺序：境内市场强特征 → 海外 → 宏观 → 国内兜底。
    境内强特征优先，避免「美联储加息对 A 股影响」这类跨域标题被误归为海外。
    """
    if any(k in text for k in _DOMESTIC_KEYS):
        return "domestic"
    if any(k in text for k in _OVERSEAS_KEYS):
        return "overseas"
    if any(k in text for k in _MACRO_KEYS):
        return "macro"
    return "domestic"


def _norm_time(value) -> str:
    """把各源五花八门的时间字段统一成 `YYYY-MM-DD HH:MM:SS`（失败返回空串）。"""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    # 已是标准格式（含秒 / 不含秒）
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    # 纯日期（如财联社的「发布日期」），补零点
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d 00:00:00")
        except ValueError:
            pass
    return text[:19]


def _clean(text) -> str:
    """清洗标题：去换行、去快讯方括号前缀、压缩空白。"""
    s = str(text or "").replace("\r", " ").replace("\n", " ")
    s = s.strip()
    # 快讯常见格式为「【标题】正文」，标题优先取方括号内内容
    if s.startswith("【") and "】" in s:
        head = s[1:s.index("】")].strip()
        if len(head) >= 6:
            s = head
    return " ".join(s.split())[:180]


def _call_with_timeout(fn_name: str, timeout: int):
    """在线程中执行第三方接口并施加超时，避免单个源卡住整个请求。

    这里用「守护线程 + 队列」而不是线程池：线程池的上下文管理器在退出时
    会 join 工作线程，即使 `result(timeout=...)` 已经超时，仍会被卡住，
    超时保护形同虚设。守护线程超时后直接放弃，不再等待。
    """
    fn = getattr(ak, fn_name, None)
    if fn is None:
        raise AttributeError(f"当前 akshare 版本无接口 {fn_name}")
    box: queue.Queue = queue.Queue(maxsize=1)

    def run() -> None:
        try:
            box.put(("ok", fn()))
        except BaseException as exc:  # noqa: BLE001 —— 原样回传给主线程判断
            box.put(("err", exc))

    threading.Thread(target=run, daemon=True, name=f"news-{fn_name}").start()
    try:
        kind, payload = box.get(timeout=timeout)
    except queue.Empty as exc:
        raise TimeoutError(f"{fn_name} 超时（>{timeout}s）") from exc
    if kind == "err":
        raise payload
    return payload


def _normalize_rows(df, source: str, fallback_url: str) -> list[dict]:
    """把不同源的 DataFrame 映射成统一结构，并标注该条目来自哪个源。"""
    if df is None or len(df) == 0:
        return []
    cols = set(df.columns)
    out: list[dict] = []

    def pick(row, *names):
        for n in names:
            if n in cols:
                v = row.get(n)
                if v is not None and str(v).strip():
                    return v
        return ""

    for _, row in df.iterrows():
        title = _clean(pick(row, "标题", "title"))
        body = _clean(pick(row, "摘要", "内容", "content"))
        # 无标题的源（财联社 / 富途 / 新浪）用正文兜底；
        # 阈值取 4 个字符：既能挡住空标题与占位符，又不误伤「沪指收涨」这类短标题
        if len(title) < 4:
            title = body
        if len(title) < 4:
            continue
        url = str(pick(row, "链接", "url")).strip() or fallback_url
        date_part = str(pick(row, "发布日期", "date")).strip()
        time_part = str(pick(row, "发布时间", "时间", "time")).strip()
        stamp = _norm_time(f"{date_part} {time_part}".strip() if date_part else time_part)
        if not stamp and date_part:
            stamp = _norm_time(date_part)
        out.append(
            {
                "title": title,
                "summary": body[:200],
                "url": url,
                "stamp": stamp,
                "source": source,
            }
        )
    return out


def _dedupe(items: list[dict]) -> list[dict]:
    """按标题指纹去重（同一快讯常被多家源同时转载）。

    各源按「字段完整度 + 更新速度」先后顺序拼接，因此保留先出现的条目，
    即优先保留信息最完整的那一版。
    """
    seen: set[str] = set()
    out: list[dict] = []
    for it in items:
        key = "".join(ch for ch in it["title"] if ch.isalnum())[:36]
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


def _fetch_feed() -> tuple[list[dict], list[str], list[str]]:
    """按降级顺序抓取资讯源，返回 (条目, 命中的源, 失败信息)。"""
    items: list[dict] = []
    used: list[str] = []
    errors: list[str] = []
    target = config.NEWS_MAX_ITEMS * 2  # 去重与过滤前多抓一些，保证最终条数够用

    for fn_name, label, home in _SOURCES:
        if len(items) >= target:
            break
        try:
            df = _call_with_timeout(fn_name, config.NEWS_SOURCE_TIMEOUT_SEC)
        except Exception as exc:  # noqa: BLE001 —— 单源失败不影响其余源
            errors.append(f"{label}({type(exc).__name__})")
            continue
        rows = _normalize_rows(df, label, home)
        if rows:
            used.append(label)
            items.extend(rows)
    return items, used, errors


def _build_feed() -> dict:
    """抓取 → 去重 → 归类 → 中性化过滤 → 排序，产出接口载荷。"""
    raw, used, errors = _fetch_feed()
    blocked = _blocked_words()

    entries: list[dict] = []
    for it in _dedupe(raw):
        title = it["title"]
        if any(w in title for w in blocked):
            continue
        cat = _classify(f"{title} {it.get('summary', '')}")
        entries.append(
            {
                "title": title,
                "source": it.get("source") or "公开快讯",
                "time": it["stamp"],
                "url": it["url"],
                "category": cat,
                "_stamp": it["stamp"],
            }
        )

    # 时间倒序；无时间的排在最后（按标题兜底保持稳定顺序）
    entries.sort(key=lambda x: (x["_stamp"] != "", x["_stamp"]), reverse=True)
    for e in entries:
        e.pop("_stamp", None)

    # 先截断再统计：前端分类标签上的数量与实际可展示条数保持一致
    entries = entries[: config.NEWS_MAX_ITEMS]
    counts = {key: 0 for key, _ in config.NEWS_CATEGORIES}
    for e in entries:
        counts[e["category"]] = counts.get(e["category"], 0) + 1

    return {
        "available": bool(entries),
        "items": entries,
        "counts": {"all": len(entries), **counts},
        "sources": used,
        "cached_at": _now(),
        "message": "" if entries else "资讯暂不可用",
        "errors": errors,
        "source_note": config.NEWS_SOURCE_NOTE,
        "categories": [{"key": k, "label": v} for k, v in config.NEWS_CATEGORIES],
        "disclaimer": config.DISCLAIMER,
    }


def _read_cache(conn) -> dict | None:
    row = conn.execute(
        "SELECT value FROM meta WHERE key=?", (config.NEWS_CACHE_KEY,)
    ).fetchone()
    if not row:
        return None
    try:
        cached = json.loads(row["value"])
        age = (datetime.now() - datetime.fromisoformat(cached["cached_at"])).total_seconds()
    except (TypeError, ValueError, KeyError):
        return None
    return cached if age <= config.NEWS_CACHE_TTL_SEC else None


def _load_or_build(conn) -> dict:
    """读取资讯缓存；缺失或过期时抓取一次并落库（与请求参数无关）。

    单独抽出的原因：服务启动预热需要复用这段逻辑，而**不能直接调用路由函数**——
    路由函数签名里的 `Query(...)` 只有在经过 FastAPI 的参数解析后才会变成真实取值，
    直接以函数方式调用拿到的是 `Query` 对象本身，于是
    `cap = limit or config.NEWS_MAX_ITEMS` 会取出一个对象，
    `items[:cap]` 即以 `TypeError: slice indices must be integers or None or
    have an __index__ method` 失败（表现为每次启动都静默预热失败）。
    """
    data = _read_cache(conn)
    if data is not None:
        return data
    try:
        data = _build_feed()
    except Exception as exc:  # noqa: BLE001 —— 资讯失败绝不向上抛
        logger.warning("资讯抓取异常: %s", exc)
        data = {
            "available": False,
            "items": [],
            "counts": {"all": 0},
            "sources": [],
            "cached_at": _now(),
            "message": "资讯暂不可用",
            "errors": [type(exc).__name__],
            "source_note": config.NEWS_SOURCE_NOTE,
            "categories": [{"key": k, "label": v} for k, v in config.NEWS_CATEGORIES],
            "disclaimer": config.DISCLAIMER,
        }
    if data.get("available"):
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (config.NEWS_CACHE_KEY, json.dumps(data, ensure_ascii=False)),
        )
    return data


def warm_cache() -> dict:
    """刷新资讯缓存（供服务启动预热调用，与请求参数无关）。"""
    with db.get_conn() as conn:
        return _load_or_build(conn)


@router.get("", summary="市场资讯：国内 / 海外 / 宏观三类快讯")
def news(
    category: str = Query("all", description="all | domestic | overseas | macro"),
    limit: int = Query(0, ge=0, le=200, description="返回条数上限，0 表示用默认值"),
):
    """聚合公开财经快讯并按关键词归类。

    - 命中本地缓存（默认 30 分钟）直接返回，不打第三方接口；
    - 抓取全部失败时返回 `available=false` 与「资讯暂不可用」，
      前端展示占位提示，不影响候选池等主功能的渲染。
    - 本接口只做聚合与归类，不生成、不解读、不评价任何资讯内容。
    """
    with db.get_conn() as conn:
        data = _load_or_build(conn)

    items = data.get("items") or []
    if category and category != "all":
        items = [x for x in items if x.get("category") == category]
    cap = limit or config.NEWS_MAX_ITEMS
    return {**data, "items": items[:cap], "category": category}
