"""P3.5/P4 容器内批量检验器（云端自治，本机关机也能跑）。

由 host cron 周日 19:30 `docker exec` 触发：
  读 factor_library.json 的 candidate 队列 → 可算因子自动过 L2→L3→L4→L5
  → 全过升 active，否则 rejected 留痕 → 写 batch_YYYYWW.md 周报。

因子族：
  - 波动族 vol10/vol30/vol60：日线可算（低波异象）；
  - 价值族 pe_ttm_inv/pb_inv：stock_value_em 日频 PE(TTM)/PB（带磁盘缓存）；
  - 源B 残差正交：每期截面把候选对 active(低波) OLS 取残差，用残差算增量IC；
  - 真实 L4：active低波Q1 组合 vs active+候选合成Q1 组合，OOS 段比信息比率。

跳过（不误判）：
  - 季度财务快照 net_margin/roe/np_growth：无披露日历PIT，待专业源；
  - 规则过滤 filter_*：非连续因子，待人工规则化。

闸门：
  L2 时序单因子：|mean rankIC|≥0.02 且 bootstrap IC95%CI不含0；
  L3 相关去冗：与 active 平均截面 |pearson|>0.9 拒；
  L4 lift增量：加入后组合 IR 净提升（info_ratio 对比）；
  L5 OOS门：80/20，OOS超额>0 且回撤<15%。
"""
from __future__ import annotations

import datetime
import json
import time
from pathlib import Path

from connectors.factor_library import (
    FactorLibrary, bootstrap_ci, ic_pvalue, pearson, lift_gate, empty_record,
    ic_stats, review_factor, info_ratio,
)
from connectors.walkforward import rolling_windows, classify_gate
from connectors.jev_guardrail import jev_factor_verdict

import os  # noqa: E402

# 股票池（可用环境变量 ANENGOS_INDEX 覆盖）：000300沪深300 / 000905中证500
INDEX_CODE = os.environ.get("ANENGOS_INDEX", "000905")
BM_SYMBOL = "sh000300" if INDEX_CODE == "000300" else "sh000905"

WS = Path("/srv/anengos/workspace")
LIB = WS / "factor_library.json"
VAL_CACHE = WS / f"val_cache_{INDEX_CODE}.json"
DAY_CACHE = WS / f"day_cache_{INDEX_CODE}.json"

# 波动族：id -> 窗口N
VOL_FACTORS = {"vol10": 10, "vol30": 30, "vol60": 60}
# 价值/规模族：id -> stock_value_em 列名（方向均为 asc，越小越好：低PE/低PB/小市值）
VAL_FACTORS = {
    "pe_ttm_inv": "PE(TTM)",
    "pb_inv": "市净率",
    "mcap_inv": "总市值",
}
# 跳过类
SKIP_PIT = {"net_margin", "roe", "np_growth", "pe_ttm"}
SKIP_RULE = {"filter_signal_quality", "filter_risk_stop"}

REBAL = 5
SPLIT = 0.8
# 成本门（学做市派）：周频换仓每期扣千2.5（双边佣金万2.5+印花税千1+spread）
# 以后所有因子检验自动扣成本，净夏普<0.5不允许进active
COST_PER_REBAL = 0.0025


def _ak_retry(fn, tries: int = 4, base_sleep: float = 2.0):
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(base_sleep * (i + 1))
    raise last


def _rank(vals: list[float]) -> list[float]:
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    for r, i in enumerate(order):
        ranks[i] = r + 1
    return ranks


def _spearman(xs: list[float], ys: list[float]) -> float:
    rx, ry = _rank(xs), _rank(ys)
    n = len(rx)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx) ** 0.5
    dy = sum((b - my) ** 2 for b in ry) ** 0.5
    return num / (dx * dy) if dx and dy else 0.0


def _residual(xs: list[float], ys: list[float]) -> list[float]:
    """源B：一元OLS取残差（候选对active回归），剥离与active共线的线性成分。"""
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    syy = sum((y - my) ** 2 for y in ys)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / syy if syy else 0.0
    a = mx - b * my
    return [x - (a + b * y) for x, y in zip(xs, ys)]


