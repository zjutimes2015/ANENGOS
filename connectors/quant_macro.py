"""宏观传感器连接器（第 15 个 agent / Tavily 数据源）。

实现「全球宏观对冲基金经理」周报——用 Tavily API 联网搜索五类核心内容，
每周生成《全球市场核心交易策略周报》：
  1. 宏观日历（CPI / PCE / GDP / 非农 / FOMC）
  2. 地缘与政治事件
  3. 企业财报（科技七巨头 / 半导体 / 能源）
  4. 市场情绪（VIX / Crypto 贪婪恐慌指数 / A股风险偏好）
  5. 资产表现（美元指数 / 美债收益率 / 黄金 / 原油 / 主要股指）

按 5 层 AI loop 抽象：
  - 传感器层：Tavily API（为 AI agent 优化的联网搜索，返回去广告的结构化结果+RAG评分）
  - 决策层：逻辑链推演（关键词证据 → 方向判断）→ 结构化操作建议
  - 工具层：REST API（urllib，无第三方依赖，容器内直接可用）
  - 质量门：来源去重、每类覆盖下限、时效标注（尽量近 7 天）
  - 学习层：weekly_macro_YYYYWW.md 沉淀，供 retro_loop / paper_trader 作宏观上下文

API Key 从环境变量 TAVILY_API_KEY 读取（云上 .env 注入，不上传任何凭证到仓库）。
"""

from __future__ import annotations

import datetime
import json
import os
import re
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

from connectors.base import AgentAdapter, AgentResult, _now_iso

TAVILY_URL = "https://api.tavily.com/search"

# 五类传感器查询（中英混合，每类 2 条，周报场景覆盖面）
SENSOR_QUERIES: dict[str, list[str]] = {
    "宏观日历": [
        "本周 美国 经济数据 日历 CPI PCE GDP 非农 发布 预告",
        "FOMC 美联储 利率决议 最新 表态 2026年10月",
    ],
    "地缘与政治": [
        "本周 地缘政治 中东 俄乌 贸易关税 最新事件 本周",
        "global geopolitical risk news this week",
    ],
    "企业财报": [
        "科技七巨头 财报 微软 英伟达 苹果 亚马逊 最新业绩",
        "semiconductor chip earnings Nvidia TSMC this week",
    ],
    "市场情绪": [
        "VIX 恐慌指数 美股 市场情绪 最新",
        "crypto fear greed index bitcoin sentiment",
        "VIX index stock market sentiment S&P 500 this week",
    ],
    "资产表现": [
        "美元指数 美债收益率 黄金 原油 本周 走势 分析",
        "US dollar index treasury yield gold oil weekly outlook",
    ],
}


