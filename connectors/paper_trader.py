"""虚拟盘交易 agent：候选自动买入 + 卖出规则自动平仓。

买入（两类来源）：
  1. 多因子候选（multifactor Top10）→ 当日收盘价买 100 股（普通持仓）
  2. 二四棒信号（趋势池 = quant_all_a Top30，当日突破左峰）→ 按风险定额开仓（strategy=ersibang）
卖出（按持仓 strategy 分流）：
  普通持仓（优先级从高到低）：
    1. 收盘价 < MA20（破位）→ 全部止损卖出
    2. 浮盈 ≥ TAKE_PROFIT_PCT（止盈）→ 全部落袋
    3. RSI > 70（超买）→ 减仓一半，保留剩余仓位吃趋势延续
  ersibang 持仓：
    二四棒离场：有效止损 = max(左级Low, 移动止损=近4根最低·只上移[仅今日High>昨日High更新])
    当日 Low < 有效止损 → 全部卖出；同一左峰不重复开仓（历史含同 peak_idx 跳过）
持仓跟踪：每日更新最新价，算浮盈/浮亏

cron：每天 15:35 自动跑。
"""

from __future__ import annotations

import datetime
import json
import os
import re
from pathlib import Path
from typing import Any

# 止盈阈值：浮盈达到该百分比即全部卖出落袋（本数值由 2026-W42 周复盘 IMP-001 改进项驱动）
TAKE_PROFIT_PCT = 8.0

# 【停买令】experimental 来源（多因子/全A，未过晋升门）候选只输出观察清单，不自动买入。
# 依据：docs/cognition/promotion_gate.md（闸3 前置）+ 用户确认 2026-10-18。
# 解除：环境变量 ANENGOS_HALT_EXPERIMENTAL_BUY=0（待多因子回测过门后由用户拍板解除）。
HALT_EXPERIMENTAL_BUY = os.environ.get("ANENGOS_HALT_EXPERIMENTAL_BUY", "1") != "0"

from connectors.base import AgentAdapter, AgentResult, _now_iso
from connectors.error_log import error_guard
from connectors.quant_ersibang import esb_signal, fetch_kline

try:
    import akshare as ak  # type: ignore
    _AK_AVAILABLE = True
except Exception:  # noqa: BLE001
    ak = None  # type: ignore
    _AK_AVAILABLE = False


def _sina_code(code: str) -> str:
    return f"sh{code}" if code.startswith("6") else f"sz{code}"


def _esb_trailing_stop(highs: list[float], lows: list[float], pos: dict) -> float:
    """二四棒移动止损（只上移）：仅今日High>昨日High 更新 trail=近4根含今日 min Low。"""
    stop = float(pos.get("stop", pos.get("init_stop", pos["buy_price"])))
    if len(highs) >= 2 and highs[-1] > highs[-2]:
        trail = min(lows[-4:]) if len(lows) >= 4 else min(lows)
        if trail > stop:
            stop = trail
    return stop


def _trend_pool(ws) -> list[dict]:
    """读 quant_all_a 趋势池（all_a_output/all_a_*.md Top30 表格行）。"""
    pool: list[dict] = []
    d = ws / "all_a_output"
    if d.exists():
        files = sorted(d.glob("all_a_*.md"), reverse=True)
        if files:
            text = files[0].read_text(encoding="utf-8", errors="replace")
            for line in text.splitlines():
                m = re.match(r"\|\s*\d+\s*\|\s*(\d{6})\s*\|\s*([^|]+?)\s*\|", line)
                if m:
                    pool.append({"code": m.group(1), "name": m.group(2).strip()})
    return pool


def _daily(code: str, days: int = 60):
    """拉日线 DataFrame。"""
    try:
        df = ak.stock_zh_a_daily(symbol=_sina_code(code), start_date="20240601", adjust="qfq")
        if df is None or df.empty:
            return None
        return df.tail(days)
    except Exception:
        return None


def _rsi(closes: list[float], n: int = 14) -> float:
    if len(closes) < n + 1:
        return float("nan")
    gains, losses = [], []
    for i in range(-n, 0):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains) / n
    avg_loss = sum(losses) / n
    if avg_loss == 0:
        return 100.0
    return 100 - (100 / (1 + avg_gain / avg_loss))


