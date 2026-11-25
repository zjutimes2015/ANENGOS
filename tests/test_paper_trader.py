"""paper_trader / 风控 / 持久化 / 题材打分 回归测试（纯标准库，不触网）。"""

from __future__ import annotations

import json
from pathlib import Path

from connectors import paper_trader as pt
from connectors import quant_theme as theme
from connectors import quant_ersibang as q


# ---- 成本/滑点口径 ----

def test_buy_exec_min_commission():
    bx = pt._buy_exec(10.0, 100)  # 小额，佣金应触最低 5 元
    assert bx["commission"] == 5.0
    assert bx["fill"] > 10.0  # 买入滑点上浮
    assert bx["outlay"] > bx["fill"] * 100


def test_sell_exec_stamp_tax_only_on_sell():
    sx = pt._sell_exec(10.0, 100)
    assert sx["stamp"] > 0  # 卖出收印花税
    assert sx["fill"] < 10.0  # 卖出滑点下浮
    assert sx["net"] < sx["fill"] * 100


# ---- 组合风控闸门 ----

def _pos(code="600000", price=10.0, shares=1000):
    return {"code": code, "current_price": price, "shares": shares}


def test_risk_check_halt_blocks_all_buy():
    ok, why = pt._risk_check("600001", 1000.0, 1000.0, [], 1_000_000, 1_000_000, True)
    assert not ok and "熔断" in why


def test_risk_check_cash_short():
    ok, why = pt._risk_check("600001", 50_000.0, 50_000.0, [], 1_000_000, 1_000.0, False)
    assert not ok and "现金不足" in why


def test_risk_check_single_position_cap():
    # 已有该票 18 万市值，再加 5 万 → 单票 >20%（20 万）被拦
    pos = [_pos("600001", 18.0, 10_000)]  # 18 万
    ok, why = pt._risk_check("600001", 50_000.0, 50_000.0, pos, 1_000_000, 1_000_000, False)
    assert not ok and "单票" in why


def test_risk_check_gross_exposure_cap():
    # 持仓 88 万（已超 50 万绝对额上限），再加 5 万 → 总仓被拦
    pos = [_pos("600001", 10.0, 88_000)]
    ok, why = pt._risk_check("600002", 50_000.0, 50_000.0, pos, 1_000_000, 1_000_000, False)
    assert not ok and "总仓位" in why


def test_risk_check_absolute_cap_500k_blocks_over():
    # 现有 40 万市值，新买 15 万 → 合计 55 万 > 50 万绝对额上限被拦
    pos = [_pos("600001", 10.0, 40_000)]
    ok, why = pt._risk_check("600002", 150_000.0, 150_000.0, pos, 1_000_000, 1_000_000, False)
    assert not ok and "总仓位" in why


def test_risk_check_absolute_cap_allows_within_500k():
    # 现有 30 万 + 新买 15 万 = 45 万 ≤ 50 万；新买单票 15% ≤ 20% → 放行
    pos = [_pos("600001", 10.0, 30_000)]
    ok, why = pt._risk_check("600002", 150_000.0, 150_000.0, pos, 1_000_000, 1_000_000, False)
    assert ok and why == ""


def test_risk_check_pass_when_within_limits():
    pos = [_pos("600001", 10.0, 10_000)]  # 10 万
    ok, why = pt._risk_check("600002", 20_000.0, 20_000.0, pos, 1_000_000, 800_000.0, False)
    assert ok and why == ""


# ---- 回撤熔断 ----

def test_drawdown_halt_triggers_at_threshold():
    hist = [{"equity": 1_000_000}, {"equity": 9_000_000}]
    halt, dd = pt._drawdown_halt(hist, 7_600_000)  # 峰值 900 万 → 回撤 15.6%
    assert halt and dd >= pt.CIRCUIT_BREAKER_DD


def test_drawdown_halt_clear_above_threshold():
    hist = [{"equity": 1_000_000}]
    halt, dd = pt._drawdown_halt(hist, 950_000)
    assert not halt and dd < pt.CIRCUIT_BREAKER_DD


# ---- 持久化：原子写 + 损坏备份 + 不静默丢仓 ----

def test_atomic_write_and_read_back(tmp_path: Path):
    f = tmp_path / "paper_positions.json"
    pt._atomic_write_json(f, {"cash": 1.0})  # 首次建文件，无旧版可备份
    payload = {"positions": [{"code": "600018"}], "history": [], "cash": 500_000.0}
    pt._atomic_write_json(f, payload)  # 二次写：旧版留 .bak
    data, warns = pt._load_account(f)
    assert data["cash"] == 500_000.0 and data["positions"][0]["code"] == "600018"
    assert warns == []
    assert (tmp_path / "paper_positions.json.bak").exists()


