# -*- coding: utf-8 -*-
"""
update_incremental.py — 全 A 日线增量更新（Tushare 备用源）
=====================================================================
背景：baostock 服务器 IP 被列入黑名单（error 10001011），本脚本改用
Tushare Pro 按交易日批量回补/刷新沪深日线，作为每日增量更新机制。

口径对齐现有 daily 表（code/date/open/high/low/close/volume/amount/turn）：
  - Tushare vol 单位「手」 -> 入库 volume 单位「股」：vol * 100
  - Tushare amount 单位「千元」 -> 入库 amount 单位「元」：amount * 1000
  - turnover_rate(%) 直接写入 turn 列
  - 日期 YYYYMMDD -> YYYY-MM-DD
  - ts_code 600000.SH -> sh.600000；000001.SZ -> sz.000001
  - 仅入库 SH/SZ；BJ（北交所）按现有口径跳过

复权说明：Tushare daily 为不复权行情；历史库由 baostock 前复权构建。
近期无除权个股两者一致；除权日附近可能存在口径跳空，属备用源固有局限。

并发与锁：SQLite WAL 单写者模型，入库与其它偶发写路径（扫描器现金流缓存、
在线请求触发的 connector）可能短时撞锁。本脚本通过以下手段把写库做扎实：
  - 显式 busy_timeout，让 SQLite 在内置忙等窗口内排队，而不是立刻 locked；
  - 单日数据按小批量分批提交，缩短单次写事务持锁时间；
  - 遇到 database is locked / busy 按指数退避重试若干次；
  - 连接在 finally 中释放，避免异常路径泄漏连接残留读锁。

用法：
  python update_incremental.py                     # 自动回补库内最新日期之后的交易日
  python update_incremental.py --start 2026-11-09 --end 2026-11-12
  python update_incremental.py --dry-run           # 只统计计划，不写库
"""
import os
import sys
import time
import fcntl
import sqlite3
import argparse
from datetime import datetime, timedelta

sys.path.insert(0, "/srv/anengos")
from connectors.tushare_data import get_pro

DB_PATH = "/srv/anengos/data/a_stock_daily.db"
LOCK_PATH = "/srv/anengos/data/update_incremental.lock"
FIELDS = "ts_code,trade_date,open,high,low,close,vol,amount"

# 写库韧性参数
BATCH_SIZE = 500          # 单批提交的行数，缩短写事务持锁窗口
MAX_RETRIES = 3           # 单日写入撞锁后的重试次数
RETRY_BACKOFF = (2, 5, 10)  # 退避秒数，依次取用，最后一次用完仍失败则抛出
BUSY_TIMEOUT_MS = 60000   # 内置忙等窗口


def log(msg):
    print(f"[{datetime.now().strftime('%F %T')}] {msg}", flush=True)


def ts_to_bs(ts_code: str):
    code, exch = ts_code.split(".")
    if exch == "SH":
        return "sh." + code
    if exch == "SZ":
        return "sz." + code
    return None  # BJ 等跳过


def fnum(x):
    try:
        return float(x) if x not in (None, "") else None
    except (ValueError, TypeError):
        return None


def daterange(start: str, end: str):
    s = datetime.strptime(start, "%Y-%m-%d")
    e = datetime.strptime(end, "%Y-%m-%d")
    cur = s
    while cur <= e:
        yield cur.strftime("%Y-%m-%d")
        cur += timedelta(days=1)


def _is_lock_error(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=BUSY_TIMEOUT_MS / 1000)
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    # WAL 下 NORMAL 已能保证已提交事务不被回滚（除断电外），且减少每次 commit
    # 的 fsync 次数，缩短写事务持锁窗口；实盘迁移前可按需调回 FULL。
    cur.execute("PRAGMA synchronous=NORMAL")
    return conn


def _write_day(conn, day: str, recs: list[tuple], last_by_code: dict) -> None:
    """把某一交易日的日线分批写入 daily，并回写 downloaded 表。"""
    cur = conn.cursor()
    for i in range(0, len(recs), BATCH_SIZE):
        batch = recs[i:i + BATCH_SIZE]
        cur.executemany(
            """INSERT OR REPLACE INTO daily
               (code,date,open,high,low,close,volume,amount,turn)
               VALUES (?,?,?,?,?,?,?,?,?)""", batch)
        conn.commit()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for bs, d in last_by_code.items():
        cur.execute(
            """INSERT OR REPLACE INTO downloaded (code,last_date,download_time)
               VALUES (?,?,?)""", (bs, d, now))
    conn.commit()


