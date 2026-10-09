"""外部智能体统一接口 + 注册器（多智能体总装线的「适配器层」）。

总装线目标：把 Grokbot / 豆包工作 / Codex 等外部智能体纳入 ANENGOS 统一指挥。
每个外部智能体实现 AgentAdapter：
- name          智能体标识（如 codex / doubao / grok）
- submit(task, workspace) -> 执行任务并返回结果摘要

治理：register_adapter 以 `{name}.submit` 注册进 ToolRegistry，由 Gatekeeper
判定副作用（外部执行一律视为有副作用 -> 模拟放行 + 审批），批准后真实调用
submit；产出再交由监督智能体（Reviewer）互审；审计全程留痕。
"""

from __future__ import annotations

from typing import Any, Protocol


class AgentAdapter(Protocol):
    """外部智能体适配器统一协议。"""

    name: str

    def submit(self, task: str, workspace: str) -> str:
        """把任务交给外部智能体执行，返回结果摘要。"""
        ...


def register_adapter(adapter: AgentAdapter, workspace: str, reg) -> None:
    """把任意外部智能体注册为受治理工具 `{name}.submit`。"""

    def _submit(args: dict[str, Any]) -> str:
        return adapter.submit(str(args.get("task", "")), workspace)

    reg.register(
        f"{adapter.name}.submit",
        _submit,
        {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": f"交给 {adapter.name} 的任务"},
                "repo": {"type": "string", "description": "目标仓库路径（可选）"},
            },
            "required": ["task"],
        },
    )