def test_corrupt_file_backed_up_not_wiped(tmp_path: Path):
    f = tmp_path / "paper_positions.json"
    f.write_text("{ this is not json ", encoding="utf-8")
    data, warns = pt._load_account(f)
    # 回退空结构，但带告警；原损坏文件保留，另存 corrupt 样本
    assert data["positions"] == [] and data["history"] == []
    assert warns and "解析失败" in warns[0]
    assert f.exists()
    corrupt = list(tmp_path.glob("paper_positions.json.corrupt-*"))
    assert corrupt


# ---- 挂单有效期：日历不可用时跳周末 ----

def test_next_trade_day_skips_weekend():
    # 2026-11-13 周五 → 下一个自然日跳过周六周日 → 周一 11-16
    assert pt._next_trade_day("2026-11-13") == "2026-11-16"


# ---- 题材打分：缺维度剔除分母，不被 0 拉低 ----

def test_theme_score_ignores_missing_dimension():
    board = {
        "change_pct": 6.0,     # 涨幅满分档（+6% → 30 分）
        "leader_pct": 10.0,    # 领涨满分档（10% → 20 分）
        "up_count": None, "down_count": None,      # 广度缺数据
        "main_net_inflow": None,                  # 资金流缺数据
    }
    # 仅涨幅+领涨两维（权重 30/20），满分应归一到 100，而不是被缺维度压到 50 分档
    assert theme.score_board(board) == 100.0


def test_theme_score_partial_dimensions():
    board = {
        "change_pct": -2.0, "leader_pct": 0.0,
        "up_count": None, "down_count": None, "main_net_inflow": None,
    }
    assert theme.score_board(board) == 0.0


# ---- P0 风控封顶：风险定额定仓 / 窄距离保护 / 上限约束 ----

def test_size_shares_risk_budget_basis():
    # 100万本金、1% 风险预算=1万；入场10、止损9.50，每股风险0.50 → 2万股
    shares = q.size_shares(1_000_000, 0.01, 10.0, 9.50)
    assert shares == 20_000
    # 止损潜在亏损不超过风险预算
    assert (10.0 - 9.50) * shares <= 1_000_000 * 0.01 + 100 * 10.0


def test_size_shares_narrow_stop_distance_caps_leverage():
    # 止损距离仅0.10（< 入场价2%）：不放大到旧公式的 10万股；
    # 本例单票市值20%上限先到（2万股），与距离折算共同压住仓位。
    shares = q.size_shares(1_000_000, 0.01, 10.0, 9.90)
    assert shares == 20_000
    assert shares < 100_000  # 未保护旧公式会得到 100,000 股


def test_size_shares_narrow_distance_folded_min_dist():
    # 放宽市值上限以隔离距离折算：0.10 的窄距离按 0.20 折算，股数不翻倍
    shares = q.size_shares(1_000_000, 0.01, 10.0, 9.90, position_cap_pct=1.0)
    assert shares == 50_000  # 旧公式（按0.10）会得到 100,000 股


def test_size_shares_position_cap_bounds_market_value():
    # 风险定额算出的股数受单票市值20%上限约束（入场3元、预算放宽）
    shares = q.size_shares(1_000_000, 0.15, 3.0, 2.70)
    assert 3.0 * shares <= 1_000_000 * pt.SINGLE_POSITION_CAP
    assert shares == 66_600


def test_size_shares_cash_bound():
    # 可用现金 5000 元、入场 10 元 → 最多 500 股
    shares = q.size_shares(1_000_000, 0.01, 10.0, 9.50, cash=5_000.0)
    assert shares == 500


def test_risk_check_blocks_above_risk_budget():
    # 单笔潜在亏损 1.8 万 > 1.5% 预算 1.5 万 → 拦截
    ok, why = pt._risk_check("600001", 1000.0, 1000.0, [], 1_000_000,
                             1_000_000, False, risk_amt=18_000.0)
    assert not ok and "风险" in why


def test_risk_check_pass_within_risk_budget():
    # 潜在亏损 8000 ≤ 预算 1.5 万，其余条件满足 → 放行
    ok, why = pt._risk_check("600002", 20_000.0, 20_000.0, [], 1_000_000,
                             800_000.0, False, risk_amt=8_000.0)
    assert ok and why == ""


