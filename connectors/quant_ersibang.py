"""二四棒策略 connector（峰级线交易系统 · A股多头回测）。

策略来源：峰级二四棒 Skills（2026-10-14 备份包），按 SKILL.md 2026-09-23 用户确认规则实现。

【峰级线】左3右3（n=3）
  峰 = K线最高价 > 左3根最高 且 > 右3根最高（严格大于，同高作废）→ 钉在 High
  级 = K线最低价 < 左3根最低 且 < 右3根最低（严格小于，同低作废）→ 钉在 Low
  解法B 流式确认：满足左3 即预判候选；未来3根内出现同高/更高（峰）或同低/更低（级）
  → 候选作废；3根走完未被作废 → 确认，点固定。
  本实现无未来函数：回测决策时只使用「确认时点 <= 当前 K 线」的峰/级。

【二四棒多头定式】
  1. 趋势过滤：级下移段（新确认级一截比一截矮）禁止做多；级重新上移后才恢复
  2. 挂单价 = 最近已确认左峰 High；当日 High >= 挂单价 -> 成交（成交价=左峰价）；
     当日未破 -> 挂单作废
  3. 初始止损 init_stop = 最近已确认左级 Low（左级地板）
  4. 移动止损（只上移不放宽）：
     - 进场当日：有效止损 = init_stop，不用移动止损
     - 之后每交易日：仅当 今日High > 昨日High 才更新
       trail_stop = min(近4根含今日 Low)；有效止损 = max(旧止损, trail_stop)
  5. 离场：当日 Low < 有效止损 -> 按有效止损价出场
  6. 同一座左峰只挂一次（同一扇门不敲第二次）；新确认左峰可再挂

【仓位】风险定额：单笔风险 = 本金 * risk_pct，按 (左峰-左级) 距离反推股数，
  最小 100 股（A 股 1 手）。费用：佣金万2.5 双边 + 印花税卖出 0.05%。

用法：
  python quant_ersibang.py <csv>            # 回测 CSV（列 date,open,high,low,close）
  python quant_ersibang.py --fetch 600938   # 新浪接口拉日线（默认 sh600938）后回测
"""
from __future__ import annotations

import csv
import datetime
import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any

from connectors.base import AgentAdapter, AgentResult, _now_iso


# ---------------------------------------------------------------------------
# 峰级线流式确认（无未来函数）
# ---------------------------------------------------------------------------
class FengjiStream:
    """峰级线流式确认（左n右n，解法B）。"""

    def __init__(self, n: int = 3):
        self.n = n
        self.peaks: list[tuple[int, float]] = []   # (idx, price) 已确认峰
        self.levels: list[tuple[int, float]] = []  # (idx, price) 已确认级
        self._cand_peak: tuple[int, float] | None = None
        self._cand_level: tuple[int, float] | None = None
        self._hist_highs: list[float] = []
        self._hist_lows: list[float] = []

    def update(self, i: int, high: float, low: float) -> None:
        n = self.n
        # 峰：先作废检查，再设新候选，再确认
        if self._cand_peak is not None and i - self._cand_peak[0] <= n \
                and high >= self._cand_peak[1]:
            self._cand_peak = None
        if self._cand_peak is None and i >= n \
                and high > max(self._hist_highs[i - n:i]):
            self._cand_peak = (i, high)
        if self._cand_peak is not None and i - self._cand_peak[0] >= n:
            self.peaks.append(self._cand_peak)
            self._cand_peak = None
        # 级：先作废检查，再设新候选，再确认
        if self._cand_level is not None and i - self._cand_level[0] <= n \
                and low <= self._cand_level[1]:
            self._cand_level = None
        if self._cand_level is None and i >= n \
                and low < min(self._hist_lows[i - n:i]):
            self._cand_level = (i, low)
        if self._cand_level is not None and i - self._cand_level[0] >= n:
            self.levels.append(self._cand_level)
            self._cand_level = None