def _vol(seq: list[float], n: int) -> float:
    if len(seq) < n + 1:
        return float("nan")
    rets = [(seq[i] - seq[i - 1]) / seq[i - 1] for i in range(-n, 0)]
    m = sum(rets) / len(rets)
    return (sum((r - m) ** 2 for r in rets) / len(rets)) ** 0.5


def _walkforward_gate(excess: list[float]) -> dict:
    """P0-2：对每期多头超额收益序列做滚动 walk-forward，复用质量门判定。

    训练窗60%/样本外窗20%按期数滚动；只有 ROBUST/MODERATE 通过，
    WEAK（样本外无正期望）/OVERFITTED（IS正OOS负）自动拦截。
    """
    n = len(excess)
    if n < 9:
        return {"level": "WEAK", "reason": "期数不足，无法滚动检验",
                "is_metrics": {}, "oos_metrics": {}, "oos_positive_ratio": 0.0,
                "pass": False}
    train_len = max(int(n * 0.6), 3)
    test_len = max(int(n * 0.2), 2)
    wins = rolling_windows(n, train_len, test_len)
    per, oos_concat, is_concat = [], [], []
    for is0, is1, o0, o1 in wins:
        is_concat.extend(excess[is0:is1])
        oos_concat.extend(excess[o0:o1])
        seg = excess[o0:o1]
        per.append({"net_pnl": sum(seg) / len(seg)})
    is_m = {"net_pnl": sum(is_concat) / len(is_concat)}
    oos_m = {"net_pnl": sum(oos_concat) / len(oos_concat),
             "sharpe": info_ratio(oos_concat)}
    v = classify_gate(is_m, oos_m, per).to_dict()
    v["pass"] = v["level"] in ("ROBUST", "MODERATE")
    return v


def _assess(ics: list[float], comb_rets: list[float], bench_rets: list[float],
            pit: bool, n_p: int, corr_vs_active: float | None = None) -> dict:
    """P0-1/P0-2 公共评估：IC稳健性统计 + 独立审查 + 滚动walk-forward门 + JEV护栏。"""
    stats = ic_stats(ics)
    excess = [c - b for c, b in zip(comb_rets, bench_rets)]
    wf = _walkforward_gate(excess)
    review = review_factor(stats, pit, n_p)

    # JEV护栏：用能拿到的字段做置信度判断
    mean_ic = stats.get("mean_ic", 0.0)
    oos_sharpe = wf.get("oos_metrics", {}).get("sharpe", 0.0)
    is_sharpe = wf.get("is_metrics", {}).get("sharpe", 0.0)
    jev = jev_factor_verdict(
        ic=mean_ic,
        oos_sharpe=oos_sharpe if oos_sharpe == oos_sharpe else 0.0,  # NaN处理
        is_sharpe=is_sharpe if is_sharpe == is_sharpe else 0.0,
        corr=corr_vs_active if corr_vs_active is not None else 0.0,
        cost_net_sharpe=None,  # 成本后夏普在调用方算
    )

    return {"stats": stats, "wf": wf, "review": review,
            "review_pass": review["pass"], "wf_pass": wf["pass"],
            "jev": jev}


