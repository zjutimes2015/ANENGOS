"""因子库（P1 地基）：结构化台账 + 状态机 + 过拟合校正纯函数。

设计对应《A股因子研究笔记》§2.1：
- 每个因子一张卡（公式/池/频率/方向/PIT/逻辑/证据/状态），缺项不受理；
- 状态机：candidate → active / rejected；active → retired；rejected/retired 可复检回 candidate；
- L2 校正：bootstrap IC 置信区间 + BH(Benjamini-Hochberg) 多重检验校正，在入库时裁决。

库文件：<workspace>/factor_library.json（持久挂载，不进 git 机密）。
本模块只做台账与统计，不拉行情；行情检验仍由 quant_multifactor.run_factor_test 产出。
"""
from __future__ import annotations

import json
import math
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 状态机
STATUSES = ("candidate", "active", "rejected", "retired")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 统计纯函数（云上无 scipy，全部纯 python）
# ---------------------------------------------------------------------------
def _norm_cdf(x: float) -> float:
    """标准正态 CDF 近似（Abramowitz & Stegun 7.1.26，误差<7.5e-8）。"""
    if x >= 0:
        return 1 - _norm_pdf_tail(x)
    return _norm_pdf_tail(-x)


def _norm_pdf_tail(x: float) -> float:
    t = 1.0 / (1.0 + 0.2316419 * x)
    pdf = math.exp(-x * x / 2) / math.sqrt(2 * math.pi)
    poly = (0.319381530 * t - 0.356563782 * t * t + 1.781477937 * t ** 3
            - 1.821255978 * t ** 4 + 1.330274429 * t ** 5)
    return pdf * poly


def ic_pvalue(ic: float, n: int) -> float:
    """由相关系数 IC 近似双侧 p 值（t = r*sqrt((n-2)/(1-r^2))，正态近似）。"""
    if n < 3 or abs(ic) >= 1:
        return 1.0
    t = abs(ic) * math.sqrt((n - 2) / (1 - ic * ic))
    return 2 * (1 - _norm_cdf(t))


def bh_adjust(pvalues: list[float]) -> list[float]:
    """Benjamini-Hochberg 校正，返回与输入等长的 q 值（多重检验控制 FDR）。"""
    m = len(pvalues)
    order = sorted(range(m), key=lambda i: pvalues[i])
    q = [1.0] * m
    prev = 1.0
    for rank_idx in range(m - 1, -1, -1):
        i = order[rank_idx]
        rank = rank_idx + 1
        val = min(prev, pvalues[i] * m / rank)
        q[i] = val
        prev = val
    return [min(x, 1.0) for x in q]


def bootstrap_ci(values: list[float], n_boot: int = 1000, seed: int = 42,
                alpha: float = 0.05) -> tuple[float, float]:
    """对 IC 序列有放回重采样，返回均值的 (lower, upper) 分位置信区间。"""
    if len(values) < 3:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    means = []
    n = len(values)
    for _ in range(n_boot):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    lo = means[int(alpha / 2 * n_boot)]
    hi = means[int((1 - alpha / 2) * n_boot)]
    return (lo, hi)


# ---------------------------------------------------------------------------
# P2：L3 相关去冗 + L4 lift 增量门（纯函数，面板由批量挖掘流程喂入）
# ---------------------------------------------------------------------------
def pearson(xs: list[float], ys: list[float]) -> float:
    """皮尔逊相关系数（L3 截面去冗用）；样本对齐、剔除 None。"""
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    n = len(pairs)
    if n < 5:
        return 0.0
    mx = sum(p[0] for p in pairs) / n
    my = sum(p[1] for p in pairs) / n
    cov = sum((a - mx) * (b - my) for a, b in pairs) / n
    sx = (sum((a - mx) ** 2 for a, _ in pairs) / n) ** 0.5
    sy = (sum((b - my) ** 2 for _, b in pairs) / n) ** 0.5
    return cov / (sx * sy) if sx and sy else 0.0


def redundancy_gate(corr: float, thr: float = 0.9) -> bool:
    """L3：与任一 active 因子平均截面 |corr|>thr → 冗余，拒。"""
    return abs(corr) > thr