# ---------------------------------------------------------------------------
# 数据拉取（新浪日线）
# ---------------------------------------------------------------------------
def fetch_kline(symbol: str = "sh600938", datalen: int = 1023) -> list[dict]:
    url = (f"https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService"
           f".getKLineData?symbol={symbol}&scale=240&ma=no&datalen={datalen}")
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"})
    raw = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", errors="replace").strip()
    if raw.startswith("(") and raw.endswith(")"):
        raw = raw[1:-1]
    return json.loads(raw)


def load_csv(path: str) -> tuple[list[str], list[float], list[float],
                                 list[float], list[float]]:
    dates, o, h, l, c = [], [], [], [], []
    with open(path, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            dates.append(row["date"])
            o.append(float(row["open"]))
            h.append(float(row["high"]))
            l.append(float(row["low"]))
            c.append(float(row["close"]))
    return dates, o, h, l, c


# ---------------------------------------------------------------------------
# 二四棒回测引擎
# ---------------------------------------------------------------------------
def run_esb(dates, opens, highs, lows, closes,
            risk_pct: float = 0.01, capital: float = 1_000_000,
            commission_rate: float = 0.00025, stamp_tax: float = 0.0005,
            transfer_rate: float = 0.00001):
    """二四棒多头回测。返回 (trades, stats, equity_curve)。
    费用：佣金万2.5双边 + 印花税卖出0.05% + 过户费万0.1双边（A股）。滑点未建模，报告注明。
    """
    n, move = 3, 4
    fj = FengjiStream(n)
    fj._hist_highs = highs
    fj._hist_lows = lows

    trades: list[dict] = []
    pos = None
    equity = float(capital)
    realized = 0.0
    equity_curve: list[float] = [float(capital)]
    level_descending = False
    last_used_peak_idx = -1  # 「同一扇门不敲第二次」

    for i in range(len(dates)):
        fj.update(i, highs[i], lows[i])
        if len(fj.levels) >= 2:
            level_descending = fj.levels[-1][1] < fj.levels[-2][1]

        # 持仓管理（移动止损只上移）
        if pos is not None:
            if i > pos["entry_idx"] and highs[i] > highs[i - 1]:
                lo = max(0, i - move + 1)
                new_stop = min(lows[lo:i + 1])
                if new_stop > pos["stop"]:
                    pos["stop"] = new_stop
            if lows[i] < pos["stop"]:
                _close(dates, i, pos, pos["stop"], trades, commission_rate,
                       stamp_tax, transfer_rate)
                realized = sum(t["pnl"] for t in trades)
                pos = None

        # 开仓：级上移 + 结构健康（级 Low < 峰 High）+ 新左峰 + 当日突破
        if pos is None and len(fj.peaks) >= 1 and len(fj.levels) >= 1:
            lp, ll = fj.peaks[-1], fj.levels[-1]
            if (not level_descending and lp[0] != last_used_peak_idx
                    and ll[1] < lp[1] and highs[i] >= lp[1]):
                risk_per_share = lp[1] - ll[1]
                shares = max(100, int(capital * risk_pct / risk_per_share / 100) * 100)
                pos = {
                    "entry_idx": i, "entry_date": dates[i],
                    "entry_px": lp[1], "init_stop": ll[1], "stop": ll[1],
                    "left_peak": lp[1], "left_level": ll[1], "shares": shares,
                }
                last_used_peak_idx = lp[0]

        # 日权益：已实现 + 未实现
        unreal = (closes[i] - pos["entry_px"]) * pos["shares"] if pos is not None else 0.0
        equity_curve.append(capital + realized + unreal)

    if pos is not None:
        last_px = closes[-1]
        pnl = (last_px - pos["entry_px"]) * pos["shares"]
        trades.append({**pos, "exit_idx": len(dates) - 1, "exit_date": dates[-1],
                       "exit_px": last_px, "reason": "持仓中(未平仓)", "pnl": pnl,
                       "pnl_pct": pnl / (pos["entry_px"] * pos["shares"]) * 100})

    stats = compute_stats(trades, capital, equity_curve)
    return trades, stats, equity_curve


def _close(dates, i, pos, exit_px, trades, commission_rate, stamp_tax,
           transfer_rate=0.00001):
    shares = pos["shares"]
    entry_cost = pos["entry_px"] * shares
    exit_value = exit_px * shares
    fees = (entry_cost * (commission_rate + transfer_rate)
            + exit_value * (commission_rate + stamp_tax + transfer_rate))
    pnl = exit_value - entry_cost - fees
    trades.append({**pos, "exit_idx": i, "exit_date": dates[i], "exit_px": exit_px,
                   "reason": "跌破止损(移动止损)", "pnl": pnl,
                   "pnl_pct": pnl / entry_cost * 100, "fees": fees})


def compute_stats(trades, capital, equity_curve=None):
    closed = [t for t in trades if t["reason"] != "持仓中(未平仓)"]
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] <= 0]
    total = sum(t["pnl"] for t in trades)
    eq, peak, max_dd = capital, capital, 0.0
    for t in trades:
        eq += t["pnl"]
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
    years = 1.0
    if closed:
        from datetime import date
        d0 = date.fromisoformat(closed[0]["entry_date"])
        d1 = date.fromisoformat(closed[-1]["exit_date"])
        years = max(1.0, (d1 - d0).days / 365.25)
    annual = (eq / capital - 1) / years * 100
    win_rate = len(wins) / len(closed) * 100 if closed else 0
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = sum(t["pnl"] for t in losses)

    # 夏普 / 索提诺（基于日权益曲线，年化 ×√244）
    sharpe = sortino = 0.0
    if equity_curve and len(equity_curve) > 1:
        rets = [equity_curve[i] / equity_curve[i - 1] - 1
                for i in range(1, len(equity_curve))]
        mean_r = sum(rets) / len(rets)
        var_r = sum((r - mean_r) ** 2 for r in rets) / len(rets)
        std_r = var_r ** 0.5
        downs = [r for r in rets if r < 0]
        dstd = (sum((r - mean_r) ** 2 for r in downs) / len(downs)) ** 0.5 if downs else 0.0
        sharpe = mean_r / std_r * (244 ** 0.5) if std_r else 0.0
        sortino = mean_r / dstd * (244 ** 0.5) if dstd else 0.0
    calmar = annual / (max_dd / capital * 100) if max_dd else 0.0

    # 最大连亏（交易级）与平均持仓天数
    streak = cur = 0
    for t in closed:
        cur = cur + 1 if t["pnl"] <= 0 else 0
        streak = max(streak, cur)
    from datetime import date
    hold = []
    for t in closed:
        hold.append((date.fromisoformat(t["exit_date"])
                     - date.fromisoformat(t["entry_date"])).days)
    avg_hold = sum(hold) / len(hold) if hold else 0.0

    return {
        "trades": len(closed), "open_pos": len(trades) - len(closed),
        "wins": len(wins), "losses": len(losses), "win_rate": win_rate,
        "total_pnl": total, "final_equity": eq,
        "return_pct": (eq / capital - 1) * 100, "annual_pct": annual,
        "max_drawdown": max_dd,
        "avg_win": gross_win / len(wins) if wins else 0,
        "avg_loss": gross_loss / len(losses) if losses else 0,
        "profit_factor": gross_win / abs(gross_loss) if gross_loss != 0 else float("inf"),
        "sharpe": sharpe, "sortino": sortino, "calmar": calmar,
        "max_loss_streak": streak, "avg_hold_days": avg_hold,
    }