def _sma(vals: list[float], n: int) -> float:
    if len(vals) < n:
        return float("nan")
    return sum(vals[-n:]) / n


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
        valid = [d for d in cand if d in trade_dates]
        if valid:
            return max(valid)
        return max(trade_dates)  # 历史候选全非交易日 → 取最新交易日
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


class PaperTraderAdapter(AgentAdapter):
    name = "paper_trader"

    def __init__(self) -> None:
        self.mode = "akshare" if _AK_AVAILABLE else "mock"

    def submit(self, task: str, workspace: str) -> AgentResult:
        ws = Path(workspace)
        out_dir = ws / "paper_trader_output"
        out_dir.mkdir(parents=True, exist_ok=True)
        pos_file = ws / "paper_positions.json"

        # 1. 读已有数据（持仓 + 已卖出历史）
        data = {"positions": [], "history": []}
        if pos_file.exists():
            try:
                loaded = json.loads(pos_file.read_text(encoding="utf-8"))
                if isinstance(loaded, list):  # 兼容旧格式（只有 positions 列表）
                    data["positions"] = loaded
                else:
                    data = loaded
            except Exception:
                pass

        positions = data["positions"]
        history = data["history"]

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
        still_held = []
        missing_names: list[str] = []
        mkt_dates: list[str] = []
        n_ma20 = n_tp = n_rsi = n_esb = 0
        for p in positions:
            df = _daily(p["code"])
            if df is None:
                still_held.append(p)
                missing_names.append(f"{p['name']}({p['code']})")
                continue
            closes = [float(x) for x in df["close"].tolist()]
            last = closes[-1]
            last_date = str(df["date"].iloc[-1])[:10]  # 行情最新交易日（非日历日）
            mkt_dates.append(last_date)
            pnl_pct = (last - p["buy_price"]) / p["buy_price"] * 100

            reason = None
            sell_shares = p["shares"]
            # 存量持仓口径迁移：buy_date 若为未来日期（历史 bug）或日历日，修正为全局市场交易日
            if p.get("buy_date") and str(p["buy_date"]) > mkt:
                p["buy_date"] = mkt
            elif p.get("buy_date") == today and last_date != today:
                p["buy_date"] = mkt
            if p.get("strategy") == "ersibang":
                highs = [float(x) for x in df["high"].tolist()]
                lows = [float(x) for x in df["low"].tolist()]
                new_stop = _esb_trailing_stop(highs, lows, p)
                p["stop"] = new_stop
                if lows[-1] < new_stop:
                    reason = f"二四棒跌破止损({lows[-1]:.2f}<{new_stop:.2f})"
                    n_esb += 1
            else:
                ma20 = _sma(closes, 20)
                rsi = _rsi(closes, 14)
                if last < ma20:
                    # 1) 破位：趋势已坏，全部止损（最高优先级）
                    reason = f"跌破MA20({last:.2f}<{ma20:.2f})"
                    n_ma20 += 1
                elif pnl_pct >= TAKE_PROFIT_PCT:
                    # 2) 止盈：浮盈达标，全部落袋
                    reason = f"止盈(+{pnl_pct:.1f}%≥{TAKE_PROFIT_PCT}%)"
                    n_tp += 1
                elif rsi > 70:
                    # 3) 超买：减仓一半，保留仓位吃趋势
                    reason = f"RSI减仓({rsi:.1f}>70)"
                    n_rsi += 1
                    sell_shares = p["shares"] // 2
                    if sell_shares < 1:
                        sell_shares = p["shares"]

            if reason:
                # 卖出（全卖或减仓）
                pnl = (last - p["buy_price"]) * sell_shares
                sale = {
                    **p,
                    "sell_date": mkt,  # 统一全局市场交易日（杜绝 buy>sell 倒挂）
                    "sell_price": last,
                    "sell_reason": reason,
                    "shares": sell_shares,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "partial": sell_shares < p["shares"],
                }
                history.append(sale)
                sold_codes.add(p["code"])
                sold_pnl_total += pnl
                cash += last * sell_shares  # 卖出回笼现金
                if sell_shares < p["shares"]:
                    # 减仓：剩余仓位继续持有，更新现价与浮盈亏
                    p["shares"] -= sell_shares
                    p["current_price"] = last
                    p["pnl"] = (last - p["buy_price"]) * p["shares"]
                    p["pnl_pct"] = pnl_pct
                    still_held.append(p)
                sold_today.append(f"{p['name']}({p['code']}) {reason} 盈亏{pnl:+.2f}元")
            else:
                # 继续持有，更新现价
                p["current_price"] = last
                p["pnl"] = (last - p["buy_price"]) * p["shares"]
                p["pnl_pct"] = (last - p["buy_price"]) / p["buy_price"] * 100
                still_held.append(p)

        positions = still_held

        # 3a. 普通买入：读最新多因子候选（Top 10）
        mf_dir = ws / "multifactor_output"
        candidates = []
        if mf_dir.exists():
            files = sorted(mf_dir.glob("multifactor_*.md"), reverse=True)
            if files:
                text = files[0].read_text(encoding="utf-8")
                for line in text.splitlines():
                    # 多因子格式：| 1 | 600919 | 江苏银行 | ...
                    m = re.match(r"\|\s*\d+\s*\|\s*(\d{6})\s*\|\s*([^|]+?)\s*\|", line)
                    if m:
                        candidates.append({"code": m.group(1), "name": m.group(2).strip()})
                candidates = candidates[:10]  # 只买 Top 10

        held_codes = {p["code"] for p in positions}
        used_esb_peaks = {(h["code"], h.get("esb_peak_idx")) for h in history
                          if h.get("esb_peak_idx") is not None}
        bought_today = []
        watchlist = []  # 停买令观察清单（experimental 候选，未过晋升门）
        new_exec_dates: list[str] = []
        cand_total = len(candidates)
        esb_total = 0
        esb_hit_total = 0
        for c in candidates:
            if c["code"] in held_codes or c["code"] in sold_codes:  # 已持仓/今日已卖出的不重复建仓
                continue
            if HALT_EXPERIMENTAL_BUY:
                # 【停买令】多因子/全A 候选未过晋升门 → 仅观察，不买入（等用户解除）
                watchlist.append(f"{c['name']}({c['code']})")
                continue
            # 错误日志拦截（自我进化：该票曾有未修复的系统 bug / 禁止回补类教训 → 跳过）
            guard = error_guard(str(ws), c["code"])
            if guard:
                bought_today.append(f"{c['name']}({c['code']}) ⛔{guard}")
                continue
            df = _daily(c["code"])
            if df is None or df.empty:
                continue
            last_date = str(df["date"].iloc[-1])[:10]  # 仅用于价格与行情新鲜度判断
            # 无新行情（周末/节假日重复运行）：跳过买入，防止重复补买
            if last_exec_date and mkt == last_exec_date:
                continue
            closes = [float(x) for x in df["close"].tolist()]
            ma20 = _sma(closes, 20)
            price = float(df["close"].iloc[-1])
            # 买入质量门槛：现价必须站上 MA20（与多因子候选口径一致，防假信号）
            if price < ma20:
                continue
            new_exec_dates.append(mkt)
            shares = 100
            cost = price * shares
            if cost > cash:  # 现金不足跳过
                continue
            cash -= cost
            positions.append({
                "code": c["code"], "name": c["name"],
                "buy_date": mkt,  # 统一全局市场交易日
                "buy_price": price,
                "shares": shares, "current_price": price,
                "pnl": 0.0, "pnl_pct": 0.0,
            })
            bought_today.append(f"{c['name']}({c['code']}) @ {price:.2f}")

        # 3b. 二四棒信号买入：趋势池（quant_all_a Top30）中当日突破左峰的票
        esb_bought = []
        for c in _trend_pool(ws):
            esb_total += 1
            if c["code"] in held_codes or c["code"] in sold_codes:
                continue
            try:
                rows = fetch_kline(_sina_code(c["code"]), datalen=300)
                if not rows:
                    continue
                dates = [r["day"] for r in rows]
                # 截断到 today：sina 源可能含未来/非交易日 bar，剔除避免信号与日期失真
                _valid = [i for i, d in enumerate(dates) if str(d)[:10] <= today]
                if not _valid:
                    continue
                dates = dates[: _valid[-1] + 1]
                o = [float(r["open"]) for r in rows][: len(dates)]
                h = [float(r["high"]) for r in rows][: len(dates)]
                lo = [float(r["low"]) for r in rows][: len(dates)]
                cl = [float(r["close"]) for r in rows][: len(dates)]
                sig = esb_signal(dates, o, h, lo, cl)
            except Exception:
                continue
            if sig is None:
                continue
            esb_hit_total += 1
            # 同一左峰不敲第二次（历史已用过的 peak 跳过）
            if (c["code"], sig["peak_idx"]) in used_esb_peaks:
                continue
            last_date = dates[-1]
            # 无新行情（周末/节假日重复运行）：跳过买入
            if last_exec_date and mkt == last_exec_date:
                continue
            new_exec_dates.append(mkt)
            price = cl[-1]
            cost = price * sig["shares"]
            if cost > cash:  # 现金不足跳过
                continue
            cash -= cost
            positions.append({
                "code": c["code"], "name": c["name"],
                "strategy": "ersibang",
                "buy_date": mkt,  # 统一全局市场交易日（sina dates[-1] 可能含未来日期）
                "buy_price": price,
                "esb_entry_px": sig["entry_px"], "init_stop": sig["stop_px"],
                "stop": sig["stop_px"], "esb_peak_idx": sig["peak_idx"],
                "esb_level_idx": sig["level_idx"],
                "shares": sig["shares"], "current_price": price,
                "pnl": 0.0, "pnl_pct": 0.0,
            })
            used_esb_peaks.add((c["code"], sig["peak_idx"]))
            esb_bought.append(f"{c['name']}({c['code']}) 突破左峰{sig['entry_px']:.2f} "
                              f"买{sig['shares']}股 @ {price:.2f}")

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
        data["equity_history"] = equity_history[-120:]  # 保留近 120 个交易日
        data["positions"] = positions
        data["history"] = history
        pos_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

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

        lines = [
            f"# 虚拟盘组合（{today}，行情截至最近交易日）",
            f"- 持仓：{len(positions)} 只（二四棒 {len(esb_pos)}） | 历史交易：{len(history)} 笔",
            f"- 今日买入：多因子 {len(bought_today)} | 二四棒 {len(esb_bought)} | 今日卖出：{len(sold_today)}",
            f"- 持仓成本：{total_cost:.2f} 元 | 市值：{total_value:.2f} 元",
            f"- 浮动盈亏：{total_pnl:+.2f} 元（{total_pnl/total_cost*100 if total_cost else 0:+.2f}%）",
            f"- 二四棒仓位：成本 {esb_cost:.2f} 元 | 市值 {esb_value:.2f} 元"
            f" | 浮盈 {esb_value-esb_cost:+.2f} 元" if esb_pos else "",
            "",
            "## 运行说明",
            f"- 本次运行日期：{today} | 行情最新交易日：{mkt}",
            f"- 多因子候选 {cand_total} 只 → 过滤（已持仓/今日已卖/破MA20/现金不足）后买入 {len(bought_today)}",
            f"- 二四棒趋势池 {esb_total} 只 → 信号命中 {esb_hit_total} → 过滤后买入 {len(esb_bought)}",
            f"- 无新行情或周末运行时自动跳过买入（last_exec_date 防重复补买）；现价为最新交易日收盘价",
            f"- 缺失行情标的：{', '.join(missing_names) if missing_names else '无'}",
            f"- 【停买令】experimental 候选（多因子/全A 未过晋升门）→ 仅观察 {len(watchlist)} 只，未自动买入"
            f"（依据 promotion_gate.md；解除需 ANENGOS_HALT_EXPERIMENTAL_BUY=0 且你确认）",
            "",
            "## 规则执行清单",
            f"- 普通持仓：跌破MA20止损 {n_ma20} 次 | 止盈≥8% {n_tp} 次 | RSI>70减仓 {n_rsi} 次",
            f"- 二四棒持仓：移动止损离场 {n_esb} 次 | 今日总卖出 {len(sold_today)}",
            "",
            "## 账户净值",
            f"- 可用现金：{cash:.2f} 元 | 持仓市值：{mkt_val:.2f} 元 | 总资产：{equity:.2f} 元",
            f"- 今日平仓盈亏：{sold_pnl_total:+.2f} 元 | 累计盈亏：{cum_pnl:+.2f} 元（{cum_pnl/INITIAL_CASH*100:+.2f}%）",
            f"- 仓位比例：{pos_ratio:.1f}% | 历史最大回撤：{'数据不足' if len(equity_history) < 2 else f'{max_dd*100:.2f}%'} | 净值记录：{len(equity_history)} 天",
            f"- 初始资金：{INITIAL_CASH:.0f} 元（虚拟盘口径，未计交易费用）",
            "",
        ]
        if bought_today:
            lines.append("## 今日买入（多因子）")
            for b in bought_today:
                lines.append(f"- {b}")
            lines.append("")
        if watchlist:
            lines.append("## 观察清单（停买令生效，未买入）")
            for w in watchlist[:15]:
                lines.append(f"- {w}")
            if len(watchlist) > 15:
                lines.append(f"- …共 {len(watchlist)} 只")
            lines.append("")
        if esb_bought:
            lines.append("## 今日买入（二四棒突破）")
            for b in esb_bought:
                lines.append(f"- {b}")
            lines.append("")
        if sold_today:
            lines.append("## 今日卖出")
            for s in sold_today:
                lines.append(f"- {s}")
            lines.append("")

        lines += [
            "## 当前持仓",
            "| 代码 | 名称 | 策略 | 买入日 | 买价 | 现价 | 数量 | 浮动盈亏 | 盈亏% |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for p in positions:
            lines.append(
                f"| {p['code']} | {p['name']} | {p.get('strategy', '普通')} | {p['buy_date']} "
                f"| {p['buy_price']:.2f} | {p['current_price']:.2f} | {p['shares']} "
                f"| {p['pnl']:+.2f} | {p['pnl_pct']:+.2f}% |"
            )

        # ---- 次日待执行指令清单（下个交易日开盘后需检查项）----
        def _next_day_instructions(poss, market_day):
            inst = [f"- 下一交易日（{market_day} 后首个交易日）检查清单："]
            for p in poss:
                if p.get("strategy") == "ersibang":
                    inst.append(f"  - {p['name']}({p['code']})：现价 vs 有效止损 {p.get('stop', 0):.2f}"
                                f"（跌破即全部卖出；仅当今日High>昨日High时上移止损）")
                else:
                    inst.append(f"  - {p['name']}({p['code']})：现价 vs MA20 / 止盈价"
                                f"（{(p['buy_price'] * (1 + TAKE_PROFIT_PCT / 100)):.2f}）"
                                f" / RSI(14)")
            inst.append("  - 买入侧：等待多因子候选刷新与二四棒新突破信号（仅在新交易日执行）")
            return "\n".join(inst)

        lines += ["", "## 次日待执行指令清单",
                  _next_day_instructions(positions, mkt), ""]

        out_path = out_dir / f"portfolio_{today.replace('-', '')}.md"
        out_path.write_text("\n".join(lines), encoding="utf-8")

        # ---- 结构化摘要（供互审监督完整核验）----
        sum_lines = [f"[{self.name}/akshare] 持仓{len(positions)}只（二四棒{len(esb_pos)}），"
                     f"今日买{len(bought_today)+len(esb_bought)}卖{len(sold_today)}"]
        sum_lines.append(f"## 运行说明")
        is_trading_day = mkt == today
        sum_lines.append(f"- 本次运行 {today}{'（非交易日）' if not is_trading_day else ''} | "
                         f"行情最新交易日 {mkt} | 现价=最新交易日收盘价")
        sum_lines.append(f"- 卖出检查：已执行（本日触发 破MA20 {n_ma20} / 止盈 {n_tp} / RSI减仓 {n_rsi} "
                         f"/ 二四棒离场 {n_esb}）")
        sum_lines.append(f"- 买入{'已跳过（无新行情）' if not is_trading_day else f'执行：多因子候选 {cand_total}→买 {len(bought_today)} | 二四棒池 {esb_total}→命中 {esb_hit_total}→买 {len(esb_bought)}'}")
        sum_lines.append(f"- 缺失行情标的：{', '.join(missing_names) if missing_names else '无'}")
        sum_lines.append(f"## 规则执行清单")
        sum_lines.append(f"- 破MA20 {n_ma20} | 止盈 {n_tp} | RSI减仓 {n_rsi} | 二四棒止损离场 {n_esb} "
                         f"| 缺失行情 {'有' if missing_names else '无'}")
        if bought_today:
            sum_lines.append("## 今日买入（多因子）")
            sum_lines += [f"- {b}" for b in bought_today]
        if esb_bought:
            sum_lines.append("## 今日买入（二四棒突破）")
            sum_lines += [f"- {b}" for b in esb_bought]
        if sold_today:
            sum_lines.append("## 今日卖出")
            sum_lines += [f"- {s}" for s in sold_today]
        # 二四棒持仓明细（监督重点）
        if esb_pos:
            sum_lines.append("## 二四棒当前持仓")
            sum_lines.append("| 代码 | 名称 | 买入日 | 买价 | 现价 | 数量 | 浮动盈亏 | 盈亏% | 止损 |")
            sum_lines.append("|---|---|---|---|---|---|---|---|---|")
            for p in esb_pos:
                sum_lines.append(
                    f"| {p['code']} | {p['name']} | {p['buy_date']} | {p['buy_price']:.2f} "
                    f"| {p['current_price']:.2f} | {p['shares']} | {p['pnl']:+.2f} "
                    f"| {p['pnl_pct']:+.2f}% | {p.get('stop', 0):.2f} |")
        # 普通持仓明细（成本/市值/权重）
        norm_pos = [p for p in positions if p.get("strategy") != "ersibang"]
        if norm_pos:
            sum_lines.append("## 普通持仓明细")
            sum_lines.append("| 代码 | 名称 | 买入日 | 买价 | 现价 | 数量 | 成本 | 市值 | 权重% | 盈亏% |")
            sum_lines.append("|---|---|---|---|---|---|---|---|---|---|")
            for p in norm_pos:
                cost_v = p["buy_price"] * p["shares"]
                val_v = p["current_price"] * p["shares"]
                wt = val_v / mkt_val * 100 if mkt_val > 0 else 0.0
                sum_lines.append(
                    f"| {p['code']} | {p['name']} | {p['buy_date']} | {p['buy_price']:.2f} "
                    f"| {p['current_price']:.2f} | {p['shares']} | {cost_v:.0f} | {val_v:.0f} "
                    f"| {wt:.1f}% | {p['pnl_pct']:+.2f}% |")
        # 分组统计
        esb_pnl = sum(p.get("pnl", 0.0) for p in esb_pos)
        norm_pnl = sum(p.get("pnl", 0.0) for p in positions if p.get("strategy") != "ersibang")
        sum_lines.append("## 分组统计")
        sum_lines.append(f"- 普通持仓 {len(positions)-len(esb_pos)} 只，浮动盈亏 {norm_pnl:+.2f}")
        sum_lines.append(f"- 二四棒持仓 {len(esb_pos)} 只，浮动盈亏 {esb_pnl:+.2f}")
        sum_lines.append(f"- 今日成交 {len(bought_today)+len(esb_bought)} 买 / {len(sold_today)} 卖")
        sum_lines.append("## 账户净值")
        sum_lines.append(f"- 今日平仓盈亏 {sold_pnl_total:+.2f} 元")
        sum_lines.append(f"- 现金 {cash:.2f} / 持仓市值 {mkt_val:.2f} / 总资产 {equity:.2f}")
        sum_lines.append(f"- 仓位 {pos_ratio:.1f}% | 累计盈亏 {cum_pnl:+.2f}（{cum_pnl/INITIAL_CASH*100:+.2f}%）| "
                        f"最大回撤 {'数据不足' if len(equity_history) < 2 else f'{max_dd*100:.2f}%'}")
        sum_lines.append(f"- 净值快照：{len(equity_history)} 天（含今日 {today}，存于 paper_positions.json equity_history）")
        sum_lines.append(_next_day_instructions(positions, mkt))
        sum_lines.append(f"- 完整报告：{out_path.relative_to(workspace)}")

        return AgentResult(True, self.name, "done", "\n".join(sum_lines),
            artifact_paths=[str(out_path.relative_to(workspace))])

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "ready": _AK_AVAILABLE, "mode": self.mode}

    def describe(self) -> dict[str, Any]:
        return {"summary": "虚拟盘：多因子+二四棒双源买入；普通持仓(破MA20/止盈/RSI减仓)与二四棒持仓(移动止损离场)分流管理",
                "params": {"task": {"type": "string", "description": "留空跑默认"}}}
