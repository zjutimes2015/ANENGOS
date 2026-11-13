"""quant_theme.py: 题材/板块强度评分 agent（抄HEXIN设计）。

评分维度（4个，0-100分）：
1. 板块涨幅分（30分）：-2%→0分，+6%→30分
2. 领涨股强度分（20分）：涨停(10%)→20分
3. 上涨家数占比（25分）：100%上涨→25分
4. 主力资金流（25分）：30亿净流入→25分

输出：Top 5 题材排名 + 强度分
"""
from __future__ import annotations
import datetime
import json
from pathlib import Path

WS = Path("/srv/anengos/workspace")
OUT = WS / "theme_daily.json"


def fetch_industry_boards() -> list[dict]:
    """从AKShare拉行业板块数据。"""
    try:
        import akshare as ak
        import warnings
        warnings.filterwarnings("ignore")

        df = ak.stock_board_industry_name_em()
        if df is None or len(df) == 0:
            return []

        boards = []
        for _, row in df.iterrows():
            boards.append({
                "name": str(row.get("板块名称", "")),
                "change_pct": float(row.get("涨跌幅", 0) or 0),
                "leader": str(row.get("领涨股票", "")),
                "leader_pct": float(row.get("领涨股票-涨跌幅", 0) or 0),
                "up_count": None,  # AKShare这个接口没有，后面补（None=缺数据，不计分）
                "down_count": None,
                "main_net_inflow": None,  # 资金流数据后面补（None=缺数据，不计分）
            })
        return boards
    except Exception as e:
        print(f"[THEME] 板块数据获取失败: {e}")
        return []


def score_board(b: dict) -> float:
    """按可得维度评分并归一到 0-100；缺数据的维度从分母剔除，不用 0 拉低总分。"""
    dims: list[tuple[float, float, bool]] = []

    # 1. 板块涨幅分（30分）
    pct = b.get("change_pct", 0)
    dims.append((max(0, min(30, 30 * (pct + 2) / 8)), 30, True))

    # 2. 领涨股强度分（20分）
    leader_pct = b.get("leader_pct", 0)
    dims.append((max(0, min(20, 20 * (leader_pct / 10))), 20, True))

    # 3. 上涨家数占比（25分）；无涨跌家数数据时该维度不参与计分
    up, down = b.get("up_count"), b.get("down_count")
    if up is not None and down is not None and (up + down) > 0:
        dims.append((up / (up + down) * 25, 25, True))
    else:
        dims.append((0.0, 25, False))

    # 4. 主力资金流（25分）；无资金流数据时该维度不参与计分
    inflow = b.get("main_net_inflow")
    if inflow is not None:
        dims.append((max(0, min(25, 25 * (inflow / 1e8) / 30)), 25, True))
    else:
        dims.append((0.0, 25, False))

    available = [(s, w) for s, w, ok in dims if ok]
    if not available:
        return 0.0
    max_total = sum(w for _, w in available)
    got = sum(s for s, _ in available)
    return round(got / max_total * 100, 1)


def run_theme_scan() -> dict:
    """跑一次题材扫描。"""
    today = datetime.date.today().isoformat()
    print(f"[THEME] {today} 开始题材扫描")

    boards = fetch_industry_boards()
    if not boards:
        result = {"date": today, "error": "板块数据获取失败", "top": []}
        OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        return result

    # 评分
    for b in boards:
        b["score"] = score_board(b)

    # 排序
    boards.sort(key=lambda x: x["score"], reverse=True)
    top5 = boards[:5]

    result = {
        "date": today,
        "total": len(boards),
        "top": [
            {
                "name": t["name"],
                "score": t["score"],
                "change_pct": t["change_pct"],
                "leader": t["leader"],
                "leader_pct": t["leader_pct"],
            }
            for t in top5
        ],
    }

    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(f"[THEME] {len(boards)}个板块, Top1: {top5[0]['name']} ({top5[0]['score']}分)")
    return result


if __name__ == "__main__":
    r = run_theme_scan()
    print(json.dumps(r, ensure_ascii=False, indent=2)[:500])
