"""全A股多因子选股（5000+只）。

两步走：
1. ak.stock_zh_a_spot() 一次拉全市场快照（秒级），粗筛：非ST/价格>2/换手率<8%
2. 对粗筛通过的票逐只拉日线，算10因子打分
3. 总分>=3 入选，Top 30 输出

cron 建议凌晨 1:00 跑（不阻塞盘中）。
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Any

from connectors.base import AgentAdapter, AgentResult, _now_iso
from connectors._lessons import load_recent_lessons
from connectors.quant_multifactor import score_stock

try:
    import akshare as ak  # type: ignore
    _AK_AVAILABLE = True
except Exception:  # noqa: BLE001
    ak = None  # type: ignore
    _AK_AVAILABLE = False


class QuantAllAAdapter(AgentAdapter):
    name = "quant_all_a"

    def __init__(self) -> None:
        self.mode = "akshare" if _AK_AVAILABLE else "mock"

    def submit(self, task: str, workspace: str) -> AgentResult:
        out_dir = Path(workspace) / "all_a_output"
        out_dir.mkdir(parents=True, exist_ok=True)
        lessons = load_recent_lessons(workspace)

        if not _AK_AVAILABLE:
            content = f"# 全A选股（mock）\n{lessons}"
            path = out_dir / f"all_a_{datetime.datetime.now().strftime('%Y%m%d')}.md"
            path.write_text(content, encoding="utf-8")
            return AgentResult(True, self.name, "done", f"[{self.name}/mock]",
                artifact_paths=[str(path.relative_to(workspace))])

        try:
            # 1. 一次拉全市场快照
            print("拉全市场快照...")
            spot = ak.stock_zh_a_spot()
            total_spot = len(spot)
            print(f"全市场 {total_spot} 只")

            # 2. 粗筛：非ST、价格>2、换手率<8%、非停牌
            coarse = []
            for _, row in spot.iterrows():
                name = str(row.get("name", ""))
                price = float(row.get("trade", 0) or 0)
                turnover = float(row.get("turnoverratio", 0) or 0)
                code = str(row.get("code", ""))
                symbol = str(row.get("symbol", ""))  # sh600519 / sz000001

                if "ST" in name or "st" in name:
                    continue
                if price < 2 or price > 200:  # 排除超低价和超高价
                    continue
                if turnover > 8:  # 排除高换手（投机太猛）
                    continue
                if price == 0:  # 停牌
                    continue
                coarse.append({"code": code, "name": name, "symbol": symbol, "price": price})

            print(f"粗筛后 {len(coarse)} 只")

            # 3. 逐只拉日线算多因子打分
            results = []
            errors = 0
            for i, c in enumerate(coarse):
                try:
                    df = ak.stock_zh_a_daily(symbol=c["symbol"], start_date="20240601", adjust="qfq")
                    if df is None or len(df) < 60:
                        continue
                    closes = [float(x) for x in df["close"].tolist()]
                    volumes = [float(x) for x in df["volume"].tolist()]
                    score, details = score_stock(closes, volumes)
                    if score >= 3:
                        results.append({
                            "code": c["code"], "name": c["name"],
                            "close": closes[-1], "score": score, "details": details,
                        })
                except Exception:
                    errors += 1
                    continue
                if (i + 1) % 200 == 0:
                    print(f"进度 {i+1}/{len(coarse)}，候选 {len(results)}")

            results.sort(key=lambda x: -x["score"])
            today = datetime.datetime.now().strftime("%Y%m%d")

            lines = [
                f"# 全A股多因子选股（{total_spot}只粗筛→{len(coarse)}只精筛）",
                f"- 扫描时间：{_now_iso()}",
                f"- 全市场：{total_spot} 只",
                f"- 粗筛通过：{len(coarse)} 只（非ST/价格2-200/换手<8%）",
                f"- 精筛候选（总分>=3）：{len(results)} 只（失败 {errors}）",
                f"- 因子：RSI/MA20/MA60/MA排列/5日涨跌/波动率/量比/布林/MACD",
                "",
                lessons,
                "",
                "| 排名 | 代码 | 名称 | 收盘 | 总分 | 得分明细 |",
                "|---|---|---|---|---|---|",
            ]
            for i, r in enumerate(results[:30]):
                lines.append(f"| {i+1} | {r['code']} | {r['name']} | {r['close']:.2f} "
                             f"| **{r['score']:+.0f}** | {'; '.join(r['details'][:5])} |")

            path = out_dir / f"all_a_{today}.md"
            path.write_text("\n".join(lines), encoding="utf-8")

            return AgentResult(True, self.name, "done",
                f"[{self.name}/akshare] 全A {total_spot}只 → {len(results)}只候选",
                artifact_paths=[str(path.relative_to(workspace))])
        except Exception as e:  # noqa: BLE001
            return AgentResult(False, self.name, "error",
                f"[{self.name}/akshare] 失败：{e}", error=str(e))

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "ready": _AK_AVAILABLE, "mode": self.mode}

    def describe(self) -> dict[str, Any]:
        return {"summary": "全A股5000+只多因子选股，两步粗筛+精筛",
                "params": {"task": {"type": "string", "description": "留空跑全A"}}}
