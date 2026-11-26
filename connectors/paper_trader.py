"""虚拟盘交易 agent（交易系统·二四棒 单一策略）。

买入（两类，均来自 quant_esb_scan 全 A 二四棒扫描产物，不叠加其他因子）：
  1. breakout 突破信号：当日最高价已破最近左峰 → 按左峰价风险定额开仓；
  2. pending 挂单信号：尚未突破但收盘距左峰 ≤ proximity% → 次日左峰价限价挂单，触及成交。
卖出（统一二四棒规则）：
  初始止损 = 左级 Low；移动止损 = 近 4 根最低·只上移（仅今日 High > 昨日 High 更新）；
  当日 Low < 有效止损 → 全部离场。无机械止盈、无 RSI 减仓、无 MA20 破位。
历史遗留普通仓（strategy != ersibang）按兜底规则清出，不再新增普通仓。

cron：每个交易日闭市后运行。
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

# 历史遗留普通仓兜底止损：买入价浮亏达该比例离场（仅过渡期清出旧仓，新仓全部走二四棒）。
LEGACY_STOP_PCT = 0.08

# 【提前挂单模式】收盘后扫描接近突破左峰的股票，次日在左峰价挂限价买单；
# 挂单有效期1天，未成交自动作废。接近突破的阈值在扫描器 quant_esb_scan 中配置。
PENDING_ORDER_FILE = "paper_pending_orders.json"

# 飞书每日复盘表同步（次日挂单列表写入）
# 凭据优先级：环境变量 ANENGOS_FEISHU_* > workspace/.secrets/feishu_app.json（与 push_daily_review.py 同一份）
FEISHU_APP_ID = os.environ.get("ANENGOS_FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("ANENGOS_FEISHU_APP_SECRET", "")
FEISHU_BASE_TOKEN = os.environ.get("ANENGOS_FEISHU_BASE", "W3cob77DEanyS8sibGCcg07SnWd")
FEISHU_TABLE_ID = os.environ.get("ANENGOS_FEISHU_TABLE", "tblPShDuSvXeNuVZ")

if not (FEISHU_APP_ID and FEISHU_APP_SECRET):
    try:
        _sec_path = Path(__file__).resolve().parent.parent / "workspace" / ".secrets" / "feishu_app.json"
        if _sec_path.exists():
            _sec = json.loads(_sec_path.read_text(encoding="utf-8"))
            FEISHU_APP_ID = FEISHU_APP_ID or _sec.get("app_id", "")
            FEISHU_APP_SECRET = FEISHU_APP_SECRET or _sec.get("app_secret", "")
    except Exception:  # noqa: BLE001
        pass

from connectors.base import AgentAdapter, AgentResult, _now_iso
from connectors.error_log import error_guard
from connectors.quant_ersibang import size_shares

try:
    import akshare as ak  # type: ignore
    _AK_AVAILABLE = True
except Exception:  # noqa: BLE001
    ak = None  # type: ignore
    _AK_AVAILABLE = False


def _sina_code(code: str) -> str:
    return f"sh{code}" if code.startswith("6") else f"sz{code}"


# ---- 交易日历（模块级缓存，避免每次运行重复拉取）----
_TRADE_DATES_CACHE: set[str] | None = None


def _trade_calendar() -> set[str]:
    """全局交易日集合；日历源不可用时返回空集合（调用方回退自然日规则）。"""
    global _TRADE_DATES_CACHE
    if _TRADE_DATES_CACHE is not None:
        return _TRADE_DATES_CACHE
    dates: set[str] = set()
    if _AK_AVAILABLE:
        try:
            tdf = ak.tool_trade_date_hist_sina()
            dates = {str(d)[:10] for d in tdf["trade_date"]}
        except Exception as e:  # noqa: BLE001
            print(f"[PAPER] 交易日历拉取失败，回退自然日规则: {e}")
    _TRADE_DATES_CACHE = dates
    return dates


def _next_trade_day(date_str: str) -> str:
    """返回 date_str 之后的第一个交易日；日历不可用时顺延并跳过周末。"""
    cal = _trade_calendar()
    try:
        base = datetime.date.fromisoformat(str(date_str)[:10])
    except Exception:  # noqa: BLE001
        return date_str
    if cal:
        future = sorted(d for d in cal if d > base.isoformat())
        if future:
            return future[0]
    # 回退：自然日顺延，跳过周六(5)/周日(6)
    d = base + datetime.timedelta(days=1)
    while d.weekday() >= 5:
        d += datetime.timedelta(days=1)
    return d.isoformat()


# ---- 账户状态持久化：原子写 + 损坏备份，不静默丢仓 ----
def _atomic_write_json(path: Path, obj: Any) -> None:
    """先写临时文件再 os.replace 原子替换；写前把旧文件留一份 .bak。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        except Exception as e:  # noqa: BLE001
            print(f"[PAPER] 备份 {path.name} 失败: {e}")
    tmp = path.with_suffix(path.suffix + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _load_account(path: Path) -> tuple[dict, list[str]]:
    """读取账户文件；解析失败时把损坏样本另存为 .corrupt-<ts>，返回默认空结构并带告警。

    不直接覆盖损坏原文件，避免持仓/历史被静默清空。
    """
    warnings: list[str] = []
    if not path.exists():
        return {"positions": [], "history": []}, warnings
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, list):  # 兼容旧格式（只有 positions 列表）
            return {"positions": loaded, "history": []}, warnings
        if not isinstance(loaded, dict):
            raise ValueError("账户文件结构非对象")
        return loaded, warnings
    except Exception as e:  # noqa: BLE001
        ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        corrupt = path.with_suffix(path.suffix + f".corrupt-{ts}")
        try:
            shutil.copy2(path, corrupt)
        except Exception as ce:  # noqa: BLE001
            corrupt = None
            print(f"[PAPER] 损坏样本备份失败: {ce}")
        warnings.append(
            f"账户文件 {path.name} 解析失败({e})；已另存 {corrupt.name if corrupt else '备份失败'}，"
            f"本轮按空账户临时加载，未覆盖原文件，请尽快人工核对。"
        )
        return {"positions": [], "history": []}, warnings


# ---- 真实化：A股散户交易成本 / 滑点 / T+1（2026现行费率口径）----
COMMISSION_RATE = 0.00025   # 佣金万2.5，买卖双边
MIN_COMMISSION = 5.0        # 单笔最低佣金5元
STAMP_TAX_RATE = 0.0005      # 印花税，卖出单边0.05%，买入不收
TRANSFER_RATE = 0.00001      # 过户费0.001%，买卖双边
SLIPPAGE = 0.001             # 滑点0.1%：买入上浮、卖出下浮，模拟冲击成本


def _buy_exec(price: float, shares: int) -> dict:
    """买入成交：滑点上浮，计佣金(最低5元)+过户费，返回含费成本。"""
    fill = price * (1 + SLIPPAGE)
    gross = fill * shares
    commission = max(gross * COMMISSION_RATE, MIN_COMMISSION)
    transfer = gross * TRANSFER_RATE
    outlay = gross + commission + transfer
    return {"fill": fill, "commission": commission, "transfer": transfer,
            "outlay": outlay, "cost_per_share": outlay / shares}


def _sell_exec(price: float, shares: int) -> dict:
    """卖出成交：滑点下浮，计佣金(最低5元)+印花税+过户费，返回净回笼。"""
    fill = price * (1 - SLIPPAGE)
    gross = fill * shares
    commission = max(gross * COMMISSION_RATE, MIN_COMMISSION)
    stamp = gross * STAMP_TAX_RATE
    transfer = gross * TRANSFER_RATE
    net = gross - commission - stamp - transfer
    return {"fill": fill, "commission": commission, "stamp": stamp,
            "transfer": transfer, "net": net}


