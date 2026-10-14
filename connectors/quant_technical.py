"""量化技术面分析连接器：对指定标的做 K 线 / RSI / MACD / 布林带技术分析。

后端：tradingview-mcp-server（sidecar 容器，MCP Streamable HTTP）：
  - ANENGOS_TV_MCP_URL   sidecar MCP 端点（默认 http://tradingview-mcp:8000/mcp）
  - ANENGOS_TV_MODE      http|mock（默认：有 URL 走 http，否则 mock）

任务解析：从自然语言提取 symbol / timeframe / exchange，例如
  "分析 BTCUSDT @ BINANCE 1h"  ->  BTCUSDT / 1h / BINANCE

统一 Schema：submit 返回 AgentResult；health()/describe() 供管理台可观测性与自动注册。
mock 模式会在 workspace/quant_technical_output/ 下产出结构化 markdown，链路可端到端演示。
"""

from __future__ import annotations

import datetime
import os
import re
from pathlib import Path
from typing import Any

from connectors.base import AgentAdapter, AgentResult, _now_iso
from connectors._tv_mcp_client import TVMCPClient
from connectors._lessons import load_recent_lessons


class QuantTechnicalAdapter(AgentAdapter):
    name = "quant_technical"

    def __init__(self, mcp_url: str | None = None, mode: str | None = None) -> None:
        self.mcp_url = (mcp_url or os.environ.get("ANENGOS_TV_MCP_URL", "")).rstrip("/")
        if mode or os.environ.get("ANENGOS_TV_MODE"):
            self.mode = (mode or os.environ["ANENGOS_TV_MODE"]).lower()
        else:
            self.mode = "http" if self.mcp_url else "mock"

    def submit(self, task: str, workspace: str) -> AgentResult:
        task = (task or "").strip()
        if not task:
            return AgentResult(False, self.name, "skipped", f"[{self.name}] 任务为空，未执行",
                               error="task 为空")
        symbol, timeframe, exchange = self._parse_task(task)
        if self.mode == "http":
            return self._run_http(task, workspace, symbol, timeframe, exchange)
        return self._run_mock(task, workspace, symbol, timeframe, exchange)

    # ---------- 任务解析 ----------

    @staticmethod
    def _parse_task(task: str) -> tuple[str, str, str]:
        """从自然语言粗略提取 symbol / timeframe / exchange。

        支持写法：
          "分析 BTCUSDT @ BINANCE 1h"
          "ETH/USDT 4h"
          "AAPL 日线"
        解析失败时回退默认值，不抛异常。
        """
        symbol, timeframe, exchange = "BTCUSDT", "1h", "BINANCE"

        # symbol @ exchange
        m = re.search(r"([A-Z0-9]{3,15})\s*@\s*([A-Z]{2,8})", task, re.IGNORECASE)
        if m:
            symbol, exchange = m.group(1).upper(), m.group(2).upper()
        else:
            m2 = re.search(r"\b([A-Z0-9]{3,15})[/\-](USDT|USD|CNY|HKD)\b", task, re.IGNORECASE)
            if m2:
                symbol = (m2.group(1) + m2.group(2)).upper()
                exchange = "BINANCE"

        # timeframe
        tf = re.search(r"\b(\d+[smhdw]|\d+mo)\b", task, re.IGNORECASE)
        if tf:
            timeframe = tf.group(1).lower()
        elif "日线" in task or "daily" in task.lower():
            timeframe = "1d"
        elif "周线" in task or "weekly" in task.lower():
            timeframe = "1w"

        return symbol, timeframe, exchange

    # ---------- 真实后端（第一版 stub，sidecar 就绪后补 MCP JSON-RPC） ----------

    def _run_http(self, task: str, workspace: str,
                  symbol: str, timeframe: str, exchange: str) -> AgentResult:
        client = TVMCPClient(self.mcp_url, timeout=90)
        text = client.call_tool("coin_analysis", {
            "symbol": symbol, "exchange": exchange, "timeframe": timeframe,
        })
        if not text:
            # MCP 调用失败，降级到 mock（保证链路不崩）
            return self._run_mock(task, workspace, symbol, timeframe, exchange, note="fallback-mock")

        out_dir = Path(workspace) / "quant_technical_output"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
        safe_symbol = re.sub(r"[^A-Z0-9]", "_", symbol)
        name = f"tech_{safe_symbol}_{timeframe}_{ts}.md"
        lessons = load_recent_lessons(workspace)
        content = (
            f"# 技术面分析（真实数据）\n\n"
            f"- 任务：{task}\n"
            f"- 标的：{symbol} @ {exchange}\n"
            f"- 周期：{timeframe}\n"
            f"- 生成时间（UTC）：{_now_iso()}\n\n"
            f"{lessons}\n\n"
            f"## TradingView 返回\n\n```\n{text}\n```\n"
        )
        (out_dir / name).write_text(content, encoding="utf-8")
        return AgentResult(
            True, self.name, "done",
            f"[{self.name}/http] 真实分析完成 {symbol} {timeframe}@{exchange}",
            artifact_paths=[f"{out_dir.name}/{name}"],
            meta={"mode": "http", "symbol": symbol, "timeframe": timeframe,
                  "exchange": exchange, "ts": _now_iso()},
        )

    # ---------- 演示后端 ----------

    def _run_mock(self, task: str, workspace: str, symbol: str, timeframe: str,
                  exchange: str, note: str = "mock") -> AgentResult:
        out_dir = Path(workspace) / "quant_technical_output"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
        safe_symbol = re.sub(r"[^A-Z0-9]", "_", symbol)
        name = f"tech_{safe_symbol}_{timeframe}_{ts}.md"

        content = (
            f"# 技术面分析（{note}）\n\n"
            f"- 任务：{task}\n"
            f"- 标的：{symbol} @ {exchange}\n"
            f"- 周期：{timeframe}\n"
            f"- 生成时间（UTC）：{_now_iso()}\n\n"
            f"## 指标信号（演示模板，待 sidecar 接入后由真实数据替换）\n\n"
            f"| 指标 | 读数 | 信号 |\n"
            f"|------|------|------|\n"
            f"| RSI(14) | 52 | 中性 |\n"
            f"| MACD(12,26,9) | 金叉边缘 | 偏多 |\n"
            f"| 布林带(20,2) | 中轨附近 | 中性 |\n"
            f"| EMA20/50 | 缠绕 | 待方向 |\n\n"
            f"## 结论\n\n"
            f"当前周期无强趋势信号，建议等待突破或回踩确认。\n\n"
            f"## 说明\n\n"
            f"配置 ANENGOS_TV_MCP_URL 指向 tradingview-mcp sidecar 后，"
            f"本文件将由真实技术指标数据自动填充。\n"
        )
        (out_dir / name).write_text(content, encoding="utf-8")

        return AgentResult(
            True, self.name, "done",
            f"[{self.name}/{note}] 已产出 {out_dir.name}/{name}（{symbol} {timeframe}@{exchange}）",
            artifact_paths=[f"{out_dir.name}/{name}"],
            meta={
                "mode": note,
                "symbol": symbol,
                "timeframe": timeframe,
                "exchange": exchange,
                "ts": _now_iso(),
            },
        )

    # ---------- 可观测性 ----------

    def health(self) -> dict[str, Any]:
        ready = self.mode == "mock" or (self.mode == "http" and bool(self.mcp_url))
        hint = {
            "http": f"已配置 sidecar：{self.mcp_url}" if self.mcp_url else "缺少 ANENGOS_TV_MCP_URL",
            "mock": "演示模式（未配置 sidecar）",
        }.get(self.mode, "未知模式")
        return {"name": self.name, "mode": self.mode, "ready": ready, "hint": hint}

    def describe(self) -> dict[str, Any]:
        return {
            "summary": "对指定标的做技术面分析（K线/RSI/MACD/布林带），产出结构化 markdown",
            "params": {
                "task": {
                    "type": "string",
                    "required": True,
                    "description": "自然语言任务，如 '分析 BTCUSDT @ BINANCE 1h' 或 'AAPL 日线'",
                }
            },
        }
