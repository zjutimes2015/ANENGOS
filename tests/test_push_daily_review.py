"""push_daily_review 复盘日期口径 / 当日成交分类回归测试（纯标准库，不触网）。"""

from __future__ import annotations

from connectors import push_daily_review as pr


def _sell(code, sell_date, quote_day, pnl=0.0, buy_date="2026-10-16"):
    return {
        "code": code, "name": code, "buy_date": buy_date, "sell_date": sell_date,
        "sell_quote_date": quote_day, "quote_date": quote_day, "pnl": pnl, "shares": 50,
    }


def _buy(code, quote_day, buy_date):
    return {"code": code, "name": code, "buy_date": buy_date, "quote_date": quote_day, "shares": 100}


# ---- run_day 推断 ----

def test_run_day_prefers_last_market_date():
    """空仓后基准取台账最新行情日，而不是历史成交里卡住的旧行情日。"""
    d = {"last_market_date": "2026-11-20"}
    hist = [_sell("600018", "2026-11-18", "2026-11-18")]
    assert pr._run_day(d, [], hist, "2026-11-20") == "2026-11-20"


def test_run_day_falls_back_to_record_day():
    """last_market_date 缺失时，回退持仓/成交记录里的最新行情日。"""
    hist = [_sell("603456", "2026-11-16", "2026-11-16"), _sell("600018", "2026-11-18", "2026-11-18")]
    assert pr._run_day({}, [], hist, "2026-11-20") == "2026-11-18"


def test_run_day_falls_back_to_review_date():
    """无台账行情日、无记录行情日时，回退日历当天。"""
    assert pr._run_day({}, [], [], "2026-11-20") == "2026-11-20"


# ---- 当日成交分类 ----

def test_empty_portfolio_old_sell_not_counted_today():
    """复现空仓重复计入：上一日止盈离场的旧成交，不应在后续交易日被当作当日卖出。"""
    d = {"last_market_date": "2026-11-20"}
    hist = [_sell("600018", "2026-11-18", "2026-11-18", 19.55)]
    run_day = pr._run_day(d, [], hist, "2026-11-20")
    sells, buys = pr._split_trades(hist, run_day)
    assert sells == []
    assert buys == []


def test_sell_on_run_day_counted():
    """行情日等于本次运行日的卖出计入当日；旧行情日卖出不计入。"""
    hist = [
        _sell("600018", "2026-11-20", "2026-11-20", 19.55),
        _sell("603456", "2026-11-16", "2026-11-16", -236.55),
    ]
    sells, _ = pr._split_trades(hist, "2026-11-20")
    assert len(sells) == 1
    assert sells[0]["code"] == "600018"


def test_buy_on_run_day_counted_and_sold_excluded():
    """当日未卖出买入计入当日买入；已卖出历史笔不计入买入。"""
    hist = [_buy("600001", "2026-11-20", "2026-11-20"), _sell("600018", "2026-11-20", "2026-11-20", 1.0)]
    sells, buys = pr._split_trades(hist, "2026-11-20")
    assert len(buys) == 1 and buys[0]["code"] == "600001"
    assert len(sells) == 1
