"""quant_aggregator.py: 每日决策面板聚合器（抄HEXIN设计）。

读取各个agent的输出JSON，生成一张每日决策面板（Markdown格式）。
包括：
1. 新闻情绪（能不能做）
2. 题材评分（做什么板块）
3. 选股结果（买什么股）
4. 虚拟盘净值（赚了亏了）
5. 一句话结论

输出：workspace/decision_board_YYYY-MM-DD.md
"""
from __future__ import annotations
import datetime
import json
from pathlib import Path

WS = Path("/srv/anengos/workspace")


def load_json(name: str) -> dict:
    """读取agent输出JSON。"""
    path = WS / name
    if not path.exists():
        return {"error": "文件不存在"}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"error": str(e)}


def build_decision_board() -> str:
    """生成每日决策面板。"""
    today = datetime.date.today().isoformat()
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    lines = []
    lines.append(f"# ANENGOS 决策面板 {today}")
    lines.append("")
    lines.append(f"> 生成时间: {now} | 仅供决策参考，最终判断由你确认")
    lines.append("")

    # 1. 新闻情绪（能不能做）
    lines.append("## 一、新闻情绪（能不能做）")
    news = load_json("news_daily.json")
    if "error" not in news:
        score = news.get("sentiment_score", 0)
        label = news.get("sentiment_label", "?")
        lines.append(f"- 情绪评分: **{score}**（{label}）")
        lines.append(f"- 利好: {news.get('counts', {}).get('bullish', 0)}条")
        lines.append(f"- 利空: {news.get('counts', {}).get('bearish', 0)}条")
        lines.append(f"- 热点板块: {', '.join([s[0] for s in news.get('hot_sectors', [])])}")
    else:
        lines.append(f"- {news['error']}")
    lines.append("")

    # 2. 题材评分（做什么板块）
    lines.append("## 二、题材评分（做什么）")
    theme = load_json("theme_daily.json")
    if "error" not in theme and theme.get("top"):
        for i, t in enumerate(theme["top"][:5], 1):
            lines.append(f"{i}. **{t['name']}**: {t['score']}分（涨{t['change_pct']}%，领涨: {t['leader']}）")
    else:
        lines.append(f"- {theme.get('error', '题材数据未生成')}")
    lines.append("")

    # 3. 选股结果（买什么）
    lines.append("## 三、选股结果（买什么）")
    # TODO: 读取飞书表的选股结果
    lines.append("- 详见飞书表「全A选股」")
    lines.append("")

    # 4. 虚拟盘净值（赚了亏了）
    lines.append("## 四、虚拟盘净值（赚了亏了）")
    paper = load_json("paper_track.json")
    if "error" not in paper:
        lines.append(f"- 净值: {paper.get('nav', '?')}")
        lines.append(f"- 今日收益: {paper.get('today_return', '?')}")
    else:
        lines.append(f"- {paper['error']}")
    lines.append("")

    # 5. 一句话结论
    lines.append("## 五、一句话结论")
    news_score = news.get("sentiment_score", 0) if "error" not in news else 0
    if news_score > 20:
        lines.append("> 情绪偏多，可积极参与，关注Top题材板块。")
    elif news_score < -20:
        lines.append("> 情绪偏空，降低仓位或空仓观望，不追高。")
    else:
        lines.append("> 方向不明，半仓以下，等待信号确认。")
    lines.append("")

    lines.append("---")
    lines.append("*合规声明：本面板由AI生成，不构成投资建议。所有决策需你人工确认后在券商客户端执行。*")

    content = "\n".join(lines)
    out_path = WS / f"decision_board_{today}.md"
    out_path.write_text(content, encoding="utf-8")
    print(f"[AGGREGATOR] 决策面板已生成: {out_path}")
    return content


if __name__ == "__main__":
    build_decision_board()
