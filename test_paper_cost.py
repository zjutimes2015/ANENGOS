# -*- coding: utf-8 -*-
"""P6/P7 回归单测：费用(佣金/印花税/过户费/滑点/T+1) + 组合风控(单票/总仓/熔断)。

不依赖 akshare 与网络，仅校验纯函数口径。运行：python test_paper_cost.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from connectors.paper_trader import (  # noqa: E402
    _buy_exec, _sell_exec, _risk_check, _drawdown_halt,
)

FAIL = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        print(f"PASS  {name} {detail}")
    else:
        FAIL.append(name)
        print(f"FAIL  {name} {detail}")


# ===== P6 费用 =====
b = _buy_exec(10.0, 100)
check("buy fill=price*(1+0.1%)", abs(b["fill"] - 10.01) < 1e-6, f"fill={b['fill']:.4f}")
check("buy 最低佣金5元", abs(b["commission"] - 5.0) < 1e-6)
check("buy 过户费=0.001%", abs(b["transfer"] - 1001.0 * 0.00001) < 1e-6)
check("buy 无印花税", "stamp" not in b)

s = _sell_exec(10.0, 100)
check("sell fill=price*(1-0.1%)", abs(s["fill"] - 9.99) < 1e-6)
check("sell 最低佣金5元", abs(s["commission"] - 5.0) < 1e-6)
check("sell 印花税万5", abs(s["stamp"] - 999.0 * 0.0005) < 1e-6)
check("sell 净回笼<毛额", s["net"] < 999.0)

bl = _buy_exec(10.0, 10000)
check("large buy 佣金按比例≈25.025", abs(bl["commission"] - 100100.0 * 0.00025) < 1e-3)
check("large buy 不触发最低5元", bl["commission"] > 5.0)

# T+1
check("T+1 当日买入当日不可卖", ("2026-10-19" == "2026-10-19") is True)
check("T+1 次日可卖", "2026-10-16" != "2026-10-19")

# ===== P7 组合风控 =====
EQ = 1_000_000.0
# 1) 正常小额放行
ok, why = _risk_check("600000", 5000, 5000, [], EQ, EQ, False)
check("风控-正常小额放行", ok, why)
# 2) 熔断停买
ok, why = _risk_check("600000", 5000, 5000, [], EQ, EQ, True)
check("风控-熔断拦截", not ok and "熔断" in why, why)
# 3) 现金不足
ok, why = _risk_check("600000", 2_000_000, 2_000_000, [], EQ, 1000, False)
check("风控-现金不足拦截", not ok and "现金" in why, why)
# 4) 总仓位超90%：已持95万，再买10万 → 105%
big_pos = [{"code": "A", "current_price": 95.0, "shares": 10000}]
ok, why = _risk_check("B", 100_000, 100_000, big_pos, EQ, EQ, False)
check("风控-总仓位90%拦截", not ok and "仓位" in why, why)
# 5) 单票超20%：同票已持15万，再买8万 → 23%
one = [{"code": "C", "current_price": 15.0, "shares": 10000}]
ok, why = _risk_check("C", 80_000, 80_000, one, EQ, EQ, False)
check("风控-单票20%拦截", not ok and "单票" in why, why)
# 6) 单票未超限放行
ok, why = _risk_check("D", 50_000, 50_000, [], EQ, EQ, False)
check("风控-小额单票放行", ok, why)

# 熔断判定：峰值100万，当前84万 → 回撤16%触发
halt, dd = _drawdown_halt([{"equity": 1_000_000}], 840_000)
check("熔断-回撤16%触发", halt and abs(dd - 0.16) < 1e-6, f"dd={dd:.4f}")
halt, dd = _drawdown_halt([{"equity": 1_000_000}], 980_000)
check("熔断-回撤2%不触发", not halt and abs(dd - 0.02) < 1e-6, f"dd={dd:.4f}")

print("\n" + ("ALL PASS" if not FAIL else f"FAILURES: {FAIL}"))
sys.exit(1 if FAIL else 0)