def _tavily_search(query: str, max_results: int = 5, api_key: str | None = None, days: int = 14) -> list[dict]:
    """调用 Tavily /search（REST，无第三方依赖）。days 限制时间窗，保证证据时效性。"""
    key = api_key or os.environ.get("TAVILY_API_KEY", "")
    if not key:
        return []
    body = json.dumps({
        "query": query,
        "max_results": max_results,
        "search_depth": "basic",
        "include_answer": False,
        "days": days,
    }).encode("utf-8")
    req = urllib.request.Request(
        TAVILY_URL, data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        return d.get("results", [])
    except Exception:
        return []


def _dedup(results: list[dict], max_n: int = 5) -> list[dict]:
    """按 URL 去重 + 按相关性评分排序 + 过滤常青工具页（Dashboard/Monitor/日历/指数页等）。

    互审 58 分意见：主题索引页/工具页不是"本周事件证据"，对周报价值低。
    """
    evergreen = re.compile(
        r"dashboard|monitor|homepage|calendar|index\.html|tradingview\.com/ideas|"
        r"coinmarketcap|fear and greed|geopolitical futures|risk dashboard|"
        r"\.html$|live market", re.IGNORECASE,
    )
    seen: set[str] = set()
    out: list[dict] = []
    for r in sorted(results, key=lambda x: -float(x.get("score", 0.0))):
        u = r.get("url", "")
        if not u or u in seen:
            continue
        title = (r.get("title", "") or "")
        # 常青页/工具页：标题或 URL 命中则跳过（保留 max_n 之外的新闻型结果时不受影响）
        if evergreen.search(u) or evergreen.search(title):
            continue
        seen.add(u)
        out.append(r)
        if len(out) >= max_n:
            break
    return out


def _week_label() -> str:
    now = datetime.datetime.now()
    return f"{now.isocalendar().year}W{now.isocalendar().week:02d}"


def _publish_hint(r: dict) -> str:
    """从标题/内容提取时效线索（尽力而为，不给编造日期）。"""
    t = (r.get("title", "") or "") + (r.get("content", "") or "")[:200]
    m = re.search(r"(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})", t)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return "（发布时间未知）"


def _direction_analysis(all_text: str) -> list[str]:
    """启发式逻辑链：从证据文本提取关键信号，输出方向判断（确定性、可复现）。"""
    signals = []
    checks = [
        (r"加息|升息|紧缩|鹰派", "货币紧缩（加息/鹰派）信号 → 压制风险资产估值，利好美元/美债收益率"),
        (r"降息|宽松|鸽派", "货币宽松（降息/鸽派）信号 → 利好风险资产，压制美元"),
        (r"通胀|CPI|PCE", "通胀数据受到关注 → 关注实际数值与预期差对利率路径的扰动"),
        (r"衰退|下行|放缓", "增长放缓/衰退担忧 → 防御配置（债券/黄金）相对占优"),
        (r"财报.*(超预期|beat|击破预期)|超预期", "财报超预期 → 盈利支撑的板块维持强势"),
        (r"不及预期|miss|低于预期", "财报不及预期 → 相关板块存在回撤风险"),
        (r"VIX|恐慌", "波动率/恐慌指数被提及 → 注意情绪极端值对应的逆向机会"),
        (r"关税|制裁|贸易", "地缘/贸易摩擦 → 关注受影响行业的政策敏感度"),
        (r"油价|原油|能源", "能源价格波动 → 传导至通胀与石油板块盈利"),
        (r"黄金|避险", "避险资产走强 → 风险偏好回落信号"),
    ]
    for pat, desc in checks:
        if re.search(pat, all_text, re.IGNORECASE):
            signals.append(desc)
    # 去重保序
    seen = set()
    return [s for s in signals if not (s in seen or seen.add(s))]


def _build_report(sensor: dict[str, list[dict]], coverage: dict[str, int]) -> str:
    week = _week_label()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# 全球市场核心交易策略周报（{week}）",
        f"- 生成时间：{now}（UTC+8）| 数据源：Tavily API 联网搜索（每周 1 次，10 credits/次）",
        f"- 覆盖：宏观日历 / 地缘政治 / 企业财报 / 市场情绪 / 资产表现 五类传感器",
        "",
    ]
    all_text = ""
    for cat, qs in SENSOR_QUERIES.items():
        results = sensor.get(cat, [])
        lines.append(f"## {cat}（{len(results)} 条）")
        if not results:
            lines.append("- ⚠️ 覆盖不足：本次未取到该主题有效结果，下周将自动重试")
        for r in results:
            title = (r.get("title", "") or "").strip()
            url = r.get("url", "")
            hint = _publish_hint(r)
            score = r.get("score")
            score_s = f"，相关性 {score:.2f}" if isinstance(score, (int, float)) else ""
            lines.append(f"- [{title}]({url})（{hint}{score_s}）")
            all_text += f" {title} {r.get('content', '')[:200]}"
        lines.append("")
    lines.append("## 逻辑链推演（确定性规则，基于上述证据文本）")
    signals = _direction_analysis(all_text)
    if signals:
        lines.extend(f"- {s}" for s in signals)
    else:
        lines.append("- 本周未捕捉到强方向信号，维持中性观察")
    lines.append("")
    lines.append("## 结构化操作建议（对 ANENGOS 量化链路的含义）")
    lines.append("- 宏观传感器 → retro_loop：本报告将作为下周复盘的市场上下文，纳入改进项判断")
    lines.append("- 方向信号 → paper_trader：风险偏好走弱时降低新开仓意愿，避险资产走强时关注防御板块")
    lines.append("- 财报/地缘 → quant_all_a 趋势池：规避财报暴雷窗口期的高波动标的")
    lines.append("")
    lines.append("## 覆盖质量")
    for cat, n in coverage.items():
        lines.append(f"- {cat}：{n} 条")
    lines.append("")
    return "\n".join(lines)


