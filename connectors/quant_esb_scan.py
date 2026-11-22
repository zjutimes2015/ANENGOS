"""全 A 离线二四棒扫描器（交易系统·二四棒 唯一选股入口）。

定位（用户拍板 2026-11-22）：全面收敛到二四棒单一交易系统，**不叠加**低波、
质量价值、RSI、MA20 等任何附加因子。本扫描器只做一件事——从本地增量行情库
遍历沪深 A 股（不含北交所），用峰级二四棒规则筛出两类信号：

  - ``breakout``（突破）：当日最高价 ≥ 最近已确认左峰 → 当日可开仓；
  - ``pending``（挂单）：尚未突破，但收盘价距左峰 ≤ ``proximity_pct``%
    → 次日在左峰价挂限价买单，触及才成交。

信号判定、峰级流式确认、风险定额定仓全部复用 ``connectors.quant_ersibang``，
保证扫描口径与回测/虚拟盘一致，不另造一套规则。

产物（workspace 下）：
  - ``esb_scan_output/esb_scan_<date>.json``：结构化信号列表，供 paper_trader 读取；
  - ``esb_scan_output/esb_scan_<date>.md``：人工查看表格。

cron：交易日闭市、行情落库后运行；产物供次日虚拟盘挂单/开仓读取。
"""

from __future__ import annotations

import datetime
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from connectors.base import AgentAdapter, AgentResult, _now_iso
from connectors.quant_ersibang import esb_pending_signal, esb_signal, size_shares

LOCAL_DB = "/srv/anengos/data/a_stock_daily.db"
LOOKBACK = 120            # 每只取最近 N 根日线（峰级 n=3 + 趋势判断 + 移动止损窗）
MIN_BARS = 40             # 不足根数不参与信号判定
PRICE_FLOOR = 2.0
PRICE_CEIL = 200.0
RISK_PCT = 0.01           # 单笔风险 1%（计划股数参考；成交时按当前权益重算）
PROXIMITY_PCT = 3.0       # 收盘价距左峰 ≤3% 才生成挂单
REFERENCE_CAPITAL = 1_000_000.0
CF_LOOKBACK_YEARS = 3     # 连续三个完整年报经营现金流净额为负 → 剔出入池
CF_FETCH_SLEEP = 0.15     # 在线补拉现金流限流（秒/只）

try:
    import akshare as ak  # type: ignore
    _AK_AVAILABLE = True
except Exception:  # noqa: BLE001
    ak = None  # type: ignore
    _AK_AVAILABLE = False


def _strip_code(full: str) -> str:
    """本地库 code 形如 ``sh.600000``/``sz.000001``，取裸 6 位；北交所/非法返回空串。"""
    full = str(full or "")
    if full.startswith("bj.") or full.startswith("bj"):
        return ""
    bare = full.split(".")[-1]
    return bare if re.fullmatch(r"\d{6}", bare) else ""


def load_universe(db_path: str, lookback: int = LOOKBACK) -> list[dict]:
    """从本地行情库逐只读最近 ``lookback`` 根日线，构造离线序列。

    返回 ``[{code, dates, opens, highs, lows, closes, volumes, close, last_date}]``；
    缺行情、根数不足、价格越界、当日停牌（成交量 0）的票跳过。
    """
    db = Path(db_path)
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    try:
        codes = [r[0] for r in conn.execute("SELECT DISTINCT code FROM daily")]
        universe: list[dict] = []
        for full in codes:
            code = _strip_code(full)
            if not code:
                continue
            rows = conn.execute(
                "SELECT date, open, high, low, close, volume "
                "FROM daily WHERE code = ? ORDER BY date DESC LIMIT ?",
                (full, lookback),
            ).fetchall()
            if not rows:
                continue
            rows.reverse()
            clean = [(str(r[0])[:10], float(r[1] or 0), float(r[2] or 0),
                      float(r[3] or 0), float(r[4] or 0), float(r[5] or 0))
                     for r in rows if r[4] is not None]
            if len(clean) < MIN_BARS:
                continue
            close = clean[-1][4]
            if close < PRICE_FLOOR or close > PRICE_CEIL:
                continue
            if clean[-1][5] <= 0:  # 当日停牌（无量）→ 不参与
                continue
            universe.append({
                "code": code,
                "dates": [x[0] for x in clean],
                "opens": [x[1] for x in clean],
                "highs": [x[2] for x in clean],
                "lows": [x[3] for x in clean],
                "closes": [x[4] for x in clean],
                "volumes": [x[5] for x in clean],
                "close": close,
                "last_date": clean[-1][0],
            })
        return universe
    finally:
        conn.close()


