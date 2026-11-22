"""quant_all_a 离线选股 / 入选门槛回归测试（纯标准库 + 临时 sqlite，不触网）。"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from connectors import quant_all_a as qa


def _flat_close(level: float = 10.0, n: int = 60) -> list[float]:
    """近乎恒定的收盘序列 → 20 日波动率≈0（<3%，低波 +2）。"""
    return [level + (0.001 if i % 2 else -0.001) for i in range(n)]


def _volatile_close(n: int = 60) -> list[float]:
    """大幅震荡序列 → 20 日波动率 >7%（高波 -1）。"""
    seq = []
    px = 10.0
    for i in range(n):
        px *= 1.1 if i % 2 else 0.92
        seq.append(px)
    return seq


# ---- _strip_code ----

def test_strip_code_sh_sz():
    assert qa._strip_code("sh.600000") == "600000"
    assert qa._strip_code("sz.000001") == "000001"


def test_strip_code_bj_rejected():
    assert qa._strip_code("bj.920001") == ""


def test_strip_code_invalid():
    assert qa._strip_code("") == ""
    assert qa._strip_code("sh.60000") == ""  # 非 6 位


# ---- score_universe 入选门槛（B 重构后低波满分 +2，门槛 >0）----

def test_lowvol_selected_and_highvol_excluded():
    universe = [
        {"code": "600000", "name": "低波", "closes": _flat_close(),
         "volumes": [1e6] * 60, "close": 10.0, "last_date": "2026-11-20"},
        {"code": "000001", "name": "高波", "closes": _volatile_close(),
         "volumes": [1e6] * 60, "close": 10.0, "last_date": "2026-11-20"},
    ]
    results = qa.score_universe(universe)
    codes = [r["code"] for r in results]
    assert "600000" in codes
    assert "000001" not in codes


def test_score_zero_not_selected():
    """波动落在中性区间（3%~7%）得分 0，不入选。"""
    closes = [10.0 * (1 + (0.03 if i % 2 else -0.03)) for i in range(60)]
    universe = [{"code": "600000", "closes": closes, "volumes": [1e6] * 60,
                 "close": 10.0, "last_date": "2026-11-20"}]
    assert qa.score_universe(universe) == []


def test_score_universe_top_n_truncation():
    universe = [
        {"code": f"60000{i}", "closes": _flat_close(level=10 + i),
         "volumes": [1e6] * 60, "close": 10.0, "last_date": "2026-11-20"}
        for i in range(5)
    ]
    assert len(qa.score_universe(universe, top_n=3)) == 3


# ---- 离线本地库读取 ----

def _make_db(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE daily (
        code TEXT, date TEXT, open REAL, high REAL, low REAL,
        close REAL, volume REAL, amount REAL, turn REAL,
        PRIMARY KEY (code, date))""")
    conn.commit()

    def seed(full: str, close: float, n: int = 60):
        rows = [(full, f"2026-11-{d:02d}", close, close, close, close, 1e6)
                for d in range(1, n + 1)]
        conn.executemany(
            "INSERT INTO daily(code,date,open,high,low,close,volume) VALUES (?,?,?,?,?,?,?)",
            rows)

    seed("sh.600000", 10.0)       # 价格合规、根数足够
    seed("sz.000001", 1.5)        # 价格过低，应被过滤
    seed("bj.920001", 10.0)       # 北交所，应被排除
    conn.commit()
    conn.close()


def test_load_local_universe_filters():
    with tempfile.TemporaryDirectory() as td:
        db = str(Path(td) / "a.db")
        _make_db(db)
        uni = qa._load_local_universe(db)
        codes = {c["code"] for c in uni}
        assert codes == {"600000"}
        rec = next(c for c in uni if c["code"] == "600000")
        assert len(rec["closes"]) == 60
        assert rec["last_date"] == "2026-11-60" or rec["last_date"].startswith("2026-11")


def test_load_local_universe_missing_db_returns_empty():
    assert qa._load_local_universe("/nonexistent/a_stock_daily.db") == []
