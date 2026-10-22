"""通用滚动 walk-forward 框架单元测试（纯标准库，不依赖 sidecar/网络）。"""

from __future__ import annotations

from connectors import walkforward as wf


def _trades(pnls: list[float], start: int = 0) -> list[dict]:
    return [{"exit_date": f"2024-01-{i + 1:02d}", "pnl": p}
            for i, p in enumerate(pnls)]


def test_trade_metrics_basic():
    m = wf.trade_metrics(_trades([100, -50, 200, -50]))
    assert m["trades"] == 4
    assert m["wins"] == 2 and m["losses"] == 2
    assert m["win_rate"] == 50.0
    assert m["gross_win"] == 300.0
    assert m["gross_loss"] == 100.0
    assert m["profit_factor"] == 3.0
    assert m["net_pnl"] == 200.0


def test_trade_metrics_empty():
    m = wf.trade_metrics([])
    assert m["trades"] == 0 and m["profit_factor"] is None


def test_equity_metrics_drawdown_and_return():
    eq = [1_000_000, 1_100_000, 990_000, 1_050_000]
    m = wf.equity_metrics(eq)
    assert m["total_return_pct"] == 5.0
    assert m["max_dd_pct"] < 0  # 1.1M 峰 -> 0.99M 谷
    assert isinstance(m["sharpe"], float)


def test_rolling_windows_count():
    wins = wf.rolling_windows(n=100, train_len=40, test_len=20, step=20)
    # 40+20=60 起，每20前移，到 80->100 为止，共3段
    assert len(wins) == 3
    assert wins[0] == (0, 40, 40, 60)
    assert wins[2] == (40, 80, 80, 100)


def test_gate_overfitted():
    v = wf.classify_gate(
        {"net_pnl": 10000}, {"net_pnl": -3000, "sharpe": -0.2},
        [{"net_pnl": -3000}],
    )
    assert v.level == "OVERFITTED"


def test_gate_weak():
    # IS 非正、OOS 为负 -> WEAK（不触发 IS正/OOS负 的 OVERFITTED）
    v = wf.classify_gate(
        {"net_pnl": -50}, {"net_pnl": -100, "sharpe": 0.0},
        [{"net_pnl": -100}],
    )
    assert v.level == "WEAK"


def test_gate_robust():
    windows = [{"net_pnl": 100}, {"net_pnl": 120}, {"net_pnl": 90}, {"net_pnl": 110}]
    v = wf.classify_gate(
        {"net_pnl": 420}, {"net_pnl": 420, "sharpe": 1.1}, windows,
    )
    assert v.level == "ROBUST"


def test_trades_walkforward_end_to_end():
    # 前半训练窗稳定盈利，样本外窗也为正 -> ROBUST 路径可跑通
    trades = _trades([100, 100, -50, 100, 100, -50, 100, 100, -50, 100,
                      100, -50, 100, 100, -50, 100, 100, -50, 100, 100])
    res = wf.trades_walkforward(trades, train_len=10, test_len=5, step=5)
    assert res["window_count"] >= 1
    assert "level" in res["gate"]
    assert res["oos_metrics"]["trades"] == res["oos_trades_count"]
