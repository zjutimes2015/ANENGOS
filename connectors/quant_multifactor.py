"""B重构：仅保留单因子检验有效的 20日低波动异象（vol20）打分。

13 个技术面因子单因子检验（298只/170期）仅 vol20 有效（IC -0.0572，
Q1低波 +31.93% vs Q5高波 -0.48%），其余 12 个（RSI/MA位置/MA排列/5日涨跌/
量比/布林/MACD/动量/反转/距新高/MA斜率）|IC|<0.02 全部剔除。

低波打分：20日波动 <3% +2 / <5% +1 / >7% -1；观察清单取得分>0 的 Top15。
估值/财务因子（PE/PB/ROE/市值/增速）由后续单因子检验后叠加（见任务 #53）。
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


def _ak_retry(fn, tries: int = 4, base_sleep: float = 2.0):
    """M0-0.3：akshare 偶发 RemoteDisconnected（东财/新浪限流断连），指数退避重试。"""
    import time
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(base_sleep * (i + 1))
    raise last


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
    """B 重构后打分：13 个技术面因子单因子检验（298只/170期）仅 vol20 低波动有效，
    其余 12 个（RSI/MA位置/MA排列/5日涨跌/量比/布林/MACD/动量/反转/新高/斜率）
    |IC|<0.02 全部剔除。此处只保留低波动异象单因子打分。"""
    if len(closes) < 60:
        return 0.0, ["数据不足"]

    score = 0.0
    details = []

    # 唯一有效因子：20日波动率（低波动异象，Q1低波 +31.93% vs Q5高波 -0.48%，IC -0.0572）
    if len(closes) >= 20:
        rets = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(-20, 0)]
        vol = (sum((r - sum(rets) / len(rets)) ** 2 for r in rets) / len(rets)) ** 0.5 * 100
        if vol < 3:
            score += 2
            details.append(f"低波({vol:.1f}%)+2")
        elif vol < 5:
            score += 1
            details.append(f"中低波({vol:.1f}%)+1")
        elif vol > 7:
            score -= 1
            details.append(f"高波({vol:.1f}%)-1")

    return score, details


# ---------------------------------------------------------------------------
# #53 估值/财务因子：静态快照（仅实时观察层排序用，不进历史时序回测）
# 依据：沪深300静态截面IC（近120日已实现收益，2026-10-19，297只）
#   销售净利率 +0.2218 / 加权ROE +0.1781 / 净利增长 +0.1269 / PE(TTM) -0.1728(低PE)
#   PB -0.072(弱辅助)；总市值 +0.4514 为单窗口大盘风格暴露，不做硬因子；收入增长 -0.0089 无效剔除。
# 注意：均为当前最新快照，无法 point-in-time 时序回放，故只用于实时候选内排序，
#       历史回测过门仍只走可时序复算的低波技术因子（避免未来函数）。
# ---------------------------------------------------------------------------
def _num(x):
    try:
        v = float(x)
        return None if (v != v or v in (float("inf"), float("-inf"))) else v
    except Exception:
        return None


def fetch_fundamentals(code: str) -> dict:
    """最新快照估值/财务：PE(TTM)/PB/加权ROE/销售净利率/净利增长率。容错返回部分字段。"""
    out: dict = {}
    try:
        ve = _ak_retry(lambda: ak.stock_value_em(symbol=code))
        last = ve.iloc[-1]  # 升序，末行最新
        pe = _num(last["PE(TTM)"])
        pb = _num(last["市净率"])
        if pe is not None and pe > 0:
            out["pe"] = pe
        if pb is not None and pb > 0:
            out["pb"] = pb
    except Exception:
        pass
    try:
        fi = _ak_retry(lambda: ak.stock_financial_analysis_indicator(symbol=code))
        row = fi.iloc[-1]  # 新浪表按日期升序，末行=最新报告期（勿取 iloc[0]）
        roe = _num(row.get("加权净资产收益率(%)", row.get("净资产收益率(%)")))
        nm = _num(row.get("销售净利率(%)"))
        ng = _num(row.get("净利润增长率(%)"))
        if roe is not None:
            out["roe"] = roe
        if nm is not None:
            out["net_margin"] = nm
        if ng is not None:
            out["np_growth"] = ng
    except Exception:
        pass
    return out


def qual_score(f: dict) -> tuple[float, list[str]]:
    """质量价值附加分（静态截面有效方向）：高净利率/高ROE/高净利增长/低PE 加分。"""
    s = 0.0
    tags: list[str] = []
    nm, roe, ng, pe = f.get("net_margin"), f.get("roe"), f.get("np_growth"), f.get("pe")
    if nm is not None:
        if nm >= 30:
            s += 2; tags.append("高净利率+2")
        elif nm >= 15:
            s += 1; tags.append("净利率≥15%+1")
    if roe is not None:
        if roe >= 20:
            s += 2; tags.append("高ROE+2")
        elif roe >= 12:
            s += 1; tags.append("ROE≥12%+1")
    if ng is not None and ng >= 15:
        s += 1; tags.append("净利增长≥15%+1")
    if pe is not None:
        if 0 < pe < 15:
            s += 1; tags.append("低PE+1")
        elif pe > 40:
            s -= 1; tags.append("高PE-1")
    return s, tags


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
        codes = re.findall(r"\b(\d{6})\b", task)
        if codes:
            indexes = codes

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
                    # B重构：低波单因子满分仅2，不再用旧的 score>=3 硬门槛；
                    # 全部入列后按低波得分降序，报告/观察清单取 Top（见下）。
                    results.append({
                        "code": row["code"], "name": name,
                        "close": closes[-1], "score": score, "details": details,
                    })
                except Exception:
                    errors += 1
                    continue

            results.sort(key=lambda x: -x["score"])
            # 低波观察清单：入选门槛仍为低波得分>0（不改变已 ROBUST 的低波池），取 Top15
            watch = [r for r in results if r["score"] > 0][:15]
            # #53：仅对已入选候选补最新快照估值/财务，算质量价值附加分，用于候选内排序
            for r in watch:
                fund = fetch_fundamentals(r["code"])
                qs, qtags = qual_score(fund)
                r["fund"] = fund
                r["qual"] = qs
                r["qual_tags"] = qtags
                r["total"] = r["score"] + qs
            watch.sort(key=lambda x: -x["total"])
            today = datetime.datetime.now().strftime("%Y%m%d")

            lines = [
                f"# 低波+质量价值观察清单（{'+'.join(indexes)}）",
                f"- 扫描时间：{_now_iso()}",
                f"- 成分股：{len(rows)} 只（失败 {errors}）",
                f"- 观察候选（低波得分>0，Top15，按低波+质量价值总分排序）：{len(watch)} 只",
                "- 主因子：20日低波动异象（vol20，时序单因子检验 IC -0.0572，回测 ROBUST）",
                "- 附加分（静态截面快照，仅候选内排序，不进时序回测）：销售净利率/加权ROE/净利增长/低PE",
                "- 截面IC（沪深300静态，2026-10-19，297只，近120日收益）：净利率+0.222 / ROE+0.178 / 净利增长+0.127 / PE-0.173",
                "",
                lessons,
                ima_knowledge,
                "",
                "| 排名 | 代码 | 名称 | 收盘 | 低波 | 质量价值 | 总分 | PE | ROE% | 净利率% | 附加标签 |",
                "|---|---|---|---|---|---|---|---|---|---|---|",
            ]
            for i, r in enumerate(watch):
                f = r.get("fund", {})
                pe = f"{f['pe']:.1f}" if f.get("pe") is not None else "—"
                roe = f"{f['roe']:.1f}" if f.get("roe") is not None else "—"
                nm = f"{f['net_margin']:.1f}" if f.get("net_margin") is not None else "—"
                lines.append(
                    f"| {i+1} | {r['code']} | {r['name']} | {r['close']:.2f} "
                    f"| {r['score']:+.0f} | {r['qual']:+.0f} | **{r['total']:+.0f}** "
                    f"| {pe} | {roe} | {nm} | {'; '.join(r.get('qual_tags', [])) or '—'} |")

            path = out_dir / f"multifactor_{today}.md"
            path.write_text("\n".join(lines), encoding="utf-8")

            return AgentResult(True, self.name, "done",
                f"[{self.name}/akshare] {len(rows)}只 → {len(watch)}只低波观察候选",
                artifact_paths=[str(path.relative_to(workspace))])
        except Exception as e:  # noqa: BLE001
            return AgentResult(False, self.name, "error",
                f"[{self.name}/akshare] 失败：{e}", error=str(e))

    def health(self) -> dict[str, Any]:
        return {"name": self.name, "ready": _AK_AVAILABLE, "mode": self.mode}

    def describe(self) -> dict[str, Any]:
        return {"summary": "主因子20日低波动异象(时序检验有效/回测ROBUST)；#53叠加静态截面有效的质量价值附加分(净利率/ROE/净利增长/低PE)，仅候选内排序，不进时序回测",
                "params": {"task": {"type": "string", "description": "指数代码，默认沪深300+中证500"}}}


# ---------------------------------------------------------------------------
# 历史回测（晋升门闸1/闸2）：多因子打分 → Top10 等权组合 vs 沪深300 基准
# 依据：docs/cognition/promotion_gate.md + 认知卡 multifactor.md §3/§4（从未回测）
# ---------------------------------------------------------------------------
def run_backtest(indexes: tuple[str, ...] = ("000300", "000905"),
                 capital: float = 1_000_000.0, top_n: int = 10,
                 rebalance_every: int = 5, split: float = 0.8,
                 signal: str = "multifactor") -> dict:
    """历史回测：每 rebalance_every 个交易日期末按 signal 选股 → Top 等权持有下一期。

    signal:
      - multifactor：score_stock 10因子打分（默认，B重构后为有效因子打分）
      - screener：RSI(14)<45 且 收盘>MA20，按 RSI 升序取 Top（复用 quant_screener 规则）
    组合收益 = Top 等权下期收益；基准 = 沪深300。80/20 切分判定：
      OOS 超额>0 且 OOS 回撤<15% -> ROBUST；OOS 超额>0 -> MODERATE；
      IS 超额>0 且 OOS<=0 -> OVERFITTED；其余 -> WEAK。
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

    # 2. 基准日历 + 沪深300 收盘（新浪源，东财 em 会限流断连）
    try:
        bm = _ak_retry(lambda: ak.stock_zh_index_daily(symbol="sh000300"))
    except Exception as e:  # noqa: BLE001
        return {"verdict": "WEAK", "error": f"基准(沪深300)拉取失败：{type(e).__name__}"}
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
            if signal == "screener":
                rsi = _rsi(seq)
                ma20 = _sma(seq, 20)
                # quant_screener 规则：RSI<45 且 收盘>MA20；低 RSI（更超卖）排前
                if rsi < 45 and seq[-1] > ma20:
                    scored.append((-rsi, code))
            else:
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
        "signal": signal,
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


