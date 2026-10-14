"""虚拟盘交易 agent：screener 选出的票自动买入，每日跟踪收益。

流程：
1. 读 screener_output/ 最新候选清单
2. 对每只候选，若未持仓则以当日收盘价虚拟买入（100股）
3. 更新所有持仓最新价，算浮盈/浮亏
4. 输出 portfolio.md + positions.json 持久化

cron：每天 15:35（screener 15:30 跑完后）自动跑。
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


def _latest_price(code: str) -> float | None:
    """拉当日收盘价。"""
    try:
        df = ak.stock_zh_a_daily(symbol=_sina_code(code), start_date="20241001", adjust="qfq")
        if df is None or df.empty:
            return None
        return float(df["close"].iloc[-1])
    except Exception:
        return None


class PaperTraderAdapter(AgentAdapter):
    name = "paper_trader"

    def __init__(self) -> None:
        self.mode = "akshare" if _AK_AVAILABLE else "mock"

    def submit(self, task: str, workspace: str) -> AgentResult:
        ws = Path(workspace)
        out_dir = ws / "paper_trader_output"
        out_dir.mkdir(parents=True, exist_ok=True)
        pos_file = ws / "paper_positions.json"

        # 1. 读已有持仓
        positions = []
        if pos_file.exists():
            try:
                positions = json.loads(pos_file.read_text(encoding="utf-8"))
            except Exception:
                positions = []

        if not _AK_AVAILABLE:
            return AgentResult(True, self.name, "done",
                f"[{self.name}/mock] akshare 未装，持仓 {len(positions)} 只",
                artifact_paths=[])

        # 2. 读最新 screener 候选
        screener_dir = ws / "screener_output"
        candidates = []
        if screener_dir.exists():
            files = sorted(screener_dir.glob("screener_*.md"), reverse=True)
            if files:
                text = files[0].read_text(encoding="utf-8")
                # 解析 markdown 表格行：| 600519 | 贵州茅台 | 1286.00 | ...
                for line in text.splitlines():
                    m = re.match(r"\|\s*(\d{6})\s*\|\s*([^|]+?)\s*\|", line)
                    if m:
                        candidates.append({"code": m.group(1), "name": m.group(2).strip()})

        # 3. 新候选买入
        held_codes = {p["code"] for p in positions}
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        new_buys = []
        for c in candidates:
            if c["code"] in held_codes:
                continue
            price = _latest_price(c["code"])
            if price is None:
                continue
            positions.append({
                "code": c["code"], "name": c["name"],
                "buy_date": today, "buy_price": price,
                "shares": 100, "current_price": price,
            })
            new_buys.append(f"{c['name']}({c['code']}) @ {price:.2f}")

        # 4. 更新所有持仓最新价
        total_cost = 0.0
        total_value = 0.0
        for p in positions:
            cur = _latest_price(p["code"])
            if cur:
                p["current_price"] = cur
            cost = p["buy_price"] * p["shares"]
            value = p["current_price"] * p["shares"]
            total_cost += cost
            total_value += value
            p["pnl"] = value - cost
            p["pnl_pct"] = (value - cost) / cost * 100 if cost else 0

        # 5. 持久化
        pos_file.write_text(json.dumps(positions, ensure_ascii=False, indent=2), encoding="utf-8")

        # 6. 输出报告
        lines = [
            f"# 虚拟盘组合（{today}）",
            f"- 持仓：{len(positions)} 只（今日新买入 {len(new_buys)} 只）",
            f"- 总成本：{total_cost:.2f} 元",
            f"- 总市值：{total_value:.2f} 元",
            f"- 总盈亏：{total_value - total_cost:+.2f} 元（{(total_value - total_cost)/total_cost*100 if total_cost else 0:+.2f}%）",
            "",
        ]
        if new_buys:
            lines.append("## 今日新买入")
            for b in new_buys:
                lines.append(f"- {b}")
            lines.append("")

        lines += [
            "| 代码 | 名称 | 买入日 | 买价 | 现价 | 数量 | 盈亏 | 盈亏% |",
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
            f"[{self.name}/akshare] 持仓 {len(positions)} 只，总盈亏 {total_value-total_cost:+.2f} 元",
            artifact_paths=[str(out_path.relative_to(workspace))])

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "ready": _AK_AVAILABLE, "mode": self.mode}

    def describe(self) -> dict[str, Any]:
        return {"summary": "虚拟盘：screener候选自动买入，每日跟踪收益",
                "params": {"task": {"type": "string", "description": "留空跑默认"}}}