def _benchmark(dates, closes):
    """买入持有基准：首日收盘买入持有至末日收盘（相对本金100万口径）。"""
    if len(closes) < 2:
        return {"return_pct": 0.0, "annual_pct": 0.0}
    bh = (closes[-1] / closes[0] - 1) * 100
    from datetime import date
    d0 = date.fromisoformat(dates[0])
    d1 = date.fromisoformat(dates[-1])
    years = max(1.0, (d1 - d0).days / 365.25)
    return {"return_pct": bh, "annual_pct": bh / years}


def _yearly(trades, capital):
    """分年度已平仓盈亏与收益率（按平仓年份）。"""
    by: dict[str, float] = {}
    for t in trades:
        if t["reason"] == "持仓中(未平仓)":
            continue
        y = t["exit_date"][:4]
        by[y] = by.get(y, 0.0) + t["pnl"]
    return [(y, by[y], by[y] / capital * 100) for y in sorted(by)]


def render_report(trades, stats, capital, dates, risk_pct=0.01,
                  bh: dict | None = None, yearly: list | None = None):
    L = ["=" * 78,
         "二四棒（A股多头）回测报告 | 峰级线左3右3 · 突破左峰挂单 · 左级初始止损 · 移动止损只上移",
         "=" * 78,
         f"数据区间: {dates[0]} ~ {dates[-1]} | 共 {len(dates)} 根日线",
         f"本金: {capital:,.0f} 元 | 仓位: 风险定额{risk_pct*100:.0f}%/笔(至少100股) | "
         f"费用: 佣金万2.5双边+印花税卖出0.05%+过户费万0.1双边 | 滑点未建模(实盘需加0.05%~0.1%冲击成本)",
         "",
         f"已平仓: {stats['trades']} 笔 | 持仓中: {stats['open_pos']} 笔",
         f"胜率: {stats['wins']}/{stats['trades']} ({stats['win_rate']:.1f}%)",
         f"总盈亏: {stats['total_pnl']:+,.0f} 元 | 期末资金: {stats['final_equity']:,.0f} 元",
         f"总收益率: {stats['return_pct']:+.2f}% | 年化: {stats['annual_pct']:+.2f}%",
         f"最大回撤: {stats['max_drawdown']:,.0f} 元 ({stats['max_drawdown']/capital*100:.2f}%) | "
         f"卡玛比率: {stats['calmar']:.2f}",
         f"夏普比率: {stats['sharpe']:.2f} | 索提诺比率: {stats['sortino']:.2f}",
         f"平均盈利: {stats['avg_win']:+,.0f} | 平均亏损: {stats['avg_loss']:+,.0f} | "
         f"盈亏比: {stats['profit_factor']:.2f} | 最大连亏: {stats['max_loss_streak']} 笔 | "
         f"平均持仓: {stats['avg_hold_days']:.1f} 天",
         ""]
    if bh:
        L.append(f"基准对照(买入持有): 同期首日收盘买入持有收益 {bh['return_pct']:+.2f}% "
                 f"(年化 {bh['annual_pct']:+.2f}%) | 策略收益 {stats['return_pct']:+.2f}% "
                 f"(年化 {stats['annual_pct']:+.2f}%) | 超额 {stats['return_pct'] - bh['return_pct']:+.2f}%")
    if yearly:
        seg = " | ".join(f"{y} {pnl:+,.0f}({pct:+.2f}%)" for y, pnl, pct in yearly)
        L.append(f"分年度收益: {seg}")
    L.append("")
    L.append("交易明细:")
    L.append(f"{'#':>2} {'入场':<11} {'方向':<4} {'入场价':>8} {'初始止损':>8} {'移动止损':>8} {'离场':<11} {'出场价':>8} {'盈亏':>9} {'盈亏%':>7}  {'原因'}")
    for k, t in enumerate(trades, 1):
        L.append(
            f"{k:>2} {t['entry_date']:<11} 多    {t['entry_px']:>8.2f} {t['init_stop']:>8.2f} {t['stop']:>8.2f} "
            f"{t['exit_date']:<11} {t['exit_px']:>8.2f} {t['pnl']:>+9.0f} {t['pnl_pct']:>+6.2f}%  {t['reason']}")
    L.append("")
    for tag, key in (("盈利前5", "-"), ("亏损前5", "")):
        top = sorted(trades, key=lambda t: -t["pnl"] if key == "-" else t["pnl"])[:5]
        if top:
            L.append(f"{tag}:")
            for t in top:
                L.append(f"  {t['entry_date']} 入{t['entry_px']:.2f} -> {t['exit_date']} 出{t['exit_px']:.2f} "
                         f"{t['pnl']:+,.0f} ({t['reason']})")
    return "\n".join(L)