# ---------------------------------------------------------------------------
# 单因子有效性检验（晋升门 §4 待办①）：因子值分组单调性 + rank IC
# 依据：docs/cognition/promotion_gate.md + 认知卡 multifactor.md §4（重构前必做）
# ---------------------------------------------------------------------------
FACTOR_NAMES = ["rsi14", "px_ma20", "px_ma60", "ma_align", "chg5",
                "vol20", "vol_ratio", "boll_pos", "macd",
                "mom20", "rev5", "dist_high", "ma_slope"]


def factor_values(closes: list[float], volumes: list[float]) -> dict:
    """候选因子连续值（独立于 score_stock 阈值打分，用于分组检验 + rank IC）。

    旧池仅 vol20 有效；B 重构在此扩充新候选（60 根内可算）：
    mom20=20日动量 / rev5=5日反转(超跌为正) / dist_high=距60日新高距离 / ma_slope=MA20斜率。
    """
    out: dict[str, float] = {}
    if len(closes) < 60 or len(volumes) < 5:
        return out
    last = closes[-1]
    out["rsi14"] = _rsi(closes)
    ma5, ma20, ma60 = _sma(closes, 5), _sma(closes, 20), _sma(closes, 60)
    out["px_ma20"] = last / ma20 - 1
    out["px_ma60"] = last / ma60 - 1
    out["ma_align"] = 2 if ma5 > ma20 > ma60 else (-2 if ma5 < ma20 < ma60 else 0)
    out["chg5"] = (last - closes[-5]) / closes[-5]
    rets = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(-20, 0)]
    m = sum(rets) / len(rets)
    out["vol20"] = (sum((r - m) ** 2 for r in rets) / len(rets)) ** 0.5
    avg5 = sum(volumes[-5:]) / 5
    out["vol_ratio"] = volumes[-1] / avg5 if avg5 > 0 else 1.0
    out["boll_pos"] = _boll_position(closes)
    sig = _macd(closes)
    out["macd"] = 2 if sig == "golden" else (-2 if sig == "death" else 0)
    # —— B 重构新增候选（短周期，60 根内可算）——
    out["mom20"] = last / closes[-21] - 1
    out["rev5"] = -(last / closes[-6] - 1)
    high60 = max(closes[-60:])
    out["dist_high"] = 1 - last / high60 if high60 > 0 else 0.0
    ma20_prev = _sma(closes[:-10], 20)
    out["ma_slope"] = (ma20 - ma20_prev) / ma20_prev if ma20_prev > 0 else 0.0
    return out


