"""quant_news.py: 新闻/大V情绪因子（ANENGOS版）。

数据源：
- AKShare news_cctv（央视财经，政策信号）
- AKShare stock_news_em（东方财富个股新闻）

处理：
- 爬新闻 → 关键词分类（利好/利空/中性）
- JEV式规则判断（不用Claude，零成本）
- 输出每日情绪评分

输出：
- workspace/news_daily.json（每日新闻 + 情绪评分）
- 可直接接 feishu_push 写飞书表
"""
from __future__ import annotations
import datetime
import json
import re
from pathlib import Path

WS = Path("/srv/anengos/workspace")
OUT = WS / "news_daily.json"

# 关键词词典（简化版，可扩充）
BULLISH_KEYWORDS = [
    "降准", "降息", "利好", "增持", "回购", "减税", "补贴", "刺激",
    "宽松", "支持", "鼓励", "改革", "开放", "复苏", "增长", "超预期",
    "政策利好", "稳增长", "扩内需", "新质生产力", "数字化", "新能源",
]
BEARISH_KEYWORDS = [
    "加息", "降准", "利空", "减持", "处罚", "收紧", "监管", "打压",
    "暴跌", "风险", "违约", "退市", "亏损", "下调", "管制", "限制",
    "房地产下行", "债务风险", "通胀", "收缩", "不及预期",
]

# 板块关键词
SECTOR_MAP = {
    "新能源": ["光伏", "风电", "锂电", "新能源车", "储能", "氢能"],
    "AI/算力": ["人工智能", "AI", "算力", "大模型", "芯片", "半导体"],
    "消费": ["消费", "零售", "白酒", "食品", "旅游", "餐饮"],
    "医药": ["医药", "医疗", "创新药", "疫苗", "医保"],
    "房地产": ["房地产", "楼市", "房贷", "土拍", "保障性住房"],
    "金融": ["银行", "券商", "保险", "降准", "降息"],
    "军工": ["军工", "国防", "军费", "阅兵"],
    "周期": ["钢铁", "煤炭", "有色", "化工", "水泥"],
}


def classify_sentiment(text: str) -> str:
    """JEV式规则判断：利好/利空/中性。"""
    bull_count = sum(1 for kw in BULLISH_KEYWORDS if kw in text)
    bear_count = sum(1 for kw in BEARISH_KEYWORDS if kw in text)

    if bull_count > bear_count and bull_count > 0:
        return "bullish"
    elif bear_count > bull_count and bear_count > 0:
        return "bearish"
    else:
        return "neutral"


def detect_sectors(text: str) -> list[str]:
    """检测新闻涉及哪些板块。"""
    sectors = []
    for sector, keywords in SECTOR_MAP.items():
        if any(kw in text for kw in keywords):
            sectors.append(sector)
    return sectors


def fetch_cctv_news() -> list[dict]:
    """爬央视财经新闻。"""
    try:
        import akshare as ak
        import warnings
        warnings.filterwarnings("ignore")

        df = ak.news_cctv()
        if df is None or len(df) == 0:
            return []

        news = []
        for _, row in df.head(20).iterrows():
            title = str(row.get("title", ""))
            content = str(row.get("content", ""))[:300]
            text = title + " " + content

            news.append({
                "source": "央视财经",
                "title": title,
                "content": content,
                "time": str(row.get("date", "")),
                "sentiment": classify_sentiment(text),
                "sectors": detect_sectors(text),
            })
        return news
    except Exception as e:
        print(f"[NEWS] CCTV爬取失败: {e}")
        return []


def fetch_em_news() -> list[dict]:
    """爬东方财富要闻。"""
    try:
        import akshare as ak
        import warnings
        warnings.filterwarnings("ignore")

        df = ak.stock_news_em(symbol="000001")  # 上证指数新闻
        if df is None or len(df) == 0:
            return []

        news = []
        for _, row in df.head(20).iterrows():
            title = str(row.get("新闻标题", ""))
            content = str(row.get("新闻内容", ""))[:300]
            text = title + " " + content

            news.append({
                "source": "东方财富",
                "title": title,
                "content": content,
                "time": str(row.get("发布时间", "")),
                "sentiment": classify_sentiment(text),
                "sectors": detect_sectors(text),
            })
        return news
    except Exception as e:
        print(f"[NEWS] 东财爬取失败: {e}")
        return []


def run_daily_news() -> dict:
    """每日新闻采集 + 情绪评分。"""
    today = datetime.date.today().isoformat()
    print(f"[NEWS] {today} 开始采集新闻")

    all_news = []
    all_news.extend(fetch_cctv_news())
    all_news.extend(fetch_em_news())

    if not all_news:
        result = {"date": today, "total": 0, "error": "无新闻数据", "news": []}
        OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        return result

    # 去重
    seen = set()
    unique = []
    for n in all_news:
        key = n["title"][:50]
        if key not in seen:
            seen.add(key)
            unique.append(n)

    # 情绪统计
    bullish = [n for n in unique if n["sentiment"] == "bullish"]
    bearish = [n for n in unique if n["sentiment"] == "bearish"]
    neutral = [n for n in unique if n["sentiment"] == "neutral"]

    # 板块统计
    sector_counts = {}
    for n in unique:
        for s in n["sectors"]:
            sector_counts[s] = sector_counts.get(s, 0) + 1

    # 情绪评分（-100到+100）
    sentiment_score = round(
        (len(bullish) - len(bearish)) / max(len(unique), 1) * 100, 1
    )

    result = {
        "date": today,
        "total": len(unique),
        "sentiment_score": sentiment_score,
        "sentiment_label": "bullish" if sentiment_score > 20 else ("bearish" if sentiment_score < -20 else "neutral"),
        "counts": {
            "bullish": len(bullish),
            "bearish": len(bearish),
            "neutral": len(neutral),
        },
        "hot_sectors": sorted(sector_counts.items(), key=lambda x: -x[1])[:5],
        "top_news": unique[:10],
    }

    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"[NEWS] {len(unique)}条新闻, 情绪评分={sentiment_score}")
    return result


if __name__ == "__main__":
    r = run_daily_news()
    print(json.dumps(r, ensure_ascii=False, indent=2)[:500])