def scan_one(stock: dict, capital: float = REFERENCE_CAPITAL,
             risk_pct: float = RISK_PCT,
             proximity_pct: float = PROXIMITY_PCT) -> dict | None:
    """对单只离线序列判定二四棒信号；返回 None（无信号）或信号记录。

    优先突破信号；未突破再判接近突破挂单信号。记录里 ``type`` 为
    ``breakout`` / ``pending``，``shares`` 为按参考资金估算的计划股数。
    """
    dates = stock["dates"]
    highs = stock["highs"]
    lows = stock["lows"]
    closes = stock["closes"]

    sig = esb_signal(dates, stock["opens"], highs, lows, closes,
                     capital=capital, risk_pct=risk_pct)
    if sig is not None:
        return {
            "code": stock["code"], "name": stock.get("name", ""),
            "type": "breakout",
            "entry_px": round(sig["entry_px"], 4),
            "stop_px": round(sig["stop_px"], 4),
            "shares": sig["shares"],
            "peak_idx": sig["peak_idx"], "level_idx": sig["level_idx"],
            "gap_pct": 0.0, "close_px": round(closes[-1], 4),
            "last_date": stock["last_date"],
        }

    pend = esb_pending_signal(dates, stock["opens"], highs, lows, closes,
                              capital=capital, risk_pct=risk_pct,
                              proximity_pct=proximity_pct)
    if pend is not None:
        return {
            "code": stock["code"], "name": stock.get("name", ""),
            "type": "pending",
            "entry_px": round(pend["entry_px"], 4),
            "stop_px": round(pend["stop_px"], 4),
            "shares": pend["shares"],
            "peak_idx": pend["peak_idx"], "level_idx": pend["level_idx"],
            "gap_pct": round(pend["gap_pct"], 2),
            "close_px": round(pend["close_px"], 4),
            "last_date": stock["last_date"],
        }
    return None


def scan_universe(universe: list[dict], capital: float = REFERENCE_CAPITAL,
                  risk_pct: float = RISK_PCT,
                  proximity_pct: float = PROXIMITY_PCT) -> list[dict]:
    """遍历离线序列跑二四棒信号；突破优先，挂单按距突破距离升序。纯函数，可测试。"""
    results: list[dict] = []
    for stock in universe:
        try:
            sig = scan_one(stock, capital, risk_pct, proximity_pct)
        except Exception:  # noqa: BLE001 —— 单只异常不阻断全市场扫描
            continue
        if sig is not None:
            results.append(sig)
    results.sort(key=lambda s: (0 if s["type"] == "breakout" else 1, s["gap_pct"]))
    return results


def _online_name_map() -> dict[str, str]:
    """在线快照补名称并识别 ST；返回 {裸6位: 名称}。在线不可用时返回空 dict。"""
    names: dict[str, str] = {}
    if not _AK_AVAILABLE:
        return names
    try:
        spot = ak.stock_zh_a_spot()
        for _, row in spot.iterrows():
            code_raw = str(row.get("代码", ""))
            bare = re.fullmatch(r"(\d{6})", code_raw[-6:])
            if bare:
                names[bare.group(1)] = str(row.get("名称", ""))
    except Exception:  # noqa: BLE001
        return {}
    return names


def _is_st(name: str) -> bool:
    n = (name or "").upper()
    return ("ST" in n) or ("退" in (name or ""))


# ---- 现金流排雷：连续三年年报经营现金流净额为负 → 剔除 ----

def _annual_years(ref_date: str) -> list[int]:
    """参考行情日所在年往前推三个完整年度（当年年报尚未披露）。"""
    y = int(str(ref_date)[:4]) if ref_date else datetime.date.today().year
    return [y - 1, y - 2, y - 3]


