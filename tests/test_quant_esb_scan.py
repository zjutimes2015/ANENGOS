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
    with patch.object(es, "esb_signal", side_effect=lambda *a, **k: {
            "entry_px": 10.5, "stop_px": 9.8, "shares": 1000,
            "peak_idx": 30, "level_idx": 15} if a[0][-1].startswith("600001") else None), \
         patch.object(es, "esb_pending_signal", side_effect=lambda *a, **k: {
            "entry_px": 10.5, "stop_px": 9.8, "shares": 1000, "peak_idx": 30,
            "level_idx": 15, "gap_pct": 4.0 if a[0][-1].startswith("600002") else 1.0,
            "close_px": 9.0} if not a[0][-1].startswith("600001") else None):
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
