"""update_incremental 写库韧性回归测试（纯标准库 + 临时 sqlite，不触网）。"""

from __future__ import annotations

import sys
import types
import sqlite3
from pathlib import Path

# 主机部署上下文 connectors 为子集，可能不含 tushare_data；测试只覆盖写库
# 韧性纯函数，不触网，先注入 stub 避免模块级 import 失败。
import connectors  # noqa: F401  (命名空间包)
_stub = types.ModuleType("connectors.tushare_data")
_stub.get_pro = lambda: None
sys.modules.setdefault("connectors.tushare_data", _stub)

import update_incremental as ui


def _ensure_schema(conn) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS daily(
           code TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL,
           volume REAL, amount REAL, turn REAL, PRIMARY KEY(code,date))""")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS downloaded(
           code TEXT, last_date TEXT, download_time TEXT)""")
    conn.commit()


def _mk_recs(day: str, n: int) -> tuple[list[tuple], dict]:
    recs, last = [], {}
    for i in range(n):
        code = f"sh.{600000 + i}"
        recs.append((code, day, 10.0, 11.0, 9.5, 10.5, 1000, 10000.0, 1.2))
        last[code] = day
    return recs, last


def test_is_lock_error_classifies_only_lockish():
    assert ui._is_lock_error(sqlite3.OperationalError("database is locked"))
    assert ui._is_lock_error(sqlite3.OperationalError("database is locked"))
    assert ui._is_lock_error(sqlite3.OperationalError("busy"))
    assert not ui._is_lock_error(sqlite3.OperationalError("no such table: foo"))
    assert not ui._is_lock_error(sqlite3.OperationalError("disk I/O error"))


def test_write_day_batches_and_persists(tmp_path: Path, monkeypatch):
    db = tmp_path / "daily.db"
    monkeypatch.setattr(ui, "DB_PATH", str(db))
    conn = ui._connect()
    try:
        _ensure_schema(conn)
        recs, last = _mk_recs("2026-11-25", 1200)  # 超过单批 500，分 3 批
        ui._write_day(conn, "2026-11-25", recs, last)
        cur = conn.execute("SELECT COUNT(*) FROM daily WHERE date=?", ("2026-11-25",))
        assert cur.fetchone()[0] == 1200
        cur = conn.execute("SELECT COUNT(*) FROM downloaded")
        assert cur.fetchone()[0] == 1200
    finally:
        conn.close()


def test_connect_sets_pragmas(tmp_path: Path, monkeypatch):
    db = tmp_path / "daily.db"
    monkeypatch.setattr(ui, "DB_PATH", str(db))
    conn = ui._connect()
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == ui.BUSY_TIMEOUT_MS
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        conn.close()


def test_retry_succeeds_after_locked(monkeypatch):
    calls = {"n": 0}

    def fake_write(conn, day, recs, last):
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ui, "_write_day", fake_write)
    monkeypatch.setattr(ui.time, "sleep", lambda _s: None)
    ui._write_day_with_retry(None, "2026-11-25", [], {})
    assert calls["n"] == 3  # 前两次撞锁，第三次成功


def test_retry_exhausts_and_raises(monkeypatch):
    calls = {"n": 0}

    def always_locked(conn, day, recs, last):
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(ui, "_write_day", always_locked)
    monkeypatch.setattr(ui.time, "sleep", lambda _s: None)
    try:
        ui._write_day_with_retry(None, "2026-11-25", [], {})
        assert False, "应当抛 OperationalError"
    except sqlite3.OperationalError:
        pass
    # 初始 1 次 + MAX_RETRIES 次重试
    assert calls["n"] == ui.MAX_RETRIES + 1


def test_non_lock_error_is_not_retried(monkeypatch):
    calls = {"n": 0}

    def io_error(conn, day, recs, last):
        calls["n"] += 1
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(ui, "_write_day", io_error)
    monkeypatch.setattr(ui.time, "sleep", lambda _s: None)
    try:
        ui._write_day_with_retry(None, "2026-11-25", [], {})
        assert False, "应当抛原始错误"
    except sqlite3.OperationalError:
        pass
    assert calls["n"] == 1  # 非锁错误不重试
