"""通用滚动 Walk-Forward 框架（本地、纯标准库、与具体策略解耦）。

设计目标（供 ANENGOS 所有策略复用：screener / paper_trader / 二四棒 / 多因子 / 自定义策略）：

- 策略方只负责"给定一段数据，跑出该段的逐笔交易或净值"；
- 本框架负责：滚动切窗（训练窗 IS -> 紧随的样本外窗 OOS -> 前移）、
  IS/OOS 指标计算、样本外拼接、IS-OOS 衰减、过拟合质量门判定。

标准滚动 walk-forward 与"全程一把回测"的区别：
  全程回测把未来信息也用于评估，容易高估；walk-forward 只在训练窗内看，
  紧接着的样本外窗是"没见过的数据"，多段样本外拼接后的表现才接近未来。

质量门（ROBUST / MODERATE / WEAK / OVERFITTED）规则全部可复现，不依赖 LLM。

无第三方依赖（不引入 pandas/numpy），便于在容器、测试、cron 中直接调用。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Sequence

TRADING_DAYS = 252  # 年化交易日


# ---------------------------------------------------------------------------
# 基础指标
# ---------------------------------------------------------------------------

def trade_metrics(trades: Sequence[dict], pnl_key: str = "pnl") -> dict:
    """从逐笔交易（每笔含 pnl）计算笔数/胜率/PF/净盈亏。

    返回 dict：trades, wins, losses, win_rate, gross_win, gross_loss,
    profit_factor, net_pnl。空列表返回零值。
    """
    n = len(trades)
    if n == 0:
        return {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
                "gross_win": 0.0, "gross_loss": 0.0, "profit_factor": None,
                "net_pnl": 0.0}
    wins = [t for t in trades if float(t.get(pnl_key, 0)) > 0]
    losses = [t for t in trades if float(t.get(pnl_key, 0)) <= 0]
    gross_win = sum(float(t[pnl_key]) for t in wins)
    gross_loss = abs(sum(float(t[pnl_key]) for t in losses))
    net = sum(float(t[pnl_key]) for t in trades)
    pf = round(gross_win / gross_loss, 2) if gross_loss > 0 else None
    return {
        "trades": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / n * 100, 1),
        "gross_win": round(gross_win, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": pf,
        "net_pnl": round(net, 2),
    }


def equity_metrics(equity: Sequence[float]) -> dict:
    """从逐日净值序列计算总收益/年化/夏普/最大回撤。

    equity: 按时间升序的净值（首项为期初本金，如 1_000_000）。
    夏普无风险利率取 0；样本不足（<2 点）时夏普为 0。
    """
    n = len(equity)
    if n < 2:
        return {"total_return_pct": 0.0, "annual_pct": 0.0,
                "sharpe": 0.0, "max_dd_pct": 0.0, "points": n}
    start, end = float(equity[0]), float(equity[-1])
    total_ret = (end / start - 1) * 100

    # 日收益序列
    rets = [equity[i] / equity[i - 1] - 1 for i in range(1, n)]
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1) if len(rets) > 1 else 0.0
    std = math.sqrt(var)
    sharpe = (mean / std) * math.sqrt(TRADING_DAYS) if std > 1e-12 else 0.0

    # 最大回撤（峰谷）
    peak = equity[0]
    max_dd = 0.0
    for v in equity:
        if v > peak:
            peak = v
        dd = (v / peak - 1)
        if dd < max_dd:
            max_dd = dd
    annual = ((end / start) ** (TRADING_DAYS / (n - 1)) - 1) * 100 if end > 0 else 0.0
    return {
        "total_return_pct": round(total_ret, 2),
        "annual_pct": round(annual, 2),
        "sharpe": round(sharpe, 2),
        "max_dd_pct": round(max_dd * 100, 2),
        "points": n,
    }


# ---------------------------------------------------------------------------
# 滚动切窗
# ---------------------------------------------------------------------------

def rolling_windows(n: int, train_len: int, test_len: int,
                    step: int | None = None) -> list[tuple[int, int, int, int]]:
    """按位置生成滚动窗 (is_start, is_end, oos_start, oos_end)，半开区间。

    train_len/test_len/step 均为"数据点个数"（交易日或交易笔数，由调用方定义）。
    step 默认等于 test_len（不重叠样本外窗）。
    """
    step = step or test_len
    windows = []
    start = 0
    while start + train_len + test_len <= n:
        is_start, is_end = start, start + train_len
        oos_start, oos_end = is_end, is_end + test_len
        windows.append((is_start, is_end, oos_start, oos_end))
        start += step
    return windows


# ---------------------------------------------------------------------------
# 质量门
# ---------------------------------------------------------------------------

@dataclass
class GateVerdict:
    level: str
    reason: str
    is_metrics: dict = field(default_factory=dict)
    oos_metrics: dict = field(default_factory=dict)
    oos_positive_ratio: float = 0.0

    def to_dict(self) -> dict:
        return {
            "level": self.level,
            "reason": self.reason,
            "is_metrics": self.is_metrics,
            "oos_metrics": self.oos_metrics,
            "oos_positive_ratio": round(self.oos_positive_ratio, 2),
        }


def classify_gate(is_metrics: dict, oos_metrics: dict,
                  oos_window_results: Sequence[dict]) -> GateVerdict:
    """按 IS/OOS 指标与样本外窗正收益占比判定质量门。

    规则（优先级从高到低）：
      OVERFITTED : IS 净盈亏为正但 OOS 净盈亏为负（历史好、样本外反转）
      WEAK       : OOS 净盈亏<=0 或 OOS 夏普<=0
      MODERATE   : OOS 为正，但正窗占比<60% 或 IS->OOS 收益衰减>50%
      ROBUST     : OOS 为正、夏普>0、正窗占比>=60%、衰减<=50%
    """
    is_net = float(is_metrics.get("net_pnl", is_metrics.get("total_return_pct", 0)) or 0)
    oos_net = float(oos_metrics.get("net_pnl", oos_metrics.get("total_return_pct", 0)) or 0)
    oos_sharpe = float(oos_metrics.get("sharpe", 0) or 0)

    pos = sum(1 for w in oos_window_results
              if float(w.get("net_pnl", w.get("total_return_pct", 0)) or 0) > 0)
    ratio = pos / len(oos_window_results) if oos_window_results else 0.0

    # IS->OOS 衰减（相对 IS 规模；IS<=0 时不看衰减）
    decay = 1.0
    if is_net > 0:
        decay = max(0.0, 1 - oos_net / is_net)

    if is_net > 0 and oos_net < 0:
        return GateVerdict("OVERFITTED",
                           "训练窗为正但样本外为负，历史表现不可外推",
                           is_metrics, oos_metrics, ratio)
    if oos_net <= 0 or oos_sharpe <= 0:
        return GateVerdict("WEAK",
                           "样本外净盈亏<=0 或样本外夏普<=0，无正期望",
                           is_metrics, oos_metrics, ratio)
    if ratio < 0.6 or decay > 0.5:
        return GateVerdict("MODERATE",
                           f"样本外为正但稳健性不足（正窗占比{ratio:.0%}，衰减{decay:.0%}）",
                           is_metrics, oos_metrics, ratio)
    return GateVerdict("ROBUST",
                       f"样本外稳健（正窗占比{ratio:.0%}，衰减{decay:.0%}）",
                       is_metrics, oos_metrics, ratio)


# ---------------------------------------------------------------------------
# 逐笔交易版滚动 walk-forward（最常用：直接吃 paper_trader / 回测的 trades）
# ---------------------------------------------------------------------------

def trades_walkforward(
    trades: Sequence[dict],
    train_len: int,
    test_len: int,
    step: int | None = None,
    date_key: str = "exit_date",
    pnl_key: str = "pnl",
) -> dict:
    """对逐笔交易做滚动 walk-forward（按交易时间排序后按笔数切窗）。

    参数：
      trades: 逐笔列表，每笔至少含 {date_key, pnl_key}。
      train_len/test_len: 训练窗/样本外窗的"交易笔数"。
      step: 前移笔数，默认=test_len。
    返回：{windows, is_metrics, oos_metrics, gate, oos_trades_count}
    """
    ordered = sorted(trades, key=lambda t: str(t.get(date_key, "")))
    wins = rolling_windows(len(ordered), train_len, test_len, step)
    per_window = []
    oos_all: list[dict] = []
    is_all: list[dict] = []
    for is0, is1, o0, o1 in wins:
        is_slice = ordered[is0:is1]
        oos_slice = ordered[o0:o1]
        is_all.extend(is_slice)
        oos_all.extend(oos_slice)
        m = trade_metrics(oos_slice, pnl_key)
        m["oos_range"] = [str(oos_slice[0].get(date_key, "")),
                          str(oos_slice[-1].get(date_key, ""))]
        per_window.append(m)

    is_m = trade_metrics(is_all, pnl_key)
    oos_m = trade_metrics(oos_all, pnl_key)
    verdict = classify_gate(is_m, oos_m, per_window)
    return {
        "windows": per_window,
        "is_metrics": is_m,
        "oos_metrics": oos_m,
        "gate": verdict.to_dict(),
        "oos_trades_count": len(oos_all),
        "window_count": len(wins),
    }


# ---------------------------------------------------------------------------
# 净值序列版滚动 walk-forward（吃逐日 equity，输出 IS/OOS 夏普/回撤）
# ---------------------------------------------------------------------------

def equity_walkforward(
    equity: Sequence[float],
    train_len: int,
    test_len: int,
    step: int | None = None,
) -> dict:
    """对逐日净值做滚动 walk-forward，样本外窗拼接后整体评估。"""
    wins = rolling_windows(len(equity), train_len, test_len, step)
    per_window = []
    oos_concat: list[float] = []
    for is0, is1, o0, o1 in wins:
        # 样本外窗净值相对窗起点归一，便于拼接成连续样本外曲线
        base = equity[o0]
        seg = [v / base for v in equity[o0:o1]]
        oos_concat.extend(seg)
        per_window.append(equity_metrics(equity[o0:o1]))

    is_m = equity_metrics(equity[:wins[0][1]] if wins else equity)
    oos_m = equity_metrics(oos_concat)
    verdict = classify_gate(is_m, oos_m, per_window)
    return {
        "windows": per_window,
        "is_metrics": is_m,
        "oos_metrics": oos_m,
        "gate": verdict.to_dict(),
        "window_count": len(wins),
    }


# ---------------------------------------------------------------------------
# 通用回调版（策略自带参数优化时使用）
# ---------------------------------------------------------------------------

def callback_walkforward(
    n: int,
    train_len: int,
    test_len: int,
    step: int | None,
    train_fn: Callable[[int, int], dict],
    test_fn: Callable[[int, int, dict], dict],
) -> dict:
    """最通用的滚动框架：train_fn(is0,is1) 返回该窗训练产物（可选含选参），
    test_fn(oos0,oos1,train_artifact) 返回该样本外窗指标。框架只负责切窗与汇总。
    """
    wins = rolling_windows(n, train_len, test_len, step)
    per_window = []
    for is0, is1, o0, o1 in wins:
        artifact = train_fn(is0, is1)
        per_window.append(test_fn(o0, o1, artifact))
    return {"windows": per_window, "window_count": len(wins)}
