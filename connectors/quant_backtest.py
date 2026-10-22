"""量化回测连接器：策略历史回测 + walk-forward 过拟合判定（质量门）。

按 Tom Blomfield 5 层 AI loop 抽象：
  - 传感器层(Sensor)：读历史 K 线数据（tradingview-mcp backtest_strategy 内部完成）
  - 决策层(Policy)：哪些策略自动跑回测（9 种内置），walk-forward 结论必须人审
  - 工具层(Tool)：tradingview-mcp 的 backtest_strategy / walk_forward_backtest_strategy
  - 质量门(Quality Gate)：walk-forward 判定 ROBUST/MODERATE/WEAK/OVERFITTED
        * ROBUST     -> 推荐进虚拟盘
        * MODERATE   -> 观察，加过滤条件
        * WEAK        -> 不推荐
        * OVERFITTED  -> 淘汰（历史好未来大概率差）
  - 学习层(Learning)：回测结论 markdown 写回 workspace/quant_backtest_output/，
        作为 retro_loop 周复盘的输入——失败的回测教训沉淀为知识

后端：tradingview-mcp-server（sidecar，stdio/MCP HTTP）：
  - ANENGOS_TV_MCP_URL   sidecar MCP 端点
  - ANENGOS_TV_MODE      http|mock（默认 mock）
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
from connectors import walkforward as wf


# 9 种内置策略（对齐 tradingview-mcp backtest_strategy）
BUILTIN_STRATEGIES = {
    "rsi": "RSI 超买超卖",
    "bollinger": "布林带回归",
    "macd": "MACD 趋势",
    "ema_cross": "EMA 金叉死叉",
    "supertrend": "Supertrend 趋势跟踪",
    "donchian": "唐奇安通道突破",
    "rsi_pullback": "RSI 回踩",
    "keltner_breakout": "肯特纳突破",
    "triple_ema": "三 EMA 趋势",
}

# 质量门分级
GATE_LEVELS = {
    "ROBUST": "✅ 推荐进虚拟盘（walk-forward 稳健）",
    "MODERATE": "🟡 观察，需加过滤条件",
    "WEAK": "🟠 不推荐（样本外衰减明显）",
    "OVERFITTED": "❌ 淘汰（过拟合，历史好不代表未来）",
}


class QuantBacktestAdapter(AgentAdapter):
    name = "quant_backtest"

    def __init__(self, mcp_url: str | None = None, mode: str | None = None) -> None:
        self.mcp_url = (mcp_url or os.environ.get("ANENGOS_TV_MCP_URL", "")).rstrip("/")
        if mode or os.environ.get("ANENGOS_TV_MODE"):
            self.mode = (mode or os.environ["ANENGOS_TV_MODE"]).lower()
        else:
            self.mode = "http" if self.mcp_url else "mock"

    def submit(self, task: str, workspace: str) -> AgentResult:
        task = (task or "").strip()
        if not task:
            return AgentResult(False, self.name, "skipped", f"[{self.name}] 任务为空",
                               error="task 为空")
        symbol, timeframe, strategy, exchange = self._parse_task(task)
        if self.mode == "http":
            return self._run_http(task, workspace, symbol, timeframe, strategy, exchange)
        return self._run_mock(task, workspace, symbol, timeframe, strategy, exchange)

    # ---------- 任务解析 ----------

    @staticmethod
    def _parse_task(task: str) -> tuple[str, str, str, str]:
        """从自然语言解析 symbol / timeframe / strategy / exchange。"""
        symbol, timeframe, exchange = "BTCUSDT", "1d", "BINANCE"
        strategy = "rsi"

        m = re.search(r"([A-Z0-9]{3,15})\s*@\s*([A-Z]{2,8})", task, re.IGNORECASE)
        if m:
            symbol, exchange = m.group(1).upper(), m.group(2).upper()

        tf = re.search(r"\b(\d+[smhdw]|\d+mo)\b", task, re.IGNORECASE)
        if tf:
            timeframe = tf.group(1).lower()

        for s in BUILTIN_STRATEGIES:
            if s in task.lower():
                strategy = s
                break

        return symbol, timeframe, strategy, exchange

    # ---------- 真实后端（stub，sidecar 就绪后补 MCP JSON-RPC） ----------

    def _run_http(self, task: str, workspace: str,
                  symbol: str, timeframe: str, strategy: str, exchange: str) -> AgentResult:
        client = TVMCPClient(self.mcp_url, timeout=120)
        # 真实回测
        bt_text = client.call_tool("backtest_strategy", {
            "symbol": symbol, "exchange": exchange, "timeframe": timeframe,
            "strategy": strategy,
        })
        # walk-forward 过拟合判定（质量门）
        wf_text = client.call_tool("walk_forward_backtest_strategy", {
            "symbol": symbol, "exchange": exchange, "timeframe": timeframe,
            "strategy": strategy,
        })
        if not bt_text and not wf_text:
            return self._run_mock(task, workspace, symbol, timeframe, strategy, exchange, note="fallback-mock")

        # 从 walk-forward 文本里抽判定级别
        gate = "UNKNOWN"
        for level in ("ROBUST", "MODERATE", "WEAK", "OVERFITTED"):
            if wf_text and level in wf_text.upper():
                gate = level
                break

        out_dir = Path(workspace) / "quant_backtest_output"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
        safe_symbol = re.sub(r"[^A-Z0-9]", "_", symbol)
        name = f"bt_{safe_symbol}_{timeframe}_{strategy}_{ts}.md"
        lessons = load_recent_lessons(workspace)
        content = (
            f"# 回测报告（真实数据）\n\n"
            f"- 任务：{task}\n- 策略：{strategy}\n- 标的：{symbol} @ {exchange}\n- 周期：{timeframe}\n\n"
            f"{lessons}\n\n"
            f"## 回测结果\n\n```\n{bt_text or '(无数据)'}\n```\n\n"
            f"## 质量门（walk-forward）\n\n**判定：{gate}**\n\n```\n{wf_text or '(无数据)'}\n```\n"
        )
        (out_dir / name).write_text(content, encoding="utf-8")
        return AgentResult(
            True, self.name, "done",
            f"[{self.name}/http] 回测 {symbol} {timeframe} {strategy} -> {gate}",
            artifact_paths=[f"{out_dir.name}/{name}"],
            meta={"mode": "http", "symbol": symbol, "timeframe": timeframe,
                  "strategy": strategy, "walk_forward": gate, "ts": _now_iso()},
        )

    # ---------- 演示后端 ----------

    def _run_mock(self, task: str, workspace: str,
                  symbol: str, timeframe: str, strategy: str,
                  exchange: str, note: str = "mock") -> AgentResult:
        out_dir = Path(workspace) / "quant_backtest_output"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
        safe_symbol = re.sub(r"[^A-Z0-9]", "_", symbol)
        name = f"bt_{safe_symbol}_{timeframe}_{strategy}_{ts}.md"

        # mock 指标（演示数据，sidecar 接入后由真实数据替换）
        metrics = {
            "total_return": "+18.4%",
            "sharpe": "1.32",
            "max_drawdown": "-12.1%",
            "win_rate": "48.5%",
            "trade_count": "87",
            "walk_forward": "MODERATE",  # 质量门判定
        }
        gate = GATE_LEVELS.get(metrics["walk_forward"], "未知")

        content = (
            f"# 回测报告（{note}）\n\n"
            f"- 任务：{task}\n"
            f"- 策略：{strategy}（{BUILTIN_STRATEGIES[strategy]}）\n"
            f"- 标的：{symbol} @ {exchange}\n"
            f"- 周期：{timeframe}\n"
            f"- 生成时间（UTC）：{_now_iso()}\n\n"
            f"## 回测指标\n\n"
            f"| 指标 | 数值 |\n"
            f"|------|------|\n"
            f"| 总收益 | {metrics['total_return']} |\n"
            f"| 夏普比率 | {metrics['sharpe']} |\n"
            f"| 最大回撤 | {metrics['max_drawdown']} |\n"
            f"| 胜率 | {metrics['win_rate']} |\n"
            f"| 交易次数 | {metrics['trade_count']} |\n\n"
            f"## 质量门（walk-forward 过拟合判定）\n\n"
            f"**判定：{metrics['walk_forward']}** —— {gate}\n\n"
            f"## 下一步建议（供 retro_loop 周复盘参考）\n\n"
            f"- [ ] 若 ROBUST：进虚拟盘跑满 1 个月\n"
            f"- [ ] 若 MODERATE：加过滤条件（趋势过滤/波动率过滤）再回测\n"
            f"- [ ] 若 WEAK/OVERFITTED：淘汰，沉淀教训到知识库\n\n"
            f"## 教训沉淀\n\n"
            f"（retro_loop 每周复盘时自动补充这一节）\n"
        )
        (out_dir / name).write_text(content, encoding="utf-8")

        return AgentResult(
            True, self.name, "done",
            f"[{self.name}/{note}] 回测完成 {symbol} {timeframe} {strategy} -> {metrics['walk_forward']}",
            artifact_paths=[f"{out_dir.name}/{name}"],
            meta={
                "mode": note,
                "symbol": symbol,
                "timeframe": timeframe,
                "strategy": strategy,
                "walk_forward": metrics["walk_forward"],
                "gate": gate,
                "ts": _now_iso(),
            },
        )

    # ---------- 本地通用滚动 walk-forward（不依赖 sidecar，供所有策略复用） ----------

    @staticmethod
    def local_trades_walkforward(trades: list[dict], train_len: int,
                                 test_len: int, step: int | None = None,
                                 date_key: str = "exit_date",
                                 pnl_key: str = "pnl") -> dict:
        """对逐笔交易跑本地滚动 walk-forward（训练窗 IS -> 样本外窗 OOS -> 前移）。

        其他 connector（paper_trader / screener / 二四棒 / 多因子）可直接调用，
        无需配置 tradingview-mcp。返回 IS/OOS 指标、逐窗结果与质量门判定。
        """
        return wf.trades_walkforward(trades, train_len, test_len, step,
                                     date_key=date_key, pnl_key=pnl_key)

    @staticmethod
    def local_equity_walkforward(equity: list[float], train_len: int,
                                 test_len: int, step: int | None = None) -> dict:
        """对逐日净值序列跑本地滚动 walk-forward（输出 IS/OOS 夏普/回撤/质量门）。"""
        return wf.equity_walkforward(equity, train_len, test_len, step)

    @staticmethod
    def gate_verdict(is_metrics: dict, oos_metrics: dict,
                     oos_windows: list[dict]) -> dict:
        """独立调用质量门判定（ROBUST/MODERATE/WEAK/OVERFITTED）。"""
        return wf.classify_gate(is_metrics, oos_metrics, oos_windows).to_dict()

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
            "summary": (
                f"策略历史回测 + 滚动 walk-forward 过拟合质量门（9 种内置策略："
                f"{', '.join(BUILTIN_STRATEGIES.keys())}）；另提供本地通用滚动 "
                f"walk-forward（local_trades_walkforward / local_equity_walkforward / "
                f"gate_verdict），不依赖 sidecar，供所有策略复用"
            ),
            "params": {
                "task": {
                    "type": "string",
                    "required": True,
                    "description": "如 '回测 BTCUSDT @ BINANCE 1d rsi' 或 'AAPL 日线 macd'",
                },
                "local_walkforward": {
                    "type": "object",
                    "description": "本地滚动 walk-forward（代码调用，非自然语言任务）",
                    "properties": {
                        "trades": {"type": "array", "description": "逐笔交易，含 exit_date 与 pnl"},
                        "train_len": {"type": "integer", "description": "训练窗笔数"},
                        "test_len": {"type": "integer", "description": "样本外窗笔数"},
                        "step": {"type": "integer", "description": "前移笔数，默认=test_len"},
                    },
                },
            },
        }