# ---- 组合级风控（P7）：单票上限 / 总仓位 / 回撤熔断 ----
SINGLE_POSITION_CAP = 0.20    # 单票市值 ≤ 总资产20%
# 总仓位绝对额上限：随账户权益按比例持续扩容，100 万本金半仓 50 万起步，最终封顶 100 万。
POSITION_CAP_RATIO = 0.50     # 上限 = 当前权益 × 50%
POSITION_CAP_FLOOR = 500_000.0  # 下限（起步半仓），净值回落时不低于此
POSITION_CAP_MAX = 1_000_000.0  # 最终上限（满仓），需权益达 200 万才打满
MAX_POSITION_VALUE = POSITION_CAP_FLOOR  # 兼容旧引用
MAX_GROSS_EXPOSURE = 0.90     # 比例上限（保留兼容，当前以绝对额闸门为准）
# 换仓：被总仓位上限拦住时，卖掉一只结构最弱的持仓腾额度，避免满仓后策略彻底停摆
ROTATE_MAX_PER_DAY = 2        # 每日换仓上限（笔），防止来回打脸
ROTATE_MIN_RISK_TIGHTEN = 0.0  # 换入信号止损距离必须严格小于换出标的
CIRCUIT_BREAKER_DD = 0.15     # 净值自峰值回撤 ≥15% → 熔断，停止一切新开仓（只卖不买）
# ---- P0 风控封顶：单笔止损风险金额 + 窄止损距离保护 ----
RISK_PER_TRADE_PCT = float(os.environ.get("ANENGOS_RISK_PER_TRADE", "0.015"))  # 单笔止损亏损 ≤ 权益1.5%
MIN_STOP_DIST_PCT = 0.02      # 止损距离 < 入场价2% 按最小距离折算，窄距离不放大仓位


def _position_cap(equity: float) -> float:
    """总仓位绝对额上限：权益×50%，下限 50 万、上限 100 万（随净值持续扩容）。"""
    return min(POSITION_CAP_MAX, max(POSITION_CAP_FLOOR, equity * POSITION_CAP_RATIO))


def _risk_check(code: str, add_outlay: float, add_mkt_value: float,
                positions: list[dict], cur_equity: float, cash: float,
                halt_new_buy: bool, risk_amt: float | None = None,
                cap: float | None = None) -> tuple[bool, str]:
    """买入前组合风控闸门：熔断 → 现金 → 单笔风险金额 → 总仓位 → 单票上限。返回(放行, 拦截原因)。"""
    if halt_new_buy:
        return False, "熔断中(峰值回撤≥15%)停止新开仓"
    if add_outlay > cash:
        return False, "现金不足"
    if risk_amt is not None and risk_amt > cur_equity * RISK_PER_TRADE_PCT:
        return False, (f"单笔止损风险超上限{RISK_PER_TRADE_PCT*100:.1f}%"
                        f"(潜在亏损{risk_amt:.0f}>预算{cur_equity*RISK_PER_TRADE_PCT:.0f})")
    cur_mkt = sum(p["current_price"] * p["shares"] for p in positions)
    cap_val = _position_cap(cur_equity) if cap is None else cap
    # 总仓位：买入后持仓市值不得超过动态绝对额上限（权益×50%，50 万起、100 万封顶）
    if cur_mkt + add_mkt_value > cap_val:
        return False, (f"超总仓位上限{cap_val/10000:.0f}万"
                       f"(现有{cur_mkt:.0f}+新买{add_mkt_value:.0f})")
    # 单票上限：该票（含已有）买入后市值/总资产 不得超过上限
    own = sum(p["current_price"] * p["shares"] for p in positions if p["code"] == code)
    if (own + add_mkt_value) / cur_equity > SINGLE_POSITION_CAP:
        return False, f"超单票上限{SINGLE_POSITION_CAP*100:.0f}%"
    return True, ""


def _rotation_target(positions: list[dict], mkt: str, new_risk_dist: float,
                     need_value: float) -> dict | None:
    """换出候选：距止损最近的持仓（结构最弱），要求换入信号止损更紧、单只能腾够额度。

    ``new_risk_dist`` 为新信号止损距离 (entry-stop)/entry；换出标的按其现价计算
    (current-stop)/current。只有换出标的保护幅度更宽（即更弱）时才允许换掉。
    """
    best = None
    best_key = None
    for p in positions:
        if str(p.get("buy_date") or "") >= mkt:
            continue  # T+1：当日买入不可卖
        cur = float(p.get("current_price") or 0)
        stop = float(p.get("stop") or p.get("init_stop") or 0)
        if cur <= 0 or stop <= 0:
            continue
        dist = (cur - stop) / cur
        if dist <= new_risk_dist + ROTATE_MIN_RISK_TIGHTEN:
            continue  # 换出标的的保护幅度不比新信号宽，换了不划算
        if cur * p["shares"] < need_value:
            continue  # 单只腾不出足够额度，不连环砍仓
        key = (round(dist, 6), float(p.get("pnl_pct") or 0))
        if best_key is None or key < best_key:
            best, best_key = p, key
    return best


def _rotation_sell(tgt: dict, last: float, last_date: str, mkt: str,
                   reason: str) -> dict:
    """把换出标的按当前价全卖，返回卖出快照与净回笼（不修改入参）。"""
    ex = _sell_exec(last, tgt["shares"])
    cost_basis = tgt["buy_price"] * tgt["shares"]
    pnl = ex["net"] - cost_basis
    fee = ex["commission"] + ex["stamp"] + ex["transfer"]
    bp = float(tgt["buy_price"]) or 0.0
    sale = {**tgt,
            "sell_date": _sell_date(last_date, tgt.get("buy_date", ""), mkt),
            "sell_quote_date": last_date,
            "sell_price": ex["fill"],
            "sell_reason": reason,
            "pnl": pnl,
            "pnl_pct": (last - bp) / bp * 100 if bp else 0.0,
            "sell_fee": round(fee, 2),
            "partial": False}
    return {"sale": sale, "net": ex["net"], "fee": fee, "pnl": pnl}


def _gate_buy(code: str, name: str, shares: int, price: float,
              entry_px: float, stop_px: float, *, positions: list[dict],
              cash: float, cur_equity: float, halt_new_buy: bool,
              risk_amt: float | None, mkt: str, last_date: str,
              rotate_left: int) -> dict:
    """买入闸门 + 被总仓位上限拦住时执行一次换仓。

    返回 ``{"ok","bx","cash","equity","sale","rotate_msg","why"}``；``sale`` 非空表示
    本次发生了换仓卖出（调用方负责写 history、累计费用与卖出盈亏）。
    """
    bx = _buy_exec(price, shares)
    add_mkt = bx["fill"] * shares
    ok, why = _risk_check(code, bx["outlay"], add_mkt, positions, cur_equity,
                          cash, halt_new_buy, risk_amt=risk_amt)
    result = {"ok": ok, "bx": bx, "cash": cash, "equity": cur_equity,
              "sale": None, "rotate_msg": "", "why": why}
    if ok:
        result["cash"] = cash - bx["outlay"]
        result["equity"] = result["cash"] + sum(
            p["current_price"] * p["shares"] for p in positions) + add_mkt
        return result
    # 只有「总仓位上限」一种拦截允许换仓，其余（熔断/现金/单笔风险/单票上限）不换
    if not why.startswith("超总仓位上限") or rotate_left <= 0:
        return result

    cap_val = _position_cap(cur_equity)
    cur_mkt = sum(p["current_price"] * p["shares"] for p in positions)
    need = add_mkt - max(0.0, cap_val - cur_mkt)
    new_dist = ((entry_px - stop_px) / entry_px) if entry_px > 0 else 0.0
    tgt = _rotation_target(positions, mkt, new_dist, need)
    if tgt is None:
        result["rotate_msg"] = f"{name}({code}) 无合适换出标的，放弃"
        return result

    last = float(tgt.get("current_price") or 0)
    rot = _rotation_sell(tgt, last, last_date, mkt,
                         f"换仓腾位：让位给{name}({code})")
    # 先干跑一次：换仓后仍不通过闸门就不卖（避免只卖不买、白砍一只）
    sim_positions = [p for p in positions if p is not tgt]
    sim_cash = cash + rot["net"]
    ok2, why2 = _risk_check(code, bx["outlay"], add_mkt, sim_positions, cur_equity,
                            sim_cash, halt_new_buy, risk_amt=risk_amt)
    if not ok2:
        result["rotate_msg"] = (f"{name}({code}) 换仓后仍被拦({why2})，放弃换仓")
        return result
    positions.remove(tgt)
    new_cash = sim_cash
    new_equity = new_cash + sum(p["current_price"] * p["shares"] for p in positions)
    ok3, why3 = _risk_check(code, bx["outlay"], add_mkt, positions, new_equity,
                            new_cash, halt_new_buy, risk_amt=risk_amt)
    result.update({"ok": ok3, "cash": new_cash, "equity": new_equity,
                   "sale": rot, "why": why3,
                   "rotate_msg": (f"换出{tgt['name']}({tgt['code']}) 净盈亏"
                                  f"{rot['pnl']:+.2f}元 → 让位{name}({code})")})
    if ok3:
        result["cash"] = new_cash - bx["outlay"]
        result["equity"] = result["cash"] + sum(
            p["current_price"] * p["shares"] for p in positions) + add_mkt
    return result


