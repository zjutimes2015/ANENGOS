"""kline_cache.py: 本地K线数据库（sqlite缓存）。

设计：
- 数据库文件：/srv/anengos/workspace/kline_cache.db
- 表：daily_kline (code, date, open, high, low, close, volume, amount)
- 主键：(code, date)

功能：
1. 查一只股票的日线数据（优先从缓存读）
2. 缓存没有，从baostock拉，存进缓存
3. 自动判断是否需要更新（缓存里最新日期 vs 今天）

用法：
    from kline_cache import get_kline
    df = get_kline("sh.600000", start_date="2024-01-01", end_date="2026-10-24")
"""
from __future__ import annotations
import datetime
import sqlite3
from pathlib import Path

DB_PATH = Path("/srv/anengos/workspace/kline_cache.db")


def init_db():
    """初始化数据库表。"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_kline (
            code TEXT NOT NULL,
            date TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            amount REAL,
            PRIMARY KEY (code, date)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_kline_date ON daily_kline(date)")
    conn.commit()
    conn.close()


def get_cached_range(code: str) -> tuple[str | None, str | None]:
    """查缓存里某只股票的日期范围。"""
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute(
        "SELECT MIN(date), MAX(date) FROM daily_kline WHERE code = ?", (code,)
    )
    row = cur.fetchone()
    conn.close()
    return (row[0], row[1]) if row else (None, None)


def fetch_from_baostock(code: str, start_date: str, end_date: str) -> list[tuple]:
    """从baostock拉日线数据。"""
    try:
        import baostock as bs
        import pandas as pd

        lg = bs.login()
        if lg.error_code != "0":
            return []

        rs = bs.query_history_k_data_plus(
            code,
            "date,open,high,low,close,volume,amount",
            start_date=start_date,
            end_date=end_date,
            frequency="d",
            adjustflag="2",  # 前复权
        )

        rows = []
        while rs.error_code == "0" and rs.next():
            r = rs.get_row_data()
            # 转成数字
            try:
                rows.append((
                    code,
                    r[0],
                    float(r[1]) if r[1] else None,
                    float(r[2]) if r[2] else None,
                    float(r[3]) if r[3] else None,
                    float(r[4]) if r[4] else None,
                    float(r[5]) if r[5] else None,
                    float(r[6]) if r[6] else None,
                ))
            except (ValueError, IndexError):
                continue

        bs.logout()
        return rows
    except Exception as e:
        print(f"[KLINE_CACHE] baostock拉取失败 {code}: {e}")
        return []


def save_to_cache(rows: list[tuple]):
    """把数据存进缓存（去重）。"""
    if not rows:
        return
    conn = sqlite3.connect(DB_PATH)
    conn.executemany(
        """INSERT OR REPLACE INTO daily_kline
           (code, date, open, high, low, close, volume, amount)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()
    conn.close()


def get_kline(code: str, start_date: str, end_date: str) -> list[dict]:
    """
    查K线数据（优先缓存，没有则从baostock拉）。

    参数：
        code: 股票代码，如 "sh.600000"
        start_date: 开始日期 "2024-01-01"
        end_date: 结束日期 "2026-10-24"

    返回：
        列表，每个元素是一天的数据dict
    """
    init_db()

    # 1. 先查缓存里有没有
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """SELECT date, open, high, low, close, volume, amount
           FROM daily_kline
           WHERE code = ? AND date >= ? AND date <= ?
           ORDER BY date""",
        (code, start_date, end_date),
    )
    cached = [dict(row) for row in cur.fetchall()]
    conn.close()

    # 2. 如果缓存里已经覆盖了整个区间，直接返回
    if cached and cached[0]["date"] <= start_date and cached[-1]["date"] >= end_date:
        print(f"[KLINE_CACHE] {code} 缓存命中 ({len(cached)}条)")
        return cached

    # 3. 否则从baostock拉
    print(f"[KLINE_CACHE] {code} 缓存未命中，从baostock拉取 {start_date}~{end_date}")
    rows = fetch_from_baostock(code, start_date, end_date)
    save_to_cache(rows)

    # 4. 再从缓存读一次
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """SELECT date, open, high, low, close, volume, amount
           FROM daily_kline
           WHERE code = ? AND date >= ? AND date <= ?
           ORDER BY date""",
        (code, start_date, end_date),
    )
    result = [dict(row) for row in cur.fetchall()]
    conn.close()
    print(f"[KLINE_CACHE] {code} 拉取完成 ({len(result)}条)")
    return result


def get_cache_stats() -> dict:
    """统计缓存情况。"""
    init_db()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("SELECT COUNT(DISTINCT code), COUNT(*), MIN(date), MAX(date) FROM daily_kline")
    row = cur.fetchone()
    conn.close()
    return {
        "股票数": row[0],
        "总条数": row[1],
        "最早日期": row[2],
        "最新日期": row[3],
        "数据库大小MB": round(DB_PATH.stat().st_size / 1024 / 1024, 1) if DB_PATH.exists() else 0,
    }


if __name__ == "__main__":
    # 测试
    print("=== 测试K线缓存 ===")

    # 查一只股票
    df = get_kline("sh.600000", "2024-01-01", "2024-01-31")
    print(f"浦发银行 1月数据: {len(df)}条")
    if df:
        print(f"  第一条: {df[0]}")
        print(f"  最后一条: {df[-1]}")

    # 再查一次（应该命中缓存）
    print("\n=== 第二次查询（应该命中缓存）===")
    df2 = get_kline("sh.600000", "2024-01-01", "2024-01-31")
    print(f"浦发银行 1月数据: {len(df2)}条")

    # 统计
    print("\n=== 缓存统计 ===")
    stats = get_cache_stats()
    for k, v in stats.items():
        print(f"  {k}: {v}")