# ---- 取数兜底：在线/本地双源日期比对，取较新版并标注来源 ----

import pandas as pd  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import patch  # noqa: E402


def _daily_rows(dates, close=10.0):
    return [{"date": d, "open": close, "high": close + 0.1,
             "low": close - 0.1, "close": close} for d in dates]


def _make_online(dates, close=10.0):
    return pd.DataFrame(_daily_rows(dates, close))


def _run_daily(online_dates, cache_dates, online_raises=False):
    """隔离网络与本地库，patch 双源后调用 _daily，返回 (df, meta)。"""
    online_df = _make_online(online_dates) if online_dates else pd.DataFrame()

    def _fake_ak(**_kwargs):
        if online_raises:
            raise RuntimeError("online down")
        return online_df

    fake_ak = SimpleNamespace(stock_zh_a_daily=_fake_ak)
    with patch.object(pt, "_AK_AVAILABLE", True), \
         patch.object(pt, "ak", fake_ak), \
         patch.object(pt, "_cache_daily", return_value=_daily_rows(cache_dates)):
        return pt._daily("600000")


def test_daily_picks_cache_when_online_lagging():
    # 在线停在 11-13，本地库已入 11-16 → 采用本地较新版
    df, meta = _run_daily(["2026-11-12", "2026-11-13"],
                          ["2026-11-13", "2026-11-16"])
    assert meta["source"] == "cache_newer"
    assert meta["last_date"] == "2026-11-16"
    assert meta["online_date"] == "2026-11-13"
    assert meta["cache_date"] == "2026-11-16"
    assert "本地库" in meta["note"]
    assert df is not None and str(df["date"].iloc[-1])[:10] == "2026-11-16"


def test_daily_online_newer_wins():
    # 在线 11-16、本地仅到 11-13 → 采用在线
    df, meta = _run_daily(["2026-11-13", "2026-11-16"],
                         ["2026-11-12", "2026-11-13"])
    assert meta["source"] == "online"
    assert meta["last_date"] == "2026-11-16"
    assert meta["cache_date"] == "2026-11-13"
    assert df is not None


def test_daily_fallback_cache_when_online_error():
    # 在线抛异常 → 回退本地库（标记 cache，而非沿用旧在线序列）
    df, meta = _run_daily([], ["2026-11-15", "2026-11-16"], online_raises=True)
    assert meta["source"] == "cache"
    assert meta["last_date"] == "2026-11-16"
    assert df is not None


def test_daily_none_when_both_empty():
    # 在线异常且本地库为空 → 返回 None + none 标记
    df, meta = _run_daily([], [], online_raises=True)
    assert df is None
    assert meta["source"] == "none"
    assert meta["last_date"] is None


# ---- 成交日期口径：卖出记行情日；全局市场日取日历最新交易日 ----

def test_sell_date_prefers_quote_day():
    # 行情日不早于买入日 → 记行情日，而非滞后的日历日
    assert pt._sell_date("2026-11-16", "2026-10-16", "2026-11-11") == "2026-11-16"
    # 行情日倒退到买入日之前 → 回退日历日，杜绝 buy>sell 倒挂
    assert pt._sell_date("2026-10-10", "2026-10-16", "2026-11-11") == "2026-11-11"


def test_quote_stale_when_quote_lags_mkt():
    # 行情日早于全局交易日 → 滞后估值；同日或无行情日不判滞后
    assert pt._quote_stale("2026-11-23", "2026-11-24") is True
    assert pt._quote_stale("2026-11-24", "2026-11-24") is False
    assert pt._quote_stale(None, "2026-11-24") is False


def test_mkt_date_uses_latest_calendar_day(tmp_path: Path):
    # 台账旧候选停在 11-11，但权威日历已推进到 11-17 → 取日历最新日，不被旧候选卡住
    (tmp_path / "paper_positions.json").write_text(json.dumps({
        "last_market_date": "2026-11-11",
        "history": [{"sell_date": "2026-11-11"}],
        "positions": [{"buy_date": "2026-10-16"}],
    }), encoding="utf-8")
    cal = pd.DataFrame({"trade_date": ["2026-11-10", "2026-11-11", "2026-11-16", "2026-11-17"]})
    fake_ak = SimpleNamespace(tool_trade_date_hist_sina=lambda: cal)
    with patch.object(pt, "ak", fake_ak):
        assert pt._mkt_date(tmp_path, "2026-11-18") == "2026-11-17"
