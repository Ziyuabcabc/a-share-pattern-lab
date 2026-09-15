# -*- coding: utf-8 -*-
"""提交 v1.2.0 到 git。用完即删。"""
import subprocess

GIT = r"C:/Users/L1362/.workbuddy/binaries/PortableGit/versions/1.2.0/cmd/git.exe"


def run(*args, **kw):
    r = subprocess.run([GIT, *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", **kw)
    return r


msg = """feat: 阶段2 前端看板整体优化 + 详情图表渲染修复（v1.2.0）

一、遗留问题修复
- 修复个股明细抽屉「明细数据加载失败」：根因为 ECharts 多 grid 布局下
  xAxis 未声明 gridIndex，坐标轴与网格错配抛错并被 try/catch 吞掉；
  现 xAxis/yAxis 均显式声明 gridIndex，K线/成交量/MACD/KDJ 四区正常渲染
- 详情接口 /api/pool/{code} 随附 profile 与 score（三模块得分与逐项明细），
  抽屉展示不再依赖列表页缓存；行业口径为申万一级，候选池「其他」占比 0%

二、阶段2 布局优化
- 顶部导航区：项目名 + 本地运行版副标题 + 最新批次时间 + 扫描/导出按钮
  + 通栏红色免责声明
- 宏观参考面板：国内 4 大指数（点位+涨跌幅）/ 海外港股 3 指数（仅涨跌幅）
  双行布局，底部统一备注
- 统计概览卡四档配色（总候选/高匹配/中匹配/预筛后处理）
- 候选池表格统一行高间距、指标命中标签视觉精修，筛选搜索排序与行点击保留
- 行业分布图：申万一级口径、渐变条形、数值标签与占比提示

三、文档与合规
- README：新增界面预览（3 张全屏截图）、板块扫描独立使用说明、版本历史
- docs/project_report.md：新增前端实现要点与多维 grid 渲染约束说明
- 措辞自检词表扩充交易导向类表述；全量自检通过；测试 42 项全绿
"""

print("== 变更清单 ==")
run("add", "-A")
r = run("status", "--short")
print(r.stdout.strip()[:2000] or "(无变更)")

r = run("-c", "user.name=a-share-pattern-lab", "-c", "user.email=lab@local.dev",
        "commit", "-m", msg)
print("\ncommit exit:", r.returncode)
print((r.stdout or r.stderr)[:400])

r = run("log", "--oneline", "-4")
print("\n", r.stdout)
