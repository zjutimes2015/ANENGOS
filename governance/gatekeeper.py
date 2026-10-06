"""Gatekeeper：能力检查 + 异步审批决策（simulate-first）。

GateDecision 表达三类结果：
- 拒绝（未介绍该资源）
- 放行且真实执行（无副作用）
- 模拟放行并排队审批（有副作用，agent 不阻塞）
"""

from __future__ import annotations

from dataclasses import dataclass, field

from governance.capability import Capability, CapabilityRegistry


@dataclass
class GateDecision:
    allowed: bool
    reason: str = ""
    simulate: bool = False  # 是否先以模拟结果放行
    queue_approval: bool = False  # 是否排队等待用户审批
    capability: Capability | None = None


class Gatekeeper:
    """每个外部服务的 Gatekeeper 包装能力检查与副作用判定。"""

    def __init__(self, registry: CapabilityRegistry, service: str) -> None:
        self.registry = registry
        self.service = service

    def check(self, actor: str, tool: str) -> GateDecision:
        cap = self.registry.resolve(actor, tool)
        if cap is None:
            return GateDecision(allowed=False, reason=f"{actor} 未被介绍使用 {tool}")
        if cap.side_effect:
            # 有副作用：模拟放行、排队审批，agent 继续推进
            return GateDecision(
                allowed=True,
                simulate=True,
                queue_approval=True,
                capability=cap,
                reason="有副作用，已模拟执行，等待用户审批",
            )
        return GateDecision(allowed=True, simulate=False, capability=cap, reason="只读放行")