def _spearman(xs: list[float], ys: list[float]) -> float:
    """纯 Python Spearman 秩相关（云上无 scipy）。"""
    n = len(xs)
    if n < 3:
        return 0.0
    def _rank(vals: list[float]) -> list[float]:
        order = sorted(range(len(vals)), key=lambda i: vals[i])
        ranks = [0.0] * len(vals)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                ranks[order[k]] = avg
            i = j + 1
        return ranks
    rx, ry = _rank(xs), _rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx) ** 0.5
    dy = sum((b - my) ** 2 for b in ry) ** 0.5
    return num / (dx * dy) if dx and dy else 0.0


def run_factor_test(indexes: tuple[str, ...] = ("000300",),
                    rebalance_every: int = 5, split: float = 0.8) -> dict:
    """单因子有效性检验：每期按因子值分 5 组（Q1..Q5），累计各组下期收益 + rank IC。
    单调性判定：Q5−Q1 累计收益差（方向由因子语义决定，报告标注）。
    M0-0.3：默认单指数(沪深300)先跑通；东财源限流，基准走新浪源并容错。"""
    if not _AK_AVAILABLE:
        return {"error": "akshare 不可用"}
    seen: set[str] = set()
    rows = []
    for idx_code in indexes:
        try:
            cons = _ak_retry(lambda c=idx_code: ak.index_stock_cons_sina(c))
            for _, row in cons.iterrows():
                if row["code"] not in seen:
                    seen.add(row["code"])
                    rows.append(row)
        except Exception:
            continue
    try:
        bm = _ak_retry(lambda: ak.stock_zh_index_daily(symbol="sh000300"))
    except Exception as e:  # noqa: BLE001
        return {"error": f"基准(沪深300)拉取失败：{type(e).__name__} {str(e)[:120]}"}
    bm_dates = [str(d)[:10] for d in bm["date"]]
    bm_closes = [float(x) for x in bm["close"]]
    bm_map = dict(zip(bm_dates, bm_closes))
    if len(bm_dates) < 200:
        return {"error": "基准数据不足"}
    hist: dict[str, tuple[dict, dict]] = {}
    for row in rows:
        try:
            df = _ak_retry(lambda s=row["symbol"]: ak.stock_zh_a_daily(symbol=s, start_date="20230101", adjust="qfq"))
            if df is None or len(df) < 100:
                continue
            closes = {str(d)[:10]: float(c) for d, c in zip(df["date"], df["close"])}
            vols = {str(d)[:10]: float(v) for d, v in zip(df["date"], df["volume"])}
            hist[row["code"]] = (closes, vols)
        except Exception:
            continue
    per_dates = [d for d in bm_dates if any(d in c for c, _ in hist.values())]
    rebal_idx = list(range(60, len(per_dates), rebalance_every))
    if len(rebal_idx) < 4:
        return {"error": "重平衡期不足"}

    grp: dict[str, list[list[float]]] = {f: [[] for _ in range(5)] for f in FACTOR_NAMES}
    ics: dict[str, list[float]] = {f: [] for f in FACTOR_NAMES}
    n_periods = 0
    for k, t0 in enumerate(rebal_idx[:-1]):
        t1 = rebal_idx[k + 1]
        d0, d1 = per_dates[t0], per_dates[t1]
        if d0 not in bm_map or d1 not in bm_map:
            continue
        snap = []
        for code, (closes, vols) in hist.items():
            seq = [closes[d] for d in per_dates[: t0 + 1] if d in closes]
            vseq = [vols[d] for d in per_dates[: t0 + 1] if d in vols]
            if len(seq) < 60:
                continue
            if d0 not in closes or d1 not in closes:
                continue
            fv = factor_values(seq, vseq)
            if not fv:
                continue
            snap.append((fv, closes[d1] / closes[d0] - 1))
        if len(snap) < 25:
            continue
        n_periods += 1
        for f in FACTOR_NAMES:
            valid = [(fv.get(f), ret) for fv, ret in snap if f in fv and fv.get(f) is not None]
            if len(valid) < 10:
                continue
            srt = sorted(valid, key=lambda p: p[0])
            n = len(srt)
            size = max(n // 5, 1)
            for g in range(5):
                seg = srt[g * size: (g + 1) * size if g < 4 else n]
                if seg:
                    grp[f][g].append(sum(p[1] for p in seg) / len(seg))
            ics[f].append(_spearman([p[0] for p in valid], [p[1] for p in valid]))

    result: dict[str, dict] = {}
    for f in FACTOR_NAMES:
        if not grp[f][0]:
            result[f] = {"status": "无数据", "groups": [None] * 5, "mean_ic": None}
            continue
        cum = []
        for g in range(5):
            rets = grp[f][g]
            eq = 1.0
            for r in rets:
                eq *= (1 + r)
            cum.append((eq - 1) * 100)
        ic_vals = ics[f]
        result[f] = {
            "groups": [round(x, 2) for x in cum],
            "spread": round(cum[4] - cum[0], 2),
            "mean_ic": round(sum(ic_vals) / len(ic_vals), 4) if ic_vals else None,
            "n_periods": n_periods,
        }
    return {"factors": result, "stocks": len(hist), "periods": n_periods}


def render_factor_test(ft: dict) -> str:
    if ft.get("error"):
        return f"因子检验失败：{ft['error']}"
    names = {
        "rsi14": "RSI(14)", "px_ma20": "价/MA20", "px_ma60": "价/MA60",
        "ma_align": "MA排列", "chg5": "5日涨跌", "vol20": "20日波动",
        "vol_ratio": "量比", "boll_pos": "布林带", "macd": "MACD",
        "mom20": "20日动量", "rev5": "5日反转", "dist_high": "距60日新高",
        "ma_slope": "MA20斜率",
    }
    L = [
        "=" * 78,
        "单因子有效性检验（晋升门 §4 待办①）| Q1=因子值最低组 → Q5=最高组",
        "=" * 78,
        f"股票 {ft['stocks']} 只 | 期数 {ft['periods']} | 各期累计收益（%），分组单调 = 因子有效",
        "",
        "| 因子 | Q1 | Q2 | Q3 | Q4 | Q5 | Q5−Q1 | rank IC | 判定 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for f in FACTOR_NAMES:
        r = ft["factors"][f]
        if r.get("status") == "无数据" or r["groups"][0] is None:
            L.append(f"| {names[f]} | 无数据 | | | | | | | 无效 |")
            continue
        g = " | ".join(f"{x:+.2f}" for x in r["groups"])
        ic = f"{r['mean_ic']:.4f}" if r["mean_ic"] is not None else "—"
        spread = r["spread"]
        if abs(r["mean_ic"] or 0) >= 0.02 and abs(spread) >= 3:
            verdict = "🟢 有效" + ("(+方向)" if spread > 0 else "(-方向)")
        else:
            verdict = "🔴 无效"
        L.append(f"| {names[f]} | {g} | {spread:+.2f} | {ic} | {verdict} |")
    L += [
        "",
        "判定：|rank IC|≥0.02 且 |Q5−Q1|≥3 为有效；方向由 spread 符号决定。",
        "B 重构：仅保留有效因子进打分，无效因子剔除。",
    ]
    return "\n".join(L)


def render_backtest(bt: dict) -> str:
    """多因子历史回测报告（晋升门闸1/闸2产物）。"""
    if bt.get("error"):
        return f"多因子回测失败：{bt['error']}"
    is_ = bt["in_sample"]
    oos = bt["out_of_sample"]
    sig_label = {
        "multifactor": "低波单因子打分（B重构）",
        "screener": "RSI<45 且 收盘>MA20（screener规则）",
    }.get(bt.get("signal"), str(bt.get("signal", "multifactor")))
    L = [
        "=" * 78,
        f"历史回测报告（晋升门闸1/闸2）| {sig_label} → Top 等权 vs 沪深300",
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
    import pathlib as _pl
    argv = _sys.argv[1:]
    out = None
    if "--out" in argv:
        out = argv[argv.index("--out") + 1]
    signal = "multifactor"
    if "--signal" in argv:
        signal = argv[argv.index("--signal") + 1]
    if "--backtest" in argv:
        text = render_backtest(run_backtest(signal=signal))
    elif "--factor-test" in argv:
        text = render_factor_test(run_factor_test())
    else:
        text = "usage: --factor-test | --backtest [--signal multifactor|screener] [--out path]"
    print(text)
    if out:
        _pl.Path(out).write_text(text, encoding="utf-8")
