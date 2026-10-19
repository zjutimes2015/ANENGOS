# -*- coding: utf-8 -*-
"""P8 富途凭据安全单测：回环强制 / 默认只读 / 金额上限预留。不依赖 futu 库与网络。"""
import os, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
FAIL = []


def check(name, cond, detail=""):
    if cond:
        print(f"PASS  {name} {detail}")
    else:
        FAIL.append(name)
        print(f"FAIL  {name} {detail}")


# 回环放行
os.environ["ANENGOS_FUTU_HOST"] = "127.0.0.1"
from connectors.quant_futu import QuantFutuAdapter, READ_ONLY, MAX_ORDER_VALUE  # noqa: E402
a = QuantFutuAdapter()
check("回环127.0.0.1放行", a._loopback is True, a._block_reason)

# 非回环阻断
os.environ["ANENGOS_FUTU_HOST"] = "0.0.0.0"
b = QuantFutuAdapter()
check("0.0.0.0被阻断", b._loopback is False and b.mode == "mock", b._block_reason)
check("阻断有原因说明", "非回环" in b._block_reason)

# 公网IP阻断
os.environ["ANENGOS_FUTU_HOST"] = "81.71.13.143"
c = QuantFutuAdapter()
check("公网IP被阻断", c._loopback is False)

# 默认只读 + 金额上限预留
check("默认READ_ONLY=True", READ_ONLY is True)
check("金额上限已预留且为正", MAX_ORDER_VALUE > 0, f"MAX={MAX_ORDER_VALUE}")

print("\n" + ("ALL PASS" if not FAIL else f"FAILURES: {FAIL}"))
sys.exit(1 if FAIL else 0)
