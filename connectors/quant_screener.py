"""自动选股 agent：扫沪深300，筛 RSI 超卖 + MA20 向上的候选票。

数据源：akshare 新浪接口（index_stock_cons_sina 拿成分 + stock_zh_a_daily 算指标）
筛选逻辑：RSI(14) < 35（超卖） 且 收盘价 > MA20（趋势未坏）
输出：workspace/screener_output/YYYYMMDD.md 候选清单

cron 每天 15:30 收盘后自动跑。
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Any

from connectors.base import AgentAdapter, AgentResult, _now_iso
from connectors._lessons import load_recent_lessons

try:
    import akshare as ak  # type: ignore
    _AK_AVAILABLE = True
except Exception:  # noqa: BLE001
    ak = None  # type: ignore
    _AK_AVAILABLE = False


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


class QuantScreenerAdapter(AgentAdapter):
    name = "quant_screener"

    def __init__(self) -> None:
        self.mode = "akshare" if _AK_AVAILABLE else "mock"

    def submit(self, task: str, workspace: str) -> AgentResult:
        task = (task or "").strip()
        out_dir = Path(workspace) / "screener_output"
        out_dir.mkdir(parents=True, exist_ok=True)
        lessons = load_recent_lessons(workspace)

        # 默认沪深300，可指定指数代码
        index_code = "000300"
        m = re.search(r"\b(\d{6})\b", task)
        if m:
            index_code = m.group(1)

        if not _AK_AVAILABLE:
            content = f"# 选股扫描（mock，akshare 未装）\n\n- 指数：{index_code}\n{lessons}"
            path = out_dir / f"screener_{datetime.datetime.now().strftime('%Y%m%d')}.md"
            path.write_text(content, encoding="utf-8")
            return AgentResult(True, self.name, "done",
                f"[{self.name}/mock] {index_code} mock 扫描",
                artifact_paths=[str(path.relative_to(workspace))])

        try:
            # 1. 拿成分股（带实时行情）
            cons = ak.index_stock_cons_sina(index_code)
            total = len(cons)
            candidates = []
            errors = 0

            # 2. 逐只拉日线算指标
            for idx, row in cons.iterrows():
                sym = row["symbol"]  # sh600519 / sz000001
                name = row["name"]
                try:
                    df = ak.stock_zh_a_daily(symbol=sym, start_date="20240601", adjust="qfq")
                    if df is None or len(df) < 30:
                        continue
                    closes = [float(x) for x in df["close"].tolist()]
                    last = closes[-1]
                    ma20 = _sma(closes, 20)
                    rsi = _rsi(closes, 14)
                    # 筛选：RSI 超卖 且 价格在 MA20 之上
                    if rsi < 35 and last > ma20:
                        candidates.append({
                            "code": row["code"],
                            "name": name,
                            "close": last,
                            "ma20": ma20,
                            "rsi": rsi,
                        })
                except Exception:
                    errors += 1
                    continue

            # 3. 按 RSI 从低到高排序（越超卖越靠前）
            candidates.sort(key=lambda x: x["rsi"])

            today = datetime.datetime.now().strftime("%Y%m%d")
            lines = [
                f"# 每日选股扫描（{index_code}）",
                f"- 扫描时间：{_now_iso()}",
                f"- 成分股：{total} 只（失败 {errors}）",
                f"- 候选：{len(candidates)} 只（RSI<35 且 收盘>MA20）",
                f"- 筛选逻辑：超卖 + 趋势未坏",
                "",
                lessons,
                "",
                "| 代码 | 名称 | 收盘 | MA20 | RSI |",
                "|---|---|---|---|---|",
            ]
            for c in candidates[:30]:  # 最多输出 30 只
                lines.append(f"| {c['code']} | {c['name']} | {c['close']:.2f} | {c['ma20']:.2f} | {c['rsi']:.1f} |")

            path = out_dir / f"screener_{today}.md"
            path.write_text("\n".join(lines), encoding="utf-8")

            return AgentResult(True, self.name, "done",
                f"[{self.name}/akshare] {index_code} 扫完：{total}只 → {len(candidates)} 只候选",
                artifact_paths=[str(path.relative_to(workspace))])
        except Exception as e:  # noqa: BLE001
            return AgentResult(False, self.name, "error",
                f"[{self.name}/akshare] 扫描失败：{e}", error=str(e))

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "ready": _AK_AVAILABLE, "mode": self.mode,
                "hint": "akshare 已装" if _AK_AVAILABLE else "未装"}

    def describe(self) -> dict[str, Any]:
        return {"summary": "自动选股：扫沪深300，筛 RSI超卖+MA20向上的候选票",
                "params": {"task": {"type": "string", "description": "指数代码，默认 000300（沪深300）"}}}