class QuantMacroAdapter(AgentAdapter):
    name = "quant_macro"

    def __init__(self) -> None:
        self.mode = "tavily" if os.environ.get("TAVILY_API_KEY") else "unconfigured"

    def submit(self, task: str, workspace: str) -> AgentResult:
        task = (task or "").strip()
        ws = Path(workspace)
        out_dir = ws / "quant_macro_output"
        out_dir.mkdir(parents=True, exist_ok=True)

        key = os.environ.get("TAVILY_API_KEY", "")
        if not key:
            return AgentResult(False, self.name, "unconfigured",
                               "[quant_macro] TAVILY_API_KEY 未配置：请在云上 .env 添加后再运行",
                               artifact_paths=[], error="TAVILY_API_KEY missing")

        # 1. 传感器层：五类搜索（每类 2 查询 × 各取 4 条，合并去重取 5，限 14 天时间窗保证时效）
        sensor: dict[str, list[dict]] = {}
        coverage: dict[str, int] = {}
        for cat, qs in SENSOR_QUERIES.items():
            pooled: list[dict] = []
            for q in qs:
                pooled.extend(_tavily_search(q, max_results=4, api_key=key, days=14))
            sensor[cat] = _dedup(pooled, max_n=5)
            coverage[cat] = len(sensor[cat])

        # 2. 决策层 + 学习层：生成周报
        week = _week_label()
        report = _build_report(sensor, coverage)
        out_path = out_dir / f"weekly_macro_{week}.md"
        out_path.write_text(report, encoding="utf-8")

        # 3. 结构化摘要（供互审监督核验）
        total = sum(coverage.values())
        summary = [
            f"[{self.name}/tavily] 全球宏观周报 {week}：{total} 条证据（每类覆盖见下）",
            "## 覆盖质量",
        ]
        summary += [f"- {cat}：{n} 条" for cat, n in coverage.items()]
        summary.append("## 逻辑链推演")
        signals = _direction_analysis(
            " ".join(r.get("title", "") + " " + r.get("content", "")[:200]
                     for rs in sensor.values() for r in rs))
        summary += [f"- {s}" for s in (signals or ["本周未捕捉到强方向信号，维持中性观察"])]
        summary.append("## 各主题最新证据")
        for cat, rs in sensor.items():
            summary.append(f"- {cat}：")
            for r in rs[:3]:
                sc = f"，相关性 {float(r.get('score', 0)):.2f}" if isinstance(r.get("score"), (int, float)) else ""
                summary.append(f"  - {r.get('title', '')[:60]}（{_publish_hint(r)}{sc}）")
        summary.append(f"- 完整周报：{out_path.relative_to(workspace)}")
        return AgentResult(True, self.name, "done", "\n".join(summary),
                           artifact_paths=[str(out_path.relative_to(workspace))],
                           meta={"mode": self.mode, "coverage": coverage, "ts": _now_iso()})

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "ready": bool(os.environ.get("TAVILY_API_KEY")), "mode": self.mode}

    def describe(self) -> dict[str, Any]:
        return {"summary": "宏观传感器：Tavily 联网搜索五类主题（宏观日历/地缘/财报/情绪/资产），"
                           "每周生成《全球市场核心交易策略周报》供 retro_loop 引用",
                "params": {"task": {"type": "string", "description": "留空跑默认周报"}}}
