"""JEV式置信度护栏（零API依赖，纯规则模拟）。

核心理念（学JEV）：
- 不问"过不过"，问"有多确定"
- 高置信→自动执行，中置信→人工审查，低置信→拒绝
- 回退是设计的一部分，不是失败

用法：
  from connectors.jev_guardrail import jev_factor_verdict
  verdict = jev_factor_verdict(ic=0.03, oos_sharpe=1.5, is_sharpe=1.2, corr=0.3)
  # verdict: {"confidence": "high", "action": "auto_promote", "reason": "..."}
"""
from __future__ import annotations
from dataclasses import dataclass, asdict


@dataclass
class JevVerdict:
    confidence: str   # high / medium / low
    action: str        # auto_promote / human_review / reject
    reason: str
    score: float      # 0-100 置信分


def jev_factor_verdict(
    ic: float,
    oos_sharpe: float,
    is_sharpe: float,
    corr: float,
    cost_net_sharpe: float | None = None,
) -> dict:
    """给因子检验结果打置信度。

    五个窄问题（学JEV：五个窄问题胜过一个宽问题）：
    Q1: IC够大吗？          → |IC| >= 0.03 满分，>=0.02及格
    Q2: OOS夏普够大吗？     → OOS夏普 >= 1.5 满分，>=0.8及格
    Q3: OOS比IS好太多吗？   → OOS/IS > 1.5 警告（过拟合信号）
    Q4: 相关性够低吗？       → |corr| < 0.5 满分，<0.7及格
    Q5: 成本后还赚钱吗？     → 净夏普 > 0.5 满分，>0及格
    """
    score = 0.0
    reasons = []

    # Q1: IC
    if abs(ic) >= 0.03:
        score += 20
        reasons.append(f"IC={ic:.3f} 优秀")
    elif abs(ic) >= 0.02:
        score += 12
        reasons.append(f"IC={ic:.3f} 及格")
    else:
        reasons.append(f"IC={ic:.3f} 不足")

    # Q2: OOS夏普
    if oos_sharpe >= 1.5:
        score += 25
        reasons.append(f"OOS夏普={oos_sharpe:.2f} 优秀")
    elif oos_sharpe >= 0.8:
        score += 15
        reasons.append(f"OOS夏普={oos_sharpe:.2f} 及格")
    else:
        reasons.append(f"OOS夏普={oos_sharpe:.2f} 不足")

    # Q3: 过拟合检测（OOS比IS好太多=警告）
    if is_sharpe > 0:
        ratio = oos_sharpe / is_sharpe
        if ratio > 1.5:
            score -= 15
            reasons.append(f"OOS/IS={ratio:.1f} 过高（过拟合信号）")
        elif ratio < 0.5:
            score -= 5
            reasons.append(f"OOS/IS={ratio:.1f} 过低（IS过拟合）")
        else:
            score += 10
            reasons.append(f"OOS/IS={ratio:.1f} 合理")

    # Q4: 相关性
    if abs(corr) < 0.5:
        score += 20
        reasons.append(f"相关性={corr:.2f} 低（独立alpha）")
    elif abs(corr) < 0.7:
        score += 10
        reasons.append(f"相关性={corr:.2f} 中等")
    else:
        score -= 10
        reasons.append(f"相关性={corr:.2f} 高（不独立）")

    # Q5: 成本后净夏普
    if cost_net_sharpe is not None:
        if cost_net_sharpe >= 0.5:
            score += 15
            reasons.append(f"净夏普={cost_net_sharpe:.2f} 优秀")
        elif cost_net_sharpe >= 0:
            score += 8
            reasons.append(f"净夏普={cost_net_sharpe:.2f} 微利")
        else:
            score -= 10
            reasons.append(f"净夏普={cost_net_sharpe:.2f} 成本吃光")

    # 判定置信度
    if score >= 70:
        confidence = "high"
        action = "auto_promote"
    elif score >= 45:
        confidence = "medium"
        action = "human_review"
    else:
        confidence = "low"
        action = "reject"

    return asdict(JevVerdict(
        confidence=confidence,
        action=action,
        reason="; ".join(reasons),
        score=round(score, 1),
    ))


def jev_trade_check(
    trade_reason: str,
    regime: str,
    position_pct: float,
    single_stock_pct: float,
) -> dict:
    """交易日志自查（学JEV护栏）。

    三个问题：
    Q1: 这笔交易符合策略吗？
    Q2: regime对吗？（risk_off不该买成长）
    Q3: 风控合规吗？（单票<20%，总仓位<80%）
    """
    score = 0.0
    reasons = []

    # Q1: 交易理由是否明确
    if trade_reason and len(trade_reason) > 5:
        score += 30
        reasons.append(f"交易理由明确：{trade_reason[:30]}")
    else:
        reasons.append("交易理由模糊")

    # Q2: regime匹配
    if regime == "risk_on":
        score += 25
        reasons.append("risk_on 适合加仓")
    elif regime == "risk_off":
        score -= 10
        reasons.append("risk_off 建议降仓")
    else:
        reasons.append("regime不明")

    # Q3: 风控合规
    if single_stock_pct <= 0.20:
        score += 20
        reasons.append(f"单票{single_stock_pct*100:.0f}% 合规")
    else:
        score -= 20
        reasons.append(f"单票{single_stock_pct*100:.0f}% 超限")

    if position_pct <= 0.80:
        score += 15
        reasons.append(f"总仓位{position_pct*100:.0f}% 合规")
    else:
        score -= 15
        reasons.append(f"总仓位{position_pct*100:.0f}% 超限")

    if score >= 60:
        confidence = "high"
        action = "pass"
    elif score >= 30:
        confidence = "medium"
        action = "warn"
    else:
        confidence = "low"
        action = "violation"

    return asdict(JevVerdict(
        confidence=confidence,
        action=action,
        reason="; ".join(reasons),
        score=round(score, 1),
    ))


if __name__ == "__main__":
    # 自测
    print("=== 因子检验自测 ===")
    test_cases = [
        {"ic": 0.035, "oos_sharpe": 1.6, "is_sharpe": 1.2, "corr": 0.3, "cost_net_sharpe": 0.6},
        {"ic": 0.022, "oos_sharpe": 0.9, "is_sharpe": 1.5, "corr": 0.6, "cost_net_sharpe": 0.2},
        {"ic": 0.015, "oos_sharpe": 0.5, "is_sharpe": 0.8, "corr": 0.8, "cost_net_sharpe": -0.1},
    ]
    for tc in test_cases:
        v = jev_factor_verdict(**tc)
        print(f"  score={v['score']:5.1f}  conf={v['confidence']:6s}  action={v['action']:15s}  {v['reason']}")

    print("\n=== 交易自查自测 ===")
    trade_cases = [
        {"trade_reason": "amt20排名前10", "regime": "risk_on", "position_pct": 0.5, "single_stock_pct": 0.15},
        {"trade_reason": "感觉要涨", "regime": "risk_off", "position_pct": 0.9, "single_stock_pct": 0.25},
    ]
    for tc in trade_cases:
        v = jev_trade_check(**tc)
        print(f"  score={v['score']:5.1f}  conf={v['confidence']:6s}  action={v['action']:15s}  {v['reason']}")