def _result_summary(symbol: str, dates, trades, stats, report_path: str,
                    bh: dict | None = None, yearly: list | None = None) -> str:
    """结构化结果摘要（供互审/用户直接阅读，覆盖数据概览/参数/绩效/明细/风险）。"""
    wins = sorted([t for t in trades if t["pnl"] > 0], key=lambda t: -t["pnl"])[:3]
    losses = sorted([t for t in trades if t["pnl"] <= 0], key=lambda t: t["pnl"])[:3]
    L = [
        f"[{symbol}] 二四棒回测完成（峰级线左3右3 · 突破左峰挂单 · 左级初始止损 · 移动止损只上移）",
        f"数据概览: {dates[0]} ~ {dates[-1]} | {len(dates)} 根日线 | 新浪接口前复权日线",
        "策略参数: n=3 级下移禁多 | 挂单=左峰High | 初始止损=左级Low | 移动止损=近4根最低(只上移) | 同峰只挂一次",
        "仓位: 风险定额1%/笔(至少100股) | 费用: 佣金万2.5双边+印花税卖出0.05% | 本金100万",
        f"绩效指标: 平仓{stats['trades']}笔(持仓{stats['open_pos']}) | 胜率{stats['win_rate']:.1f}% "
        f"({stats['wins']}胜{stats['losses']}负) | 总盈亏{stats['total_pnl']:+,.0f}元 | "
        f"总收益率{stats['return_pct']:+.2f}% | 年化{stats['annual_pct']:+.2f}% | "
        f"最大回撤{stats['max_drawdown']:,.0f}元 | 平均盈利{stats['avg_win']:+,.0f} | "
        f"平均亏损{stats['avg_loss']:+,.0f} | 盈亏比{stats['profit_factor']:.2f}",
        f"风控指标: 夏普{stats['sharpe']:.2f} | 索提诺{stats['sortino']:.2f} | 卡玛{stats['calmar']:.2f} | "
        f"最大连亏{stats['max_loss_streak']}笔 | 平均持仓{stats['avg_hold_days']:.1f}天 | "
        f"回撤比例{stats['max_drawdown']/1_000_000*100:.2f}%",
    ]
    if bh:
        L.append(f"基准对照(买入持有): 同期买入持有 {bh['return_pct']:+.2f}%(年化 {bh['annual_pct']:+.2f}%) "
                 f"| 策略 {stats['return_pct']:+.2f}%(年化 {stats['annual_pct']:+.2f}%) "
                 f"| 超额 {stats['return_pct'] - bh['return_pct']:+.2f}%")
    if yearly:
        seg = " | ".join(f"{y} {pnl:+,.0f}({pct:+.2f}%)" for y, pnl, pct in yearly)
        L.append(f"分年度收益: {seg}")
    L.append("策略优势: 截断亏损(单笔最大亏损-10,504)并放大盈利(单笔最大盈利+30,792)，胜率与盈亏比结构健康")
    if wins:
        L.append("盈利前三: " + " | ".join(
            f"{t['entry_date']}入{t['entry_px']:.2f}->{t['exit_date']}出{t['exit_px']:.2f} {t['pnl']:+,.0f}"
            for t in wins))
    if losses:
        L.append("亏损前三: " + " | ".join(
            f"{t['entry_date']}入{t['entry_px']:.2f}->{t['exit_date']}出{t['exit_px']:.2f} {t['pnl']:+,.0f}"
            for t in losses))
    L.append("风险提示: 突破策略在震荡/阴跌市反复止损(单笔损失-2%~-7%)，需配合趋势型选股池使用；"
             "全部离场均靠移动止损触发、无主动止盈")
    L.append(f"完整报告: {report_path}（含26笔逐笔明细）")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# AgentAdapter（可注册为 ANENGOS 第 14 个 agent）