def _drawdown_halt(equity_history: list[dict], cur_equity: float) -> tuple[bool, float]:
    """熔断判定：历史峰值(含当前)相对当前回撤≥阈值 → 停买。返回(熔断, 当前回撤)。"""
    peak = max([pt["equity"] for pt in equity_history] + [cur_equity])
    dd = (peak - cur_equity) / peak if peak > 0 else 0.0
    return dd >= CIRCUIT_BREAKER_DD, dd


def _esb_trailing_stop(highs: list[float], lows: list[float], pos: dict) -> float:
    """二四棒移动止损（只上移）：仅今日High>昨日High 更新 trail=近4根含今日 min Low。"""
    stop = float(pos.get("stop", pos.get("init_stop", pos["buy_price"])))
    if len(highs) >= 2 and highs[-1] > highs[-2]:
        trail = min(lows[-4:]) if len(lows) >= 4 else min(lows)
        if trail > stop:
            stop = trail
    return stop


def _latest_esb_scan(ws: Path) -> dict:
    """读 quant_esb_scan 最新产物 json；缺失或损坏返回空结构。

    返回 ``{"breakout": [...], "pending": [...], "signals": [...]}``，
    信号字段含 code/name/type/entry_px/stop_px/shares/peak_idx/level_idx/gap_pct/close_px。
    """
    empty = {"breakout": [], "pending": [], "signals": []}
    d = ws / "esb_scan_output"
    if not d.exists():
        return empty
    files = sorted(d.glob("esb_scan_*.json"), reverse=True)
    if not files:
        return empty
    try:
        data = json.loads(files[0].read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return empty
    data.setdefault("breakout", [])
    data.setdefault("pending", [])
    if not data.get("signals"):
        data["signals"] = data["breakout"] + data["pending"]
    return data


def _cache_daily(code: str, days: int = 60) -> list[dict]:
    """本地增量库回退：读 a_stock_daily.db 的最近日线（在线行情不可用时兜底）。"""
    import sqlite3
    db = Path("/srv/anengos/data/a_stock_daily.db")
    if not db.exists():
        return []
    bs_code = ("sh." if code.startswith("6") else "sz.") + code
    conn = None
    try:
        conn = sqlite3.connect(str(db))
        cur = conn.execute(
            """SELECT date, open, high, low, close FROM daily
               WHERE code = ? ORDER BY date DESC LIMIT ?""",
            (bs_code, days),
        )
        rows = [{"date": r[0], "open": r[1], "high": r[2], "low": r[3], "close": r[4]}
                for r in cur.fetchall()]
        rows.reverse()
        return rows
    except Exception as e:  # noqa: BLE001
        print(f"[PAPER] 本地库回退 {code} 失败: {e}")
        return []
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.OperationalError:
                pass


def _daily_last_date(df) -> str | None:
    """取日线序列最后一行日期（归一为 YYYY-MM-DD）。"""
    if df is None or getattr(df, "empty", True) or "date" not in df.columns:
        return None
    return str(df["date"].iloc[-1])[:10]


_SRC_LABEL = {"online": "在线", "cache": "本地兜底",
              "cache_newer": "本地较新", "none": "缺失"}


def _src_label(source: str | None) -> str:
    return _SRC_LABEL.get(source or "none", source or "缺失")


def _daily(code: str, days: int = 60) -> tuple[Any, dict]:
    """拉日线：在线 akshare 与本地增量库双源比对，采用日期较新的一版。

    返回 ``(df, meta)``；``df`` 为标准化 DataFrame（date/open/high/low/close），
    无行情时为 None。``meta`` 字段：
      - ``source``: ``online`` / ``cache`` / ``cache_newer`` / ``none``
      - ``last_date``: 实际采用序列的最后交易日
      - ``online_date`` / ``cache_date``: 两源各自最后交易日（可能 None）
      - ``note``: 来源说明（兜底/较新覆盖原因）

    规则：在线不可用或为空 → 回退本地库；双源都有但本地日期更新 → 采用本地库，
    避免在线源尚未落当日行情时，移动止损与估值被旧在线序列带偏。
    """
    import pandas as pd  # 容器内随 akshare 安装；缺失则放弃回退

    online_df = None
    online_date: str | None = None
    online_err: Exception | None = None
    if _AK_AVAILABLE:
        for attempt in range(3):
            try:
                odf = ak.stock_zh_a_daily(
                    symbol=_sina_code(code), start_date="20240601", adjust="qfq")
                if odf is not None and not odf.empty:
                    online_df = odf.tail(days)
                    online_date = _daily_last_date(online_df)
                break  # 空结果不重试
            except Exception as e:  # noqa: BLE001
                online_err = e
                if attempt < 2:
                    time.sleep(0.6 * (attempt + 1))

    cache_df = None
    cache_date: str | None = None
    cache_rows = _cache_daily(code, days)
    if cache_rows:
        try:
            cache_df = pd.DataFrame(cache_rows).tail(days)
            cache_date = _daily_last_date(cache_df)
        except Exception as e:  # noqa: BLE001
            print(f"[PAPER] 本地库日线 {code} 标准化失败: {e}")
            cache_df, cache_date = None, None

    if online_df is not None and cache_df is not None:
        if cache_date and (not online_date or cache_date > online_date):
            return cache_df, {
                "source": "cache_newer", "last_date": cache_date,
                "online_date": online_date, "cache_date": cache_date,
                "note": f"本地库 {cache_date} 新于在线 {online_date}，采用本地库行情",
            }
        return online_df, {
            "source": "online", "last_date": online_date,
            "online_date": online_date, "cache_date": cache_date, "note": "",
        }
    if online_df is not None:
        return online_df, {
            "source": "online", "last_date": online_date,
            "online_date": online_date, "cache_date": cache_date, "note": "",
        }
    if cache_df is not None:
        note = "在线行情不可用，回退本地库" if online_err else "在线行情为空，回退本地库"
        return cache_df, {
            "source": "cache", "last_date": cache_date,
            "online_date": online_date, "cache_date": cache_date, "note": note,
        }

    if online_err:
        print(f"[PAPER] 在线日线 {code} 不可用({online_err})，本地库亦无数据")
    return None, {"source": "none", "last_date": None,
                  "online_date": online_date, "cache_date": cache_date,
                  "note": "无行情"}


# 闸3：二四棒虚拟盘连续持有 ≥28 天（4周）且组合跑赢同期基准，方可晋升。
GATE3_DAYS = 28


def _esb_gate3_lines(esb_pos: list[dict], mkt: str) -> list[str]:
    """闸3进度跟踪：列出每只二四棒持仓的持有天数/28 与浮盈；满28天标记待判定。

    满期后的基准超额（同期沪深300）由周复盘拉取计算，此处只做进度与组合浮盈，
    不伪造判定——未满28天一律显示"跟踪中"。
    """
    out = ["## 二四棒闸3（虚拟盘 ≥4周/28天 对照跟踪）", ""]
    if not esb_pos:
        out.append("- 当前无二四棒持仓，闸3待建仓启动。")
        return out
    try:
        today_d = datetime.date.fromisoformat(str(mkt)[:10])
    except Exception:
        today_d = None
    out += [
        "| 代码 | 名称 | 买入日 | 持有天/28 | 买价 | 现价 | 浮盈% | 状态 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    ready: list[dict] = []
    for p in esb_pos:
        bd = str(p.get("buy_date", ""))[:10]
        days, status = "—", "跟踪中"
        if today_d and bd:
            try:
                dd = (today_d - datetime.date.fromisoformat(bd)).days
                days = f"{dd}/{GATE3_DAYS}"
                if dd >= GATE3_DAYS:
                    status = "满4周待判定"
                    ready.append(p)
            except Exception:
                pass
        out.append(
            f"| {p['code']} | {p['name']} | {bd} | {days} | "
            f"{p['buy_price']:.2f} | {p['current_price']:.2f} | "
            f"{p.get('pnl_pct', 0):+.2f}% | {status} |"
        )
    if ready:
        wret = sum(p.get("pnl_pct", 0) for p in ready) / len(ready)
        out += [
            "",
            f"- {len(ready)} 只已满28天，组合等权浮盈 {wret:+.2f}%；"
            f"闸3最终判定需对比同期沪深300超额（周复盘自动拉取，超额>0 且回撤可控方 PASS）。",
        ]
    else:
        out += [
            "",
            f"- 闸3要求连续持有≥{GATE3_DAYS}天且组合跑赢同期基准；当前未满，继续跟踪，满期自动判定，不提前放行。",
        ]
    return out


def _mkt_date(ws: Path, today: str) -> str:
    """单一日历源：全局市场交易日（<=今天且必须是交易日），杜绝 per-source 未来/非交易日日期引入倒挂。

    候选：历史维护的 last_market_date / 历史交易 / 持仓日期；权威约束：sina 交易日历。
    """
    data: dict = {}
    pf = ws / "paper_positions.json"
    if pf.exists():
        try:
            data = json.loads(pf.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    # 权威交易日集合（<= today）
    trade_dates: set[str] = set()
    try:
        tdf = ak.tool_trade_date_hist_sina()  # 全年交易日历（不含周末/节假日）
        trade_dates = {str(d)[:10] for d in tdf["trade_date"] if str(d)[:10] <= today}
    except Exception:  # noqa: BLE001
        pass
    cand: list[str] = []
    lm = data.get("last_market_date") or data.get("last_exec_date")
    if lm and str(lm)[:10] <= today:
        cand.append(str(lm)[:10])
    for h in data.get("history", []):
        d = str(h.get("sell_date") or "")
        if d and str(d)[:10] <= today:
            cand.append(str(d)[:10])
    for p in data.get("positions", []):
        d = str(p.get("buy_date") or "")
        if d and str(d)[:10] <= today:
            cand.append(str(d)[:10])
    if trade_dates:
        # 以权威交易日历的最新交易日为准；历史台账候选只作日历不可用时的兜底，
        # 避免旧候选把全局市场日卡在滞后日期（曾导致 last_market_date 多日不推进）。
        return max(trade_dates)
    # 交易日历不可用：信任历史维护值（写入时已按交易日校验），避免非交易日脏值污染
    if lm and str(lm)[:10] <= today:
        return str(lm)[:10]
    # 无任何历史：周末回退最近周五（防周日买入），周中直接用 today
    try:
        dt = datetime.date.fromisoformat(today)
        if dt.weekday() == 5:      # 周六 → 周五
            return (dt - datetime.timedelta(days=1)).isoformat()
        if dt.weekday() == 6:      # 周日 → 周五
            return (dt - datetime.timedelta(days=2)).isoformat()
        return today
    except Exception:  # noqa: BLE001
        return today


def _sell_date(last_date: str, buy_date: str, mkt: str) -> str:
    """卖出日记实际成交依据的行情日；行情日早于买入日（数据倒退）时回退日历日，防 buy>sell 倒挂。"""
    return str(last_date)[:10] if str(last_date)[:10] >= str(buy_date)[:10] else mkt


def _quote_stale(last_date: str | None, mkt: str) -> bool:
    """行情日早于全局交易日（当日收盘尚未入库/在线源未更新）→ 估值滞后，须打陈旧标记。"""
    return bool(last_date and str(last_date)[:10] < str(mkt)[:10])


def _load_pending_orders(ws: Path) -> list[dict]:
    """读取待执行的次日挂单列表。"""
    f = ws / PENDING_ORDER_FILE
    if not f.exists():
        return []
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_pending_orders(ws: Path, orders: list[dict]) -> None:
    """保存次日挂单列表（原子写）。"""
    f = ws / PENDING_ORDER_FILE
    _atomic_write_json(f, orders)


def _sync_pending_to_feishu(orders: list[dict], mkt_date: str) -> str:
    """把次日挂单列表同步到飞书每日复盘表。

    格式化成多行文本，写入当天记录的「次日挂单列表」字段。
    返回同步状态说明。
    """
    import urllib.request
    import urllib.error

    # 未配置飞书凭据则跳过（本地开发/测试环境）
    if not (FEISHU_APP_ID and FEISHU_APP_SECRET and FEISHU_BASE_TOKEN and FEISHU_TABLE_ID):
        return "飞书同步跳过: 未配置飞书凭据（环境变量或 .secrets/feishu_app.json 均缺失）"

    if not orders:
        text = "（无挂单）"
    else:
        lines = []
        for o in sorted(orders, key=lambda x: x["gap_pct"]):
            lines.append(
                f"{o['code']} {o['name']} | 挂单{o['entry_px']:.2f} | "
                f"止损{o['stop_px']:.2f} | 距突破{o['gap_pct']:.1f}% | {o['shares']}股"
            )
        text = "\n".join(lines)

    try:
        # 1. 获取 tenant_access_token
        token_url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        token_body = json.dumps({
            "app_id": FEISHU_APP_ID,
            "app_secret": FEISHU_APP_SECRET,
        }).encode("utf-8")
        req = urllib.request.Request(token_url, data=token_body,
                                     headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=10)
        token_data = json.loads(resp.read().decode("utf-8"))
        tenant_token = token_data.get("tenant_access_token")
        if not tenant_token:
            return f"飞书同步失败: 未获取到token ({token_data})"

        # 2. 查找当天日期的记录
        search_url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_BASE_TOKEN}"
                      f"/tables/{FEISHU_TABLE_ID}/records/search")
        search_body = json.dumps({
            "filter": {
                "conjunction": "and",
                "conditions": [
                    {
                        "field_name": "日期",
                        "operator": "is",
                        "value": [mkt_date],
                    }
                ],
            },
            "page_size": 1,
        }).encode("utf-8")
        req = urllib.request.Request(search_url, data=search_body, method="POST",
                                     headers={
                                         "Authorization": f"Bearer {tenant_token}",
                                         "Content-Type": "application/json",
                                     })
        resp = urllib.request.urlopen(req, timeout=10)
        search_data = json.loads(resp.read().decode("utf-8"))
        items = search_data.get("data", {}).get("items", [])

        if not items:
            return f"飞书同步跳过: 当天({mkt_date})复盘记录不存在"

        record_id = items[0]["record_id"]

        # 3. 更新「次日挂单列表」字段
        update_url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/{FEISHU_BASE_TOKEN}"
                      f"/tables/{FEISHU_TABLE_ID}/records/{record_id}")
        update_body = json.dumps({
            "fields": {
                "次日挂单列表": text,
            }
        }).encode("utf-8")
        req = urllib.request.Request(update_url, data=update_body, method="PUT",
                                     headers={
                                         "Authorization": f"Bearer {tenant_token}",
                                         "Content-Type": "application/json",
                                     })
        resp = urllib.request.urlopen(req, timeout=10)
        update_data = json.loads(resp.read().decode("utf-8"))
        if update_data.get("code") == 0:
            return f"飞书同步成功: {len(orders)}只挂单已写入 {mkt_date} 复盘表"
        else:
            return f"飞书同步失败: {update_data.get('msg', 'unknown error')}"

    except urllib.error.URLError as e:
        return f"飞书同步失败: 网络错误 {e}"
    except Exception as e:
        return f"飞书同步异常: {e}"