def info_ratio(excess: list[float]) -> float:
    """超额收益序列的信息比率（均值/标准差），无风险利率取0。"""
    n = len(excess)
    if n < 3:
        return 0.0
    m = sum(excess) / n
    sd = (sum((e - m) ** 2 for e in excess) / (n - 1)) ** 0.5
    return m / sd if sd else 0.0


def lift_gate(base_excess: list[float], comb_excess: list[float],
              min_delta_ir: float = 0.0) -> dict:
    """L4：候选加入既有 active 组合后，信息比率是否净提升（看增量，不看单因子IC）。

    pass = comb_ir > 0 且 comb_ir - base_ir > min_delta_ir。
    """
    bi = info_ratio(base_excess)
    ci = info_ratio(comb_excess)
    delta = ci - bi
    return {"base_ir": round(bi, 4), "comb_ir": round(ci, 4),
            "delta_ir": round(delta, 4),
            "pass": bool(ci > 0 and delta > min_delta_ir)}


def evaluate_admission(lib: "FactorLibrary", fid: str,
                       corr_to_active: float,
                       base_excess: list[float], comb_excess: list[float],
                       min_delta_ir: float = 0.0) -> dict:
    """P2 入库裁决：L3去冗 + L4 lift增量，两道都过才 admit，结果回写台账。

    - corr_to_active：候选与库内 active 因子的平均截面相关（单active直接给，多active取max|corr|）；
    - base_excess / comb_excess：既有active组合 / 加入候选后组合的超额收益序列。
    """
    rec = lib.get(fid)
    if rec is None:
        raise KeyError(fid)
    l3_redundant = redundancy_gate(corr_to_active)
    l4 = lift_gate(base_excess, comb_excess, min_delta_ir)
    rec.setdefault("metrics", {})
    rec["metrics"]["corr_to_active"] = round(corr_to_active, 3)
    rec["metrics"]["lift"] = l4
    admit = (not l3_redundant) and l4["pass"]
    verdict = "active" if admit else "rejected"
    lib.set_status(fid, verdict,
                   f"L3冗余={l3_redundant}; L4 delta_ir={l4['delta_ir']}")
    return {"fid": fid, "l3_redundant": l3_redundant, "l4": l4, "admit": admit}


# ---------------------------------------------------------------------------
# 台账
# ---------------------------------------------------------------------------
def empty_record(fid: str, name: str, family: str) -> dict[str, Any]:
    return {
        "id": fid, "name": name, "family": family,
        "pool": [], "freq": "", "formula": "", "direction": "",
        "pit": False, "logic": "", "status": "candidate",
        "metrics": {}, "history": [],
        "created": _today(), "updated": _today(),
    }


class FactorLibrary:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data: dict[str, Any] = {"version": 1, "factors": {}}
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                pass

    # -- 持久化 --
    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    # -- 读写 --
    def get(self, fid: str) -> dict[str, Any] | None:
        return self.data["factors"].get(fid)

    def upsert(self, rec: dict[str, Any]) -> None:
        fid = rec["id"]
        old = self.data["factors"].get(fid, {})
        rec["updated"] = _today()
        rec.setdefault("created", old.get("created", _today()))
        self.data["factors"][fid] = rec

    def set_status(self, fid: str, status: str, note: str = "") -> None:
        if status not in STATUSES:
            raise ValueError(f"bad status {status}")
        rec = self.data["factors"].get(fid)
        if rec is None:
            raise KeyError(fid)
        old = rec["status"]
        rec["status"] = status
        rec["updated"] = _today()
        if old != status:
            ev = {"date": _today(), "event": f"→{status}"}
            if note:
                ev["note"] = note
            rec.setdefault("history", []).append(ev)

    def by_status(self, status: str) -> list[dict[str, Any]]:
        return [r for r in self.data["factors"].values() if r["status"] == status]

    def summary(self) -> dict[str, int]:
        out: dict[str, int] = {s: 0 for s in STATUSES}
        for r in self.data["factors"].values():
            out[r["status"]] = out.get(r["status"], 0) + 1
        return out


