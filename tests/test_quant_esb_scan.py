"""quant_esb_scan 全A二四棒扫描回归测试（纯标准库 + 临时 sqlite + mock，不触网）。"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

from connectors import quant_esb_scan as es


def _stock(code: str = "600000", close: float = 10.0, n: int = 50) -> dict:
    return {
        "code": code, "name": code,
        "dates": [f"2026-11-{d:02d}" for d in range(1, n + 1)],
        "opens": [close] * n, "highs": [close + 0.1] * n,
        "lows": [close - 0.1] * n, "closes": [close] * n,
        "volumes": [1e6] * n, "close": close, "last_date": "2026-11-20",
    }


# ---- _strip_code ----

def test_strip_code_sh_sz():
    assert es._strip_code("sh.600000") == "600000"
    assert es._strip_code("sz.000001") == "000001"


def test_strip_code_bj_rejected():
    assert es._strip_code("bj.920001") == ""
    assert es._strip_code("") == ""


# ---- scan_one 信号分支 ----

def test_scan_one_breakout_branch():
    fake_sig = {"entry_px": 10.5, "stop_px": 9.8, "shares": 2000,
                "peak_idx": 30, "level_idx": 15}
    with patch.object(es, "esb_signal", return_value=fake_sig), \
         patch.object(es, "esb_pending_signal", return_value=None) as pend:
        out = es.scan_one(_stock())
    assert out is not None
    assert out["type"] == "breakout"
    assert out["entry_px"] == 10.5 and out["stop_px"] == 9.8
    assert out["shares"] == 2000 and out["gap_pct"] == 0.0
    pend.assert_not_called()


def test_scan_one_pending_branch_when_no_breakout():
    fake_pend = {"entry_px": 10.5, "stop_px": 9.8, "shares": 1000,
                 "peak_idx": 30, "level_idx": 15, "gap_pct": 2.4, "close_px": 10.2}
    with patch.object(es, "esb_signal", return_value=None), \
         patch.object(es, "esb_pending_signal", return_value=fake_pend):
        out = es.scan_one(_stock())
    assert out is not None
    assert out["type"] == "pending"
    assert out["gap_pct"] == 2.4 and out["close_px"] == 10.2


def test_scan_one_no_signal_returns_none():
    with patch.object(es, "esb_signal", return_value=None), \
         patch.object(es, "esb_pending_signal", return_value=None):
        assert es.scan_one(_stock()) is None


# ---- scan_universe 排序与容错 ----

def test_scan_universe_breakout_first_then_pending_by_gap():
    breakout = _stock("600001")
    pend_far = _stock("600002", close=9.0)
    pend_near = _stock("600003", close=9.5)

    def _one(stock, *a, **k):
        code = stock["code"]
        if code == "600001":
            return {"code": code, "type": "breakout", "gap_pct": 0.0}
        gap = 4.0 if code == "600002" else 1.0
        return {"code": code, "type": "pending", "gap_pct": gap}

    with patch.object(es, "scan_one", side_effect=_one):
        out = es.scan_universe([breakout, pend_far, pend_near])
    types = [o["type"] for o in out]
    assert types[0] == "breakout"
    pending = [o for o in out if o["type"] == "pending"]
    assert [o["gap_pct"] for o in pending] == sorted(o["gap_pct"] for o in pending)
    assert pending[0]["code"] == "600003"


def test_scan_universe_swallows_single_stock_exception():
    bad = _stock("600004")
    good = _stock("600005")
    with patch.object(es, "esb_signal", side_effect=RuntimeError("boom")):
        out = es.scan_universe([bad, good])
    assert out == []  # 异常被吞，不阻断；good 同样无信号


# ---- 本地库过滤 ----

def _make_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE daily (
        code TEXT, date TEXT, open REAL, high REAL, low REAL,
        close REAL, volume REAL, amount REAL, turn REAL,
        PRIMARY KEY (code, date))""")
    conn.commit()

    def seed(full: str, close: float, vol: float = 1e6, n: int = 50):
        rows = [(full, f"2026-11-{d:02d}", close, close + 0.1, close - 0.1,
                 close, vol) for d in range(1, n + 1)]
        conn.executemany(
            "INSERT INTO daily(code,date,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?)",
            rows)

    seed("sh.600000", 10.0)     # 合规
    seed("sz.000001", 1.5)      # 价格过低
    seed("bj.920001", 10.0)      # 北交所
    seed("sh.600002", 10.0, vol=0)  # 当日停牌无量
    conn.commit()
    conn.close()


def test_load_universe_filters():
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "a.db")
        _make_db(db)
        uni = es.load_universe(db)
        codes = {c["code"] for c in uni}
        assert codes == {"600000"}
        rec = next(c for c in uni if c["code"] == "600000")
        assert len(rec["closes"]) == 50


def test_load_universe_missing_db_returns_empty():
    assert es.load_universe("/nonexistent/a.db") == []


# ---- 现金流排雷（纯判定 + 临时 sqlite 缓存 + mock 在线回调，不触网）----

def test_cf_all_three_negative_excluded():
    cache = {2025: -1e8, 2024: -2e8, 2023: -3e8}
    neg, reason = es.cf_three_year_negative("600000", "2026-11-20", cache, lambda c: {})
    assert neg and "现金流" in reason


def test_cf_any_positive_year_kept():
    cache = {2025: -1e8, 2024: 1e8, 2023: -3e8}
    neg, _ = es.cf_three_year_negative("600000", "2026-11-20", cache, lambda c: {})
    assert not neg


def test_cf_missing_year_kept_not_killed():
    cache = {2025: -1e8, 2023: -3e8}  # 2024 缺
    neg, reason = es.cf_three_year_negative("600000", "2026-11-20", cache, lambda c: {})
    assert not neg and "缺失" in reason


def test_apply_cf_filter_split_and_cache():
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "a.db")

        def fake_fetch(code):
            if code == "600001":  # 三年全负 → 剔除
                return {2025: -1.0, 2024: -2.0, 2023: -3.0}
            return {2025: 1.0, 2024: -2.0, 2023: -3.0}  # 有正 → 保留

        results = [
            {"code": "600001", "type": "breakout", "close": 10.0},
            {"code": "600002", "type": "pending", "close": 11.0},
        ]
        kept, excluded = es._apply_cashflow_filter(
            results, "2026-11-20", db_path=db, fetch=fake_fetch, sleep=0.0)
        assert {s["code"] for s in kept} == {"600002"}
        assert {s["code"] for s in excluded} == {"600001"}
        assert "exclude_reason" in excluded[0]

        # 缓存已落库：再次跑不再触发在线回调
        calls = []

        def tracking_fetch(code):
            calls.append(code)
            return fake_fetch(code)

        kept2, excluded2 = es._apply_cashflow_filter(
            results, "2026-11-20", db_path=db, fetch=tracking_fetch, sleep=0.0)
        assert not calls  # 缓存命中，未在线拉取
        assert {s["code"] for s in kept2} == {"600002"}
        assert {s["code"] for s in excluded2} == {"600001"}