def _ensure_cf_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS cashflow_annual(
            code TEXT, year INTEGER, netcash REAL,
            fetched_at TEXT, PRIMARY KEY(code, year))""")
    conn.commit()


def _read_cf_cache(conn: sqlite3.Connection, code: str) -> dict[int, float | None]:
    rows = conn.execute(
        "SELECT year, netcash FROM cashflow_annual WHERE code = ?", (code,)).fetchall()
    return {int(y): (None if v is None else float(v)) for y, v in rows}


def _write_cf_cache(conn: sqlite3.Connection, code: str,
                    data: dict[int, float | None]) -> None:
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for y, v in data.items():
        conn.execute(
            "INSERT OR REPLACE INTO cashflow_annual(code, year, netcash, fetched_at)"
            " VALUES (?,?,?,?)", (code, int(y), v, now))
    conn.commit()


def _em_symbol(code: str) -> str:
    return f"SH{code}" if code.startswith("6") else f"SZ{code}"


def _fetch_cashflow_em(code: str) -> dict[int, float | None]:
    """东财现金流量表：取历年年报(12-31)经营活动现金流量净额。失败/无数据返回 {}。"""
    if not _AK_AVAILABLE:
        return {}
    try:
        df = ak.stock_cash_flow_sheet_by_report_em(symbol=_em_symbol(code))
        out: dict[int, float | None] = {}
        for _, row in df.iterrows():
            d = str(row.get("REPORT_DATE", ""))[:10]
            if not d.endswith("12-31"):
                continue
            try:
                val = float(row.get("NETCASH_OPERATE"))
            except (TypeError, ValueError):
                val = None
            out[int(d[:4])] = val
        return out
    except Exception:  # noqa: BLE001 —— 单只取数失败不阻断扫描，保留待人工复核
        return {}


def cf_three_year_negative(code: str, ref_date: str,
                           cache: dict[int, float | None],
                           online_fetch) -> tuple[bool, str]:
    """近三个完整年报经营现金流均 <0 → (True, 原因)；数据不足或任一为正 → (False, 原因)。

    cache: 本地缓存 {year: netcash}；online_fetch(code)->{year: netcash} 在线补拉回调。
    缺数据一律保留（不误杀），仅三年齐全且全部为负才剔除。
    """
    years = _annual_years(ref_date)
    data = dict(cache)
    if any(y not in data for y in years):
        data.update(online_fetch(code))
    vals = {y: data.get(y) for y in years}
    missing = [y for y, v in vals.items() if v is None]
    if missing:
        return False, "现金流数据缺失,保留"
    if all(v < 0 for v in vals.values()):
        return True, f"近{len(years)}年经营现金流均为负"
    return False, ""


def _apply_cashflow_filter(results: list[dict], last_date: str,
                            db_path: str = LOCAL_DB, fetch=None,
                            sleep: float = CF_FETCH_SLEEP) -> tuple[list[dict], list[dict]]:
    """对二四棒信号做现金流排雷；返回 (保留信号, 被剔除信号)。缓存命中不在线拉取。"""
    fetch = fetch or _fetch_cashflow_em
    kept: list[dict] = []
    excluded: list[dict] = []
    conn = None
    try:
        conn = sqlite3.connect(db_path)
        _ensure_cf_table(conn)
        for sig in results:
            code = sig["code"]
            cache = _read_cf_cache(conn, code)
            years = _annual_years(last_date)
            if any(y not in cache for y in years):
                fetched = fetch(code)
                if fetched:
                    _write_cf_cache(conn, code, fetched)
                    cache.update(fetched)
                if sleep:
                    time.sleep(sleep)
            neg, reason = cf_three_year_negative(code, last_date, cache, fetch)
            if neg:
                excluded.append({**sig, "exclude_reason": reason})
            else:
                kept.append(sig)
    finally:
        if conn is not None:
            conn.close()
    return kept, excluded


class QuantEsbScanAdapter(AgentAdapter):
    name = "quant_esb_scan"

    def __init__(self) -> None:
        self.mode = "local" if Path(LOCAL_DB).exists() else "mock"

    def submit(self, task: str, workspace: str) -> AgentResult:
        out_dir = Path(workspace) / "esb_scan_output"
        out_dir.mkdir(parents=True, exist_ok=True)

        universe = load_universe(LOCAL_DB)
        errors = 0
        st_excluded: list[str] = []
        if universe:
            names = _online_name_map()
            for c in universe:
                c["name"] = names.get(c["code"], c["code"])
            # 在线名称可用时剔除 ST/退市；名称缺失（在线不可用）保留，交由纸盘/人工复核
            if names:
                kept_u, st_u = [], []
                for c in universe:
                    (st_u if _is_st(c["name"]) else kept_u).append(c)
                universe = kept_u
                st_excluded = [x["code"] for x in st_u]
            results = scan_universe(universe)
            scanned = len(universe)
            last_date = max((c["last_date"] for c in universe), default="")
        else:
            results, scanned, last_date = [], 0, ""

        # 现金流排雷：连续三年年报经营现金流为负的信号剔出入池（数据缺失保留）
        results, cf_excluded = _apply_cashflow_filter(results, last_date) if results else ([], [])

        report_day = last_date or datetime.datetime.now().strftime("%Y%m%d")
        report_day_compact = report_day.replace("-", "")

        breakout = [r for r in results if r["type"] == "breakout"]
        pending = [r for r in results if r["type"] == "pending"]

        # 结构化产物（供 paper_trader / 飞书写表读取）
        json_path = out_dir / f"esb_scan_{report_day_compact}.json"
        json_path.write_text(
            __import__("json").dumps(
                {"last_date": report_day, "scanned": scanned,
                 "breakout": breakout, "pending": pending, "signals": results,
                 "excluded_st": st_excluded, "excluded_cf": cf_excluded},
                ensure_ascii=False, indent=2),
            encoding="utf-8")

        lines = [
            f"# 全 A 二四棒扫描（{scanned} 只扫描 → {len(results)} 只信号）",
            f"- 扫描时间：{_now_iso()}",
            f"- 行情日：{report_day}",
            "- 规则：峰级二四棒（左3右3）；级上移 + 左级Low<左峰High；突破左峰入场，左级为止损，近4根最低移动止损只上移",
            "- 不含任何附加因子（低波/质量价值/RSI/MA20 均不参与估值打分）",
            f"- 排雷剔除：ST/退市 {len(st_excluded)} 只 | 连续三年经营现金流为负 {len(cf_excluded)} 只",
            f"- 突破信号 {len(breakout)} 只 | 接近突破挂单 {len(pending)} 只",
            "",
            "| 类型 | 代码 | 名称 | 收盘价 | 入场价(左峰) | 止损价(左级) | 距突破% | 计划股数 |",
            "|---|---|---|---|---|---|---|---|",
        ]
        if cf_excluded:
            lines.append("")
            lines.append("## 现金流排雷剔除（近三年年报经营现金流均为负）")
            for e in cf_excluded:
                lines.append(f"- {e['code']} {e.get('name','')}：{e['exclude_reason']}")
        for r in results:
            lines.append(
                f"| {'突破' if r['type'] == 'breakout' else '挂单'} | {r['code']} | {r['name']} "
                f"| {r['close_px']:.2f} | {r['entry_px']:.2f} | {r['stop_px']:.2f} "
                f"| {r['gap_pct']:.2f}% | {r['shares']} |")
        md_path = out_dir / f"esb_scan_{report_day_compact}.md"
        md_path.write_text("\n".join(lines), encoding="utf-8")

        return AgentResult(
            True, self.name, "done",
            f"[{self.name}/{self.mode}] 全A {scanned}只 → 突破{len(breakout)} "
            f"挂单{len(pending)}",
            artifact_paths=[str(json_path.relative_to(workspace)),
                            str(md_path.relative_to(workspace))],
            meta={"source": self.mode, "scanned": scanned,
                  "breakout": len(breakout), "pending": len(pending),
                  "excluded_st": len(st_excluded),
                  "excluded_cf": len(cf_excluded),
                  "last_date": report_day, "errors": errors})

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "ready": Path(LOCAL_DB).exists(),
                "mode": self.mode, "local_db": LOCAL_DB}

    def describe(self) -> dict[str, Any]:
        return {"summary": "全A离线二四棒扫描：峰级突破/接近突破挂单信号，唯一选股入口，无附加因子",
                "params": {"task": {"type": "string", "description": "留空跑全A扫描"}}}
