"""异步审批（simulate-first）：有副作用的动作先模拟放行，用户稍后批量批准/拒绝。

解决「同步审批卡死 agent」的问题：agent 不需要停下来等人批准，
而是拿到模拟结果继续推进；人审的是风险，不是每个点击。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass
class ApprovalItem:
    request_id: str
    actor: str
    tool: str
    args: dict[str, Any]
    simulated_result: str
    status: ApprovalStatus = ApprovalStatus.PENDING


class ApprovalQueue:
    """待审批队列 + 模拟执行器。"""

    def __init__(self) -> None:
        self.items: list[ApprovalItem] = []
        self._seq = 0

    def submit(
        self,
        actor: str,
        tool: str,
        args: dict[str, Any],
        simulator: Callable[[str, dict[str, Any]], str],
    ) -> ApprovalItem:
        """提交副作用动作：模拟执行并排队，返回排队项。"""
        self._seq += 1
        item = ApprovalItem(
            request_id=f"req-{self._seq}",
            actor=actor,
            tool=tool,
            args=args,
            simulated_result=simulator(tool, args),
        )
        self.items.append(item)
        return item

    def approve(self, request_id: str) -> bool:
        for item in self.items:
            if item.request_id == request_id and item.status == ApprovalStatus.PENDING:
                item.status = ApprovalStatus.APPROVED
                return True
        return False

    def reject(self, request_id: str) -> bool:
        for item in self.items:
            if item.request_id == request_id and item.status == ApprovalStatus.PENDING:
                item.status = ApprovalStatus.REJECTED
                return True
        return False

    def approve_all(self) -> int:
        n = 0
        for item in self.items:
            if item.status == ApprovalStatus.PENDING:
                item.status = ApprovalStatus.APPROVED
                n += 1
        return n

    def pending(self) -> list[ApprovalItem]:
        return [i for i in self.items if i.status == ApprovalStatus.PENDING]