def load_panel() -> tuple[dict, list[str], dict]:
    """拉指数成分日线 + 基准，返回 hist, per_dates, bm_map；日线按指数磁盘缓存。"""
    if DAY_CACHE.exists() and os.environ.get("ANENGOS_REFRESH") != "1":
        try:
            c = json.loads(DAY_CACHE.read_text(encoding="utf-8"))
            return c["hist"], c["per_dates"], c["bm_map"]
        except Exception:  # noqa: BLE001
            pass
    import akshare as ak  # type: ignore
    seen: set[str] = set()
    symbols: dict[str, str] = {}
    cons = _ak_retry(lambda: ak.index_stock_cons_sina(INDEX_CODE))
    for _, row in cons.iterrows():
        if row["code"] not in seen:
            seen.add(row["code"])
            symbols[row["code"]] = row["symbol"]
    bm = _ak_retry(lambda: ak.stock_zh_index_daily(symbol=BM_SYMBOL))
    bm_map = {str(d)[:10]: float(c) for d, c in zip(bm["date"], bm["close"])}
    hist: dict[str, dict] = {}
    for code, sym in symbols.items():
        try:
            df = _ak_retry(lambda s=sym: ak.stock_zh_a_daily(symbol=s, start_date="20230101", adjust="qfq"))
            if df is None or len(df) < 120:
                continue
            hist[code] = {str(d)[:10]: float(c) for d, c in zip(df["date"], df["close"])}
        except Exception:  # noqa: BLE001
            continue
    per_dates = [d for d in bm_map if any(d in c for c in hist.values())]
    per_dates.sort()
    DAY_CACHE.write_text(json.dumps({"hist": hist, "per_dates": per_dates, "bm_map": bm_map}),
                         encoding="utf-8")
    return hist, per_dates, bm_map


