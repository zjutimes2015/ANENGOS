"""多因子打分选股 agent：对每只股票算 N 个因子，加权总分排序。

因子清单（10个）：
1. RSI(14)：<30 超卖=+2分，30-45=+1分，>70 超买=-2分
2. 价格 vs MA20：在 MA20 上方=+1分，下方=-1分
3. 价格 vs MA60：在 MA60 上方=+1分，下方=-1分
4. MA 排列：MA5>MA20>MA60（多头）=+2分，反之=-2分
5. 近5日涨跌幅：<-3%（超跌）=+1分，>+5%（追高）=-1分
6. 近20日波动率：低波动（<3%）=+1分，高波动（>5%）=-1分
7. 成交量比（当日/5日均量）：>1.5（放量）=+1分
8. 布林带位置：价格在下轨附近=+1分，在上轨附近=-1分
9. MACD：金叉=+2分，死叉=-2分
10. 换手率：适中（1-5%）=+1分

总分 >= 3 分入选候选。
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Any

from connectors.base import AgentAdapter, AgentResult, _now_iso
from connectors._lessons import load_recent_lessons
from connectors.quant_ima import retrieve_ima_knowledge

try:
    import akshare as ak  # type: ignore
    _AK_AVAILABLE = True
except Exception:  # noqa: BLE001
    ak = None  # type: ignore
    _AK_AVAILABLE = False


def _sma(vals: list[float], n: int) -> float:
    if len(vals) < n:
        return float("nan")
    return sum(vals[-n:]) / n


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


def _macd(closes: list[float]) -> str:
    """简化 MACD：快慢线对比。"""
    if len(closes) < 26:
        return "unknown"
    ema12 = sum(closes[-12:]) / 12
    ema26 = sum(closes[-26:]) / 26
    prev_ema12 = sum(closes[-13:-1]) / 12 if len(closes) >= 13 else ema12
    prev_ema26 = sum(closes[-27:-1]) / 26 if len(closes) >= 27 else ema26
    curr_diff = ema12 - ema26
    prev_diff = prev_ema12 - prev_ema26
    if curr_diff > 0 and prev_diff <= 0:
        return "golden"
    if curr_diff < 0 and prev_diff >= 0:
        return "death"
    return "neutral"


def _boll_position(closes: list[float], n: int = 20) -> float:
    """价格在布林带的位置：0=下轨，1=上轨，0.5=中轨。"""
    if len(closes) < n:
        return 0.5
    ma = sum(closes[-n:]) / n
    std = (sum((x - ma) ** 2 for x in closes[-n:]) / n) ** 0.5
    upper = ma + 2 * std
    lower = ma - 2 * std
    last = closes[-1]
    if upper == lower:
        return 0.5
    return (last - lower) / (upper - lower)


def score_stock(closes: list[float], volumes: list[float]) -> tuple[float, list[str]]:
    """对单只股票打分，返回 (总分, 各项明细)。"""
    if len(closes) < 60:
        return 0.0, ["数据不足"]

    last = closes[-1]
    rsi = _rsi(closes)
    ma5 = _sma(closes, 5)
    ma20 = _sma(closes, 20)
    ma60 = _sma(closes, 60)

    score = 0.0
    details = []

    # 1. RSI
    if rsi < 30:
        score += 2
        details.append(f"RSI超卖({rsi:.0f})+2")
    elif rsi < 45:
        score += 1
        details.append(f"RSI偏弱({rsi:.0f})+1")
    elif rsi > 70:
        score -= 2
        details.append(f"RSI超买({rsi:.0f})-2")

    # 2. 价格 vs MA20
    if last > ma20:
        score += 1
        details.append("价>MA20 +1")
    else:
        score -= 1
        details.append("价<MA20 -1")

    # 3. 价格 vs MA60
    if last > ma60:
        score += 1
        details.append("价>MA60 +1")
    else:
        score -= 1
        details.append("价<MA60 -1")

    # 4. MA 排列
    if ma5 > ma20 > ma60:
        score += 2
        details.append("多头排列 +2")
    elif ma5 < ma20 < ma60:
        score -= 2
        details.append("空头排列 -2")

    # 5. 近5日涨跌
    chg5 = (closes[-1] - closes[-5]) / closes[-5] * 100 if len(closes) >= 5 else 0
    if chg5 < -3:
        score += 1
        details.append(f"5日超跌({chg5:.1f}%)+1")
    elif chg5 > 5:
        score -= 1
        details.append(f"5日追高({chg5:.1f}%)-1")

    # 6. 近20日波动率
    if len(closes) >= 20:
        rets = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(-20, 0)]
        vol = (sum((r - sum(rets)/len(rets))**2 for r in rets) / len(rets)) ** 0.5 * 100
        if vol < 3:
            score += 1
            details.append(f"低波({vol:.1f}%)+1")
        elif vol > 5:
            score -= 1
            details.append(f"高波({vol:.1f}%)-1")

    # 7. 量比（当日量 / 5日均量）
    if len(volumes) >= 5:
        avg_vol5 = sum(volumes[-5:]) / 5
        if avg_vol5 > 0 and volumes[-1] / avg_vol5 > 1.5:
            score += 1
            details.append("放量+1")

    # 8. 布林带位置
    boll_pos = _boll_position(closes)
    if boll_pos < 0.2:
        score += 1
        details.append("触下轨+1")
    elif boll_pos > 0.8:
        score -= 1
        details.append("贴上轨-1")

    # 9. MACD
    macd_sig = _macd(closes)
    if macd_sig == "golden":
        score += 2
        details.append("MACD金叉+2")
    elif macd_sig == "death":
        score -= 2
        details.append("MACD死叉-2")

    return score, details


class QuantMultifactorAdapter(AgentAdapter):
    name = "quant_multifactor"

    def __init__(self) -> None:
        self.mode = "akshare" if _AK_AVAILABLE else "mock"

    def submit(self, task: str, workspace: str) -> AgentResult:
        task = (task or "").strip()
        out_dir = Path(workspace) / "multifactor_output"
        out_dir.mkdir(parents=True, exist_ok=True)
        lessons = load_recent_lessons(workspace)
        ima_knowledge = retrieve_ima_knowledge("多因子 选股 趋势 策略")

        indexes = ["000300", "000905"]
        m = re.search(r"\b(\d{6})\b", task)
        if m:
            indexes = [m.group(1)]

        if not _AK_AVAILABLE:
            content = f"# 多因子选股（mock）\n{lessons}"
            path = out_dir / f"multifactor_{datetime.datetime.now().strftime('%Y%m%d')}.md"
            path.write_text(content, encoding="utf-8")
            return AgentResult(True, self.name, "done", f"[{self.name}/mock]",
                artifact_paths=[str(path.relative_to(workspace))])

        try:
            # 拿成分股
            seen = set()
            rows = []
            for idx_code in indexes:
                try:
                    cons = ak.index_stock_cons_sina(idx_code)
                    for _, row in cons.iterrows():
                        if row["code"] not in seen:
                            seen.add(row["code"])
                            rows.append(row)
                except Exception:
                    continue

            results = []
            errors = 0
            for row in rows:
                sym = row["symbol"]
                name = row["name"]
                try:
                    df = ak.stock_zh_a_daily(symbol=sym, start_date="20240601", adjust="qfq")
                    if df is None or len(df) < 60:
                        continue
                    closes = [float(x) for x in df["close"].tolist()]
                    volumes = [float(x) for x in df["volume"].tolist()]
                    score, details = score_stock(closes, volumes)
                    if score >= 3:  # 总分>=3 入选
                        results.append({
                            "code": row["code"], "name": name,
                            "close": closes[-1], "score": score, "details": details,
                        })
                except Exception:
                    errors += 1
                    continue

            results.sort(key=lambda x: -x["score"])
            today = datetime.datetime.now().strftime("%Y%m%d")

            lines = [
                f"# 多因子打分选股（{'+'.join(indexes)}）",
                f"- 扫描时间：{_now_iso()}",
                f"- 成分股：{len(rows)} 只（失败 {errors}）",
                f"- 候选（总分>=3）：{len(results)} 只",
                f"- 因子：RSI/MA20/MA60/MA排列/5日涨跌/波动率/量比/布林/MACD",
                "",
                lessons,
                ima_knowledge,
                "",
                "| 排名 | 代码 | 名称 | 收盘 | 总分 | 得分明细 |",
                "|---|---|---|---|---|---|",
            ]
            for i, r in enumerate(results[:20]):
                lines.append(f"| {i+1} | {r['code']} | {r['name']} | {r['close']:.2f} "
                             f"| **{r['score']:+.0f}** | {'; '.join(r['details'][:5])} |")

            path = out_dir / f"multifactor_{today}.md"
            path.write_text("\n".join(lines), encoding="utf-8")

            return AgentResult(True, self.name, "done",
                f"[{self.name}/akshare] {len(rows)}只 → {len(results)}只候选（总分>=3）",
                artifact_paths=[str(path.relative_to(workspace))])
        except Exception as e:  # noqa: BLE001
            return AgentResult(False, self.name, "error",
                f"[{self.name}/akshare] 失败：{e}", error=str(e))

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "ready": _AK_AVAILABLE, "mode": self.mode}

    def describe(self) -> dict[str, Any]:
        return {"summary": "多因子打分：10个因子加权评分，>=3分入选",
                "params": {"task": {"type": "string", "description": "指数代码，默认沪深300+中证500"}}}


# ---------------------------------------------------------------------------
# 历史回测（晋升门闸1/闸2）：多因子打分 → Top10 等权组合 vs 沪深300 基准
# 依据：docs/cognition/promotion_gate.md + 认知卡 multifactor.md §3/§4（从未回测）
# ---------------------------------------------------------------------------
def run_backtest(indexes: tuple[str, ...] = ("000300", "000905"),
                 capital: float = 1_000_000.0, top_n: int = 10,
                 rebalance_every: int = 5, split: float = 0.8) -> dict:
    """历史回测：每 rebalance_every 个交易日期末打分 → Top10 等权持有下一期。

    组合收益 = Top10 等权（简单平均）下期收益；基准 = 沪深300 指数。
    样本内外 80/20 切分判定：
      OOS 超额 > 0 且 OOS 回撤 < 15%        -> ROBUST
      OOS 超额 > 0 但回撤偏大              -> MODERATE
      IS 超额 > 0 且 OOS 超额 <= 0          -> OVERFITTED
      其余                                  -> WEAK
    """
    if not _AK_AVAILABLE:
        return {"verdict": "WEAK", "error": "akshare 不可用"}

    # 1. 成分股（去重）
    seen: set[str] = set()
    rows = []
    for idx_code in indexes:
        try:
            cons = ak.index_stock_cons_sina(idx_code)
            for _, row in cons.iterrows():
                if row["code"] not in seen:
                    seen.add(row["code"])
                    rows.append(row)
        except Exception:
            continue

    # 2. 基准日历 + 沪深300 收盘（对齐用）
    bm = ak.stock_zh_index_daily_em(symbol="sh000300")
    bm_dates = [str(d)[:10] for d in bm["date"]]
    bm_closes = [float(x) for x in bm["close"]]
    if len(bm_dates) < 200:
        return {"verdict": "WEAK", "error": "基准数据不足"}

    # 3. 逐股拉日线（2023-01 起，前复权）缓存为 {code: {date: close}, {date: volume}}
    hist: dict[str, tuple[dict, dict]] = {}
    for row in rows:
        try:
            df = ak.stock_zh_a_daily(symbol=row["symbol"], start_date="20230101", adjust="qfq")
            if df is None or len(df) < 100:
                continue
            closes = {str(d)[:10]: float(c) for d, c in zip(df["date"], df["close"])}
            vols = {str(d)[:10]: float(v) for d, v in zip(df["date"], df["volume"])}
            hist[row["code"]] = (closes, vols)
        except Exception:
            continue

    # 4. 重平衡模拟（从第 60 根基准日历开始打分）
    per_dates = [d for d in bm_dates if any(d in c for c, _ in hist.values())]
    rebal_idx = list(range(60, len(per_dates), rebalance_every))
    if len(rebal_idx) < 4:
        return {"verdict": "WEAK", "error": "重平衡期不足"}

    port_ret: list[float] = []       # 组合每期收益
    port_dates: list[str] = []       # 每期期初基准日期
    bm_ret: list[float] = []         # 基准每期收益

    for k, t0 in enumerate(rebal_idx[:-1]):
        t1 = rebal_idx[k + 1]
        d0, d1 = per_dates[t0], per_dates[t1]
        if d0 not in bm_closes_map(bm_dates, bm_closes) or d1 not in bm_closes_map(bm_dates, bm_closes):
            continue
        scored = []
        for code, (closes, vols) in hist.items():
            # 截至 d0 的最近 60 根
            seq = [closes[d] for d in per_dates[: t0 + 1] if d in closes]
            vseq = [vols[d] for d in per_dates[: t0 + 1] if d in vols]
            if len(seq) < 60:
                continue
            s, _ = score_stock(seq, vseq)
            scored.append((s, code))
        if not scored:
            continue
        top = [c for _, c in sorted(scored, key=lambda x: -x[0])[:top_n]]
        # 组合下期收益 = Top10 等权
        rs = []
        for code in top:
            closes = hist[code][0]
            if d0 in closes and d1 in closes:
                rs.append(closes[d1] / closes[d0] - 1)
        if not rs:
            continue
        port_ret.append(sum(rs) / len(rs))
        port_dates.append(d0)
        # 基准下期收益
        c0 = dict(zip(bm_dates, bm_closes))[d0]
        c1 = dict(zip(bm_dates, bm_closes))[d1]
        bm_ret.append(c1 / c0 - 1)

    if len(port_ret) < 6:
        return {"verdict": "WEAK", "error": f"有效期数不足（{len(port_ret)}）"}

    # 5. 净值与统计
    def _series_stats(rets: list[float], name: str) -> dict:
        eq = capital
        peak, max_dd = capital, 0.0
        for r in rets:
            eq *= (1 + r)
            peak = max(peak, eq)
            max_dd = max(max_dd, peak - eq)
        total = (eq / capital - 1) * 100
        years = len(rets) * rebalance_every / 244
        annual = total / years if years > 0 else 0
        return {"name": name, "total_return_pct": total, "annual_pct": annual,
                "max_drawdown": max_dd, "final_equity": eq}

    n_split = max(int(len(port_ret) * split), 3)
    is_r, oos_r = port_ret[:n_split], port_ret[n_split:]
    is_b, oos_b = bm_ret[:n_split], bm_ret[n_split:]
    is_p, oos_p = _series_stats(is_r, "in-sample"), _series_stats(oos_r, "out-of-sample")
    is_bm_s, oos_bm_s = _series_stats(is_b, "bm"), _series_stats(oos_b, "bm")
    oos_exc = oos_p["total_return_pct"] - oos_bm_s["total_return_pct"]
    is_exc = is_p["total_return_pct"] - is_bm_s["total_return_pct"]
    oos_dd_pct = oos_p["max_drawdown"] / capital * 100

    if oos_exc > 0 and oos_dd_pct < 15:
        verdict = "ROBUST"
    elif oos_exc > 0:
        verdict = "MODERATE"
    elif is_exc > 0:
        verdict = "OVERFITTED"
    else:
        verdict = "WEAK"

    return {
        "verdict": verdict,
        "indexes": list(indexes), "top_n": top_n, "rebalance_every": rebalance_every,
        "stocks_scanned": len(hist), "periods": len(port_ret),
        "range": f"{port_dates[0]} ~ {per_dates[rebal_idx[-1]]}",
        "in_sample": {"periods": len(is_r), "portfolio": is_p, "benchmark": is_bm_s,
                      "excess_pct": is_exc},
        "out_of_sample": {"periods": len(oos_r), "portfolio": oos_p, "benchmark": oos_bm_s,
                          "excess_pct": oos_exc},
    }


def bm_closes_map(dates: list, closes: list) -> dict:
    return dict(zip(dates, closes))


def render_backtest(bt: dict) -> str:
    """多因子历史回测报告（晋升门闸1/闸2产物）。"""
    if bt.get("error"):
        return f"多因子回测失败：{bt['error']}"
    is_ = bt["in_sample"]
    oos = bt["out_of_sample"]
    L = [
        "=" * 78,
        "多因子历史回测报告（晋升门闸1/闸2）| 10因子打分 → Top10 等权 vs 沪深300",
        "=" * 78,
        f"选股池：{'+'.join(bt['indexes'])} | 扫描 {bt['stocks_scanned']} 只 | 重平衡每 {bt['rebalance_every']} 交易日",
        f"区间：{bt['range']} | 共 {bt['periods']} 期 | 样本内外 80/20",
        "",
        f"**判定：{bt['verdict']}**",
        {
            "ROBUST": "✅ 样本外超额为正且回撤可控：可通过晋升门闸1/闸2，进虚拟盘对照",
            "MODERATE": "🟡 样本外超额为正但回撤偏大：观察，加过滤条件",
            "OVERFITTED": "❌ 样本内好样本外差：过拟合，淘汰/需重构因子",
            "WEAK": "🟠 不推荐",
        }[bt["verdict"]],
        "",
        "| 指标 | In-Sample | Out-of-Sample |",
        "|---|---|---|",
        f"| 期数 | {is_['periods']} | {oos['periods']} |",
        f"| 组合收益 | {is_['portfolio']['total_return_pct']:+.2f}% | {oos['portfolio']['total_return_pct']:+.2f}% |",
        f"| 组合年化 | {is_['portfolio']['annual_pct']:+.2f}% | {oos['portfolio']['annual_pct']:+.2f}% |",
        f"| 组合回撤 | {is_['portfolio']['max_drawdown']:,.0f} 元 | {oos['portfolio']['max_drawdown']:,.0f} 元 |",
        f"| 基准(沪深300) | {is_['benchmark']['total_return_pct']:+.2f}% | {oos['benchmark']['total_return_pct']:+.2f}% |",
        f"| 超额 | {is_['excess_pct']:+.2f}% | {oos['excess_pct']:+.2f}% |",
        "",
        "注：组合为等权、未计交易费用；周度重平衡为近似，实盘需考虑冲击成本。",
    ]
    return "\n".join(L)


if __name__ == "__main__":
    import sys as _sys
    if "--backtest" in _sys.argv:
        bt = run_backtest()
        print(render_backtest(bt))
