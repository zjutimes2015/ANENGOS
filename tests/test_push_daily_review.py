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


def test_positions_bought_today_counted():
    """当日买入仍持有的仓位计入当日买入；旧买入持仓不计入。"""
    positions = [
        {"code": "601838", "name": "成都银行", "buy_date": "2026-11-26", "quote_date": "2026-11-26", "shares": 9300},
        {"code": "600177", "name": "雅戈尔", "buy_date": "2026-11-26", "quote_date": "2026-11-26", "shares": 13800},
        {"code": "600000", "name": "浦发银行", "buy_date": "2026-11-23", "quote_date": "2026-11-26", "shares": 14200},
    ]
    sells, buys = pr._split_trades([], "2026-11-26", positions)
    assert sells == []
    assert {b["code"] for b in buys} == {"601838", "600177"}


def test_position_buy_day_not_quote_day():
    """持仓按 buy_date 归属买入日；当日估值刷新的 quote_date 不得算作当日买入。"""
    positions = [
        {"code": "600000", "name": "浦发银行", "buy_date": "2026-11-23", "quote_date": "2026-11-26", "shares": 14200},
    ]
    _, buys = pr._split_trades([], "2026-11-26", positions)
    assert buys == []


# ---- 健壮性兜底 ----

def test_parse_json_fallback():
    """非 JSON 响应体回退空 dict，None 也不抛异常。"""
    assert pr._parse_json('{"a": 1}') == {"a": 1}
    assert pr._parse_json("not json") == {}
    assert pr._parse_json(None) == {}


def test_max_drawdown_skips_bad_entries():
    """净值序列跳过缺 equity 字段的损坏条目；空序列回撤为 0。"""
    eh = [{"equity": 1100000}, {"note": "bad"}, {"equity": 990000}]
    assert pr._max_drawdown(eh, 1000000) == 10.0
    assert pr._max_drawdown([], 1000000) == 0.0


def test_find_record_match():
    """按日期匹配既有记录；未命中返回 None。"""
    items = [{"fields": {"日期": "2026-11-19"}}, {"fields": {"日期": "2026-11-20"}}]
    assert pr._find_record(items, "2026-11-20")["fields"]["日期"] == "2026-11-20"
    assert pr._find_record(items, "2026-11-21") is None


def test_list_all_records_paginates(monkeypatch):
    """跨页拉全量：首页 has_more + page_token，末页聚合两批记录。"""
    def fake_req(method, url, body=None, token=None):
        if "page_token" not in url:
            return 200, {"data": {"items": [{"record_id": "a", "fields": {"日期": "2026-11-19"}}],
                                  "has_more": True, "page_token": "tok2"}}
        return 200, {"data": {"items": [{"record_id": "b", "fields": {"日期": "2026-11-20"}}],
                             "has_more": False}}
    monkeypatch.setattr(pr, "req", fake_req)
    items = pr._list_all_records(pr.request_with_retry, "t")
    assert [i["record_id"] for i in items] == ["a", "b"]


def test_retryable_status_retries_then_success(monkeypatch):
    """5xx 瞬时错误退避重试，第二次成功。"""
    seq = [503, 200]
    calls = []

    def fake_req(method, url, body=None, token=None):
        calls.append(1)
        return seq.pop(0), {"code": 0}

    monkeypatch.setattr(pr, "req", fake_req)
    monkeypatch.setattr(pr.time, "sleep", lambda _s: None)
    s, _r = pr.request_with_retry("GET", "http://x")
    assert s == 200 and len(calls) == 2


def test_4xx_not_retried(monkeypatch):
    """4xx 与业务错误不重试，直接返回。"""
    calls = []

    def fake_req(method, url, body=None, token=None):
        calls.append(1)
        return 400, {"code": 1254061}

    monkeypatch.setattr(pr, "req", fake_req)
    s, r = pr.request_with_retry("GET", "http://x")
    assert s == 400 and len(calls) == 1 and r["code"] == 1254061