def load_val_panel(codes: list[str]) -> dict:
    """stock_value_em 日频 PE(TTM)/PB/总市值，按指数分磁盘缓存（跨周复用）。
    返回 val[code] = {date: {"PE(TTM)":..,"市净率":..,"总市值":..}}。"""
    import akshare as ak  # type: ignore
    if VAL_CACHE.exists():
        try:
            return json.loads(VAL_CACHE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    cols = ["PE(TTM)", "市净率", "总市值"]
    val: dict[str, dict] = {}
    for code in codes:
        try:
            df = _ak_retry(lambda c=code: ak.stock_value_em(symbol=c), tries=3, base_sleep=1.0)
            if df is None or len(df) == 0:
                continue
            m = {}
            for _, row in df.iterrows():
                rec = {}
                ok = True
                for c in cols:
                    v = row.get(c)
                    if v is None or v != v:
                        ok = False
                        break
                    rec[c] = float(v)
                if ok:
                    m[str(row["数据日期"])[:10]] = rec
            val[code] = m
        except Exception:  # noqa: BLE001
            continue
    VAL_CACHE.write_text(json.dumps(val, ensure_ascii=False), encoding="utf-8")
    return val


def _oos_metrics(q1_rets: list[float], active_q1: list[float],
                 bench: list[float], cut: int) -> dict:
    """L5 OOS + 真实L4：base(active)超额 vs comb(候选Q1)超额 比IR。"""
    oos_q = q1_rets[cut:]
    oos_a = active_q1[cut:]
    oos_b = bench[cut:]
    comb_ex = [q - b for q, b in zip(oos_q, oos_b)]
    base_ex = [a - b for a, b in zip(oos_a, oos_b)]
    oos_excess = sum(comb_ex) / len(comb_ex) * 100
    nav, peak, dd = 1.0, 1.0, 0.0
    for r in oos_q:
        nav *= (1 + r)
        peak = max(peak, nav)
        dd = max(dd, (peak - nav) / peak)
    l4 = lift_gate(base_ex, comb_ex)
    return {"oos_excess_pct": round(oos_excess, 2), "oos_dd_pct": round(dd * 100, 2), "l4": l4}


def evaluate_vol(fid: str, n: int, hist: dict, per_dates: list[str], bm_map: dict) -> dict:
    rebal_idx = list(range(60, len(per_dates), REBAL))
    ics, corr_list, q1_rets, active_q1, bench_rets = [], [], [], [], []
    for k in range(len(rebal_idx) - 1):
        t0, t1 = rebal_idx[k], rebal_idx[k + 1]
        d0, d1 = per_dates[t0], per_dates[t1]
        if d0 not in bm_map or d1 not in bm_map:
            continue
        snap = []
        for code, closes in hist.items():
            seq = [closes[d] for d in per_dates[: t0 + 1] if d in closes]
            if len(seq) < max(n, 20) + 1 or d0 not in closes or d1 not in closes:
                continue
            v, v20 = _vol(seq, n), _vol(seq, 20)
            if v != v or v20 != v20:
                continue
            snap.append((v, v20, closes[d1] / closes[d0] - 1))
        if len(snap) < 25:
            continue
        ics.append(_spearman([s[0] for s in snap], [s[2] for s in snap]))
        corr_list.append(pearson([s[0] for s in snap], [s[1] for s in snap]))
        q = max(len(snap) // 5, 1)
        srt = sorted(snap, key=lambda s: s[0])
        srt_a = sorted(snap, key=lambda s: s[1])
        q1_rets.append(sum(s[2] for s in srt[:q]) / q - COST_PER_REBAL)
        active_q1.append(sum(s[2] for s in srt_a[:q]) / q - COST_PER_REBAL)
        bench_rets.append(bm_map[d1] / bm_map[d0] - 1)
    n_p = len(ics)
    if n_p < 20:
        return {"fid": fid, "error": f"期数不足({n_p})"}
    mean_ic = sum(ics) / n_p
    ci_lo, ci_hi = bootstrap_ci(ics)
    avg_corr = sum(corr_list) / len(corr_list)
    m = _oos_metrics(q1_rets, active_q1, bench_rets, int(n_p * SPLIT))
    a = _assess(ics, q1_rets, bench_rets, True, n_p, corr_vs_active=avg_corr)
    l2 = abs(mean_ic) >= 0.02 and not (ci_lo <= 0 <= ci_hi)
    l3 = abs(avg_corr) <= 0.9
    l5 = m["oos_excess_pct"] > 0 and m["oos_dd_pct"] < 15
    return {"fid": fid, "n_periods": n_p, "mean_ic": round(mean_ic, 4),
            "ic_ci": [round(ci_lo, 4), round(ci_hi, 4)],
            "p_value": round(ic_pvalue(mean_ic, n_p), 4),
            "corr_vs_active": round(avg_corr, 3),
            "oos_excess_pct": m["oos_excess_pct"], "oos_dd_pct": m["oos_dd_pct"],
            "l2": l2, "l3": l3, "l4": m["l4"]["pass"], "l5": l5, "l4_detail": m["l4"],
            "ir": a["stats"]["ir"], "ic_pos_ratio": a["stats"]["ic_pos_ratio"],
            "ic_ac1": a["stats"]["ic_ac1"], "ic_seg_means": a["stats"]["ic_seg_means"],
            "wf_gate": a["wf"], "review": a["review"],
            "review_pass": a["review_pass"], "wf_pass": a["wf_pass"]}


def evaluate_val(fid: str, col: str, val: dict, hist: dict,
                 per_dates: list[str], bm_map: dict) -> dict:
    """价值/规模族 + 源B残差正交：每期候选对active低波OLS取残差，用残差算增量IC。"""
    rebal_idx = list(range(60, len(per_dates), REBAL))
    ics, corr_list, comb_q1, active_q1, bench_rets = [], [], [], [], []
    for k in range(len(rebal_idx) - 1):
        t0, t1 = rebal_idx[k], rebal_idx[k + 1]
        d0, d1 = per_dates[t0], per_dates[t1]
        if d0 not in bm_map or d1 not in bm_map:
            continue
        snap = []
        for code, closes in hist.items():
            if code not in val or d0 not in val[code] or d0 not in closes or d1 not in closes:
                continue
            vv = val[code][d0].get(col)
            if vv is None or vv != vv or vv <= 0:
                continue
            if col == "PE(TTM)" and vv <= 0:  # 剔除亏损股
                continue
            seq = [closes[d] for d in per_dates[: t0 + 1] if d in closes]
            if len(seq) < 21:
                continue
            v20 = _vol(seq, 20)
            snap.append((vv, v20, closes[d1] / closes[d0] - 1))
        if len(snap) < 25:
            continue
        cand = [s[0] for s in snap]
        act = [s[1] for s in snap]
        fwd = [s[2] for s in snap]
        resid = _residual(cand, act)  # 源B 残差正交
        ics.append(_spearman(resid, fwd))  # 增量IC（剥离低波后）
        corr_list.append(pearson(cand, act))
        q = max(len(snap) // 5, 1)
        # 真实组合：active低波Q1 / active+价值合成Q1（rank和越小越好）
        r_act = _rank(act)
        r_cand = _rank(cand)
        score = [a + c for a, c in zip(r_act, r_cand)]
        order = sorted(range(len(snap)), key=lambda i: score[i])
        act_order = sorted(range(len(snap)), key=lambda i: act[i])
        comb_q1.append(sum(fwd[i] for i in order[:q]) / q - COST_PER_REBAL)
        active_q1.append(sum(fwd[i] for i in act_order[:q]) / q - COST_PER_REBAL)
        bench_rets.append(bm_map[d1] / bm_map[d0] - 1)
    n_p = len(ics)
    if n_p < 20:
        return {"fid": fid, "error": f"期数不足({n_p})"}
    mean_ic = sum(ics) / n_p
    ci_lo, ci_hi = bootstrap_ci(ics)
    avg_corr = sum(corr_list) / len(corr_list)
    m = _oos_metrics(comb_q1, active_q1, bench_rets, int(n_p * SPLIT))
    a = _assess(ics, comb_q1, bench_rets, True, n_p, corr_vs_active=avg_corr)
    l2 = abs(mean_ic) >= 0.02 and not (ci_lo <= 0 <= ci_hi)
    l3 = abs(avg_corr) <= 0.9
    l5 = m["oos_excess_pct"] > 0 and m["oos_dd_pct"] < 15
    return {"fid": fid, "n_periods": n_p, "mean_ic": round(mean_ic, 4),
            "ic_ci": [round(ci_lo, 4), round(ci_hi, 4)],
            "p_value": round(ic_pvalue(mean_ic, n_p), 4),
            "corr_vs_active": round(avg_corr, 3),
            "oos_excess_pct": m["oos_excess_pct"], "oos_dd_pct": m["oos_dd_pct"],
            "l2": l2, "l3": l3, "l4": m["l4"]["pass"], "l5": l5, "l4_detail": m["l4"],
            "ir": a["stats"]["ir"], "ic_pos_ratio": a["stats"]["ic_pos_ratio"],
            "ic_ac1": a["stats"]["ic_ac1"], "ic_seg_means": a["stats"]["ic_seg_means"],
            "wf_gate": a["wf"], "review": a["review"],
            "review_pass": a["review_pass"], "wf_pass": a["wf_pass"],
            "jev": a["jev"]}


def evaluate_size(fid: str, val: dict, hist: dict,
                  per_dates: list[str], bm_map: dict) -> dict:
    """小市值族优化构造：剔最小10%微盘壳 → 取core内10%~30%分位偏小档 → 剔该档最高波动1/3。
    L2/L3在剔壳后全样本算（壳不污染IC），L4/L5用过滤后的可投小市值档。"""
    rebal_idx = list(range(60, len(per_dates), REBAL))
    ics, corr_list, comb_q1, active_q1, bench_rets = [], [], [], [], []
    for k in range(len(rebal_idx) - 1):
        t0, t1 = rebal_idx[k], rebal_idx[k + 1]
        d0, d1 = per_dates[t0], per_dates[t1]
        if d0 not in bm_map or d1 not in bm_map:
            continue
        rows = []
        for code, closes in hist.items():
            if code not in val or d0 not in val[code] or d0 not in closes or d1 not in closes:
                continue
            mc = val[code][d0].get("总市值")
            if mc is None or mc != mc or mc <= 0:
                continue
            seq = [closes[d] for d in per_dates[: t0 + 1] if d in closes]
            if len(seq) < 21:
                continue
            v20 = _vol(seq, 20)
            if v20 != v20:
                continue
            rows.append((mc, v20, closes[d1] / closes[d0] - 1))
        if len(rows) < 40:
            continue
        rows.sort(key=lambda r: r[0])
        shell = max(int(len(rows) * 0.1), 1)  # 剔最小10%微盘壳
        core = rows[shell:]
        n = len(core)
        lo, hi = int(n * 0.10), int(n * 0.30)  # core内偏小一档(10%~30%分位)
        cand = [r[0] for r in core]
        act = [r[1] for r in core]
        fwd = [r[2] for r in core]
        tier_idx = list(range(lo, hi))
        tier_idx.sort(key=lambda i: act[i])  # 波动升序，剔最高1/3
        keep_idx = tier_idx[: max(len(tier_idx) * 2 // 3, 1)]
        resid = _residual(cand, act)
        ics.append(_spearman(resid, fwd))
        corr_list.append(pearson(cand, act))
        q = max(n // 5, 1)
        act_order = sorted(range(n), key=lambda i: act[i])
        base_idx = act_order[:q]  # 纯低波Q1（base）
        active_q1.append(sum(fwd[i] for i in base_idx) / q - COST_PER_REBAL)
        merged = list(set(base_idx) | set(keep_idx))  # 低波Q1 ∪ 过滤后小市值档（合并组合）
        comb_q1.append(sum(fwd[i] for i in merged) / len(merged) - COST_PER_REBAL)
        bench_rets.append(bm_map[d1] / bm_map[d0] - 1)
    n_p = len(ics)
    if n_p < 20:
        return {"fid": fid, "error": f"期数不足({n_p})"}
    mean_ic = sum(ics) / n_p
    ci_lo, ci_hi = bootstrap_ci(ics)
    avg_corr = sum(corr_list) / len(corr_list)
    m = _oos_metrics(comb_q1, active_q1, bench_rets, int(n_p * SPLIT))
    a = _assess(ics, comb_q1, bench_rets, True, n_p, corr_vs_active=avg_corr)
    l2 = abs(mean_ic) >= 0.02 and not (ci_lo <= 0 <= ci_hi)
    l3 = abs(avg_corr) <= 0.9
    l5 = m["oos_excess_pct"] > 0 and m["oos_dd_pct"] < 15
    return {"fid": fid, "n_periods": n_p, "mean_ic": round(mean_ic, 4),
            "ic_ci": [round(ci_lo, 4), round(ci_hi, 4)],
            "p_value": round(ic_pvalue(mean_ic, n_p), 4),
            "corr_vs_active": round(avg_corr, 3),
            "oos_excess_pct": m["oos_excess_pct"], "oos_dd_pct": m["oos_dd_pct"],
            "l2": l2, "l3": l3, "l4": m["l4"]["pass"], "l5": l5, "l4_detail": m["l4"],
            "ir": a["stats"]["ir"], "ic_pos_ratio": a["stats"]["ic_pos_ratio"],
            "ic_ac1": a["stats"]["ic_ac1"], "ic_seg_means": a["stats"]["ic_seg_means"],
            "wf_gate": a["wf"], "review": a["review"],
            "review_pass": a["review_pass"], "wf_pass": a["wf_pass"],
            "jev": a["jev"]}


def main() -> None:
    lib = FactorLibrary(LIB)
    # 确保价值/规模族入 candidate（规范台账记录）
    for fid, col in VAL_FACTORS.items():
        if lib.get(fid) is None:
            family = "size" if col == "总市值" else "value"
            rec = empty_record(fid, f"低{col}", family)
            rec["formula"] = f"截面升序{col}"
            rec["direction"] = "asc"
            rec["pool"] = INDEX_CODE
            rec["logic"] = "源B对低波OLS残差正交后检验增量IC"
            lib.upsert(rec)
    hist, per_dates, bm_map = load_panel()
    val = load_val_panel(list(hist.keys()))
    results = []
    # 待检集合：candidate + 强制在当前池重检的价值/规模族（池变后旧rejected结论需复核）
    todo = {r["id"] for r in lib.by_status("candidate")}
    todo |= set(VAL_FACTORS)
    for fid in todo:
        rec = lib.get(fid)
        if rec is None:
            continue
        if fid in SKIP_PIT:
            results.append({"fid": fid, "skip": "无披露日历PIT财务库，待专业源"})
            continue
        if fid in SKIP_RULE:
            results.append({"fid": fid, "skip": "规则过滤因子，待人工规则化"})
            continue
        if fid in VOL_FACTORS:
            r = evaluate_vol(fid, VOL_FACTORS[fid], hist, per_dates, bm_map)
        elif fid == "mcap_inv":
            r = evaluate_size(fid, val, hist, per_dates, bm_map)
        elif fid in VAL_FACTORS:
            r = evaluate_val(fid, VAL_FACTORS[fid], val, hist, per_dates, bm_map)
        else:
            results.append({"fid": fid, "skip": "非可算因子，跳过"})
            continue
        results.append(r)
        if "error" in r:
            continue
        admit = (r["l2"] and r["l3"] and r["l4"] and r["l5"]
                 and r["review_pass"] and r["wf_pass"])
        rec = lib.get(fid)
        rec.setdefault("metrics", {}).update({
            "pool": INDEX_CODE, "mean_ic": r["mean_ic"], "ic_ci": r["ic_ci"],
            "ir": r["ir"], "ic_pos_ratio": r["ic_pos_ratio"], "ic_ac1": r["ic_ac1"],
            "ic_seg_means": r["ic_seg_means"],
            "corr_vs_active": r["corr_vs_active"],
            "oos_excess_pct": r["oos_excess_pct"], "oos_dd_pct": r["oos_dd_pct"],
            "l4": r["l4_detail"], "wf_gate": r["wf_gate"], "review": r["review"],
        })
        if admit:
            lib.set_status(fid, "active",
                           f"[{INDEX_CODE}] L2/L3/L4/L5+审查+WF全过 IR={r['ir']} "
                           f"OOS超额{r['oos_excess_pct']}% 回撤{r['oos_dd_pct']}% "
                           f"WF={r['wf_gate']['level']}")
        else:
            why = []
            if not r["l2"]:
                why.append("L2时序/CI不过")
            if not r["l3"]:
                why.append("L3与active冗余")
            if not r["l4"]:
                why.append("L4无lift增量")
            if not r["l5"]:
                why.append("L5 OOS不过")
            if not r["review_pass"]:
                why.extend("审查:" + h for h in r["review"]["hard"])
            if not r["wf_pass"]:
                why.append(f"WF门={r['wf_gate']['level']}({r['wf_gate']['reason']})")
            lib.set_status(fid, "rejected", f"[{INDEX_CODE}] " + ";".join(why))
    lib.save()

    now = datetime.datetime.now(datetime.timezone.utc)
    y, w, _ = now.isocalendar()
    out_dir = WS / "factor_batch_output"
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"# 批量检验周报 {y}W{w:02d}（{now.strftime('%Y-%m-%d')} UTC，池={INDEX_CODE}）", "",
             f"库存：{lib.summary()}", ""]
    for r in results:
        if "skip" in r:
            lines.append(f"- `{r['fid']}` 跳过：{r['skip']}")
        elif "error" in r:
            lines.append(f"- `{r['fid']}` 失败：{r['error']}")
        else:
            ok = (r["l2"] and r["l3"] and r["l4"] and r["l5"]
                  and r["review_pass"] and r["wf_pass"])
            tag = "✅active" if ok else "❌rejected"
            lines.append(
                f"- `{r['fid']}` {tag} | 增量IC {r['mean_ic']} IR={r['ir']} "
                f"正IC占比{r['ic_pos_ratio']} CI{r['ic_ci']} "
                f"corr{r['corr_vs_active']} OOS超额{r['oos_excess_pct']}% "
                f"回撤{r['oos_dd_pct']}% WF={r['wf_gate']['level']} | "
                f"L2={r['l2']} L3={r['l3']} L4={r['l4']} L5={r['l5']} "
                f"审查={r['review_pass']} WF={r['wf_pass']}")
            if r["review"]["hard"]:
                lines.append(f"    - 审查否决：{'；'.join(r['review']['hard'])}")
            if r["review"]["warn"]:
                lines.append(f"    - 提示：{'；'.join(r['review']['warn'])}")
    (out_dir / f"batch_{y}W{w:02d}.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"BATCH_DONE {lib.summary()} results={len(results)}")


if __name__ == "__main__":
    main()