def _write_day_with_retry(conn, day: str, recs: list[tuple],
                          last_by_code: dict) -> None:
    """单日写入撞锁时退避重试；耗尽重试次数后向上抛出。"""
    attempt = 0
    while True:
        try:
            _write_day(conn, day, recs, last_by_code)
            return
        except sqlite3.OperationalError as e:
            if not _is_lock_error(e) or attempt >= MAX_RETRIES:
                raise
            wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
            log(f"{day} 写库撞锁({repr(e)[:80]})，{wait}s 后重试 "
                f"[{attempt + 1}/{MAX_RETRIES}]")
            attempt += 1
            # 回滚当前未提交事务，释放已持的读/写意向锁后再退避
            if conn is not None:
                try:
                    conn.rollback()
                except sqlite3.OperationalError:
                    pass
            time.sleep(wait)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    lock_fh = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("另一个增量更新实例在运行，退出")
        sys.exit(0)

    conn = None
    try:
        conn = _connect()
        cur = conn.cursor()

        # 确定回补区间
        row = cur.execute("SELECT MAX(date) FROM daily").fetchone()
        latest = row[0] or "2026-11-06"
        end = args.end or datetime.now().strftime("%Y-%m-%d")
        start = args.start or (datetime.strptime(latest, "%Y-%m-%d") - timedelta(days=3)).strftime("%Y-%m-%d")
        log(f"回补区间 {start} ~ {end}（库内最新 {latest}）dry_run={args.dry_run}")

        pro = get_pro()
        total_rows = 0
        total_bj = 0
        per_day = {}

        for day in daterange(start, end):
            ymd = day.replace("-", "")
            try:
                df = pro.daily(trade_date=ymd, fields=FIELDS)
            except Exception as e:
                log(f"{day} daily 拉取失败: {repr(e)[:120]}")
                continue
            if df is None or len(df) == 0:
                log(f"{day} 非交易日或暂无行情，跳过")
                continue

            # 换手率面板（同日全市场）
            turn_map = {}
            try:
                db = pro.daily_basic(trade_date=ymd, fields="ts_code,turnover_rate")
                if db is not None and len(db):
                    turn_map = dict(zip(db.ts_code, db.turnover_rate))
            except Exception as e:
                log(f"{day} daily_basic 换手率拉取失败: {repr(e)[:120]}")

            recs = []
            bj = 0
            last_by_code = {}
            for r in df.itertuples(index=False):
                bs = ts_to_bs(r.ts_code)
                if bs is None:
                    bj += 1
                    continue
                vol = fnum(r.vol)
                amount = fnum(r.amount)
                recs.append((
                    bs, day,
                    fnum(r.open), fnum(r.high), fnum(r.low), fnum(r.close),
                    vol * 100 if vol is not None else None,
                    amount * 1000 if amount is not None else None,
                    fnum(turn_map.get(r.ts_code)),
                ))
                last_by_code[bs] = day

            per_day[day] = len(recs)
            total_rows += len(recs)
            total_bj += bj
            log(f"{day}: 沪深 {len(recs)} 只，跳过 BJ {bj} 只")

            if args.dry_run:
                continue

            _write_day_with_retry(conn, day, recs, last_by_code)
            time.sleep(0.4)  # 轻微限速，避免触发 Tushare 频率限制

        if not args.dry_run:
            n_stock = cur.execute("SELECT COUNT(DISTINCT code) FROM daily").fetchone()[0]
            n_row = cur.execute("SELECT COUNT(*) FROM daily").fetchone()[0]
            mn, mx = cur.execute("SELECT MIN(date),MAX(date) FROM daily").fetchone()
            log(f"=== 更新完成：{n_stock} 只 / {n_row} 行 / {mn}~{mx} ===")
            log(f"本次写入 {total_rows} 行，跳过 BJ {total_bj} 只；分日 {per_day}")
        else:
            log(f"[dry-run] 计划写入 {total_rows} 行，跳过 BJ {total_bj} 只；分日 {per_day}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.OperationalError:
                pass


if __name__ == "__main__":
    main()
