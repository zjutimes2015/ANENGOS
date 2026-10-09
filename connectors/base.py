"""外部智能体统一接口（多智能体总装线的「适配器层」）。

总装线目标：把 Grokbot / 豆包工作 / Muse / WorkBuddy / Codex 等外部智能体
纳入 ANENGOS 统一指挥。每个外部智能体实现 AgentAdapter：
- submit(task, workspace) -> 执行任务并返回结果摘要（同步语义，便于治理闭环）

治理：适配器以工具形式注册进内核（如 codex.submit），由 Gatekeeper 判定
副作用（外部执行一律视为有副作用 -> 模拟放行 + 审批），批准后真实调用
submit；审计全程留痕。
"""

from __future__ import annotations

from typing import Protocol


class AgentAdapter(Protocol):
    """外部智能体适配器统一协议。"""

    name: str

    def submit(self, task: str, workspace: str) -> str:
        """把任务交给外部智能体执行，返回结果摘要。"""
        ...
