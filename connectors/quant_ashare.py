"""A 股技术面连接器：用 akshare 拉 A 股日线 + 算简单技术指标。

后端：akshare（容器内 pip install，纯 Python，无需账号）
  - 数据源：东方财富 / 新浪（免费、无需 key）
  - 覆盖：沪深 A 股全市场日线 / 实时行情

任务解析：从自然语言提取 6 位 A 股代码，例如
  "分析 600519"      ->  600519（贵州茅台）
  "看 000001 平安银行" ->  000001

输出：最近 60 日 K 线摘要 + MA5/MA20/RSI/MACD 简评，写入 workspace/quant_ashare_output/。
akshare 不可用时降级为 mock。
"""

from __future__ import annotations

import datetime
import os
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


def _parse_code(task: str) -> str | None:
    m = re.search(r"\b(\d{6})\b", task)
    return m.group(1) if m else None


def _sina_symbol(code: str) -> str:
    """6 位代码转新浪前缀：6 开头=sh，0/3 开头=sz。"""
    if code.startswith("6"):
        return f"sh{code}"
    return f"sz{code}"


def _fetch_daily(code: str, start: str, end: str):
    """新浪主接口（稳定），东财 fallback。"""
    try:
        return ak.stock_zh_a_daily(symbol=_sina_symbol(code), start_date=start, end_date=end, adjust="qfq"), "sina"
    except Exception:
        return ak.stock_zh_a_hist(symbol=code, period="daily", start_date=start, end_date=end, adjust="qfq"), "eastmoney"


def _sma(values: list[float], n: int) -> float:
    if len(values) < n:
        return float("nan")
    return sum(values[-n:]) / n


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
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


class QuantAShareAdapter(AgentAdapter):
    name = "quant_ashare"

    def __init__(self) -> None:
        self.mode = "akshare" if _AK_AVAILABLE else "mock"

    def submit(self, task: str, workspace: str) -> AgentResult:
        task = (task or "").strip()
        code = _parse_code(task)
        out_dir = Path(workspace) / "quant_ashare_output"
        out_dir.mkdir(parents=True, exist_ok=True)

        if not code:
            return AgentResult(
                False, self.name, "skipped",
                f"[{self.name}/{self.mode}] 未识别到 6 位 A 股代码，例如：分析 600519",
            )

        now = datetime.datetime.now()
        start = (now - datetime.timedelta(days=120)).strftime("%Y%m%d")
        end = now.strftime("%Y%m%d")

        lessons = load_recent_lessons(workspace)

        if not _AK_AVAILABLE:
            content = (
                f"# A 股分析（mock，akshare 未装）\n\n"
                f"- 代码：{code}\n- 任务：{task}\n- 时间：{_now_iso()}\n\n{lessons}\n"
                f"（mock：容器内未装 akshare，部署后即可真实拉数）\n"
            )
            path = out_dir / f"ashare_{code}_{now.strftime('%Y%m%d_%H%M%S')}.md"
            path.write_text(content, encoding="utf-8")
            return AgentResult(
                True, self.name, "done",
                f"[{self.name}/mock] {code} mock 报告已生成（部署 akshare 后切真实数据）",
                artifact_paths=[str(path.relative_to(workspace))],
            )

        try:
            df, source = _fetch_daily(code, start, end)
            if df is None or df.empty:
                raise ValueError(f"akshare 返回空：{code}")

            closes = [float(x) for x in df["close" if "close" in df.columns else "收盘"].tolist()]
            last_close = closes[-1]
            ma5 = _sma(closes, 5)
            ma20 = _sma(closes, 20)
            rsi = _rsi(closes, 14)

            trend = "上行" if last_close > ma20 else "下行"
            rsi_note = "超买" if rsi > 70 else ("超卖" if rsi < 30 else "中性")

            date_col = "date" if "date" in df.columns else "日期"
            open_col = "open" if "open" in df.columns else "开盘"
            vol_col = "volume" if "volume" in df.columns else "成交量"

            content = (
                f"# A 股技术面分析（真实数据 akshare/{source}）\n\n"
                f"- 代码：{code}\n- 任务：{task}\n- 最新收盘：{last_close:.2f}\n"
                f"- MA5：{ma5:.2f} / MA20：{ma20:.2f}（{trend}）\n"
                f"- RSI(14)：{rsi:.1f}（{rsi_note}）\n"
                f"- 数据范围：{df[date_col].iloc[0]} ~ {df[date_col].iloc[-1]}（{len(df)} 个交易日）\n\n"
                f"{lessons}\n\n"
                f"## 最近 5 日\n\n"
                + "\n".join(
                    f"- {r[date_col]}：开 {float(r[open_col]):.2f} / 收 {float(r['close' if 'close' in df.columns else '收盘']):.2f} / 量 {r[vol_col]}"
                    for _, r in df.tail(5).iterrows()
                )
            )
            path = out_dir / f"ashare_{code}_{now.strftime('%Y%m%d_%H%M%S')}.md"
            path.write_text(content, encoding="utf-8")
            return AgentResult(
                True, self.name, "done",
                f"[{self.name}/akshare] {code} 分析完成：收盘 {last_close:.2f}，{trend}，RSI {rsi:.1f}",
                artifact_paths=[str(path.relative_to(workspace))],
            )
        except Exception as e:  # noqa: BLE001
            return AgentResult(
                False, self.name, "error",
                f"[{self.name}/akshare] {code} 失败：{e}",
                error=str(e),
            )

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "ready": _AK_AVAILABLE, "mode": self.mode,
                "hint": "akshare 已装" if _AK_AVAILABLE else "容器内未装 akshare"}

    def describe(self) -> dict[str, Any]:
        return {
            "summary": "A 股技术面分析（akshare 免费数据）：输入 6 位代码，返回最近 60 日 K 线 + MA/RSI 简评",
            "params": {"task": {"type": "string", "description": "例如：分析 600519 贵州茅台"}},
        }
