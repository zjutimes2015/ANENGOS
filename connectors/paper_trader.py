"""虚拟盘交易 agent：screener 候选自动买入 + 卖出规则自动平仓。

买入：screener 候选未持仓 → 当日收盘价买 100 股
卖出：RSI>70（超买） 或 收盘价<MA20（破位）→ 自动卖出
持仓跟踪：每日更新最新价，算浮盈/浮亏

cron：每天 15:35 自动跑。
"""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path
from typing import Any

from connectors.base import AgentAdapter, AgentResult, _now_iso

try:
    import akshare as ak  # type: ignore
    _AK_AVAILABLE = True
except Exception:  # noqa: BLE001
    ak = None  # type: ignore
    _AK_AVAILABLE = False


def _sina_code(code: str) -> str:
    return f"sh{code}" if code.startswith("6") else f"sz{code}"


def _daily(code: str, days: int = 60):
    """拉日线 DataFrame。"""
    try:
        df = ak.stock_zh_a_daily(symbol=_sina_code(code), start_date="20240601", adjust="qfq")
        if df is None or df.empty:
            return None
        return df.tail(days)
    except Exception:
        return None


def _rsi(closes: list[float], n: int = 14) -> float:
    if len(closes) < n + 1:
        return float("nan")
    gains, losses = [], []
    for i in range(-n, 0):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains) / n
    avg_loss = sum(losses) / n
    if avg_loss == 0:
        return 100.0
    return 100 - (100 / (1 + avg_gain / avg_loss))


def _sma(vals: list[float], n: int) -> float:
    if len(vals) < n:
        return float("nan")
    return sum(vals[-n:]) / n


class PaperTraderAdapter(AgentAdapter):
    name = "paper_trader"

    def __init__(self) -> None:
        self.mode = "akshare" if _AK_AVAILABLE else "mock"

    def submit(self, task: str, workspace: str) -> AgentResult:
        ws = Path(workspace)
        out_dir = ws / "paper_trader_output"
        out_dir.mkdir(parents=True, exist_ok=True)
        pos_file = ws / "paper_positions.json"

        # 1. 读已有数据（持仓 + 已卖出历史）
        data = {"positions": [], "history": []}
        if pos_file.exists():
            try:
                loaded = json.loads(pos_file.read_text(encoding="utf-8"))
                if isinstance(loaded, list):  # 兼容旧格式（只有 positions 列表）
                    data["positions"] = loaded
                else:
                    data = loaded
            except Exception:
                pass

        positions = data["positions"]
        history = data["history"]

        if not _AK_AVAILABLE:
            return AgentResult(True, self.name, "done",
                f"[{self.name}/mock] akshare 未装，持仓 {len(positions)} 只",
                artifact_paths=[])

        today = datetime.datetime.now().strftime("%Y-%m-%d")

        # 2. 卖出检查：遍历持仓，RSI>70 或 收盘<MA20 → 卖出
        sold_today = []
        still_held = []
        for p in positions:
            df = _daily(p["code"])
            if df is None:
                still_held.append(p)
                continue
            closes = [float(x) for x in df["close"].tolist()]
            last = closes[-1]
            ma20 = _sma(closes, 20)
            rsi = _rsi(closes, 14)

            reason = None
            if rsi > 70:
                reason = f"RSI超买({rsi:.1f}>70)"
            elif last < ma20:
                reason = f"跌破MA20({last:.2f}<{ma20:.2f})"

            if reason:
                # 卖出
                pnl = (last - p["buy_price"]) * p["shares"]
                p["sell_date"] = today
                p["sell_price"] = last
                p["sell_reason"] = reason
                p["pnl"] = pnl
                p["pnl_pct"] = (last - p["buy_price"]) / p["buy_price"] * 100
                history.append(p)
                sold_today.append(f"{p['name']}({p['code']}) {reason} 盈亏{pnl:+.2f}元")
            else:
                # 继续持有，更新现价
                p["current_price"] = last
                p["pnl"] = (last - p["buy_price"]) * p["shares"]
                p["pnl_pct"] = (last - p["buy_price"]) / p["buy_price"] * 100
                still_held.append(p)

        positions = still_held

        # 3. 读最新 screener 候选，新买入
        screener_dir = ws / "screener_output"
        candidates = []
        if screener_dir.exists():
            files = sorted(screener_dir.glob("screener_*.md"), reverse=True)
            if files:
                text = files[0].read_text(encoding="utf-8")
                for line in text.splitlines():
                    m = re.match(r"\|\s*(\d{6})\s*\|\s*([^|]+?)\s*\|", line)
                    if m:
                        candidates.append({"code": m.group(1), "name": m.group(2).strip()})

        held_codes = {p["code"] for p in positions}
        bought_today = []
        for c in candidates:
            if c["code"] in held_codes:
                continue
            df = _daily(c["code"])
            if df is None or df.empty:
                continue
            price = float(df["close"].iloc[-1])
            positions.append({
                "code": c["code"], "name": c["name"],
                "buy_date": today, "buy_price": price,
                "shares": 100, "current_price": price,
                "pnl": 0.0, "pnl_pct": 0.0,
            })
            bought_today.append(f"{c['name']}({c['code']}) @ {price:.2f}")

        # 4. 持久化
        data["positions"] = positions
        data["history"] = history
        pos_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

        # 5. 输出报告
        total_cost = sum(p["buy_price"] * p["shares"] for p in positions)
        total_value = sum(p["current_price"] * p["shares"] for p in positions)
        total_pnl = total_value - total_cost

        lines = [
            f"# 虚拟盘组合（{today}）",
            f"- 持仓：{len(positions)} 只 | 历史交易：{len(history)} 笔",
            f"- 今日买入：{len(bought_today)} | 今日卖出：{len(sold_today)}",
            f"- 持仓成本：{total_cost:.2f} 元 | 市值：{total_value:.2f} 元",
            f"- 浮动盈亏：{total_pnl:+.2f} 元（{total_pnl/total_cost*100 if total_cost else 0:+.2f}%）",
            "",
        ]
        if bought_today:
            lines.append("## 今日买入")
            for b in bought_today:
                lines.append(f"- {b}")
            lines.append("")
        if sold_today:
            lines.append("## 今日卖出")
            for s in sold_today:
                lines.append(f"- {s}")
            lines.append("")

        lines += [
            "## 当前持仓",
            "| 代码 | 名称 | 买入日 | 买价 | 现价 | 数量 | 浮动盈亏 | 盈亏% |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for p in positions:
            lines.append(
                f"| {p['code']} | {p['name']} | {p['buy_date']} | {p['buy_price']:.2f} "
                f"| {p['current_price']:.2f} | {p['shares']} | {p['pnl']:+.2f} | {p['pnl_pct']:+.2f}% |"
            )

        out_path = out_dir / f"portfolio_{today.replace('-', '')}.md"
        out_path.write_text("\n".join(lines), encoding="utf-8")

        return AgentResult(True, self.name, "done",
            f"[{self.name}/akshare] 持仓{len(positions)}只，今日买{len(bought_today)}卖{len(sold_today)}",
            artifact_paths=[str(out_path.relative_to(workspace))])

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "ready": _AK_AVAILABLE, "mode": self.mode}

    def describe(self) -> dict[str, Any]:
        return {"summary": "虚拟盘：自动买入+自动卖出（RSI>70或破MA20）",
                "params": {"task": {"type": "string", "description": "留空跑默认"}}}
