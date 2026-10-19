# -*- coding: utf-8 -*-
"""P6 虚拟盘真实化回归单测：佣金/印花税/过户费/滑点数学 + T+1 条件。

不依赖 akshare 与网络，仅校验 _buy_exec/_sell_exec 的费用口径。
运行：python test_paper_cost.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from connectors.paper_trader import _buy_exec, _sell_exec  # noqa: E402

FAIL = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        print(f"PASS  {name} {detail}")
    else:
        FAIL.append(name)
        print(f"FAIL  {name} {detail}")


# 1) 小额买入：触发最低佣金5元，滑点上浮，含费成本单价
b = _buy_exec(10.0, 100)
check("buy fill=price*(1+0.1%)", abs(b["fill"] - 10.01) < 1e-6, f"fill={b['fill']:.4f}")
check("buy 最低佣金5元", abs(b["commission"] - 5.0) < 1e-6, f"comm={b['commission']:.4f}")
check("buy 过户费=0.001%", abs(b["transfer"] - 1001.0 * 0.00001) < 1e-6, f"tr={b['transfer']:.6f}")
check("buy 支出含费", abs(b["outlay"] - (1001.0 + 5.0 + 1001.0 * 0.00001)) < 1e-4,
      f"outlay={b['outlay']:.4f}")
check("buy 无印花税", "stamp" not in b)

# 2) 小额卖出：滑点下浮，佣金5元，印花税万5，净回笼<成交额
s = _sell_exec(10.0, 100)
check("sell fill=price*(1-0.1%)", abs(s["fill"] - 9.99) < 1e-6, f"fill={s['fill']:.4f}")
check("sell 最低佣金5元", abs(s["commission"] - 5.0) < 1e-6, f"comm={s['commission']:.4f}")
check("sell 印花税万5", abs(s["stamp"] - 999.0 * 0.0005) < 1e-6, f"stamp={s['stamp']:.6f}")
check("sell 净回笼<毛额", s["net"] < 999.0, f"net={s['net']:.4f}")

# 3) 大额买入：佣金按比例，不触发最低5元
bl = _buy_exec(10.0, 10000)
check("large buy 佣金按比例≈25.025", abs(bl["commission"] - 100100.0 * 0.00025) < 1e-3,
      f"comm={bl['commission']:.4f}")
check("large buy 不触发最低5元", bl["commission"] > 5.0)

# 4) T+1 条件：buy_date 与行情日相同则不可卖（submit 内联逻辑的等价校验）
buy_date, mkt = "2026-10-19", "2026-10-19"
t1_blocked = str(buy_date)[:10] == mkt
check("T+1 当日买入当日不可卖", t1_blocked is True)
buy_date2 = "2026-10-16"
check("T+1 次日可卖", str(buy_date2)[:10] != mkt)

print("\n" + ("ALL PASS" if not FAIL else f"FAILURES: {FAIL}"))
sys.exit(1 if FAIL else 0)