# ---------------------------------------------------------------------------
# P1 初始归档种子（已检验历史结论，幂等 upsert；新检验由闸门流程追加）
# ---------------------------------------------------------------------------
def seed_initial(lib: FactorLibrary) -> None:
    """把 #48/#53/#56/#57 已检验因子归档进库。重跑幂等。"""
    def add(rec: dict, status: str, note: str, metrics: dict, history: list[dict]) -> None:
        rec["status"] = status
        rec["metrics"] = metrics
        rec["history"] = history
        lib.upsert(rec)
        lib.set_status(rec["id"], status, note)

    r = empty_record("vol20", "20日低波动", "low_vol")
    r.update(pool=["000300", "000905"], freq="5d",
             formula="std(日收益率,20)", direction="asc(低波优先)", pit=True,
             logic="低波防御异象：高波动股下期回撤大，低波熊市抗跌")
    add(r, "active", "B重构+#56复核，ROBUST",
        {"mean_ic": -0.0602, "spread": -39.76, "oos_excess_pct": 8.21,
         "oos_dd_pct": 13.01, "verdict": "ROBUST"},
        [{"date": "2026-10-18", "event": "B重构单因子检验有效"},
         {"date": "2026-10-18", "event": "OOS门 ROBUST"},
         {"date": "2026-10-19", "event": "#56时序复核IC-0.0602"}])

    for fid, name, fam, formula, logic, m in [
        ("net_margin", "销售净利率", "quality", "净利率截面排序", "高净利率盈利质量优", {"static_ic": 0.2218, "n": 247}),
        ("roe", "加权ROE", "quality", "ROE截面排序", "高ROE资本回报优", {"static_ic": 0.1781, "n": 295}),
        ("pe_ttm", "低PE(TTM)", "value", "PE升序(低PE优先)", "低估值价值修复", {"static_ic": -0.1728, "n": 267}),
        ("np_growth", "净利增长率", "growth", "净利增长降序", "高成长", {"static_ic": 0.1269, "n": 250}),
    ]:
        rr = empty_record(fid, name, fam)
        rr.update(pool=["000300"], freq="static", formula=formula,
                  direction="见公式", pit=False, logic=logic)
        add(rr, "candidate", "静态截面有效，缺PIT财务库未过时序门", m,
            [{"date": "2026-10-19", "event": "#53静态截面检验"}])

    for fid, name, fam, formula, note, m in [
        ("mom5", "5日动量", "momentum", "close/close[-6]-1", "时序IC+0.0092无效", {"mean_ic": 0.0092, "verdict": "无效"}),
        ("mom20", "20日动量", "momentum", "close/close[-21]-1", "时序IC-0.0088无效", {"mean_ic": -0.0088, "verdict": "无效"}),
        ("mom60", "60日动量", "momentum", "close/close[-60]-1", "时序IC-0.0088无效，静态IC虚高", {"mean_ic": -0.0088, "static_ic": 0.469, "verdict": "无效"}),
        ("dist_high", "超跌反转(距60日新高)", "reversal", "1-close/max60", "OOS WEAK：超额-18.70%、回撤34%，接飞刀", {"mean_ic": -0.0203, "oos_excess_pct": -18.7, "verdict": "WEAK"}),
        ("screener_rsi", "RSI<45且收盘>MA20", "mean_revert", "RSI(14)<45 & close>MA20", "OVERFITTED：IS+59.63%/OOS-16.15%", {"oos_excess_pct": -16.15, "verdict": "OVERFITTED"}),
        ("rev_growth", "收入增长率", "growth", "主营业务收入增速", "静态IC-0.0089无效", {"static_ic": -0.0089, "verdict": "无效"}),
        ("mcap", "总市值", "style", "总市值", "单窗口大盘风格暴露，非alpha，不取", {"static_ic": 0.4514, "verdict": "风格暴露"}),
        ("pb", "市净率", "value", "PB升序", "弱辅助IC-0.072，不足独立入库", {"static_ic": -0.072, "verdict": "弱辅助"}),
    ]:
        rr = empty_record(fid, name, fam)
        rr.update(pool=["000300"], freq="5d", formula=formula, direction="", pit=True, logic="")
        add(rr, "rejected", note, m, [{"date": "2026-10-19", "event": "门证伪归档"}])