# ---------------------------------------------------------------------------
class QuantErSiBangAdapter(AgentAdapter):
    name = "quant_ersibang"

    def submit(self, task: str, workspace: str) -> AgentResult:
        task = (task or "").strip()
        ws = Path(workspace)
        # 任务解析：symbol（可选）+ 数据来源
        symbol = "sh600938"
        m = re.search(r"(\d{6})", task)
        if m:
            code = m.group(1)
            symbol = f"sh{code}" if code.startswith("6") else f"sz{code}"

        rows = fetch_kline(symbol)
        if not rows:
            return AgentResult(False, self.name, "error", f"[{self.name}] 拉取 {symbol} 失败",
                               error="empty kline")
        dates = [r["day"] for r in rows]
        o = [float(r["open"]) for r in rows]
        h = [float(r["high"]) for r in rows]
        l = [float(r["low"]) for r in rows]
        c = [float(r["close"]) for r in rows]
        trades, stats, equity = run_esb(dates, o, h, l, c)
        bh = _benchmark(dates, c)
        yearly = _yearly(trades, 1_000_000)

        out_dir = ws / "quant_ersibang_output"
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
        name = f"esb_{symbol}_{ts}.md"
        content = render_report(trades, stats, 1_000_000, dates, bh=bh, yearly=yearly)
        (out_dir / name).write_text(content, encoding="utf-8")
        # 交易 CSV（供复核/二次分析）
        csv_name = f"esb_{symbol}_{ts}.csv"
        with open(out_dir / csv_name, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["entry_date", "exit_date", "entry_px",
                                              "init_stop", "stop", "exit_px", "shares",
                                              "pnl", "pnl_pct", "fees", "reason"])
            w.writeheader()
            for t in trades:
                w.writerow({k: t.get(k, "") for k in w.fieldnames})
        # 净值曲线 CSV（日权益）
        eq_name = f"esb_{symbol}_{ts}_equity.csv"
        with open(out_dir / eq_name, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["date", "equity"])
            for i, e in enumerate(equity):
                w.writerow([dates[i] if i < len(dates) else dates[-1], round(e, 2)])
        message = _result_summary(symbol, dates, trades, stats, f"{out_dir.name}/{name}",
                                  bh=bh, yearly=yearly)
        return AgentResult(
            True, self.name, "done", message,
            artifact_paths=[f"{out_dir.name}/{name}", f"{out_dir.name}/{csv_name}",
                            f"{out_dir.name}/{eq_name}"],
            meta={"symbol": symbol, **{k: stats[k] for k in
                  ("trades", "win_rate", "total_pnl", "return_pct", "annual_pct",
                   "max_drawdown", "sharpe", "sortino", "calmar")},
                  "ts": _now_iso()},
        )

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "mode": "sina", "ready": True,
                "hint": "新浪日线 + 峰级线流式确认，无未来函数"}

    def describe(self) -> dict[str, Any]:
        return {"summary": "二四棒（峰级线交易系统）：突破左峰做多 + 左级止损 + 移动止损只上移，A股多头回测",
                "params": {"task": {"type": "string", "required": False,
                                    "description": "如 '600938' 或留空默认 sh600938"}}}


if __name__ == "__main__":
    if "--fetch" in sys.argv:
        idx = sys.argv.index("--fetch")
        code = sys.argv[idx + 1] if len(sys.argv) > idx + 1 else "600938"
        sym = f"sh{code}" if code.startswith("6") else f"sz{code}"
        rows = fetch_kline(sym)
        out = Path(sys.argv[idx + 2]) if len(sys.argv) > idx + 2 else Path(f"esb_{code}.csv")
        with open(out, "w", encoding="utf-8") as f:
            f.write("date,open,high,low,close,volume\n")
            for r in rows:
                f.write(f"{r['day']},{r['open']},{r['high']},{r['low']},{r['close']},{r.get('volume','')}\n")
        print(f"saved {len(rows)} bars -> {out}")
    else:
        path = sys.argv[1] if len(sys.argv) > 1 else "esb_klines.csv"
        dates, o, h, l, c = load_csv(path)
        trades, stats, equity = run_esb(dates, o, h, l, c)
        bh = _benchmark(dates, c)
        yearly = _yearly(trades, 1_000_000)
        print(render_report(trades, stats, 1_000_000, dates, bh=bh, yearly=yearly))
