"""虚拟盘交易 agent：screener 候选自动买入 + 卖出规则自动平仓。

买入：screener 候选未持仓 → 当日收盘价买 100 股
卖出（优先级从高到低）：
  1. 收盘价 < MA20（破位）→ 全部止损卖出
  2. 浮盈 ≥ TAKE_PROFIT_PCT（止盈）→ 全部落袋
  3. RSI > 70（超买）→ 减仓一半，保留剩余仓位吃趋势延续
持仓跟踪：每日更新最新价，算浮盈/浮亏

cron：每天 15:35 自动跑。
"""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path
from typing import Any

# 止盈阈值：浮盈达到该百分比即全部卖出落袋（本数值由 2026-W42 周复盘 IMP-001 改进项驱动）
TAKE_PROFIT_PCT = 8.0

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

        # 2. 卖出检查：破MA20 止损 > 止盈落袋 > RSI>70 减仓
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
            pnl_pct = (last - p["buy_price"]) / p["buy_price"] * 100

            reason = None
            sell_shares = p["shares"]
            if last < ma20:
                # 1) 破位：趋势已坏，全部止损（最高优先级）
                reason = f"跌破MA20({last:.2f}<{ma20:.2f})"
            elif pnl_pct >= TAKE_PROFIT_PCT:
                # 2) 止盈：浮盈达标，全部落袋
                reason = f"止盈(+{pnl_pct:.1f}%≥{TAKE_PROFIT_PCT}%)"
            elif rsi > 70:
                # 3) 超买：减仓一半，保留仓位吃趋势
                reason = f"RSI减仓({rsi:.1f}>70)"
                sell_shares = p["shares"] // 2
                if sell_shares < 1:
                    sell_shares = p["shares"]

            if reason:
                # 卖出（全卖或减仓）
                pnl = (last - p["buy_price"]) * sell_shares
                sale = {
                    **p,
                    "sell_date": today,
                    "sell_price": last,
                    "sell_reason": reason,
                    "shares": sell_shares,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "partial": sell_shares < p["shares"],
                }
                history.append(sale)
                if sell_shares < p["shares"]:
                    # 减仓：剩余仓位继续持有，更新现价与浮盈亏
                    p["shares"] -= sell_shares
                    p["current_price"] = last
                    p["pnl"] = (last - p["buy_price"]) * p["shares"]
                    p["pnl_pct"] = pnl_pct
                    still_held.append(p)
                sold_today.append(f"{p['name']}({p['code']}) {reason} 盈亏{pnl:+.2f}元")
            else:
                # 继续持有，更新现价
                p["current_price"] = last
                p["pnl"] = (last - p["buy_price"]) * p["shares"]
                p["pnl_pct"] = (last - p["buy_price"]) / p["buy_price"] * 100
                still_held.append(p)

        positions = still_held

        # 3. 读最新多因子候选（Top 10）
        mf_dir = ws / "multifactor_output"
        candidates = []
        if mf_dir.exists():
            files = sorted(mf_dir.glob("multifactor_*.md"), reverse=True)
            if files:
                text = files[0].read_text(encoding="utf-8")
                for line in text.splitlines():
                    # 多因子格式：| 1 | 600919 | 江苏银行 | ...
                    m = re.match(r"\|\s*\d+\s*\|\s*(\d{6})\s*\|\s*([^|]+?)\s*\|", line)
                    if m:
                        candidates.append({"code": m.group(1), "name": m.group(2).strip()})
                candidates = candidates[:10]  # 只买 Top 10

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
        return {"summary": "虚拟盘：自动买入+自动卖出（破MA20止损 / 浮盈≥8%止盈 / RSI>70减仓）",
                "params": {"task": {"type": "string", "description": "留空跑默认"}}}