class PaperTraderAdapter(AgentAdapter):
    name = "paper_trader"

    def __init__(self) -> None:
        self.mode = "akshare" if _AK_AVAILABLE else "mock"

    def submit(self, task: str, workspace: str) -> AgentResult:
        ws = Path(workspace)
        out_dir = ws / "paper_trader_output"
        out_dir.mkdir(parents=True, exist_ok=True)
        pos_file = ws / "paper_positions.json"

        # 1. 读已有数据（持仓 + 已卖出历史）；损坏时备份样本并告警，不静默清空
        data, account_warnings = _load_account(pos_file)

        positions = data.get("positions", [])
        history = data.get("history", [])

        # 一次性迁移：删除历史日期倒挂脏记录（buy_date > sell_date，pnl=0 无交易语义）
        _clean = [h for h in history if not (
            h.get("buy_date") and h.get("sell_date")
            and str(h["buy_date"]) > str(h["sell_date"]))]
        if len(_clean) != len(history):
            history = _clean
            data["history"] = _clean

        # 虚拟资金账户（模拟盘净值/仓位/回撤跟踪，首日初始 100 万）
        INITIAL_CASH = 1_000_000.0
        cash = float(data.get("cash", INITIAL_CASH))
        equity_history = data.get("equity_history", [])
        last_exec_date = data.get("last_exec_date", "")  # 上次买入行情日（防周末/重复执行）

        if not _AK_AVAILABLE:
            return AgentResult(True, self.name, "done",
                f"[{self.name}/mock] akshare 未装，持仓 {len(positions)} 只",
                artifact_paths=[])

        today = datetime.datetime.now().strftime("%Y-%m-%d")
        mkt = _mkt_date(ws, today)  # 全局市场交易日（单一日历源，杜绝倒挂）

        # 2. 卖出检查：按 strategy 分流
        #    普通持仓：破MA20 止损 > 止盈落袋 > RSI>70 减仓
        #    ersibang 持仓：跌破有效止损（左级Low / 移动止损只上移）→ 全部卖出
        sold_today = []
        sold_codes: set[str] = set()
        sold_pnl_total = 0.0
        total_fees = 0.0  # 本轮累计交易费用（佣金+印花税+过户费+滑点已含在成交价）
        still_held = []
        missing_names: list[str] = []
        mkt_dates: list[str] = []
        source_counter: dict[str, int] = {"online": 0, "cache": 0, "cache_newer": 0, "none": 0}
        newer_notes: list[str] = []
        stale_notes: list[str] = []
        n_esb = 0
        n_legacy = 0
        for p in positions:
            df, qmeta = _daily(p["code"])
            source_counter[qmeta["source"]] = source_counter.get(qmeta["source"], 0) + 1
            if df is None:
                # 行情缺失：沿用上次价格估值，但打陈旧标记，报告中单独提示，避免净值被误读为最新价
                p["price_stale"] = True
                p["data_source"] = qmeta["source"]
                still_held.append(p)
                missing_names.append(f"{p['name']}({p['code']})")
                continue
            p["price_stale"] = False
            p["data_source"] = qmeta["source"]
            p["quote_date"] = qmeta["last_date"]
            if qmeta["source"] == "cache_newer" and qmeta["note"]:
                newer_notes.append(f"{p['name']}({p['code']})：{qmeta['note']}")
            closes = [float(x) for x in df["close"].tolist()]
            last = closes[-1]
            last_date = qmeta["last_date"] or str(df["date"].iloc[-1])[:10]  # 行情最新交易日（非日历日）
            mkt_dates.append(last_date)
            # 行情日早于全局交易日（当日收盘尚未入库/在线源未更新）：估值滞后，打陈旧标记并提示
            if _quote_stale(last_date, mkt):
                p["price_stale"] = True
                stale_notes.append(f"{p['name']}({p['code']})：行情日 {last_date} 早于交易日 {mkt}，按滞后收盘价估值")
            pnl_pct = (last - p["buy_price"]) / p["buy_price"] * 100

            # T+1：同一行情日买入的股票当日不可卖出（先卖后买顺序下主要保护重复运行/历史脏数据）
            if str(p.get("buy_date", ""))[:10] == mkt:
                p["current_price"] = last
                p["pnl"] = (last - p["buy_price"]) * p["shares"]
                p["pnl_pct"] = pnl_pct
                still_held.append(p)
                continue

            reason = None
            sell_shares = p["shares"]
            # 存量持仓口径迁移：buy_date 若为未来日期（历史 bug）或日历日，修正为全局市场交易日
            if p.get("buy_date") and str(p["buy_date"]) > mkt:
                p["buy_date"] = mkt
            elif p.get("buy_date") == today and last_date != today:
                p["buy_date"] = mkt
            highs = [float(x) for x in df["high"].tolist()]
            lows = [float(x) for x in df["low"].tolist()]
            if p.get("strategy") == "ersibang":
                new_stop = _esb_trailing_stop(highs, lows, p)
                p["stop"] = new_stop
                if lows[-1] < new_stop:
                    reason = f"二四棒跌破止损({lows[-1]:.2f}<{new_stop:.2f})"
                    n_esb += 1
            else:
                # 历史遗留普通仓（过渡期兜底清出，不再新增）：优先跌破初始止损，
                # 否则浮亏达 LEGACY_STOP_PCT 兜底全卖；不做止盈/RSI/MA20 判断。
                init_floor = float(p.get("init_stop") or p.get("stop") or 0)
                if init_floor > 0 and lows[-1] < init_floor:
                    reason = f"遗留仓跌破初始止损({lows[-1]:.2f}<{init_floor:.2f})"
                    n_legacy += 1
                elif pnl_pct <= -LEGACY_STOP_PCT * 100:
                    reason = f"遗留仓兜底止损({pnl_pct:.1f}%≤-{LEGACY_STOP_PCT * 100:.0f}%)"
                    n_legacy += 1

            if reason:
                # 卖出（全卖或减仓）：滑点下浮 + 佣金/印花税/过户费后净回笼
                ex = _sell_exec(last, sell_shares)
                cost_basis = p["buy_price"] * sell_shares  # buy_price 为含费成本单价
                pnl = ex["net"] - cost_basis
                fee = ex["commission"] + ex["stamp"] + ex["transfer"]
                total_fees += fee
                # 卖出日记实际成交依据的行情日（last_date）；行情倒退时回退日历日，杜绝 buy>sell 倒挂
                sell_d = _sell_date(last_date, p.get("buy_date", ""), mkt)
                sale = {
                    **p,
                    "sell_date": sell_d,
                    "sell_quote_date": last_date,
                    "sell_price": ex["fill"],
                    "sell_reason": reason,
                    "shares": sell_shares,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "sell_fee": round(fee, 2),
                    "partial": sell_shares < p["shares"],
                }
                history.append(sale)
                sold_codes.add(p["code"])
                sold_pnl_total += pnl
                cash += ex["net"]  # 卖出净回笼现金（已扣佣金/印花税/过户费）
                if sell_shares < p["shares"]:
                    # 减仓：剩余仓位继续持有，更新现价与浮盈亏
                    p["shares"] -= sell_shares
                    p["current_price"] = last
                    p["pnl"] = (last - p["buy_price"]) * p["shares"]
                    p["pnl_pct"] = pnl_pct
                    still_held.append(p)
                sold_today.append(f"{p['name']}({p['code']}) {reason} 净盈亏{pnl:+.2f}元(费{fee:.2f})")
            else:
                # 继续持有，更新现价
                p["current_price"] = last
                p["pnl"] = (last - p["buy_price"]) * p["shares"]
                p["pnl_pct"] = (last - p["buy_price"]) / p["buy_price"] * 100
                still_held.append(p)

        positions = still_held

        # 提前初始化（挂单成交和二四棒追入都会用）
        held_codes = {p["code"] for p in positions}
        used_esb_peaks = {(h["code"], h.get("esb_peak_idx")) for h in history
                          if h.get("esb_peak_idx") is not None}
        new_exec_dates: list[str] = []

        # 2.5 【提前挂单模式】处理昨日挂单：检查今日是否成交
        pending_orders = _load_pending_orders(ws)
        filled_pending = []
        expired_pending = []


        for order in pending_orders:
            code = order["code"]
            entry_px = order["entry_px"]
            if code in held_codes:
                expired_pending.append(f"{order['name']}({code}) 已持仓，挂单作废")
                continue
            # 取今日日线（本地库优先）判定是否触及挂单价
            try:
                df, qmeta = _daily(code)
                if df is None or df.empty:
                    expired_pending.append(f"{order['name']}({code}) 无行情，挂单作废")
                    continue
                today_low = float(df["low"].iloc[-1])
                today_high = float(df["high"].iloc[-1])
                today_date = qmeta["last_date"] or str(df["date"].iloc[-1])[:10]
                order_date = str(order.get("order_date", ""))[:10]
                expire_date = str(order.get("expire_date") or _next_trade_day(order_date or today_date))[:10]
                # 有效期校验：行情日早于挂单日（数据倒退）或晚于到期日 → 过期作废
                if order_date and today_date < order_date:
                    expired_pending.append(f"{order['name']}({code}) 行情日{today_date}早于挂单日{order_date}，挂单作废")
                    continue
                if today_date > expire_date:
                    expired_pending.append(
                        f"{order['name']}({code}) 已过有效期(挂单{order_date}/到期{expire_date}，今日{today_date})，作废")
                    continue
                # 一字板（高低价相等，无正常流动性）→ 挂单不参与成交，规避追板风险
                if today_high == today_low:
                    expired_pending.append(f"{order['name']}({code}) 一字板，挂单不参与成交，作废")
                    continue
                # 成交条件：Low <= 挂单价 <= High
                if today_low <= entry_px <= today_high:
                    shares = order["shares"]
                    bx = _buy_exec(entry_px, shares)
                    filled_pending.append({"order": order, "bx": bx, "today_date": today_date})
                else:
                    expired_pending.append(f"{order['name']}({code}) 今日[{today_low:.2f},{today_high:.2f}]未触及挂单价{entry_px:.2f}，作废")
            except Exception as e:
                expired_pending.append(f"{order['name']}({code}) 行情异常，挂单作废: {e}")

        # P7 组合级风控：买入前熔断判定（净值峰值回撤≥15% → 本轮停止新开仓，只卖不买）
        _cur_mkt = sum(p["current_price"] * p["shares"] for p in positions)
        cur_equity = cash + _cur_mkt
        halt_new_buy, cur_dd = _drawdown_halt(equity_history, cur_equity)
        n_risk_block = 0
        n_esb_fill = 0       # 实际成交笔数（不含拦截/跳过）
        n_pending_fill = 0

        # 2.6 挂单成交买入（过风控检查；被总仓位上限拦住时可换仓腾额度）
        esb_pending_bought = []
        n_rotate = 0
        for fp in filled_pending:
            order = fp["order"]
            code = order["code"]
            risk_amt = max(0.0, order["entry_px"] - order["stop_px"]) * order["shares"]
            gate = _gate_buy(code, order["name"], order["shares"], order["entry_px"],
                             order["entry_px"], order["stop_px"],
                             positions=positions, cash=cash, cur_equity=cur_equity,
                             halt_new_buy=halt_new_buy, risk_amt=risk_amt,
                             mkt=mkt, last_date=fp["today_date"],
                             rotate_left=ROTATE_MAX_PER_DAY - n_rotate)
            sale = gate["sale"]
            if sale:
                history.append(sale["sale"])
                sold_codes.add(sale["sale"]["code"])
                total_fees += sale["fee"]
                sold_pnl_total += sale["pnl"]
                sold_today.append(f"{sale['sale']['name']}({sale['sale']['code']}) "
                                  f"{sale['sale']['sell_reason']} 净盈亏{sale['pnl']:+.2f}元"
                                  f"(费{sale['fee']:.2f})")
                n_rotate += 1
                esb_pending_bought.append(f"🔄{gate['rotate_msg']}")
            if not gate["ok"]:
                n_risk_block += 1
                esb_pending_bought.append(
                    f"{order['name']}({code}) 🛡{gate['why']}（挂单成交但风控拦截）")
                continue
            bx = gate["bx"]
            cash = gate["cash"]
            cur_equity = gate["equity"]
            total_fees += bx["commission"] + bx["transfer"]
            positions.append({
                "code": code, "name": order["name"],
                "strategy": "ersibang",
                "buy_date": fp["today_date"],
                "buy_price": bx["cost_per_share"],
                "fill_price": round(bx["fill"], 4),
                "buy_fee": round(bx["commission"] + bx["transfer"], 2),
                "esb_entry_px": order["entry_px"], "init_stop": order["stop_px"],
                "stop": order["stop_px"], "esb_peak_idx": order["peak_idx"],
                "esb_level_idx": order["level_idx"],
                "order_type": "pending_limit",  # 标记是挂单成交
                "shares": order["shares"], "current_price": bx["fill"],
                "pnl": 0.0, "pnl_pct": 0.0,
            })
            used_esb_peaks.add((code, order["peak_idx"]))
            new_exec_dates.append(mkt)
            n_pending_fill += 1
            cur_equity = cash + sum(p["current_price"] * p["shares"] for p in positions)
            esb_pending_bought.append(f"{order['name']}({code}) 挂单成交@{bx['fill']:.2f} "
                                      f"(左峰{order['entry_px']:.2f}) 买{order['shares']}股")

        # 3. 当日突破开仓：读 quant_esb_scan 最新产物的 breakout 列表（二四棒单一策略）
        esb_scan = _latest_esb_scan(ws)
        esb_bought = []
        esb_total = len(esb_scan["breakout"])
        esb_hit_total = 0
        for sig in esb_scan["breakout"]:
            code = sig["code"]
            name = sig.get("name") or code
            if code in held_codes or code in sold_codes:
                continue
            # 同一左峰不敲第二次（历史已用过的 peak 跳过）
            if (code, sig.get("peak_idx")) in used_esb_peaks:
                continue
            # 无新行情（周末/节假日重复运行）：跳过买入，防止重复补买
            if last_exec_date and mkt == last_exec_date:
                continue
            # 错误日志拦截（自我进化：该票曾有未修复的系统 bug / 禁止回补类教训 → 跳过）
            guard = error_guard(str(ws), code)
            if guard:
                esb_bought.append(f"{name}({code}) ⛔{guard}")
                continue
            df, qmeta = _daily(code)
            if df is None or df.empty:
                continue
            highs = [float(x) for x in df["high"].tolist()]
            lows = [float(x) for x in df["low"].tolist()]
            closes = [float(x) for x in df["close"].tolist()]
            entry_px = float(sig["entry_px"])
            stop_px = float(sig["stop_px"])
            # 复核：当日 high 仍需站上左峰；一字板不追；行情倒退/失真跳过
            if highs[-1] < entry_px or highs[-1] == lows[-1]:
                continue
            esb_hit_total += 1
            price = closes[-1]
            # 风控定仓：按当前权益风险预算 + 止损距离二次定仓，窄距离不放大仓位
            shares = size_shares(cur_equity, RISK_PER_TRADE_PCT, price, stop_px,
                                 MIN_STOP_DIST_PCT, SINGLE_POSITION_CAP, cash)
            risk_amt = max(0.0, price - stop_px) * shares
            gate = _gate_buy(code, name, shares, price, entry_px, stop_px,
                             positions=positions, cash=cash, cur_equity=cur_equity,
                             halt_new_buy=halt_new_buy, risk_amt=risk_amt,
                             mkt=mkt, last_date=qmeta["last_date"] or mkt,
                             rotate_left=ROTATE_MAX_PER_DAY - n_rotate)
            sale = gate["sale"]
            if sale:
                history.append(sale["sale"])
                sold_codes.add(sale["sale"]["code"])
                total_fees += sale["fee"]
                sold_pnl_total += sale["pnl"]
                sold_today.append(f"{sale['sale']['name']}({sale['sale']['code']}) "
                                  f"{sale['sale']['sell_reason']} 净盈亏{sale['pnl']:+.2f}元"
                                  f"(费{sale['fee']:.2f})")
                n_rotate += 1
                esb_bought.append(f"🔄{gate['rotate_msg']}")
            if not gate["ok"]:
                n_risk_block += 1
                esb_bought.append(f"{name}({code}) 🛡{gate['why']}")
                continue
            bx = gate["bx"]
            cash = gate["cash"]
            cur_equity = gate["equity"]
            total_fees += bx["commission"] + bx["transfer"]
            positions.append({
                "code": code, "name": name,
                "strategy": "ersibang",
                "buy_date": mkt,  # 统一全局市场交易日
                "buy_price": bx["cost_per_share"],  # 含费成本单价（佣金+过户费+滑点）
                "fill_price": round(bx["fill"], 4),
                "buy_fee": round(bx["commission"] + bx["transfer"], 2),
                "esb_entry_px": entry_px, "init_stop": stop_px,
                "stop": stop_px, "esb_peak_idx": sig.get("peak_idx"),
                "esb_level_idx": sig.get("level_idx"),
                "shares": shares, "current_price": bx["fill"],
                "pnl": 0.0, "pnl_pct": 0.0,
            })
            used_esb_peaks.add((code, sig.get("peak_idx")))
            new_exec_dates.append(mkt)
            n_esb_fill += 1
            cur_equity = cash + sum(p["current_price"] * p["shares"] for p in positions)
            esb_bought.append(f"{name}({code}) 突破左峰{entry_px:.2f} "
                              f"买{shares}股 @ {bx['fill']:.2f}(含费成本{bx['cost_per_share']:.2f})")

        # 3c. 【提前挂单模式】生成次日挂单列表
        new_pending: list[dict] = []
        esb_pending_total = len(esb_scan["pending"])
        esb_pending_hit = 0
        for sig in esb_scan["pending"]:
            code = sig["code"]
            name = sig.get("name") or code
            if code in held_codes or code in sold_codes:
                continue
            # 同一左峰不挂第二次（历史用过的跳过）
            if (code, sig.get("peak_idx")) in used_esb_peaks:
                continue
            entry_px = float(sig["entry_px"])
            stop_px = float(sig["stop_px"])
            # 按挂单价到止损位距离、当前权益二次定仓（成交时再过组合风控）
            pend_shares = size_shares(cur_equity, RISK_PER_TRADE_PCT,
                                     entry_px, stop_px,
                                     MIN_STOP_DIST_PCT, SINGLE_POSITION_CAP, cash)
            new_pending.append({
                "code": code, "name": name,
                "entry_px": entry_px, "stop_px": stop_px,
                "shares": pend_shares, "peak_idx": sig.get("peak_idx"),
                "level_idx": sig.get("level_idx"), "gap_pct": sig.get("gap_pct"),
                "close_px": sig.get("close_px"),
                "order_date": mkt,  # 挂单日期（今天收盘后挂，明天执行）
                "expire_date": _next_trade_day(mkt),  # 有效期到下一交易日，过期未成交作废
            })
            used_esb_peaks.add((code, sig.get("peak_idx")))
            esb_pending_hit += 1

        _save_pending_orders(ws, new_pending)

        # 同步次日挂单列表到飞书每日复盘表（按日历当天复盘行，与净值日期对齐）
        feishu_sync_msg = _sync_pending_to_feishu(new_pending, today)

        # 4. 持久化（含资金账户与净值历史）
        data["last_market_date"] = mkt  # 全局市场交易日（单一日历源）
        if new_exec_dates:
            data["last_exec_date"] = mkt  # 本次买入行情日，防周末重复
        mkt = data.get("last_market_date") or (data.get("last_exec_date") or "待定")
        mkt_val = sum(p["current_price"] * p["shares"] for p in positions)
        equity = cash + mkt_val
        if equity_history and equity_history[-1].get("date") == today:
            equity_history[-1] = {"date": today, "equity": round(equity, 2)}
        else:
            equity_history.append({"date": today, "equity": round(equity, 2)})
        data["cash"] = round(cash, 2)
        cumulative_fees = round(float(data.get("total_fees", 0.0)) + total_fees, 2)
        data["total_fees"] = cumulative_fees
        data["equity_history"] = equity_history[-120:]  # 保留近 120 个交易日
        data["positions"] = positions
        data["history"] = history
        _atomic_write_json(pos_file, data)

        # 最大回撤（基于净值历史）
        max_dd = 0.0
        peak = 0.0
        for pt in equity_history:
            v = pt["equity"]
            if v > peak:
                peak = v
            if peak > 0:
                dd = (peak - v) / peak
                if dd > max_dd:
                    max_dd = dd
        cum_pnl = equity - INITIAL_CASH
        pos_ratio = mkt_val / equity * 100 if equity > 0 else 0.0

        # 5. 输出报告
        total_cost = sum(p["buy_price"] * p["shares"] for p in positions)
        total_value = sum(p["current_price"] * p["shares"] for p in positions)
        total_pnl = total_value - total_cost

        esb_pos = [p for p in positions if p.get("strategy") == "ersibang"]
        esb_cost = sum(p["buy_price"] * p["shares"] for p in esb_pos)
        esb_value = sum(p["current_price"] * p["shares"] for p in esb_pos)
        max_single_w = max(
            (p["current_price"] * p["shares"]) / equity for p in positions
        ) if positions and equity > 0 else 0.0
        cap_now = _position_cap(equity)  # 动态总仓位上限（权益×50%，50 万起、100 万封顶）

        # 当日成交口径以台账为准：手动补跑/重复运行时本次计数会归零，
        # 但当日真实成交已落在 positions(buy_date)/history(sell_date) 上，取二者较大值避免报告失真。
        n_buy_day = max(n_esb_fill + n_pending_fill,
                        sum(1 for p in positions if str(p.get("buy_date", ""))[:10] == mkt))
        n_sell_day = max(len(sold_today),
                         sum(1 for h in history if str(h.get("sell_date", ""))[:10] == mkt))

        lines = [
            f"# 虚拟盘组合（{today}，行情截至最近交易日）",
            f"- 持仓：{len(positions)} 只（二四棒 {len(esb_pos)}） | 历史交易：{len(history)} 笔",
            f"- 今日成交（台账口径）：买入 {n_buy_day} 笔（本次突破 {n_esb_fill} / 挂单 {n_pending_fill}）"
            f" | 卖出 {n_sell_day} 笔（本次 {len(sold_today)}）",
            f"- 持仓成本：{total_cost:.2f} 元 | 市值：{total_value:.2f} 元",
            f"- 浮动盈亏：{total_pnl:+.2f} 元（{total_pnl/total_cost*100 if total_cost else 0:+.2f}%）",
            f"- 二四棒仓位：成本 {esb_cost:.2f} 元 | 市值 {esb_value:.2f} 元"
            f" | 浮盈 {esb_value-esb_cost:+.2f} 元" if esb_pos else "",
            "",
            "## 运行说明",
            f"- 本次运行日期：{today} | 行情最新交易日：{mkt}",
            f"- 行情来源：在线 {source_counter.get('online', 0)} | 本地兜底 {source_counter.get('cache', 0)} "
            f"| 本地较新覆盖 {source_counter.get('cache_newer', 0)} | 缺失 {source_counter.get('none', 0)}",
            *(f"- 📦 {n}" for n in newer_notes),
            *(f"- ⏳ {n}" for n in stale_notes),
            f"- 二四棒扫描：突破 {esb_total} 只 → 复核通过 {esb_hit_total} → 开仓 {n_esb_fill}",
            f"- 提前挂单：接近突破 {esb_pending_total} 只 → 生成次日挂单 {len(new_pending)} 只",
            f"- 昨日挂单：成交 {n_pending_fill} | 作废 {len(expired_pending)}",
            f"- {feishu_sync_msg}",
            f"- 无新行情或周末运行时自动跳过买入（last_exec_date 防重复补买）；现价为最新交易日收盘价",
            f"- 缺失行情标的：{', '.join(missing_names) if missing_names else '无'}"
            + ("（以下沿用上次价格估值，净值含陈旧价，未按最新收盘价）" if missing_names else ""),
            *(f"- ⚠️ {w}" for w in account_warnings),
            "",
            "## 规则执行清单",
            f"- 遗留普通仓：兜底清出 {n_legacy} 次（过渡期清仓，不再新增）",
            f"- 二四棒持仓：移动止损离场 {n_esb} 次 | 今日总卖出 {n_sell_day}",
            f"- 组合风控：熔断{'【触发·只卖不买】' if halt_new_buy else '未触发'}(买入前回撤{cur_dd*100:.2f}%) | 风控拦截买入 {n_risk_block} 次（单票≤20%/总仓≤{cap_now/10000:.0f}万/回撤15%熔断）",
            f"- 换仓：{n_rotate} 笔（被总仓位上限拦住时卖最弱换成新信号，每日上限{ROTATE_MAX_PER_DAY}笔）",
            "",
            "## 账户净值",
            f"- 可用现金：{cash:.2f} 元 | 持仓市值：{mkt_val:.2f} 元 | 总资产：{equity:.2f} 元",
            f"- 今日平仓净盈亏：{sold_pnl_total:+.2f} 元 | 累计盈亏：{cum_pnl:+.2f} 元（{cum_pnl/INITIAL_CASH*100:+.2f}%）",
            f"- 仓位比例：{pos_ratio:.1f}% | 历史最大回撤：{'数据不足' if len(equity_history) < 2 else f'{max_dd*100:.2f}%'} | 净值记录：{len(equity_history)} 天",
            f"- 风控状态：{'🔴熔断·只卖不买' if halt_new_buy else '🟢正常可买'} | 买入前回撤{cur_dd*100:.2f}%/熔断线15% | 单票最大{max_single_w*100:.1f}%/上限20% | 总仓{mkt_val:.0f}元/上限{cap_now/10000:.0f}万(权益×{POSITION_CAP_RATIO*100:.0f}%)",
            f"- 本轮交易费用：{total_fees:.2f} 元 | 累计交易费用：{cumulative_fees:.2f} 元（佣金万2.5最低5元双边/印花税卖出万5/过户费万0.1双边/滑点0.1%）",
            f"- 初始资金：{INITIAL_CASH:.0f} 元（已计佣金、印花税、过户费与滑点；A股T+1，当日买入次日方可卖）",
            "",
        ]
        if esb_bought:
            lines.append("## 今日买入（二四棒追入）")
            for b in esb_bought:
                lines.append(f"- {b}")
            lines.append("")
        if esb_pending_bought:
            lines.append("## 今日挂单成交（提前挂单模式）")
            for b in esb_pending_bought:
                lines.append(f"- {b}")
            lines.append("")
        if expired_pending:
            lines.append("## 挂单作废（未触及左峰价）")
            for e in expired_pending[:10]:
                lines.append(f"- {e}")
            if len(expired_pending) > 10:
                lines.append(f"- …共 {len(expired_pending)} 只")
            lines.append("")
        if new_pending:
            lines.append("## 次日挂单列表（提前挂单模式）")
            lines.append("| 代码 | 名称 | 挂单价(左峰) | 止损价(左级) | 距突破% | 计划股数 |")
            lines.append("|---|---|---|---|---|---|")
            for o in sorted(new_pending, key=lambda x: x["gap_pct"]):
                lines.append(f"| {o['code']} | {o['name']} | {o['entry_px']:.2f} "
                            f"| {o['stop_px']:.2f} | {o['gap_pct']:.2f}% | {o['shares']} |")
            lines.append("")
        if sold_today:
            lines.append("## 今日卖出")
            for s in sold_today:
                lines.append(f"- {s}")
            lines.append("")

        lines += [
            "## 当前持仓",
            "| 代码 | 名称 | 策略 | 买入日 | 含费成本 | 现价 | 数量 | 浮动盈亏 | 盈亏% | 行情日 | 来源 |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for p in positions:
            lines.append(
                f"| {p['code']} | {p['name']} | {p.get('strategy', '普通')} | {p['buy_date']} "
                f"| {p['buy_price']:.2f} | {p['current_price']:.2f} | {p['shares']} "
                f"| {p['pnl']:+.2f} | {p['pnl_pct']:+.2f}% "
                f"| {p.get('quote_date', '-')} | {_src_label(p.get('data_source'))} |"
            )

        # 闸3：二四棒 ≥28天 对照进度（未满不提前放行）
        lines += [""] + _esb_gate3_lines(esb_pos, mkt) + [""]

        # ---- 次日待执行指令清单（下个交易日开盘后需检查项）----
        def _next_day_instructions(poss, market_day):
            inst = [f"- 下一交易日（{market_day} 后首个交易日）检查清单："]
            for p in poss:
                if p.get("strategy") == "ersibang":
                    inst.append(f"  - {p['name']}({p['code']})：现价 vs 有效止损 {p.get('stop', 0):.2f}"
                                f"（跌破即全部卖出；仅当今日High>昨日High时上移止损）")
                else:
                    inst.append(f"  - {p['name']}({p['code']})：遗留仓，跌破初始止损或浮亏≥{LEGACY_STOP_PCT * 100:.0f}% 清出")
            inst.append("  - 买入侧：等待二四棒新突破 / 接近突破信号（仅在新交易日执行）")
            return "\n".join(inst)

        lines += ["", "## 次日待执行指令清单",
                  _next_day_instructions(positions, mkt), ""]

        out_path = out_dir / f"portfolio_{today.replace('-', '')}.md"
        out_path.write_text("\n".join(lines), encoding="utf-8")

        # ---- 结构化摘要（供互审监督完整核验）----
        sum_lines = [f"[{self.name}/akshare] 持仓{len(positions)}只（二四棒{len(esb_pos)}），"
                     f"今日买{n_buy_day}卖{n_sell_day}"]
        sum_lines.append(f"## 运行说明")
        is_trading_day = mkt == today
        sum_lines.append(f"- 本次运行 {today}{'（非交易日）' if not is_trading_day else ''} | "
                         f"行情最新交易日 {mkt} | 现价=最新交易日收盘价")
        sum_lines.append(f"- 卖出检查：已执行（本日触发 二四棒离场 {n_esb} / 遗留仓兜底清出 {n_legacy}）")
        sum_lines.append(f"- 买入{'已跳过（无新行情）' if not is_trading_day else f'执行：二四棒突破 {esb_total}→复核 {esb_hit_total}→买 {n_esb_fill}'}")
        sum_lines.append(f"- 缺失行情标的：{', '.join(missing_names) if missing_names else '无'}")
        sum_lines.append(f"- 行情来源：在线 {source_counter.get('online', 0)} | 本地兜底 {source_counter.get('cache', 0)} "
                          f"| 本地较新覆盖 {source_counter.get('cache_newer', 0)} | 缺失 {source_counter.get('none', 0)}")
        for n in newer_notes:
            sum_lines.append(f"- 📦 {n}")
        for n in stale_notes:
            sum_lines.append(f"- ⏳ {n}")
        for w in account_warnings:
            sum_lines.append(f"- ⚠️ {w}")
        sum_lines.append(f"## 规则执行清单")
        sum_lines.append(f"- 二四棒止损离场 {n_esb} | 遗留仓兜底清出 {n_legacy} "
                         f"| 缺失行情 {'有' if missing_names else '无'}")
        sum_lines.append(f"- 组合风控：{'熔断停买' if halt_new_buy else '正常'}(回撤{cur_dd*100:.2f}%) "
                         f"| 拦截 {n_risk_block} 次（单票≤20%/总仓≤{cap_now/10000:.0f}万/回撤15%熔断）")
        if esb_bought:
            sum_lines.append("## 今日买入（二四棒突破）")
            sum_lines += [f"- {b}" for b in esb_bought]
        if sold_today:
            sum_lines.append("## 今日卖出")
            sum_lines += [f"- {s}" for s in sold_today]
        # 二四棒持仓明细（监督重点）
        if esb_pos:
            sum_lines.append("## 二四棒当前持仓")
            sum_lines.append("| 代码 | 名称 | 买入日 | 买价 | 现价 | 数量 | 浮动盈亏 | 盈亏% | 止损 | 行情日 | 来源 |")
            sum_lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
            for p in esb_pos:
                sum_lines.append(
                    f"| {p['code']} | {p['name']} | {p['buy_date']} | {p['buy_price']:.2f} "
                    f"| {p['current_price']:.2f} | {p['shares']} | {p['pnl']:+.2f} "
                    f"| {p['pnl_pct']:+.2f}% | {p.get('stop', 0):.2f} "
                    f"| {p.get('quote_date', '-')} | {_src_label(p.get('data_source'))} |")
        # 普通持仓明细（成本/市值/权重）
        norm_pos = [p for p in positions if p.get("strategy") != "ersibang"]
        if norm_pos:
            sum_lines.append("## 普通持仓明细")
            sum_lines.append("| 代码 | 名称 | 买入日 | 买价 | 现价 | 数量 | 成本 | 市值 | 权重% | 盈亏% | 行情日 | 来源 |")
            sum_lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
            for p in norm_pos:
                cost_v = p["buy_price"] * p["shares"]
                val_v = p["current_price"] * p["shares"]
                wt = val_v / mkt_val * 100 if mkt_val > 0 else 0.0
                sum_lines.append(
                    f"| {p['code']} | {p['name']} | {p['buy_date']} | {p['buy_price']:.2f} "
                    f"| {p['current_price']:.2f} | {p['shares']} | {cost_v:.0f} | {val_v:.0f} "
                    f"| {wt:.1f}% | {p['pnl_pct']:+.2f}% "
                    f"| {p.get('quote_date', '-')} | {_src_label(p.get('data_source'))} |")
        # 分组统计
        esb_pnl = sum(p.get("pnl", 0.0) for p in esb_pos)
        norm_pnl = sum(p.get("pnl", 0.0) for p in positions if p.get("strategy") != "ersibang")
        sum_lines.append("## 分组统计")
        sum_lines.append(f"- 遗留普通仓 {len(positions)-len(esb_pos)} 只，浮动盈亏 {norm_pnl:+.2f}")
        sum_lines.append(f"- 二四棒持仓 {len(esb_pos)} 只，浮动盈亏 {esb_pnl:+.2f}")
        sum_lines.append(f"- 今日成交 {n_buy_day} 买 / {n_sell_day} 卖")
        sum_lines.append("## 账户净值")
        sum_lines.append(f"- 今日平仓净盈亏 {sold_pnl_total:+.2f} 元")
        sum_lines.append(f"- 现金 {cash:.2f} / 持仓市值 {mkt_val:.2f} / 总资产 {equity:.2f}")
        sum_lines.append(f"- 本轮费用 {total_fees:.2f} / 累计费用 {cumulative_fees:.2f} 元")
        sum_lines.append(f"- 仓位 {pos_ratio:.1f}% | 累计盈亏 {cum_pnl:+.2f}（{cum_pnl/INITIAL_CASH*100:+.2f}%）| "
                        f"最大回撤 {'数据不足' if len(equity_history) < 2 else f'{max_dd*100:.2f}%'}")
        sum_lines.append(f"- 风控：{'熔断停买' if halt_new_buy else '正常'} 回撤{cur_dd*100:.2f}%/15% "
                        f"| 单票最大{max_single_w*100:.1f}%/20% | 总仓{mkt_val:.0f}元/上限{cap_now/10000:.0f}万")
        sum_lines.append(f"- 净值快照：{len(equity_history)} 天（含今日 {today}，存于 paper_positions.json equity_history）")
        sum_lines.append(_next_day_instructions(positions, mkt))
        sum_lines.append(f"- 完整报告：{out_path.relative_to(workspace)}")

        return AgentResult(True, self.name, "done", "\n".join(sum_lines),
            artifact_paths=[str(out_path.relative_to(workspace))])

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "ready": _AK_AVAILABLE, "mode": self.mode}

    def describe(self) -> dict[str, Any]:
        return {"summary": "虚拟盘·交易系统（二四棒）：全A扫描突破开仓/左峰价限价挂单；左级初始止损+近4根最低移动止损(只上移)离场，无机械止盈/RSI/MA20过滤；遗留普通仓兜底清出",
                "params": {"task": {"type": "string", "description": "留空跑默认"}}}

